"""Tests for the scorer web UI backend helpers.

Run with: python3 -m tests.test_web_ui
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import openclaw.web_ui as web_ui
from openclaw.scoring.ledger import JobLedger


class DummyHandler(web_ui.JobScorerHandler):
    def __init__(self):
        pass

    def send_json(self, data, status=200):
        self.payload = data
        self.status_code = status


class TestWebUiBackend(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.ledger_path = Path(self.tempdir.name) / "job_ledger.json"
        self.original_jobledger = web_ui.JobLedger
        web_ui.JobLedger = lambda: JobLedger(self.ledger_path)

    def tearDown(self):
        web_ui.JobLedger = self.original_jobledger
        self.tempdir.cleanup()

    def test_handle_get_jobs_returns_real_stats_and_breakdown(self):
        ledger = JobLedger(self.ledger_path)
        ledger.add_job(
            url="https://jobs.example.com/backend-1",
            company="Acme",
            role="Backend Engineer Intern",
            location="Remote",
            category="software engineering",
            age_hours=1.0,
        )
        ledger.add_job(
            url="https://jobs.example.com/ml-1",
            company="Beta",
            role="ML Intern",
            location="Seattle, WA",
            category="machine learning",
            age_hours=48.0,
        )
        ledger.update_score(
            "https://jobs.example.com/backend-1",
            score=88.0,
            breakdown={"confidence": 80.0, "experience_match": 90.0},
            reasoning="Strong fit",
            recommendation="high_priority",
        )
        ledger.save()

        handler = DummyHandler()
        handler.handle_get_jobs(
            {
                "min_score": ["0"],
                "unapplied_only": ["false"],
                "max_post_age_hours": ["0"],
            }
        )
        self.assertEqual(handler.status_code, 200)
        self.assertEqual(handler.payload["stats"]["unscored"], 1)
        self.assertEqual(handler.payload["stats"]["high_priority"], 1)
        self.assertEqual(len(handler.payload["jobs"]), 1)
        self.assertEqual(handler.payload["jobs"][0]["score_breakdown"]["confidence"], 80.0)

    def test_job_within_age_limit(self):
        self.assertTrue(web_ui._job_within_age_limit("2026-03-09T10:00:00+00:00", None, 1000))
        self.assertFalse(web_ui._job_within_age_limit(None, None, 24))


if __name__ == "__main__":
    unittest.main()
