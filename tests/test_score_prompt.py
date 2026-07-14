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
EXPECTED = f"""You are screening jobs for a candidate. Score the fit 0-100.
There are TWO acceptable kinds of role.

TRACK A — technical / data role (preferred):
  data analyst, BI, data/AI/ML engineering, IT/service-desk support, software,
  automation, etc. Score by overlap with the candidate's skills and projects.
    85-100: technical role closely matching the skills/projects.
    60-84 : technical but only partial overlap, or borderline seniority.
  "Technical" means SOFTWARE/DATA/IT technical. A role in an unrelated engineering or
  science domain (mechanical, civil, electrical, chemical, construction, lab/clinical,
  pharma QA, finance/audit, legal) scores <= 35 UNLESS its day-to-day tasks are
  substantially programming, data or IT work matching the candidate's actual skills.
  Do not award points for the word "engineer" or "analyst" alone.

TRACK B — foot-in-the-door role AT a tech company:
  office assistant, reception, front desk, workplace/facilities, logistics,
  operations, coordinator, administration, support. The candidate wants to enter a
  tech company via 7 years of combined corporate-operations and hospitality experience,
  then move laterally."""


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
