from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openclaw.answer_bank import expand_placeholders, is_human_sentinel, normalize_text, match_question_bank, normalize_text_fuzzy
from openclaw.auth import maybe_auto_authenticate
from openclaw.captcha.detector import detect_captcha
from openclaw.captcha.solver import can_auto_solve, try_solve_captcha
from openclaw.profile import UserProfile
from openclaw.questions import QuestionAnswerer
from openclaw.utils import (
    build_application_id,
    capture_step,
    human_pause,
    maybe_await,
    smart_click,
    smart_fill,
    smart_goto,
    smart_upload,
    utc_now_iso,
)


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplyContext:
    job_url: str
    company: str
    role: str
    profile: UserProfile
    question_answerer: QuestionAnswerer
    output_dir: Path
    screenshots: list[str]
    dry_run: bool
    force_submit: bool
    is_top_tier: bool
    source: str | None = None
    job_description: str = ""
    quality: bool = False
    cover_letter_text: str = ""
    tailor_resume: bool = False
    upload_tailored_resume: bool = False
    resume_path_override: Path | None = None
    human_in_loop: bool = False
    max_custom_questions: int = 12
    max_form_pages: int = 3
    pause_on_captcha: bool = True
    pause_on_auth: bool = True
    pause_on_missing_fields: bool = True
    allow_captcha_auto_solve: bool = True


@dataclass(slots=True)
class BaseATSHandler:
    ats_name: str = "generic"
    apply_prompts: list[str] = field(default_factory=lambda: ["Click the Apply button"])
    apply_selectors: list[str] = field(
        default_factory=lambda: [
            "a:has-text('Apply')",
            "button:has-text('Apply')",
            "[data-qa*='apply']",
            "[aria-label*='Apply']",
        ]
    )
    submit_prompts: list[str] = field(default_factory=lambda: ["Submit application"])
    submit_selectors: list[str] = field(
        default_factory=lambda: [
            "button:has-text('Submit')",
            "button[type='submit']",
            "input[type='submit']",
        ]
    )
    next_selectors: list[str] = field(
        default_factory=lambda: [
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button:has-text('Review')",
        ]
    )
    resume_selectors: list[str] = field(
        default_factory=lambda: [
            "input[type='file'][accept*='pdf']",
            "input[type='file'][name*='resume']",
            "input[type='file'][id*='resume']",
            "input[type='file']",
        ]
    )
    cover_letter_selectors: list[str] = field(
        default_factory=lambda: [
            "input[type='file'][name*='cover']",
            "input[type='file'][id*='cover']",
            "input[type='file'][data-field*='cover']",
        ]
    )
    field_prompts: dict[str, str] = field(default_factory=dict)
    field_selectors: dict[str, list[str]] = field(default_factory=dict)
    _last_detected_page: str | None = field(default=None, repr=False)
    _pages_filled: set = field(default_factory=set, repr=False)

    async def apply(self, page: Any, ctx: ApplyContext) -> dict[str, Any]:
        fields_filled = 0
        custom_questions = 0

        start_url = self._canonical_job_url(ctx.job_url)
        logger.info("[%s] Navigating to job page: %s", ctx.company, start_url)
        await smart_goto(page, start_url, wait_until="domcontentloaded", timeout_ms=60_000)
        logger.info("[%s] Job page loaded.", ctx.company)
        if not ctx.job_description:
            logger.info("[%s] Extracting job description...", ctx.company)
            ctx.job_description = await self._extract_job_description(page)
            if ctx.job_description:
                logger.info("[%s] Job description extracted (%d chars).", ctx.company, len(ctx.job_description))
                try:
                    (ctx.output_dir / "job_description.txt").write_text(
                        ctx.job_description, encoding="utf-8"
                    )
                except Exception:
                    pass
            else:
                logger.warning("[%s] Could not extract job description.", ctx.company)
        if ctx.quality and not ctx.cover_letter_text:
            logger.info("[%s] Generating cover letter via LLM...", ctx.company)
            try:
                ctx.cover_letter_text = await ctx.question_answerer.cover_letter(
                    company=ctx.company,
                    role=ctx.role,
                    profile_summary=ctx.profile.summary,
                    resume_text=ctx.profile.resume_text,
                    job_description=ctx.job_description,
                )
                if ctx.cover_letter_text:
                    logger.info("[%s] Cover letter generated (%d chars).", ctx.company, len(ctx.cover_letter_text))
                    (ctx.output_dir / "cover_letter.txt").write_text(
                        ctx.cover_letter_text, encoding="utf-8"
                    )
            except Exception:
                logger.warning("[%s] Cover letter generation failed.", ctx.company, exc_info=True)
        if ctx.quality and ctx.tailor_resume and not ctx.resume_path_override and ctx.profile.resume_text.strip():
            logger.info("[%s] Tailoring resume via LLM...", ctx.company)
            try:
                tailored = await ctx.question_answerer.tailor_resume(
                    company=ctx.company,
                    role=ctx.role,
                    resume_text=ctx.profile.resume_text,
                    job_description=ctx.job_description,
                )
                if tailored.strip():
                    logger.info("[%s] Tailored resume generated (%d chars).", ctx.company, len(tailored))
                    (ctx.output_dir / "tailored_resume.txt").write_text(tailored, encoding="utf-8")
                    try:
                        from openclaw.documents import render_text_pdf

                        pdf_path = ctx.output_dir / "tailored_resume.pdf"
                        if render_text_pdf(pdf_path, tailored) and ctx.upload_tailored_resume:
                            ctx.resume_path_override = pdf_path
                            logger.info("[%s] Tailored resume PDF saved: %s", ctx.company, pdf_path)
                    except Exception:
                        pass
            except Exception:
                logger.warning("[%s] Resume tailoring failed.", ctx.company, exc_info=True)
        await capture_step(page, ctx.output_dir, "01-job-page", ctx.screenshots)
        logger.info("[%s] Screenshot: 01-job-page", ctx.company)

        logger.info("[%s] Checking for auth wall (pre-apply)...", ctx.company)
        auth0 = await maybe_auto_authenticate(
            page,
            job_url=ctx.job_url,
            standard_fields=ctx.profile.standard_fields,
            output_dir=ctx.output_dir,
            screenshots=ctx.screenshots,
            human_in_loop=ctx.human_in_loop,
            pause_on_auth=ctx.pause_on_auth,
            stage="01-auth",
        )
        if not auth0.get("ok", False):
            logger.warning("[%s] Auth wall detected pre-apply, cannot proceed.", ctx.company)
            shot = await capture_step(page, ctx.output_dir, "01-auth-blocked", ctx.screenshots)
            return {
                "status": "needs_review",
                "company": ctx.company,
                "role": ctx.role,
                "reason": auth0.get("reason") or "Sign-in or account creation required",
                "url": self._page_url(page) or ctx.job_url,
                "screenshot": shot,
                "fields_filled": fields_filled,
                "custom_questions": custom_questions,
                "timestamp": utc_now_iso(),
            }

        logger.info("[%s] Opening application form...", ctx.company)
        await self._open_apply_form(page)
        await capture_step(page, ctx.output_dir, "02-apply-open", ctx.screenshots)
        logger.info("[%s] Screenshot: 02-apply-open", ctx.company)

        logger.info("[%s] Checking for auth wall (post-apply-open)...", ctx.company)
        auth1 = await maybe_auto_authenticate(
            page,
            job_url=ctx.job_url,
            standard_fields=ctx.profile.standard_fields,
            output_dir=ctx.output_dir,
            screenshots=ctx.screenshots,
            human_in_loop=ctx.human_in_loop,
            pause_on_auth=ctx.pause_on_auth,
            stage="02-auth",
        )
        if not auth1.get("ok", False):
            logger.warning("[%s] Auth wall detected post-apply-open, cannot proceed.", ctx.company)
            shot = await capture_step(page, ctx.output_dir, "02-auth-blocked", ctx.screenshots)
            return {
                "status": "needs_review",
                "company": ctx.company,
                "role": ctx.role,
                "reason": auth1.get("reason") or "Sign-in or account creation required",
                "url": self._page_url(page) or ctx.job_url,
                "screenshot": shot,
                "fields_filled": fields_filled,
                "custom_questions": custom_questions,
                "timestamp": utc_now_iso(),
            }
        if auth1.get("performed"):
            # Many ATS redirect back to the job posting after auth; re-open the application form.
            await self._open_apply_form(page)
            await capture_step(page, ctx.output_dir, "02-apply-open-after-auth", ctx.screenshots)

        import asyncio as _asyncio

        resume_uploaded = False
        cover_letter_uploaded = False
        _cl_gen_task: _asyncio.Task | None = None  # background cover letter generation
        _cl_pdf_path: Path | None = None  # cached CL path once generated
        for page_index in range(max(ctx.max_form_pages, 1)):
            logger.info("[%s] === Form page %d/%d ===", ctx.company, page_index + 1, ctx.max_form_pages)

            # Kick off cover letter generation concurrently on the first page.
            # The LLM call + PDF render don't touch the browser, so they run safely
            # in parallel with form filling. The PDF will be ready for upload later.
            if _cl_gen_task is None and _cl_pdf_path is None and not cover_letter_uploaded:
                _cl_gen_task = _asyncio.create_task(self._generate_cover_letter_pdf(ctx))

            # --- Pre-fill special controls FIRST (e.g. Workday directory pickers, page-specific widgets) ---
            # This also detects the page type, and fills structured fields (work experience, education, skills)
            # before attempting file uploads (which may not exist on every page).
            await self._pre_fill_special_controls(page, ctx)

            if not resume_uploaded:
                resume_path = ctx.resume_path_override or ctx.profile.resume_pdf_path
                logger.info("[%s] Uploading resume: %s", ctx.company, resume_path)
                uploaded = await self._upload_resume_if_available(page, resume_path, ctx)
                fields_filled += uploaded
                resume_uploaded = uploaded > 0
                if resume_uploaded:
                    logger.info("[%s] Resume uploaded successfully.", ctx.company)
                else:
                    logger.debug("[%s] Resume upload: no file input found or upload skipped.", ctx.company)

            # Upload cover letter — await the background generation if still running
            if not cover_letter_uploaded:
                if _cl_gen_task is not None:
                    _cl_pdf_path = await _cl_gen_task
                    _cl_gen_task = None  # consumed
                elif _cl_pdf_path is None:
                    _cl_pdf_path = self._find_existing_cover_letter_pdf(ctx)
                if _cl_pdf_path:
                    uploaded = await smart_upload(
                        page,
                        prompt="Upload cover letter",
                        file_path=_cl_pdf_path,
                        selectors=self.cover_letter_selectors,
                    )
                    if uploaded:
                        logger.info("[%s] Cover letter uploaded successfully.", ctx.company)
                        cover_letter_uploaded = True
                    else:
                        logger.debug("[%s] No cover letter upload field found (may not be required).", ctx.company)

            # --- Unified snapshot-and-fill: one pass over all empty fields ---
            logger.info("[%s] Snapshot-and-fill: scanning all empty fields...", ctx.company)
            snap_count, snap_cq = await self._snapshot_and_fill_all_fields(page, ctx)
            fields_filled += snap_count
            custom_questions += snap_cq
            logger.info("[%s] Snapshot-and-fill complete: %d fields filled, %d custom Qs.", ctx.company, snap_count, snap_cq)

            logger.info("[%s] Trying to click Next/Continue for multi-page form...", ctx.company)
            next_clicked = await smart_click(
                page,
                prompt="If this application has multiple steps, click Next or Continue",
                selectors=self.next_selectors,
                text_candidates=["Next", "Continue", "Review", "Save and Continue"],
            )
            if not next_clicked:
                logger.info("[%s] No Next/Continue button found — assuming single-page form.", ctx.company)
                break
            logger.info("[%s] Clicked Next/Continue — advancing to page %d.", ctx.company, page_index + 2)
            await capture_step(page, ctx.output_dir, f"02-next-{page_index+1:02d}", ctx.screenshots)

            auth_mid = await maybe_auto_authenticate(
                page,
                job_url=ctx.job_url,
                standard_fields=ctx.profile.standard_fields,
                output_dir=ctx.output_dir,
                screenshots=ctx.screenshots,
                human_in_loop=ctx.human_in_loop,
                pause_on_auth=ctx.pause_on_auth,
                stage=f"02-auth-next-{page_index+1:02d}",
            )
            if not auth_mid.get("ok", False):
                shot = await capture_step(page, ctx.output_dir, "02-auth-blocked-midform", ctx.screenshots)
                return {
                    "status": "needs_review",
                    "company": ctx.company,
                    "role": ctx.role,
                    "reason": auth_mid.get("reason") or "Sign-in or account creation required",
                    "url": self._page_url(page) or ctx.job_url,
                    "screenshot": shot,
                    "fields_filled": fields_filled,
                    "custom_questions": custom_questions,
                    "timestamp": utc_now_iso(),
                }

        await capture_step(page, ctx.output_dir, "03-form-filled", ctx.screenshots)
        logger.info(
            "[%s] Form filling complete. Total fields filled: %d, custom questions: %d. Screenshot: 03-form-filled",
            ctx.company, fields_filled, custom_questions,
        )

        auth2 = await maybe_auto_authenticate(
            page,
            job_url=ctx.job_url,
            standard_fields=ctx.profile.standard_fields,
            output_dir=ctx.output_dir,
            screenshots=ctx.screenshots,
            human_in_loop=ctx.human_in_loop,
            pause_on_auth=ctx.pause_on_auth,
            stage="03-auth",
        )
        if not auth2.get("ok", False):
            shot = await capture_step(page, ctx.output_dir, "04-auth-required", ctx.screenshots)
            return {
                "status": "needs_review",
                "company": ctx.company,
                "role": ctx.role,
                "reason": auth2.get("reason") or "Sign-in or account creation required",
                "url": self._page_url(page) or ctx.job_url,
                "screenshot": shot,
                "fields_filled": fields_filled,
                "custom_questions": custom_questions,
                "timestamp": utc_now_iso(),
            }

        logger.info("[%s] Checking for missing required fields...", ctx.company)
        missing_required = await self._find_missing_required_fields(page)
        if missing_required:
            logger.warning("[%s] Found %d missing required field(s): %s", ctx.company, len(missing_required),
                           [m.get("label", m.get("name", "?")) for m in missing_required])
            shot = await capture_step(page, ctx.output_dir, "04-missing-required", ctx.screenshots)
            if ctx.human_in_loop and ctx.pause_on_missing_fields and sys.stdin.isatty():
                print(
                    "\nSome required fields are still empty.\n"
                    f"- Missing: {len(missing_required)}\n"
                    f"- Artifacts: {ctx.output_dir}\n"
                    "Fill the remaining required fields manually, then press Enter.\n"
                ,
                    file=sys.stderr,
                )
                await human_pause("Continue after filling required fields> ")
                missing_required = await self._find_missing_required_fields(page)
                if missing_required:
                    shot = await capture_step(
                        page, ctx.output_dir, "04-missing-required-still", ctx.screenshots
                    )
                    return {
                        "status": "needs_review",
                        "company": ctx.company,
                        "role": ctx.role,
                        "reason": "Missing required fields",
                        "url": self._page_url(page) or ctx.job_url,
                        "screenshot": shot,
                        "fields_filled": fields_filled,
                        "custom_questions": custom_questions,
                        "missing_required_fields": missing_required,
                        "timestamp": utc_now_iso(),
                    }
            else:
                return {
                    "status": "needs_review",
                    "company": ctx.company,
                    "role": ctx.role,
                    "reason": "Missing required fields",
                    "url": self._page_url(page) or ctx.job_url,
                    "screenshot": shot,
                    "fields_filled": fields_filled,
                    "custom_questions": custom_questions,
                    "missing_required_fields": missing_required,
                    "timestamp": utc_now_iso(),
                }

        logger.info("[%s] Checking for CAPTCHA...", ctx.company)
        captcha = await detect_captcha(page)
        if captcha.detected and ctx.allow_captcha_auto_solve and can_auto_solve() and captcha.site_key:
            logger.info("[%s] CAPTCHA detected (%s), attempting auto-solve...", ctx.company, captcha.captcha_type)
            token = try_solve_captcha(
                site_key=captcha.site_key,
                page_url=self._page_url(page) or ctx.job_url,
                captcha_type=captcha.captcha_type or "",
            )
            if token and await self._inject_captcha_token(page, token):
                captcha = await detect_captcha(page)

        if captcha.detected:
            logger.warning("[%s] CAPTCHA blocking submission: %s", ctx.company, captcha.captcha_type or "unknown")
            shot = await capture_step(page, ctx.output_dir, "04-captcha-detected", ctx.screenshots)
            if ctx.human_in_loop and ctx.pause_on_captcha and sys.stdin.isatty():
                print(
                    "\nCAPTCHA detected. Solve it manually, then press Enter (or type 'abort' to stop).\n"
                    f"- URL: {self._page_url(page) or ctx.job_url}\n"
                    f"- Artifacts: {ctx.output_dir}\n"
                ,
                    file=sys.stderr,
                )
                user_input = (await human_pause("Continue after CAPTCHA solve> ")).strip().lower()
                if user_input != "abort":
                    captcha = await detect_captcha(page)
                    if captcha.detected:
                        shot = await capture_step(
                            page, ctx.output_dir, "04-captcha-still-present", ctx.screenshots
                        )
                        return {
                            "status": "captcha_blocked",
                            "company": ctx.company,
                            "role": ctx.role,
                            "captcha_type": captcha.captcha_type or "unknown",
                            "url": self._page_url(page) or ctx.job_url,
                            "screenshot": shot,
                            "message": "CAPTCHA still detected after manual attempt",
                            "fields_filled": fields_filled,
                            "custom_questions": custom_questions,
                            "missing_required_fields": missing_required,
                            "timestamp": utc_now_iso(),
                        }
                else:
                    return {
                        "status": "captcha_blocked",
                        "company": ctx.company,
                        "role": ctx.role,
                        "captcha_type": captcha.captcha_type or "unknown",
                        "url": self._page_url(page) or ctx.job_url,
                        "screenshot": shot,
                        "message": "CAPTCHA detected - manual intervention needed",
                        "fields_filled": fields_filled,
                        "custom_questions": custom_questions,
                        "missing_required_fields": missing_required,
                        "timestamp": utc_now_iso(),
                    }
            else:
                return {
                    "status": "captcha_blocked",
                    "company": ctx.company,
                    "role": ctx.role,
                    "captcha_type": captcha.captcha_type or "unknown",
                    "url": self._page_url(page) or ctx.job_url,
                    "screenshot": shot,
                    "message": "CAPTCHA detected - manual intervention needed",
                    "fields_filled": fields_filled,
                    "custom_questions": custom_questions,
                    "missing_required_fields": missing_required,
                    "timestamp": utc_now_iso(),
                }

        if ctx.is_top_tier and not ctx.force_submit:
            logger.info("[%s] Top-tier company — flagging for manual review (use --force to override).", ctx.company)
            shot = await capture_step(page, ctx.output_dir, "04-review-required", ctx.screenshots)
            return {
                "status": "needs_review",
                "company": ctx.company,
                "role": ctx.role,
                "reason": "Top-tier company - manual review required",
                "url": self._page_url(page) or ctx.job_url,
                "screenshot": shot,
                "fields_filled": fields_filled,
                "custom_questions": custom_questions,
                "missing_required_fields": missing_required,
                "timestamp": utc_now_iso(),
            }

        if ctx.dry_run:
            logger.info("[%s] DRY RUN — form filled but not submitting. fields=%d, custom_q=%d",
                        ctx.company, fields_filled, custom_questions)
            await capture_step(page, ctx.output_dir, "04-dry-run-ready", ctx.screenshots)
            return {
                "status": "success",
                "company": ctx.company,
                "role": ctx.role,
                "url": self._page_url(page) or ctx.job_url,
                "application_id": build_application_id(ctx.company),
                "screenshots": ctx.screenshots,
                "fields_filled": fields_filled,
                "custom_questions": custom_questions,
                "missing_required_fields": missing_required,
                "submitted": False,
                "timestamp": utc_now_iso(),
            }

        logger.info("[%s] Submitting application...", ctx.company)
        submitted = await self._submit(page)
        await capture_step(page, ctx.output_dir, "04-submitted", ctx.screenshots)
        if not submitted:
            logger.error("[%s] Submit failed — button not found or click failed.", ctx.company)
            return {
                "status": "error",
                "company": ctx.company,
                "role": ctx.role,
                "error": "Submit button not found or submit failed",
                "url": self._page_url(page) or ctx.job_url,
                "screenshot": ctx.screenshots[-1] if ctx.screenshots else None,
                "timestamp": utc_now_iso(),
            }

        logger.info("[%s] Application submitted successfully!", ctx.company)
        return {
            "status": "success",
            "company": ctx.company,
            "role": ctx.role,
            "url": self._page_url(page) or ctx.job_url,
            "application_id": build_application_id(ctx.company),
            "screenshots": ctx.screenshots,
            "fields_filled": fields_filled,
            "custom_questions": custom_questions,
            "submitted": True,
            "timestamp": utc_now_iso(),
        }

    async def _open_apply_form(self, page: Any) -> None:
        for prompt in self.apply_prompts:
            clicked = await smart_click(
                page,
                prompt=prompt,
                selectors=self.apply_selectors,
                text_candidates=["Apply", "Apply Now", "Start Application"],
            )
            if clicked:
                return

    def _find_existing_cover_letter_pdf(self, ctx: Any) -> Path | None:
        """Return the path to an already-generated cover letter PDF, if it exists."""
        try:
            sf = ctx.profile.standard_fields
            first = sf.get("first_name", "").strip().replace(" ", "")
            last = sf.get("last_name", "").strip().replace(" ", "")
            company_slug = re.sub(r"[^A-Za-z0-9]+", "_", ctx.company).strip("_")
            filename = f"{first}_{last}_CoverLetter_{company_slug}.pdf" if first and last else "Cover_Letter.pdf"
            pdf_path = ctx.output_dir / filename
            if pdf_path.exists():
                return pdf_path
        except Exception:
            pass
        return None

    def _canonical_job_url(self, job_url: str) -> str:
        """
        Some sources link directly to an application route rather than the job posting.
        ATS handlers can override this to start on a page that contains the job description
        and an explicit Apply button.
        """
        return job_url

    async def _extract_job_description(self, page: Any, *, limit_chars: int = 12_000) -> str:
        evaluate_fn = getattr(page, "evaluate", None)
        if evaluate_fn is None:
            return ""
        script = """
        () => {
          const pick = () => {
            return (
              document.querySelector("[data-testid*='job-description']") ||
              document.querySelector("[data-qa*='job-description']") ||
              document.querySelector("main") ||
              document.querySelector("article") ||
              document.body
            );
          };
          const el = pick();
          const text = (el?.innerText || "").replace(/\\s+\\n/g, "\\n").trim();
          return text;
        }
        """
        try:
            text = await maybe_await(evaluate_fn(script))
            out = str(text or "").strip()
            if len(out) > limit_chars:
                out = out[:limit_chars]
            return out
        except Exception:
            return ""

    async def _fill_standard_fields(self, page: Any, standard_fields: dict[str, str]) -> int:
        count = 0
        prefilled = await self._extract_prefilled_values(page)
        if prefilled:
            logger.debug("  Prefilled values detected: %s", list(prefilled.keys()))

        # Build the ordered list of fields we want to fill.
        preferred_order = [
            "first_name",
            "last_name",
            "full_name",
            "email",
            "phone",
            "linkedin",
            "github",
            "school",
            "degree",
            "gpa",
            "graduation",
        ]
        seen: set[str] = set()
        ordered_items: list[tuple[str, str]] = []
        for key in preferred_order:
            if key in standard_fields:
                ordered_items.append((key, standard_fields.get(key, "")))
                seen.add(key)
        for key, value in standard_fields.items():
            if key not in seen:
                ordered_items.append((key, value))

        # Filter out fields we don't need to fill.
        to_fill: list[tuple[str, str]] = []
        for key, value in ordered_items:
            if not value:
                continue
            if prefilled.get(key):
                continue
            if (
                key == "full_name"
                and standard_fields.get("first_name")
                and standard_fields.get("last_name")
                and key not in self.field_selectors
            ):
                continue
            to_fill.append((key, value))

        if not to_fill:
            return 0

        # --- Scan-first: find all empty text/email/tel/url inputs on the page ---
        evaluate_fn = getattr(page, "evaluate", None)
        locator_fn = getattr(page, "locator", None)
        dom_fields: list[dict[str, str]] = []
        if evaluate_fn is not None:
            scan_script = """
            () => {
              const normalize = (v) => (v || "").toLowerCase().replace(/\\s+/g, " ").trim();
              const cssEscape = (v) => {
                try { return (window.CSS && CSS.escape) ? CSS.escape(String(v || "")) : String(v || ""); }
                catch (e) { return String(v || ""); }
              };
              const isHidden = (el) => {
                if (!el) return true;
                try {
                  const rect = el.getBoundingClientRect();
                  if (!rect || rect.width < 2 || rect.height < 2) return true;
                } catch (e) { return true; }
                let cur = el;
                for (let i = 0; i < 5 && cur; i += 1) {
                  try {
                    const ariaHidden = normalize(cur.getAttribute ? (cur.getAttribute("aria-hidden") || "") : "");
                    if (ariaHidden === "true") return true;
                    const style = window.getComputedStyle(cur);
                    if (style.display === "none" || style.visibility === "hidden") return true;
                  } catch (e) {}
                  cur = cur.parentElement;
                }
                return false;
              };
              const labelTextFor = (el) => {
                const pieces = [];
                const aria = el.getAttribute("aria-label");
                if (aria) pieces.push(aria);
                const labelledBy = el.getAttribute("aria-labelledby");
                if (labelledBy) {
                  for (const id of labelledBy.split(/\\s+/)) {
                    const n = document.getElementById(id);
                    if (n?.innerText) pieces.push(n.innerText);
                  }
                }
                if (el.id) {
                  const byFor = document.querySelector(`label[for="${cssEscape(el.id)}"]`);
                  if (byFor?.innerText) pieces.push(byFor.innerText);
                }
                const wrap = el.closest("label");
                if (wrap?.innerText) pieces.push(wrap.innerText);
                const placeholder = el.getAttribute("placeholder");
                if (placeholder) pieces.push(placeholder);
                const name = el.getAttribute("name");
                if (name) pieces.push(name);
                return pieces.join(" ").replace(/\\s+/g, " ").trim();
              };
              const selectorFor = (el) => {
                if (!el) return "";
                if (el.id) return "#" + cssEscape(el.id);
                const name = el.getAttribute("name");
                if (name) return `${el.tagName.toLowerCase()}[name="${cssEscape(name)}"]`;
                const parts = [];
                let cur = el;
                for (let i = 0; i < 5 && cur && cur.nodeType === 1; i += 1) {
                  const tag = cur.tagName.toLowerCase();
                  let index = 1;
                  let sib = cur.previousElementSibling;
                  while (sib) {
                    if (sib.tagName === cur.tagName) index += 1;
                    sib = sib.previousElementSibling;
                  }
                  parts.unshift(`${tag}:nth-of-type(${index})`);
                  cur = cur.parentElement;
                }
                return parts.join(" > ");
              };

              const out = [];
              const inputs = document.querySelectorAll("input, textarea, select");
              for (const el of inputs) {
                if (el.disabled || el.readOnly) continue;
                const tag = (el.tagName || "").toLowerCase();
                const type = normalize(el.type || "");
                if (type === "hidden" || type === "password" || type === "submit" ||
                    type === "button" || type === "reset" || type === "file" ||
                    type === "checkbox" || type === "radio") continue;
                if (isHidden(el)) continue;
                // Only include empty fields.
                if (tag === "select") {
                  // Select is "empty" if no option or first (placeholder) is selected.
                  const idx = el.selectedIndex;
                  const opt = el.options[idx];
                  const val = opt ? normalize(opt.value) : "";
                  if (val && val !== "" && idx > 0) continue;
                } else {
                  if ((el.value || "").trim()) continue;
                }
                const label = labelTextFor(el);
                if (!label) continue;
                const sel = selectorFor(el);
                if (!sel) continue;
                out.push({ label: normalize(label), selector: sel, tag, type: type || "text" });
              }
              return out;
            }
            """
            try:
                dom_fields = await maybe_await(evaluate_fn(scan_script))
                if not isinstance(dom_fields, list):
                    dom_fields = []
            except Exception:
                dom_fields = []

        if dom_fields and locator_fn:
            logger.debug("  DOM scan found %d empty input field(s) for standard field matching.", len(dom_fields))
            # Map standard field keys to keywords for matching.
            key_keywords: dict[str, list[str]] = {
                "first_name": ["first name", "first_name", "firstname", "given name"],
                "last_name": ["last name", "last_name", "lastname", "surname", "family name"],
                "full_name": ["full name", "full_name", "fullname", "your name", "name"],
                "email": ["email", "e-mail", "email address"],
                "phone": ["phone", "telephone", "mobile", "cell"],
                "linkedin": ["linkedin"],
                "github": ["github"],
                "school": ["school", "university", "college", "institution"],
                "degree": ["degree"],
                "gpa": ["gpa", "grade point"],
                "graduation": ["graduation", "grad date", "grad year"],
            }

            filled_keys: set[str] = set()
            for key, value in to_fill:
                keywords = key_keywords.get(key, [key.replace("_", " ")])
                best_field = None
                best_score = -1
                for df in dom_fields:
                    lbl = df.get("label", "")
                    for kw in keywords:
                        if kw in lbl:
                            score = len(kw)
                            if score > best_score:
                                best_score = score
                                best_field = df
                if best_field is not None:
                    sel = best_field["selector"]
                    try:
                        loc = locator_fn(sel).first
                        tag = best_field.get("tag", "")
                        if tag == "select":
                            select_opt_fn = getattr(loc, "select_option", None)
                            if select_opt_fn:
                                try:
                                    await maybe_await(select_opt_fn(label=value, timeout=1500))
                                    count += 1
                                    filled_keys.add(key)
                                    logger.debug("  Filled standard field (scan): %s = %s", key, value[:40])
                                    continue
                                except Exception:
                                    try:
                                        await maybe_await(select_opt_fn(value=value, timeout=1500))
                                        count += 1
                                        filled_keys.add(key)
                                        logger.debug("  Filled standard field (scan): %s = %s", key, value[:40])
                                        continue
                                    except Exception:
                                        pass
                        else:
                            await maybe_await(loc.fill(value, timeout=1500))
                            count += 1
                            filled_keys.add(key)
                            logger.debug("  Filled standard field (scan): %s = %s", key, value[:40])
                    except Exception:
                        logger.debug("  Failed to fill via scan: %s -> %s", key, sel[:40], exc_info=True)

            # Fallback: for fields not matched by scan, try ATS-specific selectors only.
            for key, value in to_fill:
                if key in filled_keys:
                    continue
                selectors = self.field_selectors.get(key)
                if not selectors:
                    continue
                prompt = self.field_prompts.get(key, key.replace("_", " ").title())
                if await smart_fill(page, prompt=prompt, value=value, selectors=selectors):
                    logger.debug("  Filled standard field (selector): %s = %s", key, value[:40] if len(value) > 40 else value)
                    count += 1
        else:
            # No DOM scan available, fall back to original smart_fill loop.
            for key, value in to_fill:
                prompt = self.field_prompts.get(key, key.replace("_", " ").title())
                selectors = self.field_selectors.get(key)
                if await smart_fill(page, prompt=prompt, value=value, selectors=selectors):
                    logger.debug("  Filled standard field: %s = %s", key, value[:40] if len(value) > 40 else value)
                    count += 1
        return count

    async def _extract_prefilled_values(self, page: Any) -> dict[str, str]:
        evaluate_fn = getattr(page, "evaluate", None)
        if evaluate_fn is None:
            return {}
        script = """
        () => {
          const out = {};
          const keys = [
            "first", "last", "name", "email", "phone",
            "linkedin", "github", "school", "university",
            "degree", "gpa", "graduation", "grad"
          ];

          const inputs = Array.from(document.querySelectorAll("input, textarea, select"));
          for (const el of inputs) {
            const name = (el.getAttribute("name") || "").toLowerCase();
            const id = (el.id || "").toLowerCase();
            const placeholder = (el.getAttribute("placeholder") || "").toLowerCase();
            const label = (
              (el.closest("label")?.innerText || "") +
              " " +
              (document.querySelector(`label[for='${el.id}']`)?.innerText || "")
            ).toLowerCase();
            const hay = `${name} ${id} ${placeholder} ${label}`;
            const value = ("value" in el ? (el.value || "") : "").trim();
            if (!value) continue;
            if (hay.includes("first")) out.first_name = value;
            if (hay.includes("last")) out.last_name = value;
            if (hay.includes("full") || hay.includes("name")) out.full_name = value;
            if (hay.includes("email")) out.email = value;
            if (hay.includes("phone")) out.phone = value;
            if (hay.includes("linkedin")) out.linkedin = value;
            if (hay.includes("github")) out.github = value;
            if (hay.includes("school") || hay.includes("university")) out.school = value;
            if (hay.includes("degree")) out.degree = value;
            if (hay.includes("gpa")) out.gpa = value;
            if (hay.includes("graduation") || hay.includes("grad")) out.graduation = value;
          }
          return out;
        }
        """
        try:
            result = await maybe_await(evaluate_fn(script))
            if isinstance(result, dict):
                return {str(k): str(v) for k, v in result.items() if str(v).strip()}
        except Exception:
            return {}
        return {}

    async def _upload_resume_if_available(self, page: Any, resume_pdf_path: Path, ctx: ApplyContext | None = None) -> int:
        if not resume_pdf_path.exists():
            logger.warning("Resume file does not exist: %s", resume_pdf_path)
            return 0

        # Create a properly named copy of the resume
        upload_path = resume_pdf_path
        if ctx is not None:
            try:
                sf = ctx.profile.standard_fields
                first = sf.get("first_name", "").strip().replace(" ", "")
                last = sf.get("last_name", "").strip().replace(" ", "")
                company_slug = re.sub(r"[^A-Za-z0-9]+", "_", ctx.company).strip("_")
                if first and last and company_slug:
                    named = f"{first}_{last}_Resume_{company_slug}.pdf"
                    named_path = ctx.output_dir / named
                    named_path.parent.mkdir(parents=True, exist_ok=True)
                    import shutil
                    shutil.copy2(str(resume_pdf_path), str(named_path))
                    upload_path = named_path
                    logger.info("[%s] Resume renamed: %s", ctx.company, named)
            except Exception:
                logger.debug("Resume rename failed, using original.", exc_info=True)

        uploaded = await smart_upload(
            page,
            prompt="Upload resume",
            file_path=upload_path,
            selectors=self.resume_selectors,
        )
        return 1 if uploaded else 0

    async def _generate_and_upload_cover_letter(self, page: Any, ctx: ApplyContext) -> bool:
        """
        Generate a high-quality cover letter via LLM, render to PDF with
        proper naming, and upload it to the form's cover letter field.
        Returns True if a cover letter was uploaded.
        """
        pdf_path = await self._generate_cover_letter_pdf(ctx)
        if not pdf_path:
            return False

        # Upload to form
        uploaded = await smart_upload(
            page,
            prompt="Upload cover letter",
            file_path=pdf_path,
            selectors=self.cover_letter_selectors,
        )
        if uploaded:
            logger.info("[%s] Cover letter uploaded successfully.", ctx.company)
        else:
            logger.debug("[%s] No cover letter upload field found (may not be required).", ctx.company)

        return uploaded

    async def _generate_cover_letter_pdf(self, ctx: ApplyContext) -> Path | None:
        """
        Generate a cover letter via LLM and render to PDF.
        Does NOT touch the browser — safe to run concurrently with form filling.
        Returns the PDF path, or None on failure.
        """
        logger.info("[%s] Generating tailored cover letter...", ctx.company)
        try:
            cover_text = await ctx.question_answerer.cover_letter(
                company=ctx.company,
                role=ctx.role,
                profile_summary=ctx.profile.summary,
                resume_text=ctx.profile.resume_text,
                job_description=ctx.job_description,
            )
        except Exception:
            logger.debug("Cover letter generation failed.", exc_info=True)
            return None

        if not cover_text or len(cover_text.strip()) < 50:
            logger.warning("[%s] Cover letter generation returned empty/short text.", ctx.company)
            return None

        logger.info("[%s] Cover letter generated (%d chars).", ctx.company, len(cover_text))

        # Build proper filename
        sf = ctx.profile.standard_fields
        first = sf.get("first_name", "").strip().replace(" ", "")
        last = sf.get("last_name", "").strip().replace(" ", "")
        company_slug = re.sub(r"[^A-Za-z0-9]+", "_", ctx.company).strip("_")
        filename = f"{first}_{last}_CoverLetter_{company_slug}.pdf" if first and last else "Cover_Letter.pdf"

        pdf_path = ctx.output_dir / filename
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        # Save the raw text for reference (always, even if PDF fails)
        try:
            (ctx.output_dir / filename.replace(".pdf", ".txt")).write_text(cover_text, encoding="utf-8")
        except Exception:
            pass

        # Render to PDF
        try:
            from openclaw.documents import render_cover_letter_pdf
            success = render_cover_letter_pdf(
                path=pdf_path,
                text=cover_text,
                applicant_name=f"{first} {last}".strip(),
                company=ctx.company,
                role=ctx.role,
                email=sf.get("email", ""),
                phone=sf.get("phone", ""),
                linkedin=sf.get("linkedin", ""),
                website=sf.get("website", ""),
            )
        except Exception:
            logger.debug("Cover letter PDF rendering (advanced) failed, trying basic text PDF.", exc_info=True)
            try:
                from openclaw.documents import render_text_pdf
                success = render_text_pdf(pdf_path, cover_text)
            except Exception:
                logger.debug("Cover letter PDF rendering (basic) also failed.", exc_info=True)
                return None

        if not success or not pdf_path.exists():
            logger.warning("[%s] Cover letter PDF rendering failed.", ctx.company)
            return None

        logger.info("[%s] Cover letter PDF saved: %s", ctx.company, filename)
        return pdf_path

    async def _fill_standard_questions(self, page: Any, ctx: ApplyContext | None = None) -> int:
        evaluate_fn = getattr(page, "evaluate", None)
        if evaluate_fn is None:
            return 0

        how_heard_answer = "Online Job Board"
        source_hint = ""
        try:
            source_hint = str(getattr(ctx, "source", "") or "")
        except Exception:
            source_hint = ""
        if source_hint.strip().lower() in {"simplify", "simplifyjobs"}:
            how_heard_answer = "SimplifyJobs (GitHub)"

        template_values: dict[str, str] = {}
        if ctx is not None:
            try:
                template_values.update(dict(getattr(ctx.profile, "standard_fields", {}) or {}))
            except Exception:
                pass
            template_values.setdefault("company", ctx.company)
            template_values.setdefault("role", ctx.role)
        template_values.setdefault("how_heard", how_heard_answer)

        bank_mappings: list[tuple[str, str]] = []
        if ctx is not None:
            try:
                for needle, answer in list(getattr(ctx.profile, "question_bank", []) or []):
                    bank_mappings.append((str(needle), expand_placeholders(str(answer), template_values)))
            except Exception:
                pass

        mappings: list[tuple[str, str]] = []
        mappings.extend(bank_mappings)

        # Low-risk defaults (can be overridden by question_bank entries above).
        mappings.extend(
            [
                ("how did you hear", how_heard_answer),
                ("where did you hear", how_heard_answer),
                ("source", how_heard_answer),
                ("start date", "Summer 2026"),
                ("when can you start", "Summer 2026"),
                ("available to start", "Summer 2026"),
                ("salary expectation", "Open / Negotiable"),
                ("compensation expectation", "Open / Negotiable"),
                ("desired salary", "Open / Negotiable"),
                ("referral", "No"),
                ("referred", "No"),
            ]
        )
        script = """
        (mappings) => {
          const normalize = (v) => (v || "").toLowerCase().replace(/\\s+/g, " ").trim();
          const isHuman = (ans) => {
            const a = normalize(ans);
            if (!a) return true;
            if (a === "__human__" || a === "__ask__" || a === "__manual__" || a === "__skip__") return true;
            return a.startsWith("__human");
          };

          const safeFocus = (el) => {
            // Avoid scroll-jank when filling many fields.
            try { el && el.focus && el.focus({ preventScroll: true }); } catch (e) {}
          };

          const isHidden = (el) => {
            if (!el) return true;
            try {
              const rect = el.getBoundingClientRect();
              if (!rect || rect.width < 2 || rect.height < 2) return true;
            } catch (e) {
              return true;
            }
            let cur = el;
            for (let i = 0; i < 5 && cur; i += 1) {
              try {
                const ariaHidden = normalize(cur.getAttribute ? (cur.getAttribute("aria-hidden") || "") : "");
                if (ariaHidden === "true") return true;
                const style = window.getComputedStyle(cur);
                if (style.display === "none" || style.visibility === "hidden") return true;
                const opacity = parseFloat(style.opacity || "1");
                if (!Number.isNaN(opacity) && opacity < 0.05) return true;
              } catch (e) {}
              cur = cur.parentElement;
            }
            return false;
          };

          const setNativeValue = (el, value) => {
            const tag = (el.tagName || "").toLowerCase();
            const proto = tag === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, "value");
            if (desc && typeof desc.set === "function") desc.set.call(el, value);
            else el.value = value;
          };

          const labelTextFor = (el) => {
            const pieces = [];
            const aria = el.getAttribute("aria-label");
            if (aria) pieces.push(aria);

            const labelledBy = el.getAttribute("aria-labelledby");
            if (labelledBy) {
              for (const id of labelledBy.split(/\\s+/)) {
                const n = document.getElementById(id);
                if (n?.innerText) pieces.push(n.innerText);
              }
            }

            if (el.id) {
              const byFor = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
              if (byFor?.innerText) pieces.push(byFor.innerText);
            }

            const wrap = el.closest("label");
            if (wrap?.innerText) pieces.push(wrap.innerText);

            let root = el.closest("fieldset,section,div,li,tr,td,dd,form");
            for (let i = 0; i < 3 && root; i += 1) {
              const legend = root.querySelector("legend");
              if (legend?.innerText) pieces.push(legend.innerText);
              const labelLike = root.querySelector("label,h3,h4,p,strong,span,div");
              if (labelLike?.innerText) pieces.push(labelLike.innerText);
              root = root.parentElement;
            }

            const placeholder = el.getAttribute("placeholder");
            if (placeholder) pieces.push(placeholder);
            const name = el.getAttribute("name");
            if (name) pieces.push(name);
            return pieces.join(" ").replace(/\\s+/g, " ").trim();
          };

          const matchAnswer = (text) => {
            const t = normalize(text);
            let best = null;
            let bestScore = -1;
            for (const [needleRaw, answer] of mappings) {
              const needleStr = String(needleRaw || "").trim();
              const needle = normalize(needleStr);
              if (!needle) continue;

              if (needle.startsWith("re:")) {
                const pattern = needleStr.slice(3).trim();
                if (!pattern) continue;
                try {
                  const re = new RegExp(pattern, "i");
                  if (re.test(text || "")) {
                    const ans = String(answer || "");
                    const score = 10000 + pattern.length;
                    if (score > bestScore) {
                      best = isHuman(ans) ? null : ans;
                      bestScore = score;
                    }
                  }
                } catch (e) {
                  continue;
                }
                continue;
              }

              if (t.includes(needle)) {
                const score = needle.length;
                if (score <= bestScore) continue;
                const ans = String(answer || "");
                best = isHuman(ans) ? null : ans;
                bestScore = score;
              }
            }
            return best;
          };

          const setValue = (control, answer) => {
            const tag = (control.tagName || "").toLowerCase();
            const type = (control.type || "").toLowerCase();
            const ansNorm = normalize(answer);

            if (tag === "select") {
              const options = Array.from(control.options || []);
              // Prefer exact-ish match, then substring.
              const exact = options.find((o) => normalize(o.textContent) === ansNorm || normalize(o.value) === ansNorm);
              const partial = options.find((o) => normalize(o.textContent).includes(ansNorm) || normalize(o.value).includes(ansNorm));
              let match = exact || partial;
              // Fallback for yes/no semantics when options are longer phrases.
              if (!match && (ansNorm === "yes" || ansNorm === "no")) {
                match = options.find((o) => {
                  const t = normalize(o.textContent) + " " + normalize(o.value);
                  if (ansNorm === "yes") {
                    return t.includes("yes") || t.includes("authorized") || t.includes("eligible") || t.includes("i am");
                  }
                  return t.includes("no") || t.includes("not") || t.includes("do not") || t.includes("require");
                });
              }
              if (match) {
                control.value = match.value;
                control.dispatchEvent(new Event("input", { bubbles: true }));
                control.dispatchEvent(new Event("change", { bubbles: true }));
                return true;
              }
              return false;
            }

            if (type === "radio") {
              const name = control.name || "";
              if (!name) return false;
              const radios = Array.from(document.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`));
              let want = ansNorm;
              if (want === "true") want = "yes";
              if (want === "false") want = "no";
              const target = radios.find((r) => {
                const t = normalize(r.closest("label")?.innerText || "") + " " + normalize(r.value || "");
                return t.includes(want);
              });
              if (target) {
                try { target.checked = true; } catch (e) {}
                target.dispatchEvent(new Event("input", { bubbles: true }));
                target.dispatchEvent(new Event("change", { bubbles: true }));
                return true;
              }
              // Common yes/no radio cases.
              if (want.startsWith("yes") || want.startsWith("no")) {
                const yn = radios.find((r) => {
                  const t = normalize(r.closest("label")?.innerText || "") + " " + normalize(r.value || "");
                  return want.startsWith("yes") ? t.includes("yes") : t.includes("no");
                });
                if (yn) {
                  try { yn.checked = true; } catch (e) {}
                  yn.dispatchEvent(new Event("input", { bubbles: true }));
                  yn.dispatchEvent(new Event("change", { bubbles: true }));
                  return true;
                }
              }
              return false;
            }

            if (type === "checkbox") {
              const truthy = new Set(["yes", "true", "y", "1", "on", "checked"]);
              if (truthy.has(ansNorm) && !control.checked) {
                try { control.checked = true; } catch (e) {}
                control.dispatchEvent(new Event("input", { bubbles: true }));
                control.dispatchEvent(new Event("change", { bubbles: true }));
                return true;
              }
              return false;
            }

            const role = normalize(control.getAttribute ? control.getAttribute("role") : "");
            if (role === "combobox" || normalize(control.className || "").includes("select__input")) {
              // Many modern dropdowns require trusted user events. Leave these to Playwright-level interactions.
              return false;
            }

            if ("value" in control) {
              const existing = (control.value || "").trim();
              if (existing) return false;
              safeFocus(control);
              setNativeValue(control, answer);
              control.dispatchEvent(new Event("input", { bubbles: true }));
              control.dispatchEvent(new Event("change", { bubbles: true }));
              return true;
            }
            return false;
          };

          let filled = 0;
          const seen = new Set();

          const controls = Array.from(document.querySelectorAll("input,textarea,select"));
          for (const el of controls) {
            if (el.disabled || el.readOnly) continue;
            if (isHidden(el)) continue;
            const tag = (el.tagName || "").toLowerCase();
            const type = normalize(el.type || "");
            if (type === "hidden" || type === "submit" || type === "button" || type === "reset") continue;
            if (type === "file") continue;
            if (seen.has(el)) continue;

            const labelText = labelTextFor(el);
            if (!labelText) continue;
            const answer = matchAnswer(labelText);
            if (!answer) continue;

            if (type === "radio") {
              // Only act on first radio in a group.
              const name = el.name || "";
              if (name && document.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`).length > 1) {
                const first = document.querySelector(`input[type="radio"][name="${CSS.escape(name)}"]`);
                if (first && first !== el) continue;
              }
            }

            if (setValue(el, answer)) {
              seen.add(el);
              filled += 1;
            }
          }

          return filled;
        }
        """
        try:
            result = await maybe_await(evaluate_fn(script, mappings))
            filled = result if isinstance(result, int) else 0
            logger.info("  Standard questions JS pass filled: %d fields", filled)
        except Exception:
            logger.debug("  Standard questions JS pass failed.", exc_info=True)
            filled = 0

        # Some modern UIs (ex: react-select comboboxes) require trusted user events to select an option.
        # Do a second pass with Playwright-level interactions when available.
        try:
            logger.debug("  Standard questions: checkboxes & radios pass...")
            cr_count = await self._fill_standard_questions_checkboxes_and_radios(page, mappings)
            filled += cr_count
            if cr_count:
                logger.info("  Standard questions checkboxes/radios filled: %d", cr_count)
        except Exception:
            logger.debug("  Standard questions checkboxes/radios pass failed.", exc_info=True)
        try:
            logger.debug("  Standard questions: comboboxes pass...")
            cb_count = await self._fill_standard_questions_comboboxes(page, mappings)
            filled += cb_count
            if cb_count:
                logger.info("  Standard questions comboboxes filled: %d", cb_count)
        except Exception:
            logger.debug("  Standard questions comboboxes pass failed.", exc_info=True)
        try:
            logger.debug("  Standard questions: text inputs Playwright pass...")
            ti_count = await self._fill_standard_questions_text_inputs_playwright(page, mappings)
            filled += ti_count
            if ti_count:
                logger.info("  Standard questions text inputs filled: %d", ti_count)
        except Exception:
            logger.debug("  Standard questions text inputs Playwright pass failed.", exc_info=True)
        return filled

    async def _fill_standard_questions_checkboxes_and_radios(
        self, page: Any, mappings: list[tuple[str, str]]
    ) -> int:
        """
        Scan-first approach for checkbox/radio controls using trusted Playwright events.

        1. Single JS call extracts all visible checkboxes + radio groups with labels/options/selectors.
        2. Python-side matching against the answer bank (instant).
        3. Playwright interactions only for confirmed matches.
        """
        evaluate_fn = getattr(page, "evaluate", None)
        locator_fn = getattr(page, "locator", None)
        get_by_role_fn = getattr(page, "get_by_role", None)
        if evaluate_fn is None or (locator_fn is None and get_by_role_fn is None):
            return 0

        # --- Step 1: Scan the DOM for all visible checkboxes and radio groups ---
        scan_script = """
        () => {
          const normalize = (v) => (v || "").toLowerCase().replace(/\\s+/g, " ").trim();
          const cssEscape = (v) => {
            try { return (window.CSS && CSS.escape) ? CSS.escape(String(v || "")) : String(v || ""); }
            catch (e) { return String(v || ""); }
          };

          const isHidden = (el) => {
            if (!el) return true;
            try {
              const rect = el.getBoundingClientRect();
              if (!rect || rect.width < 2 || rect.height < 2) return true;
            } catch (e) { return true; }
            let cur = el;
            for (let i = 0; i < 5 && cur; i += 1) {
              try {
                const ariaHidden = normalize(cur.getAttribute ? (cur.getAttribute("aria-hidden") || "") : "");
                if (ariaHidden === "true") return true;
                const style = window.getComputedStyle(cur);
                if (style.display === "none" || style.visibility === "hidden") return true;
                const opacity = parseFloat(style.opacity || "1");
                if (!Number.isNaN(opacity) && opacity < 0.05) return true;
              } catch (e) {}
              cur = cur.parentElement;
            }
            return false;
          };

          const labelTextFor = (el) => {
            const pieces = [];
            const aria = el.getAttribute("aria-label");
            if (aria) pieces.push(aria);
            const labelledBy = el.getAttribute("aria-labelledby");
            if (labelledBy) {
              for (const id of labelledBy.split(/\\s+/)) {
                const n = document.getElementById(id);
                if (n?.innerText) pieces.push(n.innerText);
              }
            }
            if (el.id) {
              const byFor = document.querySelector(`label[for="${cssEscape(el.id)}"]`);
              if (byFor?.innerText) pieces.push(byFor.innerText);
            }
            const wrap = el.closest("label");
            if (wrap?.innerText) pieces.push(wrap.innerText);
            let root = el.closest("fieldset,section,div,li,tr,td,dd,form");
            for (let i = 0; i < 3 && root; i += 1) {
              const legend = root.querySelector("legend");
              if (legend?.innerText) pieces.push(legend.innerText);
              const labelLike = root.querySelector("label,h3,h4,p,strong,span,div");
              if (labelLike?.innerText) pieces.push(labelLike.innerText);
              root = root.parentElement;
            }
            const placeholder = el.getAttribute("placeholder");
            if (placeholder) pieces.push(placeholder);
            const name = el.getAttribute("name");
            if (name) pieces.push(name);
            return pieces.join(" ").replace(/\\s+/g, " ").trim();
          };

          const selectorFor = (el) => {
            if (!el) return "";
            if (el.id) return "#" + cssEscape(el.id);
            const name = el.getAttribute("name");
            if (name) return `${el.tagName.toLowerCase()}[name="${cssEscape(name)}"]`;
            const parts = [];
            let cur = el;
            for (let i = 0; i < 5 && cur && cur.nodeType === 1; i += 1) {
              const tag = cur.tagName.toLowerCase();
              let index = 1;
              let sib = cur.previousElementSibling;
              while (sib) {
                if (sib.tagName === cur.tagName) index += 1;
                sib = sib.previousElementSibling;
              }
              parts.unshift(`${tag}:nth-of-type(${index})`);
              cur = cur.parentElement;
            }
            return parts.join(" > ");
          };

          const result = { checkboxes: [], radioGroups: [] };

          // --- Checkboxes ---
          const checkboxes = document.querySelectorAll("input[type='checkbox']");
          for (const cb of checkboxes) {
            if (cb.disabled || cb.checked) continue;
            if (isHidden(cb)) continue;
            const label = labelTextFor(cb);
            if (!label) continue;
            const sel = selectorFor(cb);
            if (!sel) continue;
            result.checkboxes.push({ label, selector: sel });
          }

          // --- Radio groups ---
          // Collect unique groups by name attribute.
          const radiosByName = {};
          const radios = document.querySelectorAll("input[type='radio']");
          for (const r of radios) {
            if (r.disabled) continue;
            if (isHidden(r)) continue;
            const name = r.getAttribute("name") || "";
            if (!name) continue;
            if (!radiosByName[name]) radiosByName[name] = [];
            const optLabel = labelTextFor(r);
            radiosByName[name].push({
              optionLabel: optLabel || r.value || "",
              optionValue: r.value || "",
              selector: selectorFor(r),
              checked: r.checked
            });
          }
          for (const [name, options] of Object.entries(radiosByName)) {
            // Skip groups where a choice is already selected.
            if (options.some(o => o.checked)) continue;
            // Determine the group label from the first radio's context.
            const firstRadio = document.querySelector(`input[type='radio'][name="${cssEscape(name)}"]`);
            let groupLabel = "";
            if (firstRadio) {
              // Look for fieldset > legend, or ARIA group label, or nearest heading.
              const fieldset = firstRadio.closest("fieldset");
              if (fieldset) {
                const legend = fieldset.querySelector("legend");
                if (legend?.innerText) groupLabel = legend.innerText;
              }
              if (!groupLabel) {
                const group = firstRadio.closest("[role='radiogroup'],[role='group']");
                if (group) {
                  const aria = group.getAttribute("aria-label") || "";
                  const ariaBy = group.getAttribute("aria-labelledby") || "";
                  if (aria) groupLabel = aria;
                  else if (ariaBy) {
                    const el = document.getElementById(ariaBy);
                    if (el?.innerText) groupLabel = el.innerText;
                  }
                }
              }
              if (!groupLabel) {
                // Walk up to find nearest label-like text
                let parent = firstRadio.parentElement;
                for (let i = 0; i < 5 && parent; i++) {
                  const heading = parent.querySelector("label,h3,h4,p.question,legend,strong");
                  if (heading?.innerText && heading.innerText.length > 3) {
                    groupLabel = heading.innerText;
                    break;
                  }
                  parent = parent.parentElement;
                }
              }
            }
            if (!groupLabel) continue;
            result.radioGroups.push({
              groupLabel: groupLabel.replace(/\\s+/g, " ").trim(),
              name,
              options: options.map(o => ({
                label: o.optionLabel,
                value: o.optionValue,
                selector: o.selector
              }))
            });
          }

          // Also detect ARIA radiogroups (some modern UIs don't use <input type=radio>).
          const ariaGroups = document.querySelectorAll("[role='radiogroup']");
          const seenGroupLabels = new Set(result.radioGroups.map(g => normalize(g.groupLabel)));
          for (const group of ariaGroups) {
            if (isHidden(group)) continue;
            const aria = group.getAttribute("aria-label") || "";
            const ariaBy = group.getAttribute("aria-labelledby") || "";
            let groupLabel = aria;
            if (!groupLabel && ariaBy) {
              const el = document.getElementById(ariaBy);
              if (el?.innerText) groupLabel = el.innerText;
            }
            if (!groupLabel) continue;
            groupLabel = groupLabel.replace(/\\s+/g, " ").trim();
            if (seenGroupLabels.has(normalize(groupLabel))) continue;
            const roleRadios = group.querySelectorAll("[role='radio']");
            if (!roleRadios.length) continue;
            const alreadySelected = Array.from(roleRadios).some(r =>
              r.getAttribute("aria-checked") === "true"
            );
            if (alreadySelected) continue;
            const options = [];
            for (const r of roleRadios) {
              if (isHidden(r)) continue;
              const optLabel = r.getAttribute("aria-label") || r.innerText || "";
              options.push({
                label: optLabel.replace(/\\s+/g, " ").trim(),
                value: r.getAttribute("data-value") || optLabel.replace(/\\s+/g, " ").trim(),
                selector: selectorFor(r)
              });
            }
            if (options.length > 0) {
              result.radioGroups.push({ groupLabel, name: "", options });
            }
          }

          return result;
        }
        """

        try:
            scanned = await maybe_await(evaluate_fn(scan_script))
        except Exception:
            logger.debug("    checkbox/radio DOM scan failed.", exc_info=True)
            return 0

        if not isinstance(scanned, dict):
            return 0

        checkboxes = scanned.get("checkboxes") or []
        radio_groups = scanned.get("radioGroups") or []
        logger.info("    DOM scan found %d checkbox(es), %d radio group(s).",
                     len(checkboxes), len(radio_groups))

        if not checkboxes and not radio_groups:
            return 0

        # --- Step 2: Match extracted controls against answer bank (pure Python, instant) ---
        from openclaw.answer_bank import match_question_bank

        truthy = {"yes", "true", "y", "1", "on", "checked", "i agree", "i acknowledge",
                  "i acknowledge and agree", "i consent"}
        count = 0

        # Process checkboxes: match label, check if answer is truthy.
        for cb in checkboxes:
            label = str(cb.get("label") or "").strip()
            selector = str(cb.get("selector") or "").strip()
            if not label or not selector:
                continue
            logger.debug("    checkbox label: '%s' (selector: %s)", label[:80], selector[:40])
            matched = match_question_bank(label, mappings)
            if matched is None:
                logger.debug("    checkbox NO MATCH for: '%s'", label[:80])
                continue
            ans_norm = str(matched).strip().lower()
            if is_human_sentinel(str(matched)):
                logger.debug("    checkbox HUMAN sentinel: '%s' -> %s", label[:50], matched)
                continue
            if ans_norm not in truthy:
                logger.debug("    checkbox SKIP (not truthy): '%s' -> '%s'", label[:50], ans_norm)
                continue
            logger.debug("    checkbox MATCH: %s -> %s", label[:50], matched)
            try:
                loc = locator_fn(selector) if locator_fn else None
                if loc is not None:
                    check_fn = getattr(loc.first, "check", None)
                    if check_fn:
                        await maybe_await(check_fn(timeout=1500))
                        count += 1
                        continue
                # Fallback: click
                if loc is not None:
                    await maybe_await(loc.first.click(timeout=1500))
                    count += 1
            except Exception:
                logger.debug("    checkbox fill failed: %s", label[:50], exc_info=True)

        # Process radio groups: match group label, then find best option.
        for rg in radio_groups:
            group_label = str(rg.get("groupLabel") or "").strip()
            options = rg.get("options") or []
            if not group_label or not options:
                continue
            matched = match_question_bank(group_label, mappings)
            if matched is None:
                continue
            if is_human_sentinel(str(matched)):
                continue
            answer = str(matched).strip()
            answer_norm = answer.lower().replace("/", " ").strip()

            # Find the best matching option.
            best_opt = None
            best_score = -1
            for opt in options:
                opt_label = str(opt.get("label") or opt.get("value") or "").strip()
                opt_norm = opt_label.lower().replace("/", " ").strip()
                # Exact match.
                if opt_norm == answer_norm:
                    best_opt = opt
                    best_score = 10000
                    break
                # Containment match.
                if answer_norm in opt_norm or opt_norm in answer_norm:
                    score = len(answer_norm)
                    if score > best_score:
                        best_opt = opt
                        best_score = score
                # Yes/No semantic match.
                if answer_norm in {"yes", "true", "y"} and opt_norm in {"yes", "true", "y"}:
                    best_opt = opt
                    best_score = 5000
                if answer_norm in {"no", "false", "n"} and opt_norm in {"no", "false", "n"}:
                    best_opt = opt
                    best_score = 5000

            if best_opt is None:
                logger.debug("    radio MATCH but no option: group=%s, answer=%s, options=%s",
                             group_label[:40], answer[:30],
                             [o.get("label", "")[:20] for o in options[:4]])
                continue

            opt_sel = str(best_opt.get("selector") or "").strip()
            logger.debug("    radio MATCH: %s -> %s (option: %s)",
                         group_label[:40], answer[:30],
                         str(best_opt.get("label") or "")[:30])
            try:
                if opt_sel and locator_fn:
                    loc = locator_fn(opt_sel).first
                    check_fn = getattr(loc, "check", None)
                    if check_fn:
                        await maybe_await(check_fn(timeout=1500))
                    else:
                        await maybe_await(loc.click(timeout=1500))
                    count += 1
                    continue
                # Fallback: use get_by_role if available.
                if get_by_role_fn is not None:
                    try:
                        needle_re = re.compile(re.escape(group_label[:60]), re.I)
                        group_loc = get_by_role_fn("radiogroup", name=needle_re).first
                        ans_re = re.compile(re.escape(str(best_opt.get("label") or answer)[:60]), re.I)
                        opt_loc = group_loc.get_by_role("radio", name=ans_re).first
                        check_fn = getattr(opt_loc, "check", None)
                        if check_fn:
                            await maybe_await(check_fn(timeout=1500))
                        else:
                            await maybe_await(opt_loc.click(timeout=1500))
                        count += 1
                    except Exception:
                        pass
            except Exception:
                logger.debug("    radio fill failed: %s", group_label[:40], exc_info=True)

        return count

    async def _fill_standard_questions_text_inputs_playwright(
        self, page: Any, mappings: list[tuple[str, str]]
    ) -> int:
        """
        Second pass for text inputs using Playwright `.fill()` (trusted user events).
        """
        locator_fn = getattr(page, "locator", None)
        evaluate_fn = getattr(page, "evaluate", None)
        if locator_fn is None or evaluate_fn is None:
            return 0

        def _needle_for_scan(raw: str) -> str:
            n = str(raw or "").strip()
            if not n:
                return ""
            lower = n.lower()
            if lower.startswith("re:"):
                candidate = n[3:]
                candidate = re.sub(r"\\s\\+|\\s\\*|\\s\\?|\\s", " ", candidate)
                candidate = re.sub(r"[^a-zA-Z0-9 ]+", " ", candidate)
                return normalize_text(candidate)
            return normalize_text(n)

        needles: list[str] = []
        seen_needles: set[str] = set()
        for needle, _answer in mappings:
            scan = _needle_for_scan(str(needle))
            if not scan or scan in seen_needles:
                continue
            needles.append(scan)
            seen_needles.add(scan)

        if not needles:
            return 0

        scan_script = """
        (needles) => {
          const normalize = (v) => (v || "").toLowerCase().replace(/\\s+/g, " ").trim();
          const cssEscape = (v) => {
            try { return (window.CSS && CSS.escape) ? CSS.escape(String(v || "")) : String(v || ""); } catch (e) { return String(v || ""); }
          };

          const isHidden = (el) => {
            if (!el) return true;
            try {
              const rect = el.getBoundingClientRect();
              if (!rect || rect.width < 2 || rect.height < 2) return true;
            } catch (e) {
              return true;
            }
            let cur = el;
            for (let i = 0; i < 5 && cur; i += 1) {
              try {
                const ariaHidden = normalize(cur.getAttribute ? (cur.getAttribute("aria-hidden") || "") : "");
                if (ariaHidden === "true") return true;
                const style = window.getComputedStyle(cur);
                if (style.display === "none" || style.visibility === "hidden") return true;
                const opacity = parseFloat(style.opacity || "1");
                if (!Number.isNaN(opacity) && opacity < 0.05) return true;
              } catch (e) {}
              cur = cur.parentElement;
            }
            return false;
          };

          const yFor = (el) => {
            try {
              const r = el.getBoundingClientRect();
              return (r.top || 0) + window.scrollY;
            } catch (e) {
              return 0;
            }
          };

          const labelTextFor = (el) => {
            const pieces = [];
            const aria = el.getAttribute("aria-label");
            if (aria) pieces.push(aria);
            const labelledBy = el.getAttribute("aria-labelledby");
            if (labelledBy) {
              for (const id of labelledBy.split(/\\s+/)) {
                const n = document.getElementById(id);
                if (n?.innerText) pieces.push(n.innerText);
              }
            }
            if (el.id) {
              const byFor = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
              if (byFor?.innerText) pieces.push(byFor.innerText);
            }
            const wrap = el.closest("label");
            if (wrap?.innerText) pieces.push(wrap.innerText);
            let root = el.closest("fieldset,section,div,li,tr,td,dd,form");
            for (let i = 0; i < 2 && root; i += 1) {
              const legend = root.querySelector("legend");
              if (legend?.innerText) pieces.push(legend.innerText);
              const labelLike = root.querySelector("label,h3,h4,p,strong,span,div");
              if (labelLike?.innerText) pieces.push(labelLike.innerText);
              root = root.parentElement;
            }
            const placeholder = el.getAttribute("placeholder");
            if (placeholder) pieces.push(placeholder);
            const name = el.getAttribute("name");
            if (name) pieces.push(name);
            return pieces.join(" ").replace(/\\s+/g, " ").trim();
          };

          const selectorFor = (el) => {
            if (!el) return "";
            const esc = (v) => (window.CSS && CSS.escape) ? CSS.escape(String(v || "")) : String(v || "");
            if (el.id) return "#" + esc(el.id);
            const automationId = el.getAttribute("data-automation-id");
            if (automationId) return `${el.tagName.toLowerCase()}[data-automation-id="${esc(automationId)}"]`;
            const name = el.getAttribute("name");
            if (name) return `${el.tagName.toLowerCase()}[name="${esc(name)}"]`;
            const parts = [];
            let cur = el;
            for (let i = 0; i < 5 && cur && cur.nodeType === 1; i += 1) {
              const tag = cur.tagName.toLowerCase();
              let index = 1;
              let sib = cur.previousElementSibling;
              while (sib) {
                if (sib.tagName === cur.tagName) index += 1;
                sib = sib.previousElementSibling;
              }
              parts.unshift(`${tag}:nth-of-type(${index})`);
              cur = cur.parentElement;
            }
            return parts.join(" > ");
          };

          const matchesAny = (label) => {
            const t = normalize(label);
            return needles.some((n) => n && t.includes(n));
          };

          const out = [];
          const controls = Array.from(document.querySelectorAll("input,textarea"));
          for (const el of controls) {
            if (out.length >= 18) break;
            if (el.disabled || el.readOnly) continue;
            if (isHidden(el)) continue;

            const tag = (el.tagName || "").toLowerCase();
            const type = normalize(el.type || "");
            if (type === "hidden" || type === "password" || type === "submit" || type === "button" || type === "reset") continue;
            if (type === "file" || type === "checkbox" || type === "radio") continue;
            if ((el.value || "").trim()) continue;

            const label = labelTextFor(el);
            if (!label) continue;
            if (!matchesAny(label)) continue;
            const sel = selectorFor(el);
            if (!sel) continue;
            out.push({ selector: sel, label, tag, type, y: yFor(el) });
          }
          out.sort((a, b) => (a.y || 0) - (b.y || 0));
          return out.slice(0, 12);
        }
        """

        try:
            scanned = await maybe_await(evaluate_fn(scan_script, needles))
        except Exception:
            return 0

        if not isinstance(scanned, list) or not scanned:
            return 0

        candidates: list[dict[str, str]] = []
        for item in scanned:
            if not isinstance(item, dict):
                continue
            selector = str(item.get("selector") or "").strip()
            label = str(item.get("label") or "").strip()
            tag = str(item.get("tag") or "").strip()
            typ = str(item.get("type") or "").strip()
            if selector and label:
                candidates.append({"selector": selector, "label": label, "tag": tag, "type": typ})

        if not candidates:
            return 0

        count = 0
        for item in candidates:
            selector = item["selector"]
            label_text = item["label"]

            answer: str | None = None
            best_score = -1
            label_norm_text = normalize_text(label_text)
            for needle, mapped in mappings:
                needle_str = str(needle or "").strip()
                if not needle_str:
                    continue
                if needle_str.lower().startswith("re:"):
                    pattern = needle_str[3:].strip()
                    if not pattern:
                        continue
                    try:
                        if re.search(pattern, label_text, flags=re.I):
                            score = 10_000 + len(pattern)
                            if score > best_score:
                                best_score = score
                                answer = str(mapped)
                    except re.error:
                        continue
                    continue

                n = normalize_text(needle_str)
                if n and n in label_norm_text:
                    score = len(n)
                    if score > best_score:
                        best_score = score
                        answer = str(mapped)
            if not answer or is_human_sentinel(answer):
                continue

            loc = locator_fn(selector)
            # Prefer visible matches to avoid hidden duplicates (common in modern form UIs).
            visible = None
            try:
                max_scan = min(int(await maybe_await(loc.count())), 8)
            except Exception:
                max_scan = 1
            for i in range(max_scan):
                cand = loc.nth(i)
                try:
                    if await maybe_await(cand.is_visible()):
                        visible = cand
                        break
                except Exception:
                    continue
            if visible is None:
                visible = loc.first
            try:
                await maybe_await(visible.scroll_into_view_if_needed(timeout=2000))
            except Exception:
                pass
            try:
                await maybe_await(visible.fill(answer, timeout=2500))
                count += 1
                continue
            except Exception:
                pass

            # Fallback: click + type for masked inputs.
            try:
                await maybe_await(visible.click(timeout=2000))
                keyboard = getattr(page, "keyboard", None)
                if keyboard is not None:
                    type_fn = getattr(keyboard, "type", None)
                    if type_fn:
                        await maybe_await(type_fn(answer, delay=18))
                        count += 1
            except Exception:
                continue

        return count

    async def _fill_standard_questions_comboboxes(
        self, page: Any, mappings: list[tuple[str, str]]
    ) -> int:
        """
        Fill react-select / combobox based standard questions using trusted browser events.

        DOM-driven `.evaluate()` can fail here because many frameworks ignore synthetic/untrusted events.
        """
        locator_fn = getattr(page, "locator", None)
        keyboard = getattr(page, "keyboard", None)
        evaluate_fn = getattr(page, "evaluate", None)
        if locator_fn is None or keyboard is None or evaluate_fn is None:
            return 0

        def _needle_for_scan(raw: str) -> str:
            n = str(raw or "").strip()
            if not n:
                return ""
            lower = n.lower()
            if lower.startswith("re:"):
                # Best-effort: strip regex noise so JS substring matching can still find the field.
                candidate = n[3:]
                candidate = re.sub(r"\\s\\+|\\s\\*|\\s\\?|\\s", " ", candidate)
                candidate = re.sub(r"[^a-zA-Z0-9 ]+", " ", candidate)
                return normalize_text(candidate)
            return normalize_text(n)

        needles: list[str] = []
        seen_needles: set[str] = set()
        for needle, _answer in mappings:
            scan = _needle_for_scan(str(needle))
            if not scan or scan in seen_needles:
                continue
            needles.append(scan)
            seen_needles.add(scan)

        if not needles:
            return 0

        scan_script = """
        (needles) => {
          const normalize = (v) => (v || "").toLowerCase().replace(/\\s+/g, " ").trim();

          const isHidden = (el) => {
            if (!el) return true;
            try {
              const rect = el.getBoundingClientRect();
              if (!rect || rect.width < 2 || rect.height < 2) return true;
            } catch (e) {
              return true;
            }
            let cur = el;
            for (let i = 0; i < 5 && cur; i += 1) {
              try {
                const ariaHidden = normalize(cur.getAttribute ? (cur.getAttribute("aria-hidden") || "") : "");
                if (ariaHidden === "true") return true;
                const style = window.getComputedStyle(cur);
                if (style.display === "none" || style.visibility === "hidden") return true;
                const opacity = parseFloat(style.opacity || "1");
                if (!Number.isNaN(opacity) && opacity < 0.05) return true;
              } catch (e) {}
              cur = cur.parentElement;
            }
            return false;
          };

          const yFor = (el) => {
            try {
              const r = el.getBoundingClientRect();
              return (r.top || 0) + window.scrollY;
            } catch (e) {
              return 0;
            }
          };

          const labelTextFor = (el) => {
            const parts = [];
            const aria = el.getAttribute("aria-label");
            if (aria) parts.push(aria);
            const labelledBy = el.getAttribute("aria-labelledby");
            if (labelledBy) {
              for (const id of labelledBy.split(/\\s+/)) {
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
              const labelLike = root.querySelector("label,h3,h4,p,strong,span,div");
              if (labelLike?.innerText) parts.push(labelLike.innerText);
              root = root.parentElement;
            }
            const placeholder = el.getAttribute("placeholder");
            if (placeholder) parts.push(placeholder);
            const name = el.getAttribute("name");
            if (name) parts.push(name);
            return parts.join(" ").replace(/\\s+/g, " ").trim();
          };

          const selectorFor = (el) => {
            if (!el) return "";
            const esc = (v) => (window.CSS && CSS.escape) ? CSS.escape(String(v || "")) : String(v || "");
            if (el.id) return "#" + esc(el.id);
            const automationId = el.getAttribute("data-automation-id");
            if (automationId) return `${el.tagName.toLowerCase()}[data-automation-id="${esc(automationId)}"]`;
            const name = el.getAttribute("name");
            if (name) return `${el.tagName.toLowerCase()}[name="${esc(name)}"]`;
            // Fallback: short-ish nth-of-type path.
            const parts = [];
            let cur = el;
            for (let i = 0; i < 5 && cur && cur.nodeType === 1; i += 1) {
              const tag = cur.tagName.toLowerCase();
              let index = 1;
              let sib = cur.previousElementSibling;
              while (sib) {
                if (sib.tagName === cur.tagName) index += 1;
                sib = sib.previousElementSibling;
              }
              parts.unshift(`${tag}:nth-of-type(${index})`);
              cur = cur.parentElement;
            }
            return parts.join(" > ");
          };

          const openSelectorFor = (el) => {
            if (!el) return "";
            const container =
              el.closest(".select__control, [aria-haspopup='listbox'], [role='combobox'], fieldset, section, div, li, td, dd, form") ||
              el.parentElement;
            if (container) {
              const cand = container.querySelector(
                "button[aria-haspopup='listbox'], [role='button'][aria-haspopup='listbox'], button[aria-label*='open'], button[aria-label*='expand'], button[aria-label*='dropdown'], .select__dropdown-indicator, .select__indicator"
              );
              if (cand && !isHidden(cand)) {
                const sel = selectorFor(cand);
                if (sel) return sel;
              }
            }
            return selectorFor(el);
          };

          const hasSelectedValue = (el) => {
            try {
              const ariaVal = el.getAttribute("aria-valuetext") || el.getAttribute("aria-value") || "";
              const t = normalize(ariaVal);
              if (t && !t.includes("select") && !t.includes("choose") && !t.includes("pick")) return true;
            } catch (e) {}

            try {
              const tag = (el.tagName || "").toLowerCase();
              if (tag === "input") {
                const v = normalize(el.value || "");
                if (v && !v.includes("select") && !v.includes("choose") && !v.includes("pick")) return true;
              }
            } catch (e) {}

            // react-select style selected value container.
            const control = el.closest(".select__control") || el.parentElement;
            if (control) {
              const node = control.querySelector(".select__single-value") || control.querySelector(".select__multi-value");
              const t = normalize(node?.innerText || node?.textContent || "");
              if (t && !t.includes("select") && !t.includes("choose") && !t.includes("pick")) return true;
            }
            return false;
          };

          const matchesAny = (label) => {
            const t = normalize(label);
            return needles.some((n) => n && t.includes(n));
          };

          const out = [];
          const combos = Array.from(
            document.querySelectorAll("[role='combobox'], button[aria-haspopup='listbox'], [aria-haspopup='listbox']")
          );
          for (const el of combos) {
            if (out.length >= 16) break;
            if (el.disabled || el.readOnly) continue;
            if (isHidden(el)) continue;
            // Avoid non-interactive containers.
            const tag = (el.tagName || "").toLowerCase();
            const tabindex = Number(el.getAttribute("tabindex") || "0");
            if (tag !== "input" && tag !== "button" && tabindex < 0) continue;
            const label = labelTextFor(el);
            if (!label) continue;
            if (!matchesAny(label)) continue;
            if (hasSelectedValue(el)) continue;
            const sel = selectorFor(el);
            if (!sel) continue;
            const openSel = openSelectorFor(el) || sel;
            // react-select exposes the listbox id via aria-controls (best) or aria-describedby placeholder.
            const ariaControls = el.getAttribute("aria-controls") || "";
            const describedBy = el.getAttribute("aria-describedby") || "";
            let listboxId = ariaControls;
            if (!listboxId) {
              const m = describedBy.match(/(react-select-[^\\s]+?)-placeholder/);
              if (m && m[1]) listboxId = `${m[1]}-listbox`;
            }
            out.push({ selector: sel, open_selector: openSel, listbox_id: listboxId, id: el.id || "", label, tag, y: yFor(el) });
          }
          out.sort((a, b) => (a.y || 0) - (b.y || 0));
          return out.slice(0, 12);
        }
        """

        try:
            scanned = await maybe_await(evaluate_fn(scan_script, needles))
        except Exception:
            return 0

        if not isinstance(scanned, list) or not scanned:
            return 0

        candidates: list[dict[str, str]] = []
        for item in scanned:
            if not isinstance(item, dict):
                continue
            selector = str(item.get("selector") or "").strip()
            open_selector = str(item.get("open_selector") or "").strip()
            listbox_id = str(item.get("listbox_id") or "").strip()
            cid = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            tag = str(item.get("tag") or "").strip()
            if selector and label:
                candidates.append(
                    {
                        "selector": selector,
                        "open_selector": open_selector,
                        "listbox_id": listbox_id,
                        "id": cid,
                        "label": label,
                        "tag": tag,
                    }
                )

        if not candidates:
            return 0

        count = 0
        for item in candidates:
            selector = item["selector"]
            open_selector = item.get("open_selector") or ""
            listbox_id = item.get("listbox_id") or ""
            cid = item["id"]

            label_text = item["label"]
            answer: str | None = None
            best_score = -1
            label_norm_text = normalize_text(label_text)
            for needle, mapped in mappings:
                needle_str = str(needle or "").strip()
                if not needle_str:
                    continue
                if needle_str.lower().startswith("re:"):
                    pattern = needle_str[3:].strip()
                    if not pattern:
                        continue
                    try:
                        if re.search(pattern, label_text, flags=re.I):
                            score = 10_000 + len(pattern)
                            if score > best_score:
                                best_score = score
                                answer = str(mapped)
                    except re.error:
                        continue
                    continue

                n = normalize_text(needle_str)
                if n and n in label_norm_text:
                    score = len(n)
                    if score > best_score:
                        best_score = score
                        answer = str(mapped)
            if not answer:
                continue
            if is_human_sentinel(answer):
                continue

            # Many UIs require clicking an explicit dropdown icon/button (not the input itself).
            opener = open_selector or selector
            try:
                loc = locator_fn(opener)
                # Prefer the first visible match to avoid hidden duplicates.
                chosen = None
                try:
                    max_scan = min(int(await maybe_await(loc.count())), 8)
                except Exception:
                    max_scan = 1
                for i in range(max_scan):
                    cand = loc.nth(i)
                    try:
                        if await maybe_await(cand.is_visible()):
                            chosen = cand
                            break
                    except Exception:
                        continue
                if chosen is None:
                    chosen = loc.first
                try:
                    await maybe_await(chosen.scroll_into_view_if_needed(timeout=2000))
                except Exception:
                    pass
                await maybe_await(chosen.click(timeout=2500))
            except Exception:
                continue

            # react-select reliably opens on ArrowDown with trusted events.
            press_fn = getattr(keyboard, "press", None)
            if press_fn:
                try:
                    await maybe_await(press_fn("ArrowDown"))
                except Exception:
                    pass

            # Type to filter when supported (react-select / workday prompts often require this).
            type_fn = getattr(keyboard, "type", None)
            if type_fn and answer and len(str(answer)) <= 120:
                try:
                    # Ensure the actual combobox/input is focused.
                    if opener != selector:
                        try:
                            await maybe_await(locator_fn(selector).first.click(timeout=1500))
                        except Exception:
                            pass
                    try:
                        await maybe_await(press_fn("Control+A"))
                    except Exception:
                        pass
                    try:
                        await maybe_await(press_fn("Meta+A"))
                    except Exception:
                        pass
                    try:
                        await maybe_await(press_fn("Backspace"))
                    except Exception:
                        pass
                    await maybe_await(type_fn(str(answer), delay=16))
                except Exception:
                    pass

            clicked = False
            try:
                # Prefer a specific listbox when we can derive it; otherwise fall back to any visible listbox.
                options_loc = None

                if listbox_id:
                    try:
                        listbox_sel = f"#{listbox_id}"
                        listbox = locator_fn(listbox_sel)
                        await maybe_await(listbox.wait_for(state="visible", timeout=1500))
                        options_loc = locator_fn(f"{listbox_sel} [role=option]")
                    except Exception:
                        options_loc = None

                # Legacy react-select guess (only works for some DOMs). If it fails, continue to generic fallback.
                if options_loc is None and cid:
                    try:
                        listbox_sel = f"#react-select-{cid}-listbox"
                        listbox = locator_fn(listbox_sel)
                        await maybe_await(listbox.wait_for(state="visible", timeout=800))
                        options_loc = locator_fn(f"{listbox_sel} [role=option]")
                    except Exception:
                        options_loc = None

                if options_loc is None:
                    try:
                        await maybe_await(
                            locator_fn("[role=listbox]").first.wait_for(state="visible", timeout=1500)
                        )
                    except Exception:
                        pass
                    options_loc = locator_fn("[role=listbox] [role=option]")

                option_count = await maybe_await(options_loc.count())
                if not option_count or option_count <= 0:
                    # Workday-style options.
                    options_loc = locator_fn("[data-automation-id='promptOption']")
                    option_count = await maybe_await(options_loc.count())
                if not option_count or option_count <= 0:
                    options_loc = locator_fn(".select__menu [role=option], .select__menu .select__option")
                    option_count = await maybe_await(options_loc.count())

                if option_count and option_count > 0:
                    try:
                        texts = await maybe_await(options_loc.all_inner_texts())
                    except Exception:
                        texts = []
                        max_scan = min(int(option_count), 50)
                        for i in range(max_scan):
                            try:
                                t = await maybe_await(options_loc.nth(i).inner_text())
                            except Exception:
                                t = ""
                            texts.append(str(t or ""))

                    ans_norm = normalize_text(answer)
                    tokens = [t for t in ans_norm.split(" ") if len(t) >= 3]

                    best_idx = -1
                    best_opt_score = 0
                    for i, txt in enumerate(texts[:80]):
                        opt_norm = normalize_text(txt)
                        if not opt_norm:
                            continue
                        score = 0

                        if ans_norm and ans_norm in opt_norm:
                            score += 200 + len(ans_norm)
                        for tok in tokens:
                            if tok in opt_norm:
                                score += len(tok)

                        # Yes/No semantics support (very common).
                        if ans_norm in {"yes", "no"}:
                            if ans_norm == "yes" and ("yes" in opt_norm or "i am" in opt_norm):
                                score += 250
                            if ans_norm == "no" and ("no" in opt_norm or "i am not" in opt_norm or "do not" in opt_norm):
                                score += 250

                        if score > best_opt_score:
                            best_opt_score = score
                            best_idx = i

                    # Require a meaningful match; otherwise leave for HITL.
                    if best_idx >= 0 and best_opt_score >= 25:
                        await maybe_await(options_loc.nth(best_idx).click(timeout=2500))
                        clicked = True
            except Exception:
                clicked = False

            if clicked:
                count += 1

        return count

    async def _fill_cover_letter_text(self, page: Any, cover_letter_text: str) -> int:
        evaluate_fn = getattr(page, "evaluate", None)
        if evaluate_fn is None:
            return 0
        if not cover_letter_text.strip():
            return 0

        script = """
        (text) => {
          const normalize = (v) => (v || "").toLowerCase().replace(/\\s+/g, " ").trim();
          const safeFocus = (el) => {
            try { el && el.focus && el.focus({ preventScroll: true }); } catch (e) {}
          };

          const setNativeValue = (el, value) => {
            const tag = (el.tagName || "").toLowerCase();
            const proto = tag === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, "value");
            if (desc && typeof desc.set === "function") desc.set.call(el, value);
            else el.value = value;
          };

          const labelTextFor = (el) => {
            const parts = [];
            const aria = el.getAttribute("aria-label");
            if (aria) parts.push(aria);
            const labelledBy = el.getAttribute("aria-labelledby");
            if (labelledBy) {
              for (const id of labelledBy.split(/\\s+/)) {
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
              const labelLike = root.querySelector("label,h3,h4,p,strong,span,div");
              if (labelLike?.innerText) parts.push(labelLike.innerText);
              root = root.parentElement;
            }
            const placeholder = el.getAttribute("placeholder");
            if (placeholder) parts.push(placeholder);
            const name = el.getAttribute("name");
            if (name) parts.push(name);
            return parts.join(" ").replace(/\\s+/g, " ").trim();
          };

          let filled = 0;
          const controls = Array.from(document.querySelectorAll("textarea, input[type='text'], input:not([type])"));
          for (const el of controls) {
            if (el.disabled || el.readOnly) continue;
            if ((el.value || "").trim()) continue;
            const hay = normalize(labelTextFor(el));
            if (!hay) continue;
            if (!hay.includes("cover letter")) continue;
            safeFocus(el);
            setNativeValue(el, text);
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
            filled += 1;
          }
          return filled;
        }
        """
        try:
            result = await maybe_await(evaluate_fn(script, cover_letter_text))
            if isinstance(result, int):
                return result
        except Exception:
            return 0
        return 0

    async def _fill_inferred_fields(self, page: Any, standard_fields: dict[str, str]) -> int:
        """
        DOM-driven field filling that does not rely on platform-specific selectors.
        This is the main reliability lever for unknown ATS / custom form layouts.
        """
        evaluate_fn = getattr(page, "evaluate", None)
        if evaluate_fn is None:
            return 0

        # Normalize a few values up-front for common validation rules.
        gpa = (standard_fields.get("gpa") or "").strip()
        if gpa and "/" in gpa:
            gpa = gpa.split("/", 1)[0].strip()

        values = dict(standard_fields)
        if gpa:
            values["gpa"] = gpa
        values.setdefault("portfolio", values.get("github") or values.get("linkedin") or "")

        script = """
        (values) => {
          const normalize = (v) => (v || "").toLowerCase().replace(/\\s+/g, " ").trim();
          const safeFocus = (el) => {
            try { el && el.focus && el.focus({ preventScroll: true }); } catch (e) {}
          };
          const isHidden = (el) => {
            if (!el) return true;
            try {
              const rect = el.getBoundingClientRect();
              if (!rect || rect.width < 2 || rect.height < 2) return true;
            } catch (e) {
              return true;
            }
            let cur = el;
            for (let i = 0; i < 5 && cur; i += 1) {
              try {
                const ariaHidden = normalize(cur.getAttribute ? (cur.getAttribute("aria-hidden") || "") : "");
                if (ariaHidden === "true") return true;
                const style = window.getComputedStyle(cur);
                if (style.display === "none" || style.visibility === "hidden") return true;
                const opacity = parseFloat(style.opacity || "1");
                if (!Number.isNaN(opacity) && opacity < 0.05) return true;
              } catch (e) {}
              cur = cur.parentElement;
            }
            return false;
          };

          const setNativeValue = (el, value) => {
            const tag = (el.tagName || "").toLowerCase();
            const proto = tag === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, "value");
            if (desc && typeof desc.set === "function") desc.set.call(el, value);
            else el.value = value;
          };

          const labelTextFor = (el) => {
            const parts = [];
            const aria = el.getAttribute("aria-label");
            if (aria) parts.push(aria);
            const labelledBy = el.getAttribute("aria-labelledby");
            if (labelledBy) {
              for (const id of labelledBy.split(/\\s+/)) {
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
              const labelLike = root.querySelector("label,h3,h4,p,strong,span,div");
              if (labelLike?.innerText) parts.push(labelLike.innerText);
              root = root.parentElement;
            }
            const placeholder = el.getAttribute("placeholder");
            if (placeholder) parts.push(placeholder);
            const name = el.getAttribute("name");
            if (name) parts.push(name);
            const id = el.getAttribute("id");
            if (id) parts.push(id);
            const autocomplete = el.getAttribute("autocomplete");
            if (autocomplete) parts.push(autocomplete);
            return parts.join(" ").replace(/\\s+/g, " ").trim();
          };

          const scoreKey = (hay, key) => {
            const h = normalize(hay);
            const checks = {
              first_name: ["first name", "given name", "forename", "firstname"],
              last_name: ["last name", "surname", "family name", "lastname"],
              full_name: ["full name", "your name", "name"],
              email: ["email"],
              phone: ["phone", "mobile", "cell"],
              linkedin: ["linkedin"],
              github: ["github"],
              portfolio: ["portfolio", "website", "personal site", "url", "web site"],
              school: ["school", "university", "college", "institution"],
              degree: ["degree", "major", "field of study", "program"],
              gpa: ["gpa", "grade point"],
              graduation: ["graduation", "grad date", "expected graduation", "graduating"]
            };
            const needles = checks[key] || [];
            let score = 0;
            for (const n of needles) {
              if (h.includes(n)) score += n.length;
            }
            return score;
          };

          const chooseKey = (el, hay) => {
            const type = normalize(el.type || "");
            const tag = normalize(el.tagName || "");

            // Strong type hints.
            if (type === "email") return "email";
            if (type === "tel") return "phone";
            if (type === "url") {
              const h = normalize(hay);
              if (h.includes("linkedin")) return "linkedin";
              if (h.includes("github")) return "github";
              if (h.includes("portfolio") || h.includes("website")) return "portfolio";
              // Otherwise, pick a reasonable default.
              return values.linkedin ? "linkedin" : "portfolio";
            }

            // Text/select inference by label/name/etc.
            const keys = ["first_name","last_name","full_name","email","phone","linkedin","github","portfolio","school","degree","gpa","graduation"];
            let bestKey = "";
            let bestScore = 0;
            for (const k of keys) {
              let score = scoreKey(hay, k);
              // Avoid over-filling generic "name" fields that are clearly not candidate name.
              const h = normalize(hay);
              if (k === "full_name" && score > 0) {
                const bad = ["company", "employer", "business", "school", "university", "reference", "referrer", "manager"];
                if (bad.some((b) => h.includes(b))) score = 0;
              }
              if (score > bestScore) {
                bestScore = score;
                bestKey = k;
              }
            }
            if (bestScore < 4) return "";

            // If we have first+last, prefer those instead of full_name for ambiguous "name" labels.
            if (bestKey === "full_name" && values.first_name && values.last_name) {
              const h = normalize(hay);
              if (hasFirstNameField && hasLastNameField && !h.includes("full") && !h.includes("your name")) return "";
            }

            // Some controls should only be filled if we have a value.
            if (!values[bestKey]) return "";
            return bestKey;
          };

          const fillControl = (el, value) => {
            const tag = normalize(el.tagName || "");
            const type = normalize(el.type || "");
            if (tag === "select") {
              const opts = Array.from(el.options || []);
              const targetNorm = normalize(value);
              const exact = opts.find((o) => normalize(o.textContent) === targetNorm || normalize(o.value) === targetNorm);
              const partial = opts.find((o) => normalize(o.textContent).includes(targetNorm) || normalize(o.value).includes(targetNorm));
              const match = exact || partial;
              if (match) {
                el.value = match.value;
                el.dispatchEvent(new Event("input", { bubbles: true }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
                return true;
              }
              return false;
            }

            if (type === "radio") return false;
            if (type === "checkbox") return false;
            if (type === "file") return false;

            if ("value" in el) {
              const existing = (el.value || "").trim();
              if (existing) return false;
              safeFocus(el);
              setNativeValue(el, value);
              el.dispatchEvent(new Event("input", { bubbles: true }));
              el.dispatchEvent(new Event("change", { bubbles: true }));
              return true;
            }
            return false;
          };

          const controls = Array.from(document.querySelectorAll("input,textarea,select"));
          const hasFirstNameField = controls.some((el) => {
            const h = normalize(labelTextFor(el));
            return h.includes("first name") || h.includes("firstname") || h.includes("given name") || h.includes("given-name");
          });
          const hasLastNameField = controls.some((el) => {
            const h = normalize(labelTextFor(el));
            return h.includes("last name") || h.includes("lastname") || h.includes("surname") || h.includes("family name") || h.includes("family-name");
          });
          let filled = 0;
          for (const el of controls) {
            if (el.disabled || el.readOnly) continue;
            if (isHidden(el)) continue;
            const type = normalize(el.type || "");
            if (type === "hidden" || type === "submit" || type === "button" || type === "reset") continue;
            if (type === "password") continue;
            if (type === "file") continue;

            const hay = labelTextFor(el);
            if (!hay) continue;
            const key = chooseKey(el, hay);
            if (!key) continue;

            const value = values[key] || "";
            if (!value) continue;

            if (fillControl(el, value)) filled += 1;
          }
          return filled;
        }
        """

        try:
            result = await maybe_await(evaluate_fn(script, values))
            if isinstance(result, int):
                return result
        except Exception:
            return 0
        return 0

    async def _find_missing_required_fields(self, page: Any) -> list[dict[str, str]]:
        evaluate_fn = getattr(page, "evaluate", None)
        if evaluate_fn is None:
            return []

        script = """
        () => {
          const normalize = (v) => (v || "").toLowerCase().replace(/\\s+/g, " ").trim();

          const labelTextFor = (el) => {
            const parts = [];
            const aria = el.getAttribute("aria-label");
            if (aria) parts.push(aria);
            const labelledBy = el.getAttribute("aria-labelledby");
            if (labelledBy) {
              for (const id of labelledBy.split(/\\s+/)) {
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
            if (root) {
              const legend = root.querySelector("legend");
              if (legend?.innerText) parts.push(legend.innerText);
              const labelLike = root.querySelector("label,h3,h4,p,strong,span,div");
              if (labelLike?.innerText) parts.push(labelLike.innerText);
            }
            const placeholder = el.getAttribute("placeholder");
            if (placeholder) parts.push(placeholder);
            const name = el.getAttribute("name");
            if (name) parts.push(name);
            return parts.join(" ").replace(/\\s+/g, " ").trim();
          };

          const cssPath = (el) => {
            if (!el) return "";
            const esc = (v) => (window.CSS && CSS.escape) ? CSS.escape(String(v || "")) : String(v || "");
            if (el.id) return "#" + esc(el.id);
            const name = el.getAttribute("name");
            if (name) return `${el.tagName.toLowerCase()}[name="${esc(name)}"]`;

            const parts = [];
            let cur = el;
            for (let i = 0; i < 5 && cur && cur.nodeType === 1; i += 1) {
              const tag = cur.tagName.toLowerCase();
              let index = 1;
              let sib = cur.previousElementSibling;
              while (sib) {
                if (sib.tagName === cur.tagName) index += 1;
                sib = sib.previousElementSibling;
              }
              parts.unshift(`${tag}:nth-of-type(${index})`);
              cur = cur.parentElement;
            }
            return parts.join(" > ");
          };

          const isHidden = (el) => {
            if (!el) return true;
            const ariaHidden = normalize(el.getAttribute("aria-hidden") || "");
            if (ariaHidden === "true") return true;
            const style = window.getComputedStyle(el);
            if (style.display === "none" || style.visibility === "hidden") return true;
            const opacity = parseFloat(style.opacity || "1");
            if (!Number.isNaN(opacity) && opacity < 0.05) return true;
            if (el.type && normalize(el.type) === "hidden") return true;
            return false;
          };

          const isRequired = (el) => {
            if (el.required) return true;
            const aria = el.getAttribute("aria-required");
            if (normalize(aria) === "true") return true;
            const name = normalize(el.getAttribute("name") || "");
            const id = normalize(el.getAttribute("id") || "");
            const cls = normalize(el.className || "");
            if (cls.includes("required")) return true;
            const label = normalize(labelTextFor(el));
            if (label.includes("*")) return true;
            if (label.includes("required")) return true;
            if (name.includes("required") || id.includes("required")) return true;
            return false;
          };

          const isEmpty = (el) => {
            const tag = normalize(el.tagName || "");
            const type = normalize(el.type || "");
            const role = normalize(el.getAttribute("role") || "");
            const hasPopup = normalize(el.getAttribute("aria-haspopup") || "");
            if (tag === "select") {
              const v = (el.value || "").trim();
              if (!v) return true;
              const opt = el.selectedOptions && el.selectedOptions.length ? el.selectedOptions[0] : null;
              const t = normalize(opt?.textContent || "");
              if (opt && opt.disabled && opt.selected) return true;
              if (t === "select" || t.includes("select one")) return true;
              return false;
            }
            if (type === "checkbox") return !el.checked;
            if (type === "radio") {
              const name = el.name || "";
              if (!name) return true;
              const anyChecked = Array.from(document.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`))
                .some((r) => r.checked);
              return !anyChecked;
            }
            if (type === "file") {
              const files = el.files || [];
              return !files.length && !(el.value || "").trim();
            }
            if (role === "combobox" || normalize(el.className || "").includes("select__input")) {
              const control = el.closest(".select__control") || el.parentElement;
              const hasValue = !!(control && (control.querySelector(".select__single-value") || control.querySelector(".select__multi-value")));
              return !hasValue;
            }
            if (hasPopup === "listbox") {
              try {
                const ariaVal = el.getAttribute("aria-valuetext") || el.getAttribute("aria-value") || "";
                const t = normalize(ariaVal);
                if (t && !t.includes("select") && !t.includes("choose") && !t.includes("pick")) return false;
              } catch (e) {}
              const txt = normalize(el.innerText || el.textContent || "");
              if (txt && !txt.includes("select") && !txt.includes("choose") && !txt.includes("pick")) return false;
              return true;
            }
            const v = (el.value || "").trim();
            // Workday multiSelect: the input is always empty but selections appear as chips
            if (!v && el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id="multiselectInputContainer"]')) {
              const container = el.closest('[data-automation-id="multiSelectContainer"]') || el.closest('[data-automation-id="multiselectInputContainer"]');
              // Check for "N items selected" text or actual selection chips
              const ariaDesc = el.getAttribute("aria-describedby") || "";
              if (ariaDesc) {
                const descEl = document.getElementById(ariaDesc);
                const descText = normalize(descEl?.textContent || "");
                if (descText && !descText.startsWith("0 items")) return false;
              }
              const chips = container.querySelectorAll('[data-automation-id="selectedItem"], [role="option"]');
              if (chips.length > 0) return false;
              const label = container.querySelector('[data-automation-id="promptSelectionLabel"]');
              if (label && (label.textContent || "").trim()) return false;
            }
            return !v;
          };

          const missing = [];
          const controls = Array.from(document.querySelectorAll("input,textarea,select,[role='combobox'],[aria-haspopup='listbox']"));
          for (const el of controls) {
            if (el.disabled || el.readOnly) continue;
            if (isHidden(el)) continue;
            const type = normalize(el.type || "");
            if (type === "submit" || type === "button" || type === "reset" || type === "password") continue;

            if (!isRequired(el)) continue;
            if (!isEmpty(el)) continue;

            // Highlight for human-in-loop.
            try { el.style.outline = "3px solid #ff4d4f"; el.style.outlineOffset = "2px"; } catch (e) {}

            missing.push({
              label: (labelTextFor(el) || "").trim().slice(0, 240),
              selector: cssPath(el),
              tag: (el.tagName || "").toLowerCase(),
              type: (el.type || "").toLowerCase()
            });
          }
          return missing;
        }
        """
        try:
            result = await maybe_await(evaluate_fn(script))
            if isinstance(result, list):
                out: list[dict[str, str]] = []
                for item in result:
                    if not isinstance(item, dict):
                        continue
                    label = str(item.get("label") or "").strip()
                    selector = str(item.get("selector") or "").strip()
                    tag = str(item.get("tag") or "").strip()
                    typ = str(item.get("type") or "").strip()
                    out.append(
                        {
                            "label": label,
                            "selector": selector,
                            "tag": tag,
                            "type": typ,
                        }
                    )
                return out
        except Exception:
            return []
        return []

    async def _fill_name_like_fields(self, page: Any, full_name: str) -> int:
        if not full_name.strip():
            return 0

        evaluate_fn = getattr(page, "evaluate", None)
        if evaluate_fn is None:
            return 0

        script = """
        (fullName) => {
          const normalize = (v) => (v || "").toLowerCase().replace(/\\s+/g, " ").trim();
          const safeFocus = (el) => {
            try { el && el.focus && el.focus({ preventScroll: true }); } catch (e) {}
          };
          let filled = 0;

          const labels = Array.from(document.querySelectorAll("label,legend,p,span,div,strong"));
          const visited = new Set();

          function getControl(node) {
            if (!node) return null;
            if (node.tagName === "LABEL") {
              if (node.htmlFor) {
                const byFor = document.getElementById(node.htmlFor);
                if (byFor) return byFor;
              }
              const nested = node.querySelector("input,textarea");
              if (nested) return nested;
            }
            let root = node.closest("fieldset,section,div,li,tr,td,dd,form") || node.parentElement;
            while (root) {
              const candidate = root.querySelector("input[type='text'],input:not([type]),textarea");
              if (candidate) return candidate;
              root = root.parentElement;
            }
            return null;
          }

          for (const node of labels) {
            const text = normalize(node.innerText);
            if (!text || text.length < 3) continue;
            if (!text.includes("name")) continue;
            if (text.includes("first") || text.includes("last") || text.includes("preferred")) continue;

            const control = getControl(node);
            if (!control || visited.has(control)) continue;
            if (control.disabled || control.readOnly) continue;
            if ((control.value || "").trim()) continue;

            safeFocus(control);
            control.value = fullName;
            control.dispatchEvent(new Event("input", { bubbles: true }));
            control.dispatchEvent(new Event("change", { bubbles: true }));
            visited.add(control);
            filled += 1;
          }
          return filled;
        }
        """

        try:
            result = await maybe_await(evaluate_fn(script, full_name))
            if isinstance(result, int):
                return result
        except Exception:
            return 0
        return 0

    # =========================================================================
    # Unified snapshot-and-fill: one single pass over all visible form fields.
    # For dropdowns we ALWAYS click-open → read options → pick best → click.
    # For text inputs we only fill if empty.
    # For checkboxes we only check if unchecked and answer is truthy.
    # =========================================================================

    async def _pre_fill_special_controls(self, page: Any, ctx: Any) -> None:
        """Override in ATS subclasses to handle non-standard controls before snapshot-and-fill."""
        pass

    def _get_snapshot_skip_labels(self) -> list[str]:
        """Override in ATS subclasses to skip certain labels in snapshot-and-fill.
        Returns list of lowercase substrings. If any substring is found in a label,
        the field will be skipped."""
        return []

    async def _snapshot_and_fill_all_fields(self, page: Any, ctx: ApplyContext) -> tuple[int, int]:
        """
        Single-pass form filler.  Returns (fields_filled, custom_questions_count).

        1. JS snapshot of every visible, empty/unselected form control.
        2. For each control decide the answer (answer-bank → standard fields → LLM).
        3. Fill using the right strategy per control type (dropdown-first, etc.).
        """
        import asyncio as _asyncio

        evaluate_fn = getattr(page, "evaluate", None)
        locator_fn = getattr(page, "locator", None)
        keyboard = getattr(page, "keyboard", None)
        if evaluate_fn is None or locator_fn is None:
            return (0, 0)

        # --- Build the answer mappings (same as before) ---
        how_heard_answer = "Online Job Board"
        template_values: dict[str, str] = {}
        try:
            template_values.update(dict(getattr(ctx.profile, "standard_fields", {}) or {}))
        except Exception:
            pass
        template_values.setdefault("company", ctx.company)
        template_values.setdefault("role", ctx.role)
        template_values.setdefault("how_heard", how_heard_answer)

        bank_mappings: list[tuple[str, str]] = []
        try:
            for needle, answer in list(getattr(ctx.profile, "question_bank", []) or []):
                bank_mappings.append((str(needle), expand_placeholders(str(answer), template_values)))
        except Exception:
            pass

        mappings: list[tuple[str, str]] = []
        mappings.extend(bank_mappings)
        mappings.extend([
            ("how did you hear", how_heard_answer),
            ("where did you hear", how_heard_answer),
            ("source", how_heard_answer),
            ("start date", "Summer 2026"),
            ("when can you start", "Summer 2026"),
            ("available to start", "Summer 2026"),
            ("salary expectation", "Open / Negotiable"),
            ("compensation expectation", "Open / Negotiable"),
            ("desired salary", "Open / Negotiable"),
            ("referral", "No"),
            ("referred", "No"),
        ])

        standard_fields = dict(ctx.profile.standard_fields or {})

        # --- Step 1: JS snapshot of all visible form controls ---
        snapshot_script = r"""
        () => {
          const esc = (v) => {
            try { return (window.CSS && CSS.escape) ? CSS.escape(String(v || "")) : String(v || ""); }
            catch(e) { return String(v || ""); }
          };
          const normalize = (v) => (v || "").toLowerCase().replace(/\s+/g, " ").trim();

          const isHidden = (el) => {
            if (!el) return true;
            try {
              const rect = el.getBoundingClientRect();
              if (!rect || rect.width < 2 || rect.height < 2) return true;
            } catch(e) { return true; }
            let cur = el;
            for (let i = 0; i < 5 && cur; i++) {
              try {
                if ((cur.getAttribute && cur.getAttribute("aria-hidden")) === "true") return true;
                const st = window.getComputedStyle(cur);
                if (st.display === "none" || st.visibility === "hidden") return true;
              } catch(e) {}
              cur = cur.parentElement;
            }
            return false;
          };

          const selectorFor = (el) => {
            if (!el) return "";
            if (el.id) return "#" + esc(el.id);
            const name = el.getAttribute("name");
            if (name) return el.tagName.toLowerCase() + '[name="' + esc(name) + '"]';
            const parts = [];
            let cur = el;
            for (let i = 0; i < 5 && cur && cur.nodeType === 1; i++) {
              const tag = cur.tagName.toLowerCase();
              let idx = 1;
              let sib = cur.previousElementSibling;
              while (sib) { if (sib.tagName === cur.tagName) idx++; sib = sib.previousElementSibling; }
              parts.unshift(tag + ":nth-of-type(" + idx + ")");
              cur = cur.parentElement;
            }
            return parts.join(" > ");
          };

          const labelFor = (el) => {
            const ps = [];
            // Priority 1: fieldset > legend (Workday question text lives here)
            const fieldset = el.closest("fieldset");
            if (fieldset) {
              const legend = fieldset.querySelector("legend");
              if (legend && legend.innerText) ps.push(legend.innerText.trim());
            }
            const aria = el.getAttribute("aria-label");
            if (aria) ps.push(aria);
            if (el.id) {
              const lbl = document.querySelector('label[for="' + esc(el.id) + '"]');
              if (lbl && lbl.innerText) ps.push(lbl.innerText);
            }
            const wrap = el.closest("label");
            if (wrap && wrap.innerText) ps.push(wrap.innerText);
            if (!fieldset) {
              let root = el.closest("section,div,li,tr,td,dd");
              for (let i = 0; i < 2 && root; i++) {
                const lbl = root.querySelector("label,legend,h3,h4,p,strong");
                if (lbl && lbl.innerText) ps.push(lbl.innerText);
                root = root.parentElement;
              }
            }
            const ph = el.getAttribute("placeholder");
            if (ph) ps.push(ph);
            const nm = el.getAttribute("name");
            if (nm) ps.push(nm);
            return ps.join(" ").replace(/\s+/g, " ").trim();
          };

          const controls = [];

          // --- All <select> elements ---
          for (const el of document.querySelectorAll("select")) {
            if (el.disabled || isHidden(el)) continue;
            const val = el.value || "";
            const selectedText = el.options[el.selectedIndex]?.text || "";
            const isDefault = !val || normalize(selectedText).includes("select") || normalize(selectedText).includes("choose") || el.selectedIndex === 0;
            if (!isDefault) continue; // already has a real value
            const options = [];
            for (const opt of el.options) {
              const t = (opt.text || "").trim();
              if (t && !normalize(t).match(/^(select|choose|pick|--)/)) options.push(t);
            }
            controls.push({
              kind: "select", selector: selectorFor(el), label: labelFor(el),
              value: "", options, tag: "select"
            });
          }

          // --- All combobox/autocomplete inputs ---
          for (const el of document.querySelectorAll("[role='combobox'], [aria-haspopup='listbox']")) {
            if (el.disabled || isHidden(el)) continue;
            const tag = el.tagName.toLowerCase();
            if (tag !== "input" && tag !== "button" && Number(el.getAttribute("tabindex") || 0) < 0) continue;
            // Check if already has value
            const val = normalize(el.value || el.getAttribute("aria-valuetext") || "");
            if (val && !val.includes("select") && !val.includes("choose")) continue;
            // Check react-select single value
            const ctrl = el.closest(".select__control") || el.parentElement;
            if (ctrl) {
              const sv = ctrl.querySelector(".select__single-value");
              if (sv && normalize(sv.innerText || "").length > 0) continue;
            }
            controls.push({
              kind: "combobox", selector: selectorFor(el), label: labelFor(el),
              value: "", options: [], tag
            });
          }

          // --- Workday custom dropdown buttons (button inside fieldset, text = "Select One") ---
          const seenComboSelectors = new Set(controls.filter(c => c.kind === "combobox").map(c => c.selector));
          for (const el of document.querySelectorAll("button[type='button']")) {
            if (el.disabled || isHidden(el)) continue;
            const btnText = normalize(el.innerText || "");
            const ariaLabel = normalize(el.getAttribute("aria-label") || "");
            if (!btnText.includes("select one") && !btnText.includes("select...") && !ariaLabel.includes("select one") && !ariaLabel.includes("select...")) continue;
            // Must be inside a fieldset (Workday question pattern)
            const fieldset = el.closest("fieldset");
            if (!fieldset) continue;
            const sel = selectorFor(el);
            if (seenComboSelectors.has(sel)) continue;
            controls.push({
              kind: "combobox", selector: sel, label: labelFor(el),
              value: "", options: [], tag: "button"
            });
          }

          // --- All text/textarea/url/tel inputs ---
          for (const el of document.querySelectorAll("input[type='text'], input[type='url'], input[type='tel'], input[type='email'], input:not([type]), textarea")) {
            if (el.disabled || el.readOnly || isHidden(el)) continue;
            const type = (el.getAttribute("type") || "").toLowerCase();
            if (["checkbox","radio","hidden","file","submit","button","image","reset"].includes(type)) continue;
            // Skip if role=combobox (already captured above)
            if (el.getAttribute("role") === "combobox" || el.getAttribute("aria-haspopup") === "listbox") continue;
            // Skip if this input is a sibling/child of a Workday dropdown button already captured
            const sibBtn = el.parentElement?.querySelector("button[type='button']");
            if (sibBtn && seenComboSelectors.has(selectorFor(sibBtn))) continue;
            const val = (el.value || "").trim();
            if (val) continue; // already filled
            controls.push({
              kind: "text", selector: selectorFor(el), label: labelFor(el),
              value: "", options: [], tag: el.tagName.toLowerCase()
            });
          }

          // --- All unchecked checkboxes ---
          for (const el of document.querySelectorAll("input[type='checkbox']")) {
            if (el.disabled || el.checked || isHidden(el)) continue;
            controls.push({
              kind: "checkbox", selector: selectorFor(el), label: labelFor(el),
              value: "false", options: [], tag: "input"
            });
          }

          // --- Radio groups (unchecked) ---
          const radioNames = {};
          for (const el of document.querySelectorAll("input[type='radio']")) {
            if (el.disabled || isHidden(el)) continue;
            const nm = el.getAttribute("name") || "";
            if (!nm) continue;
            if (!radioNames[nm]) radioNames[nm] = { options: [], anyChecked: false, label: "", selector: "" };
            if (el.checked) radioNames[nm].anyChecked = true;
            const optLabel = labelFor(el);
            radioNames[nm].options.push({ label: optLabel, value: el.value || "", selector: selectorFor(el) });
            if (!radioNames[nm].label) {
              // Use group-level label from fieldset/legend
              const fs = el.closest("fieldset");
              if (fs) { const lg = fs.querySelector("legend"); if (lg) radioNames[nm].label = lg.innerText; }
              if (!radioNames[nm].label) {
                let p = el.parentElement;
                for (let i = 0; i < 5 && p; i++) {
                  const h = p.querySelector("label,h3,h4,p.question,legend,strong");
                  if (h && h.innerText && h.innerText.length > 3) { radioNames[nm].label = h.innerText; break; }
                  p = p.parentElement;
                }
              }
            }
          }
          for (const [nm, rg] of Object.entries(radioNames)) {
            if (rg.anyChecked || !rg.label) continue;
            controls.push({
              kind: "radio", selector: "", label: rg.label.replace(/\s+/g, " ").trim(),
              value: "", options: rg.options.map(o => o.label), tag: "input",
              _radioOptions: rg.options
            });
          }

          return controls;
        }
        """

        try:
            controls = await maybe_await(evaluate_fn(snapshot_script))
        except Exception:
            logger.debug("  Snapshot script failed.", exc_info=True)
            return (0, 0)

        if not isinstance(controls, list) or not controls:
            logger.info("  Snapshot found 0 empty form controls.")
            return (0, 0)

        # Summarize what was found
        kinds = {}
        for c in controls:
            k = c.get("kind", "?")
            kinds[k] = kinds.get(k, 0) + 1
        logger.info("  Snapshot found %d empty controls: %s", len(controls), kinds)

        # --- Standard field keywords for quick matching ---
        std_field_keywords = {
            "first_name": ["first name", "first_name", "firstname"],
            "last_name": ["last name", "last_name", "lastname", "surname"],
            "full_name": ["full name", "full_name", "your name"],
            "email": ["email", "e-mail"],
            "phone": ["phone", "telephone", "mobile", "cell"],
            "linkedin": ["linkedin"],
            "github": ["github"],
            "school": ["school", "university", "college", "institution"],
            "degree": ["degree"],
            "gpa": ["gpa", "grade point"],
            "graduation": ["graduation", "grad date", "grad year"],
        }

        fields_filled = 0
        custom_q_count = 0
        skip_labels = self._get_snapshot_skip_labels()

        for i, ctrl in enumerate(controls):
            kind = ctrl.get("kind", "")
            selector = str(ctrl.get("selector") or "").strip()
            label = str(ctrl.get("label") or "").strip()
            options = ctrl.get("options") or []

            if not label and not selector:
                continue

            # Skip labels handled by pre-fill (e.g. Workday directory pickers)
            if skip_labels:
                label_lower = label.lower()
                if any(skip in label_lower for skip in skip_labels):
                    logger.debug("  [%d] SKIP (pre-fill handled): %s", i+1, label[:50])
                    continue

            label_preview = label[:80] + ("..." if len(label) > 80 else "")
            logger.debug("  [%d/%d] %s: %s (sel=%s)", i+1, len(controls), kind, label_preview, selector[:40] if selector else "?")

            # --- Determine the answer ---
            answer: str | None = None
            answer_source = ""
            _skip_field = False

            # 1. Try answer bank match FIRST (most specific, user-defined patterns)
            matched = match_question_bank(label, mappings)
            if matched is not None and not is_human_sentinel(matched):
                if matched.strip().lower() == "__skip__":
                    logger.debug("  [%d] SKIP (answer_bank __skip__): %s", i+1, label_preview)
                    _skip_field = True
                else:
                    answer = matched
                    answer_source = "answer_bank"

            if _skip_field:
                continue

            # 2. Try standard fields (first/last name, email, etc.)
            if answer is None:
                label_lower = label.lower()
                for sf_key, keywords in std_field_keywords.items():
                    if any(kw in label_lower for kw in keywords):
                        val = standard_fields.get(sf_key, "")
                        if val:
                            answer = val
                            answer_source = f"standard_field:{sf_key}"
                            break

            # 3. For custom questions (text/textarea only), use LLM
            if answer is None and kind in ("text", "combobox") and len(label) >= 12:
                # Skip standard hint fields for LLM
                std_hints = {"first name", "last name", "email", "phone", "linkedin",
                             "github", "school", "university", "degree", "gpa",
                             "graduation", "resume", "cv"}
                label_norm = normalize_text(label)
                if not any(h in label_norm for h in std_hints):
                    try:
                        llm_answer = await ctx.question_answerer.answer(
                            label,
                            company=ctx.company,
                            role=ctx.role,
                            profile_summary=ctx.profile.summary,
                            resume_text=ctx.profile.resume_text,
                            job_description=ctx.job_description,
                            question_bank=list(getattr(ctx.profile, "question_bank", []) or []),
                            template_values=standard_fields,
                            source=ctx.source,
                        )
                        if llm_answer and str(llm_answer).strip():
                            answer = str(llm_answer).strip()
                            answer_source = "llm"
                            custom_q_count += 1
                    except Exception:
                        logger.debug("  LLM answer failed for: %s", label_preview, exc_info=True)

            if not answer:
                logger.debug("  [%d] SKIP (no answer): %s", i+1, label_preview)
                continue

            ans_preview = answer[:50] + ("..." if len(answer) > 50 else "")
            logger.info("  [%d] %s -> '%s' (via %s)", i+1, label_preview[:50], ans_preview, answer_source)

            # --- Fill the control based on its kind ---
            try:
                if kind == "select":
                    filled = await self._fill_select_dropdown(page, selector, answer, options)
                elif kind == "combobox":
                    filled = await self._fill_combobox_dropdown(page, selector, answer, label, question_answerer=ctx.question_answerer)
                elif kind == "checkbox":
                    truthy = {"yes", "true", "y", "1", "on", "checked", "i agree",
                              "i acknowledge", "i acknowledge and agree", "i consent"}
                    ans_norm = answer.strip().lower()
                    if ans_norm in truthy:
                        filled = await self._check_checkbox(page, selector)
                    else:
                        filled = True  # answer is "false" / "no" -- intentionally not checking
                elif kind == "radio":
                    radio_opts = ctrl.get("_radioOptions") or []
                    filled = await self._fill_radio_group(page, answer, radio_opts)
                elif kind == "text":
                    filled = await self._fill_text_field(page, selector, answer)
                else:
                    filled = False

                if filled:
                    fields_filled += 1
            except Exception:
                logger.debug("  [%d] fill error: %s", i+1, label_preview, exc_info=True)

        return (fields_filled, custom_q_count)

    # --- Individual fill strategies ---

    @staticmethod
    def _best_option_match(answer: str, options: list[str]) -> tuple[int, int]:
        """
        Pure-logic option matcher (no Playwright).  Returns (best_index, best_score).
        Score 0 means no match.  Called by both select and combobox handlers.
        """
        ans_norm = normalize_text(answer)
        if not ans_norm:
            return (-1, 0)

        ans_tokens = [t for t in ans_norm.split() if len(t) >= 2]
        best_idx = -1
        best_score = 0

        for idx, opt_text in enumerate(options):
            opt_norm = normalize_text(opt_text)
            if not opt_norm:
                continue
            score = 0

            # Exact match
            if ans_norm == opt_norm:
                return (idx, 10000)
            # Answer is substring of option
            if ans_norm in opt_norm:
                score = max(score, 500 + len(ans_norm))
            # Option is substring of answer
            if opt_norm in ans_norm:
                score = max(score, 400 + len(opt_norm))
            # Token overlap
            if score == 0:
                token_hits = sum(1 for t in ans_tokens if t in opt_norm)
                if token_hits > 0:
                    score = token_hits * 30

            if score > best_score:
                best_score = score
                best_idx = idx

        return (best_idx, best_score)

    async def _fill_select_dropdown(self, page: Any, selector: str, answer: str, options: list[str]) -> bool:
        """Native <select> element: use Playwright's select_option with best-match label."""
        locator_fn = getattr(page, "locator", None)
        if not locator_fn or not selector:
            return False

        best_idx, best_score = self._best_option_match(answer, options)

        if best_idx < 0:
            logger.debug("    select: no matching option for '%s' in %s", answer[:30], [o[:20] for o in options[:5]])
            return False

        best_opt = options[best_idx]
        loc = locator_fn(selector).first
        try:
            await maybe_await(loc.select_option(label=best_opt, timeout=3000))
            logger.debug("    select: picked '%s' (score=%d)", best_opt[:40], best_score)
            return True
        except Exception:
            try:
                await maybe_await(loc.select_option(value=best_opt, timeout=2000))
                return True
            except Exception:
                return False

    async def _fill_combobox_dropdown(self, page: Any, selector: str, answer: str, label: str, *, question_answerer: Any = None) -> bool:
        """
        Combobox / react-select / autocomplete.

        Strategy (DROPDOWN-FIRST):
        1. Click to open the dropdown.
        2. Read ALL visible options.
        3. Pick the best fuzzy match.
        4. If no fuzzy match, ask the LLM to pick from the actual options.
        5. Click it.
        6. Only if there are 0 options or >=25 (huge list), type to filter first.
        """
        locator_fn = getattr(page, "locator", None)
        keyboard_obj = getattr(page, "keyboard", None)
        if not locator_fn or not selector:
            return False

        import asyncio as _asyncio

        loc = locator_fn(selector).first
        try:
            if not await maybe_await(loc.is_visible()):
                return False
        except Exception:
            return False

        try:
            await maybe_await(loc.scroll_into_view_if_needed(timeout=2000))
        except Exception:
            pass

        press_fn = getattr(keyboard_obj, "press", None) if keyboard_obj else None
        type_fn = getattr(keyboard_obj, "type", None) if keyboard_obj else None

        # --- Phase 1: Click to open dropdown ---
        try:
            await maybe_await(loc.click(timeout=2000))
        except Exception:
            return False

        # Press ArrowDown to ensure the dropdown opens (react-select pattern)
        if press_fn:
            try:
                await maybe_await(press_fn("ArrowDown"))
            except Exception:
                pass

        await _asyncio.sleep(0.4)

        # --- Phase 2: Read ALL options ---
        option_texts, option_locs = await self._read_dropdown_options(page)
        logger.debug("    combobox phase1: found %d options after click-open", len(option_texts))

        # --- Phase 3: If too many options (>=25, close to our 30-cap) or 0 options, type to filter ---
        if len(option_texts) >= 25 or len(option_texts) == 0:
            logger.debug("    combobox: typing to filter (%d raw options)", len(option_texts))
            # Close any open dropdown first
            if press_fn:
                try:
                    await maybe_await(press_fn("Escape"))
                except Exception:
                    pass
                await _asyncio.sleep(0.1)

            # Build filter attempts: full answer, then progressively shorter word prefixes
            words = answer.replace(",", " ").split()
            filter_attempts = [answer]
            # Add progressively shorter prefixes (at least 2 words)
            for end in range(len(words) - 1, 1, -1):
                prefix = " ".join(words[:end])
                if prefix != answer:
                    filter_attempts.append(prefix)
            # Also try just the first word if it's long enough
            if len(words) >= 1 and len(words[0]) >= 3 and words[0] != answer:
                filter_attempts.append(words[0])

            for attempt in filter_attempts:
                # Re-click, clear, and type this attempt
                try:
                    await maybe_await(loc.click(timeout=2000))
                except Exception:
                    pass
                if press_fn:
                    for key in ("Control+A", "Meta+A", "Backspace"):
                        try:
                            await maybe_await(press_fn(key))
                        except Exception:
                            pass
                if type_fn and attempt:
                    try:
                        await maybe_await(type_fn(attempt, delay=30))
                    except Exception:
                        pass
                await _asyncio.sleep(0.6)

                option_texts, option_locs = await self._read_dropdown_options(page)
                logger.debug("    combobox filter '%s': found %d options", attempt[:30], len(option_texts))

                if option_texts:
                    break  # Found some options, proceed to Phase 4

        # --- Phase 4: Pick best match and click ---
        if option_texts and option_locs:
            best_idx, best_score = self._best_option_match(answer, option_texts)

            # If no good match but only 1 option, take it
            if best_idx < 0 and len(option_texts) == 1:
                best_idx = 0
                best_score = 1

            if best_idx >= 0:
                try:
                    await maybe_await(option_locs[best_idx].click(timeout=2000))
                    logger.debug("    combobox: clicked '%s' (score=%d, of %d opts)",
                                 option_texts[best_idx][:40], best_score, len(option_texts))
                    return True
                except Exception:
                    logger.debug("    combobox: click failed on option %d", best_idx, exc_info=True)
            else:
                # --- Phase 4b: LLM fallback — ask the LLM to pick from actual options ---
                logger.debug("    combobox: no fuzzy match for '%s' in %d options: %s",
                             answer[:30], len(option_texts),
                             [o[:25] for o in option_texts[:5]])
                if question_answerer is not None:
                    pick_fn = getattr(question_answerer, "pick_option", None)
                    if pick_fn:
                        logger.info("    combobox: asking LLM to pick from %d options for '%s'",
                                    len(option_texts), label[:40])
                        try:
                            llm_idx = pick_fn(
                                question_label=label,
                                intended_answer=answer,
                                options=option_texts,
                            )
                            if llm_idx is not None and 0 <= llm_idx < len(option_locs):
                                await maybe_await(option_locs[llm_idx].click(timeout=2000))
                                logger.debug("    combobox: LLM picked '%s' (#%d of %d)",
                                             option_texts[llm_idx][:40], llm_idx, len(option_texts))
                                return True
                        except Exception:
                            logger.debug("    combobox: LLM pick failed", exc_info=True)

        # --- Phase 5: Last resort — type and Tab/Enter ---
        logger.debug("    combobox: falling back to type+Tab for '%s'", answer[:30])
        if press_fn:
            try:
                await maybe_await(press_fn("Escape"))
            except Exception:
                pass
            await _asyncio.sleep(0.1)
        try:
            await maybe_await(loc.click(timeout=2000))
        except Exception:
            pass
        if press_fn:
            for key in ("Control+A", "Meta+A", "Backspace"):
                try:
                    await maybe_await(press_fn(key))
                except Exception:
                    pass
        if type_fn and answer:
            try:
                await maybe_await(type_fn(answer, delay=20))
            except Exception:
                pass
        if press_fn:
            try:
                await maybe_await(press_fn("Tab"))
            except Exception:
                pass
        return True

    async def _read_dropdown_options(self, page: Any) -> tuple[list[str], list[Any]]:
        """Read all visible dropdown/listbox options. Returns (texts, locators)."""
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return ([], [])

        # Try several common dropdown selectors
        selectors = [
            "[role='listbox'] [role='option']",
            ".select__menu [role='option']",
            ".select__menu .select__option",
            "[data-automation-id='promptOption']",
            ".autocomplete-results li",
            ".pac-container .pac-item",
            "ul[role='listbox'] li",
        ]

        for sel in selectors:
            try:
                opts_loc = locator_fn(sel)
                count = await maybe_await(opts_loc.count())
                if count and count > 0:
                    texts = []
                    locs = []
                    for idx in range(min(int(count), 30)):
                        opt = opts_loc.nth(idx)
                        try:
                            vis = await maybe_await(opt.is_visible())
                            if not vis:
                                continue
                        except Exception:
                            continue
                        try:
                            txt = str(await maybe_await(opt.inner_text()) or "").strip()
                        except Exception:
                            txt = ""
                        if txt:
                            texts.append(txt)
                            locs.append(opt)
                    if texts:
                        return (texts, locs)
            except Exception:
                continue

        return ([], [])

    async def _check_checkbox(self, page: Any, selector: str) -> bool:
        locator_fn = getattr(page, "locator", None)
        if not locator_fn or not selector:
            return False
        try:
            loc = locator_fn(selector).first
            check_fn = getattr(loc, "check", None)
            if check_fn:
                await maybe_await(check_fn(timeout=2000))
            else:
                await maybe_await(loc.click(timeout=2000))
            return True
        except Exception:
            return False

    async def _fill_radio_group(self, page: Any, answer: str, radio_options: list[dict]) -> bool:
        locator_fn = getattr(page, "locator", None)
        if not locator_fn or not radio_options:
            return False

        ans_norm = normalize_text(answer)
        best_opt = None
        best_score = -1
        for opt in radio_options:
            opt_label = normalize_text(opt.get("label") or opt.get("value") or "")
            if not opt_label:
                continue
            if ans_norm == opt_label:
                best_opt = opt
                best_score = 10000
                break
            if ans_norm in opt_label or opt_label in ans_norm:
                score = len(ans_norm)
                if score > best_score:
                    best_opt = opt
                    best_score = score

        if best_opt is None:
            return False

        sel = str(best_opt.get("selector") or "").strip()
        if not sel:
            return False
        try:
            loc = locator_fn(sel).first
            check_fn = getattr(loc, "check", None)
            if check_fn:
                await maybe_await(check_fn(timeout=2000))
            else:
                await maybe_await(loc.click(timeout=2000))
            return True
        except Exception:
            return False

    async def _fill_text_field(self, page: Any, selector: str, answer: str) -> bool:
        locator_fn = getattr(page, "locator", None)
        if not locator_fn or not selector:
            return False
        try:
            loc = locator_fn(selector).first
            # Double-check it's still empty
            try:
                current = str(await maybe_await(loc.input_value()) or "").strip()
            except Exception:
                try:
                    current = str(await maybe_await(loc.inner_text()) or "").strip()
                except Exception:
                    current = ""
            if current:
                logger.debug("    text: already has value '%s', skipping.", current[:30])
                return False
            await maybe_await(loc.fill(answer, timeout=3000))
            return True
        except Exception:
            return False

    async def _fill_autocomplete_input(
        self, page: Any, selector: str, answer: str
    ) -> bool:
        """
        Handle autocomplete/combobox text inputs (e.g. Greenhouse's Location, dropdown questions).

        Strategy: click the input, clear it, type slowly to trigger autocomplete,
        wait for a dropdown to appear, and click the best-matching option.
        Falls back to just typing + pressing Enter if no dropdown appears.
        """
        locator_fn = getattr(page, "locator", None)
        keyboard = getattr(page, "keyboard", None)
        if locator_fn is None or keyboard is None:
            return False

        try:
            loc = locator_fn(selector)
            if await maybe_await(loc.count()) <= 0:
                return False
            first = loc.first
            if not await maybe_await(first.is_visible()):
                return False

            # Check if the element is an interactive input (not hidden checkbox, etc.)
            tag = str(await maybe_await(first.evaluate("el => el.tagName")) or "").lower()
            input_type = str(
                await maybe_await(first.get_attribute("type")) or ""
            ).strip().lower()
            if tag != "input" or input_type in ("checkbox", "radio", "hidden", "file"):
                return False

            # Click to focus and open any autocomplete
            try:
                await maybe_await(first.scroll_into_view_if_needed(timeout=2000))
            except Exception:
                pass
            await maybe_await(first.click(timeout=2000))

            # Clear existing value
            press_fn = getattr(keyboard, "press", None)
            type_fn = getattr(keyboard, "type", None)
            if press_fn:
                try:
                    await maybe_await(press_fn("Meta+A"))
                except Exception:
                    pass
                try:
                    await maybe_await(press_fn("Control+A"))
                except Exception:
                    pass
                try:
                    await maybe_await(press_fn("Backspace"))
                except Exception:
                    pass

            # Type slowly to trigger autocomplete dropdown
            if type_fn and len(answer) <= 200:
                await maybe_await(type_fn(answer, delay=30))
            else:
                await maybe_await(first.fill(answer, timeout=2000))

            # Wait a bit for autocomplete dropdown to appear
            import asyncio as _asyncio
            await _asyncio.sleep(0.5)

            # Look for autocomplete dropdown options
            dropdown_clicked = False
            dropdown_selectors = [
                "[role='listbox'] [role='option']",
                ".autocomplete-results li",
                ".suggestions li",
                ".pac-container .pac-item",  # Google Places autocomplete
                "[class*='dropdown'] [class*='option']",
                "[class*='suggestion']",
                "[class*='result'] li",
            ]

            ans_norm = normalize_text(answer)
            ans_tokens = [t for t in ans_norm.split() if len(t) >= 3]

            for dd_sel in dropdown_selectors:
                try:
                    options_loc = locator_fn(dd_sel)
                    await maybe_await(options_loc.first.wait_for(state="visible", timeout=800))
                    option_count = await maybe_await(options_loc.count())
                    if option_count > 0:
                        # Find best matching option
                        best_idx = 0
                        best_score = 0
                        for idx in range(min(int(option_count), 20)):
                            try:
                                txt = str(await maybe_await(options_loc.nth(idx).inner_text()) or "")
                                opt_norm = normalize_text(txt)
                                if not opt_norm:
                                    continue
                                score = 0
                                if ans_norm in opt_norm:
                                    score += 200 + len(ans_norm)
                                elif opt_norm in ans_norm:
                                    score += 150 + len(opt_norm)
                                else:
                                    for tok in ans_tokens:
                                        if tok in opt_norm:
                                            score += 10
                                if score > best_score:
                                    best_score = score
                                    best_idx = idx
                            except Exception:
                                continue

                        if best_score > 0 or option_count == 1:
                            try:
                                await maybe_await(options_loc.nth(best_idx).click(timeout=2000))
                                dropdown_clicked = True
                                logger.debug("  Autocomplete: clicked option %d (score=%d).", best_idx, best_score)
                                break
                            except Exception:
                                pass
                except Exception:
                    continue

            # If no dropdown appeared, try pressing Enter to confirm the typed value
            if not dropdown_clicked and press_fn:
                try:
                    await maybe_await(press_fn("Enter"))
                except Exception:
                    pass
                # Also try Tab to move focus (some forms validate on blur)
                try:
                    await maybe_await(press_fn("Tab"))
                except Exception:
                    pass

            return True
        except Exception:
            return False

    async def _extract_custom_question_candidates(self, page: Any, limit: int = 5) -> list[dict[str, str]]:
        evaluate_fn = getattr(page, "evaluate", None)
        if evaluate_fn is None:
            return []

        script = """
        (limit) => {
          const standardHints = [
            "first name", "last name", "email", "phone", "linkedin", "github",
            "school", "university", "degree", "gpa", "graduation", "resume", "cv",
            "work authorization", "sponsorship", "relocate", "start date", "salary"
          ];

          const normalize = (v) => (v || "").toLowerCase().replace(/\\s+/g, " ").trim();

          function selectorFor(el) {
            if (!el) return "";
            const esc = (v) => (window.CSS && CSS.escape) ? CSS.escape(String(v || "")) : String(v || "");
            if (el.id) return "#" + esc(el.id);
            const name = el.getAttribute("name");
            if (name) return `${el.tagName.toLowerCase()}[name="${esc(name)}"]`;

            const parts = [];
            let cur = el;
            for (let i = 0; i < 5 && cur && cur.nodeType === 1; i += 1) {
              const tag = cur.tagName.toLowerCase();
              let index = 1;
              let sib = cur.previousElementSibling;
              while (sib) {
                if (sib.tagName === cur.tagName) index += 1;
                sib = sib.previousElementSibling;
              }
              parts.unshift(`${tag}:nth-of-type(${index})`);
              cur = cur.parentElement;
            }
            return parts.join(" > ");
          }

          function questionFor(el) {
            const id = el.id;
            if (id) {
              const byFor = document.querySelector(`label[for="${CSS.escape(id)}"]`);
              if (byFor?.innerText) return byFor.innerText;
            }
            const wrapLabel = el.closest("label");
            if (wrapLabel?.innerText) return wrapLabel.innerText;
            let root = el.closest("fieldset,section,div,li,tr,td,dd,form");
            while (root) {
              const labelLike = root.querySelector("label,legend,h3,p,strong");
              if (labelLike?.innerText) return labelLike.innerText;
              root = root.parentElement;
            }
            return el.placeholder || el.name || "";
          }

          const candidates = [];
          const inputs = Array.from(document.querySelectorAll("textarea, input[type='text'], input[type='url'], input:not([type])"));
          for (const el of inputs) {
            if (candidates.length >= limit) break;
            if (el.disabled || el.readOnly) continue;
            if ((el.value || "").trim()) continue;

            const q = normalize(questionFor(el));
            if (!q || q.length < 12) continue;
            if (standardHints.some((hint) => q.includes(hint))) continue;

            candidates.push({
              question: questionFor(el).trim().slice(0, 700),
              selector: selectorFor(el),
            });
          }
          return candidates;
        }
        """
        try:
            result = await maybe_await(evaluate_fn(script, limit))
            if isinstance(result, list):
                normalized: list[dict[str, str]] = []
                for item in result:
                    if not isinstance(item, dict):
                        continue
                    q = str(item.get("question") or "").strip()
                    s = str(item.get("selector") or "").strip()
                    if q:
                        normalized.append({"question": q, "selector": s})
                return normalized
        except Exception:
            return []
        return []

    async def _advance_multi_page_if_needed(self, page: Any, max_steps: int = 2) -> None:
        for _ in range(max_steps):
            clicked = await smart_click(
                page,
                prompt="If needed, click Next or Continue to proceed in the application form",
                selectors=self.next_selectors,
                text_candidates=["Next", "Continue", "Review"],
            )
            if not clicked:
                break

    async def _detect_auth_gate(self, page: Any) -> dict[str, str | bool]:
        evaluate_fn = getattr(page, "evaluate", None)
        if evaluate_fn is None:
            return {"detected": False}
        script = """
        () => {
          const bodyText = (document.body?.innerText || "").toLowerCase();
          const passwordInputs = document.querySelectorAll("input[type='password']");
          const authHints = [
            "create account",
            "sign in",
            "log in",
            "already have an account",
            "create your account",
            "password"
          ];
          const hintMatch = authHints.some((hint) => bodyText.includes(hint));
          const detected = passwordInputs.length > 0 && hintMatch;
          return {
            detected,
            reason: detected ? "Sign-in or account creation required before applying" : ""
          };
        }
        """
        try:
            result = await maybe_await(evaluate_fn(script))
            if isinstance(result, dict):
                return {
                    "detected": bool(result.get("detected")),
                    "reason": str(result.get("reason") or ""),
                }
        except Exception:
            return {"detected": False}
        return {"detected": False}

    async def _submit(self, page: Any) -> bool:
        for prompt in self.submit_prompts:
            if await smart_click(
                page,
                prompt=prompt,
                selectors=self.submit_selectors,
                text_candidates=["Submit", "Submit Application", "Send Application"],
            ):
                return True
        return await smart_click(
            page,
            selectors=self.submit_selectors,
            text_candidates=["Submit", "Submit Application", "Apply"],
        )

    async def _inject_captcha_token(self, page: Any, token: str) -> bool:
        evaluate_fn = getattr(page, "evaluate", None)
        if evaluate_fn is None:
            return False
        script = """
        (token) => {
          const recaptcha = document.getElementById("g-recaptcha-response")
            || document.querySelector("textarea[name='g-recaptcha-response']");
          const hcaptcha = document.querySelector("textarea[name='h-captcha-response']");
          const target = recaptcha || hcaptcha;
          if (!target) return false;
          target.value = token;
          target.dispatchEvent(new Event("input", { bubbles: true }));
          target.dispatchEvent(new Event("change", { bubbles: true }));
          return true;
        }
        """
        try:
            result = await maybe_await(evaluate_fn(script, token))
            return bool(result)
        except Exception:
            return False

    def _page_url(self, page: Any) -> str:
        try:
            value = getattr(page, "url", "")
            if callable(value):
                return str(value())
            return str(value or "")
        except Exception:
            return ""
