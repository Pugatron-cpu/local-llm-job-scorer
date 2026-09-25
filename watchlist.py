"""
watchlist.py — watch named companies' OWN careers pages for new postings.

Why: companies you have a way in to often post only on their own site, so the keyword boards
(Jobindex / The Hub) never see the role, and you hear about it after the deadline. The watchlist
reads each company's careers page on every run, records every posting it lists, checks each new
one's location, and hands the in-location ones to the normal pipeline to be scored. The report
then shows them in their own section, whatever their score or type.

Everything personal (which companies, which location counts) lives in the profile:

    watch_confirm_terms = ["østerbro", "vibenshuset", "2100 københavn"]   # location confirmed
    watch_area_terms    = ["copenhagen", "københavn"]                     # shown, flagged

    [[watch]]
    name      = "Dalux"
    aliases   = ["dalux"]                         # company names as the boards spell them
    url       = "https://dalux.hr-on.com/"        # the page that lists the jobs
    render    = "static"                          # or "browser" for JavaScript-built pages
    link_regex    = 'show-job/(\\d+)'             # finds each posting in the page source
    link_template = "https://dalux.hr-on.com/show-job/{0}"   # optional: build the url
    confirm_terms = ["copenhagen", "denmark"]     # optional per-company override

A posting counts as OPEN while it is still listed on the careers page: that is the ground truth,
better than guessing from its age. State lives in watchlist_postings.csv (derived data, safe to
delete: the next run rebuilds it, re-fetching each posting once).
"""

import csv
import html
import os
import random
import re
import time
from datetime import datetime
from urllib.parse import urljoin

import requests

STATE_FIELDS = ["url", "company", "title", "location_status", "location_hint",
                "first_seen", "last_seen"]

# Location verdicts. "elsewhere" is hidden; the rest are shown, the last three flagged.
CONFIRMED, AREA, UNKNOWN, ELSEWHERE = "confirmed", "area", "unknown", "elsewhere"

_HEADERS = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}

# Postings that aren't jobs: the open-application / talent-pool entries most boards list.
_NOT_A_JOB_RE = re.compile(r"unsolicited|uopfordret|talent\s*pool|spontaneous|open application",
                           re.I)


# ---------------------------------------------------------------------------
# pure helpers (tested)
# ---------------------------------------------------------------------------

# Company boilerplate that names the HQ city without saying where THIS job is:
# "Founded in Copenhagen in 2005", "at our Copenhagen HQ", "headquartered in Copenhagen".
_BOILER_BEFORE_RE = re.compile(r"(?:founded|headquartered|headquarters|hq|born|started|established)"
                               r"\s+in\s+$")
_BOILER_AFTER_RE = re.compile(r"^\s*(?:hq\b|headquarters|head\s+office|in\s+(?:19|20)\d\d)")

# A posting's own labelled location field: "Location: Spain", or the label on its own line
# with the value on the next ("Lokation:\n\nKøbenhavn Ø"). When present it decides alone.
_LABEL_RE = re.compile(
    r"(?im)^[ \t]*(?:job[ \t]+)?(?:location|lokation|arbejdssted|placering|work[ \t]+location)"
    r"(?:[ \t]+of[ \t]+(?:the[ \t]+)?(?:job|position|role))?[ \t]*:?[ \t]*(?:\n[ \t]*)*([^\n]{2,80})$")


def _term_hit(text: str, terms, skip_boilerplate: bool = False) -> str:
    """The first term found in text (case-insensitive, word-bounded), or "". With
    skip_boilerplate, occurrences in company-description phrases don't count."""
    low = (text or "").lower()
    for t in terms or []:
        t = str(t).lower().strip()
        if not t:
            continue
        for m in re.finditer(r"(?<!\w)" + re.escape(t) + r"(?!\w)", low):
            if skip_boilerplate and (_BOILER_BEFORE_RE.search(low[max(0, m.start() - 30):m.start()])
                                     or _BOILER_AFTER_RE.search(low[m.end():m.end() + 20])):
                continue
            return t
    return ""


def labelled_location(text: str, location_regex: str = "") -> str:
    """The value of the posting's own location field, or "" if it has none. A company's own
    location_regex (group 1 = the value) is tried first, e.g. Dalux's 'Dalux | <office>' line."""
    if location_regex:
        m = re.search(location_regex, text or "", re.M)
        if m:
            return m.group(1).strip()
    m = _LABEL_RE.search(text or "")
    return m.group(1).strip() if m else ""


def location_status(text: str, confirm_terms, area_terms, location_regex: str = ""):
    """(status, hint). A labelled location field decides alone. Otherwise: CONFIRMED if a
    confirm term appears anywhere (the building address only shows up when the job is there),
    else AREA if an area term appears outside company boilerplate, else ELSEWHERE. No text at
    all (fetch failed) -> UNKNOWN, which is shown, never hidden: a posting we couldn't read
    must not disappear silently."""
    if not (text or "").strip():
        return UNKNOWN, ""
    label = labelled_location(text, location_regex)
    scope = label or text
    hit = _term_hit(scope, confirm_terms)
    if hit:
        return CONFIRMED, hit
    hit = _term_hit(scope, area_terms, skip_boilerplate=not label)
    if hit:
        return AREA, hit
    return ELSEWHERE, (f"location: {label}" if label else "")


def extract_links(page_source: str, base_url: str, link_regex: str, link_template: str = ""):
    """Posting urls found in a careers page's source, in page order, de-duplicated.
    With a template, each match's groups fill it ({0}, {1}, ...); without, the match itself
    (or its first group) is the url, resolved against base_url if relative."""
    rx = re.compile(link_regex)
    out, seen = [], set()
    for m in rx.finditer(page_source or ""):
        if link_template:
            url = link_template.format(*(m.groups() or (m.group(0),)))
        else:
            url = m.group(1) if m.groups() else m.group(0)
        url = urljoin(base_url, url.replace("&amp;", "&"))
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def company_matches(company: str, aliases) -> bool:
    """Word-bounded alias match on a company name ("Nets A/S" yes, "Planets ApS" no)."""
    return bool(_term_hit(company, aliases))


# ---------------------------------------------------------------------------
# state (watchlist_postings.csv)
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict:
    """url -> state row. Missing file -> {}."""
    if not os.path.isfile(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {r["url"]: r for r in csv.DictReader(f) if r.get("url")}


def save_state(path: str, state: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=STATE_FIELDS)
        w.writeheader()
        for r in state.values():
            w.writerow({k: r.get(k, "") for k in STATE_FIELDS})
    os.replace(tmp, path)            # never leave a half-written state file


def check_key(company: str) -> str:
    """State key of a company's check record: the date its careers page was last READ
    successfully. A posting is still listed iff its last_seen equals that date. Kept per company,
    so one page failing doesn't make another company's postings look delisted, and a page that
    now lists nothing does make them delisted."""
    return f"#checked:{company}"


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

class _Browser:
    """One lazily-started headless Chromium for the whole watch pass (only started if some
    entry or posting actually needs it)."""

    def __init__(self):
        self._pw = self._browser = self.page = None

    def get(self):
        if self.page is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self.page = self._browser.new_page(user_agent=_HEADERS["User-Agent"])
        return self.page

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


def _page_source(entry: dict, browser: _Browser, timeout_s: int) -> str:
    if entry.get("render") == "browser":
        page = browser.get()
        page.goto(entry["url"], wait_until="networkidle", timeout=timeout_s * 1000)
        time.sleep(2)                     # late-rendering lists
        return page.content()
    r = requests.get(entry["url"], headers=_HEADERS, timeout=timeout_s)
    r.raise_for_status()
    return r.text


def _fetch_posting(url: str, browser: _Browser, timeout_s: int):
    """(title, FULL text) of one posting, rendered in the browser (many boards build the ad
    with JavaScript). Full, not capped: some boards put the office line at the very end.
    ("", "") on failure."""
    try:
        page = browser.get()
        # not "networkidle": chat widgets and trackers keep some pages busy forever
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        time.sleep(2.5)
        title = ""
        try:
            h1 = page.query_selector("h1")
            title = (h1.inner_text() if h1 else "").strip()
        except Exception:
            pass
        if not title:             # tab title: drop the " | Company" suffix it usually carries
            title = re.sub(r"\s+[|–-]\s+[^|–-]+$", "", (page.title() or "").strip())
        text = re.sub(r"\n{3,}", "\n\n", page.inner_text("body")).strip()
        if len(text) >= 200:
            return re.sub(r"\s+", " ", title)[:200], text
    except Exception:
        pass
    return _fetch_posting_static(url, timeout_s)


def _fetch_posting_static(url: str, timeout_s: int):
    """Fallback when the browser gets nothing (slow page, bot check): a plain download,
    tags stripped. Enough for the location check and for scoring server-rendered ads."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout_s)
        r.raise_for_status()
        src = r.text
    except Exception:
        return "", ""
    title = ""
    for rx in (r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>"):
        m = re.search(rx, src, re.S | re.I)
        title = html.unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(1))).strip() if m else ""
        if title:
            if "title>" in rx:     # tab title: drop the " | Company" suffix
                title = re.sub(r"\s+[|–-]\s+[^|–-]+$", "", title)
            break
    src = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", src)
    src = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|dt|dd)>", "\n", src)
    text = html.unescape(re.sub(r"<[^>]+>", " ", src))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return re.sub(r"\s+", " ", title)[:200], text


# ---------------------------------------------------------------------------
# the watch pass (a pipeline source)
# ---------------------------------------------------------------------------

def scrape(watchlist, state_path, confirm_terms, area_terms, archived_urls,
           canon, log, timeout_s: int = 45):
    """Generator of teaser dicts for the pipeline: every IN-LOCATION posting on a watched
    careers page that isn't in the archive yet, with its body attached (so the fetch stage is
    skipped). Updates the state file as it goes. `archived_urls` holds canonical urls already
    scored; `canon` is core.canonical_url. Each company is isolated: one broken page logs and
    is skipped, and its postings keep their last_seen (so they don't vanish on a hiccup)."""
    state = load_state(state_path)
    today = datetime.now().strftime("%Y-%m-%d")
    browser = _Browser()
    try:
        for entry in watchlist:
            name = entry.get("name", "?")
            try:
                src = _page_source(entry, browser, timeout_s)
                urls = extract_links(src, entry["url"], entry["link_regex"],
                                     entry.get("link_template", ""))
            except Exception as e:
                log.error(f"  [watch] {name}: careers page failed, skipped: {str(e)[:160]}")
                continue
            state[check_key(name)] = {"url": check_key(name), "company": name,
                                      "last_seen": today}
            ct = entry.get("confirm_terms") or confirm_terms
            at = entry.get("area_terms") or area_terms
            new = shown = 0
            for url in urls:
                st = state.get(url)
                body = None
                if st is None:            # first sighting: read it once, judge the location
                    title, body = _fetch_posting(url, browser, timeout_s)
                    status, hint = location_status(body, ct, at, entry.get("location_regex", ""))
                    st = {"url": url, "company": name, "title": title,
                          "location_status": status, "location_hint": hint,
                          "first_seen": today}
                    state[url] = st
                    new += 1
                    time.sleep(random.uniform(0.8, 1.6))    # polite
                st["last_seen"] = today
                if st["location_status"] == ELSEWHERE or _NOT_A_JOB_RE.search(st["title"]):
                    continue
                if canon(url) in archived_urls:
                    continue              # already scored; shown from the archive
                if body is None:          # seen before but never scored (e.g. score error)
                    title, body = _fetch_posting(url, browser, timeout_s)
                    st["title"] = st["title"] or title
                shown += 1
                teaser = {"title": st["title"] or url, "company": name, "location": "N/A",
                          "published_date": "N/A", "snippet": (body or "")[:500],
                          "url": url, "source_site": "watch"}
                if body and len(body) > 200:
                    teaser["_description"] = body[:6000]   # same cap as the fetch stage
                    teaser["source"] = "full"
                yield teaser
            log.info(f"  [watch] {name}: {len(urls)} listed, {new} new, "
                     f"{shown} to score")
            save_state(state_path, state)   # per company: an interruption keeps progress
    finally:
        browser.close()
        save_state(state_path, state)
