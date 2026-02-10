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
    summary: str


def load_user_profile(memory_root: Path) -> UserProfile:
    profile_data = _read_json(memory_root / "profile.json")
    resume_data = _read_json(memory_root / "resume.json")
    resume_pdf_path = memory_root / "resume.pdf"
    resume_text = _extract_resume_text(resume_pdf_path)

    standard_fields = _build_standard_fields(profile_data, resume_data)
    question_bank = _build_question_bank(profile_data)
    summary = _build_summary(profile_data, resume_data, standard_fields)

    return UserProfile(
        profile=profile_data,
        resume=resume_data,
        resume_pdf_path=resume_pdf_path,
        resume_text=resume_text,
        standard_fields=standard_fields,
        question_bank=question_bank,
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
) -> str:
    identity = profile_data.get("identity") or {}
    headline = profile_data.get("headline") or ""
    skills = resume_data.get("skills") or profile_data.get("skills") or []
    skills_text = ", ".join(str(skill) for skill in skills[:12]) if skills else "not provided"

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
    ]
    return "\n".join(summary_parts)


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
