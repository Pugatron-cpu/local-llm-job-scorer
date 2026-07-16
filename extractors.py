"""
extractors.py — deterministic extraction of the MECHANICAL scoring fields.

Several fields the scorer emits are mechanical, not judgmental: whether an ad says
"studentermedhjælper", which city it names, whether a stated deadline parses. The LLM gets
those wrong occasionally (and differently per model); a keyword rule gets them right every
time or knows that it doesn't know. This module owns the mechanical fields so the LLM can be
left with only the fit judgment — and behaviour is identical whichever Ollama model is
configured, because none of this touches a model.

THE CONTRACT (every function in this file):
  - FILLS fields, never filters. Nothing here drops, gates, or hides a role.
  - Returns a CONFIDENT value or a SENTINEL ("" / "unknown" / None). When signals conflict
    or are absent it stays silent, and the LLM's value stands downstream. Never guess.
  - Pure: no network, no GPU, no I/O. Testable with plain strings.

Wiring (stage 2) merges these AFTER score_job returns: a confident value overwrites the
LLM's field; a sentinel leaves it alone. The scoring prompt itself is untouched, so scores
stay on the same scale as the 1500+ archived rows.
"""

import re
from datetime import datetime

import config   # only for defaults (COMMUTABLE_AREAS); every function also takes overrides

# ---------------------------------------------------------------------------
# EMPLOYMENT TYPE
# ---------------------------------------------------------------------------
# The same Danish-market vocabulary the scoring prompt enumerates (studenterjob /
# studentermedhjælper / student assistant; deltid; fuldtid; praktik / internship).
# Word-boundary regexes over title+description, lowercased. Danish compounds are matched by
# stem where safe (studentermedhjælperen, deltidsstilling, praktikplads).

_STUDENT_RE = re.compile(
    r"\b(?:studenterjob|studentermedhj[æa]lper\w*|studentermedarbejder\w*|studiejob"
    r"|studenterstilling\w*|student\s+assistant|student\s+worker|student\s+employee"
    r"|student\s+position)\b")
_PART_TIME_RE = re.compile(r"\b(?:deltid\w*|part[\s-]?time)\b")
_FULL_TIME_RE = re.compile(r"\b(?:fuldtid\w*|full[\s-]?time)\b")
# NOTE: bare "intern" is deliberately absent — in Danish it means "internal"
# ("intern kommunikation"), so it would misfire constantly on Danish ads.
_INTERNSHIP_RE = re.compile(r"\b(?:praktik\w*|internship\w*)\b")


def extract_employment_type(title: str, description: str) -> str:
    """"student" | "part_time" | "full_time" | "internship" | "unknown".

    One unambiguous signal -> that type. Conflicting signals -> "unknown" (the LLM's call
    stands), with ONE documented exception: student + part_time collapses to "student",
    because the taxonomy itself defines "part_time" as a NON-student part-time role — every
    studenterjob is part-time hours, so "studentermedhjælper, 15 timer/uge (deltid)" is not
    a conflict, it's a student job."""
    text = f"{title}\n{description}".lower()
    hits = {name for name, rx in (("student", _STUDENT_RE), ("part_time", _PART_TIME_RE),
                                  ("full_time", _FULL_TIME_RE), ("internship", _INTERNSHIP_RE))
            if rx.search(text)}
    if "student" in hits:
        hits.discard("part_time")          # a studenterjob IS part-time; not a conflict
    if len(hits) == 1:
        return hits.pop()
    return "unknown"                       # absent, or genuinely conflicting signals

# ---------------------------------------------------------------------------
# WORK MODE
# ---------------------------------------------------------------------------

_HYBRID_RE = re.compile(r"\bhybrid\w*\b")
_REMOTE_RE = re.compile(
    r"\b(?:fully\s+remote|100\s*%\s*remote|remote(?:-first)?|work\s+from\s+home"
    r"|hjemmefra|hjemmearbejde\w*)\b")
# "no remote work" / "ikke remote" / "remote work is not possible": an explicit statement
# that remote is NOT offered. Suppresses the remote signal (we do NOT flip it to "onsite" —
# that would be an inference, and the contract says never guess).
_REMOTE_NEG_RE = re.compile(
    r"\b(?:not?|ikke)\s+(?:an?\s+|en\s+|et\s+)?(?:fully\s+)?remote\b"
    r"|\bremote\s+(?:work\s+)?is\s+not\b")
_ONSITE_RE = re.compile(
    r"\bon[\s-]?site\b|\bp[åa]\s+kontoret\b|\bi\s+kontoret\b|\bfysisk\s+fremm[øo]de\b"
    r"|\bfremm[øo]de\b")


def extract_work_mode(description: str) -> str:
    """"onsite" | "hybrid" | "remote" | "unknown" — "unknown" unless the signal is clear.

    "hybrid" wins outright when stated: hybrid ads naturally mention both home and office,
    so remote/onsite words alongside it are descriptions of the hybrid split, not conflicts.
    Without it, remote-only -> "remote", onsite-only -> "onsite", both -> "unknown"."""
    text = (description or "").lower()
    if _HYBRID_RE.search(text):
        return "hybrid"
    remote = bool(_REMOTE_RE.search(text)) and not _REMOTE_NEG_RE.search(text)
    onsite = bool(_ONSITE_RE.search(text))
    if remote and not onsite:
        return "remote"
    if onsite and not remote:
        return "onsite"
    return "unknown"

# ---------------------------------------------------------------------------
# LOCATION + COMMUTE
# ---------------------------------------------------------------------------
# Danish city/area names, canonical display form -> lowercase variants matched in text.
# Recognition vocabulary (what a Danish location LOOKS like), not policy — which of these
# count as commutable is config.COMMUTABLE_AREAS, per profile.

_KNOWN_CITIES = {
    "Copenhagen":     ("copenhagen", "københavn", "kbh"),
    "Frederiksberg":  ("frederiksberg",),
    "Kongens Lyngby": ("kongens lyngby", "kgs. lyngby", "kgs lyngby", "lyngby"),
    "Glostrup":       ("glostrup",),
    "Ballerup":       ("ballerup",),
    "Hellerup":       ("hellerup",),
    "Roskilde":       ("roskilde",),
    "Ørestad":        ("ørestad", "orestad"),
    "Herlev":         ("herlev",),
    "Gentofte":       ("gentofte",),
    "Gladsaxe":       ("gladsaxe",),
    "Søborg":         ("søborg", "soborg"),
    "Valby":          ("valby",),
    "Brøndby":        ("brøndby", "brondby"),
    "Hvidovre":       ("hvidovre",),
    "Rødovre":        ("rødovre", "rodovre"),
    "Albertslund":    ("albertslund",),
    "Taastrup":       ("høje-taastrup", "høje taastrup", "taastrup"),
    "Ishøj":          ("ishøj", "ishoj"),
    "Kastrup":        ("kastrup",),
    "Hillerød":       ("hillerød", "hillerod"),
    "Helsingør":      ("helsingør", "helsingor"),
    "Birkerød":       ("birkerød", "birkerod"),
    "Farum":          ("farum",),
    "Køge":           ("køge", "koge"),
    "Næstved":        ("næstved", "naestved"),
    "Aarhus":         ("aarhus", "århus"),
    "Odense":         ("odense",),
    "Aalborg":        ("aalborg", "ålborg"),
    "Esbjerg":        ("esbjerg",),
    "Randers":        ("randers",),
    "Kolding":        ("kolding",),
    "Vejle":          ("vejle",),
    "Horsens":        ("horsens",),
    "Fredericia":     ("fredericia",),
    "Silkeborg":      ("silkeborg",),
    "Herning":        ("herning",),
    "Sønderborg":     ("sønderborg", "sonderborg"),
    "Viborg":         ("viborg",),
    "Billund":        ("billund",),
}

# variant -> canonical, longest variants first so "kongens lyngby" wins over "lyngby".
_CITY_VARIANTS = sorted(((v, canon) for canon, vs in _KNOWN_CITIES.items() for v in vs),
                        key=lambda x: -len(x[0]))
_CITY_RE = re.compile(
    r"(?<![a-zæøå])(" + "|".join(re.escape(v) for v, _ in _CITY_VARIANTS) + r")(?![a-zæøå])")
_VARIANT_TO_CANON = dict(_CITY_VARIANTS)


def _cities_in(text: str) -> list:
    """DISTINCT canonical city names found in the text, in order of first appearance."""
    seen, out = set(), []
    for m in _CITY_RE.finditer((text or "").lower()):
        canon = _VARIANT_TO_CANON[m.group(1)]
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def extract_location(job: dict, description: str) -> str:
    """The role's location as a string, or "" when not confident.

    Prefers the SOURCE-provided job["location"] (The Hub and ATS teasers carry a real one;
    Jobindex teasers say "N/A"). Falls back to scanning title+description for a known Danish
    city — but only when EXACTLY ONE distinct city appears; "offices in Copenhagen and
    Aarhus" is ambiguous, so it stays silent rather than pick one."""
    loc = str(job.get("location") or "").strip()
    if loc and loc.upper() != "N/A":
        return loc
    cities = _cities_in(f"{job.get('title') or ''}\n{description or ''}")
    if len(cities) == 1:
        return cities[0]
    return ""


def commute_ok(location: str, areas: set | None = None):
    """True / False / None — a pure lookup of the location string against COMMUTABLE_AREAS
    (config.py; per-profile via commutable_areas). This replaces asking the LLM to do Danish
    geography, which it gets wrong.

      True  : the location names a commutable area (or the role is remote).
      False : the location names a KNOWN Danish city that is not in the commutable set —
              "Aarhus" is confidently not a Copenhagen commute.
      None  : empty / "N/A" / unrecognised ("Greater Copenhagen Area" minus a known name) —
              not confident either way, the LLM's judgment stands."""
    areas = config.COMMUTABLE_AREAS if areas is None else areas
    loc = str(location or "").strip().lower()
    if not loc or loc == "n/a" or not areas:
        return None
    if any(a in loc for a in areas):
        return True
    if _cities_in(loc):                    # a known city, and none of it commutable
        return False
    return None

# ---------------------------------------------------------------------------
# DEADLINE
# ---------------------------------------------------------------------------
# _DEADLINE_RE and _MONTHS moved here VERBATIM from core.py, so the drop gate
# (core.deadline_passed) and the cosmetic field (extract_deadline) share ONE parser and can
# never disagree about what a deadline says. core imports parse_deadline from here.

_DEADLINE_RE = re.compile(
    r"(?:ans[øo]gningsfrist|frist|deadline|ans[øo]g\s+senest|s[øo]g\s+senest|senest\s+den"
    r"|apply\s+(?:by|before|no\s+later\s+than)|closing\s+date)"
    r"[:\s]*(?:den\s+)?"
    r"(\d{1,2})[.\s/-]\s*(\d{1,2}|\w+)[.\s/-]\s*(\d{2,4})",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    ["januar", "februar", "marts", "april", "maj", "juni", "juli", "august",
     "september", "oktober", "november", "december"], start=1)}


def parse_deadline(text: str):
    """The stated application deadline as a datetime.date, or None when nothing parses
    confidently. Exactly the parsing deadline_passed always did, returned instead of
    compared — so both consumers share it."""
    m = _DEADLINE_RE.search(text or "")
    if not m:
        return None
    day, mon, year = m.groups()
    try:
        day = int(day)
        month = int(mon) if mon.isdigit() else _MONTHS.get(mon.lower())
        if not month:
            return None
        year = int(year)
        if year < 100:
            year += 2000
        return datetime(year, month, day).date()
    except (ValueError, TypeError):
        return None


def extract_deadline(text: str) -> str:
    """"YYYY-MM-DD" when a deadline parses confidently, else "" (the LLM's value stands)."""
    d = parse_deadline(text)
    return d.isoformat() if d else ""

# ---------------------------------------------------------------------------
# MATCHED SKILLS
# ---------------------------------------------------------------------------


def _term_re(term: str):
    """Whole-word, case-insensitive matcher for one vocabulary term. Boundaries are
    'not adjacent to another letter/digit' rather than \\b, so terms ending in symbols
    ("c++", "c#") and multi-word terms ("power bi") both work, and "java" never matches
    inside "javascript"."""
    return re.compile(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])")


def extract_matched_skills(description: str, skills_vocab: list):
    """The skills from `skills_vocab` (the profile's skills_vocab list) literally present in
    the description — case-insensitive whole-word intersection, vocab order and casing kept.

    No vocab configured -> None (sentinel: the LLM's list stands). With a vocab, an empty
    result IS confident ("none of your skills appear") and overwrites downstream."""
    if not skills_vocab:
        return None
    text = (description or "").lower()
    out, seen = [], set()
    for term in skills_vocab:
        t = str(term).strip()
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        if _term_re(t).search(text):
            out.append(t)
    return out

# ---------------------------------------------------------------------------
# DANISH-LEVEL FLOOR
# ---------------------------------------------------------------------------
# A FLOOR, not a value: merged with the LLM's danish_level as max() on the
# none < preferred < required ordering (mirroring the existing ad_language->preferred lift
# in core's _score_worker). It can only RAISE the grade to "required", never lower it.

_DANISH_REQUIRED_PATTERNS = [re.compile(p) for p in (
    r"dansk\s+er\s+et\s+krav",
    r"dansk\s+er\s+p[åa]kr[æa]vet",
    r"kr[æa]ver\s+(?:flydende\s+)?dansk",
    r"flydende\s+(?:i\s+)?dansk",
    r"dansk\s+p[åa]\s+modersm[åa]lsniveau",
    r"dansktalende",
    r"danish\s+is\s+(?:a\s+)?require(?:d|ment)",
    r"danish\s+is\s+mandatory",
    r"requires?\s+(?:fluent\s+)?danish",
    r"must\s+(?:speak|be\s+fluent\s+in)\s+danish",
    r"fluen(?:t|cy)\s+(?:in\s+)?danish",
    r"danish[\s-]speaking\s+.{0,30}\brequired",
)]
# Softeners: the same phrase inside "flydende dansk er en fordel" / "fluent Danish is a
# plus" is NOT a requirement. Checked in a window around each match; any hit -> stay silent.
_DANISH_SOFTENERS = ("fordel", "et plus", "a plus", "advantage", "nice to have",
                     "nice-to-have", "bonus", "preferred but", "not required",
                     "not a requirement", "ikke et krav", "ikke n[øo]dvendig")
_DANISH_SOFTENER_RE = re.compile("|".join(_DANISH_SOFTENERS))


def danish_level_floor(description: str):
    """"required" when the ad EXPLICITLY says the role needs Danish ("dansk er et krav",
    "flydende dansk", "must speak Danish", ...), else None.

    Fires only on the enumerated phrases, and not when the surrounding sentence softens them
    ("flydende dansk er en fordel" is 'preferred', which is the LLM's call, not ours)."""
    text = (description or "").lower()
    for pat in _DANISH_REQUIRED_PATTERNS:
        for m in pat.finditer(text):
            ctx = text[max(0, m.start() - 80): m.end() + 80]
            if _DANISH_SOFTENER_RE.search(ctx):
                continue
            return "required"
    return None

# ---------------------------------------------------------------------------
# ANALYTICS-ONLY CAPTURES (stated salary / stated experience)
# ---------------------------------------------------------------------------
# CAPTURE-ONLY: these two feed raw analytics columns (raw_teasers.csv + the archive) and are
# NEVER read by scoring, filtering, or the shortlist. Raw matched string, no normalisation —
# "35.000 kr./md." and "DKK 35,000 per month" are kept exactly as the ad wrote them; parse
# at analysis time if you ever need numbers. Blank when absent.

_SALARY_RE = re.compile(
    # "DKK 35.000" / "kr. 160"            | "30.000-35.000 kr." / "160 DKK" / "35.000 kroner"
    r"(?:(?:dkk|kr\.?)\s*\d[\d.,]*|(?:\d[\d.,]*\s*[-–]\s*)?\d[\d.,]*\s*(?:dkk|kr(?:oner)?)\b\.?)"
    # optional period: "/md.", "pr. måned", "per month", "om måneden", "monthly", "/time"...
    # (the connector itself is optional: "DKK 38,000 monthly" states one without it)
    r"(?:\s*(?:/|pr\.?\s|per\s|om\s)?\s*(?:md\.?|mdr\.?|måned(?:en)?|month(?:ly)?|time(?:n)?"
    r"|hour|år(?:et)?|year|annum))?",
    re.IGNORECASE)

_EXPERIENCE_RE = re.compile(
    # "3 års erfaring" / "3-5 års erfaring" / "5+ years (of) experience" / "years' experience"
    r"\d+\s*(?:[-–]\s*\d+)?\s*\+?\s*(?:års?\s+erfaring|years?'?\s+(?:of\s+)?experience)",
    re.IGNORECASE)


def extract_stated_salary(text: str) -> str:
    """The first salary-looking kr/DKK amount stated in the text, as the RAW matched string
    ("35.000 kr./md."), or "" when absent. Analytics capture only — see the block comment."""
    m = _SALARY_RE.search(text or "")
    return m.group(0).strip() if m else ""


def extract_stated_experience(text: str) -> str:
    """The first stated years-of-experience phrase ("3 års erfaring", "5+ years of
    experience"), as the RAW matched string, or "" when absent. Analytics capture only."""
    m = _EXPERIENCE_RE.search(text or "")
    return m.group(0).strip() if m else ""
