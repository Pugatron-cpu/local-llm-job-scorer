# Job Search Pipeline

A local, privacy-preserving job-search tool: pulls roles from Jobindex, The Hub, and a
watchlist of companies' public career APIs (plus an optional Jobnet scaffold), scores them
against a profile with a local LLM (Ollama; model chosen via `MODEL_PRESETS` in
`config.py`), de-duplicates across sources, and keeps an actionable, status-aware shortlist
of currently-open matches. The mechanical facts about each role (employment type, work
mode, location, commute, deadline, skills) are owned by a deterministic extraction layer
(`extractors.py`); the LLM does the fit judgment.

Built end-to-end with Claude Code. It runs entirely on local hardware: the scoring model is
served by a local Ollama instance, so job data and the candidate profile never leave the machine.

## Run order

Run the lettered scripts in order. `config.py` and `core.py` are shared libraries, you don't
run them directly.

| File | Role | When to run |
|------|------|-------------|
| **`config.py`** | All settings (search terms, filters, model presets, thresholds, profiles) | edit, don't run |
| **`core.py`** | The engine (scrape, fetch, score, archive, report) | imported, don't run |
| **`extractors.py`** | Deterministic extraction of the mechanical fields (see below) | imported, don't run |
| **`a_scrape.py`** | **STEP A** — search + score + rebuild the shortlist | first, and regularly |
| **`b_insights.py`** | **STEP B** — insights over your data (read-only) | anytime, to see funnel / market / skills |
| **`c_prepare.py`** | **STEP C** — prep a chosen role + log it to the tracker | when you pick a role |

```bash
python a_scrape.py            # find & score roles -> Weekly_Job_Matches.md
python a_scrape.py --rescore  # maintenance: refresh open rows missing Danish/ad-language flags
python a_scrape.py --rescore-all  # re-score ALL open rows (use after a model/prompt change)
python a_scrape.py --model-preset fallback  # score with another MODEL_PRESETS entry (see below)
python b_insights.py          # funnel + score-vs-behaviour + market + skill demand
python b_insights.py --funnel # just the application funnel, response times & overdue chasing
python b_insights.py --market # just the market view (score / Danish gate / employers)
python b_insights.py --skills # just the ranked skill-demand table
python c_prepare.py           # list the shortlist, each row tagged with its tracker status
python c_prepare.py --new     # list only roles not applied to yet
python c_prepare.py 3         # prep shortlist item #3   (or: python c_prepare.py <url>)
python c_prepare.py --status <url> applied   # update a tracked role's status (also logs the transition)
python c_prepare.py --score-tracker  # backfill model scores for roles added by URL (eval set)
python c_prepare.py --rebrief <url>  # regenerate the brief for a role already in the tracker
python c_prepare.py --archive-briefs # sweep settled briefs out of the queue into _archive/
python c_prepare.py --clear-stale    # DRY RUN: which queued roles have aged out (default 30d)
python c_prepare.py --clear-stale 14 --yes   # apply it: mark them skipped, archive their briefs
```

`--rebrief` exists because prepping an exact-URL duplicate is deliberately a no-op (no re-fetch,
no new brief), which leaves no way to recover a brief that went missing or refresh a stale one.
It rewrites only the brief and the row's `brief_file`; status, dates and notes are never touched,
and a settled role's brief is regenerated straight into `_archive/` rather than back into the
queue. If the ad can't be fetched and the transform returns nothing, it refuses rather than
overwrite a good brief with an empty one.

## Running the scrape every morning (`jobctl.py`)

```bash
python jobctl.py on         # scrape daily at 06:00 Europe/Copenhagen
python jobctl.py on weekly  # ...Mondays instead (e.g. once you've landed a job)
python jobctl.py on monthly # ...the 1st instead
python jobctl.py off        # stop it
python jobctl.py status     # armed? what cadence? did the last scrape actually find anything?
python jobctl.py run        # run it now
python jobctl.py logs       # what it printed
```

**`off` means off.** `Persistent=true` does not keep it ticking while disarmed — it only means a
run missed because the machine was *powered down* happens once at next boot, while the timer is
on. The cadence lives only in the unit file (never mirrored into a config, so they can't drift);
`status` reads it back out of systemd, so what it prints is what will actually happen.

It installs a systemd **user** timer, so `on`/`off` need no `sudo` and it survives logout
(linger is enabled for this user). `Persistent=true`, so a run missed while the box was off
happens at next boot instead of being silently skipped. A few minutes of jitter keep the
scrape off a fixed 06:00:00 heartbeat.

**Only STEP A is automated.** `c_prepare.py` stays manual: briefs are written for roles *you*
chose, not for everything the scraper finds.

Two things the units handle that scheduled jobs usually get wrong. systemd never sources
`~/.bashrc`, so `JOBSEARCH_OWNER` is captured into the unit at install time (without it the run
dies picking a profile), and the venv interpreter is baked in by absolute path. The schedule
also pins `Europe/Copenhagen` in `OnCalendar`, so it stays 06:00 Danish time across DST and even
if the host is on UTC.

`status` reads `runs.csv`, not just systemd. A scrape that exits 0 but finds nothing (a job board
changed its markup) is the failure that otherwise goes unnoticed for weeks — so check the scored
/ matches counts, not just "success".

## Profiles (running it for someone else)

**Check a profile before you trust it: `python profile_check.py <name>`.** No scrape, no LLM, ~1s.

A profile in another field (finance, treasury, law) inherits **tech-shaped defaults** —
`TECH_TERMS` is full of `kubernetes` and `mlops`. Give it treasury queries but no treasury
vocabulary and the pipeline does not fail: it scrapes fine, drops everything at the keyword gate,
scores nothing, and returns an empty shortlist. From the outside, *"no results"* is indistinguishable
from *"no such jobs exist in Denmark"*. That silent failure is the main trap in running this for
someone else, and `profile_check.py` is what makes it loud — it runs the profile's own queries
through the profile's own gate and tells you if the two disagree.

Per-profile keys (all optional; each falls back to the `config.py` default):

| what it controls | keys |
|---|---|
| what gets searched | `queries`, `thehub_queries`, `ats_companies`, `excluded_companies` |
| the keyword gate | `tech_terms`, `bridge_terms`, `include_terms`, `exclude_terms` |
| the scoring rubric | `track_a_def`, `track_b_def`, `hard_no`, `target_sector`, `track_b_bridge` |
| the shortlist view | `accepted_employment_types`, `graduate_programmes`, `score_threshold`, `require_commutable`, `danish_ok`, `hide_danish_ads` |
| the deterministic extractors | `commutable_areas`, `skills_vocab` |
| the brief handoff | the `brief_*` wording |

**The rubric is per-profile too, and for a non-tech profile it has to be.** The `config.py`
defaults are tech-shaped: Track A means "SOFTWARE/DATA/IT technical" and explicitly caps
`finance/audit` at ≤ 35. Inherit that in another field and every role you scrape is capped at 35 by
a rubric written for someone else — the pipeline reports success and the shortlist is empty. So a
treasury profile sets its own `track_a_def`; `track_b_def = ""` drops the foot-in-the-door track
entirely (the model is then told there is ONE kind of role and can only answer `"A"` or `"none"`).

The two tracks are a **strategy, not a domain**: Track A is the roles you want, Track B is adjacent
roles at employers in your target sector, taken as a way in. Only the vocabulary is domain-specific.

Changing the rubric changes the scores, and old rows are then no longer comparable to new ones.
`tests/test_score_prompt.py` pins the owner's rendered prompt byte-for-byte so that can't happen by
accident; the defaults reproduce it exactly. If you edit the rubric deliberately, re-score
(`a_scrape.py --rescore-all`) or accept that rows before and after are on different scales.

**Known limit:** `is_tech_company` is still the archive *column name* (renaming it means an archive
migration). For a non-tech profile, read it as "is the employer in this candidate's target sector",
which is what `target_sector` defines.

By default the tool runs for one owner against the top-level `job_market_data/` and
`applications/` folders, exactly as above. The owner is whoever `JOBSEARCH_OWNER` names (see
First-time setup); their settings live in `profiles/<owner>.toml`, same as anyone else. No
personal data is hardcoded in the repo. You can also run it for someone else without touching
your own data:

```bash
python a_scrape.py --profile jan
```

This loads `profiles/jan.toml` and runs fully sandboxed: it judges fit against that person's
profile and commute, and writes everything to `job_market_data/_profiles/jan/`. Nothing a
profile run does can land in, or be skipped because of, the owner's archive. Their shortlist
is `job_market_data/_profiles/jan/Weekly_Job_Matches.md`.

To add a profile:

1. Collect the person's details with `profiles/QUESTIONNAIRE.md` (send it to them; a CV helps).
2. Copy `profiles/_template.toml` to `profiles/<name>.toml` and fill it in (candidate profile,
   commute rule, and optionally search terms, a `danish_ok` flag, and a `require_commutable` flag).
3. Run `python a_scrape.py --profile <name>` and share the resulting shortlist.

The `--profile` flag works on `b_insights.py` and `c_prepare.py` too. Note: the tool runs on the
owner's hardware, so "running it for someone else" means you run it and hand back their shortlist.

## Sources

Teasers come from a small source seam (`iter_sources` in `core.py`); each source yields the
same teaser shape and is isolated, so one failing source can't take down the run.

- **Jobindex** (always on) — Playwright scrape over `TARGET_QUERIES`.
- **The Hub** (`thehub.io`, on) — Nordic startup/scaleup board, English-first and tech-heavy.
  Hits the JSON search API directly (`THEHUB_ENABLED = True`, endpoint verified 2026-06-25). To
  turn it off, set `THEHUB_ENABLED = False`.
- **ATS watchlist** (`greenhouse.io` / `lever.co`, on) — polls the **public career APIs of a
  hand-picked list of companies** you'd actually want to work at (`ATS_COMPANIES` in
  `config.py`, or `ats_companies` in your toml). No auth, no scraping; high precision, and it
  surfaces roles that never hit Jobindex/The Hub. A location filter (`ATS_LOCATION_KEEP`) keeps
  a big global board from flooding scoring with non-commutable roles. Add a company by testing
  its slug: `curl https://boards-api.greenhouse.io/v1/boards/<slug>/jobs`.
- **Jobnet** (`jobnet.dk`, off) — scaffold for Denmark's public job board. Left disabled: as of
  2026-07 its search API sits behind a StarPlatform (MitID) login, so there is no public
  keyword search to use; see the note in `config.py`.

The same role from two sources is collapsed by a **canonical URL** key plus a normalised
company+title fallback (`role_key`), and that key links archive rows to the tracker. The
tracker reuses the same key both ways: on the shortlist an already-applied role is tagged
(not shown as *new*), and at prep time prepping an **exact-URL duplicate is skipped entirely**
(no re-fetch, no new brief), while a **re-post under a different URL** is flagged before a
second row is logged (you confirm before it's added). A brief `.md` is written only when a
tracker row is added, so the dated `applications/*.md` files stay a clean "what to apply next"
queue. Prepping also prints a heads-up listing any other roles you already track at that
employer.

The queue also **forgets**. A brief sits in `applications/` only while its role is still worth
acting on (status `interested`, per `BRIEF_QUEUE_STATUSES`). The moment a role settles
(applied / rejected / skipped / ...) its brief is **moved** into `applications/_archive/` — moved,
never deleted, and still resolved by name if you re-paste that URL. Without this the folder just
grows: it hit 71 briefs for 7 live roles before the archive existed, drowning the "what to apply
next" signal it was supposed to be.

The sync runs **on every `c_prepare.py` invocation**, silently unless it actually moves something,
and it works **both ways**: settle a role and its brief leaves the queue; put one back in play
(status edited back to `interested`) and its brief comes back out of `_archive/`. So the tracker
is the single source of truth and the folder follows it — edit `applications.csv` by hand however
you like, and the next time you run the tool at all, the briefs sort themselves out. Files are
only ever moved, never deleted or rewritten. `--archive-briefs` runs the same sync on demand and
prints what it moves.

Note that a hand-edited status still skips what `--status` gives you: the `status_history.csv`
transition row (which `b_insights --funnel` reads for response times) and the tracker snapshot.
The archive self-heals; the funnel history does not.

### Ageing the queue out (`--clear-stale`)

Archiving only reacts to a status **you** set. A role you prepped, looked at, and never settled
keeps status `interested` forever, so its brief never leaves — and the queue drifts back into the
same noise the archive was built to stop, just more slowly. `--clear-stale` is the ageing pass:
it settles queued roles older than `STALE_AFTER_DAYS` (config, default 30) or past a stated
deadline as `skipped`, then runs the normal archive sweep.

- **Dry run by default.** It prints its verdict and writes nothing until you add `--yes`. This is
  the one command that settles rows you never touched, so a typo'd day count shouldn't be able to
  bury a fortnight of work.
- **Age comes from the tracker's `date_added`, not the brief filename** — `--rebrief` rewrites a
  brief without changing when the role was found, and judging by file date would reset that clock.
- **A deadline that doesn't parse is treated as no deadline**, never as a reason to skip. The
  transform occasionally emits junk into that column; it must not cost you a live role.
- **Rows with an unreadable `date_added` are listed and left alone.** Guessing at a date we
  couldn't read, to bury a role silently, is the wrong trade. Settle those with `--status`.
- It goes through the same plumbing as any other status change — tracker snapshot to `_backups/`,
  one `status_history.csv` row per transition, and a `notes` stamp saying it was automatic. Undo
  is the usual one: set a status back to `interested` and the next run restores its brief.

It never touches orphan `.md` files with no tracker row (e.g. briefs left by a failed prep, where
company extraction produced `..._role_2.md`). The sweep is driven by each row's `brief_file`, so
files no row points at are invisible to it and have to be moved by hand.

## Deterministic extraction (`extractors.py`)

Several fields the scorer records are mechanical, not judgmental: whether an ad says
"studentermedhjælper", which city it names, whether a stated deadline parses. The LLM gets
those wrong occasionally (and differently per model); a keyword rule gets them right every
time or knows that it doesn't know. So after each role is scored, a deterministic merge
(`core.merge_extracted_fields`) overwrites the six mechanical fields — `employment_type`,
`work_mode`, `location`, `commute_ok`, `deadline`, `matched_skills` — wherever an extractor
is confident; where it isn't, the LLM's answer stands. The judgment fields (`score`,
`track`, `reasoning`, `is_tech_company`) are never touched, and the scoring prompt is
byte-for-byte unchanged, so scores stay on the archive's scale.

The contract (pinned by the tests): extractors **fill fields, never filter** — nothing in
this layer can drop a role — and they return a confident value or a sentinel, never a
guess. `danish_level` is the one special case: an explicit "dansk er et krav" in the ad
raises the LLM's grade to `required` (a floor; it can never lower it). Each run logs how
many fields were set deterministically vs left to the LLM (`fields_det` / `fields_llm` in
runs.csv) — that coverage is also the evidence gate for the future slim-prompt stage
(`PLAN_STAGE5.md`).

Two profile keys feed this layer (both optional, see `_template.toml`):

- `commutable_areas` — the geography `commute_ok` checks stated locations against. The
  `config.py` default is the owner's Copenhagen circle, so **a profile anchored anywhere
  else must set its own list** (or `[]` to disable the deterministic check and let the
  scorer's judgment stand).
- `skills_vocab` — skill names to detect as whole words in ad text. When set,
  `matched_skills` becomes "which of THESE appear in the ad" instead of the model's
  free-associated list, which makes `b_insights --skills` far more comparable across roles.

## Model presets (`MODEL_PRESETS`)

Scores are only comparable within one model, so the model is managed explicitly:

- `config.MODEL_PRESETS` defines two presets, each pinned to a model, an **Ollama endpoint
  (i.e. a GPU)**, a context window, worker count, and per-request scoring timeout:
  - `fast` (default, unchanged behaviour) — the 31B on the **48GB 3090 NVLink pool**, served
    by the main Ollama instance on `:11434`; 4 workers, `num_ctx` 8192.
  - `fallback` (`gemma4:12b-it-q8_0`) — the 12B on the **RTX A4000**, served by a *separate*,
    A4000-pinned Ollama instance on `:11436` (see **A4000 fallback endpoint** below); 1 worker,
    `num_ctx` 4096, longer timeout. For when the 3090 pool is busy or absent.
- Select with `--model-preset <name>` or `JOBSEARCH_MODEL_PRESET=<name>`. Selection is
  **explicit only — there is no auto-failover**: every run preflights Ollama at start
  (`core.ensure_model_available`) against *that preset's* endpoint and exits loudly, naming
  both presets, if the chosen model isn't being served. Silently switching models would
  silently change the score scale.
- Every archived row is stamped with the exact model that scored it (`scoring_model`), and
  runs.csv records the active preset + model per run. Rows from before the column are
  blank.

### A4000 fallback endpoint

The `fallback` preset points at `http://localhost:11436`, a **second Ollama instance pinned to
the A4000**, kept separate from the main `:11434` instance (which is locked to the two 3090s via
`CUDA_VISIBLE_DEVICES=0,2`). The pipeline does not create it — stand it up once, host-side. It
shares the existing model store, so the 12B is already present (no re-pull).

(`:11436` is just a free port on this host — `:11435` is already taken by an unrelated Docker
Ollama container. If you change the port, change it in both `MODEL_PRESETS["fallback"]["ollama_url"]`
and the unit's `OLLAMA_HOST` below.)

Pinning this instance to *only* the A4000 needs two settings, and both matter (learned the
hard way on Ollama 0.32):

1. **Pin CUDA by GPU UUID, not by numeric index.** Ollama does not order devices by PCI bus,
   so `CUDA_VISIBLE_DEVICES=1` put the model on a 3090. A UUID is unambiguous. Get it with:
   `nvidia-smi --query-gpu=index,name,uuid --format=csv` (A4000 UUID on this host below).
2. **Disable the Vulkan backend (`OLLAMA_VULKAN=0`).** Ollama 0.32 enables Vulkan by default,
   and Vulkan enumerates GPUs *independently* of `CUDA_VISIBLE_DEVICES` — it re-discovered the
   two 3090s and the scheduler preferred them (more free VRAM), so the CUDA pin alone was not
   enough. With Vulkan off, only the CUDA-pinned A4000 is visible. (Native CUDA is also ~4x
   faster here than the Vulkan path: ~12s vs ~48s per score.)

Create `/etc/systemd/system/ollama-a4000.service`:

```ini
[Unit]
Description=Ollama (A4000, port 11436)
After=network-online.target

[Service]
User=ollama
Group=ollama
# Pin to the A4000 by UUID (NOT index — Ollama doesn't order devices by PCI bus).
Environment="CUDA_VISIBLE_DEVICES=GPU-cb9e616d-a778-32ae-4ba8-22d0b37e729f"
# Vulkan ignores CUDA_VISIBLE_DEVICES and would re-add the 3090s — turn it off.
Environment="OLLAMA_VULKAN=0"
Environment="OLLAMA_HOST=127.0.0.1:11436"
Environment="OLLAMA_NUM_PARALLEL=1"           # matches the fallback preset's 1 worker
Environment="OLLAMA_CONTEXT_LENGTH=4096"      # matches the fallback preset's num_ctx
Environment="OLLAMA_MODELS=/usr/share/ollama/.ollama/models"   # shared store — no re-pull
ExecStart=/usr/local/bin/ollama serve
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ollama-a4000
curl -s 127.0.0.1:11436/api/tags | grep gemma4:12b   # confirm it serves the 12B
# Confirm placement: after one score, the A4000 (not a 3090) should hold ~13 GB:
#   nvidia-smi --query-gpu=index,name,memory.used --format=csv,noheader
```

Then `python a_scrape.py --model-preset fallback` scores on the A4000. Two instances sharing one
model store is fine (reads only — don't `ollama pull` on both at once). The A4000 12B runs
*sequentially* and on a slower card, so the scoring stage is markedly slower than the 3090 pool —
expected for a degraded-mode fallback. Its scores sit on the 12B's own scale (`scoring_model`
records it); don't compare 12B rows against 31B rows.

## Graduate programmes (`graduate_programmes`)

Graduate programmes are full-time, so `ACCEPTED_EMPLOYMENT_TYPES` hides them from a student
shortlist, even though they're exactly what you apply to in your final year. Setting
`graduate_programmes = true` in a profile lets them through **without** accepting full-time
roles in general:

- A role counts as a graduate programme if its **title** says `graduate`, `trainee` or
  `early career` (`extractors.is_graduate_programme`; `undergraduate` / `postgraduate` don't
  match). It's decided from the title when the shortlist is built, so there's no archive column, no
  prompt change, and roles scored before the option existed are covered straight away.
- They appear in their own **Graduate programmes** section at the end of
  `Weekly_Job_Matches.md`, numbered after the main list, so `c_prepare.py <number>` works as
  usual (the `c_prepare` listing shows their type as `graduate`).
- Every other shortlist filter still applies: score, commute, Danish, track B bar, still open.
  `--rescore` / `--rescore-all` include them.
- **The start date is not checked.** The scorer doesn't extract it, and nearly all intakes
  recruiting in autumn start the following year, so check it yourself.
- A graduate ad with no stated deadline stays on the shortlist for `GRADUATE_FRESH_DAYS` (60)
  after it was last seen, not the usual `REPORT_FRESH_DAYS` (21): intakes recruit for months.
  A stated deadline still wins.

The default queries include a few graduate searches (`graduate programme`, `graduate data`,
`graduate AI`, ...) and `graduate` / `trainee` / `early career` are in the keyword gate: before
2026-09 no query targeted them, so they were only found by accident.

## What it produces

- `job_market_data/job_market_data.csv` — the archive: every scored role.
- `job_market_data/Weekly_Job_Matches.md` — the open shortlist (the actionable list), with
  graduate programmes in their own section at the end when the profile opts in.
- `job_market_data/runs.csv` — one row per run: timing + funnel counts.
- `job_market_data/raw_teasers.csv` — **every posting the scraper saw**, every run, logged before
  dedup and before the keyword gate, with the `passed_prefilter` verdict on each. See below.
- `applications/` — the live queue: an Application Brief per role still worth acting on, plus
  `applications.csv` (the tracker) and `status_history.csv` (append-only log of every status
  change, for funnel timing).
  - `applications/_archive/` — briefs for settled roles. Moved here, never deleted; still
    reachable, and their filenames stay reserved so a `brief_file` always names one brief.
  - `applications/_backups/` — timestamped tracker snapshots, taken before any rewrite of
    `applications.csv`. The last `TRACKER_BACKUPS_KEEP` (3) are kept; hand-named ones
    (e.g. `.bak-manualfix-...`) are never auto-pruned.

(For a profile run, the same files live under `job_market_data/_profiles/<name>/` and
`applications/_profiles/<name>/`.)

### `raw_teasers.csv` — the unfiltered record

The archive is a **biased sample by construction**: only roles matching `INCLUDE_TERMS` get scored
and kept, so roughly 4 in 5 of what the scraper actually sees is discarded in memory. That's right
for a job search and useless for anything else. `raw_teasers.csv` is the unfiltered record, and
it's the one thing that **cannot be backfilled** — miss a day and that day is gone.

One row per posting **per run**, so a still-live ad re-seen on nine consecutive runs writes nine
rows. That repetition is the signal: it's what lets you derive days-on-market, posting velocity,
which employers repost, and seasonality. Group by `canonical_url` at analysis time. The
`passed_prefilter` column records the keyword gate's verdict, so you can also ask what your own
search terms are throwing away, and tune them against real data instead of guessing.

Two analytics-only columns ride along (here and in the archive): `stated_salary` and
`stated_experience_years` — the raw kr/DKK amount and "X års erfaring"/"X+ years" phrase
exactly as the ad wrote them, blank when absent. Nothing in the pipeline reads them; they
exist so market questions ("do student ads state pay?") can be answered later from data
that can't be backfilled.

Nothing in the pipeline reads this file. The write happens before dedup/filtering/scoring, adds no
fetch and no LLM call, and is wrapped so that if it ever fails the scrape carries on regardless.
Set `LOG_RAW_TEASERS = False` in `config.py` to stop appending. Volume is ~250 rows/day.

Caveat worth remembering: it captures what *your scraper* surfaced (your `TARGET_QUERIES` on
Jobindex, plus The Hub), not the whole Danish market. Change the queries and the coverage changes
with them, so read it as "everything my scraper saw", not "everything that existed".

## Data & privacy

This repo holds the pipeline **code only**. The scored job data (`job_market_data/`) and
application materials (`applications/`) are gitignored, since they contain personal job-search
data. Per-person profiles (`profiles/*.toml`) are gitignored too (only `_template.toml` ships);
they contain other people's details. A candidate CV, if used to build a profile, is never
committed. All scoring runs against a local Ollama model, so nothing is sent to a third party.

## Settings you'll tweak most (in `config.py`)

- `OWNER_PROFILE` — the no-flag default profile name; read from the `JOBSEARCH_OWNER` env var
  (default `owner`). Per-person fields (`candidate_profile`, `location_anchor`, `name`) live in
  `profiles/<name>.toml`, not here.
- `ACCEPTED_EMPLOYMENT_TYPES` — add `"full_time"` if your situation changes.
- `GRADUATE_PROGRAMMES` — show graduate / trainee intakes despite being full-time (default
  `False`; per profile via `graduate_programmes`, see Graduate programmes above).
- `REQUIRE_COMMUTABLE` — `True` keeps only commutable / remote roles; `False` drops the filter.
- `REPORT_FRESH_DAYS` — how long a no-deadline role stays on the shortlist (default 21);
  `GRADUATE_FRESH_DAYS` (60) is the same for graduate programmes.
- `STALE_AFTER_DAYS` — how old a queued role gets before `--clear-stale` offers to skip it
  (default 30). Note this is about *your* queue going stale, not the ad closing —
  `REPORT_FRESH_DAYS` governs the shortlist, this governs `applications/`.
- `SCORE_THRESHOLD`, `TARGET_QUERIES`; the model via `MODEL_PRESETS` (+ `--model-preset` /
  `JOBSEARCH_MODEL_PRESET` — see Model presets above).
- `COMMUTABLE_AREAS` — the deterministic commute check's geography (per profile via
  `commutable_areas`).
- `THEHUB_*` — The Hub source (on; endpoint verified). `JOBNET_*` — Jobnet scaffold (off).
- `ATS_COMPANIES` — your target-employer watchlist (`"greenhouse:<slug>"` / `"lever:<slug>"`);
  `ATS_LOCATION_KEEP` — locations to keep. `ATS_ENABLED` toggles the whole source.

- `INCLUDE_TERMS` (= `TECH_TERMS` + `BRIDGE_TERMS`) and `EXCLUDE_TERMS` — the Stage-2 keyword gate.
  **The two are not symmetric.** INCLUDE is a cheap recall gate: a false positive costs one LLM
  call, so err on the side of keeping a term. EXCLUDE is a hard veto: a false positive silently
  deletes a role you'd have wanted, and you'll never know. Never add an EXCLUDE term without
  first checking it against the archive — count the titles it hits and how many of them ever
  scored >= 75. If that second number isn't zero, don't add it.

The employment-type and commute filters are **views**: every role is scored on merit and
stored regardless, so changing a filter re-surfaces matching roles without re-scoring.

## When you add/remove archive columns

If you change `ARCHIVE_FIELDS` in `core.py`, the next run realigns the CSV in place
automatically (`migrate_csv_if_needed`): existing values are kept by column name, new
columns are left blank, removed ones dropped. The same applies to `runs.csv` and
`raw_teasers.csv`. Blank values in old rows are expected, not corruption — e.g. rows
scored before `scoring_model` existed simply don't say which model scored them.

The migration can't un-scramble a file whose rows were already column-shifted by some
earlier mismatched append. Only in that case, back up and rebuild:

```bash
mv job_market_data/job_market_data.csv job_market_data/job_market_data.csv.bak
python a_scrape.py
```

## Requirements

Python 3.11+ (profiles use the stdlib `tomllib`; on 3.10 and older, `pip install tomli`).

```bash
pip install -r requirements.txt
playwright install chromium      # once, fetches the browser Playwright drives
```

## First-time setup (the owner needs a profile too)

Personal settings are **not** stored in the repo, so before the first run, set your owner name
and create your profile:

```bash
export JOBSEARCH_OWNER=yourname        # add to ~/.bashrc so it persists
cp profiles/_template.toml profiles/yourname.toml
# edit profiles/yourname.toml: candidate_profile + location_anchor (and optionally name,
# queries, danish_ok, require_commutable)
```

`profiles/*.toml` is gitignored (only `_template.toml` ships), so your profile stays local. If
`JOBSEARCH_OWNER` is unset it defaults to `owner`, and the tool will ask you to create
`profiles/owner.toml`.

Plus a running Ollama serving the active preset's model (`MODEL_PRESETS` in `config.py`).
Every scoring run checks this at start and exits with instructions if the model isn't
served — no scrape time is wasted first.
