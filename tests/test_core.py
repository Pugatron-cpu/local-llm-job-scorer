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


class StatusTracker(unittest.TestCase):
    """The read-only tracker view (e_status.py): which roles are overdue, which have a live
    deadline, and how the funnel counts. Pure selection logic, no file/tty."""
    from datetime import date
    TODAY = date(2026, 7, 5)

    def _rows(self):
        # status, next_followup, deadline
        return [
            {"company": "A", "role": "r", "status": "applied",    "next_followup": "2026-06-28", "deadline": ""},
            {"company": "B", "role": "r", "status": "interested", "next_followup": "2026-07-10", "deadline": "2026-07-08"},
            {"company": "C", "role": "r", "status": "interview",  "next_followup": "",           "deadline": ""},
            {"company": "D", "role": "r", "status": "rejected",   "next_followup": "2026-06-01", "deadline": ""},
            {"company": "E", "role": "r", "status": "skipped",    "next_followup": "2026-06-01", "deadline": "2026-07-20"},
            {"company": "F", "role": "r", "status": "interested", "next_followup": "2026-06-20", "deadline": "2026-06-30"},
            {"company": "G", "role": "r", "status": "offer",      "next_followup": "",           "deadline": "2026-07-09"},
        ]

    def test_overdue_only_active_and_past(self):
        import e_status
        got = [r["company"] for _, r in e_status.overdue_followups(self._rows(), self.TODAY)]
        # A (applied, 06-28) and F (interested, 06-20) are past; D/E are terminal, B is future.
        self.assertEqual(got, ["F", "A"])            # most overdue first

    def test_followups_unset_active_only(self):
        import e_status
        got = [r["company"] for r in e_status.followups_unset(self._rows())]
        self.assertEqual(got, ["C"])                 # active + blank; offer G is not "active"

    def test_upcoming_deadlines_open_future_sorted(self):
        import e_status
        got = [r["company"] for _, r in e_status.upcoming_deadlines(self._rows(), self.TODAY)]
        # B (07-08) future+interested; E terminal, F past, G already an offer -> excluded.
        self.assertEqual(got, ["B"])

    def test_missed_deadlines_interested_and_past(self):
        import e_status
        got = [r["company"] for _, r in e_status.missed_deadlines(self._rows(), self.TODAY)]
        self.assertEqual(got, ["F"])                 # interested + deadline already gone

    def test_funnel_counts(self):
        import e_status
        c = e_status.funnel_counts(self._rows())
        self.assertEqual(c["interested"], 2)
        self.assertEqual(c["applied"], 1)
        self.assertEqual(c["offer"], 1)
        self.assertEqual(c["skipped"], 1)

    def test_interview_outcome_statuses_are_terminal(self):
        import e_status
        # hired and rejected_after_interview are outcomes, not live -> no follow-up chasing,
        # excluded from upcoming deadlines, and counted as "reached interview".
        rows = [
            {"company": "H", "role": "r", "status": "hired",
             "next_followup": "2026-06-01", "deadline": "2026-07-20"},
            {"company": "R", "role": "r", "status": "rejected_after_interview",
             "next_followup": "2026-06-01", "deadline": "2026-07-20"},
        ]
        self.assertEqual(e_status.overdue_followups(rows, self.TODAY), [])   # not ACTIVE
        self.assertEqual(e_status.upcoming_deadlines(rows, self.TODAY), [])  # not ACTIVE
        self.assertIn("hired", e_status.FUNNEL)                     # success endpoint of the funnel
        self.assertIn("rejected_after_interview", e_status.TERMINAL)
        self.assertIn("hired", e_status.REACHED_INTERVIEW)
        self.assertIn("rejected_after_interview", e_status.REACHED_INTERVIEW)

    def test_date_parses_and_tolerates_junk(self):
        import e_status
        self.assertEqual(e_status._date("2026-07-05"), self.TODAY)
        self.assertIsNone(e_status._date(""))
        self.assertIsNone(e_status._date("N/A"))
        self.assertIsNone(e_status._date("not-a-date"))


class AtsWatchlist(unittest.TestCase):
    """The ATS source (Greenhouse/Lever): entry parsing, location gate, and the shared teaser
    builder. Pure logic — the HTTP fetch itself is not unit-tested (it's I/O)."""
    from datetime import date
    CUTOFF = date(2026, 6, 1)

    def test_parse_entry(self):
        self.assertEqual(core._parse_ats_entry("greenhouse:trustpilot"),
                         ("greenhouse", "trustpilot", "Trustpilot"))
        self.assertEqual(core._parse_ats_entry("lever:my-co|My Co A/S"),
                         ("lever", "my-co", "My Co A/S"))
        self.assertEqual(core._parse_ats_entry("greenhouse:some-corp")[2], "Some Corp")

    def test_location_filter(self):
        old = core.ATS_LOCATION_KEEP
        core.ATS_LOCATION_KEEP = ["denmark", "remote"]
        try:
            self.assertTrue(core._ats_location_ok("Copenhagen, Denmark"))
            self.assertTrue(core._ats_location_ok("Remote; Poland"))
            self.assertFalse(core._ats_location_ok("Berlin, Germany"))
            core.ATS_LOCATION_KEEP = []
            self.assertTrue(core._ats_location_ok("Anywhere"))   # empty keep-list = keep all
        finally:
            core.ATS_LOCATION_KEEP = old

    def test_teaser_build_marks_full_body(self):
        old = core.ATS_LOCATION_KEEP
        core.ATS_LOCATION_KEEP = ["denmark"]
        try:
            body = "x" * 250
            t = core._ats_teaser(title="Data Student", company="Acme",
                                 location="Copenhagen, Denmark", url="https://x/1",
                                 body=body, published="2026-07-01", cutoff_date=self.CUTOFF)
            self.assertEqual(t["source_site"], "ats")
            self.assertEqual(t["source"], "full")       # substantial body -> skip fetch stage
            self.assertEqual(t["_description"], body)
        finally:
            core.ATS_LOCATION_KEEP = old

    def test_teaser_drops_stale_offlocation_and_urlless(self):
        old = core.ATS_LOCATION_KEEP
        core.ATS_LOCATION_KEEP = ["denmark"]
        try:
            base = dict(title="T", company="A", url="https://x/1", body="b",
                        cutoff_date=self.CUTOFF)
            self.assertIsNone(core._ats_teaser(location="Copenhagen, Denmark",
                              published="2026-05-01", **base))               # stale
            self.assertIsNone(core._ats_teaser(location="Berlin, Germany",
                              published="2026-07-01", **base))               # off-location
            self.assertIsNone(core._ats_teaser(title="T", company="A", url="", body="b",
                              location="Copenhagen, Denmark", published="2026-07-01",
                              cutoff_date=self.CUTOFF))                       # no url
        finally:
            core.ATS_LOCATION_KEEP = old

    def test_teaser_excludes_company(self):
        old_loc, old_exc = core.ATS_LOCATION_KEEP, core.EXCLUDED_COMPANIES
        core.ATS_LOCATION_KEEP, core.EXCLUDED_COMPANIES = ["denmark"], ["kommune"]
        try:
            self.assertIsNone(core._ats_teaser(title="T", company="Aarhus Kommune",
                              location="Aarhus, Denmark", url="https://x/1", body="b",
                              published="2026-07-01", cutoff_date=self.CUTOFF))
        finally:
            core.ATS_LOCATION_KEEP, core.EXCLUDED_COMPANIES = old_loc, old_exc


class TrackerScoreBackfill(unittest.TestCase):
    """c_prepare._fill_blanks: the safety core of --score-tracker. Must fill blanks only,
    treat score '0' as blank, and never overwrite an existing value."""

    def test_fills_only_blanks(self):
        import c_prepare
        row = {"score": "", "track": "", "employment_type": "", "status": "applied"}
        changed = c_prepare._fill_blanks(row, {"score": "88", "track": "A",
                                               "employment_type": "student"})
        self.assertTrue(changed)
        self.assertEqual(row["score"], "88")
        self.assertEqual(row["track"], "A")
        self.assertEqual(row["status"], "applied")          # untouched

    def test_never_overwrites_existing(self):
        import c_prepare
        row = {"score": "95", "track": "A", "employment_type": "student"}
        changed = c_prepare._fill_blanks(row, {"score": "10", "track": "B",
                                               "employment_type": "full_time"})
        self.assertFalse(changed)
        self.assertEqual(row["score"], "95")                # kept
        self.assertEqual(row["track"], "A")

    def test_score_zero_counts_as_blank(self):
        import c_prepare
        row = {"score": "0", "track": "A"}
        c_prepare._fill_blanks(row, {"score": "77", "track": "B"})
        self.assertEqual(row["score"], "77")                # '0' -> filled
        self.assertEqual(row["track"], "A")                 # 'A' not overwritten

    def test_ignores_empty_new_values(self):
        import c_prepare
        row = {"score": "", "track": ""}
        changed = c_prepare._fill_blanks(row, {"score": None, "track": ""})
        self.assertFalse(changed)
        self.assertEqual(row["score"], "")


if __name__ == "__main__":
    unittest.main()
