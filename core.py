"""
Jobindex.dk + The Hub -> local-LLM relevance pipeline (multi-source).

Three-stage funnel:
  1. Scrape teasers (cheap)           -> dedup + date filter + company exclusion
  2. Python keyword pre-filter (free) -> keep tech-relevant, drop HR/marketing
  3. Fetch FULL description + score    -> only for survivors, via Ollama structured output

MULTI-SOURCE (this version):
  - Teasers now come from a small SOURCE SEAM (iter_sources), not a single hard-coded
    scraper. Each source yields the SAME teaser dict shape:
        {title, company, location, published_date, snippet, url, source_site}
    plus, OPTIONALLY, "_description" (+ "source": "full") when the source already has the
    full ad body (e.g. The Hub's JSON API). Teasers that arrive with a body SKIP the fetch
    stage entirely. Every source is isolated in iter_sources: if one raises, it is logged
    and skipped, so a flaky/unverified source can never take down the proven Jobindex path.
  - Sources today: Jobindex (Playwright), The Hub (HTTP JSON API; off until verified, see
    config.THEHUB_*). Adding a third (e.g. ATS watchlist) = one more generator in iter_sources.

  - URL CANONICALISATION (canonical_url) is now the matching key everywhere a URL identifies
    a role: the archive "seen" set, the shortlist de-dup, and (in c_prepare) the tracker and
    --status matching. It strips tracking params (utm_*, source, Codes, ...) and normalises
    host/slash, so the SAME role counts as seen no matter which source or aggregator produced
    the link (Jobindex's "thehub.io/...?utm_source=jobindex" == The Hub's clean canonical URL).
    Functional query params (e.g. hr-manager's ProjectId) are KEPT, so distinct roles stay distinct.
  - CROSS-SOURCE DE-DUP: beyond the URL key, a normalised company+title key drops the same
    role surfaced by two different sources under two different URLs (e.g. a company's own ATS
    link via Jobindex vs the same role on The Hub).

Earlier change (Danish gate): the language filter runs on WHATEVER text gets scored
(snippet or full description), so it works regardless of FETCH_FULL_DESC.

VERIFY BEFORE RELYING ON IT: the CSS selectors in scrape_teasers() must match Jobindex's
CURRENT markup. The Hub endpoint + JSON field names must be confirmed once from your browser
(see config.THEHUB_* and _thehub_teaser); it ships disabled so it can't feed unverified data
into your archive.
"""

import os
import csv
import re
import json
import time
import random
import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

from config import *   # settings: paths, MODEL, thresholds, ACCEPTED_*, REQUIRE_COMMUTABLE, THEHUB_*, ...

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL CANONICALISATION + ROLE KEYS (shared matching keys across sources/tools)
# ---------------------------------------------------------------------------

# Query-string keys that are pure tracking/attribution and never change which role a URL
# points to. Stripped before a URL is used as an identity key. Everything else is kept,
# so functional params (e.g. hr-manager.net's ProjectId / cid) still distinguish roles.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "source", "trid", "rx_campaign", "codes", "gh_src", "ref", "referrer",
    "mc_cid", "mc_eid", "fbclid", "gclid",
}


def canonical_url(u: str) -> str:
    """Normalise a job URL into a stable identity key: lowercase host, drop a leading 'www.',
    drop tracking query params (utm_*, source, Codes, ...), keep functional ones (sorted for
    stability), strip the fragment and any trailing slash. The SAME role then maps to the same
    key regardless of which source/aggregator produced the link. Used as the de-dup / lookup
    key in the archive, the shortlist, the tracker, and --status matching. NOTE: this never
    rewrites stored data; it is only applied at comparison time, so existing CSVs stay intact."""
    if not u:
        return ""
    u = u.strip()
    try:
        s = urlsplit(u)
    except ValueError:
        return u
    if not s.scheme and not s.netloc:   # not a real URL (e.g. "N/A") -> leave as-is
        return u
    host = (s.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    kept = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS]
    query = urlencode(sorted(kept))
    path = s.path.rstrip("/") or "/"
    return urlunsplit((s.scheme or "https", host, path, query, ""))


def role_key(job: dict) -> str:
    """A cross-source identity for the SAME role under different URLs: normalised
    company + title (lowercased, alphanumerics only). Empty if either is missing, in which
    case the caller falls back to the URL key alone. Deliberately simple: it collapses exact
    company+title matches across sources; it won't catch minor wording differences (e.g.
    'Monta' vs 'Monta ApS'), which is acceptable for a de-dup safety net."""
    c = re.sub(r"[^a-z0-9]", "", (job.get("company") or "").lower())
    t = re.sub(r"[^a-z0-9]", "", (job.get("title") or "").lower())
    return f"{c}|{t}" if c and t else ""


def _html_to_text(html: str) -> str:
    """Flatten an HTML ad body (The Hub returns HTML) to readable text, capped to fit NUM_CTX."""
    if not html:
        return ""
    try:
        txt = BeautifulSoup(html, "html.parser").get_text("\n")
    except Exception:
        txt = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()[:6000]

# ---------------------------------------------------------------------------
# STAGE 1: SCRAPE TEASERS — JOBINDEX (Playwright)
# ---------------------------------------------------------------------------

def scrape_teasers(page, keyword: str, cutoff_date):
    """Yield teaser dicts for one keyword. Reuses an already-open Playwright page."""
    base_url = "https://www.jobindex.dk/jobsoegning?q={}&page={}"

    for page_num in range(1, MAX_PAGES + 1):
        url = base_url.format(keyword.replace(" ", "+"), page_num)
        log.info(f"  page {page_num}: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded")
            # Wait for the results region. Adjust if Jobindex renamed the app root.
            page.wait_for_selector("#jobsearch-app, .jobsearch-result", timeout=8000)
            time.sleep(1)
            html = page.content()
        except Exception as e:
            log.error(f"  failed to load page {page_num}: {e}")
            break

        if DEBUG_DUMP_HTML and page_num == 1:
            with open(DEBUG_HTML_PATH, "w", encoding="utf-8") as f:
                f.write(html)
            log.info(f"  dumped HTML -> {DEBUG_HTML_PATH} (inspect to fix selectors)")

        soup = BeautifulSoup(html, "html.parser")
        containers = soup.select("div.jobsearch-result")  # one wrapper per job
        if not containers:
            log.info(f"  no containers on page {page_num} (end of results or stale selectors)")
            break

        for c in containers:
            teaser = _parse_teaser(c, cutoff_date)
            if teaser:
                yield teaser

        time.sleep(random.uniform(2.0, 4.0))  # be polite


def _parse_teaser(container, cutoff_date):
    """Extract one teaser. Returns None if it should be skipped (old / excluded / unparseable)."""
    # --- title + url: the headline anchor (h4 a) holds the title text and the real
    # job link (jobindex.dk/jobannonce/... or the employer's ATS). The first <a> in
    # the card is the company-logo link to the company homepage -- do NOT use it.
    head_link = container.select_one("h4 a, h3 a")
    title = head_link.get_text(strip=True) if head_link else ""
    url = head_link.get("href", "") if head_link else ""
    if url.startswith("/"):
        url = "https://www.jobindex.dk" + url

    # --- company: confirmed at .jix-toolbar-top__company ---
    comp_elem = container.select_one(".jix-toolbar-top__company")
    company = comp_elem.get_text(" ", strip=True) if comp_elem else ""

    if company and any(x in company.lower() for x in EXCLUDED_COMPANIES):
        return None

    # --- date ---
    published = ""
    t = container.find("time")
    if t and t.has_attr("datetime"):
        try:
            d = datetime.strptime(t["datetime"][:10], "%Y-%m-%d").date()
            if d < cutoff_date:
                return None
            published = str(d)
        except ValueError:
            published = t.get_text(strip=True)
    elif t:
        published = t.get_text(strip=True)

    # --- snippet: the card's <p> text is the real ad preview (now populated, not N/A) ---
    ps = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    ps = [p for p in ps if p]
    snippet = " ".join(ps)[:500]

    if not title or not url or url == "N/A":
        log.debug(f"  skipping unparseable container (title={title!r} url={url!r})")
        return None

    return {
        "title": title,
        "company": company or "N/A",
        "location": "N/A",
        "published_date": published or "N/A",
        "snippet": snippet,
        "url": url,
        "source_site": "jobindex",
    }


def _jobindex_teasers(cutoff_date):
    """Source adapter: drive the Playwright scrape over TARGET_QUERIES and yield teasers.
    Owns its own browser (sync Playwright is thread-affine), closing it when done."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            for q in TARGET_QUERIES:
                log.info(f"--- [jobindex] query: {q!r} ---")
                yield from scrape_teasers(page, q, cutoff_date)
        finally:
            browser.close()

# ---------------------------------------------------------------------------
# STAGE 1: SCRAPE TEASERS — THE HUB (thehub.io, HTTP JSON API)
# ---------------------------------------------------------------------------

def _thehub_extract_list(data):
    """Pull the list of job objects out of The Hub's JSON. CONFIRMED shape (2026-06-25 curl):
        {"docs": [...], ...}
    i.e. a TOP-LEVEL "docs" array (the earlier {"jobs":{"docs":...}} guess was wrong). The
    "jobs"/"featuredJobs" wrapper branch below finds nothing and we fall through to the generic
    "docs" branch, which returns it. The wrapper branch is kept in case The Hub reintroduces it
    or returns featured roles separately. Returns [] if no recognised shape."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    out = []
    for container_key in ("jobs", "featuredJobs"):
        c = data.get(container_key)
        if isinstance(c, dict) and isinstance(c.get("docs"), list):
            out.extend(c["docs"])
    if out:
        return out
    # generic fallbacks (endpoint changed / different shape)
    for key in ("docs", "hits", "results", "items"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def _thehub_teaser(d: dict, cutoff_date):
    """Map one Hub job object -> a teaser dict (with the full body attached so it skips the
    fetch stage). Field names confirmed against a real 2026-06-25 response (title, company.name,
    id, description, location.{locality,address}); the extra pick() fallbacks are kept as
    defensive alternates in case the schema shifts."""
    if not isinstance(d, dict):
        return None

    def pick(*keys):
        for k in keys:
            v = d.get(k)
            if v not in (None, "", [], {}):
                return v
        return ""

    title = pick("title", "headline", "name", "jobTitle")
    if not title:
        return None

    company = pick("companyName", "company", "employer", "organisation", "organization")
    if isinstance(company, dict):
        company = company.get("name") or company.get("title") or company.get("companyName") or ""

    if company and any(x in str(company).lower() for x in EXCLUDED_COMPANIES):
        return None

    job_id = pick("id", "_id", "objectID", "uuid")   # NOT "key": the canonical job URL uses
                                                     # the id, which matches existing tracker rows
    url = pick("url", "applicationUrl", "jobUrl", "link", "permalink")
    if isinstance(url, dict):
        url = url.get("href") or ""
    if not url and job_id:
        url = f"https://thehub.io/jobs/{job_id}"
    if not url:
        return None

    # CONFIRMED (2026-06-25 run): The Hub's LIST response DOES carry a usable "description"
    # body. When it's substantial (>200 chars, handled below) the role is marked source="full"
    # and SKIPS the fetch stage. If a given object happens to lack a body, this is "" and the
    # role falls through to the normal fetch path like any other source.
    raw_desc = pick("description", "descriptionHtml", "jobDescription", "body", "content", "text")
    desc = _html_to_text(raw_desc) if raw_desc else ""

    loc = pick("location", "city", "workplace", "region")
    if isinstance(loc, dict):                         # The Hub: {"country","locality","address"}
        loc = (loc.get("locality") or loc.get("city") or loc.get("address")
               or loc.get("name") or loc.get("country") or "")
    if isinstance(loc, list):
        loc = ", ".join(str(x) for x in loc if x)

    pub = pick("publishedAt", "published", "createdAt", "datePosted", "created", "postedAt")
    published = "N/A"
    if pub:
        d_parsed = _parse_date(str(pub))
        if d_parsed is not None:
            if d_parsed < cutoff_date:
                return None          # too old
            published = str(d_parsed)

    snippet = pick("excerpt", "teaser", "summary", "shortDescription")
    if not snippet:
        snippet = desc[:500]

    teaser = {
        "title": str(title).strip(),
        "company": (str(company).strip() or "N/A"),
        "location": (str(loc).strip() or "N/A"),
        "published_date": published,
        "snippet": str(snippet)[:500],
        "url": str(url).strip(),
        "source_site": "thehub",
    }
    # Full body available -> attach it so the role SKIPS the fetch stage. Only when it's
    # substantial; a too-short body falls through to the normal snippet path.
    if desc and len(desc) > 200:
        teaser["_description"] = desc
        teaser["source"] = "full"
    return teaser


def scrape_thehub(cutoff_date):
    """Source adapter: The Hub (thehub.io). Hits the site's JSON search backend (NOT Playwright)
    and yields teaser dicts (title, company, location, id->URL). The list response carries a
    "description" body, so most roles arrive with source="full" and SKIP the Stage 3a fetch
    (confirmed 2026-06-25). The list has no post-date, which is fine for a live board (a listed
    role is an open role; downstream freshness uses scraped_date).

    ENDPOINT VERIFIED (2026-06-25): GET https://thehub.io/api/jobs?search=&countryCode=DK&
    sorting=mostPopular&page=N returns {"docs":[{"id","title","company":{...},"description",...}]}.
    If it ever changes (empty list / HTML / 404), re-confirm from the browser: open
    https://thehub.io/jobs, devtools (F12) -> Network -> Fetch/XHR, run a search, find the JSON
    request, and update THEHUB_API_URL / the params in config.py and the field names in
    _thehub_teaser(). If THEHUB_API_URL is empty this yields nothing (logs a reminder)."""
    if not THEHUB_API_URL:
        log.warning("  [thehub] THEHUB_API_URL is empty -> skipping. Set it (and confirm the "
                    "JSON fields) per the steps in config.py / scrape_thehub().")
        return

    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "application/json",
        "Referer": "https://thehub.io/jobs",
    }
    seen_urls = set()
    page_start = 0 if THEHUB_PAGE_ZERO_INDEXED else 1
    for term in (THEHUB_QUERIES or [""]):
        log.info(f"--- [thehub] query: {term!r} ---")
        for page_num in range(page_start, page_start + THEHUB_MAX_PAGES):
            params = dict(THEHUB_QUERY_PARAMS)
            if term and THEHUB_SEARCH_PARAM:
                params[THEHUB_SEARCH_PARAM] = term
            if THEHUB_PAGE_PARAM:
                params[THEHUB_PAGE_PARAM] = page_num
            try:
                r = requests.get(THEHUB_API_URL, params=params, headers=headers,
                                 timeout=TIMEOUT_S)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                log.error(f"  [thehub] request failed (term={term!r} page={page_num}): "
                          f"{str(e)[:160]}")
                break
            docs = _thehub_extract_list(data)
            if not docs:
                if page_num == page_start:
                    log.warning(f"  [thehub] no job list found in response for term={term!r}. "
                                f"Check THEHUB_API_URL / _thehub_extract_list keys.")
                break
            new_on_page = 0
            for d in docs:
                t = _thehub_teaser(d, cutoff_date)
                if not t:
                    continue
                cu = canonical_url(t["url"])
                if cu in seen_urls:
                    continue
                seen_urls.add(cu)
                new_on_page += 1
                yield t
            if new_on_page == 0:            # nothing new -> assume end of results
                break
            time.sleep(random.uniform(0.5, 1.2))   # be polite

# ---------------------------------------------------------------------------
# SOURCE SEAM
# ---------------------------------------------------------------------------

def iter_sources(cutoff_date):
    """Yield teaser dicts from every enabled source, one source at a time. Each source is
    isolated in its own try/except: a source that raises is logged and skipped, so a flaky or
    not-yet-verified source can never take down the run. Add a new source by appending another
    guarded generator here (e.g. an ATS-watchlist source for stage 2)."""
    sources = [("jobindex", _jobindex_teasers)]
    if THEHUB_ENABLED:
        sources.append(("thehub", scrape_thehub))

    for name, fn in sources:
        try:
            yield from fn(cutoff_date)
        except Exception as e:
            log.error(f"source {name!r} failed and was skipped: {str(e)[:200]}")

# ---------------------------------------------------------------------------
# STAGE 2: FREE KEYWORD PRE-FILTER
# ---------------------------------------------------------------------------

def passes_prefilter(job: dict) -> bool:
    text = f" {job['title']} {job['snippet']} ".lower()
    title = f" {job['title']} ".lower()
    if not any(term in text for term in INCLUDE_TERMS):
        return False
    if any(term in title for term in EXCLUDE_TERMS):  # exclude on title only
        return False
    return True

# ---------------------------------------------------------------------------
# STAGE 3a: FETCH FULL DESCRIPTION + LANGUAGE + DEADLINE
# ---------------------------------------------------------------------------

def fetch_description(page, url: str):
    """Open the job page; return (text, error). error is None on success."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        time.sleep(1)
        body = re.sub(r"\n{3,}", "\n\n", page.inner_text("body")).strip()
        if len(body) < 200:
            return "", "body too short (redirect/interstitial?)"
        return body[:6000], None  # cap to keep the prompt within NUM_CTX
    except Exception as e:
        return "", str(e)[:140]


def _fetch_worker(task_q: "queue.Queue"):
    """One fetch worker = one OWN Playwright browser (sync Playwright is thread-affine, so
    workers can't share a browser/page). Drains the shared queue; attaches the result to
    each job as _fetched / _fetch_err. Each job is handled by exactly one worker, so the
    per-job writes never race. Politeness sleep is kept per worker."""
    pw = browser = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        while True:
            try:
                job = task_q.get_nowait()
            except queue.Empty:
                break
            try:
                job["_fetched"], job["_fetch_err"] = fetch_description(page, job["url"])
            except Exception as e:                      # fetch_description already guards,
                job["_fetched"], job["_fetch_err"] = "", str(e)[:140]   # belt + suspenders
            time.sleep(random.uniform(0.6, 1.4))        # be polite -> avoid rate limiting
    except Exception as e:
        log.error(f"  fetch worker failed: {e}")
    finally:
        try:
            if browser:
                browser.close()
            if pw:
                pw.stop()
        except Exception:
            pass


def fetch_all(jobs: list):
    """Fetch every job's full page concurrently across FETCH_WORKERS browsers. Results are
    attached to each job (_fetched / _fetch_err) in place; nothing is returned."""
    if not jobs:
        return
    task_q = queue.Queue()
    for j in jobs:
        task_q.put(j)
    n = max(1, min(FETCH_WORKERS, len(jobs)))
    threads = [threading.Thread(target=_fetch_worker, args=(task_q,), daemon=True)
               for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def fetch_one(url: str):
    """Fetch a single job page live (one browser via fetch_all). Returns (text, error);
    error is None on success. Used by c_prepare to re-fetch the chosen ad fresh."""
    job = {"url": url}
    fetch_all([job])
    return job.get("_fetched", ""), job.get("_fetch_err", "no result")


_DANISH_MARKERS = (" og ", " er ", " til ", " som ", " med ", " af ", " vi ",
                   " du ", "erfaring", "medarbejder", "ansøg", "arbejde")
_ENGLISH_MARKERS = (" the ", " and ", " you ", " we ", " with ", " for ",
                    "experience", "responsibilities", "requirements", "apply")

def looks_danish_only(text: str) -> bool:
    """True if the ad's main language is Danish. Prefers a real language detector
    (lingua, then langdetect); falls back to the stopword heuristic. Only returns
    True on a confident Danish call, so English ads are never dropped by accident."""
    t = text.strip()
    if len(t) < 40:
        return False  # too short to judge confidently

    lang = _detect_lang(t[:2000])
    if lang == "da":
        return True
    if lang in ("en", "no", "sv"):  # detector is confident it's NOT Danish
        return False

    # Fallback heuristic (detector unavailable / unsure)
    s = f" {t.lower()} "
    da = sum(s.count(m) for m in _DANISH_MARKERS) + s.count("æ") + s.count("ø") + s.count("å")
    en = sum(s.count(m) for m in _ENGLISH_MARKERS)
    return da > 0 and en < max(3, da * 0.3)


def confidently_danish(text: str, min_len: int = 60) -> bool:
    """STRICT pre-fetch gate (used on the teaser, before we have the full body).
    Returns True ONLY when the language detector is confident the text is Danish.
    Deliberately does NOT use the stopword heuristic and refuses to judge on too little
    text -- so it never drops a Danish-TITLED / English-BODY role on a guess. When unsure
    it returns False, and the caller falls through to fetch the real body."""
    t = text.strip()
    if len(t) < min_len:
        return False
    return _detect_lang(t[:2000]) == "da"


_LINGUA = None
def _detect_lang(text: str):
    """Return 'da'/'en'/'no'/'sv'/None. Lazily loads lingua, then langdetect."""
    global _LINGUA
    try:
        if _LINGUA is None:
            from lingua import Language, LanguageDetectorBuilder
            names = ["ENGLISH", "DANISH", "SWEDISH", "NORWEGIAN_BOKMAL", "NYNORSK"]
            langs = [getattr(Language, n) for n in names if hasattr(Language, n)]
            _LINGUA = LanguageDetectorBuilder.from_languages(*langs).build()
        res = _LINGUA.detect_language_of(text)
        return {"DANISH": "da", "ENGLISH": "en", "NORWEGIAN_BOKMAL": "no",
                "NYNORSK": "no", "SWEDISH": "sv"}.get(res.name) if res else None
    except ImportError:
        pass
    try:
        from langdetect import detect
        code = detect(text)
        return code if code in ("da", "en", "no", "sv") else None
    except Exception:
        return None


_DEADLINE_RE = re.compile(
    r"(?:ans[øo]gningsfrist|frist|deadline)[:\s]*"
    r"(\d{1,2})[.\s/-]\s*(\d{1,2}|\w+)[.\s/-]\s*(\d{2,4})",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    ["januar", "februar", "marts", "april", "maj", "juni", "juli", "august",
     "september", "oktober", "november", "december"], start=1)}

def deadline_passed(text: str) -> bool:
    """Best-effort: returns True only if we confidently parse a past deadline."""
    m = _DEADLINE_RE.search(text)
    if not m:
        return False
    day, mon, year = m.groups()
    try:
        day = int(day)
        month = int(mon) if mon.isdigit() else _MONTHS.get(mon.lower())
        if not month:
            return False
        year = int(year)
        if year < 100:
            year += 2000
        return datetime(year, month, day).date() < datetime.now().date()
    except (ValueError, TypeError):
        return False

# ---------------------------------------------------------------------------
# STAGE 3b: LLM SCORING (structured output)
# ---------------------------------------------------------------------------

# CANDIDATE_PROFILE and LOCATION_ANCHOR now live in config.py (per-person settings) and
# arrive here via `from config import *`. A non-owner profile overrides them from
# profiles/<name>.toml.

# Schema-constrained output. Ollama is given this as `format`, so the model is forced to
# return conformant JSON. Keep the prompt example in sync with this schema.
SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "track": {"type": "string", "enum": ["A", "B", "none"]},
        "is_tech_company": {"type": "boolean"},
        "employment_type": {"type": "string",
                            "enum": ["student", "part_time", "full_time",
                                     "internship", "unknown"]},
        "work_mode": {"type": "string",
                      "enum": ["onsite", "hybrid", "remote", "unknown"]},
        "location": {"type": "string"},
        "commute_ok": {"type": "boolean"},
        "danish_level": {"type": "string",
                         "enum": ["none", "preferred", "required"]},
        "deadline": {"type": "string"},
        "reasoning": {"type": "string"},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "track", "is_tech_company", "employment_type",
                 "work_mode", "commute_ok", "danish_level", "reasoning"],
}

def score_job(job: dict, description: str) -> dict:
    prompt = f"""You are screening jobs for a candidate. Score the fit 0-100.
There are TWO acceptable kinds of role.

TRACK A — technical / data role (preferred):
  data analyst, BI, data/AI/ML engineering, IT/service-desk support, software,
  automation, etc. Score by overlap with the candidate's skills and projects.
    85-100: technical role closely matching the skills/projects.
    60-84 : technical but only partial overlap, or borderline seniority.

TRACK B — foot-in-the-door role AT a tech company:
  office assistant, reception, front desk, workplace/facilities, logistics,
  operations, coordinator, administration, support. The candidate wants to enter a
  tech company via 7 years of combined corporate-operations and hospitality experience,
  then move laterally.
    Score 70-90 ONLY IF the EMPLOYER is clearly a software / IT / AI / data / tech company.
    If the employer is NOT a tech company, score these <= 35.

Any HR, marketing, or sales role scores 0.

EMPLOYMENT TYPE (Danish market — classify factually; this does NOT affect the score,
a downstream filter handles the candidate's current availability):
  - "student"    : studenterjob / studentermedhjælper / student assistant.
  - "part_time"  : deltid — a non-student part-time role.
  - "full_time"  : fuldtid.
  - "internship" : praktik / internship.
  - "unknown"    : hours/type not stated.

ALSO extract:
  - "work_mode"       : "onsite" | "hybrid" | "remote" | "unknown".
  - "location"        : the role's city/area as stated (e.g. "Copenhagen", "Aarhus",
                        "Lyngby", "remote"), else "".
  - "commute_ok"      : true if the role is {LOCATION_ANCHOR}. This does NOT affect the score.
  - "danish_level"     : how much Danish the ROLE requires (judge the requirement, not the
                        ad's writing language; an English-written ad can still require Danish):
                          "required"  : the role needs working/fluent Danish (e.g. "Danish is
                                        required", "must speak Danish", Danish-facing support).
                          "preferred" : Danish is a plus / nice-to-have / an advantage, but not
                                        mandatory; English is enough to do the job.
                          "none"      : no Danish needed (English-only is fine, or not mentioned).
                        Does NOT affect the score; it is a flag for the candidate.
  - "deadline"        : application deadline as "YYYY-MM-DD" if clearly stated, else "".

Set "track" to "A", "B", or "none", and "is_tech_company" to whether the employer is a
software/IT/AI/data/tech company.

Candidate profile:
{CANDIDATE_PROFILE}

Job title: {job['title']}
Company: {job['company']}
Description:
{description[:5000]}

Respond with ONLY a JSON object, no markdown fences, no other text, exactly like:
{{"score": 0-100, "track": "A"|"B"|"none", "is_tech_company": true|false, "employment_type": "student"|"part_time"|"full_time"|"internship"|"unknown", "work_mode": "onsite"|"hybrid"|"remote"|"unknown", "location": "city"|"", "commute_ok": true|false, "danish_level": "none"|"preferred"|"required", "deadline": "YYYY-MM-DD"|"", "reasoning": "one sentence", "matched_skills": ["skill", "skill"]}}"""

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "format": SCORE_SCHEMA,  # schema-constrained output (was plain "json")
        "think": False,          # qwen3.6 is a reasoning model: keep output in `response`
        "options": {"temperature": 0.1, "num_ctx": NUM_CTX, "num_predict": 400},
    }
    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
        # Reasoning models may still route the JSON into `thinking`; accept either.
        raw = (data.get("response") or data.get("thinking") or "").strip()
    except Exception as e:
        log.error(f"  ollama call failed for {job['title']!r}: {e}")
        return _scoring_error()

    if not raw:
        # Empty generation -> log what Ollama actually returned so it's debuggable.
        log.error(f"  empty response for {job['title']!r}: {r.text[:200]}")
        return _scoring_error()

    parsed = _parse_score(raw)
    if parsed is None:
        log.error(f"  unparseable response for {job['title']!r}: {raw[:200]}")
        return _scoring_error()
    return parsed


def _scoring_error() -> dict:
    return {"score": 0, "track": "none", "is_tech_company": False,
            "employment_type": "unknown", "work_mode": "unknown",
            "location": "", "commute_ok": True,
            "danish_level": "none", "deadline": "",
            "reasoning": "scoring error", "matched_skills": []}


def _parse_score(raw: str):
    """Tolerant parse: direct JSON, then fenced JSON, then regex for the key fields.
    With schema-constrained output the first path almost always succeeds."""
    candidates = [raw]
    fenced = re.search(r"\{.*\}", raw, re.DOTALL)  # grab the first {...} block
    if fenced:
        candidates.append(fenced.group(0))
    for c in candidates:
        try:
            d = json.loads(c)
            return {
                "score": int(d.get("score", 0)),
                "track": d.get("track", "none"),
                "is_tech_company": bool(d.get("is_tech_company", False)),
                "employment_type": d.get("employment_type", "unknown"),
                "work_mode": d.get("work_mode", "unknown"),
                "location": d.get("location", ""),
                "commute_ok": bool(d.get("commute_ok", True)),
                "danish_level": _coerce_danish_level(d),
                "deadline": d.get("deadline", ""),
                "reasoning": d.get("reasoning", ""),
                "matched_skills": d.get("matched_skills", []),
            }
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    # Last resort: pull the key fields out with regex.
    sm = re.search(r'"score"\s*:\s*(\d+)', raw)
    if not sm:
        return None
    tm = re.search(r'"track"\s*:\s*"([AB]|none)"', raw)
    rm = re.search(r'"reasoning"\s*:\s*"([^"]+)"', raw)
    em = re.search(r'"employment_type"\s*:\s*"(\w+)"', raw)
    wm = re.search(r'"work_mode"\s*:\s*"(\w+)"', raw)
    lm = re.search(r'"location"\s*:\s*"([^"]*)"', raw)
    dm = re.search(r'"deadline"\s*:\s*"([^"]*)"', raw)
    dlm = re.search(r'"danish_level"\s*:\s*"(none|preferred|required)"', raw)
    return {
        "score": int(sm.group(1)),
        "track": tm.group(1) if tm else "none",
        "is_tech_company": '"is_tech_company": true' in raw.lower(),
        "employment_type": em.group(1) if em else "unknown",
        "work_mode": wm.group(1) if wm else "unknown",
        "location": lm.group(1) if lm else "",
        "commute_ok": '"commute_ok": false' not in raw.lower(),  # default True unless explicit
        # danish_level from the new field; else fall back to the old boolean if present
        "danish_level": (dlm.group(1) if dlm else
                         ("required" if '"danish_required": true' in raw.lower() else "none")),
        "deadline": dm.group(1) if dm else "",
        "reasoning": rm.group(1) if rm else "regex fallback",
        "matched_skills": [],
    }


def _coerce_danish_level(d: dict) -> str:
    """Read danish_level from a parsed score dict, accepting the new enum and tolerating the
    old boolean danish_required (true -> 'required'). Anything unrecognised -> 'none'."""
    lvl = str(d.get("danish_level", "")).strip().lower()
    if lvl in ("none", "preferred", "required"):
        return lvl
    if "danish_required" in d:                       # backward compatibility
        return "required" if bool(d.get("danish_required")) else "none"
    return "none"


def ollama_json(prompt: str, schema: dict, num_predict: int = 1500):
    """Generic schema-constrained Ollama call. Returns a parsed dict, or None on failure.
    Same transport as score_job (same MODEL, think:False, low temperature) but task-agnostic,
    so c_prepare can reuse it for the job-ad -> structured-brief transform without touching
    the proven scoring path. (Single definition: an earlier duplicate of this helper was
    removed; the later 1500-default version always won at import, so behaviour is unchanged.)"""
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "format": schema,
        "think": False,
        "options": {"temperature": 0.1, "num_ctx": NUM_CTX, "num_predict": num_predict},
    }
    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
        raw = (data.get("response") or data.get("thinking") or "").strip()
    except Exception as e:
        log.error(f"  ollama_json call failed: {e}")
        return None
    if not raw:
        return None
    candidates = [raw]
    m = re.search(r"\{.*\}", raw, re.DOTALL)   # first {...} block, in case of stray prose
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return None

# ---------------------------------------------------------------------------
# ARCHIVE + REPORT
# ---------------------------------------------------------------------------

ARCHIVE_FIELDS = ["scraped_date", "title", "company", "location", "published_date",
                  "url", "track", "score", "employment_type", "work_mode", "commute_ok",
                  "danish_level", "is_tech_company", "deadline", "matched_skills",
                  "source", "reasoning"]

def load_seen_urls(path: str) -> set:
    """Return the set of CANONICAL URLs already in the archive, so a role already scored
    under any source/aggregator URL variant is recognised as seen and not re-scored."""
    if not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {canonical_url(row["url"]) for row in csv.DictReader(f) if row.get("url")}


def migrate_archive_if_needed(path: str) -> bool:
    """If the archive exists but its header doesn't match ARCHIVE_FIELDS (e.g. a column was
    added, removed, or reordered), rewrite it in place under the current schema BEFORE any
    append: existing values are kept by column NAME, new columns filled blank, unknown columns
    dropped. This stops the silent column-shift corruption a bare append would otherwise cause.
    Returns True if it migrated. NOTE: it cannot un-scramble rows already corrupted by an
    earlier mismatched append -- for that, rebuild the archive from scratch (see README)."""
    if not os.path.isfile(path):
        return False
    with open(path, newline="", encoding="utf-8") as f:
        try:
            header = next(csv.reader(f))
        except StopIteration:
            return False
    if header == ARCHIVE_FIELDS:
        return False
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ARCHIVE_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in ARCHIVE_FIELDS})
    os.replace(tmp, path)
    log.warning(f"Archive schema changed -> migrated {len(rows)} rows in "
                f"{os.path.basename(path)} to the current columns (new columns left blank). "
                f"If some older rows now look wrong, they were corrupted by a pre-migration "
                f"append; rebuild from scratch (see README).")
    return True

def _row_from(job: dict) -> dict:
    """Project a scored job onto ARCHIVE_FIELDS; join list fields (matched_skills)."""
    row = {}
    for k in ARCHIVE_FIELDS:
        val = job.get(k, "")
        if isinstance(val, list):           # e.g. matched_skills -> joined string
            val = "; ".join(str(x) for x in val)
        row[k] = val
    return row


class ArchiveWriter:
    """Incremental CSV writer. Each scored row is written and flushed immediately, so a
    hung Ollama call or a Ctrl-C doesn't discard work already done this run (the rows are
    handed to the OS before the next scoring call begins). Use as a context manager.

    Thread-safe by construction: write() takes a lock, so Phase 3 can parallelize scoring
    without touching this class. (The lock is a no-op cost while scoring is sequential.)"""

    def __init__(self, path: str):
        migrate_archive_if_needed(path)   # upgrade an old-schema CSV before appending
        new = not os.path.isfile(path)
        self._f = open(path, "a", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._f, fieldnames=ARCHIVE_FIELDS)
        self._lock = threading.Lock()
        self.count = 0
        if new:
            self._w.writeheader()
            self._f.flush()

    def write(self, job: dict):
        row = _row_from(job)
        with self._lock:
            self._w.writerow(row)
            self._f.flush()        # survive a Python crash / Ctrl-C without losing the row
            self.count += 1

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


RUNS_FIELDS = ["run_ts", "duration_s", "scrape_s", "fetch_gate_s", "score_s",
               "teasers", "prefiltered", "danish_early", "danish_body",
               "deadline_dropped", "fetched", "scored", "errors", "matches"]

def _log_run(run_start, total_s, timings, funnel):
    """Append one row per run to runs.csv: when it ran, how long each stage took, and the
    funnel counts. This is the longitudinal 'runs' table that analyze.py can summarize."""
    new = not os.path.isfile(RUNS_LOG)
    row = {
        "run_ts": run_start.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": round(total_s, 1),
        "scrape_s": round(timings.get("scrape", 0), 1),
        "fetch_gate_s": round(timings.get("fetch_gate", 0), 1),
        "score_s": round(timings.get("score", 0), 1),
    }
    row.update(funnel)
    with open(RUNS_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RUNS_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in RUNS_FIELDS})


def _parse_date(s):
    """Parse 'YYYY-MM-DD' (tolerant of trailing text); return a date or None."""
    s = (s or "").strip()
    if len(s) < 10:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def role_open_status(row, today=None, fresh_days=None):
    """Decide whether a scored role is still worth showing as 'open', and how urgent.
    Returns (is_open: bool, days_left: int|None).
      - Stated deadline in the past         -> closed.
      - Stated deadline today/future        -> open; days_left = days until deadline.
      - No usable deadline, seen recently    -> open; days_left = None.
      - No usable deadline, seen long ago    -> closed (assumed filled; still in archive).
    days_left is None when there's no deadline to count down to."""
    today = today or datetime.now().date()
    fresh_days = REPORT_FRESH_DAYS if fresh_days is None else fresh_days
    dl = _parse_date(row.get("deadline"))
    if dl is not None:
        return (dl >= today, (dl - today).days)
    seen = _parse_date(row.get("scraped_date"))
    if seen is not None:
        return ((today - seen).days <= fresh_days, None)
    return (True, None)  # no dates at all -> don't hide it


def open_shortlist(archive_path: str) -> list:
    """The actionable shortlist: from the full archive, dedup by CANONICAL URL (highest score),
    keep roles that pass the score / employment-type / commute filters and are still open, and
    sort by urgency (soonest deadline first) then score. Each row gets r["_days_left"].
    Shared by write_report and c_prepare so item numbering is identical."""
    if not os.path.isfile(archive_path):
        return []
    best = {}  # de-dup by canonical url, keeping the highest score
    with open(archive_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["score"] = int(r.get("score") or 0)
            except ValueError:
                r["score"] = 0
            u = canonical_url(r.get("url", ""))
            if u and (u not in best or r["score"] > best[u]["score"]):
                best[u] = r

    today_d = datetime.now().date()
    open_matches = []
    for r in best.values():
        if r["score"] < SCORE_THRESHOLD:
            continue
        if r.get("employment_type", "unknown") not in ACCEPTED_EMPLOYMENT_TYPES:
            continue
        if REQUIRE_COMMUTABLE and str(r.get("commute_ok", "true")).lower() == "false":
            continue
        if EXCLUDE_DANISH_REQUIRED and str(r.get("danish_level", "")).lower() == "required":
            continue
        is_open, days_left = role_open_status(r, today_d)
        if not is_open:
            continue
        r["_days_left"] = days_left
        open_matches.append(r)

    # urgency first (known deadline, soonest), then score
    return sorted(
        open_matches,
        key=lambda r: (r["_days_left"] is None,
                       r["_days_left"] if r["_days_left"] is not None else 0,
                       -r["score"]),
    )


def write_report(report_path: str, archive_path: str):
    """Rebuild the report fresh from the full archive each run, showing only roles that are
    likely STILL OPEN (deadline not passed; or, lacking a deadline, seen within
    REPORT_FRESH_DAYS). The archive keeps everything; this is just the actionable view."""
    if not os.path.isfile(archive_path):
        log.info("No archive yet; nothing to report.")
        return

    matches = open_shortlist(archive_path)

    today = datetime.now().strftime("%Y-%m-%d")
    with open(report_path, "w", encoding="utf-8") as f:  # 'w' = rebuilt each run
        f.write(f"# Job Matches — updated {today} — {len(matches)} open roles "
                f"(score >= {SCORE_THRESHOLD}, types: {', '.join(sorted(ACCEPTED_EMPLOYMENT_TYPES))})\n\n")
        f.write("Run `python c_prepare.py <number>` to prep one for Claude (e.g. "
                "`c_prepare.py 1`).\n\n")
        for i, j in enumerate(matches, 1):
            badge = "Technical" if j.get("track") == "A" else "Foot-in-door"
            if j.get("source") == "snippet":
                badge += " · ⚠ teaser only"

            # metadata line: employment type / work mode / Danish flag / deadline
            meta = []
            et = j.get("employment_type", "")
            if et and et != "unknown":
                meta.append(et)
            wm = j.get("work_mode", "")
            if wm and wm != "unknown":
                meta.append(wm)
            loc = j.get("location", "")
            if loc and loc.upper() != "N/A":
                meta.append(loc)
            dlvl = str(j.get("danish_level", "")).lower()
            if dlvl == "required":
                meta.append("⚠ Danish required")
            elif dlvl == "preferred":
                meta.append("Danish a plus")
            dl = j.get("deadline")
            dl = dl if _parse_date(dl) else ""   # ignore non-date junk (e.g. a stray bool)
            days_left = j.get("_days_left")
            if dl and days_left is not None:
                if days_left <= 0:
                    meta.append(f"⏰ closes today ({dl})")
                elif days_left <= 7:
                    meta.append(f"⏰ closes in {days_left}d ({dl})")
                else:
                    meta.append(f"deadline {dl} ({days_left}d)")
            elif dl:
                meta.append(f"deadline {dl}")
            meta_line = " · ".join(meta)

            f.write(f"### {i}. [{j['title']}]({j['url']}) — {j['score']}/100 · {badge}\n")
            f.write(f"**Company:** {j.get('company', '')}  \n")
            if meta_line:
                f.write(f"**Details:** {meta_line}  \n")
            f.write(f"**Seen:** {j.get('scraped_date', '')}  \n")
            f.write(f"**Why:** {j.get('reasoning', '')}\n\n---\n\n")
    log.info(f"Wrote {len(matches)} matches -> {report_path}")

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    run_start = datetime.now()
    t0 = time.monotonic()
    timings = {}  # stage -> seconds
    def _mark(stage, since):
        timings[stage] = time.monotonic() - since
        return time.monotonic()

    os.makedirs(BASE_DIR, exist_ok=True)
    log.info(f"Active profile: {ACTIVE_PROFILE} "
             + ("(owner; persisting to the main data dirs)" if IS_OWNER
                else f"(sandboxed -> {BASE_DIR}; nothing is written to your own data)"))
    cutoff = datetime.now().date() - timedelta(days=MAX_DAYS_OLD)
    seen = load_seen_urls(MASTER_ARCHIVE)        # CANONICAL urls already in the archive
    today = datetime.now().strftime("%Y-%m-%d")

    fresh, survivors = [], []
    seen_role_keys = set()                       # cross-source (company+title) de-dup, this run
    by_site = {}                                 # teaser counts per source, for the log

    # Stage 1 — scrape from every enabled source (each isolated in iter_sources).
    #   De-dup twice: (a) canonical URL against the archive + this run, and
    #                 (b) normalised company+title across sources this run.
    for job in iter_sources(cutoff):
        cu = canonical_url(job.get("url", ""))
        if not cu:
            continue
        if cu in seen:                           # already scored (any source/URL variant)
            continue
        rk = role_key(job)
        if rk and rk in seen_role_keys:          # same role from another source this run
            continue
        seen.add(cu)
        if rk:
            seen_role_keys.add(rk)
        by_site[job.get("source_site", "?")] = by_site.get(job.get("source_site", "?"), 0) + 1
        fresh.append(job)
    log.info(f"Stage 1: {len(fresh)} fresh teasers "
             + (", ".join(f"{k}={v}" for k, v in by_site.items()) or "(none)"))

    # Stage 2 — free pre-filter. Jobindex teasers carry a real snippet, so the full
    # INCLUDE/EXCLUDE keyword filter applies. The Hub LIST endpoint has no snippet body, so an
    # INCLUDE check would be title-only and overly strict; for that source apply only the
    # EXCLUDE-title guard (drop obvious HR/marketing) and let the LLM score the rest -- the
    # board is already tech/startup-curated and its volume is low.
    def _keep_candidate(j):
        if j.get("source_site") == "thehub":
            title = f" {j['title']} ".lower()
            return not any(term in title for term in EXCLUDE_TERMS)
        return passes_prefilter(j)
    candidates = [j for j in fresh if _keep_candidate(j)]
    log.info(f"Stage 2: {len(candidates)} passed keyword pre-filter")
    t_after_scrape = _mark("scrape", t0)

    # Stage 3a, in phases:
    #   (0) split: teasers that arrived WITH a full body (e.g. The Hub) skip fetching
    #   (1) OPTIONAL pre-fetch Danish gate (DROP_DANISH_LANGUAGE_ADS, default OFF) — when off,
    #       NOTHING is dropped on language here; everything is fetched and the multilingual LLM
    #       scores it and grades danish_level. The old behaviour (drop confident-Danish teasers
    #       to save fetches) is kept behind the flag for when recall matters less than speed.
    #   (2) concurrent fetch of the survivors' full pages
    #   (3) uniform post-process — deadline (+ OPTIONAL language gate), on body or snippet
    drop = {"danish_early": 0, "danish": 0, "deadline": 0}
    fallback = 0
    fetch_errors = []

    to_fetch, preloaded = [], []
    for job in candidates:
        if job.get("_description"):              # source already supplied the full body
            preloaded.append(job)
            continue
        # Pre-fetch language drop is OPT-IN. With it off (the default), no role is culled on
        # language before scoring — the LLM judges fit on the real body in any language and
        # records how much Danish the role needs as danish_level.
        if DROP_DANISH_LANGUAGE_ADS and confidently_danish(f"{job['title']}\n{job['snippet']}"):
            drop["danish_early"] += 1
            continue
        to_fetch.append(job)

    # (2) concurrent fetch (only the ones that need it, and only when fetching is on)
    if FETCH_FULL_DESC and to_fetch:
        log.info(f"Stage 3a: fetching {len(to_fetch)} pages with "
                 f"{min(FETCH_WORKERS, len(to_fetch))} workers "
                 f"({len(preloaded)} already have a body from their source)...")
        fetch_all(to_fetch)

    # (3) resolve each candidate to a description + source, then gate uniformly.
    def _gate_and_keep(job, desc):
        """deadline gate always; language drop only if DROP_DANISH_LANGUAGE_ADS is on."""
        if job.get("source") == "full" and deadline_passed(desc):
            drop["deadline"] += 1
            return
        if DROP_DANISH_LANGUAGE_ADS and looks_danish_only(desc):
            drop["danish"] += 1
            return
        job["_description"] = desc
        survivors.append(job)

    for job in to_fetch:
        desc = None
        if FETCH_FULL_DESC:
            fetched = job.pop("_fetched", "")
            err = job.pop("_fetch_err", "")
            if fetched:
                desc = fetched
                job["source"] = "full"
            else:
                if len(fetch_errors) < 5:
                    fetch_errors.append(f"{err}  <- {job['url']}")
                fallback += 1
        if desc is None:                          # fetch off or failed -> teaser snippet
            desc = f"{job['title']}\n{job['company']}\n{job['snippet']}"
            job["source"] = "snippet"
        _gate_and_keep(job, desc)

    for job in preloaded:                          # bodies supplied by the source (The Hub)
        job.setdefault("source", "full")
        _gate_and_keep(job, job["_description"])

    full_n = sum(1 for j in survivors if j.get("source") == "full")
    snip_n = len(survivors) - full_n
    log.info(f"Stage 3a: {len(survivors)} to score "
             f"(full desc: {full_n}, snippet: {snip_n}; "
             f"dropped danish_early={drop['danish_early']}, danish={drop['danish']}, "
             f"deadline={drop['deadline']})")
    if fetch_errors:
        log.warning("Sample fetch failures (these fell back to snippet scoring):")
        for e in fetch_errors:
            log.warning(f"  {e}")
    t_after_fetch = _mark("fetch_gate", t_after_scrape)

    # Stage 3b — LLM scoring. The score_job calls (network I/O to local Ollama) run in a
    # thread pool of SCORE_WORKERS; the model stays loaded the whole time. Archiving and
    # match bookkeeping happen in THIS thread as each future completes, so writes stay
    # ordered-by-completion and single-threaded (ArchiveWriter is locked regardless).
    # Each row is still written + flushed immediately, so an interruption keeps finished work.
    def _score_worker(job):
        res = score_job(job, job.pop("_description"))
        job.update(res)
        job["scraped_date"] = today
        return job

    matches = []
    errors = 0
    with ArchiveWriter(MASTER_ARCHIVE) as archive:
        with ThreadPoolExecutor(max_workers=max(1, SCORE_WORKERS)) as pool:
            futures = [pool.submit(_score_worker, j) for j in survivors]
            for fut in as_completed(futures):
                try:
                    job = fut.result()
                except Exception as e:               # _score_worker should never raise,
                    log.error(f"  scoring worker crashed: {e}")  # but don't kill the run
                    errors += 1
                    continue
                if job["reasoning"] == "scoring error":
                    errors += 1
                    continue  # don't archive failures -> URLs stay unseen and get retried
                archive.write(job)
                # Console shortlist mirrors the report's VIEW filter: score + accepted type.
                if (job["score"] >= SCORE_THRESHOLD
                        and job["employment_type"] in ACCEPTED_EMPLOYMENT_TYPES):
                    print(f"  [{job['score']:>3}/100 {job['track']} {job['employment_type']}] "
                          f"{job['title']} @ {job['company']} ({job.get('source_site','?')})")
                    matches.append(job)
        log.info(f"Stage 3b: scored + archived {archive.count} jobs "
                 f"({len(matches)} in shortlist; {errors} errors; {SCORE_WORKERS} workers)")
    t_after_score = _mark("score", t_after_fetch)

    write_report(MARKDOWN_REPORT, MASTER_ARCHIVE)

    # Per-run log (timing + funnel) -> a small "runs" table you can analyze over time.
    total_s = time.monotonic() - t0
    _log_run(run_start, total_s, timings, {
        "teasers": len(fresh), "prefiltered": len(candidates),
        "danish_early": drop["danish_early"], "danish_body": drop["danish"],
        "deadline_dropped": drop["deadline"], "fetched": len(to_fetch),
        "scored": archive.count, "errors": errors, "matches": len(matches),
    })
    log.info(f"Done in {total_s:.1f}s "
             f"(scrape {timings.get('scrape', 0):.1f}s, "
             f"fetch+gate {timings.get('fetch_gate', 0):.1f}s, "
             f"score {timings.get('score', 0):.1f}s).")
