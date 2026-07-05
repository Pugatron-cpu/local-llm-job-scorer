"""
d_model_ab.py — compare two scoring models on YOUR archive, before trusting either.

Re-fetches the N most recently scored roles live and scores each with BOTH models on
identical input, then prints scores, tracks and Danish grades side by side plus timing.
Read-only: nothing is written to the archive or the report. Use it whenever a new model
looks tempting — benchmarks measure coding and trivia, not "does this Copenhagen student
role fit this candidate", so decide on your own data.

    python d_model_ab.py                                  # config.MODEL vs the previous model
    python d_model_ab.py gemma4:31b-it-q8_0 qwen3.6:27b-q8_0
    python d_model_ab.py <modelA> <modelB> 25             # compare on 25 roles

Both models must already be pulled in Ollama. Needs Playwright (re-fetches pages live).
"""

import sys
import time
import csv
import os

import config
import core

DEFAULT_B = "qwen3.6:27b-q8_0"   # the previous production model
DEFAULT_N = 15


def _recent_rows(n: int) -> list:
    """The n most recently scored DISTINCT roles (latest row per canonical URL), mixing
    shortlist hits and misses so the comparison covers both sides of the threshold."""
    rows = core._dedup_archive(config.MASTER_ARCHIVE)
    rows.sort(key=lambda r: (r.get("scraped_date") or "", r.get("score", 0)), reverse=True)
    return rows[:n]


def main(argv):
    model_a = argv[0] if len(argv) > 0 else config.MODEL
    model_b = argv[1] if len(argv) > 1 else (DEFAULT_B if config.MODEL != DEFAULT_B
                                             else "gemma4:31b-it-q8_0")
    n = int(argv[2]) if len(argv) > 2 else DEFAULT_N

    if not os.path.isfile(config.MASTER_ARCHIVE):
        sys.exit("No archive yet — run a_scrape.py first so there is something to compare on.")
    rows = _recent_rows(n)
    if not rows:
        sys.exit("Archive is empty.")

    print(f"A = {model_a}\nB = {model_b}\nRe-fetching {len(rows)} recent roles live...\n")
    jobs = [{"title": r.get("title", ""), "company": r.get("company", ""),
             "snippet": "", "url": r.get("url", ""), "_old_score": r.get("score", "")}
            for r in rows]
    core.fetch_all(jobs)

    header = (f"{'old':>4} | {'A':>4} {'trk':<3} {'danish':<9} | "
              f"{'B':>4} {'trk':<3} {'danish':<9} | {'Δ':>4}  role")
    print(header)
    print("-" * len(header))

    deltas, track_dis, danish_dis = [], 0, 0
    t_a = t_b = 0.0
    compared = 0
    for job in jobs:
        desc = job.pop("_fetched", "")
        job.pop("_fetch_err", "")
        if not desc:
            print(f"{'':>4} | {'skip: fetch failed':<25} | {'':>23} | {'':>4}  "
                  f"{job['title'][:38]}")
            continue
        t0 = time.monotonic(); ra = core.score_job(job, desc, model=model_a); t_a += time.monotonic() - t0
        t0 = time.monotonic(); rb = core.score_job(job, desc, model=model_b); t_b += time.monotonic() - t0
        if "error" in (ra["reasoning"], rb["reasoning"]):
            print(f"{'':>4} | {'skip: scoring error':<25} | {'':>23} | {'':>4}  "
                  f"{job['title'][:38]}")
            continue
        compared += 1
        d = ra["score"] - rb["score"]
        deltas.append(abs(d))
        if ra["track"] != rb["track"]:
            track_dis += 1
        if ra["danish_level"] != rb["danish_level"]:
            danish_dis += 1
        print(f"{str(job['_old_score']):>4} | {ra['score']:>4} {ra['track']:<3} "
              f"{ra['danish_level']:<9} | {rb['score']:>4} {rb['track']:<3} "
              f"{rb['danish_level']:<9} | {d:>+4}  {job['title'][:38]}")

    if not compared:
        sys.exit("\nNothing compared (all fetches/scorings failed).")
    print("-" * len(header))
    print(f"compared {compared} roles"
          f" · mean |Δscore| = {sum(deltas)/len(deltas):.1f}"
          f" · track disagreements = {track_dis}"
          f" · danish_level disagreements = {danish_dis}")
    print(f"avg latency  A: {t_a/compared:.1f}s   B: {t_b/compared:.1f}s")
    print("\nNow the human part: for the rows where A and B disagree, open the ad and decide "
          "which grade YOU agree with. The better model is the one that matches your own "
          "judgement — not the higher scorer.")


if __name__ == "__main__":
    main(sys.argv[1:])
