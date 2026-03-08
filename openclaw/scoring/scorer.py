"""LLM-powered job scoring engine using AWS Bedrock Claude."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ScoredJob:
    """Result of scoring a job."""
    url: str
    company: str
    role: str
    score: float  # 0-100
    breakdown: dict[str, float] = field(default_factory=dict)
    reasoning: str = ""
    recommendation: str = ""  # high_priority, medium, low, skip
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "company": self.company,
            "role": self.role,
            "score": self.score,
            "breakdown": self.breakdown,
            "reasoning": self.reasoning,
            "recommendation": self.recommendation,
            "error": self.error,
        }


@dataclass(slots=True)
class JobScorer:
    """
    Score jobs based on fit with candidate profile using LLM analysis.
    
    Considers:
    - Profile/resume fit (skills, experience, education)
    - Company quality (prestige, growth, intern conversion)
    - Role quality (specificity, growth potential)
    - Timing (freshness, competition level)
    """
    region_name: str = os.getenv("OPENCLAW_BEDROCK_REGION", "us-east-1")
    model_id: str = os.getenv(
        "OPENCLAW_BEDROCK_MODEL_ID", "arn:aws:bedrock:us-east-1:128009599260:inference-profile/us.anthropic.claude-sonnet-4-6"
    )
    max_tokens: int = 800
    temperature: float = 0.3

    def score_job(
        self,
        *,
        company: str,
        role: str,
        location: str,
        job_description: str,
        profile_summary: str,
        resume_text: str,
        age_hours: float | None,
    ) -> ScoredJob:
        """
        Score a single job. Returns a ScoredJob with score 0-100.
        """
        url = ""  # Will be set by caller
        
        try:
            result = self._score_with_bedrock(
                company=company,
                role=role,
                location=location,
                job_description=job_description,
                profile_summary=profile_summary,
                resume_text=resume_text,
                age_hours=age_hours,
            )
            if result:
                return ScoredJob(
                    url=url,
                    company=company,
                    role=role,
                    score=result.get("score", 50),
                    breakdown=result.get("breakdown", {}),
                    reasoning=result.get("reasoning", ""),
                    recommendation=result.get("recommendation", "medium"),
                )
        except Exception as exc:
            logger.warning("Scoring failed for %s @ %s: %s", role, company, exc)
            return ScoredJob(
                url=url,
                company=company,
                role=role,
                score=50,  # Default middle score on error
                error=str(exc),
            )

        # Fallback if LLM returns nothing
        return ScoredJob(
            url=url,
            company=company,
            role=role,
            score=50,
            reasoning="LLM scoring unavailable, using default score",
        )

    def _score_with_bedrock(
        self,
        *,
        company: str,
        role: str,
        location: str,
        job_description: str,
        profile_summary: str,
        resume_text: str,
        age_hours: float | None,
    ) -> dict[str, Any] | None:
        """Call Bedrock Claude to score the job."""
        try:
            import boto3  # type: ignore
        except ImportError:
            logger.warning("boto3 not available for job scoring")
            return None

        # Truncate inputs to manage token usage
        jd_excerpt = (job_description or "").strip()[:8000]
        resume_excerpt = (resume_text or "").strip()[:6000]
        profile_excerpt = (profile_summary or "").strip()[:2000]

        age_str = f"{age_hours:.1f} hours" if age_hours is not None else "unknown"

        prompt = self._build_scoring_prompt(
            company=company,
            role=role,
            location=location,
            job_description=jd_excerpt,
            profile_summary=profile_excerpt,
            resume_text=resume_excerpt,
            age_str=age_str,
        )

        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
        }

        try:
            client = boto3.client("bedrock-runtime", region_name=self.region_name)
            response = client.invoke_model(modelId=self.model_id, body=json.dumps(payload))
            body = response.get("body")
            if body is None:
                return None
            raw = body.read() if hasattr(body, "read") else body
            parsed = json.loads(raw)
            content = parsed.get("content") or []
            
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text") or "").strip()
                    if text:
                        return self._parse_scoring_response(text)
        except Exception as exc:
            logger.debug("Bedrock scoring call failed: %s", exc)
            return None

        return None

    def _build_scoring_prompt(
        self,
        *,
        company: str,
        role: str,
        location: str,
        job_description: str,
        profile_summary: str,
        resume_text: str,
        age_str: str,
    ) -> str:
        """Build the scoring prompt for the LLM."""
        return f"""Score this job for Aditya (0-100). Be harsh - use the full range.

## Scoring (these are the ONLY factors, in order of importance):

1. **Recency (30%)**: How old is the posting? 
   - 0-6 hours: 100 | 6-24 hours: 90 | 1-2 days: 75 | 2-3 days: 60 | 3+ days: 40 | 1 week+: 20
   - This job is {age_str} old.

2. **Experience Match (50%)**: Can Aditya credibly speak to this JD?
   - NOT "does his resume already say this" but "has he done something related he could talk about"
   - Look at his projects, coursework, work experience - could he adapt/reframe any of it?
   - 90+: Direct experience with core requirements
   - 70-89: Related experience he could reframe convincingly  
   - 50-69: Tangential experience, would need to stretch
   - <50: No relevant experience to draw from

3. **Location (20%)**: Is it a major US city?
   - Bay Area, NYC, Seattle, LA, Austin, Chicago, Boston, Denver, etc: 100
   - Other US cities: 80
   - Remote US: 90
   - International/visa issues: 30

## Candidate Resume
{resume_text if resume_text else "[not available]"}

## Job
Company: {company}
Role: {role}
Location: {location}

## Job Description
{job_description if job_description else "[No JD - score lower due to uncertainty]"}

Return ONLY JSON:
{{"score": <0-100>, "recency": <0-100>, "experience_match": <0-100>, "location": <0-100>, "reasoning": "<1-2 sentences, be specific about what experience he could leverage>"}}"""

    def _parse_scoring_response(self, text: str) -> dict[str, Any] | None:
        """Parse the LLM's JSON response."""
        json_match = re.search(r"\{[\s\S]*\}", text)
        if not json_match:
            logger.debug("No JSON found in scoring response")
            return None

        try:
            data = json.loads(json_match.group())
            
            score = data.get("score")
            if not isinstance(score, (int, float)) or score < 0 or score > 100:
                logger.debug("Invalid score in response: %s", score)
                return None

            # Build breakdown from new format
            breakdown = {
                "recency": data.get("recency", 50),
                "experience_match": data.get("experience_match", 50),
                "location": data.get("location", 50),
            }

            # Determine recommendation based on score
            if score >= 80:
                recommendation = "high_priority"
            elif score >= 60:
                recommendation = "medium"
            elif score >= 40:
                recommendation = "low"
            else:
                recommendation = "skip"

            return {
                "score": float(score),
                "breakdown": breakdown,
                "reasoning": str(data.get("reasoning", "")),
                "recommendation": recommendation,
            }
        except json.JSONDecodeError as exc:
            logger.debug("Failed to parse scoring JSON: %s", exc)
            return None


def score_jobs_batch(
    jobs: list[dict[str, Any]],
    *,
    profile_summary: str,
    resume_text: str,
    scorer: JobScorer | None = None,
) -> list[ScoredJob]:
    """
    Score multiple jobs. Each job dict should have:
    - url, company, role, location, job_description, age_hours
    
    Returns list of ScoredJob objects.
    """
    if scorer is None:
        scorer = JobScorer()

    results: list[ScoredJob] = []
    for job in jobs:
        scored = scorer.score_job(
            company=job.get("company", ""),
            role=job.get("role", ""),
            location=job.get("location", ""),
            job_description=job.get("job_description", ""),
            profile_summary=profile_summary,
            resume_text=resume_text,
            age_hours=job.get("age_hours"),
        )
        scored.url = job.get("url", "")
        results.append(scored)
        logger.info(
            "Scored: %s @ %s -> %.0f (%s)",
            scored.role[:30],
            scored.company[:20],
            scored.score,
            scored.recommendation,
        )

    return results
