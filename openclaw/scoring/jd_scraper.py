"""Job description scraper using Playwright."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def _default_cache_dir() -> Path:
    """Default JD cache location: ~/.openclaw/jd_cache/"""
    return Path.home() / ".openclaw" / "jd_cache"


def _cache_key(url: str) -> str:
    """Generate a cache filename for a URL."""
    return hashlib.sha256(url.encode()).hexdigest()[:24] + ".json"


# Common selectors for job descriptions across different ATS platforms
JD_SELECTORS = [
    # Workday
    "[data-automation-id='jobPostingDescription']",
    ".job-description",
    ".jobDescription",
    # Greenhouse
    "#content",
    ".job__description",
    "[data-qa='job-description']",
    # Lever
    ".posting-page .content",
    ".posting-description",
    "[data-qa='posting-description']",
    # Ashby
    ".ashby-job-posting-description",
    "[data-testid='job-description']",
    # Generic fallbacks
    "article",
    ".job-details",
    ".job-content",
    "[role='main']",
    "main",
]

# Selectors to exclude (navigation, headers, footers, etc.)
EXCLUDE_SELECTORS = [
    "nav",
    "header",
    "footer",
    ".nav",
    ".header",
    ".footer",
    ".sidebar",
    "[role='navigation']",
    "[role='banner']",
    "[role='contentinfo']",
    "script",
    "style",
    "noscript",
]


async def scrape_job_description(
    url: str,
    *,
    cache_dir: Path | None = None,
    timeout_ms: int = 15000,
    use_cache: bool = True,
) -> str:
    """
    Scrape the job description from a job posting URL.
    
    Returns the extracted text, or empty string on failure.
    Uses caching to avoid re-scraping the same URL.
    """
    cache_dir = cache_dir or _default_cache_dir()
    cache_file = cache_dir / _cache_key(url)

    # Check cache first
    if use_cache and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            jd = cached.get("job_description", "")
            if jd:
                logger.debug("JD cache hit for %s", url[:60])
                return jd
        except Exception:
            pass

    # Scrape the page
    jd = await _scrape_page(url, timeout_ms=timeout_ms)

    # Cache the result (even if empty, to avoid re-scraping failures)
    if use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            cache_file.write_text(
                json.dumps({"url": url, "job_description": jd}, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("Failed to cache JD: %s", exc)

    return jd


async def scrape_job_descriptions_batch(
    urls: list[str],
    *,
    cache_dir: Path | None = None,
    timeout_ms: int = 15000,
    concurrency: int = 5,
    use_cache: bool = True,
) -> dict[str, str]:
    """
    Scrape job descriptions for multiple URLs in parallel.
    
    Returns a dict mapping URL -> job description text.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def scrape_one(url: str) -> tuple[str, str]:
        async with semaphore:
            jd = await scrape_job_description(
                url,
                cache_dir=cache_dir,
                timeout_ms=timeout_ms,
                use_cache=use_cache,
            )
            return (url, jd)

    tasks = [scrape_one(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    output: dict[str, str] = {}
    for result in results:
        if isinstance(result, tuple):
            url, jd = result
            output[url] = jd
        # Skip exceptions silently - we'll just have empty JDs for those

    logger.info(
        "Scraped %d/%d job descriptions",
        sum(1 for jd in output.values() if jd),
        len(urls),
    )
    return output


async def _scrape_page(url: str, *, timeout_ms: int) -> str:
    """Actually scrape a page using Playwright."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright not available for JD scraping")
        return ""

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            try:
                await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                # Give JS a moment to render
                await page.wait_for_timeout(2000)
            except Exception as exc:
                logger.debug("Failed to load page %s: %s", url[:60], exc)
                await browser.close()
                return ""

            # Try to extract job description
            jd = await _extract_jd_from_page(page)

            await browser.close()
            return jd

    except Exception as exc:
        logger.debug("Playwright scraping failed for %s: %s", url[:60], exc)
        return ""


async def _extract_jd_from_page(page: Any) -> str:
    """Extract job description text from a loaded page."""
    # Try each selector in order
    for selector in JD_SELECTORS:
        try:
            element = await page.query_selector(selector)
            if element:
                text = await element.inner_text()
                text = _clean_text(text)
                if len(text) > 200:  # Minimum viable JD length
                    logger.debug("Found JD using selector: %s (%d chars)", selector, len(text))
                    return text
        except Exception:
            continue

    # Fallback: get all text from body, excluding nav/header/footer
    try:
        # Remove unwanted elements first
        for exclude_sel in EXCLUDE_SELECTORS:
            try:
                await page.evaluate(f"""
                    document.querySelectorAll('{exclude_sel}').forEach(el => el.remove());
                """)
            except Exception:
                pass

        body = await page.query_selector("body")
        if body:
            text = await body.inner_text()
            text = _clean_text(text)
            if len(text) > 200:
                logger.debug("Used body fallback for JD (%d chars)", len(text))
                return text[:15000]  # Cap at 15k chars
    except Exception:
        pass

    return ""


def _clean_text(text: str) -> str:
    """Clean up extracted text."""
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text)
    # Remove excessive newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip
    text = text.strip()
    # Cap length
    if len(text) > 15000:
        text = text[:15000]
    return text
