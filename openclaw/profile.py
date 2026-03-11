from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class UserProfile:
    profile: dict[str, Any]
    resume: dict[str, Any]
    resume_pdf_path: Path
    resume_text: str
    standard_fields: dict[str, str]
    question_bank: list[tuple[str, str]]
    job_preferences: dict[str, list[str] | str]
    summary: str


def load_user_profile(memory_root: Path) -> UserProfile:
    profile_data = _read_json(memory_root / "profile.json")
    resume_data = _read_json(memory_root / "resume.json")
    resume_pdf_path = memory_root / "resume.pdf"
    resume_text = _extract_resume_text(resume_pdf_path)

    standard_fields = _build_standard_fields(profile_data, resume_data)
    question_bank = _build_question_bank(profile_data)
    job_preferences = _build_job_preferences(profile_data)
    summary = _build_summary(profile_data, resume_data, standard_fields, job_preferences)

    return UserProfile(
        profile=profile_data,
        resume=resume_data,
        resume_pdf_path=resume_pdf_path,
        resume_text=resume_text,
        standard_fields=standard_fields,
        question_bank=question_bank,
        job_preferences=job_preferences,
        summary=summary,
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_standard_fields(profile_data: dict[str, Any], resume_data: dict[str, Any]) -> dict[str, str]:
    identity = profile_data.get("identity") or {}
    resume_contact = resume_data.get("contact") or {}
    education = resume_data.get("education") or []
    education0 = education[0] if education else {}

    full_name = str(identity.get("name") or "").strip()
    name_parts = [part for part in full_name.split() if part]
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    fields: dict[str, str] = {
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "email": str(identity.get("email") or "").strip(),
        "alternate_email": str(identity.get("alternateEmail") or identity.get("alternate_email") or "").strip(),
        "phone": str(
            resume_contact.get("phone")
            or identity.get("phone")
            or (resume_data.get("basics") or {}).get("phone")
            or ""
        ).strip(),
        "linkedin": str(identity.get("linkedin") or "").strip(),
        "github": str(identity.get("github") or "").strip(),
        "school": str(education0.get("institution") or "").strip(),
        "degree": str(education0.get("degree") or education0.get("studyType") or "").strip(),
        "gpa": str(education0.get("gpa") or "").strip(),
        "graduation": str(education0.get("graduationDate") or "").strip(),
    }
    return fields


def _build_summary(
    profile_data: dict[str, Any],
    resume_data: dict[str, Any],
    standard_fields: dict[str, str],
    job_preferences: dict[str, list[str] | str],
) -> str:
    identity = profile_data.get("identity") or {}
    headline = profile_data.get("headline") or ""
    skills = resume_data.get("skills") or profile_data.get("skills") or []
    skill_names: list[str] = []
    for skill in skills[:12]:
        if isinstance(skill, dict):
            keywords = [str(item).strip() for item in (skill.get("keywords") or []) if str(item).strip()]
            skill_names.extend(keywords[:4])
        elif str(skill).strip():
            skill_names.append(str(skill).strip())
    skills_text = ", ".join(skill_names[:12]) if skill_names else "not provided"
    highlights_text = _build_resume_highlights(resume_data)

    summary_parts = [
        f"Name: {standard_fields.get('full_name') or 'Unknown'}",
        f"Email: {standard_fields.get('email') or 'Unknown'}",
        f"Phone: {standard_fields.get('phone') or 'Unknown'}",
        f"Headline: {headline or 'Not provided'}",
        f"LinkedIn: {identity.get('linkedin') or 'Not provided'}",
        f"GitHub: {identity.get('github') or 'Not provided'}",
        f"Education: {standard_fields.get('school') or 'Not provided'} ({standard_fields.get('degree') or 'Unknown degree'})",
        f"Graduation: {standard_fields.get('graduation') or 'Not provided'}",
        f"Skills: {skills_text}",
        f"Preferred roles: {_display_list(job_preferences.get('target_roles'))}",
        f"Preferred locations: {_display_list(job_preferences.get('preferred_locations'))}",
        f"Avoid locations: {_display_list(job_preferences.get('avoid_locations'))}",
        f"Company preferences: {_display_list(job_preferences.get('company_types'))}",
        f"Target companies: {_display_list(job_preferences.get('target_companies'))}",
        f"Avoid companies: {_display_list(job_preferences.get('avoid_companies'))}",
        f"Resume highlights: {highlights_text}",
    ]
    return "\n".join(summary_parts)


def _build_job_preferences(profile_data: dict[str, Any]) -> dict[str, list[str] | str]:
    raw = profile_data.get("jobPreferences") or profile_data.get("job_preferences") or {}

    def _list(key: str) -> list[str]:
        value = raw.get(key) or []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    return {
        "target_roles": _list("targetRoles") or _list("target_roles"),
        "preferred_locations": _list("preferredLocations") or _list("preferred_locations"),
        "avoid_locations": _list("avoidLocations") or _list("avoid_locations"),
        "company_types": _list("companyTypes") or _list("company_types"),
        "target_companies": _list("targetCompanies") or _list("target_companies"),
        "avoid_companies": _list("avoidCompanies") or _list("avoid_companies"),
        "comp": str(raw.get("comp") or "").strip(),
    }


def _build_resume_highlights(resume_data: dict[str, Any]) -> str:
    highlights: list[str] = []

    basics = resume_data.get("basics") or {}
    summary = str(basics.get("summary") or "").strip()
    if summary:
        highlights.append(summary)

    work_entries = resume_data.get("work") or []
    for entry in work_entries[:3]:
        if not isinstance(entry, dict):
            continue
        position = str(entry.get("position") or "").strip()
        company = str(entry.get("name") or "").strip()
        item = " at ".join(part for part in (position, company) if part)
        if item:
            highlights.append(item)

    project_entries = resume_data.get("projects") or []
    for entry in project_entries[:2]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        description = str(entry.get("description") or entry.get("summary") or "").strip()
        if name and description:
            highlights.append(f"{name}: {description}")
        elif name:
            highlights.append(name)

    if not highlights:
        return "Not provided"
    return " | ".join(highlights[:5])


def _display_list(value: Any) -> str:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(items) if items else "Not provided"
    text = str(value or "").strip()
    return text or "Not provided"


def _build_question_bank(profile_data: dict[str, Any]) -> list[tuple[str, str]]:
    defaults = profile_data.get("applicationDefaults") or profile_data.get("application_defaults") or {}
    bank = defaults.get("questionBank") or defaults.get("question_bank") or []

    out: list[tuple[str, str]] = []
    if isinstance(bank, dict):
        items = list(bank.items())
    elif isinstance(bank, list):
        items = bank
    else:
        items = []

    for item in items:
        if isinstance(item, tuple | list) and len(item) >= 2:
            pattern = str(item[0] or "").strip()
            answer = str(item[1] or "").strip()
            if pattern and answer:
                out.append((pattern, answer))
            continue

        if isinstance(item, dict):
            answer = str(item.get("answer") or "").strip()
            if not answer:
                continue

            # Allow either a single pattern or a list of patterns for the same answer.
            patterns: list[str] = []
            if isinstance(item.get("patterns"), list):
                patterns = [str(p or "").strip() for p in item.get("patterns") if str(p or "").strip()]

            single = str(item.get("pattern") or item.get("needle") or "").strip()
            if single:
                patterns.append(single)

            for pattern in patterns:
                if pattern and answer:
                    out.append((pattern, answer))
            continue
    return out


def _extract_resume_text(resume_pdf_path: Path, *, limit_chars: int = 12_000) -> str:
    """
    Best-effort resume text extraction for higher-quality LLM answers.
    This should never hard-fail the applier if PDF parsing is unavailable.
    """
    if not resume_pdf_path.exists():
        return ""
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(resume_pdf_path))
        chunks: list[str] = []
        for page in reader.pages[:6]:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if text.strip():
                chunks.append(text.strip())
        out = "\n\n".join(chunks).strip()
        if len(out) > limit_chars:
            out = out[:limit_chars].rsplit("\n", 1)[0].strip()
        return out
    except Exception:
        return ""
