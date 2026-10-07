"""
Tests for the company watchlist: watchlist.py's pure helpers (location verdict, link
extraction, company matching) and core's watchlist view (which archive rows the Watchlist
section shows). No network: the live careers pages were verified by hand when the entries
were added (see the profile comments).
"""

import os
import sys
import csv
import shutil
import tempfile
import unittest
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

CONFIRM = ["østerbro", "vibenshuset", "lyngbyvej 2", "københavn ø"]
AREA = ["copenhagen", "københavn"]


class LocationStatus(unittest.TestCase):
    def st(self, text, **kw):
        return watchlist.location_status(text, CONFIRM, AREA, **kw)[0]

    def test_confirmed_area_elsewhere_unknown(self):
        self.assertEqual(self.st("Our office: Lyngbyvej 2, 2100 København Ø"), watchlist.CONFIRMED)
        self.assertEqual(self.st("This position is offered in Copenhagen."), watchlist.AREA)
        self.assertEqual(self.st("This position is offered in Madrid."), watchlist.ELSEWHERE)
        self.assertEqual(self.st(""), watchlist.UNKNOWN)   # unreadable: shown, never hidden

    def test_labelled_location_decides_alone(self):
        # BASE: company text mentions Copenhagen, but the field says Spain.
        text = "We are a Copenhagen company.\n\nApply\nLocation\n\nSpain\n\nDeadline"
        self.assertEqual(self.st(text), watchlist.ELSEWHERE)
        self.assertEqual(self.st("Søg stillingen\n\nLokation:\n\nKøbenhavn Ø\n\nFuldtid"),
                         watchlist.CONFIRMED)
        # Paychex/Emply: "Location of job:" is one label, not "Location" + value "of job:"
        self.assertEqual(watchlist.labelled_location("Location of job:\nAllerød (hybrid)"),
                         "Allerød (hybrid)")

    def test_company_boilerplate_is_not_a_location(self):
        self.assertEqual(self.st("Founded in Copenhagen in 2005, Dalux builds ..."),
                         watchlist.ELSEWHERE)
        self.assertEqual(self.st("quarterly gatherings at our Copenhagen HQ."),
                         watchlist.ELSEWHERE)
        # ...but "join our Copenhagen office" IS where the job is
        self.assertEqual(self.st("We need a Bid Manager to join our Copenhagen office."),
                         watchlist.AREA)

    def test_company_location_regex(self):
        # Dalux ends every ad with its office line; the company text mentions Copenhagen.
        leeds = "Founded in Copenhagen.\nDalux | Leeds\n10-12 East Parade"
        cph = "Some text.\nDalux | København Ø\nLyngbyvej 2"
        rx = r"^Dalux \| (.+)$"
        self.assertEqual(self.st(leeds, location_regex=rx), watchlist.ELSEWHERE)
        self.assertEqual(self.st(cph, location_regex=rx), watchlist.CONFIRMED)


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
    """core's shortlist logic for rows at a watched company."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig = (core.WATCHLIST, core.WATCH_STATE, core.WATCH_CONFIRM_TERMS,
                      core.WATCH_AREA_TERMS)
        core.WATCHLIST = [{"name": "Dalux", "aliases": ["dalux"], "url": "u", "link_regex": "x"}]
        core.WATCH_STATE = os.path.join(self._dir, "watchlist_postings.csv")
        core.WATCH_CONFIRM_TERMS, core.WATCH_AREA_TERMS = CONFIRM, AREA
        core._watch_cache["mtime"] = None
        self.today = datetime.now().date()
        t, old = self.today.isoformat(), (self.today - timedelta(days=3)).isoformat()
        watchlist.save_state(core.WATCH_STATE, {
            "https://d/1": {"url": "https://d/1", "company": "Dalux", "title": "Student Dev",
                            "location_status": "confirmed", "first_seen": t, "last_seen": t},
            "https://d/2": {"url": "https://d/2", "company": "Dalux", "title": "Gone",
                            "location_status": "confirmed", "first_seen": old, "last_seen": old},
            watchlist.check_key("Dalux"): {"url": watchlist.check_key("Dalux"),
                                           "company": "Dalux", "last_seen": t},
        })

    def tearDown(self):
        (core.WATCHLIST, core.WATCH_STATE, core.WATCH_CONFIRM_TERMS,
         core.WATCH_AREA_TERMS) = self._orig
        core._watch_cache["mtime"] = None
        shutil.rmtree(self._dir)

    def _row(self, **kw):
        base = {"score": "10", "employment_type": "full_time", "commute_ok": "true",
                "danish_level": "required", "ad_language": "da", "track": "none",
                "scraped_date": self.today.isoformat(), "deadline": "", "company": "Dalux",
                "title": "Role", "location": ""}
        base.update(kw)
        return base

    def test_listed_posting_shows_whatever_its_score_or_type(self):
        r = self._row(url="https://d/1")
        self.assertIsNone(core.shortlist_reject_reason(r))
        self.assertEqual(r["_watch"], "Dalux")
        self.assertTrue(r["_watch_new"])

    def test_delisted_posting_is_hidden(self):
        self.assertEqual(core.shortlist_reject_reason(self._row(url="https://d/2")),
                         "no longer listed on the careers page")

    def test_board_row_in_the_area_shows_flagged(self):
        r = self._row(url="https://jobindex/9", location="Copenhagen")
        self.assertIsNone(core.shortlist_reject_reason(r))
        self.assertEqual(r["_watch_loc"], watchlist.AREA)

    def test_board_row_takes_the_careers_page_verdict_for_the_same_title(self):
        # Jobindex copy of a careers-page posting: blank location, different url, same title.
        state = watchlist.load_state(core.WATCH_STATE)
        state["https://d/3"] = {"url": "https://d/3", "company": "Dalux", "title": "Sales, Leeds",
                                "location_status": "elsewhere",
                                "first_seen": self.today.isoformat(),
                                "last_seen": self.today.isoformat()}
        watchlist.save_state(core.WATCH_STATE, state)
        core._watch_cache["mtime"] = None
        self.assertEqual(core.shortlist_reject_reason(
            self._row(url="https://jobindex/7", title="Sales, Leeds")), "score below threshold")

    def test_board_row_elsewhere_falls_back_to_normal_filters(self):
        self.assertEqual(
            core.shortlist_reject_reason(self._row(url="https://jobindex/8", location="Aarhus")),
            "score below threshold")

    def test_other_companies_unaffected(self):
        self.assertEqual(core.shortlist_reject_reason(self._row(url="https://z/1",
                                                                company="Acme")),
                         "score below threshold")

    def test_watch_rows_sort_first(self):
        rows = [self._row(url="https://z/1", company="Acme", score="95", employment_type="student",
                          danish_level="none", ad_language="en", track="A"),
                self._row(url="https://d/1")]
        fd, path = tempfile.mkstemp(suffix=".csv", dir=self._dir)
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=core.ARCHIVE_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in core.ARCHIVE_FIELDS})
        orig = (core.EXCLUDE_DANISH_REQUIRED, core.EXCLUDE_DANISH_ADS)
        try:
            kept, _ = core.shortlist_with_reasons(path)
        finally:
            core.EXCLUDE_DANISH_REQUIRED, core.EXCLUDE_DANISH_ADS = orig
        self.assertEqual([r["url"] for r in kept], ["https://d/1", "https://z/1"])


if __name__ == "__main__":
    unittest.main()
