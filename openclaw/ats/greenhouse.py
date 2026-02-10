from __future__ import annotations

from dataclasses import dataclass, field

from .base import BaseATSHandler


@dataclass(slots=True)
class GreenhouseHandler(BaseATSHandler):
    ats_name: str = "greenhouse"
    apply_prompts: list[str] = field(
        default_factory=lambda: ["Click the Apply button for this Greenhouse role"]
    )
    apply_selectors: list[str] = field(
        default_factory=lambda: [
            "#main a[data-mapped='true']",
            "#main button:has-text('Apply')",
            "a:has-text('Apply for this job')",
            "button:has-text('Apply for this job')",
            "#apply_button",
        ]
    )
    submit_prompts: list[str] = field(
        default_factory=lambda: ["Submit this Greenhouse application now"]
    )
    submit_selectors: list[str] = field(
        default_factory=lambda: [
            "#submit_app",
            "button:has-text('Submit Application')",
            "button[type='submit']",
        ]
    )
    resume_selectors: list[str] = field(
        default_factory=lambda: [
            "input[name='resume']",
            "input#resume",
            "input[type='file'][name*='resume']",
            "input[type='file']",
        ]
    )
    cover_letter_selectors: list[str] = field(
        default_factory=lambda: [
            "input[name='cover_letter']",
            "input#cover_letter",
            "input[type='file'][name*='cover']",
            "input[type='file'][id*='cover']",
        ]
    )
    field_prompts: dict[str, str] = field(
        default_factory=lambda: {
            "full_name": "Full Name field",
            "first_name": "First Name field",
            "last_name": "Last Name field",
            "email": "Email field",
            "phone": "Phone number field",
            "linkedin": "LinkedIn profile URL field",
            "github": "GitHub profile URL field",
            "school": "School or University field",
            "degree": "Degree field",
            "gpa": "GPA field",
            "graduation": "Graduation date field",
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
            "first_name": ["#first_name", "input[name='first_name']"],
            "last_name": ["#last_name", "input[name='last_name']"],
            "email": ["#email", "input[name='email']"],
            "phone": ["#phone", "input[name='phone']"],
            "linkedin": [
                "input[name*='linkedin']",
                "input[id*='linkedin']",
                "input[placeholder*='LinkedIn']",
            ],
            "github": [
                "input[name*='github']",
                "input[id*='github']",
                "input[placeholder*='GitHub']",
            ],
            "school": [
                "input[name*='school']",
                "input[name*='university']",
                "input[id*='school']",
            ],
            "degree": [
                "input[name*='degree']",
                "input[id*='degree']",
            ],
            "gpa": [
                "input[name*='gpa']",
                "input[id*='gpa']",
            ],
            "graduation": [
                "input[name*='graduation']",
                "input[name*='grad']",
                "input[id*='graduation']",
            ],
        }
    )
