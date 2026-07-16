"""
Unit tests for extractors.py — the deterministic extraction layer for the mechanical
scoring fields. Danish and English fixture snippets per extractor, INCLUDING the conflict
cases that must return the sentinel: the contract is confident-value-or-silent, never guess.

Pure functions: no network, no Ollama, no Playwright. Same bootstrap as test_core.py
(extractors imports config for the COMMUTABLE_AREAS default, and config needs a profile).
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Minimal profile so config.py doesn't sys.exit at import for lack of an owner.
_TEST_PROFILE = os.path.join(ROOT, "profiles", "_test.toml")
if not os.path.exists(_TEST_PROFILE):
    with open(_TEST_PROFILE, "w", encoding="utf-8") as _f:
        _f.write('candidate_profile = "test candidate"\nlocation_anchor = "test anchor"\n')
os.environ.setdefault("JOBSEARCH_OWNER", "_test")

import extractors  # noqa: E402
import core        # noqa: E402  (deadline consistency test)


class EmploymentType(unittest.TestCase):
    def test_danish_student_markers(self):
        self.assertEqual(extractors.extract_employment_type(
            "Studentermedhjælper til dataanalyse", "Vi søger en studentermedhjælper."),
            "student")
        self.assertEqual(extractors.extract_employment_type(
            "Studiejob i IT-afdelingen", ""), "student")

    def test_english_student_markers(self):
        self.assertEqual(extractors.extract_employment_type(
            "Student Assistant, Data", "Join us as a student assistant."), "student")
        self.assertEqual(extractors.extract_employment_type(
            "Student Worker", "part of your studies"), "student")

    def test_part_time(self):
        self.assertEqual(extractors.extract_employment_type(
            "Regnskabsassistent", "Stillingen er en deltidsstilling på 25 timer."),
            "part_time")
        self.assertEqual(extractors.extract_employment_type(
            "Office Assistant", "This is a part-time position."), "part_time")

    def test_full_time(self):
        self.assertEqual(extractors.extract_employment_type(
            "Data Engineer", "Fuldtidsstilling med start snarest."), "full_time")
        self.assertEqual(extractors.extract_employment_type(
            "Data Engineer", "This is a full time role."), "full_time")

    def test_internship(self):
        self.assertEqual(extractors.extract_employment_type(
            "Praktikant til marketing", "Praktikplads i efteråret."), "internship")
        self.assertEqual(extractors.extract_employment_type(
            "Software Internship", "A 6-month internship."), "internship")

    def test_student_plus_deltid_is_student_not_conflict(self):
        # Every studenterjob is part-time hours; the taxonomy defines part_time as NON-student.
        self.assertEqual(extractors.extract_employment_type(
            "Studentermedhjælper", "15-20 timer om ugen, deltid."), "student")

    def test_genuine_conflict_is_unknown(self):
        self.assertEqual(extractors.extract_employment_type(
            "Studentermedhjælper eller praktikant", "studenterjob eller praktik"), "unknown")
        self.assertEqual(extractors.extract_employment_type(
            "Konsulent", "Fuldtid eller deltid efter aftale."), "unknown")

    def test_absent_is_unknown(self):
        self.assertEqual(extractors.extract_employment_type(
            "Data Analyst", "You will build dashboards."), "unknown")

    def test_danish_intern_word_does_not_mean_internship(self):
        # "intern" in Danish means "internal" — must not fire the internship rule.
        self.assertEqual(extractors.extract_employment_type(
            "Konsulent", "Du får ansvar for intern kommunikation og internationale kunder."),
            "unknown")


class WorkMode(unittest.TestCase):
    def test_remote(self):
        self.assertEqual(extractors.extract_work_mode("This role is fully remote."), "remote")
        self.assertEqual(extractors.extract_work_mode("Du kan arbejde hjemmefra."), "remote")

    def test_hybrid(self):
        self.assertEqual(extractors.extract_work_mode("We offer a hybrid setup."), "hybrid")
        self.assertEqual(extractors.extract_work_mode("Hybridarbejde er muligt."), "hybrid")

    def test_hybrid_wins_over_its_own_ingredients(self):
        # Hybrid ads naturally mention both home and office — not a conflict.
        self.assertEqual(extractors.extract_work_mode(
            "Hybrid: 3 days on-site, 2 days remote."), "hybrid")

    def test_onsite(self):
        self.assertEqual(extractors.extract_work_mode("You will work on-site in our lab."),
                         "onsite")
        self.assertEqual(extractors.extract_work_mode("Arbejdet foregår på kontoret."),
                         "onsite")
        self.assertEqual(extractors.extract_work_mode("Der er krav om fysisk fremmøde."),
                         "onsite")

    def test_negated_remote_does_not_fire(self):
        self.assertEqual(extractors.extract_work_mode(
            "This is not a remote position."), "unknown")
        # ...and with an onsite signal present, onsite wins cleanly.
        self.assertEqual(extractors.extract_work_mode(
            "This is not a remote position; you work on-site."), "onsite")

    def test_conflict_is_unknown(self):
        self.assertEqual(extractors.extract_work_mode(
            "Choose remote or on-site, whatever suits you."), "unknown")

    def test_absent_is_unknown(self):
        self.assertEqual(extractors.extract_work_mode("We build data pipelines."), "unknown")


class Location(unittest.TestCase):
    def test_source_provided_wins(self):
        job = {"title": "Data Student", "location": "Aarhus C"}
        self.assertEqual(extractors.extract_location(job, "Our office is in Copenhagen."),
                         "Aarhus C")

    def test_na_falls_through_to_text(self):
        job = {"title": "Data Student", "location": "N/A"}
        self.assertEqual(extractors.extract_location(job, "Kontoret ligger i København."),
                         "Copenhagen")

    def test_danish_spelling_variant_maps_to_canonical(self):
        job = {"title": "Studentermedhjælper", "location": ""}
        self.assertEqual(extractors.extract_location(job, "Vores kontor i Århus."), "Aarhus")

    def test_longest_variant_wins(self):
        job = {"title": "IT Student", "location": ""}
        self.assertEqual(extractors.extract_location(job, "Based in Kongens Lyngby."),
                         "Kongens Lyngby")

    def test_two_cities_is_ambiguous(self):
        job = {"title": "Data Student", "location": ""}
        self.assertEqual(extractors.extract_location(
            job, "We have offices in Copenhagen and Aarhus."), "")

    def test_no_city_is_blank(self):
        job = {"title": "Data Student", "location": ""}
        self.assertEqual(extractors.extract_location(job, "A great role in a great team."),
                         "")

    def test_city_not_matched_inside_words(self):
        job = {"title": "Data Student", "location": ""}
        # "vejle" must not fire inside "vejledning" (guidance).
        self.assertEqual(extractors.extract_location(job, "Du får grundig vejledning."), "")


class CommuteOk(unittest.TestCase):
    AREAS = {"copenhagen", "københavn", "lyngby", "remote"}

    def test_commutable(self):
        self.assertTrue(extractors.commute_ok("Copenhagen, Denmark", self.AREAS))
        self.assertTrue(extractors.commute_ok("Kgs. Lyngby", self.AREAS))
        self.assertTrue(extractors.commute_ok("Remote (EU)", self.AREAS))

    def test_known_city_not_in_set_is_false(self):
        self.assertFalse(extractors.commute_ok("Aarhus C", self.AREAS))
        self.assertFalse(extractors.commute_ok("Odense", self.AREAS))

    def test_unknown_or_empty_is_none(self):
        self.assertIsNone(extractors.commute_ok("", self.AREAS))
        self.assertIsNone(extractors.commute_ok("N/A", self.AREAS))
        self.assertIsNone(extractors.commute_ok("Somewhere in Jutland", self.AREAS))

    def test_empty_area_set_disables_the_check(self):
        # commutable_areas = [] in a profile -> deterministic check off, LLM stands.
        self.assertIsNone(extractors.commute_ok("Copenhagen", set()))

    def test_default_areas_come_from_config(self):
        import config
        self.assertEqual(extractors.commute_ok("Ørestad, København"),
                         extractors.commute_ok("Ørestad, København", config.COMMUTABLE_AREAS))


class Deadline(unittest.TestCase):
    def test_danish_month_name(self):
        self.assertEqual(extractors.extract_deadline(
            "Ansøgningsfrist: 15. august 2026"), "2026-08-15")

    def test_numeric_and_short_year(self):
        self.assertEqual(extractors.extract_deadline("Deadline: 01-09-26"), "2026-09-01")
        self.assertEqual(extractors.extract_deadline("Apply by 31/12/2026"), "2026-12-31")

    def test_no_deadline_is_blank(self):
        self.assertEqual(extractors.extract_deadline("No deadline stated here."), "")

    def test_invalid_date_is_blank(self):
        self.assertEqual(extractors.extract_deadline("Ansøgningsfrist: 32.13.2026"), "")

    def test_agrees_with_deadline_passed(self):
        # Same parser powers both: a date extract_deadline reads as past must be one
        # deadline_passed drops, and vice versa.
        past = "Ansøgningsfrist: 01-01-2020"
        future = "Apply before 31-12-2099"
        self.assertEqual(extractors.extract_deadline(past), "2020-01-01")
        self.assertTrue(core.deadline_passed(past))
        self.assertEqual(extractors.extract_deadline(future), "2099-12-31")
        self.assertFalse(core.deadline_passed(future))


class MatchedSkills(unittest.TestCase):
    VOCAB = ["Python", "SQL", "Power BI", "C++", "Java", "Excel"]

    def test_case_insensitive_whole_word_intersection(self):
        found = extractors.extract_matched_skills(
            "You know python, SQL and Power BI. C++ is a plus.", self.VOCAB)
        self.assertEqual(found, ["Python", "SQL", "Power BI", "C++"])   # vocab casing kept

    def test_java_does_not_match_javascript(self):
        self.assertEqual(extractors.extract_matched_skills(
            "We use JavaScript everywhere.", self.VOCAB), [])

    def test_no_vocab_is_sentinel_none(self):
        self.assertIsNone(extractors.extract_matched_skills("python everywhere", []))
        self.assertIsNone(extractors.extract_matched_skills("python everywhere", None))

    def test_vocab_with_no_hits_is_confident_empty(self):
        self.assertEqual(extractors.extract_matched_skills(
            "We are hiring a florist.", self.VOCAB), [])

    def test_duplicate_vocab_entries_collapse(self):
        self.assertEqual(extractors.extract_matched_skills(
            "python python python", ["python", "Python"]), ["python"])


class DanishLevelFloor(unittest.TestCase):
    def test_explicit_danish_requirements(self):
        for snippet in ("Dansk er et krav for stillingen.",
                        "Du taler flydende dansk.",
                        "Vi søger en dansktalende medarbejder.",
                        "Danish is required for this role.",
                        "You must speak Danish with our customers.",
                        "The role requires fluent Danish.",
                        "Fluency in Danish is mandatory."):
            self.assertEqual(extractors.danish_level_floor(snippet), "required",
                             f"should fire on: {snippet!r}")

    def test_softened_phrases_stay_silent(self):
        for snippet in ("Flydende dansk er en fordel, men ikke et krav.",
                        "Fluent Danish is a plus.",
                        "Danish-speaking colleagues, but the role requires no Danish... "
                        "dansktalende er nice to have."):
            self.assertIsNone(extractors.danish_level_floor(snippet),
                              f"should stay silent on: {snippet!r}")

    def test_negated_english_stays_silent(self):
        self.assertIsNone(extractors.danish_level_floor("Danish is not required."))

    def test_absent_is_none(self):
        self.assertIsNone(extractors.danish_level_floor(
            "We work in English. Great snacks in the office."))


if __name__ == "__main__":
    unittest.main()
