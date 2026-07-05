"""
b_analyze.py — STEP B: review the dataset (read-only, run anytime).

Reads the archive (job_market_data/job_market_data.csv) and prints a terminal overview:
score/track/employment-type distributions, Danish level and ad-language rates, fetch
reliability, which VIEW FILTERS are hiding what, recurring companies, the current open
shortlist (identical to Weekly_Job_Matches.md — it comes from the same core.open_shortlist),
and run-over-run drift from runs.csv.

    python b_analyze.py                 # uses the configured archive
    python b_analyze.py /path/to.csv    # explicit archive path

Read-only. Uses core.py for the shortlist so this view can NEVER diverge from the report
again (an earlier version reimplemented the filters by hand and silently drifted: it still
read the removed danish_required column and never applied EXCLUDE_DANISH_REQUIRED).
Settings come from config.py; personal data from profiles/<name>.toml.
"""

import os
import sys
import csv
from collections import Counter, defaultdict

import config
import core


def _bar(count, maxcount, width=34):
    if maxcount <= 0:
        return ""
    return "█" * max(0, int(round(width * count / maxcount)))


def _dist(title, counter, order=None):
    """Print a labelled distribution as aligned text bars, biggest first (or fixed order)."""
    print(f"\n{title}")
    if not counter:
        print("  (none)")
        return
    items = ([(k, counter.get(k, 0)) for k in order] if order
             else sorted(counter.items(), key=lambda kv: kv[1], reverse=True))
    mx = max((c for _, c in items), default=0)
    klen = max((len(str(k)) for k, _ in items), default=1)
    for k, c in items:
        print(f"  {str(k):<{klen}}  {c:>4}  {_bar(c, mx)}")


def _load(path):
    if not os.path.isfile(path):
        sys.exit(f"No archive at {path}")
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        try:
            r["_score"] = int(r.get("score") or 0)
        except ValueError:
            r["_score"] = 0
    return rows


def _truthy(v):
    return str(v).strip().lower() == "true"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else config.MASTER_ARCHIVE
    rows = _load(path)
    n = len(rows)
    if n == 0:
        sys.exit("Archive is empty.")

    # De-dup stats on the CANONICAL url (same key the engine uses everywhere).
    distinct = len({core.canonical_url(r.get("url", "")) for r in rows if r.get("url")})
    dates = sorted({r.get("scraped_date", "") for r in rows if r.get("scraped_date")})

    # The shortlist and the reasons rows were hidden — straight from the engine, so this
    # printout is guaranteed identical to Weekly_Job_Matches.md and c_prepare numbering.
    matches, dropped = core.shortlist_with_reasons(path)

    print("=" * 60)
    print(f"JOB MARKET DATASET — ANALYSIS   (profile: {config.ACTIVE_PROFILE})")
    print("=" * 60)
    print(f"Source         : {path}")
    print(f"Scored rows    : {n}")
    print(f"Distinct roles : {distinct}  (canonical URLs"
          + ("" if distinct == n else f"; {n - distinct} re-scored/duplicate rows") + ")")
    print(f"Runs (dates)   : {len(dates)}" + (f"   {dates[0]} .. {dates[-1]}" if dates else ""))
    print(f"Open shortlist : {len(matches)}")

    # Active view filters — say exactly what is being hidden and by which knob.
    print("\nACTIVE VIEW FILTERS (archive keeps everything; these only shape the shortlist)")
    print(f"  score >= {config.SCORE_THRESHOLD}"
          f"   ·   track B needs >= {config.TRACK_B_MIN_SCORE}")
    print(f"  types: {', '.join(sorted(config.ACCEPTED_EMPLOYMENT_TYPES))}")
    print(f"  commutable only          : {'ON' if config.REQUIRE_COMMUTABLE else 'off'}")
    print(f"  hide Danish-REQUIRED     : {'ON' if config.EXCLUDE_DANISH_REQUIRED else 'off'}")
    print(f"  hide Danish-WRITTEN ads  : {'ON' if config.EXCLUDE_DANISH_ADS else 'off'}")
    if dropped:
        print("  hidden from the deduped archive, by reason:")
        for reason, k in dropped.most_common():
            print(f"    {k:>4}  {reason}")

    # --- score buckets ---
    buckets = Counter()
    for r in rows:
        s = r["_score"]
        b = "85-100" if s >= 85 else "75-84" if s >= 75 else "50-74" if s >= 50 else "0-49"
        buckets[b] += 1
    _dist("SCORE DISTRIBUTION (all scored rows)", buckets,
          order=["85-100", "75-84", "50-74", "0-49"])

    # --- track ---
    track = Counter(r.get("track", "none") for r in rows)
    _dist("TRACK (all scored)", track, order=["A", "B", "none"])
    mt = Counter(r.get("track", "none") for r in matches)
    print(f"  -> among matches: A={mt.get('A', 0)}  B={mt.get('B', 0)}")

    # --- employment type ---
    et = Counter(r.get("employment_type", "unknown") for r in rows)
    _dist("EMPLOYMENT TYPE (all scored)", et,
          order=["student", "part_time", "internship", "full_time", "unknown"])
    ft = et.get("full_time", 0)
    if ft:
        in_targets = "full_time" in config.ACCEPTED_EMPLOYMENT_TYPES
        print(f"  note: {ft} full_time roles scored on merit and kept in the DB; "
              + ("currently INCLUDED in your shortlist." if in_targets
                 else "currently filtered OUT of the shortlist (not in ACCEPTED_EMPLOYMENT_TYPES)."))

    # --- work mode ---
    _dist("WORK MODE (all scored)",
          Counter(r.get("work_mode", "unknown") for r in rows),
          order=["onsite", "hybrid", "remote", "unknown"])

    # --- Danish: the ROLE's requirement (LLM-graded enum) and the AD's writing language ---
    def _lvl(r):
        v = str(r.get("danish_level", "")).strip().lower()
        return v if v in ("none", "preferred", "required") else "(blank — old scoring)"
    _dist("DANISH LEVEL — how much Danish the ROLE requires (all scored)",
          Counter(_lvl(r) for r in rows),
          order=["none", "preferred", "required", "(blank — old scoring)"])
    blanks_open = sum(1 for r in matches if _lvl(r).startswith("(blank"))
    if blanks_open:
        print(f"  ⚠ {blanks_open} shortlist row(s) predate the danish_level column — their "
              f"Danish flags are unknown.\n    Fix: python a_scrape.py --rescore")

    def _adlang(r):
        v = str(r.get("ad_language", "")).strip().lower()
        return v if v else "(blank — old scoring)"
    _dist("AD LANGUAGE — what the ad is WRITTEN in (all scored)",
          Counter(_adlang(r) for r in rows),
          order=["en", "da", "sv", "no", "(blank — old scoring)"])

    # --- tech company rate among matches ---
    if matches:
        tech = sum(1 for r in matches if _truthy(r.get("is_tech_company")))
        print(f"\nTECH-COMPANY EMPLOYER (matches): {tech}/{len(matches)} "
              f"({tech / len(matches) * 100:.0f}%)")

    # --- fetch reliability ---
    _dist("SCORING SOURCE (fetch reliability — 'snippet' rows have unreliable Danish flags)",
          Counter(r.get("source", "?") for r in rows), order=["full", "snippet"])

    # --- recurring companies ---
    comp = Counter(r.get("company", "") for r in rows if r.get("company"))
    repeat = [(c, k) for c, k in comp.most_common() if k >= 2]
    print("\nRECURRING COMPANIES (>=2 scored roles)")
    if repeat:
        for c, k in repeat[:15]:
            m = sum(1 for r in matches if r.get("company") == c)
            print(f"  {k:>2}x  {c}" + (f"   ({m} match{'es' if m != 1 else ''})" if m else ""))
    else:
        print("  (none yet)")

    # --- drift over time ---
    by_date = defaultdict(lambda: [0, 0])
    for r in rows:
        d = r.get("scraped_date", "")
        by_date[d][0] += 1
        if r["_score"] >= config.SCORE_THRESHOLD:
            by_date[d][1] += 1
    if len(by_date) > 1:
        print("\nDRIFT BY RUN (scored / high-fit)")
        mx = max(v[1] for v in by_date.values())
        for d in sorted(by_date):
            scored, m = by_date[d]
            print(f"  {d}   scored {scored:>3}   high-fit {m:>2}  {_bar(m, mx)}")

    # --- current shortlist (from the engine, so identical to the report) ---
    def _row_line(i, r):
        flags = []
        lvl = str(r.get("danish_level", "")).strip().lower()
        if lvl == "required":
            flags.append("DK required")
        elif lvl == "preferred":
            flags.append("DK a plus")
        elif lvl == "":
            flags.append("flags unknown")
        if str(r.get("ad_language", "")).lower() == "da":
            flags.append("ad in DK")
        if core._parse_date(r.get("deadline")):
            flags.append(f"due {r['deadline']}")
        if r.get("source") == "snippet":
            flags.append("teaser-only")
        tag = ("  [" + ", ".join(flags) + "]") if flags else ""
        title = (r.get("title", "")[:50])
        return (f"  {i:>2}. {r['score']:>3}  {r.get('track', '?'):<4} "
                f"{r.get('employment_type', '?'):<10} "
                f"{(r.get('company', '')[:22]):<22} {title}{tag}")

    print(f"\nCURRENT SHORTLIST — identical to Weekly_Job_Matches.md / c_prepare numbering")
    if matches:
        for i, r in enumerate(matches, 1):
            print(_row_line(i, r))
    else:
        print("  (empty)")

    # --- run history (timing + funnel) from runs.csv, if present ---
    if os.path.isfile(config.RUNS_LOG):
        with open(config.RUNS_LOG, encoding="utf-8") as f:
            runs = list(csv.DictReader(f))
        if runs:
            print("\nRUN HISTORY (runs.csv — most recent last)")
            print("  snippet_fallback = pages that failed to fetch; a spike means match "
                  "quality + Danish flags degraded that run")
            for rr in runs[-10:]:
                print(f"  {rr.get('run_ts', ''):<19}  {rr.get('duration_s', '?'):>6}s   "
                      f"teasers {rr.get('teasers', '?'):>3}  scored {rr.get('scored', '?'):>3}  "
                      f"fallback {rr.get('snippet_fallback', '?'):>2}  "
                      f"errors {rr.get('errors', '?'):>2}  "
                      f"matches {rr.get('matches', '?'):>2}")

    print()


if __name__ == "__main__":
    main()
