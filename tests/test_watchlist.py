"""
Tests for the company watchlist: watchlist.py's pure helpers (location verdict, link
extraction, company matching) and core's view of rows at watched companies. No network: the live careers pages were verified by hand when the entries
were added (see the profile comments).
"""

import os
import sys
import csv
import shutil
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TEST_PROFILE = os.path.join(ROOT, "profiles", "_test.toml")
if not os.path.exists(_TEST_PROFILE):
    with open(_TEST_PROFILE, "w", encoding="utf-8") as _f:
        _f.write('candidate_profile = "test candidate"\nlocation_anchor = "test anchor"\n')
os.environ.setdefault("JOBSEARCH_OWNER", "_test")

import core       # noqa: E402
import watchlist  # noqa: E402

class LocationStatus(unittest.TestCase):
    """Before scoring, a posting is skipped only when its location is confidently outside the
    commutable areas (config default here: the Copenhagen ring)."""

    def st(self, text, **kw):
        return watchlist.location_status(text, **kw)[0]

    def test_commutable_far_and_uncertain(self):
        self.assertEqual(self.st("Location: Copenhagen"), watchlist.OK)
        self.assertEqual(self.st("This position is based in Aarhus."), watchlist.ELSEWHERE)
        self.assertEqual(self.st("Offices in Copenhagen and Aarhus."), watchlist.OK)  # ambiguous
        self.assertEqual(self.st("This position is offered in Madrid."), watchlist.OK)  # not a
        # Danish city: scored, and the scorer's commute verdict decides in the report
        self.assertEqual(self.st(""), watchlist.OK)   # unreadable: scored, never dropped

    def test_source_location_wins(self):
        self.assertEqual(self.st("We are a Copenhagen company.", location="Aarhus"),
                         watchlist.ELSEWHERE)

    def test_labelled_location_decides_over_the_body(self):
        # BASE: company text mentions Copenhagen, but the field says Aarhus.
        text = "We are a Copenhagen company.\n\nApply\nLocation\n\nAarhus\n\nDeadline"
        self.assertEqual(self.st(text), watchlist.ELSEWHERE)
        # Paychex/Emply: "Location of job:" is one label, not "Location" + value "of job:"
        self.assertEqual(watchlist.labelled_location("Location of job:\nAllerød (hybrid)"),
                         "Allerød (hybrid)")

    def test_company_location_regex(self):
        rx = r"^Dalux \| (.+)$"
        self.assertEqual(self.st("Founded in Copenhagen.\nDalux | Aarhus", location_regex=rx),
                         watchlist.ELSEWHERE)
        self.assertEqual(self.st("Some text.\nDalux | København Ø", location_regex=rx),
                         watchlist.OK)


class TypedSources(unittest.TestCase):
    """The careers-site API adapters, against the response shapes recorded 2026-10-07."""

    class _Resp:
        def __init__(self, data):
            self._d = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._d

    def test_ms_date(self):
        self.assertEqual(watchlist.ms_date("/Date(1794783599000+0100)/"), "2026-11-15")
        self.assertEqual(watchlist.ms_date("/Date(-62135596800000)/"), "")   # 'not set'
        self.assertEqual(watchlist.ms_date(""), "")

    def test_structured_deadline_reaches_the_shared_parser(self):
        import extractors
        body = watchlist.with_deadline("Ansøgningsfrist: 18. oktober", "2026-10-19")
        self.assertEqual(extractors.extract_deadline(body), "2026-10-19")
        self.assertEqual(watchlist.with_deadline("text", ""), "text")

    def test_hrmanager_filters_a_shared_tenant_by_department(self):
        items = [{"Name": "Studentermedhjælper til SAP", "AdvertisementUrl": "https://h/1",
                  "Department": {"Id": 19779}, "PositionLocation": {"Name": "København"},
                  "ApplicationDue": "/Date(1792792799000+0200)/",
                  "Advertisements": [{"Content": "<p>Om jobbet</p>"}]},
                 {"Name": "Other agency", "AdvertisementUrl": "https://h/2",
                  "Department": {"Id": 1}, "Advertisements": []}]
        with unittest.mock.patch.object(watchlist.requests, "get",
                                        return_value=self._Resp({"Items": items})):
            out = watchlist._hrmanager_list({"customer": "x", "departments": [19779]}, 5)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["location"], "København")
        self.assertEqual(out[0]["deadline"], "2026-10-23")   # 23:59:59 +02:00
        self.assertEqual(out[0]["body"], "Om jobbet")

    def test_workday_pages_past_a_zero_total_on_later_pages(self):
        def page(n, total):
            return self._Resp({"total": total, "jobPostings": [
                {"title": f"t{n}{i}", "externalPath": f"/job/Copenhagen/x_{n}{i}"}
                for i in range(20 if n < 2 else 3)]})
        pages = [page(0, 43), page(1, 0), page(2, 0)]     # Workday sends total once
        with unittest.mock.patch.object(watchlist.requests, "post", side_effect=pages):
            out = watchlist._workday_list({"host": "h", "tenant": "t", "sites": ["S"]}, 5)
        self.assertEqual(len(out), 43)
        self.assertEqual(out[0]["url"], "https://h/S/job/Copenhagen/x_00")
        self.assertEqual(out[0]["_detail"], "https://h/wday/cxs/t/S/job/Copenhagen/x_00")

    def test_workday_detail_uses_end_date_not_start_date(self):
        info = {"jobPostingInfo": {"title": "Graduate", "jobDescription": "<p>Start 1. sep</p>",
                                   "location": "Copenhagen", "startDate": "2026-10-06",
                                   "endDate": "2026-10-19"}}
        with unittest.mock.patch.object(watchlist.requests, "get", return_value=self._Resp(info)):
            self.assertEqual(watchlist._workday_detail("u", 5),
                             ("Graduate", "Start 1. sep", "Copenhagen", "2026-10-19"))

    def test_read_posting_prefers_the_list_then_the_api(self):
        p = {"url": "u", "title": "T", "body": "B", "location": "L", "deadline": "2026-01-01"}
        self.assertEqual(watchlist.read_posting({}, p, None, 5), ("T", "B", "L", "2026-01-01"))
        entry = {"source": "smartrecruiters"}
        with unittest.mock.patch.dict(watchlist._DETAILS, {"smartrecruiters":
                                                           lambda u, t: ("", "Ad", "", "")}):
            self.assertEqual(watchlist.read_posting(entry, {"url": "u", "title": "T",
                                                            "location": "København",
                                                            "_detail": "d"}, None, 5),
                             ("T", "Ad", "København", ""))


class Events(unittest.TestCase):
    def test_event_titles(self):
        for t in ("Deloitte SAP Graduate Night CPH — November 3rd",
                  "Launch your career as a Business Tech Consultant - join our Discovery Day!",
                  "Mentor Programme 2027 | Deloitte's Consulting Practice | CPH",
                  "Step Inside Accenture: An Evening for IT Students",
                  "CV Workshop: Get Your CV Ready for Accenture's Graduate Hiring Round"):
            self.assertTrue(watchlist.is_event(t), t)
        for t in ("Event Manager til PwC", "Night shift operator", "AI & Data Engineer",
                  "Vagthavende til Trafikinformationen"):
            self.assertFalse(watchlist.is_event(t), t)

    def test_open_events(self):
        today = "2026-10-07"
        state = {
            watchlist.check_key("Deloitte"): {"company": "Deloitte", "last_seen": today},
            "a": {"url": "a", "company": "Deloitte", "kind": "event", "location_status": "ok",
                  "deadline": "2026-11-03", "last_seen": today},
            "b": {"url": "b", "company": "Deloitte", "kind": "event", "location_status": "ok",
                  "deadline": "", "last_seen": today},
            "c": {"url": "c", "company": "Deloitte", "kind": "event", "location_status": "ok",
                  "deadline": "2026-10-20", "last_seen": today},
            "gone": {"url": "gone", "company": "Deloitte", "kind": "event",
                     "location_status": "ok", "last_seen": "2026-10-01"},
            "past": {"url": "past", "company": "Deloitte", "kind": "event",
                     "location_status": "ok", "deadline": "2026-10-06", "last_seen": today},
            "aarhus": {"url": "aarhus", "company": "Deloitte", "kind": "event",
                       "location_status": "elsewhere", "last_seen": today},
            "job": {"url": "job", "company": "Deloitte", "kind": "", "location_status": "ok",
                    "last_seen": today},
        }
        self.assertEqual([e["url"] for e in watchlist.open_events(state, today)],
                         ["c", "a", "b"])


class ExtractLinks(unittest.TestCase):
    def test_template_and_dedup(self):
        src = 'a show-job/105&locale=x b show-job/105 c show-job/206'
        self.assertEqual(
            watchlist.extract_links(src, "https://x.hr-on.com/", r"show-job/(\d+)",
                                    "https://x.hr-on.com/show-job/{0}"),
            ["https://x.hr-on.com/show-job/105", "https://x.hr-on.com/show-job/206"])

    def test_relative_links_resolve(self):
        src = '<a href="/en-GB/job/a/b/c">x</a>'
        self.assertEqual(
            watchlist.extract_links(src, "https://r.example.app/",
                                    r"(?:https://r\.example\.app)?/[a-z]{2}-[A-Z]{2}/job/[a-z/]+"),
            ["https://r.example.app/en-GB/job/a/b/c"])


class CompanyMatch(unittest.TestCase):
    def test_word_bounded(self):
        self.assertTrue(watchlist.company_matches("Nets Denmark A/S", ["nets"]))
        self.assertFalse(watchlist.company_matches("Planets ApS", ["nets"]))
        self.assertTrue(watchlist.company_matches("EGN Danmark", ["egn"]))


class WatchView(unittest.TestCase):
    """core's shortlist logic for rows at a watched company: the normal relevance filters,
    plus 'still listed on the careers page' as the open test and a per-company Danish opt-out."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig = (core.WATCHLIST, core.WATCH_STATE)
        core.WATCHLIST = [{"name": "Dalux", "aliases": ["dalux"], "url": "u", "link_regex": "x"},
                          {"name": "Banedanmark", "aliases": ["banedanmark"], "danish_ok": True}]
        core.WATCH_STATE = os.path.join(self._dir, "watchlist_postings.csv")
        core._watch_cache["mtime"] = None
        self.today = datetime.now().date()
        t, old = self.today.isoformat(), (self.today - timedelta(days=90)).isoformat()
        watchlist.save_state(core.WATCH_STATE, {
            "https://d/1": {"url": "https://d/1", "company": "Dalux", "title": "Student Dev",
                            "location_status": "ok", "first_seen": old, "last_seen": t},
            "https://d/2": {"url": "https://d/2", "company": "Dalux", "title": "Gone",
                            "location_status": "ok", "first_seen": old, "last_seen": old},
            watchlist.check_key("Dalux"): {"url": watchlist.check_key("Dalux"),
                                           "company": "Dalux", "last_seen": t},
        })

    def tearDown(self):
        core.WATCHLIST, core.WATCH_STATE = self._orig
        core._watch_cache["mtime"] = None
        shutil.rmtree(self._dir)

    def _row(self, **kw):
        base = {"score": "85", "employment_type": "student", "commute_ok": "true",
                "danish_level": "none", "ad_language": "en", "track": "A",
                "scraped_date": self.today.isoformat(), "deadline": "", "company": "Dalux",
                "title": "Role", "location": ""}
        base.update(kw)
        return base

    def test_matching_listed_posting_shows_in_the_watch_section(self):
        r = self._row(url="https://d/1", scraped_date=(self.today - timedelta(days=90)).isoformat())
        self.assertIsNone(core.shortlist_reject_reason(r))   # old, but still listed: open
        self.assertEqual(r["_watch"], "Dalux")

    def test_watched_rows_pass_the_normal_filters(self):
        self.assertEqual(core.shortlist_reject_reason(self._row(url="https://d/1", score="40")),
                         "score below threshold")
        self.assertEqual(core.shortlist_reject_reason(
            self._row(url="https://d/1", employment_type="full_time")),
            "employment type not targeted")
        self.assertEqual(core.shortlist_reject_reason(
            self._row(url="https://jobindex/9", location="Aarhus")), "not commutable")

    def test_listed_posting_past_its_deadline_is_closed(self):
        past = (self.today - timedelta(days=1)).isoformat()
        self.assertEqual(core.shortlist_reject_reason(self._row(url="https://d/1", deadline=past)),
                         "closed / aged out")

    def test_delisted_posting_is_hidden(self):
        self.assertEqual(core.shortlist_reject_reason(self._row(url="https://d/2")),
                         "no longer listed on the careers page")

    def test_board_row_at_a_watched_company_is_tagged_and_ages_normally(self):
        r = self._row(url="https://jobindex/9", location="Copenhagen")
        self.assertIsNone(core.shortlist_reject_reason(r))
        self.assertEqual(r["_watch"], "Dalux")
        old = (self.today - timedelta(days=core.REPORT_FRESH_DAYS + 1)).isoformat()
        self.assertEqual(core.shortlist_reject_reason(
            self._row(url="https://jobindex/9", scraped_date=old)), "closed / aged out")

    def test_danish_ok_company_keeps_its_danish_ads(self):
        da = dict(danish_level="required", ad_language="da")
        self.assertIsNone(core.shortlist_reject_reason(
            self._row(url="https://bane/1", company="Banedanmark", **da)))
        self.assertEqual(core.shortlist_reject_reason(self._row(url="https://jobindex/9", **da)),
                         "danish required (hidden by filter)")

    def test_other_companies_unaffected(self):
        r = self._row(url="https://z/1", company="Acme")
        self.assertIsNone(core.shortlist_reject_reason(r))
        self.assertEqual(r["_watch"], "")

    def test_report_opens_with_closing_soon_and_events(self):
        soon = (self.today + timedelta(days=5)).isoformat()
        later = (self.today + timedelta(days=40)).isoformat()
        rows = [self._row(url="https://z/1", company="Acme", title="Student BI", deadline=soon),
                self._row(url="https://z/2", company="Acme", title="Student ML", deadline=later)]
        state = watchlist.load_state(core.WATCH_STATE)
        state["https://d/ev"] = {"url": "https://d/ev", "company": "Dalux", "kind": "event",
                                 "title": "Graduate Night", "location_status": "ok",
                                 "deadline": soon, "first_seen": self.today.isoformat(),
                                 "last_seen": self.today.isoformat()}
        watchlist.save_state(core.WATCH_STATE, state)
        core._watch_cache["mtime"] = None
        fd, path = tempfile.mkstemp(suffix=".csv", dir=self._dir)
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=core.ARCHIVE_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in core.ARCHIVE_FIELDS})
        report = os.path.join(self._dir, "r.md")
        core.write_report(report, path)
        text = open(report, encoding="utf-8").read()
        block = text.split("## ⏰ Closing within")[1].split("##")[0]
        self.assertIn("**#1** Acme: Student BI · 5d left", block)
        self.assertNotIn("Student ML", block)
        self.assertIn("**Dalux**: [Graduate Night](https://d/ev) · sign up by", text)


    def test_watch_rows_sort_first(self):
        rows = [self._row(url="https://z/1", company="Acme", score="95"),
                self._row(url="https://d/1", score="76")]
        fd, path = tempfile.mkstemp(suffix=".csv", dir=self._dir)
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=core.ARCHIVE_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in core.ARCHIVE_FIELDS})
        kept, _ = core.shortlist_with_reasons(path)
        self.assertEqual([r["url"] for r in kept], ["https://d/1", "https://z/1"])


if __name__ == "__main__":
    unittest.main()
