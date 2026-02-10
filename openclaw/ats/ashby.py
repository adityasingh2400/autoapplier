from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

from .base import BaseATSHandler


@dataclass(slots=True)
class AshbyHandler(BaseATSHandler):
    ats_name: str = "ashby"
    apply_prompts: list[str] = field(
        default_factory=lambda: ["Click Apply for this Ashby role"]
    )
    apply_selectors: list[str] = field(
        default_factory=lambda: [
            "a:has-text('Apply')",
            "button:has-text('Apply')",
            "button:has-text('Apply now')",
        ]
    )
    submit_prompts: list[str] = field(
        default_factory=lambda: ["Submit this Ashby application"]
    )
    submit_selectors: list[str] = field(
        default_factory=lambda: [
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
            "button[type='submit']",
        ]
    )
    resume_selectors: list[str] = field(
        default_factory=lambda: [
            "input[type='file'][name*='resume']",
            "input[type='file'][id*='resume']",
            "input[type='file']",
        ]
    )
    field_prompts: dict[str, str] = field(
        default_factory=lambda: {
            "full_name": "Full Name field",
            "first_name": "First Name field",
            "last_name": "Last Name field",
            "email": "Email field",
            "phone": "Phone number field",
            "linkedin": "LinkedIn profile field",
            "github": "GitHub profile field",
            "school": "School field",
            "degree": "Degree field",
            "gpa": "GPA field",
            "graduation": "Graduation field",
        }
    )
    field_selectors: dict[str, list[str]] = field(
        default_factory=lambda: {
            "full_name": [
                "input[name='name']",
                "input[id='name']",
                "input[name*='full']",
                "input[id*='full']",
            ],
            "first_name": ["input[name*='first']", "input[id*='first']"],
            "last_name": ["input[name*='last']", "input[id*='last']"],
            "email": ["input[type='email']", "input[name='email']"],
            "phone": ["input[type='tel']", "input[name*='phone']"],
            "linkedin": ["input[name*='linkedin']", "input[id*='linkedin']"],
            "github": ["input[name*='github']", "input[id*='github']"],
            "school": ["input[name*='school']", "input[name*='university']"],
            "degree": ["input[name*='degree']"],
            "gpa": ["input[name*='gpa']"],
            "graduation": ["input[name*='graduation']", "input[name*='grad']"],
        }
    )

    def _canonical_job_url(self, job_url: str) -> str:
        parsed = urllib.parse.urlsplit(job_url)
        path = parsed.path or ""
        if path.rstrip("/").endswith("/application"):
            trimmed = path.rstrip("/")
            path = trimmed[: -len("/application")] or "/"
            parsed = parsed._replace(path=path)
        return urllib.parse.urlunsplit(parsed)
