# Job Search Pipeline

A local, privacy-preserving job-search tool: scrapes Jobindex (and optionally The Hub),
scores roles against a profile with a local LLM (Ollama / Qwen3.6-27B), de-duplicates across
sources, and keeps an actionable, status-aware shortlist of currently-open matches.

Built end-to-end with Claude Code. It runs entirely on local hardware: the scoring model is
served by a local Ollama instance, so job data and the candidate profile never leave the machine.

## Run order

Run the lettered scripts in order. `config.py` and `core.py` are shared libraries, you don't
run them directly.

| File | Role | When to run |
|------|------|-------------|
| **`config.py`** | All settings (search terms, filters, model, thresholds, profiles) | edit, don't run |
| **`core.py`** | The engine (scrape, fetch, score, archive, report) | imported, don't run |
| **`a_scrape.py`** | **STEP A** — search + score + rebuild the shortlist | first, and regularly |
| **`b_analyze.py`** | **STEP B** — review the dataset (read-only) | anytime |
| **`c_prepare.py`** | **STEP C** — prep a chosen role + log it to the tracker | when you pick a role |

```bash
python a_scrape.py            # find & score roles -> Weekly_Job_Matches.md
python b_analyze.py           # overview of the dataset + open shortlist
python c_prepare.py           # list the shortlist, each row tagged with its tracker status
python c_prepare.py --new     # list only roles not applied to yet
python c_prepare.py 3         # prep shortlist item #3   (or: python c_prepare.py <url>)
python c_prepare.py --status <url> applied   # update a tracked role's status
```

## Profiles (running it for someone else)

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

The `--profile` flag works on `b_analyze.py` and `c_prepare.py` too. Note: the tool runs on the
owner's hardware, so "running it for someone else" means you run it and hand back their shortlist.

## Sources

Teasers come from a small source seam (`iter_sources` in `core.py`); each source yields the
same teaser shape and is isolated, so one failing source can't take down the run.

- **Jobindex** (always on) — Playwright scrape over `TARGET_QUERIES`.
- **The Hub** (`thehub.io`, off by default) — Nordic startup/scaleup board, English-first and
  tech-heavy. Hits the JSON search API directly. To enable: confirm the endpoint with the curl
  in `config.py`'s Hub section, then set `THEHUB_ENABLED = True`.

The same role from two sources is collapsed by a **canonical URL** key plus a normalised
company+title fallback, and that key links archive rows to the tracker.

## What it produces

- `job_market_data/job_market_data.csv` — the archive: every scored role.
- `job_market_data/Weekly_Job_Matches.md` — the open shortlist (the actionable list).
- `job_market_data/runs.csv` — one row per run: timing + funnel counts.
- `applications/` — per-role Application Briefs + `applications.csv` (the tracker).

(For a profile run, the same files live under `job_market_data/_profiles/<name>/` and
`applications/_profiles/<name>/`.)

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
- `REQUIRE_COMMUTABLE` — `True` keeps only commutable / remote roles; `False` drops the filter.
- `REPORT_FRESH_DAYS` — how long a no-deadline role stays on the shortlist (default 21).
- `SCORE_THRESHOLD`, `TARGET_QUERIES`, `MODEL`.
- `THEHUB_*` — The Hub source (off until confirmed).

The employment-type and commute filters are **views**: every role is scored on merit and
stored regardless, so changing a filter re-surfaces matching roles without re-scoring.

## When you add/remove archive columns

If you change `ARCHIVE_FIELDS` in `core.py`, the CSV header no longer matches. Back up and
rebuild once:

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

Plus a running Ollama serving the model named in `config.py` (`MODEL`). `b_analyze.py` needs
only the standard library.
