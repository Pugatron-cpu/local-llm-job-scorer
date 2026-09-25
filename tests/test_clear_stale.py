"""
Tests for c_prepare.clear_stale (the `--clear-stale` queue-ageing pass).

Pure logic only: config.APPLICATIONS_DIR / TRACKER_CSV point at a temp dir, rows are seeded
with dates relative to today, and we assert what the sweep writes (or, in dry-run, doesn't).
Same self-contained bootstrap as test_prepare_dup.py.
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


def _write_tracker(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=c_prepare.TRACKER_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in c_prepare.TRACKER_FIELDS})


def _read_tracker(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {r["url"]: r for r in csv.DictReader(f)}


class ClearStaleTests(unittest.TestCase):
    def setUp(self):
        self._orig = (config.APPLICATIONS_DIR, config.TRACKER_CSV)
        self._dir = tempfile.mkdtemp()
        config.APPLICATIONS_DIR = self._dir
        config.TRACKER_CSV = os.path.join(self._dir, "applications.csv")
        _write_tracker(config.TRACKER_CSV, [
            {"date_added": _days_ago(40), "status": "interested", "company": "Old Co",
             "role": "Student Data", "url": "u-old"},
            {"date_added": _days_ago(3), "status": "interested", "company": "Fresh Co",
             "role": "Student AI", "url": "u-fresh"},
            {"date_added": _days_ago(3), "status": "interested", "company": "Closed Co",
             "role": "Student IT", "url": "u-closed", "deadline": _days_ago(1)},
            {"date_added": _days_ago(3), "status": "interested", "company": "Junk Co",
             "role": "Student Dev", "url": "u-junk", "deadline": "True"},
            {"date_added": "not a date", "status": "interested", "company": "Mystery Co",
             "role": "Student Ops", "url": "u-unknown"},
            {"date_added": _days_ago(90), "status": "applied", "company": "Applied Co",
             "role": "Student BI", "url": "u-applied"},
        ])

    def tearDown(self):
        config.APPLICATIONS_DIR, config.TRACKER_CSV = self._orig
        shutil.rmtree(self._dir)

    def test_dry_run_writes_nothing(self):
        before = open(config.TRACKER_CSV, encoding="utf-8").read()
        self.assertEqual(c_prepare.clear_stale(30, apply=False), 0)
        self.assertEqual(open(config.TRACKER_CSV, encoding="utf-8").read(), before)
        self.assertFalse(os.path.exists(c_prepare._status_history_path()))

    def test_apply_skips_old_and_past_deadline_only(self):
        self.assertEqual(c_prepare.clear_stale(30, apply=True), 2)
        rows = _read_tracker(config.TRACKER_CSV)
        self.assertEqual(rows["u-old"]["status"], "skipped")
        self.assertEqual(rows["u-closed"]["status"], "skipped")
        self.assertIn("auto-skipped", rows["u-old"]["notes"])
        # fresh, junk deadline, unreadable date, and already-settled rows are untouched
        for url, status in [("u-fresh", "interested"), ("u-junk", "interested"),
                            ("u-unknown", "interested"), ("u-applied", "applied")]:
            self.assertEqual(rows[url]["status"], status, url)

    def test_apply_logs_one_history_row_per_transition(self):
        c_prepare.clear_stale(30, apply=True)
        with open(c_prepare._status_history_path(), newline="", encoding="utf-8") as f:
            hist = list(csv.DictReader(f))
        self.assertEqual(sorted(h["url"] for h in hist), ["u-closed", "u-old"])
        self.assertTrue(all(h["old_status"] == "interested" and h["new_status"] == "skipped"
                            for h in hist))

    def test_default_window_comes_from_config(self):
        self.assertEqual(c_prepare.STALE_AFTER_DAYS, config.STALE_AFTER_DAYS)


if __name__ == "__main__":
    unittest.main()
