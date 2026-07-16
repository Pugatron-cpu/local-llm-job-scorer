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

from core import main, rescore_missing_flags, rescore_all

if __name__ == "__main__":
    if "--rescore-all" in sys.argv:
        rescore_all()
    elif "--rescore" in sys.argv:
        rescore_missing_flags()
    else:
        main()
