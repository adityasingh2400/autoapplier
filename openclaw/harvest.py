from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from openclaw.answer_bank import normalize_text
from openclaw.utils import maybe_await, smart_click, smart_goto, utc_now_iso


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HarvestedField:
    label: str
    control_kind: str
    required: bool
    options: list[str]

    tag: str = ""
    typ: str = ""
    role: str = ""
    name: str = ""
    selector: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "control_kind": self.control_kind,
            "required": self.required,
            "options": list(self.options),
            "tag": self.tag,
            "type": self.typ,
            "role": self.role,
            "name": self.name,
            "selector": self.selector,
        }


async def extract_visible_form_fields(page: Any, *, limit: int = 220) -> list[HarvestedField]:
    """
    Extract a rough "form schema" from the current page:
    labels, control type, required, and options (for select/radio).

    This is deliberately heuristic: different ATS render label text in wildly different ways.
    """
    evaluate_fn = getattr(page, "evaluate", None)
    if evaluate_fn is None:
        return []

    script = r"""
    (limit) => {
      const normalize = (v) => (v || "").toLowerCase().replace(/\s+/g, " ").trim();

      const labelTextFor = (el) => {
        const parts = [];
        const aria = el.getAttribute("aria-label");
        if (aria) parts.push(aria);
        const labelledBy = el.getAttribute("aria-labelledby");
        if (labelledBy) {
          for (const id of labelledBy.split(/\s+/)) {
            const n = document.getElementById(id);
            if (n?.innerText) parts.push(n.innerText);
          }
        }
        if (el.id) {
          const byFor = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
          if (byFor?.innerText) parts.push(byFor.innerText);
        }
        const wrap = el.closest("label");
        if (wrap?.innerText) parts.push(wrap.innerText);
        let root = el.closest("fieldset,section,div,li,tr,td,dd,form");
        for (let i = 0; i < 2 && root; i += 1) {
          const legend = root.querySelector("legend");
          if (legend?.innerText) parts.push(legend.innerText);
          const labelLike = root.querySelector("label,h1,h2,h3,h4,p,strong,span,div");
          if (labelLike?.innerText) parts.push(labelLike.innerText);
          root = root.parentElement;
        }
        const placeholder = el.getAttribute("placeholder");
        if (placeholder) parts.push(placeholder);
        const name = el.getAttribute("name");
        if (name) parts.push(name);
        return parts.join(" ").replace(/\s+/g, " ").trim();
      };

      const cssPath = (el) => {
        if (!el) return "";
        if (el.id) return "#" + el.id;
        const name = el.getAttribute("name");
        if (name) return `${el.tagName.toLowerCase()}[name="${name.replace(/"/g, '\\"')}"]`;
        return "";
      };

      const isHidden = (el) => {
        if (!el) return true;
        const ariaHidden = normalize(el.getAttribute("aria-hidden") || "");
        if (ariaHidden === "true") return true;
        const style = window.getComputedStyle(el);
        if (style.display === "none" || style.visibility === "hidden") return true;
        const opacity = parseFloat(style.opacity || "1");
        if (!Number.isNaN(opacity) && opacity < 0.05) return true;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 1 || rect.height <= 1) return true;
        if (el.type && normalize(el.type) === "hidden") return true;
        return false;
      };

      const isRequired = (el) => {
        if (el.required) return true;
        const aria = el.getAttribute("aria-required");
        if (normalize(aria) === "true") return true;
        const cls = normalize(el.className || "");
        if (cls.includes("required")) return true;
        const label = normalize(labelTextFor(el));
        if (label.includes("*")) return true;
        if (label.includes("required")) return true;
        return false;
      };

      const uniqPush = (arr, value) => {
        const v = String(value || "").trim();
        if (!v) return;
        if (!arr.some((x) => String(x).trim() === v)) arr.push(v);
      };

      const results = [];

      // 1) Radio groups (grouped by name).
      const radios = Array.from(document.querySelectorAll("input[type='radio']"));
      const radioNames = Array.from(new Set(radios.map((r) => r.name || "").filter(Boolean)));
      for (const name of radioNames) {
        if (results.length >= limit) break;
        const group = radios.filter((r) => r.name === name);
        if (!group.length) continue;
        const first = group[0];
        if (first.disabled || first.readOnly) continue;
        if (isHidden(first)) continue;

        const options = [];
        for (const r of group) {
          const opt = (r.closest("label")?.innerText || r.value || "").trim();
          if (!opt) continue;
          // Avoid pulling the entire fieldset text for option labels.
          if (opt.length > 120) continue;
          uniqPush(options, opt);
        }

        const label = (labelTextFor(first) || "").trim();
        if (!label) continue;
        results.push({
          label: label.slice(0, 260),
          control_kind: "radio_group",
          required: isRequired(first),
          options,
          tag: (first.tagName || "").toLowerCase(),
          type: (first.type || "").toLowerCase(),
          role: normalize(first.getAttribute("role") || ""),
          name: name,
          selector: cssPath(first),
        });
      }

      // 2) Other controls: inputs + common ARIA-based controls (many ATS use div[role=combobox]/[role=textbox]).
      const rawControls = [
        ...document.querySelectorAll("input,textarea,select"),
        ...document.querySelectorAll("[role='textbox'],[role='combobox'],[role='checkbox']"),
        ...document.querySelectorAll("[contenteditable='true']")
      ];
      const controls = Array.from(new Set(rawControls));
      for (const el of controls) {
        if (results.length >= limit) break;
        if (el.disabled || el.readOnly) continue;
        if (isHidden(el)) continue;

        const tag = (el.tagName || "").toLowerCase();
        const type = normalize(el.type || "");
        if (type === "hidden" || type === "submit" || type === "button" || type === "reset" || type === "password") continue;
        if (type === "file") continue;
        if (type === "radio") continue; // already handled above

        const role = normalize(el.getAttribute("role") || "");
        let kind = tag;
        if (role === "combobox" || normalize(el.className || "").includes("select__input")) {
          kind = "combobox";
        } else if (type === "checkbox" || role === "checkbox") {
          kind = "checkbox";
        } else if (tag === "select") {
          kind = "select";
        } else if (tag === "textarea") {
          kind = "textarea";
        } else {
          kind = "input";
        }

        const label = (labelTextFor(el) || "").trim();
        if (!label) continue;

        const options = [];
        if (tag === "select") {
          for (const opt of Array.from(el.options || [])) {
            const text = String(opt.textContent || "").trim();
            if (!text) continue;
            uniqPush(options, text);
          }
        }

        results.push({
          label: label.slice(0, 260),
          control_kind: kind,
          required: isRequired(el),
          options,
          tag,
          type: (el.type || "").toLowerCase(),
          role,
          name: String(el.getAttribute("name") || ""),
          selector: cssPath(el),
        });
      }

      return results;
    }
    """

    def _parse_items(raw: object) -> list[HarvestedField]:
        out: list[HarvestedField] = []
        if not isinstance(raw, list):
            return out
        for item in raw:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            if not label:
                continue
            control_kind = str(item.get("control_kind") or "").strip() or "unknown"
            required = bool(item.get("required"))
            options_raw = item.get("options") or []
            options: list[str] = []
            if isinstance(options_raw, list):
                for opt in options_raw[:30]:
                    s = str(opt or "").strip()
                    if s and s not in options:
                        options.append(s)

            out.append(
                HarvestedField(
                    label=label,
                    control_kind=control_kind,
                    required=required,
                    options=options,
                    tag=str(item.get("tag") or ""),
                    typ=str(item.get("type") or ""),
                    role=str(item.get("role") or ""),
                    name=str(item.get("name") or ""),
                    selector=str(item.get("selector") or ""),
                )
            )
        return out

    # Try main frame.
    items: list[HarvestedField] = []
    try:
        raw = await maybe_await(evaluate_fn(script, limit))
        items.extend(_parse_items(raw))
    except Exception:
        pass

    # Also try iframes (Workday and many enterprise ATS embed fields in frames).
    frames_obj = getattr(page, "frames", None)
    frames: list[Any] = []
    try:
        if callable(frames_obj):
            frames = list(frames_obj())
        elif isinstance(frames_obj, list):
            frames = list(frames_obj)
    except Exception:
        frames = []

    for frame in frames:
        if frame is None:
            continue
        frame_eval = getattr(frame, "evaluate", None)
        if frame_eval is None:
            continue
        try:
            raw = await maybe_await(frame_eval(script, limit))
            items.extend(_parse_items(raw))
        except Exception:
            continue

    return items


async def harvest_job_posting_fields(
    page: Any,
    handler: Any,
    *,
    job_url: str,
    max_form_pages: int = 2,
    limit_per_page: int = 220,
) -> list[HarvestedField]:
    """
    Navigate to the job, open the apply form, and extract fields across a few pages.
    """
    start_url = getattr(handler, "_canonical_job_url", lambda u: u)(job_url)
    try:
        await smart_goto(page, start_url, wait_until="domcontentloaded", timeout_ms=60_000)
    except Exception:
        return []

    open_apply = getattr(handler, "_open_apply_form", None)
    if open_apply is not None:
        try:
            await maybe_await(open_apply(page))
        except Exception:
            # Some postings may already be on the application form.
            pass

    all_fields: list[HarvestedField] = []
    for _idx in range(max(max_form_pages, 1)):
        all_fields.extend(await extract_visible_form_fields(page, limit=limit_per_page))

        # Best-effort: try to proceed to the next step.
        next_selectors = getattr(handler, "next_selectors", None)
        try:
            clicked = await smart_click(
                page,
                prompt="If this application has multiple steps, click Next or Continue",
                selectors=next_selectors,
                text_candidates=["Next", "Continue", "Review", "Save and Continue"],
            )
        except Exception:
            clicked = False
        if not clicked:
            break

    return all_fields


def _is_sensitive_label(label_norm: str) -> bool:
    """
    Conservative heuristic for fields we generally don't want to auto-answer.
    Uses word-boundary matching for short tokens to avoid false positives (ex: "voyager" contains "age").
    """
    patterns = (
        r"citizenship",
        r"clearance",
        r"security\s+clearance",
        r"export",
        r"\bitar\b",
        r"veteran",
        r"disability",
        r"\brace\b",
        r"ethnicity",
        r"hispanic",
        r"latino",
        r"\bgender\b",
        r"sexual\s+orientation",
        r"\bage\b",
        r"date\s+of\s+birth",
        r"\bssn\b",
        r"social\s+security",
        r"criminal",
        r"felony",
    )
    return any(re.search(p, label_norm, flags=re.I) for p in patterns)


def suggest_bank_entry(field: HarvestedField) -> dict[str, object]:
    """
    Produce a profile.json `applicationDefaults.questionBank` item, including optional metadata.
    """
    label = field.label.strip()
    norm = normalize_text(label)

    patterns: list[str] = []
    answer: str = "__HUMAN__"
    notes: str = ""

    # Canonical patterns + safe defaults / placeholders.
    if "how did you hear" in norm or "where did you hear" in norm or re.search(r"\bsource\b", norm):
        patterns = ["how did you hear", "where did you hear", "source"]
        answer = "{how_heard}"
    elif re.search(r"\bcountry\b", norm):
        patterns = ["country"]
        answer = "__HUMAN__"
    elif "email" in norm:
        patterns = ["email"]
        answer = "{email}"
    elif "phone" in norm or "mobile" in norm:
        patterns = ["phone", "mobile"]
        answer = "{phone}"
    elif "linkedin" in norm:
        patterns = ["linkedin"]
        answer = "{linkedin}"
    elif "github" in norm:
        patterns = ["github"]
        answer = "{github}"
    elif "portfolio" in norm or "website" in norm or "personal site" in norm:
        patterns = ["portfolio", "website", "personal site"]
        notes = "Set to your portfolio URL (or keep __HUMAN__)."
        answer = "__HUMAN__"
    elif "first name" in norm:
        patterns = ["first name"]
        answer = "{first_name}"
    elif "last name" in norm:
        patterns = ["last name"]
        answer = "{last_name}"
    elif "full name" in norm or (norm == "name" or norm.endswith(" name")):
        patterns = ["full name", "name"]
        answer = "{full_name}"
    elif "university" in norm or "school" in norm or "institution" in norm:
        patterns = ["school", "university"]
        answer = "{school}"
    elif "degree" in norm:
        patterns = ["degree"]
        answer = "{degree}"
    elif "gpa" in norm:
        patterns = ["gpa"]
        answer = "{gpa}"
    elif "graduation" in norm or "grad date" in norm:
        patterns = ["graduation", "grad date"]
        answer = "{graduation}"
    elif "start date" in norm or "when can you start" in norm or "available to start" in norm:
        patterns = ["start date", "when can you start", "available to start"]
        answer = "Summer 2026"
    elif "salary" in norm or "compensation" in norm:
        patterns = ["salary expectation", "compensation expectation", "desired salary", "salary"]
        answer = "Open / Negotiable"
    elif "referral" in norm or "referred" in norm:
        patterns = ["referral", "referred"]
        answer = "No"
    elif "authorized to work" in norm or "work authorization" in norm or "eligible to work" in norm:
        patterns = ["work authorization", "authorized to work", "eligible to work"]
        answer = "__HUMAN__"
        notes = "Sensitive/legal. Fill explicitly once."
    elif "sponsorship" in norm or "visa" in norm:
        patterns = ["sponsorship", "visa"]
        answer = "__HUMAN__"
        notes = "Sensitive/legal. Fill explicitly once."
    elif "relocat" in norm:
        patterns = ["willing to relocate", "open to relocate", "relocation"]
        answer = "__HUMAN__"
    elif re.search(r"\bgender\b", norm):
        patterns = ["gender"]
        answer = "__HUMAN__"
        notes = "Sensitive field (EEO)."
    elif "hispanic" in norm or "latino" in norm:
        patterns = ["hispanic", "latino", "ethnicity"]
        answer = "__HUMAN__"
        notes = "Sensitive field (EEO)."
    elif "race" in norm or "ethnicity" in norm:
        patterns = ["race", "ethnicity"]
        answer = "__HUMAN__"
        notes = "Sensitive field (EEO)."
    elif "veteran" in norm:
        patterns = ["veteran"]
        answer = "__HUMAN__"
        notes = "Sensitive field (EEO)."
    elif "disability" in norm:
        patterns = ["disability"]
        answer = "__HUMAN__"
        notes = "Sensitive field (EEO)."
    elif "clearance" in norm or "citizenship" in norm or "itar" in norm or "export" in norm:
        patterns = ["citizenship", "clearance", "itar", "export control"]
        answer = "__HUMAN__"
        notes = "Sensitive/legal."
    elif _is_sensitive_label(norm):
        patterns = [re.sub(r"\\s+", " ", norm).strip()[:120]]
        answer = "__HUMAN__"
        notes = "Sensitive field."
    else:
        # Default: use a trimmed version of the label itself as the pattern so it won't accidentally match too broadly.
        cleaned = re.sub(r"[^a-z0-9 ]+", " ", norm).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        patterns = [cleaned[:120] if cleaned else label[:120]]
        answer = "__HUMAN__"

    # Ensure uniqueness + keep patterns short.
    seen: set[str] = set()
    final_patterns: list[str] = []
    for p in patterns:
        ps = str(p or "").strip()
        if not ps:
            continue
        key = normalize_text(ps)
        if not key or key in seen:
            continue
        seen.add(key)
        final_patterns.append(ps)
        if len(final_patterns) >= 6:
            break

    return {
        "patterns": final_patterns,
        "answer": answer,
        # Metadata for humans; the runtime parser ignores these keys.
        "control_kind": field.control_kind,
        "required": field.required,
        "options": field.options[:15],
        "example_label": label,
        "notes": notes,
    }


def merge_question_bank(existing: object, new_items: list[dict[str, object]]) -> list[dict[str, object]]:
    """
    Merge new questionBank items into an existing questionBank structure.

    - Avoid duplicates by pattern string (normalized).
    - Preserve existing order.
    """
    existing_list: list[dict[str, object]] = []
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, dict):
                existing_list.append(dict(item))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                existing_list.append({"pattern": str(item[0]), "answer": str(item[1])})
    elif isinstance(existing, dict):
        # Legacy: {pattern: answer}
        for k, v in existing.items():
            existing_list.append({"pattern": str(k), "answer": str(v)})

    seen_patterns: set[str] = set()
    for item in existing_list:
        pats: list[str] = []
        if isinstance(item.get("patterns"), list):
            pats.extend([str(p) for p in item.get("patterns") if str(p or "").strip()])
        if str(item.get("pattern") or "").strip():
            pats.append(str(item.get("pattern")))
        for p in pats:
            seen_patterns.add(normalize_text(p))

    out = list(existing_list)
    added = 0
    for item in new_items:
        pats = item.get("patterns")
        if not isinstance(pats, list) or not pats:
            continue
        normalized = [normalize_text(str(p)) for p in pats if str(p or "").strip()]
        if not normalized:
            continue
        if any(p in seen_patterns for p in normalized):
            continue
        out.append(item)
        for p in normalized:
            seen_patterns.add(p)
        added += 1

    logger.info("Merged %s new entries into question bank.", added)
    return out


def update_profile_answer_bank(profile_path: Path, new_items: list[dict[str, object]]) -> dict[str, object]:
    profile_data: dict[str, object] = {}
    try:
        profile_data = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        profile_data = {}

    defaults = profile_data.get("applicationDefaults")
    if not isinstance(defaults, dict):
        defaults = {}
        profile_data["applicationDefaults"] = defaults

    merged = merge_question_bank(defaults.get("questionBank"), new_items)
    defaults["questionBank"] = merged

    profile_path.write_text(json.dumps(profile_data, indent=2), encoding="utf-8")
    return profile_data


def write_harvest_report(
    out_path: Path,
    *,
    harvested: list[HarvestedField],
    suggested_items: list[dict[str, object]],
    jobs_sampled: list[dict[str, str]],
) -> None:
    payload = {
        "generated_at": utc_now_iso(),
        "jobs_sampled": jobs_sampled,
        "harvested_fields_count": len(harvested),
        "harvested_fields": [f.to_dict() for f in harvested],
        "suggested_question_bank_items": suggested_items,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
