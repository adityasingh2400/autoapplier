"""Tests for the dropdown option matching and answer bank logic.

Run with:  python3 -m tests.test_option_matching
"""
from __future__ import annotations

import sys
import unittest


# --- Test _best_option_match (the pure-logic option picker) ---

from openclaw.ats.base import BaseATSHandler


class TestBestOptionMatch(unittest.TestCase):
    """Tests for ATSHandler._best_option_match (static method)."""

    match = staticmethod(BaseATSHandler._best_option_match)

    # ---- Exact matches ----

    def test_exact_match(self):
        idx, score = self.match("Yes", ["No", "Yes", "Maybe"])
        self.assertEqual(idx, 1)
        self.assertEqual(score, 10000)

    def test_exact_match_case_insensitive(self):
        idx, score = self.match("yes", ["No", "Yes", "Maybe"])
        self.assertEqual(idx, 1)
        self.assertEqual(score, 10000)

    def test_exact_match_extra_whitespace(self):
        idx, score = self.match("  Yes ", ["No", "Yes", "Maybe"])
        self.assertEqual(idx, 1)
        self.assertEqual(score, 10000)

    # ---- Substring matches ----

    def test_answer_substring_of_option(self):
        """Answer 'Accept' is a substring of 'Accept and continue'."""
        idx, score = self.match("Accept", ["Decline", "Accept and continue"])
        self.assertEqual(idx, 1)
        self.assertGreater(score, 0)

    def test_option_substring_of_answer(self):
        """Option 'Job Board' is a substring of 'Online Job Board'."""
        idx, score = self.match("Online Job Board", ["Referral", "Job Board", "Other"])
        self.assertEqual(idx, 1)
        self.assertGreater(score, 0)

    # ---- Token-based matching ----

    def test_token_match_veteran(self):
        """'I am not a veteran' should match 'I am not a protected veteran'."""
        options = [
            "I am a protected veteran",
            "I am not a protected veteran",
            "I don't wish to answer",
        ]
        idx, score = self.match("I am not a veteran", options)
        self.assertEqual(idx, 1)
        self.assertGreater(score, 0)

    def test_token_match_disability(self):
        """'Yes, I have a disability' should match the long Greenhouse wording."""
        options = [
            "No, I do not have a disability",
            "Yes, I have a disability, or have had one in the past",
            "I do not wish to answer",
        ]
        idx, score = self.match("Yes, I have a disability", options)
        self.assertEqual(idx, 1)
        self.assertGreater(score, 0)

    def test_token_match_bachelors_degree(self):
        """'Bachelor of Science' should match a Bachelor's option."""
        options = [
            "Associates Degree received",
            "Bachelor's Degree received",
            "Bachelor's Degree in progress",
            "Master's Degree received",
        ]
        idx, score = self.match("Bachelor of Science", options)
        self.assertTrue(options[idx].startswith("Bachelor"))
        self.assertGreater(score, 0)

    # ---- Prefer not to say ----

    def test_prefer_not_to_say_gender(self):
        options = ["Male", "Female", "Decline To Self Identify"]
        idx, score = self.match("Decline To Self Identify", options)
        self.assertEqual(idx, 2)
        self.assertEqual(score, 10000)

    def test_decline_to_self_identify_hispanic(self):
        options = ["Yes", "No", "Decline To Self Identify"]
        idx, score = self.match("Decline To Self Identify", options)
        self.assertEqual(idx, 2)
        self.assertEqual(score, 10000)

    def test_decline_to_self_identify_race(self):
        options = [
            "American Indian or Alaska Native",
            "Asian",
            "Black or African American",
            "Hispanic or Latino",
            "White",
            "Two or more races",
            "Decline To Self Identify",
        ]
        idx, score = self.match("Decline To Self Identify", options)
        self.assertEqual(idx, 6)
        self.assertEqual(score, 10000)

    # ---- No match ----

    def test_no_match_returns_negative(self):
        idx, score = self.match("Klingon", ["English", "Spanish", "French"])
        self.assertEqual(idx, -1)
        self.assertEqual(score, 0)

    def test_empty_answer(self):
        idx, score = self.match("", ["A", "B"])
        self.assertEqual(idx, -1)
        self.assertEqual(score, 0)

    def test_empty_options(self):
        idx, score = self.match("Yes", [])
        self.assertEqual(idx, -1)
        self.assertEqual(score, 0)

    # ---- Real-world Greenhouse options ----

    def test_country_united_states(self):
        options = [
            "Afghanistan",
            "United Kingdom +44",
            "United States +1",
            "Uruguay +598",
        ]
        idx, score = self.match("United States", options)
        self.assertEqual(idx, 2)
        self.assertGreater(score, 0)

    def test_how_did_you_hear_job_board(self):
        options = [
            "Referral",
            "Job Board",
            "University/College",
            "LinkedIn",
            "Company Website",
            "Other",
        ]
        idx, score = self.match("Online Job Board", options)
        self.assertEqual(idx, 1)
        self.assertGreater(score, 0)

    def test_california_accept(self):
        options = ["Accept", "Decline"]
        idx, score = self.match("Accept", options)
        self.assertEqual(idx, 0)
        self.assertEqual(score, 10000)

    def test_work_authorization_yes(self):
        idx, score = self.match("Yes", ["Yes", "No"])
        self.assertEqual(idx, 0)
        self.assertEqual(score, 10000)

    def test_sponsorship_no(self):
        idx, score = self.match("No", ["Yes", "No"])
        self.assertEqual(idx, 1)
        self.assertEqual(score, 10000)

    def test_not_a_veteran_picks_correct_option(self):
        """Ensure 'I am not a veteran' doesn't pick 'I am a protected veteran'."""
        options = [
            "I am not a protected veteran",
            "I am a protected veteran",
            "I don't wish to answer",
        ]
        idx, score = self.match("I am not a veteran", options)
        self.assertEqual(idx, 0, f"Should pick 'I am not a protected veteran' but got '{options[idx]}'")

    def test_degree_status_bs_currently_pursuing(self):
        """Samsung's Degree Status dropdown has 'BS currently pursuing'."""
        options = [
            "Associates Degree received",
            "BA currently pursuing",
            "BA received",
            "BS currently pursuing",
            "BS received",
            "MA currently pursuing",
            "MA received",
            "MS currently pursuing",
            "MS received",
            "MBA currently pursuing",
            "MBA received",
            "PhD currently pursuing",
            "PhD received",
        ]
        idx, score = self.match("BS currently pursuing", options)
        self.assertEqual(idx, 3)
        self.assertEqual(score, 10000)

# --- Test answer_bank matching ---

from openclaw.answer_bank import match_question_bank, normalize_text, normalize_text_fuzzy


class TestAnswerBank(unittest.TestCase):

    def test_exact_pattern_match(self):
        bank = [("how did you hear", "Online Job Board")]
        self.assertEqual(match_question_bank("How did you hear about this job?", bank), "Online Job Board")

    def test_regex_pattern_match(self):
        bank = [("re:full\\s+time.*question.35207622002", "false")]
        label = "Full Time Please identify your target internship dates. * * question_35207622002"
        self.assertEqual(match_question_bank(label, bank), "false")

    def test_fuzzy_match_underscore_bracket(self):
        bank = [("full time question 35207622002", "false")]
        label = "Full Time question_35207622002[]"
        self.assertEqual(match_question_bank(label, bank), "false")

    def test_no_match(self):
        bank = [("something unrelated", "answer")]
        self.assertIsNone(match_question_bank("totally different", bank))

    def test_best_match_wins(self):
        bank = [("degree", "BS"), ("degree status", "Bachelor's")]
        self.assertEqual(match_question_bank("Degree Status dropdown", bank), "Bachelor's")

    def test_human_sentinel_returned(self):
        bank = [("address line 2", "__HUMAN__")]
        self.assertEqual(match_question_bank("Address Line 2", bank), "__HUMAN__")


class TestNormalization(unittest.TestCase):

    def test_normalize_text_basic(self):
        self.assertEqual(normalize_text("  Hello   World  "), "hello world")

    def test_normalize_text_fuzzy_underscores(self):
        self.assertEqual(normalize_text_fuzzy("question_35207622002[]"), "question 35207622002")

    def test_normalize_text_fuzzy_hyphens(self):
        self.assertEqual(normalize_text_fuzzy("Internship - Summer (May to August) 2026"), "internship summer may to august 2026")


if __name__ == "__main__":
    unittest.main(verbosity=2)
