"""
Regression tests for the tracker duplicate-detection in c_prepare.py:
  - re-post of an already-applied role under a DIFFERENT url (cross-source role_key match), and
  - the same-employer "am I over-applying?" heads-up.

Pure logic only (no network / Ollama / brief writing): we point config.TRACKER_CSV at a temp
file, seed rows, and assert the matchers. Same self-contained bootstrap as test_core.py.
"""

import os
import sys
import csv
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TEST_PROFILE = os.path.join(ROOT, "profiles", "_test.toml")
if not os.path.exists(_TEST_PROFILE):
    with open(_TEST_PROFILE, "w", encoding="utf-8") as _f:
        _f.write('candidate_profile = "test candidate"\nlocation_anchor = "test anchor"\n')
os.environ.setdefault("JOBSEARCH_OWNER", "_test")

import config  # noqa: E402
import c_prepare  # noqa: E402


def _write_tracker(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=c_prepare.TRACKER_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in c_prepare.TRACKER_FIELDS})


class TrackerDupTests(unittest.TestCase):
    def setUp(self):
        self._orig_csv = config.TRACKER_CSV
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8")
        self._tmp.close()
        config.TRACKER_CSV = self._tmp.name
        # One tracked role: Monta / Student DevOps Engineer via a Jobindex link (utm params).
        _write_tracker(config.TRACKER_CSV, [
            {"date_added": "2026-06-18", "status": "applied", "company": "Monta ApS",
             "role": "Student DevOps Engineer",
             "url": "https://thehub.io/jobs/abc123?utm_source=jobindex&utm_medium=jobboard"},
            {"date_added": "2026-06-20", "status": "interview", "company": "Monta",
             "role": "Student Data Analyst",
             "url": "https://monta.com/careers/data-analyst"},
        ])

    def tearDown(self):
        config.TRACKER_CSV = self._orig_csv
        os.unlink(self._tmp.name)

    def test_same_url_with_different_tracking_params_is_url_hit(self):
        # The SAME posting arriving under a clean url (no utm) must match on canonical url.
        url_hits, key_hits = c_prepare._tracker_matches(
            "https://thehub.io/jobs/abc123", "Monta ApS", "Student DevOps Engineer")
        self.assertEqual(len(url_hits), 1)
        self.assertEqual(key_hits, [])

    def test_repost_under_different_url_is_key_hit(self):
        # A RE-POST: same company + same title-token-set, but a brand-new company-site url.
        # This is the gap the feature closes — url-only matching would call it NEW.
        url_hits, key_hits = c_prepare._tracker_matches(
            "https://monta.com/careers/student-devops-engineer",
            "Monta", "DevOps Student Engineer")   # legal suffix dropped, word order changed
        self.assertEqual(url_hits, [])
        self.assertEqual(len(key_hits), 1)
        self.assertEqual(key_hits[0]["role"], "Student DevOps Engineer")

    def test_different_role_same_company_is_not_a_dup(self):
        # A genuinely different role at Monta must NOT be flagged as a duplicate.
        url_hits, key_hits = c_prepare._tracker_matches(
            "https://monta.com/careers/backend-engineer", "Monta", "Backend Engineer")
        self.assertEqual(url_hits, [])
        self.assertEqual(key_hits, [])

    def test_company_rows_lists_others_and_excludes_current(self):
        # Prepping the DevOps role: the note should surface the OTHER Monta role (Data Analyst)
        # but not the role being prepped itself.
        others = c_prepare._tracker_company_rows(
            "Monta", exclude_urls=["https://thehub.io/jobs/abc123?utm_source=jobindex"])
        roles = {r["role"] for r in others}
        self.assertEqual(roles, {"Student Data Analyst"})

    def test_company_rows_empty_for_unknown_employer(self):
        self.assertEqual(c_prepare._tracker_company_rows("Some Other GmbH"), [])

    def test_rolekey_map_annotates_by_status(self):
        m = c_prepare._tracker_rolekey_map()
        rk = c_prepare.core.role_key({"company": "Monta", "title": "Student DevOps Engineer"})
        self.assertEqual(m.get(rk), "applied")

    def test_url_match_canonicalises(self):
        self.assertIsNotNone(c_prepare._tracker_url_match(
            "https://thehub.io/jobs/abc123?utm_source=whatever"))   # tracking params ignored
        self.assertIsNone(c_prepare._tracker_url_match("https://thehub.io/jobs/zzz999"))


class DupBriefSkip(unittest.TestCase):
    """Exact-URL duplicate: prepare() must skip entirely — no brief file, no tracker row — so the
    applications/*.md queue never accumulates duplicate clutter. Runs offline: the dup check is
    before any fetch, so prepare() returns before touching the network."""
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._oapp, self._otrk = config.APPLICATIONS_DIR, config.TRACKER_CSV
        config.APPLICATIONS_DIR = self._dir
        config.TRACKER_CSV = os.path.join(self._dir, "applications.csv")
        _write_tracker(config.TRACKER_CSV, [
            {"date_added": "2026-07-01", "status": "applied", "company": "C", "role": "r",
             "url": "https://x.io/jobs/1", "brief_file": "2026-07-01_C.md"},
        ])

    def tearDown(self):
        config.APPLICATIONS_DIR, config.TRACKER_CSV = self._oapp, self._otrk
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_exact_url_dup_writes_nothing(self):
        before = set(os.listdir(self._dir))
        result = c_prepare.prepare({"url": "https://x.io/jobs/1?utm_source=linkedin"})
        self.assertIsNone(result)                       # early-exit
        self.assertEqual(set(os.listdir(self._dir)), before)   # no new .md, tracker unchanged
        with open(config.TRACKER_CSV, encoding="utf-8") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 1)  # still one row


if __name__ == "__main__":
    unittest.main()
