from __future__ import annotations

from dataclasses import dataclass, field

from .base import BaseATSHandler


@dataclass(slots=True)
class GenericATSHandler(BaseATSHandler):
    ats_name: str = "generic"
    apply_prompts: list[str] = field(
        default_factory=lambda: [
            "Find and click the main Apply button on this page",
            "Click the button that starts the job application form",
        ]
    )
    submit_prompts: list[str] = field(
        default_factory=lambda: [
            "Find the final Submit Application button and click it",
        ]
    )
    field_prompts: dict[str, str] = field(
        default_factory=lambda: {
            "first_name": "First Name field",
            "last_name": "Last Name field",
            "full_name": "Full Name field",
            "email": "Email field",
            "phone": "Phone number field",
            "linkedin": "LinkedIn URL field",
            "github": "GitHub URL field",
            "school": "School/University field",
            "degree": "Degree field",
            "gpa": "GPA field",
            "graduation": "Graduation date field",
        }
    )
