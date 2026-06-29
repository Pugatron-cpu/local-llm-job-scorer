"""
a_scrape.py — STEP A: run the job search.

Scrapes Jobindex, filters, fetches full ads, scores them with the local LLM, appends to the
archive (job_market_data/job_market_data.csv), and rebuilds the open-roles shortlist
(job_market_data/Weekly_Job_Matches.md). Run this first, and regularly.

    python a_scrape.py

All settings live in config.py; the engine lives in core.py. See README.md.
"""

from core import main

if __name__ == "__main__":
    main()
