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

The brief is a HANDOFF: a fresh Claude conversation in the Project (which has
master_profile.md + the Alipes letter) does the final CV + motivation letter. c_prepare does
NOT write the application itself — it assembles honest, structured raw material and never
fabricates company facts or candidate claims.

Settings: config.py. Engine: core.py. See README.md.
"""

import os
import re
import csv
import sys
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

STATUSES = ["interested", "applied", "interview", "offer", "rejected", "skipped"]


def _load_tracker():
    if not os.path.isfile(config.TRACKER_CSV):
        return []
    with open(config.TRACKER_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _tracker_has(url: str) -> bool:
    # Match on the CANONICAL url (tracking params stripped, host/slash normalised) so a role
    # already in the tracker under one source's URL (e.g. Jobindex's thehub.io/...?utm_source=
    # jobindex) is recognised when the same role arrives later under another source's clean URL.
    cu = core.canonical_url(url)
    return any(core.canonical_url(r.get("url", "")) == cu for r in _load_tracker())


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


def _append_tracker(row: dict):
    os.makedirs(config.APPLICATIONS_DIR, exist_ok=True)
    new = not os.path.isfile(config.TRACKER_CSV)
    with open(config.TRACKER_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRACKER_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in TRACKER_FIELDS})


def _update_status(url: str, new_status: str) -> bool:
    """Rewrite the tracker with one row's status updated. Returns True if a row matched.
    Matching is on the canonical url, so `--status <clean-or-utm-url> applied` updates the
    existing row even if the stored url carries different tracking params."""
    cu = core.canonical_url(url)
    rows = _load_tracker()
    hit = False
    for r in rows:
        if core.canonical_url(r.get("url", "")) == cu:
            r["status"] = new_status
            hit = True
    if not hit:
        return False
    with open(config.TRACKER_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRACKER_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in TRACKER_FIELDS})
    return True


# --- archive lookup (for roles already scored) --------------------------------------------
def _archive_row(url: str):
    """Return the highest-scored archive row for this URL, or None.
    Compared on the canonical url so a role scored under one source's URL is found when
    looked up by another source's variant of the same link."""
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
            if best is None or r["score"] > best["score"]:
                best = r
    return best


# --- brief assembly -----------------------------------------------------------------------
def _slug(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (s or "").strip())
    s = re.sub(r"[\s_-]+", "_", s)
    return s.strip("_")[:50] or "role"


def _unique_path(directory: str, base: str) -> str:
    path = os.path.join(directory, base + ".md")
    n = 2
    while os.path.exists(path):
        path = os.path.join(directory, f"{base}_{n}.md")
        n += 1
    return path


def _bullets(items, empty="_(none extracted)_"):
    items = [str(x).strip() for x in (items or []) if str(x).strip()]
    return "\n".join(f"- {x}" for x in items) if items else empty


def _build_brief(meta: dict, tf: dict, description: str, fetch_err) -> str:
    track = meta.get("track", "")
    lane = "A" if track == "A" else ("B" if track == "B" else "?")
    lane_word = {"A": "Lane A (technical)", "B": "Lane B (foot-in-the-door)"}.get(lane, "the matching lane")
    lead = ("Lead with the AI Content Architecture portfolio — it's the differentiator."
            if lane == "A" else
            "This is a foot-in-the-door role: lead with reliability, stakeholder calm, and the "
            "intent to grow into technical work; mention the portfolio as evidence of initiative.")

    score_line = ""
    if meta.get("score"):
        score_line = (f"- **Local scorer:** {meta['score']}/100 · Track {track or '?'}"
                      + (f" — {meta['reasoning']}" if meta.get("reasoning") else "") + "\n")
    matched = meta.get("matched_skills")
    if isinstance(matched, str):
        matched = [m.strip() for m in matched.split(",") if m.strip()]
    matched_line = f"- **Scorer matched skills:** {', '.join(matched)}\n" if matched else ""

    danish = str(meta.get("danish_required", "")).lower() == "true"
    today = datetime.now().strftime("%Y-%m-%d")

    if description.strip():
        jd_block = description.strip()
    else:
        jd_block = ("⚠ LIVE FETCH FAILED" + (f" ({fetch_err})" if fetch_err else "")
                    + " — PASTE THE FULL JOB DESCRIPTION HERE before handing this to Claude.")

    return f"""# Application Brief — {meta.get('company', '')} — {meta.get('title', '')}

> **HANDOFF TO CLAUDE.** Paste this whole file into the job-search Project. Using
> `master_profile.md` as the ONLY source of facts about {config.CANDIDATE_NAME}, produce: **(a)** a tailored
> {lane_word} CV as RxResume sections, and **(b)** a ~250–300 word motivation letter using the
> Alipes cover-letter formula. {lead} Pick ONE genuine, specific hook yourself from
> *Company facts* below — never fabricate enthusiasm. Mirror the role's ATS keywords ONLY where
> they are true of {config.CANDIDATE_NAME}. Treat *Alignment draft* as unverified hints: flag any must-have {config.CANDIDATE_NAME}
> doesn't clearly meet instead of papering over it. State nothing `master_profile.md` doesn't support.

## Role
- **Company:** {meta.get('company', '')}
- **Title:** {meta.get('title', '')}
- **Location:** {meta.get('location', '') or '—'}
- **Type:** {meta.get('employment_type', '') or '—'} · **Work mode:** {meta.get('work_mode', '') or '—'}
- **Deadline:** {meta.get('deadline', '') or '—'}
- **Danish required:** {'⚠ yes' if danish else 'no / not stated'}
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

**ATS keywords** _(mirror in the CV only where true of {config.CANDIDATE_NAME})_
{_bullets(tf.get('ats_keywords'))}

## Company facts — pick ONE genuine hook (do not invent)
{_bullets(tf.get('company_facts'))}

## Alignment draft — {config.CANDIDATE_NAME} ↔ role _(UNVERIFIED — check against master_profile.md, don't over-claim)_
{_bullets(tf.get('candidate_alignment'))}

## Full job description (verbatim, fetched {today})
{jd_block}
"""


# --- main flows ---------------------------------------------------------------------------
def print_shortlist(only_new: bool = False):
    rows = core.open_shortlist(config.MASTER_ARCHIVE)
    if not rows:
        print("Shortlist is empty. Run `python a_scrape.py` first.")
        return
    status_map = _tracker_status_map()

    # Classify every row once. The index is kept stable (same as the full shortlist and the
    # Weekly_Job_Matches.md report), so `c_prepare.py <n>` means the same role in every view.
    items = []
    for i, r in enumerate(rows, 1):
        st = status_map.get(core.canonical_url(r.get("url", "")))
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


def prepare(meta: dict):
    """meta must have at least 'url' (and ideally title/company/track from the archive)."""
    url = meta["url"]
    print(f"Re-fetching live: {url}")
    description, err = core.fetch_one(url)
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

    # Prefer archive meta; fill blanks from the transform.
    for k in ("title", "company", "location", "employment_type", "deadline"):
        if not meta.get(k) or str(meta.get(k)).upper() == "N/A":
            meta[k] = tf.get(k, meta.get(k, ""))

    os.makedirs(config.APPLICATIONS_DIR, exist_ok=True)
    base = f"{datetime.now():%Y-%m-%d}_{_slug(meta.get('company',''))}"   # date first -> chronological sort
    brief_path = _unique_path(config.APPLICATIONS_DIR, base)
    with open(brief_path, "w", encoding="utf-8") as f:
        f.write(_build_brief(meta, tf, description, err))
    print(f"  brief -> {brief_path}")

    # Tracker (skip duplicate URLs, but the brief is always (re)written).
    if _tracker_has(url):
        print("  already in tracker — brief refreshed, tracker row left as-is.")
    else:
        followup = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        _append_tracker({
            "date_added": datetime.now().strftime("%Y-%m-%d"),
            "status": "interested",
            "company": meta.get("company", ""),
            "role": meta.get("title", ""),
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
        print("URL not in the archive — preparing from a live fetch only "
              "(no local score/track available).")
        return prepare({"url": url})


def main(argv):
    if not argv:
        print_shortlist()
        return

    if argv[0] == "--new":
        print_shortlist(only_new=True)
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
        else:
            print("No tracker row matched that URL. Prep it first: python c_prepare.py <url>")
        return

    arg = argv[0]
    if arg.startswith("http"):
        prepare_by_url(arg)
    elif arg.isdigit():
        prepare_by_index(int(arg))
    else:
        print("Unrecognised argument. Use a shortlist number, a job URL, --new, or --status.\n"
              "  python c_prepare.py            # list the full shortlist (with status)\n"
              "  python c_prepare.py --new      # list only roles not yet applied to\n"
              "  python c_prepare.py 3          # prep item #3\n"
              "  python c_prepare.py <url>      # prep a URL\n"
              "  python c_prepare.py --status <url> applied")


if __name__ == "__main__":
    main(sys.argv[1:])
