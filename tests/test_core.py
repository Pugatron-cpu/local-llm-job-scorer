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

    def test_slashed_dotted_legal_forms_collapse(self):
        # 'A/S' tokenised to 'a'+'s' used to survive (neither fragment is noise), so a re-post
        # under the bare name never matched. Slash/dot legal forms must reduce like 'ApS' does.
        bare = core.role_key({"company": "Retriever", "title": "Student AI Engineer"})
        for variant in ("Retriever A/S", "Retriever Danmark A/S", "Retriever I/S",
                        "Retriever S.M.B.A."):
            k = core.role_key({"company": variant, "title": "Student AI Engineer"})
            self.assertTrue(k)
            self.assertEqual(k, bare, f"{variant!r} should collapse onto 'Retriever'")

    def test_trailing_letter_is_not_a_legal_suffix(self):
        # Guard the conservative contract: gluing punctuation must NOT drop meaningful single
        # letters, or 'Company A' and 'Company B' would false-merge.
        a = core.role_key({"company": "Company A", "title": "Data Student"})
        b = core.role_key({"company": "Company B", "title": "Data Student"})
        self.assertNotEqual(a, b)

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

    def test_graduate_programme_needs_the_profile_opt_in(self):
        grad = self._row(employment_type="full_time", title="Graduate Programme - Data 2027")
        orig = core.GRADUATE_PROGRAMMES
        try:
            core.GRADUATE_PROGRAMMES = False
            self.assertEqual(core.shortlist_reject_reason(dict(grad)),
                             "employment type not targeted")
            core.GRADUATE_PROGRAMMES = True
            r = dict(grad)
            self.assertIsNone(core.shortlist_reject_reason(r))
            self.assertTrue(r["_graduate"])
            # the opt-in is for graduate intakes only, not full-time roles in general
            self.assertEqual(core.shortlist_reject_reason(
                self._row(employment_type="full_time", title="Data Engineer")),
                "employment type not targeted")
            # and the other view filters still apply to graduate rows
            self.assertEqual(core.shortlist_reject_reason(dict(grad, score="50")),
                             "score below threshold")
        finally:
            core.GRADUATE_PROGRAMMES = orig

    def test_graduate_rows_sort_after_the_main_list(self):
        import tempfile, csv as _csv
        rows = [self._row(url="https://x/grad", employment_type="full_time", score="95",
                          title="Graduate - Data & Analytics"),
                self._row(url="https://x/student", score="76", title="Student Data")]
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=core.ARCHIVE_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in core.ARCHIVE_FIELDS})
        orig = core.GRADUATE_PROGRAMMES
        try:
            core.GRADUATE_PROGRAMMES = True
            kept, _ = core.shortlist_with_reasons(path)
            self.assertEqual([r["url"] for r in kept], ["https://x/student", "https://x/grad"])
        finally:
            core.GRADUATE_PROGRAMMES = orig
            os.remove(path)

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


class RawTeaserLog(unittest.TestCase):
    def test_appending_to_old_schema_migrates_first(self):
        """raw_teasers.csv written before the analytics columns existed must be realigned
        before an append, or every new row would be silently column-shifted."""
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        old_fields = [f for f in core.RAW_TEASER_FIELDS
                      if f not in ("stated_salary", "stated_experience_years")]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=old_fields)
            w.writeheader()
            w.writerow({k: "old" for k in old_fields})
        try:
            core._log_raw_teasers([{"title": "T", "url": "https://x/1",
                                    "stated_salary": "35.000 kr./md."}], path)
            with open(path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["title"], "old")                    # kept, by name
            self.assertEqual(rows[0]["stated_salary"], "")               # new column blank
            self.assertEqual(rows[1]["stated_salary"], "35.000 kr./md.")  # new row aligned
        finally:
            os.remove(path)

    def test_analytics_columns_present(self):
        self.assertIn("stated_salary", core.RAW_TEASER_FIELDS)
        self.assertIn("stated_experience_years", core.RAW_TEASER_FIELDS)
        self.assertIn("stated_salary", core.ARCHIVE_FIELDS)
        self.assertIn("stated_experience_years", core.ARCHIVE_FIELDS)


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


class ModelPresets(unittest.TestCase):
    """config.MODEL_PRESETS + selection: default 'fast' must reproduce today's behaviour
    exactly, the flag/env selection must be explicit, and the preflight must fail LOUDLY
    (naming every preset) rather than ever auto-switching models."""

    def test_both_presets_exist_with_model_and_workers(self):
        import config
        for name in ("fast", "fallback"):
            self.assertIn(name, config.MODEL_PRESETS)
            self.assertTrue(config.MODEL_PRESETS[name]["model"])
            self.assertGreaterEqual(config.MODEL_PRESETS[name]["score_workers"], 1)

    def test_presets_carry_endpoint_ctx_and_timeout(self):
        """Each preset is pinned to an endpoint (which GPU serves it), a context window and a
        per-request scoring timeout — not just a model + worker count."""
        import config
        for name in ("fast", "fallback"):
            spec = config.MODEL_PRESETS[name]
            self.assertTrue(spec["ollama_url"].startswith("http"))
            self.assertGreaterEqual(spec["num_ctx"], 1024)
            self.assertGreaterEqual(spec["score_timeout_s"], 1)
        # fallback = the separate A4000 instance (:11436), smaller ctx than the 3090 pool.
        self.assertIn(":11434", config.MODEL_PRESETS["fast"]["ollama_url"])
        self.assertIn(":11436", config.MODEL_PRESETS["fallback"]["ollama_url"])
        self.assertLess(config.MODEL_PRESETS["fallback"]["num_ctx"],
                        config.MODEL_PRESETS["fast"]["num_ctx"])

    @unittest.skipIf(os.environ.get("JOBSEARCH_MODEL_PRESET"),
                     "JOBSEARCH_MODEL_PRESET is set in this shell")
    def test_default_is_fast_and_drives_model_and_workers(self):
        import config
        self.assertEqual(config.ACTIVE_MODEL_PRESET, "fast")
        self.assertEqual(config.MODEL, config.MODEL_PRESETS["fast"]["model"])
        self.assertEqual(config.SCORE_WORKERS,
                         config.MODEL_PRESETS["fast"]["score_workers"])
        # endpoint / context / scoring-timeout globals also derive from the active preset
        self.assertEqual(config.OLLAMA_URL, config.MODEL_PRESETS["fast"]["ollama_url"])
        self.assertEqual(config.NUM_CTX, config.MODEL_PRESETS["fast"]["num_ctx"])
        self.assertEqual(config.SCORE_TIMEOUT_S,
                         config.MODEL_PRESETS["fast"]["score_timeout_s"])

    def test_read_model_preset_flag_env_and_default(self):
        import config
        old_argv = sys.argv[:]
        old_env = os.environ.pop("JOBSEARCH_MODEL_PRESET", None)
        try:
            sys.argv = ["prog", "--model-preset", "Fallback", "--rescore"]
            self.assertEqual(config._read_model_preset(), "fallback")
            self.assertEqual(sys.argv, ["prog", "--rescore"])   # flag+value consumed
            sys.argv = ["prog"]
            self.assertEqual(config._read_model_preset(), "fast")   # default
            os.environ["JOBSEARCH_MODEL_PRESET"] = "fallback"
            self.assertEqual(config._read_model_preset(), "fallback")   # env var
            sys.argv = ["prog", "--model-preset", "fast"]
            self.assertEqual(config._read_model_preset(), "fast")   # flag beats env
        finally:
            sys.argv = old_argv
            if old_env is not None:
                os.environ["JOBSEARCH_MODEL_PRESET"] = old_env
            else:
                os.environ.pop("JOBSEARCH_MODEL_PRESET", None)

    def test_selecting_fallback_reroutes_endpoint_ctx_workers_timeout(self):
        """The whole point of the change: choosing 'fallback' must flip the derived globals to
        the A4000 instance (:11436), the smaller context, 1 worker and the longer timeout — and
        'fast' back to the 3090-pool values. Reloads config under a patched argv, then restores
        it to the default so later tests see the untouched module."""
        import importlib
        import config
        old_argv, old_env = sys.argv[:], os.environ.pop("JOBSEARCH_MODEL_PRESET", None)
        try:
            sys.argv = ["prog", "--model-preset", "fallback"]
            importlib.reload(config)
            self.assertEqual(config.ACTIVE_MODEL_PRESET, "fallback")
            self.assertTrue(config.OLLAMA_URL.endswith(":11436/api/generate"))
            self.assertEqual(config.NUM_CTX, 4096)
            self.assertEqual(config.SCORE_WORKERS, 1)
            self.assertEqual(config.SCORE_TIMEOUT_S, 300)

            sys.argv = ["prog", "--model-preset", "fast"]
            importlib.reload(config)
            self.assertTrue(config.OLLAMA_URL.endswith(":11434/api/generate"))
            self.assertEqual(config.NUM_CTX, 8192)
            self.assertEqual(config.SCORE_WORKERS, 4)
            self.assertEqual(config.SCORE_TIMEOUT_S, 180)
        finally:
            sys.argv = ["prog"]
            importlib.reload(config)          # restore module to its default (fast) state
            sys.argv = old_argv
            if old_env is not None:
                os.environ["JOBSEARCH_MODEL_PRESET"] = old_env


class EnsureModelAvailable(unittest.TestCase):
    """The preflight: passes silently when the active model is served; exits loudly —
    naming BOTH presets and the selection mechanism — when it isn't. No auto-failover."""

    class _Resp:
        def __init__(self, models):
            self._models = models
        def raise_for_status(self):
            pass
        def json(self):
            return {"models": [{"name": n} for n in self._models]}

    def test_served_model_passes(self):
        from unittest import mock
        with mock.patch.object(core.requests, "get",
                               return_value=self._Resp([core.MODEL, "other:1b"])):
            core.ensure_model_available()          # must not raise

    def test_probes_the_active_presets_endpoint(self):
        """The preflight must check the endpoint THIS preset uses (so the fallback preset
        probes its A4000 instance, not the main :11434), derived from OLLAMA_URL."""
        from unittest import mock
        with mock.patch.object(core.requests, "get",
                               return_value=self._Resp([core.MODEL])) as g:
            core.ensure_model_available()
        called_url = g.call_args.args[0] if g.call_args.args else g.call_args.kwargs["url"]
        self.assertEqual(called_url, core.OLLAMA_URL.replace("/api/generate", "/api/tags"))

    def test_missing_model_exits_naming_all_presets(self):
        from unittest import mock
        with mock.patch.object(core.requests, "get",
                               return_value=self._Resp(["something-else:7b"])):
            with self.assertRaises(SystemExit) as cm:
                core.ensure_model_available()
        msg = str(cm.exception)
        self.assertIn(core.MODEL, msg)
        for preset, spec in core.MODEL_PRESETS.items():
            self.assertIn(preset, msg)
            self.assertIn(spec["model"], msg)
        self.assertIn("--model-preset", msg)

    def test_unreachable_server_exits_naming_all_presets(self):
        from unittest import mock
        with mock.patch.object(core.requests, "get",
                               side_effect=OSError("connection refused")):
            with self.assertRaises(SystemExit) as cm:
                core.ensure_model_available()
        msg = str(cm.exception)
        for preset in core.MODEL_PRESETS:
            self.assertIn(preset, msg)
        self.assertIn("--model-preset", msg)


class ScoringModelProvenance(unittest.TestCase):
    def test_archive_has_the_column_and_rows_project_it(self):
        self.assertIn("scoring_model", core.ARCHIVE_FIELDS)
        row = core._row_from({"title": "T", "scoring_model": "gemma4:31b-it-q8_0"})
        self.assertEqual(row["scoring_model"], "gemma4:31b-it-q8_0")

    def test_runs_log_has_the_preset_columns(self):
        self.assertIn("model_preset", core.RUNS_FIELDS)
        self.assertIn("model", core.RUNS_FIELDS)


class MergeExtractedFields(unittest.TestCase):
    """The post-score deterministic merge: confident extractor values OVERWRITE the LLM's
    mechanical fields, sentinels leave them standing, danish_level only ever gets a FLOOR,
    and the judgment fields are never touched. Pure — no Ollama call involved."""

    def _job(self, **kw):
        """A job dict as it stands right after job.update(score_job(...)) — the LLM's
        answers in place, the teaser's own location preserved under _source_location."""
        base = {"title": "Data Analyst", "_source_location": "",
                "score": 88, "track": "A", "is_tech_company": True,
                "employment_type": "full_time", "work_mode": "remote",
                "location": "Aalborg", "commute_ok": True,
                "danish_level": "none", "deadline": "2099-01-01",
                "reasoning": "llm says so", "matched_skills": ["python"]}
        base.update(kw)
        return base

    def test_confident_values_overwrite_the_llm(self):
        job = self._job(title="Studentermedhjælper til dataanalyse")
        desc = ("Vi søger en studentermedhjælper til vores kontor i København. "
                "Arbejdet foregår på kontoret. Ansøgningsfrist: 01-09-2026.")
        core.merge_extracted_fields(job, desc)
        self.assertEqual(job["employment_type"], "student")     # was full_time
        self.assertEqual(job["work_mode"], "onsite")            # was remote
        self.assertEqual(job["location"], "Copenhagen")         # was Aalborg
        self.assertTrue(job["commute_ok"])                      # Copenhagen is commutable
        self.assertEqual(job["deadline"], "2026-09-01")         # was 2099-01-01

    def test_sentinels_keep_the_llm_values(self):
        job = self._job()
        stats = core.merge_extracted_fields(job, "A role. You will do great things.")
        self.assertEqual(job["employment_type"], "full_time")   # LLM's stands
        self.assertEqual(job["work_mode"], "remote")
        # LLM's "Aalborg" stands (no confident extraction) — and the deterministic commute
        # lookup DOES know Aalborg is not commutable, so that one is corrected.
        self.assertEqual(job["location"], "Aalborg")
        self.assertFalse(job["commute_ok"])
        self.assertEqual(job["deadline"], "2099-01-01")
        self.assertEqual(job["matched_skills"], ["python"])     # no vocab -> LLM's list
        self.assertEqual(stats["det"], 1)                       # only commute_ok
        self.assertEqual(stats["llm"], 5)

    def test_judgment_fields_never_touched(self):
        job = self._job(title="Studentermedhjælper")
        core.merge_extracted_fields(job, "Studenterjob i København. Dansk er et krav.")
        self.assertEqual(job["score"], 88)
        self.assertEqual(job["track"], "A")
        self.assertTrue(job["is_tech_company"])
        self.assertEqual(job["reasoning"], "llm says so")

    def test_source_location_preferred_over_llm(self):
        job = self._job(_source_location="Lyngby", location="Odense")
        core.merge_extracted_fields(job, "No city named in this text.")
        self.assertEqual(job["location"], "Lyngby")             # teaser's own wins
        self.assertTrue(job["commute_ok"])                      # ...and it's commutable
        self.assertNotIn("_source_location", job)               # consumed, not archived

    def test_danish_floor_lifts_but_never_lowers(self):
        job = self._job(danish_level="none")
        stats = core.merge_extracted_fields(job, "Dansk er et krav for rollen.")
        self.assertEqual(job["danish_level"], "required")
        self.assertEqual(stats["danish_lift"], 1)
        job = self._job(danish_level="required")
        stats = core.merge_extracted_fields(job, "English is fine. No Danish needed.")
        self.assertEqual(job["danish_level"], "required")       # floor never lowers
        self.assertEqual(stats["danish_lift"], 0)

    def test_skills_vocab_overwrites_when_configured(self):
        old = core.SKILLS_VOCAB
        core.SKILLS_VOCAB = ["SQL", "Docker"]
        try:
            job = self._job()
            core.merge_extracted_fields(job, "You will write SQL all day.")
            self.assertEqual(job["matched_skills"], ["SQL"])    # LLM's ["python"] replaced
            job = self._job()
            core.merge_extracted_fields(job, "You will water the plants.")
            self.assertEqual(job["matched_skills"], [])         # confident empty
        finally:
            core.SKILLS_VOCAB = old

    def test_stats_cover_all_six_fields(self):
        job = self._job()
        stats = core.merge_extracted_fields(job, "Nothing extractable here.")
        self.assertEqual(stats["det"] + stats["llm"], 6)

    def test_never_drops_the_row(self):
        # The merge FILLS fields; the job dict itself must always survive intact.
        job = self._job(title="Studentermedhjælper eller fuldtid?!")
        out = core.merge_extracted_fields(job, "praktik deltid fuldtid remote on-site kaos")
        self.assertIsInstance(out, dict)
        self.assertEqual(job["score"], 88)                      # still a scoreable row


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

    def test_score_role_passthrough_and_guards(self):
        import c_prepare, core
        orig = core.score_job
        try:
            core.score_job = lambda job, desc, model=None: {
                "score": 88, "track": "A", "employment_type": "student", "reasoning": "ok"}
            res = c_prepare._score_role("T", "C", "Cph", "http://x", "a real description")
            self.assertEqual(res["score"], 88)
            self.assertIsNone(c_prepare._score_role("T", "C", "Cph", "http://x", ""))   # no desc
            core.score_job = lambda job, desc, model=None: {"reasoning": "scoring error"}
            self.assertIsNone(c_prepare._score_role("T", "C", "Cph", "http://x", "d"))  # error
        finally:
            core.score_job = orig


if __name__ == "__main__":
    unittest.main()
