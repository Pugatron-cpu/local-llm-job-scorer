"""
config.py — all tunable settings for the job-search pipeline, in one place.

Imported by core.py (the engine), a_scrape.py, and c_prepare.py, so a value
changed here applies everywhere.

PROFILES: the tool runs for ONE owner by default (you), persisting to the normal data dirs
exactly as before. `--profile <name>` runs sandboxed for someone else (their own isolated
data under _profiles/<name>/), loading profiles/<name>.toml. See README.md.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# --- profiles ------------------------------------------------------------------------
# EVERY run loads its personal data (candidate profile, location rule, name) from
# profiles/<name>.toml — including the owner's. No personal facts live in tracked code.
# The owner is whoever the JOBSEARCH_OWNER env var names (set it once in ~/.bashrc:
#   export JOBSEARCH_OWNER=<yourname>          # -> loads profiles/<yourname>.toml
# The owner runs against the top-level data dirs and persists; anyone else (via
# `--profile <name>`) runs fully sandboxed under _profiles/<name>/.
OWNER_PROFILE = os.environ.get("JOBSEARCH_OWNER", "").strip().lower()
PROFILES_DIR  = os.path.join(SCRIPT_DIR, "profiles")


def _read_profile_flag() -> str:
    """Peek at `--profile <name>` (or the JOBSEARCH_PROFILE env var) and REMOVE the flag + its
    value from sys.argv, so each tool's own argument parsing (c_prepare's <number>/<url>/--status)
    never sees it. Resolved here, not via argparse, because every tool
    does `from config import *`, so the active profile must be known before any path or query is
    read. Returns the lowercased name, or "" if none was given."""
    name = os.environ.get("JOBSEARCH_PROFILE", "").strip()
    if "--profile" in sys.argv:
        i = sys.argv.index("--profile")
        val = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        del sys.argv[i:i + 2]
        name = val.strip()
    return name.lower()


ACTIVE_PROFILE = _read_profile_flag() or OWNER_PROFILE
if not ACTIVE_PROFILE:
    sys.exit("No profile selected. Either set the owner once:\n"
             "    export JOBSEARCH_OWNER=<yourname>     # loads profiles/<yourname>.toml\n"
             "or run for a specific person:\n"
             "    python a_scrape.py --profile <name>\n"
             "Create a profile by copying profiles/_template.toml to profiles/<name>.toml.")
IS_OWNER = bool(OWNER_PROFILE) and (ACTIVE_PROFILE == OWNER_PROFILE)


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
# Gemma 4 31B (dense, Apache 2.0, 2026-04): picked for THIS task's actual profile —
# judgment/classification with structured output over mixed Danish/English ads. Gemma 4 is
# trained on 140+ languages with balanced European representation and strong instruction
# following; the 31B dense is the workstation flagship and q8_0 (34GB) fits the 48GB pool
# with room for parallel KV slots at NUM_CTX below.
# Alternatives, kept for reference (set MODEL to one of these to try it):
#   "qwen3.6:27b-q8_0"  (30GB) — the previous model; excellent, but its 3.6 gains are
#                        coding-focused, and the match-quality regression coincided with it.
#   "gemma4:31b"        (20GB QAT) — same model, quantization-aware 4-bit: near-q8 quality,
#                        14GB less VRAM -> more parallel headroom. Good speed fallback.
MODEL      = "gemma4:31b-it-q8_0"
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

# Location filter (VIEW filter, like above). The LLM judges commute_ok = reachable within
# ~45 min public transport of Ørestad, Copenhagen (Greater Copenhagen / Capital Region:
# Copenhagen, Frederiksberg, Lyngby, Glostrup, Ballerup, Hellerup, Roskilde, etc.) OR fully
# remote. Sweden / Malmö is EXCLUDED (cross-border) unless remote. Set False to drop the
# filter entirely (e.g. if you can relocate). To include Sweden, edit the prompt in core.py.
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
#   always kept. Roles scored before the danish_level column existed have it BLANK; they are
#   kept but flagged "flags unknown" in the report (run `a_scrape.py --rescore` to fix them).
EXCLUDE_DANISH_REQUIRED = True

# EXCLUDE_DANISH_ADS: a shortlist VIEW filter on the ad's detected WRITING language
#   (ad_language column, set deterministically at scoring time via the language detector).
#   True -> hide ads written mainly in Danish. Unlike DROP_DANISH_LANGUAGE_ADS this loses NO
#   recall: the ads are still fetched, scored and archived, and reappear if set to False.
#   This is the safe way to get a Danish-ad-free shortlist. Overridable per profile
#   (hide_danish_ads in the toml).
EXCLUDE_DANISH_ADS = True

# TRACK_B_MIN_SCORE: Track B (foot-in-the-door) roles are prompted into a 70-90 band, so with
#   SCORE_THRESHOLD=75 nearly any office role at a "tech company" used to make the shortlist.
#   This VIEW filter gives Track B its own, higher bar; Track A keeps SCORE_THRESHOLD.
TRACK_B_MIN_SCORE = 80

DEBUG_DUMP_HTML = False             # True -> dump page 1 HTML so you can fix selectors

# --- candidate profile (per person) --------------------------------------------------
# PERSONAL DATA LIVES IN profiles/<name>.toml (gitignored), for the OWNER too. These are
# placeholders overwritten by the active profile at the bottom of this file. Nothing
# personal is tracked in git.
CANDIDATE_PROFILE = ""
CANDIDATE_NAME    = ""

# Commute rule the scorer applies for commute_ok. Loaded from the active profile's
# location_anchor (see profiles/_template.toml for the expected shape).
LOCATION_ANCHOR = ""

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
    # --- Track A widening (2026-07): more angles on the same target roles ---
    "studentermedhjælper analyse",
    "studentermedhjælper digitalisering",
    "studentermedhjælper AI",
    "student assistant analytics",
    "backend student",
    "IT operations student",
    "system administration student",
    # --- Curated 2026-07 from match data: title patterns that were landing high-fit,
    #     shortlist-eligible roles which no existing query targeted ---
    "AI engineer student",       # your best matches are "AI Engineer" titles; none was covered
    "student worker",            # common English title variant (Student Worker @ Podimo, etc.)
    "studentermedarbejder AI",   # Danish spelling variant of studentermedhjælper (was scoring 95)
    # --- Track B: foot-in-the-door roles (LLM keeps only the ones at tech companies).
    #     Noisier; comment out if a run gets too slow. Trimmed 2026-07: dropped
    #     "workplace coordinator" + "logistics coordinator student" (generic ops/logistics,
    #     rarely a tech employer — pure scoring cost). Re-add if you want wider Track B reach.
    "office assistant",
    "office coordinator",
    "kontorassistent",
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
# VERIFIED from a real response (2026-06-25 curl on the z8):
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
THEHUB_MAX_PAGES       = 8           # raised 3 -> 5 -> 8 for recall (Hub is fast; push to 10
                                     # if you want even deeper coverage per query)

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
    # --- widening (2026-07): safe now that Hub bodies go through the keyword prefilter ---
    "analytics",
    "backend",
    "platform engineer",
    "intern",
    # --- curated 2026-07 from match data: your two strongest role themes as explicit queries
    #     (the broad "AI"/"data" terms rank differently; these surface role-specific results) ---
    "AI engineer",
    "data scientist",
]

# --- source: Jobnet (job.jobnet.dk) — SCAFFOLD, DISABLED (no public API as of 2026-07) -----
# Denmark's public job board covers publicly-funded employers Jobindex/The Hub under-serve.
# STATUS (probed 2026-07-05): NOT usable as an unauthenticated source. /CV/FindWork/Search now
# 301-redirects to identityserver-prod.starplatform.dk/Account/Login — the JSON search sits
# behind a StarPlatform (MitID) login. Individual /CV/FindWork/Details/{id} pages are still
# public, but there is no public way to DISCOVER ids, so keyword search is gone. Do NOT enable
# without a real logged-in-session token strategy (out of scope). Third-party mirrors (Apify /
# Techmap) exist but are paid + external, which breaks the local/sovereign + privacy stance.
# The scaffold below is left intact in case the public API returns; re-verify per the steps at
# job.jobnet.dk before flipping this on.
JOBNET_ENABLED    = False
JOBNET_API_URL    = ""     # e.g. "https://job.jobnet.dk/CV/FindWork/Search" — CONFIRM FIRST
JOBNET_QUERY_PARAM = "SearchString"
JOBNET_OFFSET_PARAM = "Offset"     # Jobnet pages by result offset, not page number
JOBNET_PAGE_SIZE  = 20
JOBNET_MAX_PAGES  = 3
JOBNET_QUERIES    = ["studentermedhjælper it", "studentermedhjælper data",
                     "student assistant data", "it support student"]

# --- source: ATS watchlist (Greenhouse / Lever public career APIs) --------------------
# Poll the PUBLIC job APIs of a hand-picked list of companies you'd actually want to work at.
# No auth, no scraping — these are the same JSON endpoints the companies' own career pages
# call. High precision (YOU choose the employers) and it catches roles that never reach
# Jobindex/The Hub. Roles still flow through the same prefilter + LLM scoring + dedup.
# VERIFIED live 2026-07-05: Greenhouse returns {"jobs":[...]} with the HTML body inline when
# content=true; Lever returns a JSON array.
#
# Find a company's slug from its careers page and TEST it before adding:
#   Greenhouse -> boards.greenhouse.io/<slug> (or job links carry ?gh_jid=)
#                 curl https://boards-api.greenhouse.io/v1/boards/<slug>/jobs   (200 + JSON = good)
#   Lever      -> jobs.lever.co/<slug>
#                 curl 'https://api.lever.co/v0/postings/<slug>?mode=json'
# Entry format: "provider:slug"  or  "provider:slug|Display Name".
#
# OFF by default (2026-07-05): measured on the seed list, only 2 of 55 roles these company
# boards return are student/intern — the rest are full-time professional roles that get scored
# then filtered out by employment_type, i.e. wasted LLM calls for a STUDENT search. Flip to
# True the day you search full-time roles (post-graduation); the code + seeds are ready.
ATS_ENABLED   = False
ATS_COMPANIES = [
    # --- YOUR target-employer list. These three are companies that ALREADY produced high-fit
    #     matches in past scrapes AND run a public Greenhouse board (verified live 2026-07-05:
    #     Trustpilot 61 / Wolt 20 DK / Too Good To Go 18 DK). Edit freely.
    "greenhouse:trustpilot|Trustpilot",
    "greenhouse:wolt|Wolt",
    "greenhouse:toogoodtogo|Too Good To Go",
    # More to uncomment (remote-heavy — more reach, more noise):
    # "greenhouse:remotecom|Remote",
    # "greenhouse:gitlab|GitLab",
    # Lever example (confirm the slug returns a JSON array first):
    # "lever:<slug>|<Company>",
    # NOTE: most Danish employers use Teamtailor / HR-ON, not Greenhouse/Lever, and those have
    # no public per-company API — so this watchlist stays small on purpose. Jobindex + The Hub
    # remain the volume sources; this is a precision supplement for a few named employers.
]
# Keep only roles whose location matches one of these (case-insensitive substring) so a big
# global board can't flood scoring with non-commutable roles. Empty list = keep everything.
ATS_LOCATION_KEEP = ["denmark", "danmark", "københ", "copenhagen", "kbh",
                     "aarhus", "odense", "aalborg", "remote"]

# --- load the active profile (EVERY run, owner included) -----------------------------
# All personal settings come from profiles/<name>.toml — the owner's too, so no personal
# data lives in tracked code. Engine knobs and term lists stay shared unless the profile
# overrides one of the per-person settings below.
_prof = _load_profile(ACTIVE_PROFILE)
CANDIDATE_PROFILE = (_prof.get("candidate_profile") or "").strip()
LOCATION_ANCHOR   = (_prof.get("location_anchor") or "").strip()
CANDIDATE_NAME    = (_prof.get("name") or "").strip()
if not CANDIDATE_PROFILE or not LOCATION_ANCHOR:
    sys.exit(f"Profile '{ACTIVE_PROFILE}' must set both candidate_profile and location_anchor "
             f"(see profiles/_template.toml).")
if _prof.get("queries"):
    TARGET_QUERIES = [str(q) for q in _prof["queries"]]
if _prof.get("thehub_queries"):
    THEHUB_QUERIES = [str(q) for q in _prof["thehub_queries"]]
if "ats_companies" in _prof:            # per-person target-employer watchlist (may be [])
    ATS_COMPANIES = [str(x) for x in _prof["ats_companies"]]
if _prof.get("excluded_companies"):
    EXCLUDED_COMPANIES = [str(x).lower() for x in _prof["excluded_companies"]]
if "require_commutable" in _prof:
    REQUIRE_COMMUTABLE = bool(_prof["require_commutable"])
# danish_ok = true: this person is comfortable in Danish, so DON'T hide Danish-required
# roles from their shortlist. Maps to the EXCLUDE_DANISH_REQUIRED view filter.
if "danish_ok" in _prof:
    EXCLUDE_DANISH_REQUIRED = not bool(_prof["danish_ok"])
# hide_danish_ads = true: hide ads whose MAIN LANGUAGE is Danish from the shortlist.
# View filter on the ad_language column: Danish ads are still scored + archived, and
# reappear the moment this is set back to false. Maps to EXCLUDE_DANISH_ADS.
if "hide_danish_ads" in _prof:
    EXCLUDE_DANISH_ADS = bool(_prof["hide_danish_ads"])

# --- Application Brief handoff (c_prepare) — per person -------------------------------
# The brief c_prepare writes ends with a HANDOFF paragraph telling a downstream Claude how to
# draft the CV + letter. That instruction is personal (which master-profile file is the source
# of truth, which letter formula, how to lead each lane), so it lives in the profile — NOT
# hardcoded in c_prepare, where it previously named one specific owner and would have told a
# --profile run to write the WRONG person's application. All optional; neutral fallbacks below.
BRIEF_NAME           = CANDIDATE_NAME or "the candidate"
BRIEF_MASTER_REF     = (_prof.get("brief_master_ref") or "").strip()      # e.g. "master_profile.md"
BRIEF_LETTER_FORMULA = (_prof.get("brief_letter_formula") or "a clear, specific motivation-letter structure").strip()
BRIEF_CV_FORMAT      = (_prof.get("brief_cv_format") or "tailored CV sections").strip()
BRIEF_LEAD_A         = (_prof.get("brief_lead_a") or "").strip()          # how to open a Track-A (technical) application
BRIEF_LEAD_B         = (_prof.get("brief_lead_b") or "").strip()          # how to open a Track-B (foot-in-the-door) one

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

# APPLICATIONS_DIR is the live queue: only roles still worth acting on. Settled briefs are MOVED
# (never deleted) into the _archive/ subfolder, and tracker backups into _backups/. Those two
# paths are resolved from APPLICATIONS_DIR at call time (c_prepare._archive_dir/_backup_dir), NOT
# stored here: a derived copy would go stale the moment APPLICATIONS_DIR is repointed at another
# profile, and a stale archive path means files get MOVED into the wrong folder.
TRACKER_BACKUPS_KEEP = 3   # auto .bak-<timestamp> copies to retain; 0 = keep every one
BRIEF_QUEUE_STATUSES = ["interested"]   # tracker statuses that keep a brief in the live queue
