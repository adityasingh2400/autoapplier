from __future__ import annotations

from dataclasses import dataclass, field

from .base import BaseATSHandler


@dataclass(slots=True)
class LeverHandler(BaseATSHandler):
    ats_name: str = "lever"
    apply_prompts: list[str] = field(
        default_factory=lambda: ["Click Apply for this Lever role"]
    )
    apply_selectors: list[str] = field(
        default_factory=lambda: [
            "a.postings-btn:has-text('Apply')",
            "a:has-text('Apply for this job')",
            "button:has-text('Apply')",
        ]
    )
    submit_prompts: list[str] = field(
        default_factory=lambda: ["Submit this Lever application"]
    )
    submit_selectors: list[str] = field(
        default_factory=lambda: [
            "button:has-text('Submit application')",
            "button:has-text('Submit Application')",
            "button[type='submit']",
        ]
    )
    resume_selectors: list[str] = field(
        default_factory=lambda: [
            "input[type='file'][name='resume']",
            "input[type='file'][name*='resume']",
            "input[type='file']",
        ]
    )
    field_prompts: dict[str, str] = field(
        default_factory=lambda: {
            "full_name": "Full Name field",
            "email": "Email field",
            "phone": "Phone field",
            "linkedin": "LinkedIn URL field",
            "github": "GitHub URL field",
            "school": "School field",
            "degree": "Degree field",
            "gpa": "GPA field",
            "graduation": "Graduation date field",
        }
    )
    field_selectors: dict[str, list[str]] = field(
        default_factory=lambda: {
            "full_name": ["input[name='name']", "input#name"],
            "first_name": ["input[name='firstname']", "input[name*='first']"],
            "last_name": ["input[name='lastname']", "input[name*='last']"],
            "email": ["input[name='email']", "input#email"],
            "phone": ["input[name='phone']", "input[type='tel']"],
            "linkedin": [
                "input[name*='linkedin']",
                "input[placeholder*='LinkedIn']",
            ],
            "github": [
                "input[name*='github']",
                "input[placeholder*='GitHub']",
            ],
            "school": ["input[name*='school']", "input[name*='university']"],
            "degree": ["input[name*='degree']"],
            "gpa": ["input[name*='gpa']"],
            "graduation": ["input[name*='graduation']", "input[name*='grad']"],
        }
    )
