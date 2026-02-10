from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

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

    # Deterministic dispatch table: kind -> method name.
    # Each protocol has signature: async def _proto_X(self, page, answer, ctrl) -> bool
    # ATS subclasses can override to add/replace protocols.
    _FILL_PROTOCOLS: ClassVar[dict[str, str]] = {
        "native_select":           "_proto_native_select",
        "native_text":             "_proto_native_text",
        "native_radio":            "_proto_native_radio",
        "native_checkbox":         "_proto_native_checkbox",
        "checkbox_group":          "_proto_checkbox_group",
        "workday_date":            "_proto_workday_date",
        "aria_combobox":           "_proto_aria_combobox",
        "aria_radio":              "_proto_aria_radio",
        "aria_checkbox":           "_proto_aria_checkbox",
        "workday_button_dropdown": "_proto_workday_button_dropdown",
        "react_select":            "_proto_react_select",
    }

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
        _prev_page_signature: str | None = None  # stale-page detection
        _stale_count = 0  # how many times the page didn't change after "Next"
        for page_index in range(max(ctx.max_form_pages, 1)):
            logger.info("[%s] === Form page %d/%d ===", ctx.company, page_index + 1, ctx.max_form_pages)

            # ── Stale-page detection ──
            # Take a lightweight "signature" of the page (visible control IDs/names).
            # If it matches the previous iteration, Next didn't actually advance.
            page_sig = await self._page_signature(page)
            if page_sig and page_sig == _prev_page_signature:
                _stale_count += 1
                logger.warning(
                    "[%s] Page did not change after clicking Next (stale count=%d). "
                    "Likely a required field is blocking advancement.",
                    ctx.company, _stale_count,
                )
                if _stale_count >= 2:
                    logger.error(
                        "[%s] Stuck on the same page after %d attempts — breaking loop.",
                        ctx.company, _stale_count,
                    )
                    break
            else:
                _stale_count = 0
            _prev_page_signature = page_sig

            # Kick off cover letter generation concurrently on the first page.
            # The LLM call + PDF render don't touch the browser, so they run safely
            # in parallel with form filling. The PDF will be ready for upload later.
            if _cl_gen_task is None and _cl_pdf_path is None and not cover_letter_uploaded:
                _cl_gen_task = _asyncio.create_task(self._generate_cover_letter_pdf(ctx))

            # --- Pre-fill special controls FIRST (e.g. Workday directory pickers, page-specific widgets) ---
            # This also waits for the page to be ready (via _wait_for_page_ready override).
            await self._pre_fill_special_controls(page, ctx)

            # Only attempt file uploads when we're on a new page (not stuck on a stale one).
            if _stale_count == 0:
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
            ("start date", "06/01/2026"),
            ("when can you start", "06/01/2026"),
            ("available to start", "06/01/2026"),
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
              kind: "native_select", selector: selectorFor(el), label: labelFor(el),
              value: "", options, tag: "select"
            });
          }

          // --- ARIA combobox / autocomplete inputs ---
          for (const el of document.querySelectorAll("[role='combobox'], [aria-haspopup='listbox']")) {
            if (el.disabled || isHidden(el)) continue;
            const tag = el.tagName.toLowerCase();
            if (tag !== "input" && tag !== "button" && Number(el.getAttribute("tabindex") || 0) < 0) continue;
            // Check if already has value
            const val = normalize(el.value || el.getAttribute("aria-valuetext") || "");
            if (val && !val.includes("select") && !val.includes("choose")) continue;
            // Detect react-select by ancestor class
            const rsCtrl = el.closest(".select__control") || el.closest(".select__container");
            if (rsCtrl) {
              const sv = rsCtrl.querySelector(".select__single-value");
              if (sv && normalize(sv.innerText || "").length > 0) continue;
              controls.push({
                kind: "react_select", selector: selectorFor(el), label: labelFor(el),
                value: "", options: [], tag
              });
              continue;
            }
            // Check react-select single value on parent
            const ctrl = el.parentElement;
            if (ctrl) {
              const sv = ctrl.querySelector(".select__single-value");
              if (sv && normalize(sv.innerText || "").length > 0) continue;
            }
            controls.push({
              kind: "aria_combobox", selector: selectorFor(el), label: labelFor(el),
              value: "", options: [], tag
            });
          }

          // --- Workday custom dropdown buttons (button inside fieldset, text = "Select One") ---
          const seenComboSelectors = new Set(controls.filter(c => c.kind === "aria_combobox" || c.kind === "react_select").map(c => c.selector));
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
              kind: "workday_button_dropdown", selector: sel, label: labelFor(el),
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
              kind: "native_text", selector: selectorFor(el), label: labelFor(el),
              value: "", options: [], tag: el.tagName.toLowerCase()
            });
          }

          // --- All unchecked native checkboxes ---
          const groupedCheckboxSelectors = new Set();
          // First pass: detect checkbox groups inside formField containers
          for (const container of document.querySelectorAll("[data-automation-id^='formField']")) {
            const cbs = container.querySelectorAll("input[type='checkbox']");
            const visibleCbs = [];
            for (const cb of cbs) {
              if (!cb.disabled && !cb.checked && !isHidden(cb)) visibleCbs.push(cb);
            }
            if (visibleCbs.length < 2) continue;
            // Extract question text from legend or label in this container
            let qText = "";
            const legend = container.querySelector("legend");
            if (legend && legend.innerText) qText = legend.innerText.trim();
            if (!qText) {
              const lbl = container.querySelector("label");
              if (lbl && lbl.innerText) qText = lbl.innerText.trim();
            }
            if (!qText) {
              // Try the formField's own direct text child or first child element
              const formLabel = container.querySelector("[data-automation-id*='formLabel']");
              if (formLabel && formLabel.innerText) qText = formLabel.innerText.trim();
            }
            if (!qText) continue;
            // Remove individual option labels from question text
            const cbOptions = [];
            for (const cb of visibleCbs) {
              const sel = selectorFor(cb);
              groupedCheckboxSelectors.add(sel);
              const optLabel = labelFor(cb);
              cbOptions.push({ label: optLabel, value: cb.value || "", selector: sel });
            }
            controls.push({
              kind: "checkbox_group",
              selector: selectorFor(visibleCbs[0]),
              label: qText.replace(/\s+/g, " ").trim(),
              value: "",
              options: cbOptions.map(o => o.label),
              tag: "input",
              _checkboxOptions: cbOptions,
            });
          }
          // Second pass: individual unchecked checkboxes NOT in a group
          for (const el of document.querySelectorAll("input[type='checkbox']")) {
            if (el.disabled || el.checked || isHidden(el)) continue;
            const sel = selectorFor(el);
            if (groupedCheckboxSelectors.has(sel)) continue;
            controls.push({
              kind: "native_checkbox", selector: sel, label: labelFor(el),
              value: "false", options: [], tag: "input"
            });
          }

          // --- ARIA checkboxes (role="checkbox", not already checked) ---
          for (const el of document.querySelectorAll("[role='checkbox']")) {
            if (isHidden(el)) continue;
            if (el.getAttribute("aria-checked") === "true") continue;
            if (el.tagName.toLowerCase() === "input" && el.type === "checkbox") continue; // already captured above
            controls.push({
              kind: "aria_checkbox", selector: selectorFor(el), label: labelFor(el),
              value: "false", options: [], tag: el.tagName.toLowerCase()
            });
          }

          // --- Workday date pickers (MM/DD/YYYY spinbuttons) ---
          for (const wrapper of document.querySelectorAll("[data-automation-id='dateInputWrapper']")) {
            if (isHidden(wrapper)) continue;
            // Find the Month spinbutton input inside
            const monthInput = wrapper.querySelector("input[role='spinbutton'][id*='dateSectionMonth']");
            if (!monthInput) continue;
            // Already filled? Check if any spinbutton has a value
            const allSpinbuttons = wrapper.querySelectorAll("input[role='spinbutton']");
            let anyFilled = false;
            for (const sb of allSpinbuttons) {
              if ((sb.value || "").trim()) { anyFilled = true; break; }
            }
            if (anyFilled) continue;
            // Walk up to find the parent formField container for question text
            let qText = "";
            let parent = wrapper.parentElement;
            for (let i = 0; i < 8 && parent; i++) {
              const aid = parent.getAttribute("data-automation-id") || "";
              if (aid.startsWith("formField")) {
                const legend = parent.querySelector("legend");
                if (legend && legend.innerText) { qText = legend.innerText.trim(); break; }
                const lbl = parent.querySelector("label");
                if (lbl && lbl.innerText) { qText = lbl.innerText.trim(); break; }
              }
              parent = parent.parentElement;
            }
            if (!qText) continue;
            controls.push({
              kind: "workday_date",
              selector: selectorFor(monthInput),
              label: qText.replace(/\s+/g, " ").trim(),
              value: "",
              options: [],
              tag: "input",
            });
          }

          // --- Native radio groups (unchecked) ---
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
              kind: "native_radio", selector: "", label: rg.label.replace(/\s+/g, " ").trim(),
              value: "", options: rg.options.map(o => o.label), tag: "input",
              _radioOptions: rg.options
            });
          }

          // --- ARIA radio groups (role="radio", not inside a native radio group) ---
          const ariaRadioGroups = {};
          for (const el of document.querySelectorAll("[role='radio']")) {
            if (isHidden(el)) continue;
            // Skip if this is actually a native radio input already captured
            if (el.tagName.toLowerCase() === "input" && el.type === "radio") continue;
            // Group by closest radiogroup or fieldset
            const group = el.closest("[role='radiogroup']") || el.closest("fieldset");
            const groupKey = group ? selectorFor(group) : ("__aria_rg_" + controls.length);
            if (!ariaRadioGroups[groupKey]) ariaRadioGroups[groupKey] = { options: [], anyChecked: false, label: "" };
            const checked = el.getAttribute("aria-checked") === "true";
            if (checked) ariaRadioGroups[groupKey].anyChecked = true;
            const optLabel = (el.getAttribute("aria-label") || el.innerText || "").trim();
            ariaRadioGroups[groupKey].options.push({
              label: optLabel, value: optLabel, selector: selectorFor(el)
            });
            if (!ariaRadioGroups[groupKey].label && group) {
              const lg = group.querySelector("legend");
              if (lg && lg.innerText) ariaRadioGroups[groupKey].label = lg.innerText;
              if (!ariaRadioGroups[groupKey].label) {
                const lbl = group.getAttribute("aria-label");
                if (lbl) ariaRadioGroups[groupKey].label = lbl;
              }
              if (!ariaRadioGroups[groupKey].label) {
                let p = group.parentElement;
                for (let i = 0; i < 3 && p; i++) {
                  const h = p.querySelector("label,h3,h4,p.question,legend,strong");
                  if (h && h.innerText && h.innerText.length > 3) {
                    ariaRadioGroups[groupKey].label = h.innerText;
                    break;
                  }
                  p = p.parentElement;
                }
              }
            }
          }
          for (const [gk, rg] of Object.entries(ariaRadioGroups)) {
            if (rg.anyChecked || !rg.label || !rg.options.length) continue;
            controls.push({
              kind: "aria_radio", selector: "", label: rg.label.replace(/\s+/g, " ").trim(),
              value: "", options: rg.options.map(o => o.label), tag: "div",
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
            if answer is None and kind in ("native_text", "aria_combobox", "react_select", "workday_button_dropdown", "checkbox_group", "workday_date") and len(label) >= 12:
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

            # --- Fill the control based on its kind (deterministic protocol dispatch) ---
            try:
                method_name = self._FILL_PROTOCOLS.get(kind)
                if method_name:
                    protocol = getattr(self, method_name, None)
                    if protocol:
                        filled = await protocol(page, answer, ctrl, ctx=ctx)
                    else:
                        logger.debug("  [%d] no protocol method '%s' for kind '%s'", i+1, method_name, kind)
                        filled = False
                else:
                    logger.debug("  [%d] unknown control kind '%s', skipping", i+1, kind)
                    filled = False

                if filled:
                    fields_filled += 1
            except Exception:
                logger.debug("  [%d] fill error: %s", i+1, label_preview, exc_info=True)

        return (fields_filled, custom_q_count)

    # --- Deterministic fill protocols ---
    # Each has signature: async def _proto_X(self, page, answer, ctrl, *, ctx=None) -> bool

    async def _proto_native_select(self, page: Any, answer: str, ctrl: dict, *, ctx: Any = None) -> bool:
        """Native <select>: fuzzy-match answer to options, then Playwright select_option."""
        selector = str(ctrl.get("selector") or "").strip()
        options = ctrl.get("options") or []
        locator_fn = getattr(page, "locator", None)
        if not locator_fn or not selector:
            return False
        best_idx, best_score = self._best_option_match(answer, options)
        if best_idx < 0:
            logger.debug("    native_select: no match for '%s' in %s", answer[:30], [o[:20] for o in options[:5]])
            return False
        best_opt = options[best_idx]
        loc = locator_fn(selector).first
        try:
            await maybe_await(loc.select_option(label=best_opt, timeout=3000))
            logger.debug("    native_select: picked '%s' (score=%d)", best_opt[:40], best_score)
            return True
        except Exception:
            try:
                await maybe_await(loc.select_option(value=best_opt, timeout=2000))
                return True
            except Exception:
                return False

    async def _proto_native_text(self, page: Any, answer: str, ctrl: dict, *, ctx: Any = None) -> bool:
        """Native <input type='text'> / <textarea>: Playwright .fill() with emptiness pre-check."""
        selector = str(ctrl.get("selector") or "").strip()
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
                logger.debug("    native_text: already has value '%s', skipping.", current[:30])
                return False
            await maybe_await(loc.fill(answer, timeout=3000))
            return True
        except Exception:
            return False

    async def _proto_native_radio(self, page: Any, answer: str, ctrl: dict, *, ctx: Any = None) -> bool:
        """Native <input type='radio'>: fuzzy-match answer, Playwright .check() or JS fallback."""
        radio_options = ctrl.get("_radioOptions") or []
        locator_fn = getattr(page, "locator", None)
        if not locator_fn or not radio_options:
            return False

        best_opt = self._match_radio_option(answer, radio_options)
        if best_opt is None:
            return False

        sel = str(best_opt.get("selector") or "").strip()
        if sel:
            # Primary: Try clicking the associated <label> first (for React compatibility)
            try:
                clicked_label = await maybe_await(page.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
                        let label = null;
                        if (el.id) {
                            label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                        }
                        if (!label) {
                            label = el.closest('label');
                        }
                        if (label) {
                            label.scrollIntoView({block: 'center'});
                            label.click();
                            return true;
                        }
                        return false;
                    }""",
                    sel,
                ))
                if clicked_label:
                    return True
            except Exception:
                pass

            # Fallback: Playwright .check()
            try:
                loc = locator_fn(sel).first
                check_fn = getattr(loc, "check", None)
                if check_fn:
                    await maybe_await(check_fn(timeout=2000))
                else:
                    await maybe_await(loc.click(timeout=2000))
                return True
            except Exception:
                pass

        # JS fallback: find the element by selector or index and click via JS
        evaluate_fn = getattr(page, "evaluate", None)
        if sel and evaluate_fn:
            try:
                clicked = await maybe_await(evaluate_fn(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
                        // Try clicking the label instead of the radio input for React compatibility
                        let label = null;
                        if (el.id) {
                            label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                        }
                        if (!label) {
                            label = el.closest('label');
                        }
                        if (label) {
                            label.scrollIntoView({block: 'center'});
                            label.click();
                            return true;
                        }
                        // Fallback to clicking the radio directly if no label found
                        el.scrollIntoView({block:'center'});
                        el.click();
                        return true;
                    }""",
                    sel,
                ))
                if clicked:
                    return True
            except Exception:
                pass

        # Last resort: find by option index in the group
        if evaluate_fn:
            opt_idx = radio_options.index(best_opt) if best_opt in radio_options else -1
            group_name = str(best_opt.get("value") or "")
            if opt_idx >= 0:
                try:
                    # Try clicking the nth radio with the same name
                    first_opt = radio_options[0]
                    first_sel = str(first_opt.get("selector") or "")
                    clicked = await maybe_await(evaluate_fn(
                        """(args) => {
                            const radios = document.querySelectorAll('input[type="radio"]');
                            const matching = [];
                            for (const r of radios) {
                                const rect = r.getBoundingClientRect();
                                if (rect.width >= 1 && rect.height >= 1) matching.push(r);
                            }
                            // Try to find by value
                            for (const r of matching) {
                                if (r.value === args.value) {
                                    // Click the label instead of the radio input for React compatibility
                                    let label = null;
                                    if (r.id) {
                                        label = document.querySelector('label[for="' + CSS.escape(r.id) + '"]');
                                    }
                                    if (!label) {
                                        label = r.closest('label');
                                    }
                                    if (label) {
                                        label.scrollIntoView({block: 'center'});
                                        label.click();
                                        return true;
                                    }
                                    // Fallback to clicking the radio directly if no label found
                                    r.scrollIntoView({block:'center'});
                                    r.click();
                                    return true;
                                }
                            }
                            return false;
                        }""",
                        {"value": group_name},
                    ))
                    if clicked:
                        return True
                except Exception:
                    pass

        return False

    async def _proto_native_checkbox(self, page: Any, answer: str, ctrl: dict, *, ctx: Any = None) -> bool:
        """Native <input type='checkbox'>: check if answer is truthy, then Playwright .check()."""
        selector = str(ctrl.get("selector") or "").strip()
        locator_fn = getattr(page, "locator", None)
        if not locator_fn or not selector:
            return False
        truthy = {"yes", "true", "y", "1", "on", "checked", "i agree",
                  "i acknowledge", "i acknowledge and agree", "i consent"}
        ans_norm = answer.strip().lower()
        if ans_norm not in truthy:
            return True  # answer is "false" / "no" — intentionally not checking
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

    async def _proto_checkbox_group(self, page: Any, answer: str, ctrl: dict, *, ctx: Any = None) -> bool:
        """Checkbox group: fuzzy-match answer against option labels, click the matching label."""
        import asyncio as _asyncio
        cb_options = ctrl.get("_checkboxOptions") or []
        locator_fn = getattr(page, "locator", None)
        if not locator_fn or not cb_options:
            return False

        # Use the same fuzzy matching as radio buttons
        best_opt = self._match_radio_option(answer, cb_options)
        if best_opt is None:
            logger.debug("  checkbox_group: no matching option for answer '%s'", answer)
            return False

        sel = str(best_opt.get("selector") or "").strip()
        if not sel:
            return False

        logger.debug("  checkbox_group: best match sel='%s', label='%s'", sel, best_opt.get("label", ""))

        # Use Playwright locator to get the element handle (handles CSS escape sequences
        # like #\36 properly), then pass the handle to page.evaluate for label clicking.
        try:
            loc = locator_fn(sel).first
            element_handle = await maybe_await(loc.element_handle(timeout=5000))
            if element_handle:
                clicked = await maybe_await(page.evaluate(
                    """(el) => {
                        let label = null;
                        if (el.id) {
                            label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                        }
                        if (!label) {
                            label = el.closest('label');
                        }
                        if (label) {
                            label.click();
                            return 'label';
                        }
                        el.click();
                        return 'direct';
                    }""",
                    element_handle,
                ))
                logger.debug("  checkbox_group: clicked via %s", clicked)
                await _asyncio.sleep(0.3)
                return True
        except Exception as exc:
            logger.debug("  checkbox_group: element handle approach failed: %s", exc)

        # Fallback: Playwright check
        try:
            loc = locator_fn(sel).first
            check_fn = getattr(loc, "check", None)
            if check_fn:
                await maybe_await(check_fn(timeout=2000))
            else:
                await maybe_await(loc.click(force=True, timeout=2000))
            logger.debug("  checkbox_group: clicked via Playwright fallback")
            return True
        except Exception as exc:
            logger.debug("  checkbox_group: Playwright fallback also failed: %s", exc)
            return False

    async def _proto_workday_date(self, page: Any, answer: str, ctrl: dict, *, ctx: Any = None) -> bool:
        """Workday date picker (MM/DD/YYYY spinbuttons): parse date and type into spinbuttons.

        Extends the existing MM/YYYY spinbutton pattern from work experience filling.
        Strategy: click the Month spinbutton, type MMDDYYYY as continuous string —
        Workday auto-tabs from Month to Day to Year.
        """
        import asyncio as _asyncio
        import re as _re

        selector = str(ctrl.get("selector") or "").strip()
        locator_fn = getattr(page, "locator", None)
        keyboard = getattr(page, "keyboard", None)
        if not locator_fn or not selector or not keyboard:
            return False

        # Parse the answer into MM, DD, YYYY
        ans = answer.strip()
        mm, dd, yyyy = "", "", ""

        # Handle __TODAY__ sentinel — use today's date
        if ans.upper() == "__TODAY__":
            from datetime import datetime
            now = datetime.now()
            mm, dd, yyyy = f"{now.month:02d}", f"{now.day:02d}", str(now.year)
        else:
            # Try MM/DD/YYYY or MM-DD-YYYY
            m = _re.match(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", ans)
            if m:
                mm, dd, yyyy = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
            else:
                # Try YYYY-MM-DD (ISO format)
                m = _re.match(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})", ans)
                if m:
                    yyyy, mm, dd = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
                else:
                    # Try YYYY-MM (use 1st of month)
                    m = _re.match(r"(\d{4})[/\-](\d{1,2})", ans)
                    if m:
                        yyyy, mm, dd = m.group(1), m.group(2).zfill(2), "01"
                    else:
                        # Fallback: try to extract any date-like content
                        # "Summer 2026" -> 06/01/2026
                        season_map = {"spring": "03", "summer": "06", "fall": "09", "autumn": "09", "winter": "01"}
                        ans_lower = ans.lower()
                        for season, month in season_map.items():
                            if season in ans_lower:
                                year_m = _re.search(r"(\d{4})", ans)
                                if year_m:
                                    mm, dd, yyyy = month, "01", year_m.group(1)
                                    break
                        if not mm:
                            # "ASAP" or "immediately" -> today + 14 days
                            if any(w in ans_lower for w in ("asap", "immediately", "right away", "now")):
                                from datetime import datetime, timedelta
                                target = datetime.now() + timedelta(days=14)
                                mm, dd, yyyy = f"{target.month:02d}", f"{target.day:02d}", str(target.year)

        if not mm or not dd or not yyyy:
            logger.debug("  workday_date: could not parse '%s' into MM/DD/YYYY", ans)
            return False

        # Click the Month spinbutton and type MMDDYYYY as continuous string.
        # Use JS el.click() to bypass Playwright's viewport assertion
        # (Workday nests spinbuttons in scroll containers Playwright can't reach).
        try:
            loc = locator_fn(selector).first
            element_handle = await maybe_await(loc.element_handle(timeout=5000))
            if element_handle:
                await maybe_await(page.evaluate("(el) => el.click()", element_handle))
            else:
                await maybe_await(loc.click(force=True, timeout=3000))
            await _asyncio.sleep(0.3)
            date_str = f"{mm}{dd}{yyyy}"
            await maybe_await(keyboard.type(date_str))
            logger.debug("  workday_date: typed '%s' (from answer '%s')", date_str, ans)
            return True
        except Exception as exc:
            logger.debug("  workday_date: continuous type failed: %s — trying individual fill", exc)

        # Fallback: fill each spinbutton individually.
        # Scope selectors to the specific date field ID prefix.
        month_id = str(ctrl.get("selector") or "")
        base_prefix = ""
        if "dateSectionMonth" in month_id:
            base_prefix = month_id.split("dateSectionMonth")[0].lstrip("#")
        try:
            for part, val in [("Month", mm), ("Day", dd), ("Year", yyyy)]:
                try:
                    if base_prefix:
                        sb_sel = f"[id='{base_prefix}dateSection{part}-input']"
                    else:
                        sb_sel = f"[id*='dateSection{part}-input']"
                    sb = locator_fn(sb_sel).first
                    sb_handle = await maybe_await(sb.element_handle(timeout=3000))
                    if sb_handle:
                        await maybe_await(page.evaluate("(el) => el.click()", sb_handle))
                    else:
                        await maybe_await(sb.click(force=True, timeout=2000))
                    await _asyncio.sleep(0.2)
                    await maybe_await(keyboard.type(val))
                    await _asyncio.sleep(0.2)
                except Exception:
                    continue
            return True
        except Exception as exc:
            logger.debug("  workday_date: individual fill also failed: %s", exc)
            return False

    async def _proto_aria_combobox(self, page: Any, answer: str, ctrl: dict, *, ctx: Any = None) -> bool:
        """ARIA combobox (role='combobox'): click open, read options, fuzzy match, click."""
        import asyncio as _asyncio
        selector = str(ctrl.get("selector") or "").strip()
        label = str(ctrl.get("label") or "").strip()
        locator_fn = getattr(page, "locator", None)
        keyboard_obj = getattr(page, "keyboard", None)
        if not locator_fn or not selector:
            return False

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

        # Click to open dropdown
        try:
            await maybe_await(loc.click(timeout=2000))
        except Exception:
            return False
        await _asyncio.sleep(0.4)

        # Read options
        option_texts, option_locs = await self._read_dropdown_options(page)
        logger.debug("    aria_combobox: found %d options after click-open", len(option_texts))

        # If too many or zero, type to filter
        if len(option_texts) >= 25 or len(option_texts) == 0:
            option_texts, option_locs = await self._type_to_filter_dropdown(
                page, loc, answer, press_fn, type_fn
            )

        # Fuzzy match and click
        if option_texts and option_locs:
            best_idx, best_score = self._best_option_match(answer, option_texts)
            if best_idx < 0 and len(option_texts) == 1:
                best_idx, best_score = 0, 1
            if best_idx >= 0:
                try:
                    await maybe_await(option_locs[best_idx].click(timeout=2000))
                    logger.debug("    aria_combobox: clicked '%s' (score=%d)", option_texts[best_idx][:40], best_score)
                    return True
                except Exception:
                    pass
            else:
                # LLM fallback
                qa = getattr(ctx, "question_answerer", None) if ctx else None
                if qa is not None:
                    pick_fn = getattr(qa, "pick_option", None)
                    if pick_fn:
                        try:
                            llm_idx = pick_fn(question_label=label, intended_answer=answer, options=option_texts)
                            if llm_idx is not None and 0 <= llm_idx < len(option_locs):
                                await maybe_await(option_locs[llm_idx].click(timeout=2000))
                                return True
                        except Exception:
                            pass

        # Last resort: type + Tab
        return await self._type_and_tab_fallback(page, loc, answer, press_fn, type_fn)

    async def _proto_aria_radio(self, page: Any, answer: str, ctrl: dict, *, ctx: Any = None) -> bool:
        """ARIA radio (role='radio'): fuzzy-match answer, JS el.click() since these are divs/spans."""
        radio_options = ctrl.get("_radioOptions") or []
        evaluate_fn = getattr(page, "evaluate", None)
        if not evaluate_fn or not radio_options:
            return False

        best_opt = self._match_radio_option(answer, radio_options)
        if best_opt is None:
            return False

        sel = str(best_opt.get("selector") or "").strip()
        if not sel:
            return False

        # Use JS click — Playwright .check() doesn't work on non-input elements
        try:
            clicked = await maybe_await(evaluate_fn(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    return true;
                }""",
                sel,
            ))
            if clicked:
                import asyncio as _asyncio
                await _asyncio.sleep(0.3)
                logger.debug("    aria_radio: JS-clicked option '%s'", str(best_opt.get("label", ""))[:40])
                return True
        except Exception:
            pass

        # Fallback: try Playwright click
        locator_fn = getattr(page, "locator", None)
        if locator_fn:
            try:
                loc = locator_fn(sel).first
                await maybe_await(loc.click(timeout=2000))
                return True
            except Exception:
                pass

        return False

    async def _proto_aria_checkbox(self, page: Any, answer: str, ctrl: dict, *, ctx: Any = None) -> bool:
        """ARIA checkbox (role='checkbox'): truthy check, then JS el.click()."""
        selector = str(ctrl.get("selector") or "").strip()
        evaluate_fn = getattr(page, "evaluate", None)
        if not evaluate_fn or not selector:
            return False
        truthy = {"yes", "true", "y", "1", "on", "checked", "i agree",
                  "i acknowledge", "i acknowledge and agree", "i consent"}
        ans_norm = answer.strip().lower()
        if ans_norm not in truthy:
            return True  # intentionally not checking
        try:
            clicked = await maybe_await(evaluate_fn(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    return true;
                }""",
                selector,
            ))
            return bool(clicked)
        except Exception:
            # Fallback: Playwright click
            locator_fn = getattr(page, "locator", None)
            if locator_fn:
                try:
                    loc = locator_fn(selector).first
                    await maybe_await(loc.click(timeout=2000))
                    return True
                except Exception:
                    pass
            return False

    async def _proto_workday_button_dropdown(self, page: Any, answer: str, ctrl: dict, *, ctx: Any = None) -> bool:
        """Workday <button> dropdown ('Select One'): click button, read promptOption options, pick."""
        import asyncio as _asyncio
        selector = str(ctrl.get("selector") or "").strip()
        label = str(ctrl.get("label") or "").strip()
        locator_fn = getattr(page, "locator", None)
        if not locator_fn or not selector:
            return False

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

        # Click button to open dropdown
        try:
            await maybe_await(loc.click(timeout=2000))
        except Exception:
            return False
        await _asyncio.sleep(0.4)

        # Read options (Workday uses [data-automation-id='promptOption'])
        option_texts, option_locs = await self._read_dropdown_options(page)
        logger.debug("    workday_dropdown: found %d options after click-open", len(option_texts))

        if not option_texts or not option_locs:
            return False

        # Fuzzy match
        best_idx, best_score = self._best_option_match(answer, option_texts)
        if best_idx < 0 and len(option_texts) == 1:
            best_idx, best_score = 0, 1
        if best_idx >= 0:
            try:
                await maybe_await(option_locs[best_idx].click(timeout=2000))
                logger.debug("    workday_dropdown: clicked '%s' (score=%d)", option_texts[best_idx][:40], best_score)
                return True
            except Exception:
                pass
        else:
            # LLM fallback
            qa = getattr(ctx, "question_answerer", None) if ctx else None
            if qa is not None:
                pick_fn = getattr(qa, "pick_option", None)
                if pick_fn:
                    try:
                        llm_idx = pick_fn(question_label=label, intended_answer=answer, options=option_texts)
                        if llm_idx is not None and 0 <= llm_idx < len(option_locs):
                            await maybe_await(option_locs[llm_idx].click(timeout=2000))
                            return True
                    except Exception:
                        pass

        # Close the dropdown if nothing matched
        keyboard_obj = getattr(page, "keyboard", None)
        press_fn = getattr(keyboard_obj, "press", None) if keyboard_obj else None
        if press_fn:
            try:
                await maybe_await(press_fn("Escape"))
            except Exception:
                pass
        return False

    async def _proto_react_select(self, page: Any, answer: str, ctrl: dict, *, ctx: Any = None) -> bool:
        """React-select: click, ArrowDown to open, read .select__option, type-to-filter if needed."""
        import asyncio as _asyncio
        selector = str(ctrl.get("selector") or "").strip()
        label = str(ctrl.get("label") or "").strip()
        locator_fn = getattr(page, "locator", None)
        keyboard_obj = getattr(page, "keyboard", None)
        if not locator_fn or not selector:
            return False

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

        # Click and press ArrowDown to open (react-select pattern)
        try:
            await maybe_await(loc.click(timeout=2000))
        except Exception:
            return False
        if press_fn:
            try:
                await maybe_await(press_fn("ArrowDown"))
            except Exception:
                pass
        await _asyncio.sleep(0.4)

        # Read options
        option_texts, option_locs = await self._read_dropdown_options(page)
        logger.debug("    react_select: found %d options after click-open", len(option_texts))

        # Type to filter if too many or zero
        if len(option_texts) >= 25 or len(option_texts) == 0:
            option_texts, option_locs = await self._type_to_filter_dropdown(
                page, loc, answer, press_fn, type_fn
            )

        # Fuzzy match and click
        if option_texts and option_locs:
            best_idx, best_score = self._best_option_match(answer, option_texts)
            if best_idx < 0 and len(option_texts) == 1:
                best_idx, best_score = 0, 1
            if best_idx >= 0:
                try:
                    await maybe_await(option_locs[best_idx].click(timeout=2000))
                    logger.debug("    react_select: clicked '%s' (score=%d)", option_texts[best_idx][:40], best_score)
                    return True
                except Exception:
                    pass
            else:
                # LLM fallback
                qa = getattr(ctx, "question_answerer", None) if ctx else None
                if qa is not None:
                    pick_fn = getattr(qa, "pick_option", None)
                    if pick_fn:
                        try:
                            llm_idx = pick_fn(question_label=label, intended_answer=answer, options=option_texts)
                            if llm_idx is not None and 0 <= llm_idx < len(option_locs):
                                await maybe_await(option_locs[llm_idx].click(timeout=2000))
                                return True
                        except Exception:
                            pass

        # Last resort: type + Tab
        return await self._type_and_tab_fallback(page, loc, answer, press_fn, type_fn)

    # --- Shared helpers for protocols ---

    @staticmethod
    def _match_radio_option(answer: str, radio_options: list[dict]) -> dict | None:
        """Fuzzy-match an answer against radio option labels/values. Returns best match or None."""
        ans_norm = normalize_text(answer)
        best_opt = None
        best_score = -1

        for opt in radio_options:
            opt_value = normalize_text(opt.get("value") or "")
            if opt_value and ans_norm == opt_value:
                return opt  # exact value match

            opt_label = normalize_text(opt.get("label") or opt.get("value") or "")
            if not opt_label:
                continue
            if ans_norm == opt_label:
                return opt  # exact label match

            # Last token match (avoids matching question text containing both "yes" and "no")
            label_tokens = opt_label.split()
            last_token = label_tokens[-1] if label_tokens else ""
            if last_token and ans_norm == last_token:
                score = 5000
                if score > best_score:
                    best_opt = opt
                    best_score = score
                continue

            if ans_norm in opt_label or opt_label in ans_norm:
                score = len(ans_norm)
                if score > best_score:
                    best_opt = opt
                    best_score = score

        return best_opt

    async def _type_to_filter_dropdown(
        self, page: Any, loc: Any, answer: str, press_fn: Any, type_fn: Any
    ) -> tuple[list[str], list[Any]]:
        """Type progressively shorter prefixes to filter dropdown options. Returns (texts, locs)."""
        import asyncio as _asyncio
        # Close any open dropdown first
        if press_fn:
            try:
                await maybe_await(press_fn("Escape"))
            except Exception:
                pass
            await _asyncio.sleep(0.1)

        words = answer.replace(",", " ").split()
        filter_attempts = [answer]
        for end in range(len(words) - 1, 1, -1):
            prefix = " ".join(words[:end])
            if prefix != answer:
                filter_attempts.append(prefix)
        if len(words) >= 1 and len(words[0]) >= 3 and words[0] != answer:
            filter_attempts.append(words[0])

        for attempt in filter_attempts:
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
            logger.debug("    filter '%s': found %d options", attempt[:30], len(option_texts))
            if option_texts:
                return (option_texts, option_locs)

        return ([], [])

    async def _type_and_tab_fallback(
        self, page: Any, loc: Any, answer: str, press_fn: Any, type_fn: Any
    ) -> bool:
        """Last-resort: close dropdown, type the answer, press Tab."""
        import asyncio as _asyncio
        logger.debug("    fallback: type+Tab for '%s'", answer[:30])
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

        selectors = [
            "[role='listbox']:visible [role='option']",
            ".select__menu:visible [role='option']",
            ".select__menu:visible .select__option",
            "[data-automation-id='promptOption']",
            ".autocomplete-results:visible li",
            ".pac-container:visible .pac-item",
            "ul[role='listbox']:visible li",
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

    async def _page_signature(self, page: Any) -> str:
        """Return a lightweight fingerprint of the current page's visible form controls.
        Used to detect when clicking Next didn't actually advance the page."""
        evaluate_fn = getattr(page, "evaluate", None)
        if evaluate_fn is None:
            return ""
        try:
            sig = await maybe_await(evaluate_fn("""
                () => {
                    const els = document.querySelectorAll(
                        'input:not([type="hidden"]), select, textarea, ' +
                        '[role="combobox"], [role="radio"], [role="checkbox"], ' +
                        '[data-automation-id]'
                    );
                    const ids = [];
                    for (const el of els) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width < 2 || rect.height < 2) continue;
                        ids.push(el.id || el.name || el.getAttribute('data-automation-id') || el.tagName);
                    }
                    // Sort for stability — DOM order can vary
                    ids.sort();
                    return ids.join('|');
                }
            """))
            return str(sig or "")
        except Exception:
            return ""

    async def _wait_for_page_ready(self, page: Any, *, timeout_sec: float = 3.0) -> bool:
        """
        Base-class page-ready gate. Waits for:
          1. At least one form control is visible
          2. No network activity / loading state

        ATS subclasses (e.g. Workday) override this with more specific checks.
        Returns True if the page appears ready, False on timeout.
        """
        import asyncio as _asyncio
        import time

        evaluate_fn = getattr(page, "evaluate", None)
        if evaluate_fn is None:
            await _asyncio.sleep(1.5)
            return True

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            await _asyncio.sleep(0.4)
            try:
                count = await maybe_await(evaluate_fn("""
                    () => {
                        const els = document.querySelectorAll(
                            'input:not([type="hidden"]), select, textarea'
                        );
                        let n = 0;
                        for (const el of els) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width >= 2 && rect.height >= 2) n++;
                        }
                        return n;
                    }
                """))
                if int(count or 0) > 0:
                    return True
            except Exception:
                pass

        return False

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
