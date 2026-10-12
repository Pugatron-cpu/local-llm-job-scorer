"""Golden test: the scoring prompt for the OWNER profile must not drift.

The archive holds 1500+ roles scored with this exact prompt text, and applications.csv pairs the
model's score with the real outcome (applied / interview / rejected) — an eval set. Change a word
of the prompt and the old scores stop being comparable to the new ones, silently and irreversibly.

So the owner's rendered prompt is pinned here BYTE FOR BYTE, exactly as it stood before the
profile refactor pulled the candidate's bridge experience out of core.py and into borja.toml.
Refactor freely; if the text moves, this fails.

If you INTEND to change the prompt (a better rubric, a new field), update EXPECTED deliberately —
and know that you should re-score the archive (a_scrape.py --rescore-all) or accept that rows
before and after are no longer on the same scale.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import core

JOB = {"title": "Student Data Engineer", "company": "Acme A/S"}
DESC = "We are hiring a student data engineer. Python, SQL, Azure."

# The prompt as it rendered BEFORE the refactor, with the bio inline. Reproduced literally.
# Changed deliberately 2026-10-12: the owner's Track A gained four role families (thesis,
# consulting, ERP/CRM, AI adoption) and the audit/"consultant" guards. Scores before that date
# were made with the old text. Same day, Track B was switched off in the owner profile, so the
# intro says ONE kind of role and the Track B block (with its bridge sentence) is gone.
# Later that day: technology risk / IT audit added as partial-overlap Track A.
EXPECTED = f"""You are screening jobs for a candidate. Score the fit 0-100.
There is ONE acceptable kind of role.

TRACK A — technical / data role (preferred):
  data analyst, BI, data/AI/ML engineering, IT/service-desk support, software,
  automation, etc. Score by overlap with the candidate's skills and projects.
  These are ALSO Track A, scored the same way on their day-to-day tasks:
  - master's thesis collaborations ("thesis", "speciale", "specialestuderende") on a
    data, AI, software or analytics topic. A thesis is project work, not a job; do not
    lower the score for that.
  - technology / data / AI consulting: student, junior or graduate consultant roles in
    the tech, data, AI or digital practices of consultancies (Big 4 included), client-facing
    and business-analysis work included.
  - ERP / CRM platform roles (SAP, Microsoft Dynamics 365, ServiceNow, Salesforce platform):
    functional or technical consultant, business analyst, implementation, support.
  - AI adoption, digital transformation and automation roles at ANY employer, when the
    day-to-day work is introducing, building or supporting AI, automation or data tooling.
  - technology risk / IT audit / IT controls / cyber-risk roles (student or graduate, Big 4
    included): Track A, but PARTIAL overlap at best (60-84), since the work reviews systems
    rather than builds them.
    85-100: technical role closely matching the skills/projects.
    60-84 : technical but only partial overlap, or borderline seniority.
  "Technical" means SOFTWARE/DATA/IT technical. A role in an unrelated engineering or
  science domain (mechanical, civil, electrical, chemical, construction, lab/clinical,
  pharma QA, finance/audit, legal) scores <= 35 UNLESS its day-to-day tasks are
  substantially programming, data or IT work matching the candidate's actual skills.
  Audit, tax, deal and financial-advisory consulting is finance/audit, not tech consulting
  (technology risk / IT audit, above, is the one exception).
  Do not award points for the word "engineer", "analyst" or "consultant" alone.

Any HR, marketing, or sales role scores 0."""


class ScorePromptGoldenTest(unittest.TestCase):

    def test_owner_prompt_is_byte_identical_to_pre_refactor(self):
        """The Track A + Track B block — where the hardcoded bio lived — must be unchanged."""
        if config.ACTIVE_PROFILE != "borja":
            self.skipTest(f"golden prompt is pinned for the owner profile, not {config.ACTIVE_PROFILE!r}")
        got = core._score_prompt(JOB, DESC)
        self.assertTrue(
            got.startswith(EXPECTED),
            "The scoring prompt has DRIFTED from the text the archive was scored with.\n"
            "Old and new scores are no longer comparable. If this was deliberate, update "
            "EXPECTED in this test and re-score the archive.\n\n"
            f"--- expected (first 400 chars) ---\n{EXPECTED[:400]}\n\n"
            f"--- got ---\n{got[:400]}")

    def test_bridge_sentence_comes_from_the_profile_not_the_code(self):
        """The personal fact must come from the PROFILE. Tested by behaviour, not by grepping the
        source: blank the profile value and the CV must vanish from the prompt entirely. If any of
        it were still baked into core.py, it would survive this and the assertion would fail."""
        self.assertIn(config.TRACK_B_BRIDGE, core._score_prompt(JOB, DESC))
        # Blank BOTH profile-sourced values. Whatever personal text survives that is, by
        # definition, hardcoded in the engine. (CANDIDATE_PROFILE legitimately carries the CV —
        # it just has to come FROM the profile, which is exactly what this proves.)
        original = (core.TRACK_B_BRIDGE, core.CANDIDATE_PROFILE)
        try:
            core.TRACK_B_BRIDGE = ""
            core.CANDIDATE_PROFILE = ""
            bare = core._score_prompt(JOB, DESC)
            for personal in ("hospitality", "corporate-operations", "7 years", "Borja"):
                self.assertNotIn(personal, bare,
                                 f"{personal!r} is baked into core.py — every other profile "
                                 "would be scored against the owner's CV.")
        finally:
            core.TRACK_B_BRIDGE, core.CANDIDATE_PROFILE = original

    def test_profile_without_the_key_drops_the_sentence_cleanly(self):
        """A profile that sets no track_b_bridge must not render 'via , then move laterally'."""
        if not core.TRACK_B_DEF:
            self.skipTest("active profile has Track B switched off (covered by the _test profile)")
        original = core.TRACK_B_BRIDGE
        try:
            core.TRACK_B_BRIDGE = ""
            got = core._score_prompt(JOB, DESC)
            self.assertNotIn("via ,", got)
            self.assertNotIn("The candidate wants to enter a", got)
            self.assertIn("TRACK B", got)          # the track itself survives
        finally:
            core.TRACK_B_BRIDGE = original


if __name__ == "__main__":
    unittest.main()
