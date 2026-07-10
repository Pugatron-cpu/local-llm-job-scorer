"""
b_insights.py — STEP B: insights over your job-search data (read-only, run anytime).

Turns the two CSVs into decisions:
  - TRACKER  (applications/applications.csv) — your funnel + behaviour: apply/skip rates,
    where the model's score and your actual choices DISAGREE, overdue chasing.
  - ARCHIVE  (job_market_data/job_market_data.csv) — the market you're fishing in: score
    spread, the Danish-language gate, work-mode split, which sources/companies yield fit,
    and a ranked skill-demand table (what to learn / emphasise next).

READ-ONLY: never writes either CSV. Pure aggregation — the numbers are reproducible and the
selection logic lives in small testable helpers (see tests/test_insights.py).

USAGE
    python b_insights.py               # the full report
    python b_insights.py --funnel      # just the application funnel + score-vs-behaviour gap
    python b_insights.py --market       # just the market view (score / language / sources)
    python b_insights.py --skills       # just the skill-demand ranking
    python b_insights.py --themes       # cluster the LLM reasoning into why-themes (needs scikit-learn)
    python b_insights.py --profile jan  # run against a sandboxed profile's data

Small-sample honesty: with a few dozen applications the funnel RATES are directional, not
statistical. Counts are shown alongside every rate so you can judge the weight yourself.

Settings/paths: config.py. Engine helpers: core.py. See README.md.
"""

import os
import re
import sys
import csv
import collections
import statistics

import config
import core

# --- status groups (a tracker row holds the CURRENT status; these fold the cumulative funnel) -
TRIAGE         = {"interested", "skipped"}                       # logged, never applied
APPLIED_PLUS   = {"applied", "interview", "offer", "hired",      # application actually sent
                  "rejected", "rejected_after_interview"}
INTERVIEW_PLUS = {"interview", "offer", "hired", "rejected_after_interview"}
OFFER_PLUS     = {"offer", "hired"}
ACTIVE         = {"interested", "applied", "interview"}          # still live / chaseable

_SKILL_SPLIT = re.compile(r"[;,|]")


# --------------------------------------------------------------------------- load / coerce
def _load(path):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _status(r):
    return (r.get("status") or "").strip().lower()


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _scores(rows, col="score"):
    return [s for s in (_num(r.get(col)) for r in rows) if s is not None]


def _skills(row):
    return [s.strip().lower() for s in _SKILL_SPLIT.split(row.get("matched_skills") or "")
            if s.strip()]


# --------------------------------------------------------------------------- TRACKER aggregations
def funnel(trk):
    """Cumulative funnel counts + conversion rates. A rate is None when its denominator is 0."""
    def n(group):
        return sum(1 for r in trk if _status(r) in group)
    logged, applied = len(trk), n(APPLIED_PLUS)
    interview, offer = n(INTERVIEW_PLUS), n(OFFER_PLUS)

    def rate(num, den):
        return (num / den) if den else None
    return {
        "logged": logged,
        "applied": applied,
        "interview": interview,
        "offer": offer,
        "apply_rate": rate(applied, logged),          # of everything logged, how much I pursued
        "interview_rate": rate(interview, applied),    # of what I applied to, callbacks
        "offer_rate": rate(offer, interview),
        "by_status": dict(collections.Counter(_status(r) for r in trk).most_common()),
    }


def score_by_status(trk):
    """status -> (n, mean score). The headline diagnostic: if SKIPPED roles outscore APPLIED
    ones, the model's `score` isn't capturing what actually drives your choice."""
    buckets = collections.defaultdict(list)
    for r in trk:
        s = _num(r.get("score"))
        if s is not None:
            buckets[_status(r)].append(s)
    return {k: (len(v), statistics.mean(v)) for k, v in buckets.items()}


def skip_vs_apply_by_band(trk, width=10):
    """score band -> (applied_plus, triaged_away). Shows whether high-scoring roles are being
    skipped (a sign the score and your real filters diverge)."""
    out = collections.defaultdict(lambda: [0, 0])
    for r in trk:
        s = _num(r.get("score"))
        if s is None:
            continue
        band = int(s // width) * width
        st = _status(r)
        if st in APPLIED_PLUS:
            out[band][0] += 1
        elif st in TRIAGE:
            out[band][1] += 1
    return {b: tuple(v) for b, v in sorted(out.items())}


def _history_path():
    return os.path.join(config.APPLICATIONS_DIR, "status_history.csv")


def load_history():
    return _load(_history_path())


RESPONSE_STATES = {"interview", "offer", "hired", "rejected", "rejected_after_interview"}


def response_times(history):
    """From the append-only transition log: per role, days from the `applied` transition to the
    first response (interview/rejected/...). Empty until enough `--status` changes accrue.
    Returns (list_of_days, transitions_by_type Counter)."""
    events = collections.defaultdict(list)   # canonical url -> [(date, new_status)]
    by_type = collections.Counter()
    for h in history:
        d = core._parse_date(h.get("date"))
        by_type[f"{h.get('old_status','?')}→{h.get('new_status','?')}"] += 1
        if d:
            events[core.canonical_url(h.get("url", ""))].append((d, (h.get("new_status") or "").lower()))
    days = []
    for evs in events.values():
        evs.sort()
        applied = next((d for d, s in evs if s == "applied"), None)
        if not applied:
            continue
        resp = next((d for d, s in evs if s in RESPONSE_STATES and d >= applied), None)
        if resp:
            days.append((resp - applied).days)
    return days, by_type


def overdue_followups(trk, today):
    """Active rows whose next_followup date is in the past — who to chase, most overdue first."""
    out = []
    for r in trk:
        if _status(r) not in ACTIVE:
            continue
        d = core._parse_date(r.get("next_followup"))
        if d and d < today:
            out.append(((today - d).days, r))
    return sorted(out, key=lambda x: x[0], reverse=True)   # sort on days only; rows aren't comparable


# --------------------------------------------------------------------------- ARCHIVE aggregations
def _breakdown(rows, col):
    return dict(collections.Counter((r.get(col) or "?").strip().lower() or "?"
                                    for r in rows).most_common())


def score_histogram(rows, width=10):
    h = collections.Counter(int(s // width) * width for s in _scores(rows))
    return dict(sorted(h.items()))


def completeness_split(rows, threshold):
    """`source` column is ad-body completeness (full text vs teaser snippet), NOT the origin
    board. Returns level -> (total, fit): snippet-scored roles were judged on partial text, so
    their fit is lower-confidence."""
    out = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        lvl = (r.get("source") or "?").strip() or "?"
        out[lvl][0] += 1
        s = _num(r.get("score"))
        if s is not None and s >= threshold:
            out[lvl][1] += 1
    return {k: tuple(v) for k, v in out.items()}


def top_companies(rows, threshold, limit=12):
    c = collections.Counter()
    for r in rows:
        s = _num(r.get("score"))
        if s is not None and s >= threshold:
            c[(r.get("company") or "?").strip() or "?"] += 1
    return c.most_common(limit)


def cluster_reasons(arc, k=8, seed=42):
    """Cluster the LLM `reasoning` texts into why-themes with TF-IDF + KMeans. Returns cluster
    dicts (size, mean_score, terms, examples) largest-first. Deterministic (fixed seed). Raises
    ImportError if scikit-learn is absent so the caller can degrade gracefully."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.cluster import KMeans

    rows = [r for r in arc if len((r.get("reasoning") or "").strip()) >= 20]
    if len(rows) < 4:
        return []
    k = min(k, len(rows) // 2)
    if k < 2:
        return []
    vec = TfidfVectorizer(stop_words="english", max_df=0.5, min_df=5, ngram_range=(1, 2))
    X = vec.fit_transform([r["reasoning"] for r in rows])
    km = KMeans(n_clusters=k, random_state=seed, n_init=10).fit(X)
    terms = vec.get_feature_names_out()
    dist = km.transform(X)   # per-point distance to every centre; used to pick representatives
    out = []
    for c in range(k):
        idx = [i for i, lbl in enumerate(km.labels_) if lbl == c]
        if not idx:
            continue
        top = [terms[t] for t in km.cluster_centers_[c].argsort()[::-1][:6]]
        scores = [s for s in (_num(rows[i].get("score")) for i in idx) if s is not None]
        near = sorted(idx, key=lambda i: dist[i, c])[:3]          # closest to the centroid
        out.append({
            "size": len(idx),
            "mean_score": statistics.mean(scores) if scores else None,
            "terms": top,
            "examples": [(rows[i].get("title") or "")[:48] for i in near],
        })
    return sorted(out, key=lambda c: -c["size"])


def skill_frequency(rows, min_score=None, limit=25):
    c = collections.Counter()
    for r in rows:
        if min_score is not None:
            s = _num(r.get("score"))
            if s is None or s < min_score:
                continue
        c.update(_skills(r))
    return c.most_common(limit)


# --------------------------------------------------------------------------- printers
def _pct(x):
    return "  n/a" if x is None else f"{x*100:4.0f}%"


def _bar(n, total, width=28):
    if not total:
        return ""
    fill = int(round(width * n / total))
    return "█" * fill + "·" * (width - fill)


def print_funnel(trk):
    print("\n══ APPLICATION FUNNEL & BEHAVIOUR ══  (tracker)")
    if not trk:
        print("  Tracker is empty — nothing applied yet.")
        return
    f = funnel(trk)
    print(f"\n  logged {f['logged']}  →  applied {f['applied']}  →  "
          f"interview {f['interview']}  →  offer {f['offer']}")
    print(f"    apply rate     {_pct(f['apply_rate'])}   ({f['applied']}/{f['logged']} logged pursued)")
    print(f"    interview rate {_pct(f['interview_rate'])}   ({f['interview']}/{f['applied']} applications)")
    print(f"    offer rate     {_pct(f['offer_rate'])}   ({f['offer']}/{f['interview']} interviews)")
    if f["applied"] < 40 or f["interview"] < 10:
        print("    ⚠ small sample — read these as direction, not statistics.")

    print("\n  status breakdown:")
    for st, n in f["by_status"].items():
        print(f"    {st:<26} {n:>3}  {_bar(n, f['logged'])}")

    sbs = score_by_status(trk)
    if sbs:
        print("\n  mean model-score by status  (does the score match what you actually do?):")
        for st, (n, m) in sorted(sbs.items(), key=lambda kv: -kv[1][1]):
            print(f"    {st:<26} n={n:<3} mean {m:5.1f}")
        applied_m = statistics.mean([m for st, (n, m) in sbs.items() if st in APPLIED_PLUS]) \
            if any(st in APPLIED_PLUS for st in sbs) else None
        skip_m = sbs.get("skipped", (0, None))[1]
        if applied_m is not None and skip_m is not None and skip_m > applied_m + 3:
            print(f"    → skipped roles average {skip_m:.0f} vs applied {applied_m:.0f}: the score rates "
                  "roles you reject HIGHER than\n      ones you pursue, so it isn't capturing your real "
                  "filter (language / commute / seniority).")

    band = skip_vs_apply_by_band(trk)
    if band:
        print("\n  by score band   applied / skipped-or-triaged:")
        for b, (ap, sk) in sorted(band.items(), reverse=True):
            print(f"    {b:>3}-{b+9:<3}  applied {ap:>3}   triaged {sk:>3}   {_bar(ap, ap+sk)}")


def print_response_times(history):
    print("\n══ RESPONSE TIMES ══  (status_history.csv, append-only)")
    if not history:
        print("  No transition history yet — it accrues from now on each time you run")
        print("  `python c_prepare.py --status <url> <new-status>`. Come back once a few land.")
        return
    days, by_type = response_times(history)
    print(f"  {len(history)} transitions logged.  transitions by type:")
    for t, n in by_type.most_common():
        print(f"    {t:<34} {n:>3}")
    if days:
        print(f"\n  applied → first response: median {statistics.median(days):.0f}d "
              f"(min {min(days)}, max {max(days)}, n={len(days)})")
    else:
        print("\n  applied → response: not enough completed applied→response pairs yet.")


def print_overdue(trk):
    from datetime import datetime
    od = overdue_followups(trk, datetime.now().date())
    print("\n══ OVERDUE FOLLOW-UPS ══")
    if not od:
        print("  Nothing overdue. ✓")
        return
    for days, r in od:
        print(f"  {days:>3}d overdue  {(r.get('company','') or '')[:26]:<26} "
              f"{(r.get('role','') or '')[:34]:<34} [{_status(r)}]")


def print_market(arc):
    print("\n══ MARKET VIEW ══  (archive)")
    if not arc:
        print("  Archive is empty — run a_scrape.py first.")
        return
    sc = _scores(arc)
    print(f"\n  {len(arc)} scored roles   score: mean {statistics.mean(sc):.0f}  "
          f"median {statistics.median(sc):.0f}  (fit bar = {config.SCORE_THRESHOLD})")
    fit = sum(1 for s in sc if s >= config.SCORE_THRESHOLD)
    print(f"  {fit} roles ({fit/len(sc)*100:.0f}%) clear the fit bar.")
    print("\n  score distribution:")
    for b, n in score_histogram(arc).items():
        print(f"    {b:>3}-{b+9:<3} {n:>4}  {_bar(n, len(arc))}")

    dl = _breakdown(arc, "danish_level")
    req = dl.get("required", 0)
    print("\n  Danish-language gate:")
    for k in ("required", "preferred", "none", "?"):
        if k in dl:
            print(f"    {k:<10} {dl[k]:>4}  {_bar(dl[k], len(arc))}")
    if req:
        print(f"    → {req/len(arc)*100:.0f}% of the market REQUIRES Danish — a hard ceiling on reachable roles.")

    print("\n  ad language:   ", _breakdown(arc, "ad_language"))
    print("  work mode:     ", _breakdown(arc, "work_mode"))
    print("  employment:    ", _breakdown(arc, "employment_type"),
          "\n                 (archive holds full-time too; filters are views, so it's stored anyway)")

    print("\n  ad-body completeness   (fit / total)   — snippet = scored on a teaser, lower confidence:")
    for lvl, (tot, f) in sorted(completeness_split(arc, config.SCORE_THRESHOLD).items(),
                                key=lambda kv: -kv[1][1]):
        print(f"    {lvl:<10} {f:>4} / {tot:<5}  {_bar(f, tot)}")

    print(f"\n  top employers by fit roles (score ≥ {config.SCORE_THRESHOLD}):")
    for comp, n in top_companies(arc, config.SCORE_THRESHOLD):
        print(f"    {n:>3}  {comp}")


def print_skills(arc):
    print("\n══ SKILL DEMAND ══  (archive `matched_skills`)")
    if not arc:
        print("  Archive is empty — run a_scrape.py first.")
        return
    overall = skill_frequency(arc)
    print(f"\n  most-demanded skills across {len(arc)} roles:")
    top = overall[0][1] if overall else 1
    for skill, n in overall:
        print(f"    {n:>4}  {skill:<26} {_bar(n, top)}")

    fit = skill_frequency(arc, min_score=config.SCORE_THRESHOLD, limit=15)
    if fit:
        print(f"\n  within FIT roles (score ≥ {config.SCORE_THRESHOLD}) — what your target roles ask for:")
        for skill, n in fit:
            print(f"    {n:>4}  {skill}")


def print_themes(arc, k=8):
    print("\n══ WHY-THEMES ══  (TF-IDF + KMeans over the LLM `reasoning`)")
    if not arc:
        print("  Archive is empty — run a_scrape.py first.")
        return
    try:
        clusters = cluster_reasons(arc, k=k)
    except ImportError:
        print("  scikit-learn not installed — run:  pip install -r requirements.txt")
        return
    if not clusters:
        print("  Not enough reasoning text to cluster.")
        return
    print("  Each theme = a recurring reason roles score how they do (terms are the cluster's own).")
    for i, c in enumerate(clusters, 1):
        ms = f"{c['mean_score']:.0f}" if c["mean_score"] is not None else "n/a"
        print(f"\n  #{i}  {c['size']:>4} roles · mean score {ms}")
        print(f"      terms:  {', '.join(c['terms'])}")
        for ex in c["examples"]:
            if ex:
                print(f"      e.g.    {ex}")


# --------------------------------------------------------------------------- main
def main(argv):
    trk = _load(config.TRACKER_CSV)
    arc = _load(config.MASTER_ARCHIVE)
    want = set(a for a in argv if a.startswith("--"))
    all_sections = not (want & {"--funnel", "--market", "--skills", "--themes"})

    print(f"Job-search insights  ·  tracker {len(trk)} rows  ·  archive {len(arc)} rows")
    if all_sections or "--funnel" in want:
        print_funnel(trk)
        print_response_times(load_history())
        print_overdue(trk)
    if all_sections or "--market" in want:
        print_market(arc)
    if all_sections or "--skills" in want:
        print_skills(arc)
    if all_sections or "--themes" in want:
        print_themes(arc)
    print()


if __name__ == "__main__":
    main(sys.argv[1:])
