"""Tests for the job ledger.

Run with: python3 -m tests.test_ledger
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openclaw.scoring.ledger import JobLedger


class TestJobLedger(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.ledger_path = Path(self.tempdir.name) / "job_ledger.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_record_apply_result_only_marks_successful_jobs_as_applied(self):
        ledger = JobLedger(self.ledger_path)
        ledger.add_job(
            url="https://example.com/job-1",
            company="Acme",
            role="Backend Intern",
            location="Remote",
            category="software engineering",
            age_hours=2.0,
        )
        ledger.record_apply_result("https://example.com/job-1", "error")
        entry = ledger.get("https://example.com/job-1")
        self.assertIsNotNone(entry)
        self.assertFalse(entry.applied)
        self.assertEqual(entry.apply_status, "error")

        ledger.record_apply_result("https://example.com/job-1", "success")
        entry = ledger.get("https://example.com/job-1")
        self.assertTrue(entry.applied)
        self.assertEqual(entry.apply_status, "success")
        self.assertIsNotNone(entry.applied_at)

    def test_stats_include_unscored_and_recommendation_buckets(self):
        ledger = JobLedger(self.ledger_path)
        ledger.add_job(
            url="https://example.com/job-1",
            company="Acme",
            role="Backend Intern",
            location="Remote",
            category="software engineering",
            age_hours=2.0,
        )
        ledger.add_job(
            url="https://example.com/job-2",
            company="Beta",
            role="ML Intern",
            location="Seattle, WA",
            category="machine learning",
            age_hours=24.0,
        )
        ledger.update_score(
            "https://example.com/job-1",
            score=88.0,
            breakdown={"confidence": 80.0},
            reasoning="Strong fit",
            recommendation="high_priority",
        )
        stats = ledger.stats()
        self.assertEqual(stats["total_jobs"], 2)
        self.assertEqual(stats["scored"], 1)
        self.assertEqual(stats["unscored"], 1)
        self.assertEqual(stats["high_priority"], 1)
        self.assertEqual(stats["medium"], 0)
        self.assertEqual(stats["low"], 0)
        self.assertEqual(stats["skip"], 0)

    def test_get_by_hash_returns_entry(self):
        ledger = JobLedger(self.ledger_path)
        entry = ledger.add_job(
            url="https://example.com/job-1",
            company="Acme",
            role="Backend Intern",
            location="Remote",
            category="software engineering",
            age_hours=2.0,
        )
        fetched = ledger.get_by_hash(entry.url_hash)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.url, entry.url)


if __name__ == "__main__":
    unittest.main()
