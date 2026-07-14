"""
c_prepare.py — STEP C: turn a chosen role into an Application Brief for Claude, and log it.

Pick a role from the shortlist (by number, matching Weekly_Job_Matches.md) or paste any job
URL. c_prepare re-fetches the ad live, runs the local LLM transform into a structured brief
(what the role needs, ATS keywords, factual company hooks, a draft alignment), writes an
Application Brief markdown file you paste into the job-search Project, and appends the role to
the tracker (applications/applications.csv).

USAGE
    python c_prepare.py                       # print the numbered shortlist
    python c_prepare.py 3                      # prep shortlist item #3
    python c_prepare.py https://...            # prep any job URL (in the archive or not)
    python c_prepare.py --status <url> applied # update a tracked role's status
    python c_prepare.py --score-tracker        # one-off: backfill scores for OLD url-added rows
                                               # (new url-adds are now scored automatically)
    python c_prepare.py --rebrief <url>        # regenerate the brief for an already-tracked role
    python c_prepare.py --archive-briefs       # sweep settled briefs out of the queue

applications/ is the live queue: it holds a brief only while its role is still worth acting on
(status "interested"). Once a role is applied/rejected/skipped, --status moves its brief into
applications/_archive/ — MOVED, never deleted, and still findable. Tracker snapshots go to
applications/_backups/ (last config.TRACKER_BACKUPS_KEEP kept).

The brief is a HANDOFF: a fresh Claude conversation in the Project (which has the candidate's
master profile) does the final CV + motivation letter. The handoff wording (which profile file
is the source of truth, the letter formula, how to lead each lane) is per-person, set in
profiles/<name>.toml (brief_* keys), NOT hardcoded here. c_prepare does NOT write the
application itself — it assembles honest, structured raw material and never fabricates company
facts or candidate claims.

Settings: config.py. Engine: core.py. See README.md.
"""

import os
import re
import csv
import sys
import shutil
from datetime import datetime, timedelta

import config
import core

# --- the transform: job ad -> structured brief fields -------------------------------------
TRANSFORM_SCHEMA = {
    "type": "object",
    "properties": {
        "role_summary":  {"type": "string"},
        "title":         {"type": "string"},
        "company":       {"type": "string"},
        "location":      {"type": "string"},
        "employment_type": {"type": "string",
                            "enum": ["student", "part_time", "full_time",
                                     "internship", "unknown"]},
        "deadline":      {"type": "string"},
        "must_have":     {"type": "array", "items": {"type": "string"}},
        "nice_to_have":  {"type": "array", "items": {"type": "string"}},
        "responsibilities": {"type": "array", "items": {"type": "string"}},
        "ats_keywords":  {"type": "array", "items": {"type": "string"}},
        "company_facts": {"type": "array", "items": {"type": "string"}},
        "candidate_alignment": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["role_summary", "title", "company", "must_have",
                 "responsibilities", "ats_keywords", "company_facts",
                 "candidate_alignment"],
}

TRANSFORM_NUM_PREDICT = 1500


def _transform_prompt(job_meta: dict, description: str) -> str:
    return f"""You are extracting structured facts from a job ad to help a candidate apply.
Be factual and grounded in the AD TEXT only. Do NOT invent anything not in the ad.

Extract:
  - role_summary       : 1-2 sentences, what this job actually is.
  - title, company, location, employment_type, deadline ("YYYY-MM-DD" or "").
  - must_have          : hard requirements stated in the ad (skills, tools, level, language).
  - nice_to_have       : preferred / bonus qualifications.
  - responsibilities   : the main tasks/duties.
  - ats_keywords       : concrete skills/tools/terms an ATS would scan for, taken from the ad
                         (e.g. "Python", "SQL", "Azure", "DevOps", "stakeholder management").
  - company_facts      : concrete facts STATED IN THE AD that could seed a genuine, specific
                         cover-letter hook — what the company builds, its product, team, tech
                         stack, mission as the ad describes it. Facts only, no flattery, and
                         nothing not in the ad. If the ad says little about the company, return
                         fewer items rather than inventing.
  - candidate_alignment: 3-6 DRAFT bullets mapping the candidate below to THIS role's needs,
                         honestly. Where the candidate clearly lacks a must-have, say so as a
                         gap (e.g. "Gap: ad wants 2 yrs commercial Java; candidate has
                         coursework-level Java"). Do not overstate. These are drafts to verify.

CANDIDATE (for alignment only — do not copy verbatim into output):
{core.CANDIDATE_PROFILE}

JOB AD
Title: {job_meta.get('title', '')}
Company: {job_meta.get('company', '')}
Text:
{description[:5500]}

Respond with ONLY a JSON object matching the requested fields. No markdown, no extra text."""


# --- tracker (applications/applications.csv) ----------------------------------------------
TRACKER_FIELDS = ["date_added", "status", "company", "role", "url", "employment_type",
                  "location", "deadline", "score", "track", "next_followup",
                  "brief_file", "notes"]

STATUSES = ["interested", "applied", "interview", "offer", "hired", "rejected",
            "rejected_after_interview", "skipped"]


def _load_tracker():
    if not os.path.isfile(config.TRACKER_CSV):
        return []
    with open(config.TRACKER_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _tracker_status_map() -> dict:
    """canonical_url -> status, for annotating the shortlist with where each role already
    stands (applied / rejected / skipped / interview / offer / interested). Canonical keys so
    archive rows match their tracker entry regardless of which source's URL variant is stored."""
    out = {}
    for r in _load_tracker():
        cu = core.canonical_url(r.get("url", ""))
        if cu:
            out[cu] = (r.get("status") or "").strip().lower() or "tracked"
    return out


def _tracker_rolekey_map() -> dict:
    """role_key -> status, the cross-source companion to _tracker_status_map. Lets a RE-POST of
    an already-tracked role (same company + same title-token-set, but a DIFFERENT url) be
    recognised on the shortlist even though its url never matched the tracker. First status wins
    so the oldest/most-advanced entry annotates the row."""
    out = {}
    for r in _load_tracker():
        rk = core.role_key({"company": r.get("company", ""), "title": r.get("role", "")})
        if rk:
            out.setdefault(rk, (r.get("status") or "").strip().lower() or "tracked")
    return out


def _tracker_url_match(url: str):
    """The tracker row (if any) already holding this exact role by canonical url — the cheap
    check used to short-circuit a re-prep BEFORE any fetch/scoring/brief work."""
    cu = core.canonical_url(url)
    return next((r for r in _load_tracker()
                 if core.canonical_url(r.get("url", "")) == cu), None)


def _tracker_matches(url: str, company: str, title: str):
    """Find tracker rows that are THIS role. Returns (url_hits, key_hits):
      - url_hits: same canonical url -> definitely the same posting (already handled today).
      - key_hits: same cross-source role_key (company + title-token-set) under a DIFFERENT url
        -> almost certainly a re-post of a role already logged. Excludes anything in url_hits.
    role_key is conservative (exact token set), so a key_hit means 'same role', not 'similar'."""
    cu = core.canonical_url(url)
    rk = core.role_key({"company": company, "title": title})
    url_hits, key_hits = [], []
    for r in _load_tracker():
        if cu and core.canonical_url(r.get("url", "")) == cu:
            url_hits.append(r)
        elif rk and core.role_key(
                {"company": r.get("company", ""), "title": r.get("role", "")}) == rk:
            key_hits.append(r)
    return url_hits, key_hits


def _tracker_company_rows(company: str, exclude_urls=()):
    """Other tracked applications at the same (normalised) employer — the soft 'am I over-applying
    to one company?' heads-up. Company names are normalised the same way as the cross-source key
    ('Monta ApS' == 'Monta'), and rows whose canonical url is in exclude_urls (the current role
    itself, or its matches) are left out so the note only shows OTHER roles."""
    nc = core._norm_company(company)
    if not nc:
        return []
    ex = {core.canonical_url(u) for u in exclude_urls if u}
    return [r for r in _load_tracker()
            if core._norm_company(r.get("company", "")) == nc
            and core.canonical_url(r.get("url", "")) not in ex]


def _append_tracker(row: dict):
    os.makedirs(config.APPLICATIONS_DIR, exist_ok=True)
    new = not os.path.isfile(config.TRACKER_CSV)
    with open(config.TRACKER_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRACKER_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in TRACKER_FIELDS})


# --- status-transition log (append-only; NEVER rewrites, so it's the safe place to capture
#     funnel timing the tracker can't — one row per status change). Lives beside the tracker,
#     profile-aware, and is what b_insights reads for time-to-response.
STATUS_HISTORY_FIELDS = ["date", "url", "company", "role", "old_status", "new_status"]


def _status_history_path() -> str:
    return os.path.join(config.APPLICATIONS_DIR, "status_history.csv")


def _append_status_history(transitions: list):
    """Append one row per real status change to status_history.csv. Append-only: it grows, it is
    never rewritten, so the tracker's no-rewrite constraint is untouched and the timeline is
    tamper-evident. No-op on an empty list."""
    if not transitions:
        return
    path = _status_history_path()
    os.makedirs(config.APPLICATIONS_DIR, exist_ok=True)
    new = not os.path.isfile(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=STATUS_HISTORY_FIELDS)
        if new:
            w.writeheader()
        for t in transitions:
            w.writerow({k: t.get(k, "") for k in STATUS_HISTORY_FIELDS})


def _save_tracker(rows: list):
    """Rewrite applications.csv wholesale, snapshotting it to _backups/ first.

    Every caller here rewrites the WHOLE file, so a crash or a bad row mid-write takes the
    hand-curated history with it. The snapshot is the undo. Rewrites are rare (status change,
    score backfill, re-brief) and only the last TRACKER_BACKUPS_KEEP snapshots are kept."""
    if os.path.isfile(config.TRACKER_CSV):
        _backup_tracker()
    with open(config.TRACKER_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRACKER_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in TRACKER_FIELDS})


def _update_status(url: str, new_status: str) -> bool:
    """Rewrite the tracker with one row's status updated. Returns True if a row matched.
    Matching is on the canonical url, so `--status <clean-or-utm-url> applied` updates the
    existing row even if the stored url carries different tracking params. Every real change
    (old != new) is also appended to status_history.csv so the funnel timeline is captured."""
    cu = core.canonical_url(url)
    ns = (new_status or "").strip().lower()
    rows = _load_tracker()
    hit = False
    transitions = []
    for r in rows:
        if core.canonical_url(r.get("url", "")) == cu:
            old = (r.get("status") or "").strip().lower()
            if old != ns:
                transitions.append({
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "url": r.get("url", ""), "company": r.get("company", ""),
                    "role": r.get("role", ""), "old_status": old, "new_status": ns,
                })
            r["status"] = new_status
            hit = True
    if not hit:
        return False
    _save_tracker(rows)
    _append_status_history(transitions)   # only after the tracker write succeeds
    return True


# --- backfill scores for manually-added roles (make the tracker an eval set) ---------------
_SCOREABLE = ("score", "track", "employment_type")   # tracker columns we may fill


def _fill_blanks(row: dict, vals: dict) -> bool:
    """Set row[k]=v only where the row's current value is blank ('' or '0' for score) and v is
    non-empty. Never overwrites an existing value or any column outside `vals`. Returns whether
    anything changed."""
    changed = False
    for k, v in vals.items():
        if v in (None, ""):
            continue
        cur = (row.get(k) or "").strip()
        if cur == "" or (k == "score" and cur == "0"):
            row[k] = v
            changed = True
    return changed


def _score_role(title, company, location, url, description):
    """Score one role from an ALREADY-FETCHED description (reused by inline prep-scoring and by
    --score-tracker so both behave identically). Returns the score_job dict, or None on an empty
    description or a scoring error."""
    if not (description or "").strip():
        return None
    job = {"title": title or "", "company": company or "", "location": location or "N/A",
           "url": url, "source": "full"}
    res = core.score_job(job, description)
    return None if res.get("reasoning") == "scoring error" else res


def _archive_dir() -> str:
    """Where settled briefs are moved. Resolved from APPLICATIONS_DIR on every call, so it always
    follows the active profile."""
    return os.path.join(config.APPLICATIONS_DIR, "_archive")


def _backup_dir() -> str:
    """Where tracker snapshots live. Resolved per call, for the same reason as _archive_dir()."""
    return os.path.join(config.APPLICATIONS_DIR, "_backups")


def _backup_tracker() -> str:
    """Snapshot applications.csv into _backups/ before a rewrite, then prune old snapshots."""
    os.makedirs(_backup_dir(), exist_ok=True)
    name = f"{os.path.basename(config.TRACKER_CSV)}.bak-{datetime.now():%Y%m%d_%H%M%S}"
    bak = os.path.join(_backup_dir(), name)
    shutil.copy2(config.TRACKER_CSV, bak)
    _prune_tracker_backups(config.TRACKER_BACKUPS_KEEP)
    return bak


def _prune_tracker_backups(keep=3):
    """Delete all but the newest `keep` auto-generated tracker backups.

    Only touches names this script writes (.bak-YYYYMMDD_HHMMSS). Hand-named snapshots such as
    .bak-manualfix-... are never candidates, so a deliberate rescue copy can't be swept away."""
    d = _backup_dir()
    if not keep or not os.path.isdir(d):
        return
    auto = re.compile(re.escape(os.path.basename(config.TRACKER_CSV)) + r"\.bak-\d{8}_\d{6}$")
    baks = sorted(f for f in os.listdir(d) if auto.match(f))  # name sorts == chronological
    for f in baks[:-keep]:
        os.remove(os.path.join(d, f))
        print(f"  [prune]   old backup removed: {f}")


def score_tracker_gaps():
    """Fill the model score for tracker rows added from a URL that was never scraped (e.g. a
    direct company/ATS apply link pasted into `c_prepare.py <url>`). This turns applications.csv
    into a labelled eval set: your status decision next to the model's score.

    Safe by design: backs up applications.csv first; only fills BLANK score/track/
    employment_type; never touches status, notes, dates, or any populated field. Rows whose URL
    can't be fetched (many ATS pages are JS-only) are reported and left blank."""
    rows = _load_tracker()
    todo = [r for r in rows
            if (r.get("score") or "").strip() in ("", "0") and (r.get("url") or "").strip()]
    if not todo:
        print("Every tracker row already has a score. Nothing to backfill.")
        return

    print(f"{len(todo)} tracker row(s) missing a score.")   # _save_tracker snapshots before writing
    print("Scoring (archive lookup, else live fetch + local LLM)...\n")

    live = from_archive = failed = 0
    for r in todo:
        url = r["url"].strip()
        who = (r.get("company", "") or "?")[:24]
        arc = core.canonical_url(url) and _archive_row(url)
        if arc and str(arc.get("score") or "").strip() not in ("", "0"):
            _fill_blanks(r, {"score": str(arc.get("score")), "track": arc.get("track"),
                             "employment_type": arc.get("employment_type")})
            from_archive += 1
            print(f"  [archive] {who:<24} score {arc.get('score')}")
            continue
        desc, err = core.fetch_one(url)
        if not desc:
            failed += 1
            print(f"  [skip]    {who:<24} fetch failed ({(err or 'no body')[:28]}) — left blank")
            continue
        res = _score_role(r.get("role", ""), r.get("company", ""),
                          r.get("location", "N/A"), url, desc)
        if not res:
            failed += 1
            print(f"  [skip]    {who:<24} scoring error — left blank")
            continue
        _fill_blanks(r, {"score": str(res.get("score")), "track": res.get("track"),
                         "employment_type": res.get("employment_type")})
        live += 1
        print(f"  [scored]  {who:<24} score {res.get('score')}  track {res.get('track')}  "
              f"({res.get('employment_type')})")

    _save_tracker(rows)
    print(f"\nDone: {live} scored live, {from_archive} from archive, {failed} left blank "
          f"(usually JS-only ATS pages — paste those in manually if you want them scored).")


# --- archive lookup (for roles already scored) --------------------------------------------
def _archive_row(url: str):
    """Return the MOST RECENTLY SCORED archive row for this URL (tiebreak: higher score), or
    None. Latest-wins matches core._dedup_archive, so the brief reflects the same row the
    shortlist shows. Compared on the canonical url so a role scored under one source's URL
    is found when looked up by another source's variant of the same link."""
    if not os.path.isfile(config.MASTER_ARCHIVE):
        return None
    cu = core.canonical_url(url)
    best = None
    with open(config.MASTER_ARCHIVE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if core.canonical_url(r.get("url", "")) != cu:
                continue
            try:
                r["score"] = int(r.get("score") or 0)
            except ValueError:
                r["score"] = 0
            key = (r.get("scraped_date") or "", r["score"])
            if best is None or key > ((best.get("scraped_date") or ""), best["score"]):
                best = r
    return best


# --- brief assembly -----------------------------------------------------------------------
def _slug(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (s or "").strip())
    s = re.sub(r"[\s_-]+", "_", s)
    return s.strip("_")[:50]


def _brief_slug(company: str, title: str) -> str:
    """Name a brief after the company, else the job title, else the bare 'role'.

    The title fallback is the point: when the transform can't extract a company, slugging
    straight to the constant produced briefs called role.md / role_2.md — a tracker row could
    still point at one, but nobody could tell which job it was by looking."""
    return _slug(company) or _slug(title) or "role"


def _brief_exists(name: str) -> bool:
    """True if this brief filename is taken in EITHER the live queue or the archive. Archived
    briefs still own their name: tracker rows point at briefs by basename, so a name reused after
    a brief was archived would make `brief_file` ambiguous across the two directories."""
    return any(os.path.exists(os.path.join(d, name))
               for d in (config.APPLICATIONS_DIR, _archive_dir()))


def _unique_path(directory: str, base: str) -> str:
    name = base + ".md"
    n = 2
    while _brief_exists(name):
        name = f"{base}_{n}.md"
        n += 1
    return os.path.join(directory, name)


def _find_brief(name: str) -> str:
    """Absolute path of a tracker row's brief, wherever it lives (queue or archive). '' if gone."""
    for d in (config.APPLICATIONS_DIR, _archive_dir()):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return ""


def archive_settled_briefs(quiet: bool = False) -> int:
    """Make the brief files match the tracker, in BOTH directions. Returns how many moved.

    applications/ is the "what do I apply to next" queue, and a brief belongs in it exactly while
    its role's status is in BRIEF_QUEUE_STATUSES:
      - settled (applied / rejected / skipped / ...) -> MOVE the brief out to _archive/
      - back in play (a status edited back to 'interested') -> MOVE it back into the queue

    Files are only ever MOVED, never deleted or rewritten. The reverse direction matters because
    a status can be hand-edited straight into applications.csv either way; a sweep that only
    archived would strand a revived role's brief in _archive/ and quietly leave it out of the
    queue — the exact failure the queue is supposed to prevent."""
    moved = 0
    for r in _load_tracker():
        name = (r.get("brief_file") or "").strip()
        status = (r.get("status") or "").strip()
        if not name:
            continue
        live = status in config.BRIEF_QUEUE_STATUSES
        src = os.path.join(_archive_dir() if live else config.APPLICATIONS_DIR, name)
        dst_dir = config.APPLICATIONS_DIR if live else _archive_dir()
        if not os.path.isfile(src):
            continue                      # already on the right side, or never written
        os.makedirs(dst_dir, exist_ok=True)
        shutil.move(src, os.path.join(dst_dir, name))
        moved += 1
        if not quiet:
            print(f"  [{'restore' if live else 'archive'}] {name}  (status: {status})")
    return moved


def _bullets(items, empty="_(none extracted)_"):
    items = [str(x).strip() for x in (items or []) if str(x).strip()]
    return "\n".join(f"- {x}" for x in items) if items else empty


def _build_brief(meta: dict, tf: dict, description: str, fetch_err) -> str:
    track = meta.get("track", "")
    lane = "A" if track == "A" else ("B" if track == "B" else "?")
    lane_word = {"A": "Lane A (technical)", "B": "Lane B (foot-in-the-door)"}.get(lane, "the matching lane")
    # Per-person handoff wording (config, from the active profile). Neutral fallbacks so a
    # profile that doesn't set them still produces a correct, if generic, brief — never one
    # naming the wrong candidate.
    name = config.BRIEF_NAME
    master_phrase = f"`{config.BRIEF_MASTER_REF}`" if config.BRIEF_MASTER_REF else f"{name}'s master profile"
    default_lead = ("Lead with the strongest, most role-relevant technical evidence."
                    if lane == "A" else
                    "This is a foot-in-the-door role: lead with reliability and stakeholder "
                    "skills, and the intent to grow into technical work.")
    lead = ((config.BRIEF_LEAD_A if lane == "A" else config.BRIEF_LEAD_B) or default_lead)

    score_line = ""
    if meta.get("score"):
        score_line = (f"- **Local scorer:** {meta['score']}/100 · Track {track or '?'}"
                      + (f" — {meta['reasoning']}" if meta.get("reasoning") else "") + "\n")
    matched = meta.get("matched_skills")
    if isinstance(matched, str):
        matched = [m.strip() for m in matched.split(",") if m.strip()]
    matched_line = f"- **Scorer matched skills:** {', '.join(matched)}\n" if matched else ""

    # Danish flag from the CURRENT columns (an earlier version read the removed
    # danish_required boolean here, so every brief said "no / not stated").
    lvl = str(meta.get("danish_level", "")).strip().lower()
    danish_line = {"required": "⚠ required",
                   "preferred": "a plus, not mandatory",
                   "none": "no / not stated"}.get(lvl, "unknown (scored before the "
                                                       "danish_level column — verify in the ad)")
    if str(meta.get("ad_language", "")).lower() == "da":
        danish_line += " · ad written in Danish"
    today = datetime.now().strftime("%Y-%m-%d")

    if description.strip():
        jd_block = description.strip()
    else:
        jd_block = ("⚠ LIVE FETCH FAILED" + (f" ({fetch_err})" if fetch_err else "")
                    + " — PASTE THE FULL JOB DESCRIPTION HERE before handing this to Claude.")

    return f"""# Application Brief — {meta.get('company', '')} — {meta.get('title', '')}

> **HANDOFF TO CLAUDE.** Paste this whole file into the job-search Project. Using
> {master_phrase} as the ONLY source of facts about {name}, produce: **(a)** a tailored
> {lane_word} CV as {config.BRIEF_CV_FORMAT}, and **(b)** a ~250–300 word motivation letter
> using {config.BRIEF_LETTER_FORMULA}. {lead} Pick ONE genuine, specific hook yourself from
> *Company facts* below — never fabricate enthusiasm. Mirror the role's ATS keywords ONLY where
> they are true of {name}. Treat *Alignment draft* as unverified hints: flag any must-have
> {name} doesn't clearly meet instead of papering over it. State nothing {master_phrase} doesn't support.

## Role
- **Company:** {meta.get('company', '')}
- **Title:** {meta.get('title', '')}
- **Location:** {meta.get('location', '') or '—'}
- **Type:** {meta.get('employment_type', '') or '—'} · **Work mode:** {meta.get('work_mode', '') or '—'}
- **Deadline:** {meta.get('deadline', '') or '—'}
- **Danish:** {danish_line}
- **URL:** {meta.get('url', '')}
{score_line}{matched_line}
**What this role is:** {tf.get('role_summary', '_(transform unavailable — read the JD below)_')}

## What the role needs
**Must-have**
{_bullets(tf.get('must_have'))}

**Nice-to-have**
{_bullets(tf.get('nice_to_have'))}

**Responsibilities**
{_bullets(tf.get('responsibilities'))}

**ATS keywords** _(mirror in the CV only where true of {name})_
{_bullets(tf.get('ats_keywords'))}

## Company facts — pick ONE genuine hook (do not invent)
{_bullets(tf.get('company_facts'))}

## Alignment draft — {name} ↔ role _(UNVERIFIED — check against {master_phrase}, don't over-claim)_
{_bullets(tf.get('candidate_alignment'))}

## Full job description (verbatim, fetched {today})
{jd_block}
"""


# --- main flows ---------------------------------------------------------------------------
def _confirm(prompt: str) -> bool:
    """Yes/No prompt, default No. On non-interactive (piped) stdin it defaults to No and says so,
    so an automated run never silently appends a possible-duplicate row."""
    if not sys.stdin.isatty():
        print(f"{prompt} [y/N]  (non-interactive: assuming N)")
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _company_note(company: str, exclude_urls=()):
    """Print a soft heads-up listing OTHER tracked applications at the same employer. Purely
    informational (never blocks) — surfaces 'you already have N open apps here' at prep time."""
    others = _tracker_company_rows(company, exclude_urls)
    if not others:
        return
    print(f"\n  Note: {len(others)} other tracked application(s) at {company or 'this employer'}:")
    for r in others:
        print(f"      {(r.get('role','') or '')[:50]:<50} "
              f"(status: {r.get('status','?')}, added {r.get('date_added','?')})")


def print_shortlist(only_new: bool = False):
    rows = core.open_shortlist(config.MASTER_ARCHIVE)
    if not rows:
        print("Shortlist is empty. Run `python a_scrape.py` first.")
        return
    status_map = _tracker_status_map()
    key_map = _tracker_rolekey_map()

    # Classify every row once. The index is kept stable (same as the full shortlist and the
    # Weekly_Job_Matches.md report), so `c_prepare.py <n>` means the same role in every view.
    # A row is matched by url first, then by cross-source role_key, so a RE-POST of an
    # already-tracked role (new url, same company+title) is no longer flagged as NEW.
    items = []
    for i, r in enumerate(rows, 1):
        st = status_map.get(core.canonical_url(r.get("url", "")))
        if st is None:
            st = key_map.get(core.role_key(
                {"company": r.get("company", ""), "title": r.get("title", "")}))
        kind = "new" if st is None else ("interested" if st == "interested" else st)
        items.append((i, r, kind))
    actionable = [it for it in items if it[2] in ("new", "interested")]

    if only_new:
        print(f"\nNot yet applied — {len(actionable)} role(s) worth prepping "
              f"(numbers match the full shortlist). Prep with:  python c_prepare.py <number>\n")
        if not actionable:
            print("  Nothing new: every open role is already applied/rejected/skipped.\n")
            return
        to_show = actionable
    else:
        print(f"\nOpen shortlist — {len(rows)} roles (score >= {config.SCORE_THRESHOLD}). "
              f"Prep one with:  python c_prepare.py <number>  (or `--new` for just these)\n")
        to_show = items

    for i, r, kind in to_show:
        # validate the deadline so junk like a stray "False" doesn't print as a date
        dl = r.get("deadline") if core._parse_date(r.get("deadline")) else ""
        days = r.get("_days_left")
        when = f"closes in {days}d" if (dl and isinstance(days, int)) else (dl or "no deadline")
        tag = "· NEW" if kind == "new" else f"· {kind}"
        print(f"  {i:>2}. {r['score']:>3} {r.get('track',''):<4} "
              f"{(r.get('employment_type','') or ''):<10} "
              f"{(r.get('company','') or '')[:22]:<22} {(r.get('title','') or '')[:40]:<40} "
              f"{when:<14} {tag}")

    if not only_new:
        if actionable:
            print("\nNot yet applied — worth prepping (same numbers, or run `c_prepare.py --new`):")
            for i, r, kind in actionable:
                print(f"  #{i:>2}  {kind:<10} {(r.get('company','') or '')[:22]:<22} "
                      f"{(r.get('title','') or '')[:50]}")
        else:
            print("\nEvery open role on the shortlist is already tracked (applied/rejected/skipped).")
    print()


def _fetch_and_transform(meta: dict):
    """Fetch the ad live, run the local-LLM transform, and fill blank meta fields from it.
    Mutates `meta` in place. Shared by prepare() and rebrief() so a regenerated brief is built
    exactly the same way as a first-time one. Returns (description, transform_fields, fetch_err)."""
    print(f"Re-fetching live: {meta['url']}")
    description, err = core.fetch_one(meta["url"])
    if err:
        print(f"  ⚠ fetch failed: {err}  (brief will need the JD pasted in manually)")
    else:
        print(f"  fetched {len(description)} chars")

    tf = {}
    if description.strip():
        print("Running local-LLM transform (this hits Ollama)...")
        tf = core.ollama_json(_transform_prompt(meta, description),
                              TRANSFORM_SCHEMA, num_predict=TRANSFORM_NUM_PREDICT) or {}
        if not tf:
            print("  ⚠ transform returned nothing — brief will have the JD but no structured fields.")
    else:
        print("  skipping transform (no description fetched).")

    # Prefer meta we already trust (archive/tracker); fill blanks from the transform.
    for k in ("title", "company", "location", "employment_type", "deadline"):
        if not meta.get(k) or str(meta.get(k)).upper() == "N/A":
            meta[k] = tf.get(k, meta.get(k, ""))
    return description, tf, err


def rebrief(url: str):
    """Regenerate the Application Brief for a role ALREADY in the tracker, and repoint its
    brief_file at the new one.

    prepare() deliberately refuses to touch an exact-URL duplicate (no re-fetch, no new brief),
    which is right for the normal path but leaves no way to recover a brief that was lost, or to
    refresh a stale one. This is that way. It only ever writes the brief and the brief_file cell:
    status, dates and notes are never touched. A settled role's brief is written straight into
    _archive/, so regenerating one can't smuggle a dead role back into the live queue."""
    rows = _load_tracker()
    cu = core.canonical_url(url)
    row = next((r for r in rows if core.canonical_url(r.get("url", "")) == cu), None)
    if not row:
        print("No tracker row matched that URL. Prep it first: python c_prepare.py <url>")
        return None

    status = (row.get("status") or "").strip()
    old = (row.get("brief_file") or "").strip()
    old_path = _find_brief(old) if old else ""
    print(f"Re-briefing: {row.get('company','')} — {row.get('role','')} (status: {status or '?'})")
    print(f"  existing brief: {old_path or (old + '  ⚠ missing on disk') or '(none recorded)'}")

    meta = {"url": row.get("url") or url, "title": row.get("role", ""),
            "company": row.get("company", ""), "location": row.get("location", ""),
            "employment_type": row.get("employment_type", ""), "deadline": row.get("deadline", ""),
            "score": row.get("score", ""), "track": row.get("track", "")}
    description, tf, err = _fetch_and_transform(meta)
    if not description.strip() and not tf:
        print("  ⚠ nothing fetched and no transform — refusing to overwrite with an empty brief.")
        return None

    # Keep the row's original date_added in the filename so the brief still sorts with its row.
    date = (row.get("date_added") or "").strip() or f"{datetime.now():%Y-%m-%d}"
    base = f"{date}_{_brief_slug(meta.get('company', ''), meta.get('title', ''))}"
    dest = config.APPLICATIONS_DIR if status in config.BRIEF_QUEUE_STATUSES else _archive_dir()
    os.makedirs(dest, exist_ok=True)
    path = _unique_path(dest, base)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_build_brief(meta, tf, description, err))
    print(f"  brief -> {path}")

    row["brief_file"] = os.path.basename(path)
    _save_tracker(rows)
    print(f"  tracker brief_file: {old or '(blank)'} -> {row['brief_file']}")
    if old_path and old_path != path:
        print(f"  (the old brief is left in place at {old_path} — delete it yourself if stale)")
    return path


def prepare(meta: dict):
    """meta must have at least 'url' (and ideally title/company/track from the archive)."""
    url = meta["url"]

    # Exact-URL duplicate: this role is already tracked. Skip everything — no re-fetch, no LLM
    # call, no brief. A brief is written ONLY for a role that gets a tracker row, so the dated
    # applications/*.md list stays a clean "what to apply next" queue with no duplicate clutter.
    dup = _tracker_url_match(url)
    if dup:
        bf = (dup.get("brief_file") or "").strip()
        where = _find_brief(bf) if bf else ""      # may have been archived; still findable
        print(f"Already in tracker: {dup.get('company','')} — {dup.get('role','')} "
              f"(status: {dup.get('status','?')}, added {dup.get('date_added','?')}).")
        print("  Skipped — no re-fetch, no new brief." + (f"  Existing brief: {where or bf}" if bf else ""))
        print("  (to change its status use:  python c_prepare.py --status <url> <new-status>)")
        return None

    description, tf, err = _fetch_and_transform(meta)

    # Score the role too, unless it came from the archive already carrying a score. Reuses the
    # description fetched above (no extra fetch), so a URL-added role lands in the tracker as an
    # eval-ready datapoint instead of a blank -- no later --score-tracker needed for it.
    if str(meta.get("score", "")).strip() in ("", "0"):
        res = _score_role(meta.get("title", ""), meta.get("company", ""),
                          meta.get("location", "N/A"), meta.get("url", ""), description)
        if res:
            meta["score"] = str(res.get("score", ""))
            meta["track"] = res.get("track", "") or meta.get("track", "")
            if not meta.get("employment_type") or str(meta.get("employment_type")).upper() == "N/A":
                meta["employment_type"] = res.get("employment_type", "") or meta.get("employment_type", "")
            print(f"  scored {res.get('score')}  track {res.get('track')}  "
                  f"({res.get('employment_type')})")
        elif description.strip():
            print("  ⚠ scoring failed — tracker score left blank (fill later with --score-tracker).")

    # Decide the tracker row BEFORE writing anything, so a duplicate never spawns a brief file.
    # (Exact-URL dups already returned above.) A role re-posted under a DIFFERENT url is a
    # role_key match: warn and ask. A brief is written ONLY when a row is actually added — so
    # every applications/*.md corresponds to a tracked role, keeping that list a clean queue.
    company_t = meta.get("company", "")
    title_t = meta.get("title", "")
    _, key_hits = _tracker_matches(url, company_t, title_t)

    do_append = True
    if key_hits:
        print("\n  ⚠ Possible duplicate — this role is already tracked under a different URL:")
        for r in key_hits:
            print(f"      {r.get('company','')} — {r.get('role','')}  "
                  f"(status: {r.get('status','?')}, added {r.get('date_added','?')})")
            print(f"        {r.get('url','')}")
        do_append = _confirm("  Add a new tracker row (and brief) for this posting anyway?")
        if not do_append:
            print("  skipped — no tracker row, no brief written.")
            _company_note(company_t, exclude_urls=[url] + [r.get("url", "") for r in key_hits])
            return None

    os.makedirs(config.APPLICATIONS_DIR, exist_ok=True)
    base = f"{datetime.now():%Y-%m-%d}_{_brief_slug(company_t, title_t)}"  # date first -> sorts
    brief_path = _unique_path(config.APPLICATIONS_DIR, base)
    with open(brief_path, "w", encoding="utf-8") as f:
        f.write(_build_brief(meta, tf, description, err))
    print(f"  brief -> {brief_path}")

    followup = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    _append_tracker({
        "date_added": datetime.now().strftime("%Y-%m-%d"),
        "status": "interested",
        "company": company_t,
        "role": title_t,
        "url": url,
        "employment_type": meta.get("employment_type", ""),
        "location": meta.get("location", ""),
        "deadline": meta.get("deadline", ""),
        "score": meta.get("score", ""),
        "track": meta.get("track", ""),
        "next_followup": followup,
        "brief_file": os.path.basename(brief_path),
        "notes": "",
    })
    print(f"  tracked -> {config.TRACKER_CSV}  (status=interested, follow-up {followup})")

    # Soft over-applying heads-up: other roles already tracked at this same employer.
    _company_note(company_t, exclude_urls=[url] + [r.get("url", "") for r in key_hits])

    print("\nNext: paste the brief into the job-search Project to draft the CV + letter.")
    return brief_path


def prepare_by_index(n: int):
    rows = core.open_shortlist(config.MASTER_ARCHIVE)
    if not rows:
        print("Shortlist is empty. Run `python a_scrape.py` first.")
        return
    if not (1 <= n <= len(rows)):
        print(f"No item #{n}. The shortlist has {len(rows)} roles (1–{len(rows)}).")
        return
    return prepare(dict(rows[n - 1]))


def prepare_by_url(url: str):
    row = _archive_row(url)
    if row:
        print(f"Found in archive: {row.get('company','')} — {row.get('title','')} "
              f"({row.get('score','')}/100)")
        return prepare(dict(row))
    else:
        print("URL not in the archive — fetching live, and scoring it so the tracker row "
              "still gets a score/track.")
        return prepare({"url": url})


def main(argv):
    # Self-sync the queue on EVERY run. Nothing watches applications.csv, so a status edited by
    # hand (straight into the CSV, bypassing --status) leaves a settled role's brief sitting in
    # the queue until some code looks. This is that look: it costs one CSV read plus a stat per
    # row, and stays silent unless it actually moves something. --archive-briefs does the same
    # sweep loudly, for when you want to see it happen.
    if argv[:1] != ["--archive-briefs"]:
        moved = archive_settled_briefs(quiet=True)
        if moved:
            print(f"({moved} brief(s) auto-synced with the tracker — it had been edited by hand)\n")

    if not argv:
        print_shortlist()
        return

    if argv[0] == "--new":
        print_shortlist(only_new=True)
        return

    if argv[0] == "--score-tracker":
        score_tracker_gaps()
        return

    if argv[0] == "--status":
        if len(argv) != 3:
            print("Usage: python c_prepare.py --status <url> <status>\n"
                  f"  statuses: {', '.join(STATUSES)}")
            return
        url, status = argv[1], argv[2]
        if status not in STATUSES:
            print(f"⚠ '{status}' isn't a standard status ({', '.join(STATUSES)}). Setting it anyway.")
        if _update_status(url, status):
            print(f"Updated status -> {status} for {url}")
            if archive_settled_briefs():      # the role is settled -> its brief leaves the queue
                print("  (brief moved to _archive/ — still on disk, just out of the queue)")
        else:
            print("No tracker row matched that URL. Prep it first: python c_prepare.py <url>")
        return

    if argv[0] == "--rebrief":
        if len(argv) != 2:
            print("Usage: python c_prepare.py --rebrief <url>   # regenerate an existing row's brief")
            return
        rebrief(argv[1])
        return

    if argv[0] == "--archive-briefs":
        n = archive_settled_briefs()
        live = len([f for f in os.listdir(config.APPLICATIONS_DIR) if f.endswith(".md")]) \
            if os.path.isdir(config.APPLICATIONS_DIR) else 0
        print(f"\nArchived {n} settled brief(s). {live} still in the queue "
              f"({', '.join(config.BRIEF_QUEUE_STATUSES)}).")
        return

    arg = argv[0]
    if arg.startswith("http"):
        prepare_by_url(arg)
    elif arg.isdigit():
        prepare_by_index(int(arg))
    else:
        print("Unrecognised argument. Use a shortlist number, a job URL, --new, or --status.\n"
              "  python c_prepare.py                 # list the full shortlist (with status)\n"
              "  python c_prepare.py --new           # list only roles not yet applied to\n"
              "  python c_prepare.py 3               # prep item #3\n"
              "  python c_prepare.py <url>           # prep a URL\n"
              "  python c_prepare.py --status <url> applied\n"
              "  python c_prepare.py --score-tracker # backfill scores for manually-added roles\n"
              "  python c_prepare.py --rebrief <url> # regenerate the brief for a tracked role\n"
              "  python c_prepare.py --archive-briefs # move settled briefs out of the queue")


if __name__ == "__main__":
    main(sys.argv[1:])
