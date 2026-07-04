"""
Regression tests for the pure logic in core.py — the pieces that decide identity, parsing,
freshness and de-dup, where a silent change would quietly corrupt the archive or the shortlist.

No network, no Ollama, no Playwright: every function under test is deterministic. Run with
either:
    python -m pytest tests/            # if pytest is installed
    python -m unittest discover -s tests

Importing core runs `from config import *`, which needs a profile. We create a throwaway
profiles/_test.toml (gitignored) and point JOBSEARCH_OWNER at it BEFORE importing, so the
tests are self-contained and never touch a real profile or any real data file.
"""

import os
import sys
import csv
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Minimal profile so config.py doesn't sys.exit at import for lack of an owner.
_TEST_PROFILE = os.path.join(ROOT, "profiles", "_test.toml")
if not os.path.exists(_TEST_PROFILE):
    with open(_TEST_PROFILE, "w", encoding="utf-8") as _f:
        _f.write('candidate_profile = "test candidate"\nlocation_anchor = "test anchor"\n')
os.environ.setdefault("JOBSEARCH_OWNER", "_test")

import core  # noqa: E402


class CanonicalUrl(unittest.TestCase):
    def test_strips_tracking_and_normalises(self):
        a = core.canonical_url("https://www.thehub.io/jobs/123?utm_source=jobindex&ref=x")
        b = core.canonical_url("http://thehub.io/jobs/123/")
        self.assertEqual(a, b)

    def test_keeps_functional_params(self):
        u1 = core.canonical_url("https://hr-manager.net/apply?ProjectId=7")
        u2 = core.canonical_url("https://hr-manager.net/apply?ProjectId=8")
        self.assertNotEqual(u1, u2)

    def test_non_url_passthrough(self):
        self.assertEqual(core.canonical_url("N/A"), "N/A")
        self.assertEqual(core.canonical_url(""), "")


class RoleKeyFingerprint(unittest.TestCase):
    def test_word_order_and_suffix_collapse(self):
        a = core.role_key({"company": "Monta ApS", "title": "Student Assistant, Data"})
        b = core.role_key({"company": "Monta", "title": "Data Student Assistant"})
        self.assertTrue(a)
        self.assertEqual(a, b)

    def test_geo_and_gender_tags_ignored(self):
        a = core.role_key({"company": "Netcompany Danmark", "title": "Backend Developer (m/f/d)"})
        b = core.role_key({"company": "Netcompany", "title": "Backend Developer"})
        self.assertEqual(a, b)

    def test_different_roles_stay_distinct(self):
        a = core.role_key({"company": "Netcompany", "title": "Data Analyst Student"})
        b = core.role_key({"company": "Netcompany", "title": "Data Engineer Student"})
        self.assertNotEqual(a, b)

    def test_blank_when_missing(self):
        self.assertEqual(core.role_key({"company": "", "title": "Data"}), "")
        self.assertEqual(core.role_key({"company": "Monta", "title": ""}), "")


class ParseScore(unittest.TestCase):
    def test_clean_json(self):
        raw = ('{"score": 82, "track": "A", "is_tech_company": true, '
               '"employment_type": "student", "work_mode": "hybrid", "commute_ok": true, '
               '"danish_level": "preferred", "reasoning": "good fit"}')
        p = core._parse_score(raw)
        self.assertEqual(p["score"], 82)
        self.assertEqual(p["track"], "A")
        self.assertEqual(p["danish_level"], "preferred")

    def test_empty_thought_block_prefix(self):
        # Gemma with think:False sometimes emits an empty block before the JSON.
        raw = '\n\n{"score": 40, "track": "none", "reasoning": "meh", "danish_level": "none"}'
        p = core._parse_score(raw)
        self.assertEqual(p["score"], 40)

    def test_regex_last_resort(self):
        raw = 'garbage "score": 71 , then "track": "B" trailing junk without closing brace'
        p = core._parse_score(raw)
        self.assertEqual(p["score"], 71)
        self.assertEqual(p["track"], "B")

    def test_unparseable_returns_none(self):
        self.assertIsNone(core._parse_score("no numbers here at all"))


class DanishLevelCoercion(unittest.TestCase):
    def test_enum(self):
        self.assertEqual(core._coerce_danish_level({"danish_level": "REQUIRED"}), "required")

    def test_legacy_boolean(self):
        self.assertEqual(core._coerce_danish_level({"danish_required": True}), "required")
        self.assertEqual(core._coerce_danish_level({"danish_required": False}), "none")

    def test_unknown_defaults_none(self):
        self.assertEqual(core._coerce_danish_level({"danish_level": "maybe"}), "none")


class DeadlinePassed(unittest.TestCase):
    def test_past_danish_numeric(self):
        self.assertTrue(core.deadline_passed("Ansøgningsfrist: 01-01-2020"))

    def test_future_not_passed(self):
        self.assertFalse(core.deadline_passed("Apply before 31-12-2099"))

    def test_no_deadline(self):
        self.assertFalse(core.deadline_passed("A normal ad body with no deadline stated."))


class RoleOpenStatus(unittest.TestCase):
    def test_past_deadline_closed(self):
        is_open, _ = core.role_open_status({"deadline": "2020-01-01"})
        self.assertFalse(is_open)

    def test_future_deadline_open(self):
        is_open, days = core.role_open_status({"deadline": "2099-12-31"})
        self.assertTrue(is_open)
        self.assertGreater(days, 0)

    def test_no_deadline_recent_open(self):
        from datetime import datetime, timedelta
        recent = (datetime.now().date() - timedelta(days=1)).isoformat()
        is_open, days = core.role_open_status({"scraped_date": recent})
        self.assertTrue(is_open)
        self.assertIsNone(days)

    def test_no_deadline_stale_closed(self):
        from datetime import datetime, timedelta
        old = (datetime.now().date() - timedelta(days=999)).isoformat()
        is_open, _ = core.role_open_status({"scraped_date": old})
        self.assertFalse(is_open)


class DedupArchive(unittest.TestCase):
    def _write(self, rows):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=core.ARCHIVE_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in core.ARCHIVE_FIELDS})
        return path

    def test_cross_source_same_ad_collapses(self):
        # Same role, two sources, two different URLs, slightly different company/title wording.
        rows = [
            {"url": "https://jobindex.dk/jobannonce/monta-1", "company": "Monta ApS",
             "title": "Student Assistant, Data", "score": "80", "scraped_date": "2026-07-01"},
            {"url": "https://thehub.io/jobs/monta-xyz", "company": "Monta",
             "title": "Data Student Assistant", "score": "82", "scraped_date": "2026-07-02"},
        ]
        path = self._write(rows)
        try:
            out = core._dedup_archive(path)
            self.assertEqual(len(out), 1)              # collapsed to one role
            self.assertEqual(out[0]["scraped_date"], "2026-07-02")  # latest-scored wins
        finally:
            os.remove(path)

    def test_distinct_roles_kept(self):
        rows = [
            {"url": "https://x/1", "company": "Netcompany", "title": "Data Analyst Student",
             "score": "80", "scraped_date": "2026-07-01"},
            {"url": "https://x/2", "company": "Netcompany", "title": "Data Engineer Student",
             "score": "80", "scraped_date": "2026-07-01"},
        ]
        path = self._write(rows)
        try:
            self.assertEqual(len(core._dedup_archive(path)), 2)
        finally:
            os.remove(path)


class ShortlistRejectReason(unittest.TestCase):
    def _row(self, **kw):
        from datetime import datetime
        base = {"score": "80", "employment_type": "student", "commute_ok": "true",
                "danish_level": "none", "ad_language": "en", "track": "A",
                "scraped_date": datetime.now().date().isoformat(), "deadline": ""}
        base.update(kw)
        return base

    def test_qualifying_row_passes(self):
        self.assertIsNone(core.shortlist_reject_reason(self._row()))

    def test_below_threshold(self):
        self.assertEqual(core.shortlist_reject_reason(self._row(score="50")),
                         "score below threshold")

    def test_type_not_targeted(self):
        self.assertEqual(core.shortlist_reject_reason(self._row(employment_type="full_time")),
                         "employment type not targeted")

    def test_track_b_below_its_bar(self):
        # Track B at 78 clears SCORE_THRESHOLD (75) but not TRACK_B_MIN_SCORE (80).
        reason = core.shortlist_reject_reason(self._row(score="78", track="B"))
        self.assertIn("track B", reason)

    def test_predicate_matches_shortlist_with_reasons(self):
        # The console predicate and the report's filter must agree on the same row.
        import tempfile, csv as _csv
        rows = [self._row(url="https://x/pass"),
                self._row(url="https://x/low", score="40"),
                self._row(url="https://x/ft", employment_type="full_time")]
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=core.ARCHIVE_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in core.ARCHIVE_FIELDS})
        try:
            kept, _ = core.shortlist_with_reasons(path)
            passing = [r for r in rows if core.shortlist_reject_reason(dict(r)) is None]
            self.assertEqual(len(kept), len(passing))
        finally:
            os.remove(path)


class MigrateCsv(unittest.TestCase):
    def test_realigns_changed_header(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        # Old schema: a subset of columns, in a different order.
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["title", "score", "url"])
            w.writerow(["Data Student", "80", "https://x/1"])
        try:
            migrated = core.migrate_csv_if_needed(path, core.ARCHIVE_FIELDS)
            self.assertTrue(migrated)
            with open(path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["title"], "Data Student")   # value preserved by name
            self.assertEqual(rows[0]["score"], "80")
            self.assertEqual(rows[0]["danish_level"], "")        # new column blank
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
