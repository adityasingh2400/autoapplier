from __future__ import annotations

import inspect
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


TOP_TIER = [
    "Google",
    "Meta",
    "Apple",
    "Amazon",
    "Microsoft",
    "Netflix",
    "OpenAI",
    "Anthropic",
    "DeepMind",
    "Stripe",
    "Databricks",
    "Scale AI",
    "Anduril",
    "Palantir",
    "SpaceX",
    "Tesla",
    "Jane Street",
    "Citadel",
    "Two Sigma",
    "DE Shaw",
    "HRT",
]


class ATSKind(str, Enum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    GENERIC = "generic"


ATS_HOST_MAP: dict[ATSKind, tuple[str, ...]] = {
    ATSKind.GREENHOUSE: ("greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io"),
    ATSKind.LEVER: ("lever.co", "jobs.lever.co"),
    ATSKind.ASHBY: ("ashbyhq.com", "jobs.ashbyhq.com"),
    ATSKind.WORKDAY: ("myworkdayjobs.com",),
}


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    # Suppress extremely noisy third-party debug loggers even in verbose mode.
    for noisy in ("botocore", "boto3", "urllib3", "asyncio", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def detect_ats(url: str) -> ATSKind:
    hostname = urlparse(url).netloc.lower()
    for kind, hosts in ATS_HOST_MAP.items():
        for host in hosts:
            if host in hostname:
                return kind
    return ATSKind.GENERIC


def is_top_tier_company(company: str) -> bool:
    normalized = company.casefold().strip()
    return any(tier.casefold() == normalized for tier in TOP_TIER)


def create_run_dir(memory_root: Path, company: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = memory_root / "applications" / f"{ts}_{slugify(company)}"
    run_dir = base
    index = 1
    while run_dir.exists():
        run_dir = memory_root / "applications" / f"{ts}_{slugify(company)}_{index:02d}"
        index += 1
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def build_application_id(company: str) -> str:
    ts = datetime.now().strftime("%Y%m%d")
    return f"app_{ts}_{slugify(company)}_{datetime.now().strftime('%H%M%S')}"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def human_pause(prompt: str) -> str:
    """
    Pause for human input without blocking the event loop.
    Intended for local/headful "human-in-loop" runs.
    """
    try:
        import asyncio

        return str(await asyncio.to_thread(input, prompt))
    except Exception:
        return ""


async def smart_goto(
    page: Any,
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 60_000,
) -> None:
    logger.debug("smart_goto: %s (wait_until=%s, timeout=%dms)", url, wait_until, timeout_ms)
    goto_fn = getattr(page, "goto", None)
    if goto_fn is None:
        raise RuntimeError("Page object does not support navigation.")

    # Playwright uses `wait_until`; many wrappers use `waitUntil`. Fall back to URL-only.
    try:
        await maybe_await(goto_fn(url, wait_until=wait_until, timeout=timeout_ms))
        return
    except TypeError:
        pass
    try:
        await maybe_await(goto_fn(url, waitUntil=wait_until, timeout=timeout_ms))
        return
    except TypeError:
        pass
    try:
        await maybe_await(goto_fn(url, timeout=timeout_ms))
        return
    except TypeError:
        pass
    await maybe_await(goto_fn(url))


async def smart_screenshot(page: Any, path: Path) -> bool:
    shot_fn = getattr(page, "screenshot", None)
    if shot_fn is None:
        return False

    try:
        await maybe_await(shot_fn(path=str(path), full_page=True))
        return True
    except TypeError:
        try:
            await maybe_await(shot_fn(path=str(path)))
            return True
        except Exception:
            return False
    except Exception:
        return False


async def capture_step(page: Any, output_dir: Path, step_name: str, screenshots: list[str]) -> str | None:
    filename = f"{step_name}.png"
    path = output_dir / filename
    logger.debug("Capturing screenshot: %s", filename)
    if await smart_screenshot(page, path):
        screenshots.append(filename)
        return filename
    return None


async def smart_click(
    page: Any,
    prompt: str | None = None,
    selectors: Iterable[str] | None = None,
    text_candidates: Iterable[str] | None = None,
    *,
    prefer_prompt: bool = True,
) -> bool:
    logger.debug("smart_click: prompt=%s, selectors=%s, text=%s",
                 prompt and prompt[:50], selectors and list(selectors)[:2], text_candidates and list(text_candidates)[:2])
    click_fn = getattr(page, "click", None)
    if not prefer_prompt:
        if selectors:
            for selector in selectors:
                if await _click_selector(page, selector):
                    return True

        if text_candidates:
            for text in text_candidates:
                if await _click_by_text(page, text):
                    return True

    if prompt and click_fn:
        try:
            await maybe_await(click_fn(prompt=prompt))
            return True
        except TypeError:
            pass
        except Exception:
            pass

    if prefer_prompt:
        if selectors:
            for selector in selectors:
                if await _click_selector(page, selector):
                    return True

        if text_candidates:
            for text in text_candidates:
                if await _click_by_text(page, text):
                    return True
    return False


async def smart_fill(
    page: Any,
    prompt: str,
    value: str,
    selectors: Iterable[str] | None = None,
    *,
    prefer_prompt: bool = True,
) -> bool:
    logger.debug("smart_fill: prompt=%s, value=%s",
                 prompt[:50] if prompt else None, value[:30] if value else None)
    fill_fn = getattr(page, "fill", None)
    if not prefer_prompt:
        if selectors:
            for selector in selectors:
                if await _fill_selector(page, selector, value):
                    return True

    if fill_fn and prompt:
        try:
            await maybe_await(fill_fn(prompt=prompt, value=value))
            return True
        except TypeError:
            pass
        except Exception:
            pass

    if prefer_prompt:
        if selectors:
            for selector in selectors:
                if await _fill_selector(page, selector, value):
                    return True
    return False


async def smart_upload(
    page: Any,
    prompt: str,
    file_path: Path,
    selectors: Iterable[str] | None = None,
    *,
    prefer_prompt: bool = True,
) -> bool:
    logger.debug("smart_upload: prompt=%s, file=%s", prompt, file_path)
    upload_fn = getattr(page, "upload_file", None)
    if not prefer_prompt:
        if selectors:
            for selector in selectors:
                if await _set_input_file(page, selector, file_path):
                    return True

    if upload_fn and prompt:
        try:
            await maybe_await(upload_fn(prompt=prompt, files=str(file_path)))
            return True
        except TypeError:
            pass
        except Exception:
            pass

    if prefer_prompt:
        if selectors:
            for selector in selectors:
                if await _set_input_file(page, selector, file_path):
                    return True
    return False


async def _click_selector(page: Any, selector: str) -> bool:
    locator_fn = getattr(page, "locator", None)
    if locator_fn:
        try:
            locator = locator_fn(selector)
            count = await maybe_await(locator.count())
            if count > 0:
                # Some pages render hidden duplicate controls; prefer the first visible match.
                target = None
                for i in range(min(int(count), 8)):
                    cand = locator.nth(i)
                    try:
                        visible = await maybe_await(cand.is_visible())
                    except Exception:
                        visible = False
                    if visible:
                        target = cand
                        break
                if target is None:
                    target = locator.first
                try:
                    scroll_fn = getattr(target, "scroll_into_view_if_needed", None)
                    if scroll_fn:
                        await maybe_await(scroll_fn(timeout=2000))
                except Exception:
                    pass
                await maybe_await(target.click(timeout=2000))
                return True
        except Exception:
            pass

    click_fn = getattr(page, "click", None)
    if click_fn:
        try:
            try:
                await maybe_await(click_fn(selector, timeout=2000))
            except TypeError:
                await maybe_await(click_fn(selector))
            return True
        except Exception:
            pass
    return False


async def _fill_selector(page: Any, selector: str, value: str) -> bool:
    locator_fn = getattr(page, "locator", None)
    if locator_fn:
        try:
            locator = locator_fn(selector)
            count = await maybe_await(locator.count())
            if count > 0:
                # Some pages render hidden duplicate controls; prefer the first visible match.
                first = None
                for i in range(min(int(count), 8)):
                    cand = locator.nth(i)
                    try:
                        visible = await maybe_await(cand.is_visible())
                    except Exception:
                        visible = False
                    if visible:
                        first = cand
                        break
                if first is None:
                    first = locator.first
                try:
                    scroll_fn = getattr(first, "scroll_into_view_if_needed", None)
                    if scroll_fn:
                        await maybe_await(scroll_fn(timeout=2000))
                except Exception:
                    pass
                # Native <select> needs select_option, not fill.
                select_opt = getattr(first, "select_option", None)
                if select_opt:
                    try:
                        # Prefer label match, then value match.
                        await maybe_await(select_opt(label=value, timeout=2000))
                        return True
                    except Exception:
                        try:
                            await maybe_await(select_opt(value=value, timeout=2000))
                            return True
                        except Exception:
                            pass

                # Boolean inputs should use check/uncheck semantics instead of fill.
                normalized = str(value or "").strip().lower()
                try:
                    input_type = str(await maybe_await(first.get_attribute("type")) or "").strip().lower()
                except Exception:
                    input_type = ""
                if input_type in ("checkbox", "radio"):
                    # Be conservative: only check when value is truthy.
                    truthy = {"true", "yes", "y", "1", "on", "checked"}
                    if input_type == "radio" or normalized in truthy:
                        check_fn = getattr(first, "check", None)
                        if check_fn:
                            try:
                                await maybe_await(check_fn(timeout=2000))
                                return True
                            except Exception:
                                pass
                        try:
                            await maybe_await(first.click(timeout=2000))
                            return True
                        except Exception:
                            pass
                    return False
                await maybe_await(first.fill(value, timeout=2000))
                return True
        except Exception:
            pass

    fill_fn = getattr(page, "fill", None)
    if fill_fn:
        try:
            try:
                await maybe_await(fill_fn(selector, value, timeout=2000))
            except TypeError:
                await maybe_await(fill_fn(selector, value))
            return True
        except Exception:
            pass
    return False


async def _set_input_file(page: Any, selector: str, file_path: Path) -> bool:
    set_input_files_fn = getattr(page, "set_input_files", None)
    if set_input_files_fn:
        try:
            try:
                await maybe_await(set_input_files_fn(selector, str(file_path), timeout=2000))
            except TypeError:
                await maybe_await(set_input_files_fn(selector, str(file_path)))
            return True
        except Exception:
            pass

    locator_fn = getattr(page, "locator", None)
    if locator_fn:
        try:
            locator = locator_fn(selector)
            count = await maybe_await(locator.count())
            if count > 0:
                await maybe_await(locator.first.set_input_files(str(file_path), timeout=2000))
                return True
        except Exception:
            pass
    return False


async def _click_by_text(page: Any, text: str) -> bool:
    get_by_text_fn = getattr(page, "get_by_text", None)
    if get_by_text_fn:
        try:
            target = get_by_text_fn(text, exact=False)
            await maybe_await(target.first.click(timeout=2000))
            return True
        except Exception:
            pass

    locator_fn = getattr(page, "locator", None)
    if locator_fn:
        try:
            locator = locator_fn(f"text={text}")
            count = await maybe_await(locator.count())
            if count > 0:
                await maybe_await(locator.first.click(timeout=2000))
                return True
        except Exception:
            pass
    return False


@dataclass(slots=True)
class ExecutionArtifacts:
    output_dir: Path
    screenshots: list[str]
