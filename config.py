"""
config.py — all tunable settings for the job-search pipeline, in one place.

Imported by core.py (the engine), a_scrape.py, b_analyze.py, and c_prepare.py, so a value
changed here applies everywhere.

PROFILES: the tool runs for ONE owner by default (you), persisting to the normal data dirs
exactly as before. `--profile <name>` runs sandboxed for someone else (their own isolated
data under _profiles/<name>/), loading profiles/<name>.toml. See README.md.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# --- profiles ------------------------------------------------------------------------
# The owner runs against the top-level data dirs and persists, as before. Anyone else runs
# sandboxed and report-only. "Owner" = no --profile flag, or --profile <OWNER_PROFILE>.
#
# The owner's NAME is NOT hardcoded here, so this (public) file carries no personal data.
# Set it once in your environment -- e.g. `export JOBSEARCH_OWNER=yourname` in ~/.bashrc -- and
# put your personal settings in profiles/<that-name>.toml (gitignored). Defaults to "owner".
OWNER_PROFILE = os.environ.get("JOBSEARCH_OWNER", "owner").strip().lower()
PROFILES_DIR  = os.path.join(SCRIPT_DIR, "profiles")


def _read_profile_flag() -> str:
    """Peek at `--profile <name>` (or the JOBSEARCH_PROFILE env var) and REMOVE the flag + its
    value from sys.argv, so each tool's own argument parsing (c_prepare's <number>/<url>/--status,
    b_analyze's optional path) never sees it. Resolved here, not via argparse, because every tool
    does `from config import *`, so the active profile must be known before any path or query is
    read. Returns the lowercased name, or "" if none was given."""
    name = os.environ.get("JOBSEARCH_PROFILE", "").strip()
    if "--profile" in sys.argv:
        i = sys.argv.index("--profile")
        val = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        del sys.argv[i:i + 2]
        name = val.strip()
    return name.lower()


ACTIVE_PROFILE = _read_profile_flag() or OWNER_PROFILE.lower()
IS_OWNER       = (ACTIVE_PROFILE == OWNER_PROFILE.lower())


def _load_profile(name: str) -> dict:
    """Load profiles/<name>.toml (Python 3.11+ stdlib tomllib). Exits with a clear, actionable
    message if the profile or its required fields are missing."""
    path = os.path.join(PROFILES_DIR, f"{name}.toml")
    if not os.path.isfile(path):
        avail = []
        if os.path.isdir(PROFILES_DIR):
            avail = sorted(f[:-5] for f in os.listdir(PROFILES_DIR)
                           if f.endswith(".toml") and not f.startswith("_"))
        sys.exit(f"No profile '{name}' at {path}.\n"
                 f"  Available profiles: {', '.join(avail) or '(none yet)'}\n"
                 f"  Create one: copy profiles/_template.toml to profiles/{name}.toml and fill it in.")
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            sys.exit("Reading profiles needs TOML support: use Python 3.11+ or `pip install tomli`.")
    with open(path, "rb") as f:
        return tomllib.load(f)

# --- model / Ollama ------------------------------------------------------------------
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL      = "qwen3.6:27b-q8_0"   # strong structured output + multilingual, fits the 48GB pool
NUM_CTX    = 8192                  # room for a full description
TIMEOUT_S  = 180                  # never let a hung request block the run

# --- scrape / scoring knobs ----------------------------------------------------------
MAX_DAYS_OLD = 8
MAX_PAGES    = 5     # Jobindex pages per query. Raised from 3 -> 5 for recall (speed is fine).
SCORE_THRESHOLD = 75

# Which position types appear in the shortlist RIGHT NOW. VIEW filter, not a scoring rule:
# every role (full-time included) is scored on merit and stored; this just decides what
# shows. Add "full_time" when you're open to it -> existing data resurfaces, no re-scoring.
ACCEPTED_EMPLOYMENT_TYPES = {"student", "part_time", "internship", "unknown"}

# Location filter (VIEW filter, like above). The LLM judges commute_ok against the active
# profile's location_anchor (its home area, what counts as reachable, and whether fully remote
# is acceptable). Set False to drop the filter entirely (e.g. if you can relocate).
REQUIRE_COMMUTABLE = True

# The report shows only roles likely STILL OPEN:
#   stated deadline passed -> dropped; future deadline -> kept until then (trusted over age);
#   no deadline -> kept until REPORT_FRESH_DAYS after first seen, then assumed filled.
REPORT_FRESH_DAYS = 21

# Stage 3b scoring parallelism. EFFECTIVE concurrency = min(SCORE_WORKERS, the server's
# OLLAMA_NUM_PARALLEL) -- set OLLAMA_NUM_PARALLEL on `ollama serve` (each slot needs its own
# KV cache; start 2-4 for a 27B on the 48 GB pool). 1 = sequential.
SCORE_WORKERS = 4

# Stage 3a fetch parallelism. Sync Playwright is thread-affine, so each worker owns its own
# headless browser. Keep low (2-3) to stay polite to jobindex.dk. 1 = effectively sequential.
FETCH_WORKERS = 3

# True = fetch each job's full page (richer scoring + accurate language detection).
# False = score on the teaser snippet only (use if Jobindex/ATS fetching starts failing).
FETCH_FULL_DESC = True

# --- Danish handling -----------------------------------------------------------------
# Two SEPARATE knobs, because "the ad is written in Danish" and "the role requires Danish"
# are different questions. International firms post English-working student roles in Danish
# on Danish boards, so dropping by ad LANGUAGE silently bins good roles.
#
# DROP_DANISH_LANGUAGE_ADS: the old behaviour — cull ads whose MAIN LANGUAGE is Danish before
#   scoring (a confident-Danish gate on the teaser + a body-level gate). Default OFF: the
#   multilingual LLM scores every ad regardless of language and records how much Danish the
#   ROLE needs in danish_level. Turn ON only if you want to trade recall for fewer fetches.
DROP_DANISH_LANGUAGE_ADS = False

# EXCLUDE_DANISH_REQUIRED: a shortlist VIEW filter (like REQUIRE_COMMUTABLE). When True, hide
#   roles the LLM graded danish_level="required". "preferred" (Danish a plus) and "none" are
#   always kept. Set False to keep everything and just FLAG the Danish level, deciding per role.
#   A non-owner profile's `danish_ok = true` maps to False here.
EXCLUDE_DANISH_REQUIRED = True

DEBUG_DUMP_HTML = False             # True -> dump page 1 HTML so you can fix selectors

# --- candidate profile (per person) --------------------------------------------------
# CANDIDATE_NAME / CANDIDATE_PROFILE / LOCATION_ANCHOR are PER-PERSON and are loaded from the
# active profile (profiles/<name>.toml) near the bottom of this file -- for the owner and
# everyone else alike. No personal data lives in this (public) file. See profiles/_template.toml.

# --- search terms --------------------------------------------------------------------
TARGET_QUERIES = [
    # --- Track A: technical / data / AI student roles ---
    "studentermedhjælper data",
    "student assistant data",
    "studentermedhjælper IT",
    "student assistant IT",
    "data analyst student",
    "data engineer student",
    "business intelligence student",
    "machine learning student",
    "AI student assistant",
    "generative AI student",
    "LLM student",
    "junior data analyst",
    "data scientist student",
    "IT support student",
    "software student",
    "student developer",
    "python student",
    "studentermedhjælper udvikler",
    "devops student",
    "cloud student",
    "infrastructure student",
    "automation student",
    # --- Track B: foot-in-the-door roles (LLM keeps only the ones at tech companies).
    #     Noisier; comment out if a run gets too slow.
    "office assistant",
    "office coordinator",
    "kontorassistent",
    "workplace coordinator",
    "logistics coordinator student",
]

# Company-level exclusions (substring match, lowercase).
EXCLUDED_COMPANIES = [
    "københavns kommune",
    "copenhagen municipality",
    "kbh kommune",
    "kommune",
    "politi",
    "forsvaret",
]

# Stage-2 keyword pre-filter: a title/snippet must hit >=1 INCLUDE term,
# and must NOT hit an EXCLUDE term in its TITLE.
TECH_TERMS = [
    "data", "python", "sql", "analyt", "analyst", "business intelligence", " bi ",
    "machine learning", " ml ", "mlops", " ai ", "artificial intelligence", "nlp", "llm",
    "rag", "ollama", "generativ", "generative", "computer vision",
    "it support", "it-support", "servicedesk", "service desk", "software",
    "developer", "udvikler", "programmør", "engineer", "etl", "pipeline",
    "automation", "automatisering", "devops", "backend",
    "infrastructure", "infrastruktur", "platform", "cloud", "kubernetes", "docker", "linux",
    "data scientist", "data engineer", "forecast", "forecasting",
]
# Foot-in-the-door roles. Kept by the LLM ONLY when the employer is a tech company.
# Set BRIDGE_TERMS = [] to disable Track B entirely.
BRIDGE_TERMS = [
    "office assistant", "office coordinator", "office manager", "kontorassistent",
    "workplace", "facilit", "reception", "front desk", "logistic", "logistik",
    "koordinator", "coordinator", "operations", "administrativ", "support",
]
INCLUDE_TERMS = TECH_TERMS + BRIDGE_TERMS

# Title-only exclusions.
EXCLUDE_TERMS = [
    "hr ", "human resources", "recruit", "rekrutter",
    "marketing", "markedsføring",
]

# --- source: The Hub (thehub.io) -----------------------------------------------------
# A second scrape source alongside Jobindex. The Hub is the Nordic startup/scaleup board:
# English-first, tech-company-heavy -- exactly the segment Jobindex under-covers. It's a
# single-page app backed by a JSON search API, so core.scrape_thehub() hits that API directly
# (no Playwright) for fast discovery.
#
# VERIFIED from a real response (2026-06-25 curl against the API):
#     curl -s 'https://thehub.io/api/jobs?search=data&countryCode=DK&sorting=mostPopular&page=1' \
#          -H 'Accept: application/json' | head -c 300
#   - Path /api/jobs is correct. Query params: search, countryCode, sorting, page (1-INDEXED).
#   - The job list is a TOP-LEVEL "docs" array: {"docs":[{...}, ...]} (NOT the {"jobs":{"docs"}}
#     that was originally guessed). _thehub_extract_list handles it via its generic "docs" branch.
#   - Each job has `id` + `key` but NO `url`; the page URL is built as /jobs/{id} (the id form,
#     which matches the URLs already in your tracker). `company` and `location` are objects;
#     _thehub_teaser reads company.name and location.locality/address.
#   - The list response DOES include a "description" body. When it's substantial (>200 chars)
#     the role arrives with source="full" and SKIPS the Stage 3a fetch entirely (faster, and it
#     dodges SPA fetch flakiness). Roles without a usable body fall back to the normal fetch.
#   - No post-date in the list, which is fine for a live board (a listed role is an open role;
#     downstream freshness uses scraped_date).
#
# If the endpoint ever changes (empty list / HTML / 404), re-confirm from the browser: open
# https://thehub.io/jobs -> devtools (F12) -> Network -> Fetch/XHR, run a search, find the JSON
# request, and update THEHUB_API_URL / the params below (and the field names in _thehub_teaser).
THEHUB_ENABLED         = True        # VERIFIED 2026-06-25: the curl above returns JSON
THEHUB_API_URL         = "https://thehub.io/api/jobs"   # CONFIRMED working path
THEHUB_SEARCH_PARAM    = "search"    # CONFIRMED from the site's own URLs
THEHUB_PAGE_PARAM      = "page"      # CONFIRMED
THEHUB_PAGE_ZERO_INDEXED = False     # CONFIRMED: first page is page 1, not 0
THEHUB_QUERY_PARAMS    = {           # fixed params sent on every request
    "countryCode": "DK",            # Denmark; use "REMOTE" for remote-only, or drop for all
    "sorting": "mostPopular",
}
THEHUB_MAX_PAGES       = 5           # raised from 3 -> 5 for recall (speed is fine)

# Search terms for The Hub. It's English-first and tech-heavy, so the English/technical terms
# carry the load; the LLM still keeps only genuinely relevant roles downstream.
THEHUB_QUERIES = [
    "data",
    "machine learning",
    "AI",
    "LLM",
    "data engineer",
    "software",
    "python",
    "devops",
    "infrastructure",
    "cloud",
    "automation",
    "IT support",
    "student",
    "office",
    "operations",
]

# --- load the active profile (owner AND non-owner alike) -----------------------------
# Per-person settings live in profiles/<name>.toml -- never in this file. The engine knobs and
# term lists above are shared defaults a profile may selectively override. If no profile exists
# for the active name, _load_profile() exits with instructions to create one from the template.
_prof = _load_profile(ACTIVE_PROFILE)
CANDIDATE_NAME    = (_prof.get("name") or "the candidate").strip()
CANDIDATE_PROFILE = (_prof.get("candidate_profile") or "").strip()
LOCATION_ANCHOR   = (_prof.get("location_anchor") or "").strip()
if not CANDIDATE_PROFILE or not LOCATION_ANCHOR:
    sys.exit(f"Profile '{ACTIVE_PROFILE}' must set both candidate_profile and location_anchor "
             f"(see profiles/_template.toml).")
if _prof.get("queries"):
    TARGET_QUERIES = [str(q) for q in _prof["queries"]]
if _prof.get("excluded_companies"):
    EXCLUDED_COMPANIES = [str(x).lower() for x in _prof["excluded_companies"]]
if "require_commutable" in _prof:
    REQUIRE_COMMUTABLE = bool(_prof["require_commutable"])
# danish_ok = true: this person is comfortable in Danish, so DON'T hide Danish-required roles
# from their shortlist. Maps to the EXCLUDE_DANISH_REQUIRED view filter.
if "danish_ok" in _prof:
    EXCLUDE_DANISH_REQUIRED = not bool(_prof["danish_ok"])

# --- paths (owner -> top-level dirs; anyone else -> isolated sandbox) -----------------
if IS_OWNER:
    BASE_DIR         = os.path.join(SCRIPT_DIR, "job_market_data")    # the search dataset
    APPLICATIONS_DIR = os.path.join(SCRIPT_DIR, "applications")       # what you act on
else:
    BASE_DIR         = os.path.join(SCRIPT_DIR, "job_market_data", "_profiles", ACTIVE_PROFILE)
    APPLICATIONS_DIR = os.path.join(SCRIPT_DIR, "applications", "_profiles", ACTIVE_PROFILE)

MASTER_ARCHIVE  = os.path.join(BASE_DIR, "job_market_data.csv")      # every scored role (the DB)
MARKDOWN_REPORT = os.path.join(BASE_DIR, "Weekly_Job_Matches.md")
RUNS_LOG        = os.path.join(BASE_DIR, "runs.csv")                 # one row per run: timing + funnel
DEBUG_HTML_PATH = os.path.join(BASE_DIR, "_debug_first_page.html")
TRACKER_CSV     = os.path.join(APPLICATIONS_DIR, "applications.csv") # the application tracker
