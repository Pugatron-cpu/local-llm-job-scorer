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
import sys
import csv
import re
import json
import html
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
import extractors                        # deterministic field extraction (post-score merge)
from extractors import parse_deadline    # shared deadline parser (regex lives in extractors.py)

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
    """Normalise a job URL into a stable identity key: force the scheme to https (so an http
    vs https variant of the same link collapses), lowercase host, drop a leading 'www.', drop
    tracking query params (utm_*, source, Codes, ...), keep functional ones (sorted for
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
    return urlunsplit(("https", host, path, query, ""))   # scheme forced: identity key only


# Legal-entity suffixes and generic company words that vary between sources for the SAME
# employer ("Monta" vs "Monta ApS" vs "Monta A/S Danmark"). Stripped before building the
# cross-source role key so those variants collapse to one.
_COMPANY_NOISE = {
    "aps", "as", "ivs", "ps", "amba", "smba", "ks", "pmv",
    "inc", "incorporated", "ltd", "limited", "llc", "plc", "corp", "corporation",
    "gmbh", "ag", "ab", "oy", "oyj", "bv", "nv", "sa", "srl", "spa",
    "holding", "holdings", "group", "groups", "danmark", "denmark", "dk",
    "international", "intl", "global", "nordic", "scandinavia", "the",
}
# Title noise: articles/prepositions, generic vacancy words, and the m/f/d-style gender tags
# common on Danish/German boards. Removed, and the rest sorted, so word-order and boilerplate
# differences between two sources' phrasings of the same role collapse to one key.
_TITLE_NOISE = {
    "a", "an", "the", "and", "or", "for", "of", "to", "in", "with", "at", "on", "our",
    "job", "jobs", "position", "role", "vacancy", "opening", "wanted", "soeges", "soges",
    "m", "f", "d", "w", "x", "mfd", "mwd", "mw", "fm",
}


def _norm_company(name: str) -> str:
    """Normalise an employer name for identity matching: lowercase, drop punctuation, and
    strip legal-entity suffixes (ApS, A/S, GmbH, ...) and geo/holding words that vary by
    source. 'Monta ApS', 'Monta A/S Danmark' and 'Monta' all reduce to 'monta'."""
    toks = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
    return "".join(t for t in toks if t and t not in _COMPANY_NOISE)


def _norm_title_tokens(title: str) -> list:
    """The SET of significant title tokens, sorted: punctuation and noise words removed so
    word-order/boilerplate differences don't matter, but the tokens themselves are kept
    distinct so genuinely different roles don't collapse."""
    toks = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).split()
    return sorted({t for t in toks if t and t not in _TITLE_NOISE})


def role_key(job: dict) -> str:
    """A cross-source identity for the SAME role surfaced under different URLs by different
    sources. Built from a normalised company (legal suffixes like ApS/A/S and geo/holding
    words stripped) plus the SET of significant title tokens, SORTED — so word-order,
    gender-tag (m/f/d) and boilerplate differences between two sources' phrasings collapse to
    one key ('Student Assistant, Data' == 'Data Student Assistant' @ 'Monta' == 'Monta ApS').
    Empty if either side is missing, in which case the caller falls back to the URL key alone.
    Conservative by design: it collapses same-company + same-title-token-set roles and does
    NOT fuzzy-merge different token sets ('Data Analyst Student' stays distinct from 'Data
    Engineer Student'), so genuinely different roles at one employer are never lost."""
    c = _norm_company(job.get("company") or "")
    t = "".join(_norm_title_tokens(job.get("title") or ""))
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
            if new_on_page == 0:
                # All duplicates is NOT the end of results: seen_urls is shared across
                # queries, so with sorting=mostPopular a later query's page 1 is often
                # entirely roles already seen under an earlier query — page 2+ can still
                # hold new ones. Only an EMPTY docs list (handled above) ends the query.
                log.info(f"  [thehub] page {page_num}: all {len(docs)} already seen — continuing")
            time.sleep(random.uniform(0.5, 1.2))   # be polite

# ---------------------------------------------------------------------------
# STAGE 1: SCRAPE TEASERS — JOBNET (job.jobnet.dk, HTTP JSON API) — SCAFFOLD
# ---------------------------------------------------------------------------

def _jobnet_teaser(d: dict, cutoff_date):
    """Map one Jobnet job object -> a teaser dict. FIELD NAMES ARE UNVERIFIED GUESSES from
    Jobnet's historical API shape (Title/JobHeadline, HiringOrgName, WorkPlaceCity,
    Presentation body, DetailsUrl). Before enabling, check ONE real object in devtools and
    fix the pick() keys below — same drill as _thehub_teaser once had."""
    if not isinstance(d, dict):
        return None

    def pick(*keys):
        for k in keys:
            v = d.get(k)
            if v not in (None, "", [], {}):
                return v
        return ""

    title = pick("Title", "JobHeadline", "title", "headline")
    if not title:
        return None
    company = str(pick("HiringOrgName", "EmployerName", "company", "organisation"))
    if company and any(x in company.lower() for x in EXCLUDED_COMPANIES):
        return None
    url = str(pick("DetailsUrl", "JobAdUrl", "Url", "url"))
    if not url:
        jid = pick("JobAnnouncementId", "Id", "id")
        if jid:
            url = f"https://job.jobnet.dk/CV/FindWork/Details/{jid}"
    if not url:
        return None

    desc = _html_to_text(str(pick("Presentation", "Description", "Body", "description")))
    pub = str(pick("PostingCreated", "PublishedDate", "published", "createdAt"))
    published = "N/A"
    d_parsed = _parse_date(pub)
    if d_parsed is not None:
        if d_parsed < cutoff_date:
            return None
        published = str(d_parsed)

    teaser = {
        "title": str(title).strip(),
        "company": company.strip() or "N/A",
        "location": str(pick("WorkPlaceCity", "WorkplaceCity", "city", "location")).strip() or "N/A",
        "published_date": published,
        "snippet": desc[:500],
        "url": url.strip(),
        "source_site": "jobnet",
    }
    if desc and len(desc) > 200:      # full body in the list response -> skip the fetch stage
        teaser["_description"] = desc
        teaser["source"] = "full"
    return teaser


def scrape_jobnet(cutoff_date):
    """Source adapter: Jobnet (job.jobnet.dk), Denmark's public job board — covers publicly
    funded employers that Jobindex/The Hub under-serve. DISABLED by default
    (config.JOBNET_ENABLED): the JSON endpoint and field names MUST be confirmed once from a
    real browser (steps in config.py) before this feeds the archive. Ships behind the source
    seam so enabling it can never break the proven Jobindex/Hub paths."""
    if not JOBNET_API_URL:
        log.warning("  [jobnet] JOBNET_API_URL is empty -> skipping. Verify the endpoint per "
                    "the steps in config.py, then set JOBNET_ENABLED/JOBNET_API_URL.")
        return
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "application/json",
        "Referer": "https://job.jobnet.dk/CV/FindWork",
    }
    seen_urls = set()
    for term in (JOBNET_QUERIES or [""]):
        log.info(f"--- [jobnet] query: {term!r} ---")
        for page_i in range(JOBNET_MAX_PAGES):
            params = {JOBNET_QUERY_PARAM: term,
                      JOBNET_OFFSET_PARAM: page_i * JOBNET_PAGE_SIZE}
            try:
                r = requests.get(JOBNET_API_URL, params=params, headers=headers,
                                 timeout=TIMEOUT_S)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                log.error(f"  [jobnet] request failed (term={term!r} offset="
                          f"{page_i * JOBNET_PAGE_SIZE}): {str(e)[:160]}")
                break
            docs = _thehub_extract_list(data)   # generic docs/hits/results/items extractor
            if not docs and isinstance(data, dict):
                # Jobnet historically nested under "JobPositionPostings"
                docs = data.get("JobPositionPostings") or []
            if not docs:
                if page_i == 0:
                    log.warning(f"  [jobnet] no job list in response for term={term!r}. "
                                f"Check JOBNET_API_URL / _jobnet_teaser keys.")
                break
            for d in docs:
                t = _jobnet_teaser(d, cutoff_date)
                if not t:
                    continue
                cu = canonical_url(t["url"])
                if cu in seen_urls:
                    continue
                seen_urls.add(cu)
                yield t
            time.sleep(random.uniform(0.5, 1.2))

# ---------------------------------------------------------------------------
# SOURCE: ATS WATCHLIST (Greenhouse / Lever public career APIs)
# ---------------------------------------------------------------------------
# Instead of a keyword board, poll the PUBLIC job APIs of a hand-picked list of companies
# (config.ATS_COMPANIES). No auth, no scraping — these are the same JSON endpoints the
# companies' own career pages call. High precision (you choose the employers) and it surfaces
# roles that never reach Jobindex/The Hub. Each entry is "provider:slug" (optionally
# "provider:slug|Display Name"); provider is greenhouse or lever. Verified live 2026-07-05.

_ATS_HEADERS = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
                "Accept": "application/json"}


def _parse_ats_entry(entry: str):
    """'greenhouse:trustpilot' or 'greenhouse:trustpilot|Trustpilot' -> (provider, slug,
    display). Missing display is prettified from the slug."""
    spec, _, disp = str(entry).partition("|")
    provider, _, slug = spec.strip().partition(":")
    provider, slug, disp = provider.strip().lower(), slug.strip(), disp.strip()
    if not disp:
        disp = slug.replace("-", " ").replace("_", " ").strip().title()
    return provider, slug, disp


def _ats_location_ok(loc: str) -> bool:
    """Keep only teasers whose location matches one of ATS_LOCATION_KEEP (case-insensitive
    substring), so a big global board can't dump non-commutable roles into scoring. Empty
    keep-list = keep everything."""
    if not ATS_LOCATION_KEEP:
        return True
    loc = (loc or "").lower()
    return any(k.lower() in loc for k in ATS_LOCATION_KEEP)


def _ats_teaser(title, company, location, url, body, published, cutoff_date):
    """Shared teaser builder + location/company/freshness gate for both ATS providers.
    Returns a teaser dict, or None to drop the role. A substantial body arrives inline
    (source='full') so the fetch stage is skipped, same as The Hub."""
    title = (title or "").strip()
    url = (url or "").strip()
    if not title or not url:
        return None
    if not _ats_location_ok(location):
        return None
    if company and any(x in company.lower() for x in EXCLUDED_COMPANIES):
        return None
    pub = "N/A"
    d_parsed = _parse_date(published)
    if d_parsed is not None:
        if d_parsed < cutoff_date:
            return None
        pub = str(d_parsed)
    body = (body or "").strip()
    teaser = {
        "title": title,
        "company": (company or "N/A").strip() or "N/A",
        "location": (location or "N/A").strip() or "N/A",
        "published_date": pub,
        "snippet": body[:500],
        "url": url,
        "source_site": "ats",
    }
    if body and len(body) > 200:
        teaser["_description"] = body
        teaser["source"] = "full"
    return teaser


def _greenhouse_jobs(slug, display, cutoff_date):
    """Greenhouse job-board API: {"jobs":[{title, location:{name}, absolute_url, updated_at,
    content}]}. content=true returns the (HTML-escaped) body inline."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    r = requests.get(url, params={"content": "true"}, headers=_ATS_HEADERS, timeout=TIMEOUT_S)
    r.raise_for_status()
    for j in (r.json().get("jobs") or []):
        body = _html_to_text(html.unescape(j.get("content") or ""))
        t = _ats_teaser(title=j.get("title"), company=display,
                        location=(j.get("location") or {}).get("name", ""),
                        url=j.get("absolute_url"), body=body,
                        published=(j.get("updated_at") or "")[:10], cutoff_date=cutoff_date)
        if t:
            yield t


def _lever_jobs(slug, display, cutoff_date):
    """Lever postings API (?mode=json): a JSON ARRAY of {text, categories:{location},
    hostedUrl, descriptionPlain, createdAt(epoch ms)}."""
    url = f"https://api.lever.co/v0/postings/{slug}"
    r = requests.get(url, params={"mode": "json"}, headers=_ATS_HEADERS, timeout=TIMEOUT_S)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return
    for p in data:
        if not isinstance(p, dict):
            continue
        created = p.get("createdAt")
        pub = ""
        if isinstance(created, (int, float)):
            pub = datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d")
        body = p.get("descriptionPlain") or _html_to_text(p.get("description") or "")
        t = _ats_teaser(title=p.get("text"), company=display,
                        location=(p.get("categories") or {}).get("location", ""),
                        url=p.get("hostedUrl"), body=body,
                        published=pub, cutoff_date=cutoff_date)
        if t:
            yield t


def scrape_ats(cutoff_date):
    """Source adapter: poll the public career APIs of config.ATS_COMPANIES. Each company is
    isolated (a wrong slug or a provider hiccup just logs and skips that one company), so this
    can never take down the run. Dedups within the source on the canonical url."""
    if not ATS_COMPANIES:
        log.warning("  [ats] ATS_COMPANIES is empty -> skipping. Add 'greenhouse:<slug>' / "
                    "'lever:<slug>' entries in config.py.")
        return
    providers = {"greenhouse": _greenhouse_jobs, "lever": _lever_jobs}
    seen = set()
    for entry in ATS_COMPANIES:
        provider, slug, disp = _parse_ats_entry(entry)
        fn = providers.get(provider)
        if not fn or not slug:
            log.warning(f"  [ats] bad entry {entry!r} (want 'greenhouse:slug' or "
                        f"'lever:slug') -> skip")
            continue
        log.info(f"--- [ats] {provider}:{slug} ({disp}) ---")
        try:
            kept = 0
            for t in fn(slug, disp, cutoff_date):
                cu = canonical_url(t["url"])
                if cu in seen:
                    continue
                seen.add(cu)
                kept += 1
                yield t
            log.info(f"  [ats] {slug}: kept {kept} role(s) after location/freshness filter")
        except Exception as e:
            log.error(f"  [ats] {provider}:{slug} failed and was skipped: {str(e)[:160]}")
        time.sleep(random.uniform(0.3, 0.8))

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
    if JOBNET_ENABLED:
        sources.append(("jobnet", scrape_jobnet))
    if ATS_ENABLED:
        sources.append(("ats", scrape_ats))

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
                if not job["_fetched"]:
                    # One retry before this role is condemned to snippet scoring (a
                    # snippet-scored role is archived and never re-fetched, and its
                    # danish_level is a guess without the body — so a retry is cheap
                    # insurance against a transient timeout/interstitial).
                    time.sleep(random.uniform(1.5, 3.0))
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
_LINGUA_LOCK = threading.Lock()
def _detect_lang(text: str):
    """Return 'da'/'en'/'no'/'sv'/None. Lazily loads lingua, then langdetect. The build is
    guarded by a lock (double-checked) because _detect_lang is called from the SCORE_WORKERS
    thread pool: without it, several threads on the first batch would each build the detector
    concurrently — wasteful and a data race. Pre-warm once before the pool (see main)."""
    global _LINGUA
    try:
        if _LINGUA is None:
            with _LINGUA_LOCK:
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


def deadline_passed(text: str) -> bool:
    """Best-effort: returns True only if we confidently parse a past deadline.
    The regex + month parsing moved to extractors.parse_deadline, shared with
    extract_deadline (the cosmetic field), so the two can never disagree."""
    d = parse_deadline(text)
    return d is not None and d < datetime.now().date()

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

def _score_prompt(job: dict, description: str) -> str:
    """Build the scoring prompt.

    Extracted from score_job() so it can be PINNED by a golden test. The archive holds 1500+ roles
    scored with this exact text, and applications.csv is becoming an eval set (your status decision
    next to the model's score). Change the prompt and old scores stop being comparable to new ones,
    silently. tests/test_score_prompt.py asserts the rendered text byte-for-byte, so any edit that
    would move the scores fails loudly instead."""
    # The candidate's bridge experience is the one PERSONAL fact in this prompt. It used to be a
    # hardcoded sentence of the owner's CV right here, which meant every other profile was scored
    # against it. It now comes from the active profile (track_b_bridge), and is omitted entirely
    # when a profile doesn't set one, rather than rendering a dangling "via , then move laterally".
    bridge = (f" The candidate wants to enter a\n"
              f"  tech company via {TRACK_B_BRIDGE},\n"
              f"  then move laterally." if TRACK_B_BRIDGE else "")

    # Track B is optional. Dropped entirely (not left as an empty heading) for a candidate who
    # only wants direct matches — and the intro and the "track" instruction below follow suit,
    # so the model is never offered a "B" it isn't supposed to use.
    track_b = TRACK_B_DEF.format(bridge=bridge) + "\n\n" if TRACK_B_DEF else ""
    intro = ("There are TWO acceptable kinds of role." if TRACK_B_DEF
             else "There is ONE acceptable kind of role.")
    track_values = '"A", "B", or "none"' if TRACK_B_DEF else '"A" or "none"'
    hard_no = HARD_NO + "\n\n" if HARD_NO else ""

    return f"""You are screening jobs for a candidate. Score the fit 0-100.
{intro}

{TRACK_A_DEF}

{track_b}{hard_no}EMPLOYMENT TYPE (Danish market — classify factually; this does NOT affect the score,
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
                        If the ad is written ENTIRELY in Danish and never states that English
                        is sufficient, grade at least "preferred"; if it is clearly a
                        Danish-speaking customer/user/citizen-facing role, grade "required".
                        Does NOT affect the score; it is a flag for the candidate.
  - "deadline"        : application deadline as "YYYY-MM-DD" if clearly stated, else "".

Set "track" to {track_values}, and "is_tech_company" to whether the employer is a
{TARGET_SECTOR}.

Candidate profile:
{CANDIDATE_PROFILE}

Job title: {job['title']}
Company: {job['company']}
Description:
{description[:5000]}

Respond with ONLY a JSON object, no markdown fences, no other text, exactly like:
{{"score": 0-100, "track": "A"|"B"|"none", "is_tech_company": true|false, "employment_type": "student"|"part_time"|"full_time"|"internship"|"unknown", "work_mode": "onsite"|"hybrid"|"remote"|"unknown", "location": "city"|"", "commute_ok": true|false, "danish_level": "none"|"preferred"|"required", "deadline": "YYYY-MM-DD"|"", "reasoning": "one sentence", "matched_skills": ["skill", "skill"]}}"""


def ensure_model_available():
    """Preflight, called at run start BEFORE any scraping: confirm Ollama is up and the
    ACTIVE preset's model is actually served. Fails LOUDLY (sys.exit) naming every preset —
    deliberately no auto-failover, because each model scores on its own scale, and silently
    switching models would silently make new rows incomparable to the archive."""
    presets = "\n".join(
        f"    {k:<8} -> {v['model']} ({v['score_workers']} worker(s))"
        + ("   <- selected" if k == ACTIVE_MODEL_PRESET else "")
        for k, v in MODEL_PRESETS.items())
    how = ("  Select one: python a_scrape.py --model-preset <name>   "
           "(or JOBSEARCH_MODEL_PRESET=<name>)")
    tags_url = OLLAMA_URL.replace("/api/generate", "/api/tags")
    try:
        r = requests.get(tags_url, timeout=10)
        r.raise_for_status()
        served = {str(m.get("name", "")) for m in r.json().get("models", [])}
    except Exception as e:
        sys.exit(f"Ollama is unreachable at {tags_url} ({str(e)[:120]}).\n"
                 f"  Start it (`ollama serve`), then pick a preset:\n{presets}\n{how}")
    if MODEL not in served:
        sys.exit(f"Model '{MODEL}' (preset '{ACTIVE_MODEL_PRESET}') is not served by Ollama.\n"
                 f"  Installed models: {', '.join(sorted(served)) or '(none)'}\n"
                 f"  Pull it (`ollama pull {MODEL}`) or pick a preset that is installed:\n"
                 f"{presets}\n{how}")
    log.info(f"Model preset: {ACTIVE_MODEL_PRESET} -> {MODEL} ({SCORE_WORKERS} score worker(s))")


def score_job(job: dict, description: str, model: str | None = None) -> dict:
    """Score one job. `model` overrides config.MODEL for comparing candidate models on
    identical inputs; default (None) uses config.MODEL and behaviour is unchanged."""
    prompt = _score_prompt(job, description)

    payload = {
        "model": model or MODEL,
        "prompt": prompt,
        "stream": False,
        "format": SCORE_SCHEMA,  # schema-constrained output (was plain "json")
        # Reasoning-capable models (Gemma 4, Qwen 3.6): keep thinking OFF for scoring — the
        # schema constraint + low temperature does the work, and thinking multiplies latency
        # across hundreds of calls. NOTE (Gemma 4): with thinking disabled the larger models
        # may still emit an EMPTY thought block before the JSON; the parser's {...} fallback
        # in _parse_score handles it.
        "think": False,
        # 512 (was 400): headroom so a full object — reasoning + a populated matched_skills
        # array — can't get truncated mid-JSON into a parse failure (which drops the row and
        # forces a re-fetch+re-score next run). Still tiny next to the fetch cost.
        "options": {"temperature": 0.1, "num_ctx": NUM_CTX, "num_predict": 512},
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


# ---------------------------------------------------------------------------
# POST-SCORE DETERMINISTIC MERGE (extractors.py)
# ---------------------------------------------------------------------------

_DANISH_ORDER = {"none": 0, "preferred": 1, "required": 2}


def merge_extracted_fields(job: dict, desc: str) -> dict:
    """OVERWRITE the LLM's MECHANICAL fields with confident extractor values; where an
    extractor returned its sentinel ("", "unknown", None), the LLM's value stands. The
    judgment fields (score, track, reasoning, is_tech_company) are never touched, so the
    score scale stays identical to the archive's. FILLS fields only — it never drops, gates
    or filters a row; the deadline_passed drop upstream remains the only deterministic drop.

    Called AFTER job.update(score_job(...)), so job['location'] holds the LLM's answer by
    then. The teaser's own location (The Hub/ATS carry a real one; Jobindex says "N/A") is
    preserved by the caller under '_source_location', which extract_location prefers.

    danish_level is a FLOOR, not an overwrite: max-merge on none < preferred < required,
    mirroring the ad_language->preferred lift in _score_worker. An explicit "dansk er et
    krav" in the ad can only RAISE the LLM's grade, never lower it.

    Returns {"det": n, "llm": n, "danish_lift": 0|1} — how many of the six mechanical
    fields were deterministically set vs left to the LLM, for the funnel log/runs.csv."""
    stats = {"det": 0, "llm": 0, "danish_lift": 0}

    def _merge(field, value, sentinel):
        if value != sentinel and value is not None:
            job[field] = value
            stats["det"] += 1
        else:
            stats["llm"] += 1

    src_loc = job.pop("_source_location", "")
    _merge("employment_type",
           extractors.extract_employment_type(job.get("title", ""), desc), "unknown")
    _merge("work_mode", extractors.extract_work_mode(desc), "unknown")
    _merge("location",
           extractors.extract_location({"title": job.get("title", ""),
                                        "location": src_loc}, desc), "")
    # Commute is a pure geography lookup on the best-known location string (the merged one:
    # source-provided or extracted if confident, else the LLM's own answer).
    _merge("commute_ok", extractors.commute_ok(job.get("location", "")), None)
    _merge("deadline", extractors.extract_deadline(desc), "")
    _merge("matched_skills",
           extractors.extract_matched_skills(desc, SKILLS_VOCAB), None)

    floor = extractors.danish_level_floor(desc)
    if floor and (_DANISH_ORDER.get(floor, 0)
                  > _DANISH_ORDER.get(str(job.get("danish_level", "none")).lower(), 0)):
        job["danish_level"] = floor
        stats["danish_lift"] = 1
    return stats


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
                  "danish_level", "ad_language", "is_tech_company", "deadline",
                  "matched_skills", "source", "reasoning", "scoring_model"]
# ad_language: the ad's detected WRITING language ("da"/"en"/"sv"/"no"/"" = undetected),
# set deterministically by _detect_lang at scoring time — separate from danish_level, which
# is the LLM's judgement of how much Danish the ROLE requires. Rows scored before this
# column existed have it blank (migrate_archive_if_needed leaves new columns empty);
# `python a_scrape.py --rescore` refreshes still-open shortlist rows with blank flags.
# scoring_model: PROVENANCE — the exact model string that scored this row. Scores are only
# comparable within one model, so with presets (config.MODEL_PRESETS) every row records
# which scale it is on. Rows from before this column have it blank; those were scored by
# whatever MODEL was current at their scraped_date (see git history of config.py).

def load_seen_urls(path: str) -> set:
    """Return the set of CANONICAL URLs already in the archive, so a role already scored
    under any source/aggregator URL variant is recognised as seen and not re-scored."""
    if not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {canonical_url(row["url"]) for row in csv.DictReader(f) if row.get("url")}


def load_seen_role_keys(path: str) -> set:
    """Cross-source de-dup ACROSS RUNS: the set of role_key fingerprints already in the
    archive. Catches the SAME ad re-surfacing later under a different source/URL — a new
    canonical URL that load_seen_urls alone would miss — so it isn't fetched and re-scored as
    if new. Blank keys (missing company/title) are skipped so they never collapse unrelated
    rows. Conservative fingerprint (see role_key): only same-company + same-title-token-set
    roles match, so distinct roles at one employer stay distinct."""
    if not os.path.isfile(path):
        return set()
    keys = set()
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            k = role_key(row)
            if k:
                keys.add(k)
    return keys


def migrate_csv_if_needed(path: str, fields: list) -> bool:
    """If a CSV exists but its header doesn't match `fields` (a column was added, removed, or
    reordered), rewrite it in place under the current schema BEFORE any append: existing
    values are kept by column NAME, new columns filled blank, unknown columns dropped. This
    stops the silent column-shift corruption a bare append would otherwise cause. Used for
    both the archive and runs.csv. Returns True if it migrated. NOTE: it cannot un-scramble
    rows already corrupted by an earlier mismatched append -- rebuild from scratch for that."""
    if not os.path.isfile(path):
        return False
    with open(path, newline="", encoding="utf-8") as f:
        try:
            header = next(csv.reader(f))
        except StopIteration:
            return False
    if header == fields:
        return False
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    os.replace(tmp, path)
    log.warning(f"Schema changed -> migrated {len(rows)} rows in {os.path.basename(path)} "
                f"to the current columns (new columns left blank).")
    return True


def migrate_archive_if_needed(path: str) -> bool:
    return migrate_csv_if_needed(path, ARCHIVE_FIELDS)

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
               "deadline_dropped", "fetched", "snippet_fallback", "scored",
               "errors", "matches", "fields_det", "fields_llm",
               "model_preset", "model"]
# snippet_fallback: roles whose page fetch failed (after a retry) and were scored on the
# teaser only. Watch this column: a spike means Jobindex/ATS fetching broke, which silently
# degrades BOTH match quality and danish_level accuracy.
# fields_det / fields_llm: of the six mechanical fields per scored row (employment_type,
# work_mode, location, commute_ok, deadline, matched_skills), how many the deterministic
# extractors set vs left to the LLM's answer (see merge_extracted_fields). Old rows have
# them blank (migrate_csv_if_needed).
# model_preset / model: which config.MODEL_PRESETS entry (and exact model string) scored
# this run — the run-level view of the per-row scoring_model provenance column.

def _log_run(run_start, total_s, timings, funnel):
    """Append one row per run to runs.csv: when it ran, how long each stage took, and the
    funnel counts. This is the longitudinal 'runs' table that analyze.py can summarize."""
    migrate_csv_if_needed(RUNS_LOG, RUNS_FIELDS)   # header changed? realign before appending
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


def _pick_latest(cur, r):
    """True if row r should supersede cur under latest-scored-wins (tiebreak: higher score)."""
    if cur is None:
        return True
    return (r.get("scraped_date") or "", r["score"]) > ((cur.get("scraped_date") or ""),
                                                         cur["score"])


def _dedup_archive(archive_path: str) -> list:
    """The archive reduced to one row per real role, MOST RECENTLY SCORED wins (tiebreak:
    higher score). Latest-wins matters: a re-scored role (via --rescore, or a prompt/model
    change) must supersede its older row, which highest-wins would not guarantee.

    Two passes: (1) collapse by canonical URL (tracking params stripped); then (2) collapse
    what survives by role_key fingerprint, so the SAME ad that entered the archive under two
    different sources' URLs (e.g. a company ATS link via Jobindex and the clean The Hub link)
    shows ONCE in every view built on this — the shortlist and c_prepare numbering. The archive
    file itself is never rewritten; this is comparison-time only."""
    if not os.path.isfile(archive_path):
        return []
    best = {}
    with open(archive_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["score"] = int(r.get("score") or 0)
            except ValueError:
                r["score"] = 0
            u = canonical_url(r.get("url", ""))
            if not u:
                continue
            if _pick_latest(best.get(u), r):
                best[u] = r
    # Pass 2: fold cross-source URL variants of one role together by fingerprint. Rows with a
    # blank fingerprint (missing company/title) can't be matched safely, so they pass through.
    by_rk, passthrough = {}, []
    for r in best.values():
        rk = role_key(r)
        if not rk:
            passthrough.append(r)
            continue
        if _pick_latest(by_rk.get(rk), r):
            by_rk[rk] = r
    return list(by_rk.values()) + passthrough


def shortlist_reject_reason(r: dict, today=None):
    """The single source of truth for the shortlist VIEW filters: returns the reason string a
    (deduped) archive row is NOT on the open shortlist, or None if it qualifies. Shared by
    shortlist_with_reasons (report / c_prepare) AND main()'s live console, so the
    count printed during a run matches Weekly_Job_Matches.md instead of over-counting on just
    score+type. On a qualifying row it sets r['_days_left'] for downstream sorting/display."""
    today = today or datetime.now().date()
    try:
        score = int(r.get("score") or 0)
    except (ValueError, TypeError):
        score = 0
    if score < SCORE_THRESHOLD:
        return "score below threshold"
    if r.get("employment_type", "unknown") not in ACCEPTED_EMPLOYMENT_TYPES:
        return "employment type not targeted"
    if REQUIRE_COMMUTABLE and str(r.get("commute_ok", "true")).lower() == "false":
        return "not commutable"
    if EXCLUDE_DANISH_REQUIRED and str(r.get("danish_level", "")).lower() == "required":
        return "danish required (hidden by filter)"
    if EXCLUDE_DANISH_ADS and str(r.get("ad_language", "")).lower() == "da":
        return "ad written in Danish (hidden by filter)"
    if r.get("track") == "B" and score < TRACK_B_MIN_SCORE:
        return f"track B below its own bar ({TRACK_B_MIN_SCORE})"
    is_open, days_left = role_open_status(r, today)
    if not is_open:
        return "closed / aged out"
    r["_days_left"] = days_left
    return None


def shortlist_with_reasons(archive_path: str):
    """(kept, dropped) — the open shortlist plus a Counter of why each deduped archive row
    was excluded, so 'why isn't X showing?' is
    answerable without re-reading the filter code. Filtering is delegated to
    shortlist_reject_reason so this view and main()'s live console never drift."""
    from collections import Counter
    dropped = Counter()
    today_d = datetime.now().date()
    kept = []
    for r in _dedup_archive(archive_path):
        reason = shortlist_reject_reason(r, today_d)
        if reason:
            dropped[reason] += 1
            continue
        kept.append(r)

    # urgency first (known deadline, soonest), then score
    kept.sort(key=lambda r: (r["_days_left"] is None,
                             r["_days_left"] if r["_days_left"] is not None else 0,
                             -int(r.get("score") or 0)))
    return kept, dropped


def open_shortlist(archive_path: str) -> list:
    """The actionable shortlist: deduped archive rows passing the score / type / commute /
    Danish / track-B / still-open filters, sorted by urgency then score. Each row gets
    r["_days_left"]. Shared by write_report and c_prepare so numbering is identical."""
    return shortlist_with_reasons(archive_path)[0]


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
            elif dlvl == "":
                # scored before danish_level/ad_language existed -> flags unreliable
                meta.append("⚠ flags unknown (old scoring — run a_scrape.py --rescore)")
            if str(j.get("ad_language", "")).lower() == "da":
                meta.append("ad in Danish")
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
    return len(matches)

def rescore_missing_flags(limit: int = 40):
    """Maintenance mode (`python a_scrape.py --rescore`): rows scored before the
    danish_level / ad_language columns existed have those flags BLANK, so they slip past the
    Danish view filters and show '⚠ flags unknown' in the report. This re-fetches and
    re-scores the still-relevant ones (score >= threshold, targeted type, still open) and
    appends fresh rows; latest-wins de-dup then makes the new scoring supersede the old rows
    everywhere. Capped at `limit` per invocation to bound runtime."""
    return _rescore_open(force=False, limit=limit)


def rescore_all(limit: int = 40):
    """Escape hatch (`python a_scrape.py --rescore-all`): re-score EVERY still-open
    shortlist-relevant row, not just the flag-blank ones. Use this after a model or prompt
    change — without it, roles that already carry danish_level / ad_language are frozen at
    their old scoring forever (rescore_missing_flags deliberately skips them). Same latest-wins
    supersede and per-invocation `limit` as --rescore; still scoped to OPEN roles, so a
    rejected/closed role stays frozen (that's intentional — you decided on it already)."""
    return _rescore_open(force=True, limit=limit)


def _rescore_open(force: bool, limit: int):
    """Shared worker for the two re-score modes. `force=False` only touches rows with missing
    Danish/ad-language flags; `force=True` re-scores all open shortlist-relevant rows."""
    label = "--rescore-all" if force else "--rescore"
    ensure_model_available()   # re-scoring scores too: same loud preflight as a normal run
    today = datetime.now().strftime("%Y-%m-%d")
    today_d = datetime.now().date()
    stale = []
    for r in _dedup_archive(MASTER_ARCHIVE):
        if r["score"] < SCORE_THRESHOLD:
            continue
        if r.get("employment_type", "unknown") not in ACCEPTED_EMPLOYMENT_TYPES:
            continue
        if not role_open_status(r, today_d)[0]:
            continue
        if not force and r.get("danish_level", "") and r.get("ad_language", "") != "":
            continue                       # flags already present -> nothing to fix
        stale.append(r)
    if not stale:
        if force:
            log.info(f"{label}: no open shortlist-relevant rows to re-score. Done.")
        else:
            log.info(f"{label}: no open shortlist-relevant rows with missing flags. Done.")
        return
    if len(stale) > limit:
        log.info(f"{label}: {len(stale)} rows to re-score; doing the first {limit} "
                 f"(run again for the rest).")
        stale = stale[:limit]
    log.info(f"{label}: re-fetching + re-scoring {len(stale)} rows...")

    jobs = []
    for r in stale:
        jobs.append({"title": r.get("title", ""), "company": r.get("company", ""),
                     "location": r.get("location", "N/A"),
                     "published_date": r.get("published_date", "N/A"),
                     "snippet": "", "url": r.get("url", ""),
                     "source_site": r.get("source", "")})
    fetch_all(jobs)

    with ArchiveWriter(MASTER_ARCHIVE) as archive:
        for job in jobs:
            desc = job.pop("_fetched", "")
            job.pop("_fetch_err", "")
            if not desc:
                log.warning(f"  {label}: fetch failed, skipping {job['url']}")
                continue
            job["source"] = "full"
            job["_source_location"] = job.get("location", "")   # see _score_worker
            res = score_job(job, desc)
            if res["reasoning"] == "scoring error":
                log.warning(f"  {label}: scoring failed, skipping {job['title']!r}")
                continue
            job.update(res)
            # Same post-score deterministic merge as the main scoring path, so a re-scored
            # row carries the same extractor-owned fields as a freshly scored one.
            merge_extracted_fields(job, desc)
            job["scoring_model"] = MODEL      # provenance: which scale this score is on
            job["ad_language"] = _detect_lang(desc[:2000]) or ""
            if job["ad_language"] == "da" and job.get("danish_level") == "none":
                job["danish_level"] = "preferred"
            job["scraped_date"] = today
            archive.write(job)
            print(f"  re-scored [{job['score']:>3}] {job['title']} @ {job['company']} "
                  f"(danish={job['danish_level']}, ad={job['ad_language'] or '?'})")
        log.info(f"{label}: appended {archive.count} refreshed rows.")
    write_report(MARKDOWN_REPORT, MASTER_ARCHIVE)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

RAW_TEASER_FIELDS = ["scrape_ts", "source_site", "title", "company", "location",
                     "published_date", "url", "canonical_url", "snippet", "passed_prefilter"]


def _log_raw_teasers(rows: list, path: str):
    """Append every teaser this run SAW to raw_teasers.csv — before dedup, before the keyword
    gate, before scoring. Pure side effect: nothing reads this file, so a failure here must never
    take the scrape down with it (hence the blanket except).

    Why it exists: the archive only keeps what passed INCLUDE_TERMS and then scored, so it can't
    answer "what did my filter reject?" or "how long did this ad stay up?". Those questions can
    only be answered by data captured at scrape time. Miss it and it's gone."""
    if not rows:
        return
    try:
        new = not os.path.isfile(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=RAW_TEASER_FIELDS)
            if new:
                w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in RAW_TEASER_FIELDS})
        log.info(f"Raw log: +{len(rows)} teaser sighting(s) -> {os.path.basename(path)}")
    except Exception as e:
        log.warning(f"Raw teaser log failed ({e}) — the scrape carries on; only the raw log lost.")


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
    ensure_model_available()   # fail loudly NOW, not after a 20-minute scrape
    cutoff = datetime.now().date() - timedelta(days=MAX_DAYS_OLD)
    seen = load_seen_urls(MASTER_ARCHIVE)            # CANONICAL urls already in the archive
    seen_role_keys = load_seen_role_keys(MASTER_ARCHIVE)  # role fingerprints already scored
    today = datetime.now().strftime("%Y-%m-%d")

    fresh, survivors = [], []
    dup_url = dup_role = 0                        # de-dup counters, for the log
    by_site = {}                                 # teaser counts per source, for the log
    raw_seen = []                                # EVERY sighting this run, for the raw log

    # Hoisted above Stage 1 so the raw log can record the pre-filter VERDICT for each teaser as
    # it's seen (including the ones about to be dropped). Same function Stage 2 uses below — one
    # definition, so the logged verdict can never drift from the real one.
    def _keep_candidate(j):
        if j.get("source_site") == "thehub":
            title = f" {j['title']} ".lower()
            if any(term in title for term in EXCLUDE_TERMS):
                return False
            body = j.get("_description", "")
            if body:  # full body available -> require a real INCLUDE hit like any other ad
                text = f" {j['title']} {body[:2500]} ".lower()
                return any(term in text for term in INCLUDE_TERMS)
            return True  # no body to judge -> keep, the LLM decides
        return passes_prefilter(j)

    # Stage 1 — scrape from every enabled source (each isolated in iter_sources).
    #   De-dup on two keys, each spanning THIS run AND the whole archive:
    #     (a) canonical URL      — same link (tracking params stripped) already seen.
    #     (b) role_key fingerprint — the SAME ad under a DIFFERENT source's URL (normalised
    #         company + sorted title tokens), which the URL key alone can't catch. This is
    #         what collapses "same job, two boards, two links" across sources and across runs.
    scrape_ts = run_start.strftime("%Y-%m-%d %H:%M:%S")
    for job in iter_sources(cutoff):
        cu = canonical_url(job.get("url", ""))

        # Raw log FIRST: every sighting, including the ones the next four lines are about to
        # drop as duplicates. A still-live ad re-seen on 9 consecutive runs writes 9 rows — that
        # repetition IS the signal (days-on-market). Dedup happens at analysis time, not here.
        if LOG_RAW_TEASERS:
            raw_seen.append({**job, "scrape_ts": scrape_ts, "canonical_url": cu,
                             "passed_prefilter": _keep_candidate(job)})

        if not cu:
            continue
        if cu in seen:                           # already scored (any source/URL variant)
            dup_url += 1
            continue
        rk = role_key(job)
        if rk and rk in seen_role_keys:          # same ad, different source/URL (or another run)
            dup_role += 1
            continue
        seen.add(cu)
        if rk:
            seen_role_keys.add(rk)
        by_site[job.get("source_site", "?")] = by_site.get(job.get("source_site", "?"), 0) + 1
        fresh.append(job)
    log.info(f"Stage 1: {len(fresh)} fresh teasers "
             + (", ".join(f"{k}={v}" for k, v in by_site.items()) or "(none)")
             + f"  (skipped {dup_url} seen-URL, {dup_role} cross-source/prior-run duplicates)")
    if LOG_RAW_TEASERS:
        _log_raw_teasers(raw_seen, RAW_TEASERS)   # everything SEEN, not just what survived

    # Stage 2 — free pre-filter. Jobindex teasers carry a real snippet, so the full
    # INCLUDE/EXCLUDE keyword filter applies. Sources that deliver the FULL body with the
    # teaser (The Hub) get the same INCLUDE check run over title+body — previously they
    # skipped INCLUDE entirely, which let broad Hub queries flood the LLM with unrelated
    # roles and was a main driver of junk matches. Only a body-less teaser from a non-snippet
    # source falls back to the lenient EXCLUDE-title-only guard.
    # (_keep_candidate is defined above Stage 1, so the raw log records this same verdict.)
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
        desc = job.pop("_description")
        # The teaser's own location, saved BEFORE the LLM result lands: job.update(res)
        # overwrites job['location'] with the model's answer, and extract_location prefers
        # the source-provided one (The Hub/ATS carry a real locality; Jobindex says "N/A").
        job["_source_location"] = job.get("location", "")
        res = score_job(job, desc)
        job.update(res)
        # Post-score deterministic merge: confident extractor values overwrite the LLM's
        # mechanical fields; sentinels leave them alone. Judgment fields untouched.
        if res.get("reasoning") != "scoring error":
            job["_extract_stats"] = merge_extracted_fields(job, desc)
        job["scoring_model"] = MODEL          # provenance: which scale this score is on
        # Deterministic ad WRITING language (independent of the LLM's danish_level, which is
        # the ROLE's requirement). Feeds the EXCLUDE_DANISH_ADS view filter + report flag.
        job["ad_language"] = _detect_lang(desc[:2000]) or ""
        # Belt-and-braces: an ad written entirely in Danish where the model still said the
        # role needs NO Danish is almost always an under-grade -> lift to "preferred" so the
        # flag (and the danish_level filter, if strict) errs on the honest side.
        if job["ad_language"] == "da" and job.get("danish_level") == "none" \
                and job.get("reasoning") != "scoring error":
            job["danish_level"] = "preferred"
        job["scraped_date"] = today
        return job

    # Pre-warm the language detector on this thread, so the SCORE_WORKERS threads don't race
    # to build it concurrently on their first _detect_lang call (the build is expensive).
    if survivors:
        _detect_lang("Warm up the language detector before the scoring pool starts.")

    matches = []
    errors = 0
    fields_det = fields_llm = danish_lifts = 0   # extractor-vs-LLM field counts, for the log
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
                stats = job.pop("_extract_stats", None)
                if stats:
                    fields_det += stats["det"]
                    fields_llm += stats["llm"]
                    danish_lifts += stats["danish_lift"]
                archive.write(job)
                # Console preview uses the SAME predicate as the report (score, type, commute,
                # Danish, track-B bar, still-open) via shortlist_reject_reason, so what prints
                # here can't over-count relative to Weekly_Job_Matches.md the way score+type did.
                if shortlist_reject_reason(job) is None:
                    print(f"  [{job['score']:>3}/100 {job['track']} {job['employment_type']}] "
                          f"{job['title']} @ {job['company']} ({job.get('source_site','?')})")
                    matches.append(job)
        log.info(f"Stage 3b: scored + archived {archive.count} jobs "
                 f"({len(matches)} newly qualify; {errors} errors; {SCORE_WORKERS} workers)")
        log.info(f"Extractors: {fields_det} mechanical field(s) set deterministically, "
                 f"{fields_llm} left to the LLM; danish_level floor lifted "
                 f"{danish_lifts} row(s)")
    t_after_score = _mark("score", t_after_fetch)

    # Authoritative shortlist size = the whole archive re-filtered (this run's new hits PLUS
    # still-open rows from prior runs), which is what the report and runs.csv should record.
    shortlist_n = write_report(MARKDOWN_REPORT, MASTER_ARCHIVE)

    # Per-run log (timing + funnel) -> a small "runs" table you can analyze over time.
    total_s = time.monotonic() - t0
    _log_run(run_start, total_s, timings, {
        "teasers": len(fresh), "prefiltered": len(candidates),
        "danish_early": drop["danish_early"], "danish_body": drop["danish"],
        "deadline_dropped": drop["deadline"], "fetched": len(to_fetch),
        "snippet_fallback": fallback,
        "scored": archive.count, "errors": errors, "matches": shortlist_n,
        "fields_det": fields_det, "fields_llm": fields_llm,
        "model_preset": ACTIVE_MODEL_PRESET, "model": MODEL,
    })
    log.info(f"Done in {total_s:.1f}s "
             f"(scrape {timings.get('scrape', 0):.1f}s, "
             f"fetch+gate {timings.get('fetch_gate', 0):.1f}s, "
             f"score {timings.get('score', 0):.1f}s).")
