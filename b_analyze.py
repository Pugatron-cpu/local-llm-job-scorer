"""
b_analyze.py — STEP B: review the dataset (read-only, run anytime).

Reads the archive (job_market_data/job_market_data.csv) and prints a terminal overview:
score/track/employment-type/location distributions, Danish-required rate, fetch reliability,
recurring companies, the current open shortlist, and run-over-run drift from runs.csv.

    python b_analyze.py                 # uses the configured archive
    python b_analyze.py /path/to.csv    # explicit archive path

Read-only and stdlib-only (no playwright/ollama needed). Settings come from config.py.

Scope: the archive holds only SCORED roles, so it can't show the full scrape->drop funnel
(those counts live in runs.csv). There's no per-query column either.
"""

import os
import sys
import csv
from collections import Counter, defaultdict

from config import (SCORE_THRESHOLD, ACCEPTED_EMPLOYMENT_TYPES, REPORT_FRESH_DAYS,
                    REQUIRE_COMMUTABLE, MASTER_ARCHIVE, RUNS_LOG)


def _parse_date(s):
    from datetime import datetime
    s = (s or "").strip()
    if len(s) < 10:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _is_open(row, today):
    """Mirror of core.role_open_status: still worth showing as open?"""
    dl = _parse_date(row.get("deadline"))
    if dl is not None:
        return dl >= today
    seen = _parse_date(row.get("scraped_date"))
    if seen is not None:
        return (today - seen).days <= REPORT_FRESH_DAYS
    return True


def _commutable(row):
    return (not REQUIRE_COMMUTABLE) or str(row.get("commute_ok", "true")).lower() != "false"


DEFAULT_PATH = MASTER_ARCHIVE
RUNS_PATH = RUNS_LOG


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


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    rows = _load(path)
    n = len(rows)
    if n == 0:
        sys.exit("Archive is empty.")

    urls = [r.get("url", "") for r in rows]
    distinct = len(set(urls))
    dates = sorted({r.get("scraped_date", "") for r in rows if r.get("scraped_date")})
    from datetime import date
    today = date.today()
    scored_hi = [r for r in rows if r["_score"] >= SCORE_THRESHOLD]
    in_types = [r for r in scored_hi
                if r.get("employment_type", "unknown") in ACCEPTED_EMPLOYMENT_TYPES
                and _commutable(r)]
    matches = [r for r in in_types if _is_open(r, today)]          # open shortlist (= report)
    closed = len(in_types) - len(matches)                          # aged out / past deadline
    outside = [r for r in scored_hi if r not in in_types]          # high fit, wrong type for now

    print("=" * 60)
    print("JOB MARKET DATASET — ANALYSIS")
    print("=" * 60)
    print(f"Source         : {path}")
    print(f"Scored rows    : {n}")
    print(f"Distinct URLs  : {distinct}" + ("" if distinct == n
          else f"   (!! {n - distinct} duplicate URL rows)"))
    print(f"Runs (dates)   : {len(dates)}" + (f"   {dates[0]} .. {dates[-1]}" if dates else ""))
    print(f"Open shortlist : {len(matches)}  (score >= {SCORE_THRESHOLD}, target types, "
          f"still open)" + (f"   [{closed} more matched but closed/aged out]" if closed else ""))
    if outside:
        print(f"High-fit, other types : {len(outside)}  (score >= {SCORE_THRESHOLD} but "
              f"outside current targets — available if you widen ACCEPTED_EMPLOYMENT_TYPES)")

    # --- score buckets ---
    buckets = Counter()
    for r in rows:
        s = r["_score"]
        b = "85-100" if s >= 85 else "75-84" if s >= 75 else "50-74" if s >= 50 else "0-49"
        buckets[b] += 1
    _dist("SCORE DISTRIBUTION", buckets, order=["85-100", "75-84", "50-74", "0-49"])

    # --- track ---
    track = Counter(r.get("track", "none") for r in rows)
    _dist("TRACK (all scored)", track, order=["A", "B", "none"])
    mt = Counter(r.get("track", "none") for r in matches)
    print(f"  -> among matches: A={mt.get('A',0)}  B={mt.get('B',0)}")

    # --- employment type ---
    et = Counter(r.get("employment_type", "unknown") for r in rows)
    _dist("EMPLOYMENT TYPE (all scored)", et,
          order=["student", "part_time", "internship", "full_time", "unknown"])
    ft = et.get("full_time", 0)
    if ft:
        in_targets = "full_time" in ACCEPTED_EMPLOYMENT_TYPES
        print(f"  note: {ft} full_time roles scored on merit and kept in the DB; "
              + ("currently INCLUDED in your shortlist." if in_targets
                 else "currently filtered OUT of the shortlist (not in ACCEPTED_EMPLOYMENT_TYPES)."))

    # --- work mode ---
    _dist("WORK MODE (all scored)",
          Counter(r.get("work_mode", "unknown") for r in rows),
          order=["onsite", "hybrid", "remote", "unknown"])

    # --- danish requirement (now graded: required / preferred / none) ---
    def _dk_required(r):
        return str(r.get("danish_level", "")).strip().lower() == "required"
    def _dk_preferred(r):
        return str(r.get("danish_level", "")).strip().lower() == "preferred"
    dk_all = sum(1 for r in rows if _dk_required(r))
    dk_match = sum(1 for r in matches if _dk_required(r))
    pref_match = sum(1 for r in matches if _dk_preferred(r))
    print("\nDANISH REQUIREMENT (graded by the scorer; English ads can still require Danish)")
    print(f"  required, all scored : {dk_all}/{n} ({dk_all/n*100:.0f}%)")
    if matches:
        print(f"  required, in matches : {dk_match}/{len(matches)} "
              f"({dk_match/len(matches)*100:.0f}%)   <- hide these with EXCLUDE_DANISH_REQUIRED")
        print(f"  'a plus', in matches : {pref_match}/{len(matches)} "
              f"({pref_match/len(matches)*100:.0f}%)   <- Danish preferred, not mandatory")

    # --- tech company rate among matches ---
    if matches:
        tech = sum(1 for r in matches if _truthy(r.get("is_tech_company")))
        print(f"\nTECH-COMPANY EMPLOYER (matches): {tech}/{len(matches)} "
              f"({tech/len(matches)*100:.0f}%)")

    # --- fetch reliability ---
    _dist("SCORING SOURCE (fetch reliability)",
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
        if r["_score"] >= SCORE_THRESHOLD:
            by_date[d][1] += 1
    if len(by_date) > 1:
        print("\nDRIFT BY RUN (scored / matches)")
        for d in sorted(by_date):
            scored, m = by_date[d]
            print(f"  {d}   scored {scored:>3}   matches {m:>2}  {_bar(m, max(v[1] for v in by_date.values()))}")

    # --- current shortlist ---
    def _row_line(r):
        flags = []
        dlvl = str(r.get("danish_level", "")).strip().lower()
        if dlvl == "required":
            flags.append("DK req")
        elif dlvl == "preferred":
            flags.append("DK plus")
        if _parse_date(r.get("deadline")):
            flags.append(f"due {r['deadline']}")
        if r.get("source") == "snippet":
            flags.append("teaser-only")
        tag = ("  [" + ", ".join(flags) + "]") if flags else ""
        title = (r.get("title", "")[:54])
        return (f"  {r['_score']:>3}  {r.get('track','?'):<4} {r.get('employment_type','?'):<10} "
                f"{(r.get('company','')[:22]):<22} {title}{tag}")

    print(f"\nCURRENT SHORTLIST (open: >= {SCORE_THRESHOLD}, target types, deadline not passed)")
    for r in sorted(matches, key=lambda r: r["_score"], reverse=True):
        print(_row_line(r))

    if outside:
        print(f"\nHIGH-FIT, OUTSIDE CURRENT TARGETS (>= {SCORE_THRESHOLD}, e.g. full-time)")
        for r in sorted(outside, key=lambda r: r["_score"], reverse=True)[:15]:
            print(_row_line(r))

    # --- run history (timing + funnel) from runs.csv, if present ---
    if os.path.isfile(RUNS_PATH):
        with open(RUNS_PATH, encoding="utf-8") as f:
            runs = list(csv.DictReader(f))
        if runs:
            print("\nRUN HISTORY (runs.csv — most recent last)")
            for rr in runs[-10:]:
                print(f"  {rr.get('run_ts',''):<19}  {rr.get('duration_s','?'):>6}s   "
                      f"teasers {rr.get('teasers','?'):>3}  scored {rr.get('scored','?'):>3}  "
                      f"matches {rr.get('matches','?'):>2}")

    print()


if __name__ == "__main__":
    main()
