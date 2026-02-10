from __future__ import annotations

import html
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterable

from openclaw.utils import detect_ats


logger = logging.getLogger(__name__)

SIMPLIFY_README_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md"
)


@dataclass(slots=True)
class SimplifyRole:
    company: str
    role: str
    location: str
    apply_url: str
    simplify_url: str | None
    age: str
    category: str
    age_hours: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "company": self.company,
            "role": self.role,
            "location": self.location,
            "apply_url": self.apply_url,
            "simplify_url": self.simplify_url,
            "age": self.age,
            "category": self.category,
            "age_hours": self.age_hours,
        }


def fetch_simplify_roles(
    *,
    category: str | None = "software engineering",
    company_keyword: str | None = None,
    role_keyword: str | None = None,
    max_age_hours: float | None = None,
    limit: int | None = None,
    include_unknown_ats: bool = True,
    timeout: int = 25,
) -> list[SimplifyRole]:
    markdown = _fetch_readme(timeout=timeout)
    roles = _parse_roles(markdown)
    roles = _filter_roles(
        roles,
        category=category,
        company_keyword=company_keyword,
        role_keyword=role_keyword,
        max_age_hours=max_age_hours,
        include_unknown_ats=include_unknown_ats,
    )
    roles.sort(key=lambda item: item.age_hours if item.age_hours is not None else 10_000.0)
    if limit is not None and limit > 0:
        roles = roles[:limit]
    return roles


def _fetch_readme(*, timeout: int) -> str:
    request = urllib.request.Request(
        SIMPLIFY_README_URL,
        headers={"User-Agent": "openclaw-autoapplier/0.1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return payload.decode("utf-8", errors="replace")


def _parse_roles(markdown: str) -> list[SimplifyRole]:
    roles: list[SimplifyRole] = []
    section_pattern = re.compile(r"^##\s+(.+?)\n(.*?)(?=^##\s+|\Z)", re.M | re.S)
    table_pattern = re.compile(r"<table>(.*?)</table>", re.S)
    row_pattern = re.compile(r"<tr>(.*?)</tr>", re.S)
    cell_pattern = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
    href_pattern = re.compile(r'href="([^"]+)"')

    for title, body in section_pattern.findall(markdown):
        if "internship" not in title.lower():
            continue
        table_match = table_pattern.search(body)
        if not table_match:
            continue

        category = _normalize_category(title)
        table_html = table_match.group(1)
        last_company = ""

        for row_html in row_pattern.findall(table_html):
            if "<th" in row_html.lower():
                continue

            cells = cell_pattern.findall(row_html)
            if len(cells) < 5:
                continue

            company_cell, role_cell, location_cell, app_cell, age_cell = cells[:5]
            raw_company = _clean_text(company_cell)
            company = _normalize_company(raw_company, last_company=last_company)
            if company and company != "↳":
                last_company = company
            elif last_company:
                company = last_company

            role = _clean_text(role_cell)
            location = _clean_text(location_cell)
            age = _clean_text(age_cell)

            hrefs = href_pattern.findall(app_cell)
            apply_url = _pick_apply_url(hrefs)
            simplify_url = _pick_simplify_url(hrefs)
            if not apply_url:
                continue

            role_entry = SimplifyRole(
                company=company or "Unknown",
                role=role,
                location=location,
                apply_url=apply_url,
                simplify_url=simplify_url,
                age=age,
                category=category,
                age_hours=_age_to_hours(age),
            )
            roles.append(role_entry)

    return roles


def _filter_roles(
    roles: Iterable[SimplifyRole],
    *,
    category: str | None,
    company_keyword: str | None,
    role_keyword: str | None,
    max_age_hours: float | None,
    include_unknown_ats: bool,
) -> list[SimplifyRole]:
    out: list[SimplifyRole] = []
    category_norm = (category or "").strip().lower()
    company_norm = (company_keyword or "").strip().lower()
    role_norm = (role_keyword or "").strip().lower()

    for role in roles:
        if category_norm and category_norm not in {"all", "*"}:
            if category_norm not in role.category.lower():
                continue
        if company_norm and company_norm not in role.company.lower():
            continue
        if role_norm and role_norm not in role.role.lower():
            continue
        if max_age_hours is not None and role.age_hours is not None and role.age_hours > max_age_hours:
            continue
        if not include_unknown_ats and detect_ats(role.apply_url).value == "generic":
            continue
        out.append(role)
    return out


def _normalize_category(title: str) -> str:
    title = re.sub(r"^[^A-Za-z0-9]+", "", title)
    title = re.sub(r"\bInternship Roles?\b", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" -")
    return title or "Unknown"


def _normalize_company(raw_company: str, *, last_company: str) -> str:
    text = raw_company.replace("🔥", "").replace("🛂", "").replace("🇺🇸", "").strip()
    if text in {"↳", "->"}:
        return last_company
    text = text.strip("-• ")
    return text


def _pick_apply_url(hrefs: list[str]) -> str | None:
    for href in hrefs:
        if "simplify.jobs/p/" in href:
            continue
        if "simplify.jobs/c/" in href:
            continue
        return _strip_tracking_params(href)
    return None


def _pick_simplify_url(hrefs: list[str]) -> str | None:
    for href in hrefs:
        if "simplify.jobs/p/" in href:
            return href
    return None


def _strip_tracking_params(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    keep = [(k, v) for (k, v) in params if not (k.lower().startswith("utm_") or k.lower() == "ref")]
    normalized = parsed._replace(query=urllib.parse.urlencode(keep))
    return urllib.parse.urlunsplit(normalized)


def _clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _age_to_hours(age: str) -> float | None:
    text = age.strip().lower()
    if not text:
        return None
    if text in {"new", "today", "0d"}:
        return 1.0

    patterns: list[tuple[str, float]] = [
        (r"(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes)$", 1 / 60),
        (r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours)$", 1),
        (r"(\d+(?:\.\d+)?)\s*(d|day|days)$", 24),
        (r"(\d+(?:\.\d+)?)\s*(w|week|weeks)$", 24 * 7),
        (r"(\d+(?:\.\d+)?)\s*(mo|month|months)$", 24 * 30),
    ]
    for pattern, multiplier in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1)) * multiplier
    return None
