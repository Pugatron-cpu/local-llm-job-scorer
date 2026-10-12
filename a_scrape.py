"""
a_scrape.py — STEP A: run the job search.

Scrapes the enabled sources (Jobindex, The Hub, optionally Jobnet), filters, fetches full
ads, scores them with the local LLM, appends to the archive
(job_market_data/job_market_data.csv), and rebuilds the open-roles shortlist
(job_market_data/Weekly_Job_Matches.md). Run this first, and regularly.

    python a_scrape.py               # normal run
    python a_scrape.py --rescore     # maintenance: re-score still-open shortlist rows that
                                     # predate the danish_level / ad_language columns (they
                                     # show "flags unknown" in the report until refreshed)
    python a_scrape.py --rescore-all # escape hatch: re-score ALL still-open shortlist rows
                                     # (not just flag-blank ones) — use after a model/prompt
                                     # change so the existing shortlist reflects it
    python a_scrape.py --rescore-all 50
                                     # same, from score 50 up instead of the threshold: after
                                     # a rubric WIDENS what fits, so low old scores can rise.
                                     # 40 rows per run; run again for the rest.
    python a_scrape.py --rescore-url <url> [<url> ...]
                                     # re-score specific archived roles, any score
    python a_scrape.py --watch       # quick check: ONLY the company watchlist (profile
                                     # [[watch]] entries) -> score new postings + rebuild
                                     # the report. Skips Jobindex / The Hub.
    python a_scrape.py --model-preset fallback
                                     # score with another config.MODEL_PRESETS entry (e.g.
                                     # the A4000-sized fallback). EXPLICIT only — no
                                     # auto-failover; each model scores on its own scale
                                     # (rows are stamped with scoring_model). Also settable
                                     # via JOBSEARCH_MODEL_PRESET=<name>; default "fast".

All settings live in config.py; personal data in profiles/<name>.toml; the engine in
core.py. See README.md.
"""

import sys

import core
from core import main, rescore_missing_flags, rescore_all, rescore_urls

if __name__ == "__main__":
    if "--rescore-url" in sys.argv:
        urls = sys.argv[sys.argv.index("--rescore-url") + 1:]
        if not urls:
            sys.exit("--rescore-url: give one or more archived job URLs.")
        rescore_urls(urls)
    elif "--rescore-all" in sys.argv:
        i = sys.argv.index("--rescore-all")
        nxt = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        rescore_all(min_score=int(nxt) if nxt.isdigit() else None)
    elif "--rescore" in sys.argv:
        rescore_missing_flags()
    else:
        if "--watch" in sys.argv:
            if not core.WATCHLIST:
                sys.exit("--watch: this profile has no [[watch]] entries (see watchlist.py).")
            core.WATCH_ONLY = True
        main()
