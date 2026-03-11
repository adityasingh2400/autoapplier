"""Personal-first hybrid job scoring engine using AWS Bedrock Claude."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from openclaw.utils import ATSKind, detect_ats, is_top_tier_company


logger = logging.getLogger(__name__)

SCORER_VERSION = "personal-hybrid-v1"

STOPWORDS = {
    "about",
    "across",
    "after",
    "also",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "build",
    "by",
    "for",
    "from",
    "have",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "our",
    "that",
    "the",
    "their",
    "this",
    "to",
    "using",
    "with",
    "you",
    "your",
}

MAJOR_US_LOCATION_HINTS = (
    "san francisco",
    "bay area",
    "new york",
    "nyc",
    "seattle",
    "los angeles",
    "la, ca",
    "austin",
    "chicago",
    "boston",
    "denver",
    "atlanta",
    "washington, dc",
    "palo alto",
    "mountain view",
    "san jose",
)

ATS_FRICTION_SCORES = {
    ATSKind.LEVER: 90.0,
    ATSKind.GREENHOUSE: 85.0,
    ATSKind.ASHBY: 78.0,
    ATSKind.GENERIC: 62.0,
    ATSKind.WORKDAY: 42.0,
}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _clean_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _tokenize(text: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9+.#/-]{1,}", _normalize_text(text))
        if token not in STOPWORDS and len(token) >= 3
    }
    return tokens


def _score_to_recommendation(score: float) -> str:
    if score >= 79:
        return "high_priority"
    if score >= 64:
        return "medium"
    if score >= 48:
        return "low"
    return "skip"


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
    Personal-first scorer for ranking which jobs are worth your time next.

    Deterministic signals cover freshness, role alignment, location fit, company
    signal, and ATS friction. The LLM focuses on the hardest part: whether you
    can credibly tell a strong story for the role based on your experience.
    """

    region_name: str = os.getenv("OPENCLAW_BEDROCK_REGION", "us-east-1")
    model_id: str = os.getenv(
        "OPENCLAW_BEDROCK_MODEL_ID",
        "arn:aws:bedrock:us-east-1:128009599260:inference-profile/us.anthropic.claude-sonnet-4-6",
    )
    max_tokens: int = 800
    temperature: float = 0.2

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
        candidate_name: str = "",
        job_preferences: dict[str, Any] | None = None,
        job_url: str = "",
    ) -> ScoredJob:
        """Score a single job with deterministic + LLM signals."""
        preferences = job_preferences or {}
        url = ""
        deterministic = self._build_deterministic_breakdown(
            company=company,
            role=role,
            location=location,
            age_hours=age_hours,
            job_url=job_url,
            job_preferences=preferences,
        )

        try:
            llm_result = self._score_with_bedrock(
                company=company,
                role=role,
                location=location,
                job_description=job_description,
                profile_summary=profile_summary,
                resume_text=resume_text,
                age_hours=age_hours,
                candidate_name=candidate_name,
            )
            if llm_result is None:
                llm_result = self._fallback_experience_analysis(
                    role=role,
                    job_description=job_description,
                    profile_summary=profile_summary,
                    resume_text=resume_text,
                    job_preferences=preferences,
                )
        except Exception as exc:
            logger.warning("Scoring failed for %s @ %s: %s", role, company, exc)
            llm_result = self._fallback_experience_analysis(
                role=role,
                job_description=job_description,
                profile_summary=profile_summary,
                resume_text=resume_text,
                job_preferences=preferences,
            )
            llm_result["reasoning"] = (
                f"{llm_result.get('reasoning', '')} Bedrock failed, so this score uses local heuristics."
            ).strip()
            llm_result["error"] = str(exc)

        experience_match = _clamp(float(llm_result.get("experience_match", 50.0)))
        confidence = _clamp(float(llm_result.get("confidence", 55.0)))
        if not (job_description or "").strip():
            experience_match = min(experience_match, 58.0)
            confidence = min(confidence, 40.0)

        breakdown = dict(deterministic)
        breakdown["experience_match"] = experience_match
        breakdown["confidence"] = confidence

        score = self._combine_score(experience_match=experience_match, deterministic=deterministic, confidence=confidence)
        recommendation = _score_to_recommendation(score)
        reasoning = self._build_reasoning(
            llm_result=llm_result,
            deterministic=deterministic,
            confidence=confidence,
        )

        return ScoredJob(
            url=url,
            company=company,
            role=role,
            score=score,
            breakdown=breakdown,
            reasoning=reasoning,
            recommendation=recommendation,
            error=str(llm_result.get("error") or "") or None,
        )

    def score_from_breakdown(self, breakdown: dict[str, Any]) -> tuple[float, str]:
        """Recalculate score/recommendation from an existing hybrid breakdown."""
        deterministic = {
            "recency": self._coerce_number(breakdown.get("recency"), default=55.0) or 55.0,
            "role_alignment": self._coerce_number(breakdown.get("role_alignment"), default=60.0) or 60.0,
            "location": self._coerce_number(breakdown.get("location"), default=60.0) or 60.0,
            "company_signal": self._coerce_number(breakdown.get("company_signal"), default=60.0) or 60.0,
            "application_friction": self._coerce_number(breakdown.get("application_friction"), default=62.0) or 62.0,
        }
        experience_match = self._coerce_number(breakdown.get("experience_match"), default=50.0) or 50.0
        confidence = self._coerce_number(breakdown.get("confidence"), default=60.0) or 60.0
        score = self._combine_score(
            experience_match=experience_match,
            deterministic=deterministic,
            confidence=confidence,
        )
        return score, _score_to_recommendation(score)

    def _combine_score(
        self,
        *,
        experience_match: float,
        deterministic: dict[str, float],
        confidence: float,
    ) -> float:
        """
        Calibrate the final score for real job triage.

        The previous combiner was too stingy because it over-weighted application
        friction and treated neutral companies like a permanent drag. This version
        keeps strong-fit recent jobs in the 79-85 range instead of capping them in
        the mid-to-high 70s.
        """
        score = (
            experience_match * 0.44
            + deterministic["recency"] * 0.21
            + deterministic["role_alignment"] * 0.16
            + deterministic["location"] * 0.10
            + deterministic["company_signal"] * 0.06
            + deterministic["application_friction"] * 0.03
        )

        if confidence < 50:
            score -= (50 - confidence) * 0.08

        # Fresh, credible matches should feel meaningfully ahead of marginal ones.
        if experience_match >= 72 and deterministic["recency"] >= 90:
            score += 4.0
        if deterministic["role_alignment"] >= 78:
            score += 2.0

        return round(_clamp(score), 1)

    def _build_deterministic_breakdown(
        self,
        *,
        company: str,
        role: str,
        location: str,
        age_hours: float | None,
        job_url: str,
        job_preferences: dict[str, Any],
    ) -> dict[str, float]:
        return {
            "recency": self._score_recency(age_hours),
            "role_alignment": self._score_role_alignment(role, job_preferences),
            "location": self._score_location(location, job_preferences),
            "company_signal": self._score_company_signal(company, job_preferences),
            "application_friction": self._score_application_friction(job_url),
        }

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
        candidate_name: str,
    ) -> dict[str, Any] | None:
        """Call Bedrock Claude to analyze the experience fit portion of the score."""
        try:
            import boto3  # type: ignore
        except ImportError:
            logger.warning("boto3 not available for job scoring")
            return None

        jd_excerpt = (job_description or "").strip()[:8000]
        resume_excerpt = (resume_text or "").strip()[:6000]
        profile_excerpt = (profile_summary or "").strip()[:2500]
        age_str = f"{age_hours:.1f} hours" if age_hours is not None else "unknown"

        prompt = self._build_scoring_prompt(
            company=company,
            role=role,
            location=location,
            job_description=jd_excerpt,
            profile_summary=profile_excerpt,
            resume_text=resume_excerpt,
            age_str=age_str,
            candidate_name=candidate_name or "the candidate",
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
        candidate_name: str,
    ) -> str:
        """Build the prompt for Bedrock Claude."""
        return f"""You are helping rank which jobs {candidate_name} should spend time applying to next.

Your job is NOT to decide whether the job is prestigious. Your job is to judge whether {candidate_name} can credibly tell a strong story for this role based on the available experience, projects, education, and preferences.

Focus on:
1. Experience match: could {candidate_name} plausibly speak to the core requirements?
2. Evidence: what exact experiences, skills, coursework, or projects support the fit?
3. Risks: what looks weak, missing, or like a stretch?
4. Confidence: how confident are you in this judgment given the JD quality and resume detail?

Use the full range. Be harsh but useful.

## Candidate profile
{profile_summary if profile_summary else "[not available]"}

## Candidate resume
{resume_text if resume_text else "[not available]"}

## Job
Company: {company}
Role: {role}
Location: {location}
Posting age: {age_str}

## Job description
{job_description if job_description else "[No JD available. Lower confidence because the match signal is incomplete.]"}

Return ONLY valid JSON with this exact shape:
{{"experience_match": <0-100>, "confidence": <0-100>, "evidence": ["<short bullet>", "<short bullet>"], "risks": ["<short bullet>"], "reasoning": "<1-2 sentences on why this is or is not a good use of time>"}}"""

    def _parse_scoring_response(self, text: str) -> dict[str, Any] | None:
        """Parse the LLM's JSON response."""
        json_blob = self._extract_json_object(text)
        if not json_blob:
            logger.debug("No JSON found in scoring response")
            return None

        try:
            data = json.loads(json_blob)
        except json.JSONDecodeError as exc:
            logger.debug("Failed to parse scoring JSON: %s", exc)
            return None

        experience = self._coerce_number(data.get("experience_match"))
        confidence = self._coerce_number(data.get("confidence"), default=60.0)
        if experience is None:
            logger.debug("Invalid experience_match in scoring response: %s", data.get("experience_match"))
            return None

        evidence = [str(item).strip() for item in (data.get("evidence") or []) if str(item).strip()]
        risks = [str(item).strip() for item in (data.get("risks") or []) if str(item).strip()]
        return {
            "experience_match": experience,
            "confidence": confidence,
            "evidence": evidence[:3],
            "risks": risks[:3],
            "reasoning": str(data.get("reasoning") or "").strip(),
        }

    def _fallback_experience_analysis(
        self,
        *,
        role: str,
        job_description: str,
        profile_summary: str,
        resume_text: str,
        job_preferences: dict[str, Any],
    ) -> dict[str, Any]:
        source_text = "\n".join(part for part in (profile_summary, resume_text) if part)
        resume_tokens = _tokenize(source_text)
        jd_tokens = _tokenize(job_description)
        role_tokens = _tokenize(role)

        overlap = sorted(jd_tokens & resume_tokens)
        role_overlap = sorted(role_tokens & resume_tokens)

        if not jd_tokens:
            return {
                "experience_match": 50.0,
                "confidence": 35.0,
                "evidence": role_overlap[:3],
                "risks": ["No job description was available, so this is a lower-confidence heuristic score."],
                "reasoning": "There is not enough JD detail to judge the match confidently, so this score is conservative.",
            }

        denominator = max(8, min(len(jd_tokens), 28))
        overlap_ratio = len(overlap) / denominator
        experience = 36.0 + min(42.0, overlap_ratio * 120.0) + min(12.0, len(role_overlap) * 4.0)

        target_roles = _clean_list(job_preferences.get("target_roles"))
        if target_roles:
            experience += (self._score_role_alignment(role, job_preferences) - 55.0) * 0.18

        confidence = 38.0 + min(42.0, len(overlap) * 3.5)
        if len(jd_tokens) < 20:
            confidence -= 8.0

        risks: list[str] = []
        if len(overlap) < 4:
            risks.append("Only a small amount of skill overlap was found between the JD and your profile.")
        if not role_overlap:
            risks.append("The role title itself does not line up cleanly with your existing resume keywords.")

        evidence = overlap[:3] or role_overlap[:3]
        return {
            "experience_match": round(_clamp(experience), 1),
            "confidence": round(_clamp(confidence), 1),
            "evidence": evidence,
            "risks": risks[:3],
            "reasoning": "This score uses local overlap heuristics because Bedrock output was unavailable.",
        }

    def _build_reasoning(
        self,
        *,
        llm_result: dict[str, Any],
        deterministic: dict[str, float],
        confidence: float,
    ) -> str:
        notes: list[str] = []

        if deterministic["role_alignment"] >= 85:
            notes.append("role is very close to your stated targets")
        elif deterministic["role_alignment"] <= 45:
            notes.append("role is outside your usual targets")

        if deterministic["recency"] >= 85:
            notes.append("posting is still fresh")
        elif deterministic["recency"] <= 45:
            notes.append("posting is already relatively old")

        if deterministic["location"] >= 90:
            notes.append("location matches your preferences")
        elif deterministic["location"] <= 35:
            notes.append("location looks unattractive for your preferences")

        if deterministic["application_friction"] <= 50:
            notes.append("application flow is likely high-friction")

        evidence = [str(item).strip() for item in (llm_result.get("evidence") or []) if str(item).strip()]
        risks = [str(item).strip() for item in (llm_result.get("risks") or []) if str(item).strip()]
        lead = str(llm_result.get("reasoning") or "").strip()

        tail_parts: list[str] = []
        if evidence:
            tail_parts.append(f"Evidence: {', '.join(evidence[:2])}")
        if notes:
            tail_parts.append(f"Signals: {', '.join(notes[:2])}")
        if risks:
            tail_parts.append(f"Watchouts: {', '.join(risks[:2])}")
        if confidence < 50:
            tail_parts.append("Confidence is low because the evidence is incomplete or noisy")

        pieces = [part for part in (lead, ". ".join(tail_parts)) if part]
        return " ".join(pieces).strip() or "No reasoning available."

    def _score_recency(self, age_hours: float | None) -> float:
        if age_hours is None:
            return 55.0
        if age_hours <= 6:
            return 100.0
        if age_hours <= 24:
            return 90.0
        if age_hours <= 48:
            return 78.0
        if age_hours <= 72:
            return 65.0
        if age_hours <= 168:
            return 45.0
        return 25.0

    def _score_location(self, location: str, job_preferences: dict[str, Any]) -> float:
        location_norm = _normalize_text(location)
        preferred = [_normalize_text(item) for item in _clean_list(job_preferences.get("preferred_locations"))]
        avoid = [_normalize_text(item) for item in _clean_list(job_preferences.get("avoid_locations"))]

        if any(item and item in location_norm for item in avoid):
            return 20.0
        if any(item and item in location_norm for item in preferred):
            return 100.0
        if "remote" in location_norm:
            return 92.0
        if any(hint in location_norm for hint in MAJOR_US_LOCATION_HINTS):
            return 84.0
        if any(token in location_norm for token in ("united states", "usa", "u.s.", "hybrid", "onsite")):
            return 72.0
        if any(token in location_norm for token in ("canada", "india", "europe", "uk", "singapore")):
            return 32.0
        return 60.0 if location_norm else 55.0

    def _score_role_alignment(self, role: str, job_preferences: dict[str, Any]) -> float:
        role_norm = _normalize_text(role)
        role_tokens = _tokenize(role)
        target_roles = _clean_list(job_preferences.get("target_roles"))
        if not target_roles:
            if "intern" in role_norm and "engineer" in role_norm:
                return 82.0
            if "engineer" in role_norm or "developer" in role_norm:
                return 74.0
            return 62.0

        best = 0.0
        for target in target_roles:
            target_norm = _normalize_text(target)
            target_tokens = _tokenize(target)
            if target_norm and (target_norm in role_norm or role_norm in target_norm):
                best = max(best, 1.0)
                continue
            union = role_tokens | target_tokens
            overlap = role_tokens & target_tokens
            if union:
                best = max(best, len(overlap) / len(union))

        return round(42.0 + best * 58.0, 1)

    def _score_company_signal(self, company: str, job_preferences: dict[str, Any]) -> float:
        company_norm = _normalize_text(company)
        target_companies = [_normalize_text(item) for item in _clean_list(job_preferences.get("target_companies"))]
        avoid_companies = [_normalize_text(item) for item in _clean_list(job_preferences.get("avoid_companies"))]

        if any(item and item in company_norm for item in avoid_companies):
            return 18.0
        if any(item and item in company_norm for item in target_companies):
            return 100.0
        if is_top_tier_company(company):
            return 82.0
        return 60.0 if company_norm else 50.0

    def _score_application_friction(self, job_url: str) -> float:
        if not job_url:
            return 62.0
        return ATS_FRICTION_SCORES.get(detect_ats(job_url), 62.0)

    def _coerce_number(self, value: Any, *, default: float | None = None) -> float | None:
        if isinstance(value, (int, float)):
            return round(_clamp(float(value)), 1)
        if isinstance(value, str):
            try:
                return round(_clamp(float(value.strip())), 1)
            except ValueError:
                return default
        return default

    def _extract_json_object(self, text: str) -> str | None:
        start_positions = [index for index, char in enumerate(text) if char == "{"]
        for start in start_positions:
            depth = 0
            for index in range(start, len(text)):
                char = text[index]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start : index + 1]
        return None


def score_jobs_batch(
    jobs: list[dict[str, Any]],
    *,
    profile_summary: str,
    resume_text: str,
    candidate_name: str = "",
    job_preferences: dict[str, Any] | None = None,
    scorer: JobScorer | None = None,
) -> list[ScoredJob]:
    """Score multiple jobs and return the scored results."""
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
            candidate_name=candidate_name,
            job_preferences=job_preferences,
            job_url=job.get("url", ""),
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
