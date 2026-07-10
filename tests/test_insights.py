"""
Tests for b_insights.py aggregation helpers + the status-transition logging that feeds them.

Pure functions over row lists (no files) for the analytics; a temp tracker/history dir for the
c_prepare logging. Same self-contained profile bootstrap as the other test modules.
"""

import os
import sys
import csv
import tempfile
import shutil
import unittest
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TEST_PROFILE = os.path.join(ROOT, "profiles", "_test.toml")
if not os.path.exists(_TEST_PROFILE):
    with open(_TEST_PROFILE, "w", encoding="utf-8") as _f:
        _f.write('candidate_profile = "test candidate"\nlocation_anchor = "test anchor"\n')
os.environ.setdefault("JOBSEARCH_OWNER", "_test")

import config      # noqa: E402
import b_insights  # noqa: E402
import c_prepare   # noqa: E402


class FunnelAggregations(unittest.TestCase):
    TRK = [
        {"status": "skipped",   "score": "90"},
        {"status": "skipped",   "score": "88"},
        {"status": "interested", "score": "80", "next_followup": "2026-06-01", "url": "u9",
         "company": "C", "role": "r"},
        {"status": "applied",   "score": "70", "next_followup": "2026-06-01", "url": "u1",
         "company": "C", "role": "r"},
        {"status": "applied",   "score": "72"},
        {"status": "rejected",  "score": "60"},
        {"status": "interview", "score": "95"},
    ]

    def test_funnel_counts_and_rates(self):
        f = b_insights.funnel(self.TRK)
        self.assertEqual(f["logged"], 7)
        self.assertEqual(f["applied"], 4)      # 2 applied + 1 rejected + 1 interview
        self.assertEqual(f["interview"], 1)
        self.assertEqual(f["offer"], 0)
        self.assertAlmostEqual(f["apply_rate"], 4 / 7)
        self.assertAlmostEqual(f["interview_rate"], 1 / 4)
        self.assertEqual(f["offer_rate"], 0.0)  # 0 offers / 1 interview (denominator > 0)

    def test_offer_rate_is_none_when_no_interviews(self):
        f = b_insights.funnel([{"status": "applied", "score": "70"}])
        self.assertIsNone(f["offer_rate"])      # 0 interviews -> guarded to None, not 0/0

    def test_score_by_status(self):
        sbs = b_insights.score_by_status(self.TRK)
        self.assertEqual(sbs["skipped"][0], 2)
        self.assertAlmostEqual(sbs["skipped"][1], 89.0)
        self.assertAlmostEqual(sbs["applied"][1], 71.0)

    def test_skip_vs_apply_by_band(self):
        band = b_insights.skip_vs_apply_by_band(self.TRK)
        # 90 band: interview(95) applied + skipped(90) triaged ; 70 band: 2 applied (70,72)
        self.assertEqual(band[90], (1, 1))
        self.assertEqual(band[70][0], 2)

    def test_overdue_followups_active_and_past_only(self):
        od = b_insights.overdue_followups(self.TRK, date(2026, 7, 1))
        got = [r.get("url") for _, r in od]
        # u1 (applied, 06-01 past) and u9 (interested, 06-01 past) are active+overdue
        self.assertCountEqual(got, ["u1", "u9"])


class MarketAggregations(unittest.TestCase):
    ARC = [
        {"score": "90", "danish_level": "required", "matched_skills": "Python; SQL", "source": "full"},
        {"score": "40", "danish_level": "none", "matched_skills": "python; docker", "source": "full"},
        {"score": "10", "danish_level": "required", "matched_skills": "", "source": "snippet"},
        {"score": "80", "danish_level": "preferred", "matched_skills": "Python, Azure", "source": "full"},
    ]

    def test_score_histogram(self):
        h = b_insights.score_histogram(self.ARC)
        self.assertEqual(h.get(90), 1)
        self.assertEqual(h.get(10), 1)

    def test_skill_frequency_case_and_split(self):
        top = dict(b_insights.skill_frequency(self.ARC))
        self.assertEqual(top["python"], 3)     # case-folded across ; and , separators
        self.assertEqual(top["sql"], 1)

    def test_skill_frequency_min_score_gate(self):
        top = dict(b_insights.skill_frequency(self.ARC, min_score=75))
        self.assertEqual(top.get("python"), 2)  # only the 90 and 80 rows
        self.assertNotIn("docker", top)          # docker only on the 40 row

    def test_completeness_split(self):
        cs = b_insights.completeness_split(self.ARC, threshold=75)
        self.assertEqual(cs["full"], (3, 2))     # 3 full, 2 of them >=75
        self.assertEqual(cs["snippet"], (1, 0))


class ResponseTimes(unittest.TestCase):
    HIST = [
        {"date": "2026-06-01", "url": "u1", "old_status": "interested", "new_status": "applied"},
        {"date": "2026-06-11", "url": "u1", "old_status": "applied", "new_status": "interview"},
        {"date": "2026-06-02", "url": "u2", "old_status": "interested", "new_status": "applied"},
    ]

    def test_response_days_and_types(self):
        days, by_type = b_insights.response_times(self.HIST)
        self.assertEqual(days, [10])             # u1 applied 06-01 -> interview 06-11; u2 no response
        self.assertEqual(by_type["applied→interview"], 1)
        self.assertEqual(by_type["interested→applied"], 2)

    def test_empty_history(self):
        self.assertEqual(b_insights.response_times([]), ([], __import__("collections").Counter()))


def _has_sklearn():
    try:
        import sklearn  # noqa: F401
        return True
    except ImportError:
        return False


class ReasonClustering(unittest.TestCase):
    def test_too_few_rows_returns_empty(self):
        self.assertEqual(b_insights.cluster_reasons([{"reasoning": "x"}]), [])

    @unittest.skipUnless(_has_sklearn(), "scikit-learn not installed")
    def test_separates_two_obvious_themes_deterministically(self):
        arc = ([{"reasoning": "strong python data engineering technical fit for the candidate",
                 "score": "80", "title": f"Data Eng {i}"} for i in range(10)] +
               [{"reasoning": "sales and marketing role explicitly excluded non technical",
                 "score": "0", "title": f"Sales {i}"} for i in range(10)])
        a = b_insights.cluster_reasons(arc, k=2)
        b = b_insights.cluster_reasons(arc, k=2)
        self.assertEqual([c["size"] for c in a], [c["size"] for c in b])   # deterministic
        self.assertEqual(sum(c["size"] for c in a), 20)                    # every row assigned
        allterms = " ".join(t for c in a for t in c["terms"])
        self.assertIn("python", allterms)
        self.assertIn("sales", allterms)


class StatusHistoryLogging(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._oapp, self._otrk = config.APPLICATIONS_DIR, config.TRACKER_CSV
        config.APPLICATIONS_DIR = self._dir
        config.TRACKER_CSV = os.path.join(self._dir, "applications.csv")
        with open(config.TRACKER_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=c_prepare.TRACKER_FIELDS)
            w.writeheader()
            w.writerow({"date_added": "2026-07-01", "status": "interested",
                        "company": "C", "role": "r", "url": "https://x.io/j/1"})

    def tearDown(self):
        config.APPLICATIONS_DIR, config.TRACKER_CSV = self._oapp, self._otrk
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_transition_is_logged_append_only(self):
        self.assertTrue(c_prepare._update_status("https://x.io/j/1", "applied"))
        self.assertTrue(c_prepare._update_status("https://x.io/j/1", "interview"))
        with open(c_prepare._status_history_path(), encoding="utf-8") as f:
            hist = list(csv.DictReader(f))
        self.assertEqual([(h["old_status"], h["new_status"]) for h in hist],
                         [("interested", "applied"), ("applied", "interview")])

    def test_no_op_change_is_not_logged(self):
        # setting the same status again must NOT append a transition
        c_prepare._update_status("https://x.io/j/1", "applied")
        c_prepare._update_status("https://x.io/j/1", "applied")
        with open(c_prepare._status_history_path(), encoding="utf-8") as f:
            hist = list(csv.DictReader(f))
        self.assertEqual(len(hist), 1)


if __name__ == "__main__":
    unittest.main()
