"""Tests for the personal-first job scorer.

Run with: python3 -m tests.test_scorer
"""

from __future__ import annotations

import unittest

from openclaw.scoring.scorer import JobScorer


class FakeBedrockScorer(JobScorer):
    def _score_with_bedrock(self, **kwargs):  # type: ignore[override]
        return {
            "experience_match": 91,
            "confidence": 84,
            "evidence": ["Python backend work", "FastAPI projects"],
            "risks": ["No direct fintech background"],
            "reasoning": "You have clear backend evidence and the role is a strong use of time.",
        }


class TestJobScorer(unittest.TestCase):
    def test_prompt_uses_candidate_name_and_profile_summary(self):
        scorer = JobScorer()
        prompt = scorer._build_scoring_prompt(
            company="Stripe",
            role="Backend Engineer Intern",
            location="New York, NY",
            job_description="Build Python APIs.",
            profile_summary="Preferred roles: Backend Intern\nPreferred locations: NYC",
            resume_text="Python, SQL, FastAPI",
            age_str="4.0 hours",
            candidate_name="Aditya Singh",
        )
        self.assertIn("Aditya Singh", prompt)
        self.assertIn("Preferred roles: Backend Intern", prompt)
        self.assertIn("Preferred locations: NYC", prompt)

    def test_parse_scoring_response_handles_wrapped_json(self):
        scorer = JobScorer()
        parsed = scorer._parse_scoring_response(
            """Here is the result:

```json
{"experience_match": 83, "confidence": 71, "evidence": ["Python"], "risks": ["Limited ML"], "reasoning": "Good fit."}
```
"""
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["experience_match"], 83.0)
        self.assertEqual(parsed["confidence"], 71.0)
        self.assertEqual(parsed["evidence"], ["Python"])

    def test_fallback_analysis_uses_resume_overlap(self):
        scorer = JobScorer()
        result = scorer._fallback_experience_analysis(
            role="Backend Engineer Intern",
            job_description="Build Python APIs with FastAPI and SQL for backend services.",
            profile_summary="Preferred roles: Backend Intern",
            resume_text="Built backend services in Python with FastAPI and PostgreSQL.",
            job_preferences={"target_roles": ["Backend Intern"]},
        )
        self.assertGreater(result["experience_match"], 60)
        self.assertGreater(result["confidence"], 40)
        self.assertTrue(result["evidence"])

    def test_score_job_combines_deterministic_and_llm_signals(self):
        scorer = FakeBedrockScorer()
        scored = scorer.score_job(
            company="Stripe",
            role="Backend Engineer Intern",
            location="New York, NY",
            job_description="Build Python APIs with SQL and distributed systems.",
            profile_summary="Preferred roles: Backend Intern\nPreferred locations: New York",
            resume_text="Python FastAPI SQL distributed systems backend work.",
            age_hours=3.0,
            candidate_name="Aditya Singh",
            job_preferences={
                "target_roles": ["Backend Intern"],
                "preferred_locations": ["New York"],
                "target_companies": ["Stripe"],
            },
            job_url="https://jobs.lever.co/acme/backend",
        )
        self.assertGreaterEqual(scored.score, 80)
        self.assertEqual(scored.recommendation, "high_priority")
        self.assertIn("confidence", scored.breakdown)
        self.assertIn("role_alignment", scored.breakdown)
        self.assertIn("Evidence:", scored.reasoning)


if __name__ == "__main__":
    unittest.main()
