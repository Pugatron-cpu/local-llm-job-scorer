"""
Tests for c_prepare.close_silent (the `--close-silent` pass that settles unanswered applications).

Same bootstrap as test_clear_stale.py: config paths point at a temp dir, rows are seeded with
dates relative to today, and we assert what the pass writes (or, in dry-run, doesn't).
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

import config  # noqa: E402
import c_prepare  # noqa: E402


def _days_ago(n):
    return (datetime.now().date() - timedelta(days=n)).strftime("%Y-%m-%d")


def _write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def _read_tracker(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {r["url"]: r for r in csv.DictReader(f)}


class CloseSilentTests(unittest.TestCase):
    def setUp(self):
        self._orig = (config.APPLICATIONS_DIR, config.TRACKER_CSV)
        self._dir = tempfile.mkdtemp()
        config.APPLICATIONS_DIR = self._dir
        config.TRACKER_CSV = os.path.join(self._dir, "applications.csv")
        _write_csv(config.TRACKER_CSV, c_prepare.TRACKER_FIELDS, [
            # 60 days since added, no history, no deadline -> closed
            {"date_added": _days_ago(60), "status": "applied", "company": "Old Co",
             "role": "Student Data", "url": "u-old"},
            # added long ago but the history says it was APPLIED 10 days ago -> open
            {"date_added": _days_ago(60), "status": "applied", "company": "Late Co",
             "role": "Student AI", "url": "u-late"},
            # applied 50 days ago but the deadline was 10 days ago -> still inside 28d -> open
            {"date_added": _days_ago(50), "status": "applied", "company": "Deadline Co",
             "role": "Graduate", "url": "u-deadline", "deadline": _days_ago(10)},
            # junk deadline is treated as none -> closed on the 42-day rule alone
            {"date_added": _days_ago(50), "status": "applied", "company": "Junk Co",
             "role": "Student IT", "url": "u-junk", "deadline": "True"},
            {"date_added": "not a date", "status": "applied", "company": "Mystery Co",
             "role": "Student Ops", "url": "u-unknown"},
            # only `applied` is touched
            {"date_added": _days_ago(90), "status": "rejected", "company": "Rejected Co",
             "role": "Student BI", "url": "u-rejected"},
            {"date_added": _days_ago(90), "status": "interested", "company": "Queued Co",
             "role": "Student Dev", "url": "u-queued"},
        ])
        _write_csv(c_prepare._status_history_path(), c_prepare.STATUS_HISTORY_FIELDS, [
            {"date": _days_ago(10), "url": "u-late", "old_status": "interested",
             "new_status": "applied"},
        ])

    def tearDown(self):
        config.APPLICATIONS_DIR, config.TRACKER_CSV = self._orig
        shutil.rmtree(self._dir)

    def test_dry_run_writes_nothing(self):
        with open(config.TRACKER_CSV, encoding="utf-8") as f:
            before = f.read()
        self.assertEqual(c_prepare.close_silent(apply=False), 0)
        with open(config.TRACKER_CSV, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_apply_closes_only_silent_applications(self):
        self.assertEqual(c_prepare.close_silent(apply=True), 2)
        rows = _read_tracker(config.TRACKER_CSV)
        for url in ("u-old", "u-junk"):
            self.assertEqual(rows[url]["status"], "no_response", url)
            self.assertIn("auto-closed", rows[url]["notes"])
        for url, status in [("u-late", "applied"), ("u-deadline", "applied"),
                            ("u-unknown", "applied"), ("u-rejected", "rejected"),
                            ("u-queued", "interested")]:
            self.assertEqual(rows[url]["status"], status, url)

    def test_apply_logs_transitions(self):
        c_prepare.close_silent(apply=True)
        with open(c_prepare._status_history_path(), newline="", encoding="utf-8") as f:
            closed = [h for h in csv.DictReader(f) if h["new_status"] == "no_response"]
        self.assertEqual(sorted(h["url"] for h in closed), ["u-junk", "u-old"])
        self.assertTrue(all(h["old_status"] == "applied" for h in closed))

    def test_no_response_counts_as_sent(self):
        import b_insights
        self.assertIn("no_response", b_insights.APPLIED_PLUS)
        self.assertNotIn("no_response", b_insights.ACTIVE)


if __name__ == "__main__":
    unittest.main()
