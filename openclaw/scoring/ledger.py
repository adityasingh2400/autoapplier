"""Job ledger for tracking seen jobs and avoiding re-processing."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def _default_ledger_path() -> Path:
    """Default ledger location: ~/.openclaw/job_ledger.json"""
    return Path.home() / ".openclaw" / "job_ledger.json"


def _estimate_posted_at(first_seen: str, age_hours: float | None) -> str | None:
    """Estimate when a job was actually posted: first_seen minus age_hours_at_discovery."""
    if age_hours is None or not first_seen:
        return None
    try:
        from datetime import timedelta
        seen = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
        return (seen - timedelta(hours=age_hours)).isoformat()
    except Exception:
        return None


def url_hash(url: str) -> str:
    """Generate a short hash for a URL to use as a key."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


@dataclass(slots=True)
class JobEntry:
    """A single job entry in the ledger."""
    url_hash: str
    url: str
    company: str
    role: str
    location: str
    category: str
    first_seen: str  # ISO timestamp
    age_hours_at_discovery: float | None
    posted_at: str | None = None  # ISO timestamp — estimated actual posting date
    score: float | None = None
    score_breakdown: dict[str, float] = field(default_factory=dict)
    score_reasoning: str = ""
    recommendation: str = ""  # high_priority, medium, low, skip
    job_description: str = ""
    jd_scraped: bool = False
    applied: bool = False
    applied_at: str | None = None
    apply_status: str | None = None  # success, error, needs_review, etc.

    def to_dict(self) -> dict[str, Any]:
        return {
            "url_hash": self.url_hash,
            "url": self.url,
            "company": self.company,
            "role": self.role,
            "location": self.location,
            "category": self.category,
            "first_seen": self.first_seen,
            "age_hours_at_discovery": self.age_hours_at_discovery,
            "posted_at": self.posted_at,
            "score": self.score,
            "score_breakdown": self.score_breakdown,
            "score_reasoning": self.score_reasoning,
            "recommendation": self.recommendation,
            "job_description": self.job_description,
            "jd_scraped": self.jd_scraped,
            "applied": self.applied,
            "applied_at": self.applied_at,
            "apply_status": self.apply_status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobEntry:
        return cls(
            url_hash=data.get("url_hash", ""),
            url=data.get("url", ""),
            company=data.get("company", ""),
            role=data.get("role", ""),
            location=data.get("location", ""),
            category=data.get("category", ""),
            first_seen=data.get("first_seen", ""),
            age_hours_at_discovery=data.get("age_hours_at_discovery"),
            posted_at=data.get("posted_at"),
            score=data.get("score"),
            score_breakdown=data.get("score_breakdown") or {},
            score_reasoning=data.get("score_reasoning", ""),
            recommendation=data.get("recommendation", ""),
            job_description=data.get("job_description", ""),
            jd_scraped=data.get("jd_scraped", False),
            applied=data.get("applied", False),
            applied_at=data.get("applied_at"),
            apply_status=data.get("apply_status"),
        )


class JobLedger:
    """
    Persistent ledger for tracking jobs we've seen, scored, and applied to.
    
    Provides deduplication so we only process new jobs on each run.
    """

    def __init__(self, ledger_path: Path | None = None):
        self.ledger_path = ledger_path or _default_ledger_path()
        self._jobs: dict[str, JobEntry] = {}
        self._load()

    def _load(self) -> None:
        """Load ledger from disk."""
        if not self.ledger_path.exists():
            logger.debug("Ledger file does not exist, starting fresh: %s", self.ledger_path)
            return

        try:
            data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
            jobs_data = data.get("jobs", {})
            for key, entry_data in jobs_data.items():
                entry = JobEntry.from_dict(entry_data)
                if entry.posted_at is None:
                    entry.posted_at = _estimate_posted_at(
                        entry.first_seen, entry.age_hours_at_discovery
                    )
                self._jobs[key] = entry
            logger.info("Loaded %d jobs from ledger: %s", len(self._jobs), self.ledger_path)
        except Exception as exc:
            logger.warning("Failed to load ledger, starting fresh: %s", exc)
            self._jobs = {}

    def save(self) -> None:
        """Persist ledger to disk."""
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "jobs": {key: entry.to_dict() for key, entry in self._jobs.items()},
        }
        self.ledger_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.debug("Saved %d jobs to ledger: %s", len(self._jobs), self.ledger_path)

    def has_seen(self, url: str) -> bool:
        """Check if we've already seen this job URL."""
        return url_hash(url) in self._jobs

    def get(self, url: str) -> JobEntry | None:
        """Get a job entry by URL."""
        return self._jobs.get(url_hash(url))

    def add_job(
        self,
        *,
        url: str,
        company: str,
        role: str,
        location: str,
        category: str,
        age_hours: float | None,
    ) -> JobEntry:
        """Add a new job to the ledger. Returns the entry (existing or new)."""
        key = url_hash(url)
        if key in self._jobs:
            return self._jobs[key]

        now_iso = datetime.now(timezone.utc).isoformat()
        entry = JobEntry(
            url_hash=key,
            url=url,
            company=company,
            role=role,
            location=location,
            category=category,
            first_seen=now_iso,
            age_hours_at_discovery=age_hours,
            posted_at=_estimate_posted_at(now_iso, age_hours),
        )
        self._jobs[key] = entry
        logger.debug("Added new job to ledger: %s @ %s", role, company)
        return entry

    def update_score(
        self,
        url: str,
        *,
        score: float,
        breakdown: dict[str, float],
        reasoning: str,
        recommendation: str,
    ) -> None:
        """Update the score for a job."""
        entry = self.get(url)
        if entry is None:
            logger.warning("Cannot update score for unknown job: %s", url)
            return
        entry.score = score
        entry.score_breakdown = breakdown
        entry.score_reasoning = reasoning
        entry.recommendation = recommendation

    def update_jd(self, url: str, job_description: str) -> None:
        """Update the job description for a job."""
        entry = self.get(url)
        if entry is None:
            logger.warning("Cannot update JD for unknown job: %s", url)
            return
        entry.job_description = job_description
        entry.jd_scraped = True

    def mark_applied(self, url: str, status: str) -> None:
        """Mark a job as applied."""
        entry = self.get(url)
        if entry is None:
            logger.warning("Cannot mark applied for unknown job: %s", url)
            return
        entry.applied = True
        entry.applied_at = datetime.now(timezone.utc).isoformat()
        entry.apply_status = status

    def get_new_jobs(self, urls: list[str]) -> list[str]:
        """Filter a list of URLs to only those we haven't seen."""
        return [url for url in urls if not self.has_seen(url)]

    def get_unscored_jobs(self) -> list[JobEntry]:
        """Get all jobs that haven't been scored yet."""
        return [entry for entry in self._jobs.values() if entry.score is None]

    def get_scored_jobs(
        self,
        *,
        min_score: float | None = None,
        max_score: float | None = None,
        unapplied_only: bool = False,
    ) -> list[JobEntry]:
        """Get scored jobs, optionally filtered."""
        results = []
        for entry in self._jobs.values():
            if entry.score is None:
                continue
            if min_score is not None and entry.score < min_score:
                continue
            if max_score is not None and entry.score > max_score:
                continue
            if unapplied_only and entry.applied:
                continue
            results.append(entry)
        return results

    def get_top_jobs(
        self,
        *,
        limit: int = 10,
        min_score: float | None = None,
        unapplied_only: bool = True,
    ) -> list[JobEntry]:
        """Get top-scored jobs, sorted by score descending."""
        jobs = self.get_scored_jobs(min_score=min_score, unapplied_only=unapplied_only)
        jobs.sort(key=lambda j: j.score or 0, reverse=True)
        return jobs[:limit]

    def stats(self) -> dict[str, int]:
        """Get ledger statistics."""
        total = len(self._jobs)
        scored = sum(1 for j in self._jobs.values() if j.score is not None)
        applied = sum(1 for j in self._jobs.values() if j.applied)
        high_priority = sum(
            1 for j in self._jobs.values()
            if j.recommendation == "high_priority"
        )
        return {
            "total_jobs": total,
            "scored": scored,
            "unscored": total - scored,
            "applied": applied,
            "unapplied": total - applied,
            "high_priority": high_priority,
        }

    def prune_old_jobs(self, max_age_days: int = 30) -> int:
        """Remove jobs older than max_age_days. Returns count removed."""
        cutoff = datetime.now(timezone.utc)
        removed = 0
        keys_to_remove = []

        for key, entry in self._jobs.items():
            try:
                first_seen = datetime.fromisoformat(entry.first_seen.replace("Z", "+00:00"))
                age_days = (cutoff - first_seen).days
                if age_days > max_age_days:
                    keys_to_remove.append(key)
            except Exception:
                continue

        for key in keys_to_remove:
            del self._jobs[key]
            removed += 1

        if removed > 0:
            logger.info("Pruned %d old jobs from ledger", removed)

        return removed
