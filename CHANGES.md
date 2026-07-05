# Patch set — 2026-07-02

Fixes the two reported regressions (Danish roles leaking into the shortlist; unrelated
matches) plus the bugs and improvements found in review. All logic changes are covered by a
synthetic-archive test run (migration, filters, dedup, both CLI tools) before delivery.

## Deploy

1. **Diff `config.py` against your live copy first.** If your on-disk version already has
   the 2026-06-29 profile refactor (owner via `JOBSEARCH_OWNER` + toml), reconcile rather
   than overwrite — this patch set was built from the Project's copy, which predates it.
   `core.py`, `b_analyze.py`, `c_prepare.py`, `a_scrape.py` are safe to drop in either way.
2. Copy the files over the repo copy on the Z8.
3. `profiles/borja.toml` is included (generated verbatim from the old hardcoded
   `CANDIDATE_PROFILE`/`LOCATION_ANCHOR`). Confirm `.gitignore` still covers
   `profiles/*.toml` except `_template.toml` before committing anything.
4. `export JOBSEARCH_OWNER=borja` (in `~/.bashrc` if not already there).
5. First run migrates the archive (adds `ad_language`, drops nothing) and `runs.csv`
   automatically.
6. Run `python a_scrape.py --rescore` once — it re-fetches and re-scores the still-open
   shortlist rows that predate the `danish_level`/`ad_language` columns. **This is what
   actually clears the Danish roles currently stuck on your shortlist.**

## Bug fixes

- **`b_analyze.py` read the removed `danish_required` column** → Danish stats were always
  0 and the shortlist DK flag never showed. It also reimplemented the shortlist filters by
  hand and never applied `EXCLUDE_DANISH_REQUIRED` — so its shortlist silently diverged
  from the report. Rewritten to call `core.shortlist_with_reasons()`; it now prints the
  exact same shortlist as the report/c_prepare, plus a breakdown of *what was hidden and by
  which filter*.
- **`c_prepare.py` briefs read `danish_required` too** → every brief said "Danish required:
  no / not stated". Now shows the real `danish_level` enum plus "ad written in Danish"
  when applicable, and "unknown (old scoring)" instead of a false "no".
- **Shortlist dedup was highest-score-wins** → a re-scored role could never supersede its
  older (possibly wrong) row. Now latest-scoring-wins (tiebreak: score), in both
  `core.open_shortlist` and `c_prepare._archive_row`. This is what makes `--rescore` work.
- **The Hub pagination aborted a query when a page was all duplicates** — with shared
  dedup across queries and `mostPopular` sorting, later queries often died on page 1.
  Now only an empty result page ends a query.

## Danish handling (the "non danish ad" option, done properly)

- New **`ad_language` archive column**: the ad's *writing* language, detected
  deterministically (lingua/langdetect) at scoring time — separate from `danish_level`,
  which stays the LLM's judgement of the *role's requirement*.
- New **`EXCLUDE_DANISH_ADS` view filter** (per-profile: `hide_danish_ads = true`, set in
  your toml): hides Danish-written ads from the shortlist with **zero recall loss** —
  unlike `DROP_DANISH_LANGUAGE_ADS`, everything is still fetched, scored, archived, and
  reappears if you flip it off. `DROP_DANISH_LANGUAGE_ADS` remains available but is no
  longer the way to get a Danish-free shortlist.
- **Belt-and-braces**: an ad written in Danish that the model graded `danish_level="none"`
  is lifted to `"preferred"` (an all-Danish ad almost never needs zero Danish).
- **Prompt rule added**: all-Danish ads that never say English is fine → at least
  "preferred"; Danish-facing roles → "required".
- **Blank-flag rows** (scored before these columns existed) are kept but flagged
  "⚠ flags unknown" in the report, counted in b_analyze, and fixable via
  `a_scrape.py --rescore`.

## Match-quality fixes

- **The Hub now goes through the keyword prefilter**: jobs arriving with a full body get
  the INCLUDE check over title+body (previously Hub jobs skipped INCLUDE entirely — a main
  driver of junk reaching the LLM from the broad Hub queries).
- **`TRACK_B_MIN_SCORE = 80`** view filter: Track B is prompted into a 70–90 band, so with
  the 75 threshold nearly any office role at a "tech company" made the shortlist. Track B
  now has its own bar; Track A keeps `SCORE_THRESHOLD`.
- **Prompt rule added**: "technical" means software/data/IT — roles in unrelated
  engineering/science/finance domains score ≤ 35 unless the day-to-day work is genuinely
  software/data. No points for the word "engineer" alone.
- **Fetch retry**: one retry before a role falls back to snippet scoring (snippet-scored
  rows are archived forever with guessed Danish flags).
- **`snippet_fallback` now logged per run** in `runs.csv` (with auto-migration of the old
  header) and surfaced in b_analyze's run history — a spike there means fetching broke and
  that run's matches/flags are degraded. Previously this failure mode was invisible.

## Scope expansion

- **Jobindex**: +7 Track A queries (analyse, digitalisering, AI, analytics, backend,
  IT operations, system administration).
- **The Hub**: +4 queries (analytics, backend, platform engineer, intern) — safe now that
  Hub bodies are prefiltered. Per-profile override available (`thehub_queries` in toml).
- **Jobnet scaffold** (`scrape_jobnet` behind `JOBNET_ENABLED=False`): Denmark's public
  job board, same ship-disabled-until-verified pattern The Hub used. Endpoint + field
  names must be confirmed once in devtools (steps in config.py) before enabling — the
  field names in `_jobnet_teaser` are marked as unverified guesses.

## Personal data → profiles (repo hygiene)

- `config.py` no longer contains any personal facts. **Every** run — owner included —
  loads `candidate_profile`, `location_anchor`, `name`, and the Danish preferences from
  `profiles/<name>.toml` (gitignored). Owner = whoever `JOBSEARCH_OWNER` names; a clear
  error explains setup if neither the env var nor `--profile` is given.
- `profiles/borja.toml` generated verbatim from the old hardcoded blocks (verified: Ø/ø
  round-trip, profile length, toml parses). Sets `hide_danish_ads = true` and keeps
  `danish_ok = false`, matching the two options you wanted on.
- `_template.toml` updated: owner setup steps, `name`, `hide_danish_ads`.

## Not changed (deliberately)

- `MODEL = "qwen3.6:27b-q8_0"` left as-is — but the match-quality regression coincides
  suspiciously with the model swap. Recommended A/B: re-score ~20 archived jobs with
  `qwen3:27b-q8_0` vs the new tag and compare against your own judgement before trusting
  either. `think: False` on a reasoning model is also worth an A/B if scoring judgement
  still feels off after this patch.
- `SCORE_WORKERS=4` only parallelises if `OLLAMA_NUM_PARALLEL` is set on the server —
  check `errors` in runs.csv if runs feel slow or flaky.

---

# Addendum — 2026-07-02 (model update)

- **MODEL → `gemma4:31b-it-q8_0`** (Gemma 4 31B dense, Apache 2.0, 34GB q8). Chosen for the
  task's actual profile: multilingual (Danish!) judgment + structured output, not coding.
  Qwen 3.6-27B's headline gains are coding-focused, and the match-quality regression
  coincided with switching to it. Alternatives documented in config.py.
- **`core.score_job` gains an optional `model=` parameter** (default unchanged).
- **New `d_model_ab.py`**: re-fetches your N most recent archived roles and scores them
  with two models side by side (scores, track, danish_level, latency, disagreement counts).
  Read-only. Run it BEFORE trusting any model switch — including this one.
- Gemma 4 note: with thinking disabled the 31B may emit an empty thought block before the
  JSON; the existing `{...}` parser fallback handles it. Keep `think: False` for scoring.
