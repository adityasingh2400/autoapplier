from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openclaw.ats import handler_for_url
from openclaw.ats.base import ApplyContext
from openclaw.profile import load_user_profile
from openclaw.questions import QuestionAnswerer
from openclaw.sources import fetch_simplify_roles
from openclaw.utils import (
    capture_step,
    create_run_dir,
    is_top_tier_company,
    maybe_await,
    setup_logging,
    utc_now_iso,
    write_json,
)

from openclaw.answer_bank import expand_placeholders, is_human_sentinel, match_question_bank

from openclaw.harvest import (
    HarvestedField,
    harvest_job_posting_fields,
    suggest_bank_entry,
    update_profile_answer_bank,
    write_harvest_report,
)

from openclaw.scoring import (
    JobLedger,
    JobScorer,
    scrape_job_descriptions_batch,
)


logger = logging.getLogger(__name__)


REQUIRED_MEMORY_FILES = ("profile.json", "resume.json")


def _has_required_memory_files(path: Path) -> bool:
    return all((path / name).exists() for name in REQUIRED_MEMORY_FILES)


def _missing_required_memory_files(path: Path) -> list[str]:
    return [name for name in REQUIRED_MEMORY_FILES if not (path / name).exists()]


def _suggest_memory_roots() -> list[str]:
    candidates: list[Path] = []
    repo_root = Path(__file__).resolve().parents[1]

    for p in (
        Path("/home/ubuntu/clawd/memory"),
        repo_root / "real_memory",
        repo_root / "memory",
        repo_root / "test_memory",
        Path.cwd() / "real_memory",
        Path.cwd() / "memory",
        Path.cwd() / "test_memory",
    ):
        if p not in candidates:
            candidates.append(p)

    out: list[str] = []
    for p in candidates:
        try:
            if _has_required_memory_files(p):
                out.append(str(p))
        except Exception:
            continue
    return out


def _infer_default_memory_root() -> str:
    """
    Choose a default memory root that works in both environments:
    - EC2: /home/ubuntu/clawd/memory
    - Local repo: ./real_memory (or ./memory) when present
    """
    env = os.getenv("OPENCLAW_MEMORY_ROOT")
    if env:
        return env

    ec2_default = Path("/home/ubuntu/clawd/memory")
    if _has_required_memory_files(ec2_default):
        return str(ec2_default)

    repo_root = Path(__file__).resolve().parents[1]
    for candidate in (repo_root / "real_memory", repo_root / "memory", repo_root / "test_memory"):
        if _has_required_memory_files(candidate):
            return str(candidate)

    # Last-resort: keep the EC2 path (it will fall back in _resolve_memory_root if unusable).
    return str(ec2_default)


@dataclass(slots=True)
class BrowserSession:
    page: Any
    engine: str
    _closer: Any | None = None
    cdp_url: str | None = None

    async def close(self) -> None:
        if self._closer is None:
            return
        try:
            await maybe_await(self._closer())
        except Exception:
            logger.debug("Failed to close browser session cleanly.")


async def launch_browser_session(headless: bool = True) -> BrowserSession:
    return await launch_browser_session_with_engine(headless=headless)


def _pick_free_local_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        try:
            sock.close()
        except Exception:
            pass


async def launch_browser_session_with_engine(headless: bool = True) -> BrowserSession:
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as playwright_import_error:
        raise RuntimeError(
            f"Unable to launch automation browser. Playwright import error: {playwright_import_error}. "
            "Install with: pip install playwright && playwright install chromium"
        ) from playwright_import_error

    playwright = await async_playwright().start()
    cdp_url: str | None = None
    launch_kwargs: dict[str, Any] = {"headless": headless}
    # Expose a CDP endpoint so a secondary agent can attach to the same running session if needed.
    # Bind to localhost only.
    try:
        port = _pick_free_local_port()
        launch_kwargs["args"] = [
            f"--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
        ]
        cdp_url = f"http://127.0.0.1:{port}"
    except Exception:
        cdp_url = None

    try:
        browser = await playwright.chromium.launch(**launch_kwargs)
    except Exception:
        # Fallback: launch without CDP args.
        cdp_url = None
        browser = await playwright.chromium.launch(headless=headless)
    context = await browser.new_context()
    page = await context.new_page()
    _configure_page_timeouts(page)

    async def _close_playwright() -> None:
        await context.close()
        await browser.close()
        await playwright.stop()

    return BrowserSession(page=page, engine="playwright", cdp_url=cdp_url, _closer=_close_playwright)


def _configure_page_timeouts(page: Any) -> None:
    """
    Keep navigation resilient on slower sites without slowing down normal runs.
    60s is a pragmatic default for heavy enterprise ATS (ex: Workday variants).
    """
    for fn_name in ("set_default_navigation_timeout", "set_default_timeout"):
        fn = getattr(page, fn_name, None)
        if fn is None:
            continue
        try:
            fn(60_000)
        except Exception:
            continue


async def run_single_application(
    *,
    job_url: str,
    company: str,
    role: str,
    memory_root: Path,
    profile: Any,
    question_answerer: QuestionAnswerer,
    dry_run: bool,
    force_submit: bool,
    timeout_sec: int,
    human_in_loop: bool = False,
    max_form_pages: int = 3,
    max_custom_questions: int = 12,
    pause_on_captcha: bool = True,
    pause_on_auth: bool = True,
    pause_on_missing_fields: bool = True,
    allow_captcha_auto_solve: bool = True,
    source: str | None = None,
    quality: bool = False,
    tailor_resume: bool = False,
    upload_tailored_resume: bool = False,
    keep_open: bool = False,
    session: BrowserSession | None = None,
    headless: bool = True,
) -> dict[str, Any]:
    output_dir = create_run_dir(memory_root, company)
    screenshots: list[str] = []
    handler = handler_for_url(job_url)

    owns_session = session is None
    timed_out = False
    handoff_active = False
    handoff_payload: dict[str, Any] | None = None
    try:
        if session is None:
            session = await launch_browser_session_with_engine(headless=headless)
        logger.info(
            "Applying via %s handler (Playwright): %s | %s",
            handler.ats_name,
            company,
            role,
        )

        context = ApplyContext(
            job_url=job_url,
            company=company,
            role=role,
            profile=profile,
            question_answerer=question_answerer,
            output_dir=output_dir,
            screenshots=screenshots,
            dry_run=dry_run,
            force_submit=force_submit,
            is_top_tier=is_top_tier_company(company),
            source=source,
            quality=quality,
            tailor_resume=tailor_resume,
            upload_tailored_resume=upload_tailored_resume,
            human_in_loop=human_in_loop,
            max_form_pages=max_form_pages,
            max_custom_questions=max_custom_questions,
            pause_on_captcha=pause_on_captcha,
            pause_on_auth=pause_on_auth,
            pause_on_missing_fields=pause_on_missing_fields,
            allow_captcha_auto_solve=allow_captcha_auto_solve,
        )
        # In interactive human-in-loop runs, do not kill the session while waiting for manual auth/2FA/email steps.
        if human_in_loop:
            result = await handler.apply(session.page, context)
        else:
            result = await asyncio.wait_for(handler.apply(session.page, context), timeout=timeout_sec)
    except asyncio.TimeoutError:
        timed_out = True
        logger.error("Application timed out after %s seconds.", timeout_sec)
        if session is not None:
            await capture_step(session.page, output_dir, "99-timeout-state", screenshots)
        result = {
            "status": "error",
            "company": company,
            "role": role,
            "url": job_url,
            "error": f"Application timed out after {timeout_sec} seconds",
            "screenshot": screenshots[-1] if screenshots else None,
            "timestamp": utc_now_iso(),
        }
    except Exception as exc:
        logger.exception("Application run failed.")
        if session is not None:
            await capture_step(session.page, output_dir, "99-error-state", screenshots)
        result = {
            "status": "error",
            "company": company,
            "role": role,
            "url": job_url,
            "error": str(exc),
            "trace": traceback.format_exc(limit=4),
            "screenshot": screenshots[-1] if screenshots else None,
            "timestamp": utc_now_iso(),
        }
    finally:
        # NOTE: We decide whether to keep the browser open AFTER result is built (see below).
        # This finally only ensures we close if needed.
        pass

    result.setdefault("company", company)
    result.setdefault("role", role)
    result.setdefault("url", job_url)
    result.setdefault("timestamp", utc_now_iso())
    result.setdefault("screenshots", screenshots)
    result["ats"] = handler.ats_name
    result["output_dir"] = str(output_dir)
    result["job_url"] = job_url  # original input URL
    result["final_url"] = str(result.get("url") or job_url)
    # Compatibility for orchestrators that expect `pr_url` (treat as a "result URL").
    result.setdefault("pr_url", result["final_url"])
    result["result_path"] = str(output_dir / "result.json")
    result["screenshot_paths"] = [str(output_dir / name) for name in screenshots]

    # If we didn't fully finish, emit a compact "handoff packet" so a slower agent (ex: EC2)
    # can take over only for the remaining items.
    try:
        status = str(result.get("status") or "")
        if status and status != "success":
            template_values: dict[str, str] = {}
            try:
                template_values.update(dict(getattr(profile, "standard_fields", {}) or {}))
            except Exception:
                pass
            template_values.setdefault("company", company)
            template_values.setdefault("role", role)
            template_values.setdefault("how_heard", "Online Job Board")

            expanded_bank: list[tuple[str, str]] = []
            try:
                for needle, ans in list(getattr(profile, "question_bank", []) or []):
                    expanded_bank.append((str(needle), expand_placeholders(str(ans), template_values)))
            except Exception:
                pass

            missing = result.get("missing_required_fields") if isinstance(result.get("missing_required_fields"), list) else []
            suggested: list[dict[str, str]] = []
            for item in missing or []:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "").strip()
                if not label:
                    continue
                ans = match_question_bank(label, expanded_bank)
                if not ans:
                    continue
                if is_human_sentinel(ans):
                    continue
                suggested.append({"label": label[:240], "answer": ans})

            handoff = {
                "status": status,
                "company": company,
                "role": role,
                "ats": handler.ats_name,
                "job_url": job_url,
                "final_url": result.get("final_url") or result.get("url") or job_url,
                "reason": result.get("reason") or result.get("error") or "",
                "output_dir": str(output_dir),
                "screenshot_paths": result.get("screenshot_paths") or [],
                "missing_required_fields": missing or [],
                "suggested_answers": suggested,
                "timestamp": result.get("timestamp") or utc_now_iso(),
            }
            result["handoff"] = handoff
            write_json(output_dir / "handoff.json", handoff)
            handoff_payload = handoff
            # Auto-handoff: when interactive, keep the browser open so another agent can attach.
            # This triggers on needs_review/captcha/error alike; the downstream agent can decide what to do.
            # Auto-handoff only in interactive direct-url runs (avoid stalling batch/source runs).
            if sys.stdin.isatty() and not source:
                handoff_active = True
    except Exception:
        pass

    # If handoff is active, print a machine-detectable message to stdout and pause with browser open.
    # This keeps form-filling fast (Playwright did the first pass), and only slows down on leftovers.
    if session is not None and owns_session:
        leave_open = bool(
            keep_open
            or handoff_active
            or (human_in_loop and dry_run)
            or (timed_out and human_in_loop and sys.stdin.isatty())
        )

        if handoff_active and handoff_payload is not None:
            msg = {
                "handoff": "clawdbot",
                "session_id": (session.cdp_url or ""),
                "handoff_data": handoff_payload,
            }
            print(json.dumps(msg, indent=None, separators=(",", ":")), flush=True)

            # Keep the process alive so the browser session remains open/attachable.
            try:
                from openclaw.utils import human_pause

                print(
                    "\nHandoff active: browser kept open for secondary agent.\n"
                    f"- CDP: {session.cdp_url or 'n/a'}\n"
                    f"- Output dir: {output_dir}\n"
                    "Press Enter once the handoff agent is done to close the browser and finish.\n",
                    file=sys.stderr,
                )
                await human_pause("Handoff done> ")
            except Exception:
                # If we can't pause, just keep the session alive until interrupted.
                try:
                    await asyncio.Event().wait()
                except Exception:
                    pass

        if not leave_open:
            await session.close()
        else:
            # keep-open (manual review) behavior for non-handoff cases
            if not handoff_active:
                try:
                    from openclaw.utils import human_pause

                    print(
                        "\nBrowser left open for manual review.\n"
                        f"- Output dir: {output_dir}\n"
                        "Press Enter (or Ctrl+C) to close the browser.\n"
                        ,
                        file=sys.stderr,
                    )
                    if sys.stdin.isatty():
                        await human_pause("Close browser> ")
                    else:
                        # stdin is not a TTY (e.g. run from an IDE agent).
                        # Keep the browser open and block until interrupted
                        # so the user can inspect / interact with it.
                        logger.info(
                            "--keep-open: stdin is not a TTY. "
                            "Browser will stay open until this process is killed (Ctrl+C)."
                        )
                        try:
                            await asyncio.Event().wait()  # block forever
                        except (KeyboardInterrupt, asyncio.CancelledError):
                            pass
                finally:
                    await session.close()
            else:
                # handoff already paused above; close after pause
                await session.close()

    write_json(output_dir / "result.json", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OpenClaw auto-applier module",
    )
    parser.add_argument("job_url", nargs="?", help="Direct job URL to apply for")
    parser.add_argument("--company", help="Company name for direct URL mode")
    parser.add_argument("--role", help="Role title for direct URL mode")
    parser.add_argument("--dry-run", action="store_true", help="Fill forms but do not submit")
    parser.add_argument("--force", action="store_true", help="Force submit for top-tier companies")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logs")
    parser.add_argument(
        "--memory-root",
        default=_infer_default_memory_root(),
        help="Path containing profile.json/resume.json/resume.pdf",
    )
    parser.add_argument(
        "--source",
        choices=["simplify"],
        help="Fetch jobs from an integrated source instead of a single URL",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=10,
        help="Maximum jobs to pull/apply when using --source",
    )
    parser.add_argument(
        "--category",
        default="software engineering",
        help="Category filter for source mode, or 'all'",
    )
    parser.add_argument("--company-keyword", help="Company keyword filter for source mode")
    parser.add_argument("--role-keyword", help="Role keyword filter for source mode")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        help="Only include source roles newer than this age in hours",
    )
    parser.add_argument(
        "--exclude-unknown-ats",
        action="store_true",
        help="In source mode, keep only postings with known ATS handlers",
    )
    parser.add_argument(
        "--list-source-jobs",
        action="store_true",
        help="Fetch source jobs and print them without applying",
    )
    parser.add_argument(
        "--harvest-answer-bank",
        action="store_true",
        help="Harvest form fields across postings and append suggested questionBank entries into profile.json",
    )
    parser.add_argument(
        "--harvest-report",
        default="answer_bank_harvest.json",
        help="Path (relative to memory root) to write harvest details + suggestions",
    )
    parser.add_argument(
        "--harvest-limit-per-page",
        type=int,
        default=220,
        help="Max number of fields to collect per form page while harvesting",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=180,
        help="Max seconds allowed per application run before failing",
    )
    parser.add_argument(
        "--human-in-loop",
        action="store_true",
        help="Pause for manual steps (CAPTCHA/auth/missing fields). Implies --headful unless --headless is set.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Run browser with a visible UI (recommended for debugging/HITL).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Force headless mode even if --human-in-loop or --headful is set.",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Keep browser open at the end for manual review, then press Enter to close it.",
    )
    parser.add_argument(
        "--max-form-pages",
        type=int,
        default=3,
        help="Maximum number of Next/Continue pages to traverse in an application form.",
    )
    parser.add_argument(
        "--max-custom-questions",
        type=int,
        default=12,
        help="Maximum custom questions to answer per application page.",
    )
    parser.add_argument(
        "--no-pause-on-captcha",
        action="store_true",
        help="In human-in-loop mode, do not pause on CAPTCHA detection.",
    )
    parser.add_argument(
        "--no-pause-on-auth",
        action="store_true",
        help="In human-in-loop mode, do not pause on sign-in/account creation gates.",
    )
    parser.add_argument(
        "--no-pause-on-missing-fields",
        action="store_true",
        help="In human-in-loop mode, do not pause when required fields are missing.",
    )
    parser.add_argument(
        "--no-captcha-auto-solve",
        action="store_true",
        help="Disable optional 2captcha auto-solve even if TWOCAPTCHA_API_KEY is set.",
    )
    parser.add_argument(
        "--reuse-session",
        action="store_true",
        help="In source mode, reuse one browser session across all applications (more reliable for logins).",
    )
    parser.add_argument(
        "--quality",
        action="store_true",
        help="High-quality mode: generate cover letter text and use richer context for answers (slower).",
    )
    parser.add_argument(
        "--tailor-resume",
        action="store_true",
        help="In quality mode, generate a tailored resume artifact (may take longer).",
    )
    parser.add_argument(
        "--upload-tailored-resume",
        action="store_true",
        help="If a tailored resume PDF can be generated, upload it instead of memory/resume.pdf.",
    )
    # --- Job Scoring CLI flags ---
    parser.add_argument(
        "--score-jobs",
        action="store_true",
        help="Score and rank new jobs from source. Scrapes JDs and uses LLM to score fit.",
    )
    parser.add_argument(
        "--list-scored-jobs",
        action="store_true",
        help="List previously scored jobs from the ledger, sorted by score.",
    )
    parser.add_argument(
        "--apply-top-scored",
        action="store_true",
        help="Apply to top-scored unapplied jobs from the ledger.",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="Minimum score threshold for --list-scored-jobs or --apply-top-scored.",
    )
    parser.add_argument(
        "--ledger-stats",
        action="store_true",
        help="Print job ledger statistics and exit.",
    )
    parser.add_argument(
        "--score-unscored",
        action="store_true",
        help="Score all jobs already in the ledger that haven't been scored yet.",
    )
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.headful and args.headless:
        parser.error("--headful and --headless are mutually exclusive.")
    if args.keep_open and args.headless:
        parser.error("--keep-open requires a visible browser; do not combine with --headless.")
    if args.upload_tailored_resume and not args.quality:
        parser.error("--upload-tailored-resume requires --quality.")
    if args.upload_tailored_resume and not args.tailor_resume:
        parser.error("--upload-tailored-resume requires --tailor-resume.")
    
    # Scoring mode validations
    if args.ledger_stats:
        return  # No other args needed
    if args.list_scored_jobs:
        return  # No other args needed
    if getattr(args, 'score_unscored', False):
        return  # No other args needed
    if args.score_jobs and not args.source:
        parser.error("--score-jobs requires --source simplify")
    if args.apply_top_scored and not args.source:
        parser.error("--apply-top-scored requires --source simplify")
    
    if args.source:
        if args.job_url:
            parser.error("Do not pass a positional job_url when --source is used.")
        if args.max_jobs <= 0:
            parser.error("--max-jobs must be greater than 0.")
        if args.timeout_sec <= 0:
            parser.error("--timeout-sec must be greater than 0.")
        if args.max_form_pages <= 0:
            parser.error("--max-form-pages must be greater than 0.")
        if args.max_custom_questions <= 0:
            parser.error("--max-custom-questions must be greater than 0.")
        return

    if not args.job_url:
        parser.error("job_url is required unless --source is used.")
    if not args.harvest_answer_bank:
        if not args.company:
            parser.error("--company is required in direct URL mode.")
        if not args.role:
            parser.error("--role is required in direct URL mode.")
    if args.timeout_sec <= 0:
        parser.error("--timeout-sec must be greater than 0.")
    if args.max_form_pages <= 0:
        parser.error("--max-form-pages must be greater than 0.")
    if args.max_custom_questions <= 0:
        parser.error("--max-custom-questions must be greater than 0.")


def _dedupe_harvested_fields(fields: list[HarvestedField]) -> list[HarvestedField]:
    merged: dict[str, HarvestedField] = {}
    for field in fields:
        key = (field.label or "").strip().lower()
        key = " ".join(key.split())
        if not key:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = field
            continue
        existing.required = bool(existing.required or field.required)
        for opt in field.options:
            if opt and opt not in existing.options:
                existing.options.append(opt)
    return list(merged.values())


async def run_source_harvest(args: argparse.Namespace, memory_root: Path) -> dict[str, Any]:
    try:
        roles = fetch_simplify_roles(
            category=args.category,
            company_keyword=args.company_keyword,
            role_keyword=args.role_keyword,
            max_age_hours=args.max_age_hours,
            limit=args.max_jobs,
            include_unknown_ats=not args.exclude_unknown_ats,
        )
    except Exception as exc:
        return {
            "status": "error",
            "source": "simplify",
            "error": f"Failed to fetch Simplify source: {exc}",
            "timestamp": utc_now_iso(),
        }

    if args.list_source_jobs:
        return {
            "status": "source_jobs",
            "source": "simplify",
            "selected": len(roles),
            "jobs": [item.to_dict() for item in roles],
            "timestamp": utc_now_iso(),
        }

    profile = load_user_profile(memory_root)

    headless = True
    if args.headful or args.keep_open or (args.human_in_loop and not args.headless):
        headless = False

    session: BrowserSession | None = None
    session = await launch_browser_session_with_engine(headless=headless)

    harvested: list[HarvestedField] = []
    jobs_sampled: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    try:
        for idx, role in enumerate(roles, start=1):
            jobs_sampled.append({"company": role.company, "role": role.role, "url": role.apply_url})
            handler = handler_for_url(role.apply_url)
            try:
                logger.info("Harvesting (%s/%s): %s | %s", idx, len(roles), role.company, role.apply_url)
                harvested.extend(
                    await asyncio.wait_for(
                        harvest_job_posting_fields(
                            session.page,
                            handler,
                            job_url=role.apply_url,
                            max_form_pages=args.max_form_pages,
                            limit_per_page=args.harvest_limit_per_page,
                        ),
                        timeout=args.timeout_sec,
                    )
                )
            except asyncio.TimeoutError:
                errors.append({"url": role.apply_url, "error": f"Harvest timed out after {args.timeout_sec}s"})
            except Exception as exc:
                errors.append({"url": role.apply_url, "error": str(exc)})
    finally:
        if session is not None:
            await session.close()

    harvested = _dedupe_harvested_fields(harvested)
    suggested = [suggest_bank_entry(f) for f in harvested]

    profile_path = memory_root / "profile.json"
    if profile_path.exists():
        try:
            update_profile_answer_bank(profile_path, suggested)
        except Exception as exc:
            errors.append({"url": str(profile_path), "error": f"Failed to update profile.json: {exc}"})

    report_path = memory_root / str(args.harvest_report)
    try:
        write_harvest_report(
            report_path,
            harvested=harvested,
            suggested_items=suggested,
            jobs_sampled=jobs_sampled,
        )
    except Exception as exc:
        errors.append({"url": str(report_path), "error": f"Failed to write report: {exc}"})

    return {
        "status": "harvest_complete",
        "source": "simplify",
        "selected": len(roles),
        "harvested_unique_fields": len(harvested),
        "suggested_items": len(suggested),
        "updated_profile_path": str(profile_path),
        "report_path": str(report_path),
        "errors": errors,
        "timestamp": utc_now_iso(),
    }


async def run_direct_harvest(args: argparse.Namespace, memory_root: Path) -> dict[str, Any]:
    headless = True
    if args.headful or args.keep_open or (args.human_in_loop and not args.headless):
        headless = False

    company = args.company or "Unknown"
    role = args.role or "Unknown"
    job_url = str(args.job_url)

    session = await launch_browser_session_with_engine(headless=headless)
    harvested: list[HarvestedField] = []
    errors: list[dict[str, str]] = []
    try:
        handler = handler_for_url(job_url)
        harvested = await asyncio.wait_for(
            harvest_job_posting_fields(
                session.page,
                handler,
                job_url=job_url,
                max_form_pages=args.max_form_pages,
                limit_per_page=args.harvest_limit_per_page,
            ),
            timeout=args.timeout_sec,
        )
    except asyncio.TimeoutError:
        errors.append({"url": job_url, "error": f"Harvest timed out after {args.timeout_sec}s"})
    except Exception as exc:
        errors.append({"url": job_url, "error": str(exc)})
    finally:
        await session.close()

    harvested = _dedupe_harvested_fields(harvested)
    suggested = [suggest_bank_entry(f) for f in harvested]

    profile_path = memory_root / "profile.json"
    if profile_path.exists():
        try:
            update_profile_answer_bank(profile_path, suggested)
        except Exception as exc:
            errors.append({"url": str(profile_path), "error": f"Failed to update profile.json: {exc}"})

    report_path = memory_root / str(args.harvest_report)
    try:
        write_harvest_report(
            report_path,
            harvested=harvested,
            suggested_items=suggested,
            jobs_sampled=[{"company": company, "role": role, "url": job_url}],
        )
    except Exception as exc:
        errors.append({"url": str(report_path), "error": f"Failed to write report: {exc}"})

    return {
        "status": "harvest_complete",
        "company": company,
        "role": role,
        "url": job_url,
        "harvested_unique_fields": len(harvested),
        "suggested_items": len(suggested),
        "updated_profile_path": str(profile_path),
        "report_path": str(report_path),
        "errors": errors,
        "timestamp": utc_now_iso(),
    }


async def run_source_mode(args: argparse.Namespace, memory_root: Path) -> dict[str, Any]:
    try:
        roles = fetch_simplify_roles(
            category=args.category,
            company_keyword=args.company_keyword,
            role_keyword=args.role_keyword,
            max_age_hours=args.max_age_hours,
            limit=args.max_jobs,
            include_unknown_ats=not args.exclude_unknown_ats,
        )
    except Exception as exc:
        return {
            "status": "error",
            "source": "simplify",
            "error": f"Failed to fetch Simplify source: {exc}",
            "timestamp": utc_now_iso(),
        }

    if args.list_source_jobs:
        return {
            "status": "source_jobs",
            "source": "simplify",
            "selected": len(roles),
            "jobs": [item.to_dict() for item in roles],
            "timestamp": utc_now_iso(),
        }

    if not roles:
        return {
            "status": "source_empty",
            "source": "simplify",
            "message": "No postings matched filters",
            "timestamp": utc_now_iso(),
        }

    profile = load_user_profile(memory_root)
    question_answerer = QuestionAnswerer()

    results: list[dict[str, Any]] = []
    headless = True
    if args.headful or args.keep_open or (args.human_in_loop and not args.headless):
        headless = False

    session: BrowserSession | None = None
    if args.reuse_session:
        session = await launch_browser_session_with_engine(headless=headless)

    try:
        for role in roles:
            result = await run_single_application(
                job_url=role.apply_url,
                company=role.company,
                role=role.role,
                memory_root=memory_root,
                profile=profile,
                question_answerer=question_answerer,
                dry_run=args.dry_run,
                force_submit=args.force,
                timeout_sec=args.timeout_sec,
                human_in_loop=args.human_in_loop,
                max_form_pages=args.max_form_pages,
                max_custom_questions=args.max_custom_questions,
                pause_on_captcha=not args.no_pause_on_captcha,
                pause_on_auth=not args.no_pause_on_auth,
                pause_on_missing_fields=not args.no_pause_on_missing_fields,
                allow_captcha_auto_solve=not args.no_captcha_auto_solve,
                source="simplify",
                quality=args.quality,
                tailor_resume=args.tailor_resume,
                upload_tailored_resume=args.upload_tailored_resume,
                keep_open=args.keep_open,
                session=session,
                headless=headless,
            )
            result["source"] = {
                "name": "simplify",
                "category": role.category,
                "age": role.age,
                "location": role.location,
            }
            results.append(result)
    finally:
        if session is not None:
            await session.close()

    status_counts: dict[str, int] = {}
    for item in results:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "status": "batch_complete",
        "source": "simplify",
        "selected": len(roles),
        "results": results,
        "status_counts": status_counts,
        "timestamp": utc_now_iso(),
    }


async def run_direct_mode(args: argparse.Namespace, memory_root: Path) -> dict[str, Any]:
    profile = load_user_profile(memory_root)
    question_answerer = QuestionAnswerer()

    headless = True
    if args.headful or args.keep_open or (args.human_in_loop and not args.headless):
        headless = False

    return await run_single_application(
        job_url=args.job_url,
        company=args.company,
        role=args.role,
        memory_root=memory_root,
        profile=profile,
        question_answerer=question_answerer,
        dry_run=args.dry_run,
        force_submit=args.force,
        timeout_sec=args.timeout_sec,
        human_in_loop=args.human_in_loop,
        max_form_pages=args.max_form_pages,
        max_custom_questions=args.max_custom_questions,
        pause_on_captcha=not args.no_pause_on_captcha,
        pause_on_auth=not args.no_pause_on_auth,
        pause_on_missing_fields=not args.no_pause_on_missing_fields,
        allow_captcha_auto_solve=not args.no_captcha_auto_solve,
        quality=args.quality,
        tailor_resume=args.tailor_resume,
        upload_tailored_resume=args.upload_tailored_resume,
        keep_open=args.keep_open,
        headless=headless,
    )


# =============================================================================
# Job Scoring Mode Functions
# =============================================================================


async def run_score_jobs(args: argparse.Namespace, memory_root: Path) -> dict[str, Any]:
    """
    Score new jobs from Simplify source:
    1. Fetch jobs from source
    2. Filter to only new jobs (not in ledger)
    3. Scrape job descriptions in parallel
    4. Score each job using LLM
    5. Save to ledger and return ranked results
    """
    ledger = JobLedger()
    
    # Fetch jobs from source
    try:
        max_age = args.max_age_hours if args.max_age_hours else 72.0
        roles = fetch_simplify_roles(
            category=args.category,
            company_keyword=args.company_keyword,
            role_keyword=args.role_keyword,
            max_age_hours=max_age,
            limit=None,  # Get all, we'll filter by ledger
            include_unknown_ats=not args.exclude_unknown_ats,
        )
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Failed to fetch Simplify source: {exc}",
            "timestamp": utc_now_iso(),
        }

    # Filter to new jobs only
    all_urls = [r.apply_url for r in roles]
    new_urls = set(ledger.get_new_jobs(all_urls))
    new_roles = [r for r in roles if r.apply_url in new_urls]

    if not new_roles:
        stats = ledger.stats()
        return {
            "status": "no_new_jobs",
            "message": "No new jobs found since last run",
            "total_fetched": len(roles),
            "ledger_stats": stats,
            "timestamp": utc_now_iso(),
        }

    # Limit to max_jobs for this run
    if args.max_jobs and len(new_roles) > args.max_jobs:
        new_roles = new_roles[:args.max_jobs]

    logger.info("Found %d new jobs to score (out of %d total)", len(new_roles), len(roles))

    # Add new jobs to ledger first
    for role in new_roles:
        ledger.add_job(
            url=role.apply_url,
            company=role.company,
            role=role.role,
            location=role.location,
            category=role.category,
            age_hours=role.age_hours,
        )
    ledger.save()

    # Scrape job descriptions in parallel
    urls_to_scrape = [r.apply_url for r in new_roles]
    logger.info("Scraping %d job descriptions...", len(urls_to_scrape))
    jd_map = await scrape_job_descriptions_batch(urls_to_scrape, concurrency=5)

    # Update ledger with JDs
    for url, jd in jd_map.items():
        if jd:
            ledger.update_jd(url, jd)
    ledger.save()

    # Load profile for scoring
    profile = load_user_profile(memory_root)
    scorer = JobScorer()

    # Score each job
    scored_results: list[dict[str, Any]] = []
    for role in new_roles:
        jd = jd_map.get(role.apply_url, "")
        entry = ledger.get(role.apply_url)
        
        scored = scorer.score_job(
            company=role.company,
            role=role.role,
            location=role.location,
            job_description=jd,
            profile_summary=profile.summary,
            resume_text=profile.resume_text,
            age_hours=role.age_hours,
        )
        scored.url = role.apply_url

        # Update ledger with score
        ledger.update_score(
            role.apply_url,
            score=scored.score,
            breakdown=scored.breakdown,
            reasoning=scored.reasoning,
            recommendation=scored.recommendation,
        )

        scored_results.append({
            "company": role.company,
            "role": role.role,
            "location": role.location,
            "url": role.apply_url,
            "age": role.age,
            "score": scored.score,
            "breakdown": scored.breakdown,
            "reasoning": scored.reasoning,
            "recommendation": scored.recommendation,
            "jd_length": len(jd),
        })

        logger.info(
            "Scored: %.0f %s - %s @ %s",
            scored.score,
            scored.recommendation,
            role.role[:40],
            role.company[:25],
        )

    ledger.save()

    # Sort by score descending
    scored_results.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Summary stats
    high_priority = sum(1 for r in scored_results if r.get("recommendation") == "high_priority")
    medium = sum(1 for r in scored_results if r.get("recommendation") == "medium")
    low = sum(1 for r in scored_results if r.get("recommendation") == "low")
    skip = sum(1 for r in scored_results if r.get("recommendation") == "skip")

    return {
        "status": "scoring_complete",
        "new_jobs_scored": len(scored_results),
        "total_in_source": len(roles),
        "summary": {
            "high_priority": high_priority,
            "medium": medium,
            "low": low,
            "skip": skip,
        },
        "jobs": scored_results,
        "ledger_stats": ledger.stats(),
        "timestamp": utc_now_iso(),
    }


async def run_list_scored_jobs(args: argparse.Namespace) -> dict[str, Any]:
    """List scored jobs from the ledger."""
    ledger = JobLedger()
    
    jobs = ledger.get_scored_jobs(
        min_score=args.min_score,
        unapplied_only=False,
    )
    
    # Sort by score descending
    jobs.sort(key=lambda j: j.score or 0, reverse=True)

    results = []
    for job in jobs:
        results.append({
            "company": job.company,
            "role": job.role,
            "location": job.location,
            "url": job.url,
            "score": job.score,
            "recommendation": job.recommendation,
            "reasoning": job.score_reasoning[:200] if job.score_reasoning else "",
            "applied": job.applied,
            "applied_at": job.applied_at,
            "first_seen": job.first_seen,
        })

    return {
        "status": "scored_jobs",
        "count": len(results),
        "min_score_filter": args.min_score,
        "jobs": results,
        "ledger_stats": ledger.stats(),
        "timestamp": utc_now_iso(),
    }


async def run_apply_top_scored(args: argparse.Namespace, memory_root: Path) -> dict[str, Any]:
    """Apply to top-scored unapplied jobs."""
    ledger = JobLedger()
    
    min_score = args.min_score if args.min_score is not None else 70.0
    top_jobs = ledger.get_top_jobs(
        limit=args.max_jobs,
        min_score=min_score,
        unapplied_only=True,
    )

    if not top_jobs:
        return {
            "status": "no_jobs_to_apply",
            "message": f"No unapplied jobs with score >= {min_score}",
            "ledger_stats": ledger.stats(),
            "timestamp": utc_now_iso(),
        }

    logger.info("Applying to %d top-scored jobs (min_score=%.0f)", len(top_jobs), min_score)

    profile = load_user_profile(memory_root)
    question_answerer = QuestionAnswerer()

    headless = True
    if args.headful or args.keep_open or (args.human_in_loop and not args.headless):
        headless = False

    session: BrowserSession | None = None
    if args.reuse_session:
        session = await launch_browser_session_with_engine(headless=headless)

    results: list[dict[str, Any]] = []
    try:
        for job in top_jobs:
            logger.info(
                "Applying: %.0f %s - %s @ %s",
                job.score or 0,
                job.recommendation,
                job.role[:40],
                job.company[:25],
            )

            result = await run_single_application(
                job_url=job.url,
                company=job.company,
                role=job.role,
                memory_root=memory_root,
                profile=profile,
                question_answerer=question_answerer,
                dry_run=args.dry_run,
                force_submit=args.force,
                timeout_sec=args.timeout_sec,
                human_in_loop=args.human_in_loop,
                max_form_pages=args.max_form_pages,
                max_custom_questions=args.max_custom_questions,
                pause_on_captcha=not args.no_pause_on_captcha,
                pause_on_auth=not args.no_pause_on_auth,
                pause_on_missing_fields=not args.no_pause_on_missing_fields,
                allow_captcha_auto_solve=not args.no_captcha_auto_solve,
                source="simplify",
                quality=args.quality,
                tailor_resume=args.tailor_resume,
                upload_tailored_resume=args.upload_tailored_resume,
                keep_open=args.keep_open,
                session=session,
                headless=headless,
            )

            # Update ledger with apply status
            status = str(result.get("status", "unknown"))
            ledger.mark_applied(job.url, status)
            ledger.save()

            result["job_score"] = job.score
            result["job_recommendation"] = job.recommendation
            results.append(result)

    finally:
        if session is not None:
            await session.close()

    status_counts: dict[str, int] = {}
    for item in results:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "status": "apply_batch_complete",
        "applied_count": len(results),
        "min_score": min_score,
        "results": results,
        "status_counts": status_counts,
        "ledger_stats": ledger.stats(),
        "timestamp": utc_now_iso(),
    }


async def run_score_unscored(args: argparse.Namespace, memory_root: Path) -> dict[str, Any]:
    """Score all jobs in the ledger that haven't been scored yet."""
    ledger = JobLedger()
    unscored = ledger.get_unscored_jobs()

    if not unscored:
        return {
            "status": "no_new_jobs",
            "message": "No unscored jobs in ledger — everything has already been scored.",
            "ledger_stats": ledger.stats(),
            "timestamp": utc_now_iso(),
        }

    logger.info("Scoring %d unscored jobs from ledger", len(unscored))
    profile = load_user_profile(memory_root)
    scorer = JobScorer()

    scored_results: list[dict[str, Any]] = []
    for entry in unscored:
        scored = scorer.score_job(
            company=entry.company,
            role=entry.role,
            location=entry.location,
            job_description=entry.job_description,
            profile_summary=profile.summary,
            resume_text=profile.resume_text,
            age_hours=entry.age_hours_at_discovery,
        )
        scored.url = entry.url

        ledger.update_score(
            entry.url,
            score=scored.score,
            breakdown=scored.breakdown,
            reasoning=scored.reasoning,
            recommendation=scored.recommendation,
        )

        scored_results.append({
            "company": entry.company,
            "role": entry.role,
            "location": entry.location,
            "url": entry.url,
            "score": scored.score,
            "breakdown": scored.breakdown,
            "reasoning": scored.reasoning,
            "recommendation": scored.recommendation,
        })

        logger.info(
            "Scored: %.0f %s - %s @ %s",
            scored.score,
            scored.recommendation,
            entry.role[:40],
            entry.company[:25],
        )

    ledger.save()

    scored_results.sort(key=lambda x: x.get("score", 0), reverse=True)
    high_priority = sum(1 for r in scored_results if r.get("recommendation") == "high_priority")
    medium = sum(1 for r in scored_results if r.get("recommendation") == "medium")
    low = sum(1 for r in scored_results if r.get("recommendation") == "low")
    skip = sum(1 for r in scored_results if r.get("recommendation") == "skip")

    return {
        "status": "scoring_complete",
        "new_jobs_scored": len(scored_results),
        "total_in_source": len(unscored),
        "summary": {
            "high_priority": high_priority,
            "medium": medium,
            "low": low,
            "skip": skip,
        },
        "jobs": scored_results,
        "ledger_stats": ledger.stats(),
        "timestamp": utc_now_iso(),
    }


def run_ledger_stats() -> dict[str, Any]:
    """Print ledger statistics."""
    ledger = JobLedger()
    stats = ledger.stats()
    
    # Get top 5 jobs for quick view
    top_jobs = ledger.get_top_jobs(limit=5, unapplied_only=True)
    top_preview = [
        {
            "company": j.company,
            "role": j.role,
            "score": j.score,
            "recommendation": j.recommendation,
        }
        for j in top_jobs
    ]

    return {
        "status": "ledger_stats",
        "stats": stats,
        "top_unapplied_jobs": top_preview,
        "ledger_path": str(ledger.ledger_path),
        "timestamp": utc_now_iso(),
    }


async def async_main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)
    setup_logging(verbose=args.verbose)

    # Handle ledger stats (no memory root needed)
    if args.ledger_stats:
        result = run_ledger_stats()
        print(json.dumps(result, indent=2), flush=True)
        return 0

    # Handle list scored jobs (no memory root needed)
    if args.list_scored_jobs:
        result = await run_list_scored_jobs(args)
        print(json.dumps(result, indent=2), flush=True)
        return 0

    # Handle score-unscored (needs memory root for profile)
    should_persist = not (args.source == "simplify" and args.list_source_jobs)

    if should_persist:
        memory_root = _resolve_memory_root(args.memory_root)
    else:
        memory_root = Path(args.memory_root).expanduser()

    if should_persist:
        memory_root.mkdir(parents=True, exist_ok=True)
        (memory_root / "applications").mkdir(parents=True, exist_ok=True)

        missing = _missing_required_memory_files(memory_root)
        if missing:
            payload = {
                "status": "error",
                "error": "Missing required memory files",
                "memory_root": str(memory_root),
                "missing": missing,
                "suggested_memory_roots": _suggest_memory_roots(),
                "hint": "Pass --memory-root or set OPENCLAW_MEMORY_ROOT to a folder containing profile.json and resume.json.",
                "timestamp": utc_now_iso(),
            }
            print(json.dumps(payload, indent=2), flush=True)
            return 0

    if getattr(args, 'score_unscored', False):
        result = await run_score_unscored(args, memory_root)
        print(json.dumps(result, indent=2), flush=True)
        return 0

    if args.source == "simplify":
        if args.score_jobs:
            result = await run_score_jobs(args, memory_root)
        elif args.apply_top_scored:
            result = await run_apply_top_scored(args, memory_root)
        elif args.harvest_answer_bank:
            result = await run_source_harvest(args, memory_root)
        else:
            result = await run_source_mode(args, memory_root)
    else:
        if args.harvest_answer_bank:
            result = await run_direct_harvest(args, memory_root)
        else:
            result = await run_direct_mode(args, memory_root)

    print(json.dumps(result, indent=2), flush=True)
    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    except SystemExit as exc:
        # argparse calls sys.exit() for invalid CLI usage (and for --help).
        code = exc.code if isinstance(exc.code, int) else 1
        if code == 0:
            raise
        payload = {
            "status": "error",
            "error": "Invalid CLI arguments",
            "exit_code": code,
            "timestamp": utc_now_iso(),
        }
        print(json.dumps(payload, indent=2), flush=True)
        return 0
    except Exception as exc:
        payload = {
            "status": "error",
            "error": str(exc),
            "trace": traceback.format_exc(limit=6),
            "timestamp": utc_now_iso(),
        }
        print(json.dumps(payload, indent=2), flush=True)
        return 0


def _resolve_memory_root(raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate.resolve()
    except OSError:
        fallback = Path.cwd() / "memory"
        fallback.mkdir(parents=True, exist_ok=True)
        logger.warning(
            "Memory path '%s' is unavailable in this environment; using '%s' instead.",
            candidate,
            fallback,
        )
        return fallback.resolve()


if __name__ == "__main__":
    raise SystemExit(main())
