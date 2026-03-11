"""Tests for scoring/apply pipeline edge cases.

Run with: python3 -m tests.test_scoring_pipeline
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import openclaw.applier as applier
from openclaw.scoring.ledger import JobLedger
from openclaw.scoring.scorer import ScoredJob


def _write_memory_root(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "profile.json").write_text(
        json.dumps(
            {
                "identity": {"name": "Aditya Singh", "email": "aditya@example.com"},
                "jobPreferences": {"targetRoles": ["Backend Intern"]},
            }
        ),
        encoding="utf-8",
    )
    (path / "resume.json").write_text(
        json.dumps(
            {
                "skills": ["Python", "FastAPI", "SQL"],
                "education": [{"institution": "CMU", "degree": "BS CS"}],
                "work": [{"position": "Software Engineer Intern", "name": "Acme"}],
            }
        ),
        encoding="utf-8",
    )


def _make_args(**overrides):
    defaults = {
        "min_score": 70.0,
        "max_jobs": 2,
        "dry_run": True,
        "force": False,
        "timeout_sec": 5,
        "human_in_loop": False,
        "max_form_pages": 2,
        "max_custom_questions": 5,
        "no_pause_on_captcha": False,
        "no_pause_on_auth": False,
        "no_pause_on_missing_fields": False,
        "no_captcha_auto_solve": False,
        "quality": False,
        "tailor_resume": False,
        "upload_tailored_resume": False,
        "keep_open": False,
        "reuse_session": False,
        "headful": False,
        "headless": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestScoringPipeline(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.memory_root = self.base / "memory"
        self.ledger_path = self.base / "job_ledger.json"
        _write_memory_root(self.memory_root)

        self.original_jobledger = applier.JobLedger
        self.original_scorer = applier.JobScorer
        self.original_scrape = applier.scrape_job_descriptions_batch
        self.original_run_single = applier.run_single_application
        self.original_question_answerer = applier.QuestionAnswerer

        applier.JobLedger = lambda: JobLedger(self.ledger_path)

    def tearDown(self):
        applier.JobLedger = self.original_jobledger
        applier.JobScorer = self.original_scorer
        applier.scrape_job_descriptions_batch = self.original_scrape
        applier.run_single_application = self.original_run_single
        applier.QuestionAnswerer = self.original_question_answerer
        self.tempdir.cleanup()

    def test_run_score_unscored_recovers_missing_jd_before_scoring(self):
        ledger = JobLedger(self.ledger_path)
        ledger.add_job(
            url="https://jobs.example.com/backend-1",
            company="Acme",
            role="Backend Engineer Intern",
            location="New York, NY",
            category="software engineering",
            age_hours=3.0,
        )
        ledger.save()

        async def fake_scrape(urls, **kwargs):
            return {url: "Python FastAPI SQL backend services." for url in urls}

        class FakeScorer:
            def score_job(self, **kwargs):
                self.last_kwargs = kwargs
                return ScoredJob(
                    url="",
                    company=kwargs["company"],
                    role=kwargs["role"],
                    score=89.0,
                    breakdown={"confidence": 81.0},
                    reasoning="Strong backend overlap.",
                    recommendation="high_priority",
                )

        applier.scrape_job_descriptions_batch = fake_scrape
        applier.JobScorer = FakeScorer

        result = asyncio.run(applier.run_score_unscored(_make_args(), self.memory_root))
        self.assertEqual(result["status"], "scoring_complete")

        refreshed = JobLedger(self.ledger_path).get("https://jobs.example.com/backend-1")
        self.assertIsNotNone(refreshed)
        self.assertTrue(refreshed.jd_scraped)
        self.assertIn("FastAPI", refreshed.job_description)
        self.assertEqual(refreshed.score, 89.0)

    def test_run_apply_top_scored_keeps_failed_jobs_retryable(self):
        ledger = JobLedger(self.ledger_path)
        ledger.add_job(
            url="https://jobs.example.com/backend-2",
            company="Acme",
            role="Backend Engineer Intern",
            location="Remote",
            category="software engineering",
            age_hours=2.0,
        )
        ledger.update_score(
            "https://jobs.example.com/backend-2",
            score=91.0,
            breakdown={"confidence": 80.0},
            reasoning="Great fit",
            recommendation="high_priority",
        )
        ledger.save()

        async def fake_run_single_application(**kwargs):
            return {"status": "error", "error": "captcha_blocked"}

        applier.run_single_application = fake_run_single_application
        applier.QuestionAnswerer = lambda: object()

        result = asyncio.run(applier.run_apply_top_scored(_make_args(max_jobs=1), self.memory_root))
        self.assertEqual(result["status"], "apply_batch_complete")

        refreshed = JobLedger(self.ledger_path).get("https://jobs.example.com/backend-2")
        self.assertIsNotNone(refreshed)
        self.assertFalse(refreshed.applied)
        self.assertEqual(refreshed.apply_status, "error")


if __name__ == "__main__":
    unittest.main()
