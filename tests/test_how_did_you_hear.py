#!/usr/bin/env python3
"""
Isolated test for the 'How Did You Hear About Us?' Workday field.

Usage:
  python3 test_how_did_you_hear.py

This script:
  1. Launches a headed Chromium browser
  2. Navigates to the Flextronics Workday job posting
  3. Clicks Apply / Apply Manually to open the form
  4. Waits for the form to be ready
  5. Runs ONLY the _fill_how_did_you_hear logic
  6. Pauses so you can inspect the result

Press Ctrl+C to close the browser when done.
"""

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
)
logger = logging.getLogger("test_hdyh")

JOB_URL = (
    "https://flextronics.wd1.myworkdayjobs.com/Careers/job/"
    "USA-UT-Salt-Lake-City/IT-ERP-Intern---Summer-2026_WD213943"
    "?utm_source=Simplify&ref=Simplify"
)


async def main() -> None:
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()
    page.set_default_navigation_timeout(60_000)
    page.set_default_timeout(60_000)

    # ── Navigate to job page ──
    logger.info("Navigating to job page...")
    await page.goto(JOB_URL, wait_until="domcontentloaded")
    logger.info("Job page loaded.")

    # ── Click Apply ──
    logger.info("Clicking Apply...")
    try:
        apply_btn = page.locator("button:has-text('Apply'), a:has-text('Apply')").first
        await apply_btn.click(timeout=10_000)
    except Exception as e:
        logger.error("Could not click Apply: %s", e)
        await _pause(page, browser, pw)
        return

    await asyncio.sleep(2.0)

    # ── Handle Workday dialog (Apply Manually, etc.) ──
    dialog_selectors = [
        "button:has-text('Apply Manually')",
        "button:has-text('Use My Information')",
        "button:has-text('Use Last Application')",
        "button:has-text('Continue')",
        "button[data-automation-id*='applyManually']",
        "[data-automation-id*='applyManually']",
    ]
    for _ in range(3):
        clicked = False
        for sel in dialog_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible():
                    await loc.click(timeout=3000)
                    clicked = True
                    logger.info("Clicked dialog: %s", sel[:50])
                    await asyncio.sleep(1.5)
                    break
            except Exception:
                continue
        if not clicked:
            break

    # ── Handle auth wall if present ──
    try:
        from openclaw.auth import handle_auth_check
        await handle_auth_check(page, stage="test", job_url=JOB_URL)
    except Exception:
        logger.info("(No auth module or no auth wall — continuing)")

    # ── Wait for form to be ready ──
    logger.info("Waiting for form controls to appear...")
    for attempt in range(20):
        await asyncio.sleep(1.0)
        count = await page.evaluate("""
            () => {
                const els = document.querySelectorAll(
                    'input:not([type="hidden"]), select, textarea, '
                    + '[role="combobox"], [role="radio"], [role="checkbox"]'
                );
                let n = 0;
                for (const el of els) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width >= 2 && rect.height >= 2) n++;
                }
                return n;
            }
        """)
        logger.info("  Attempt %d: %d controls visible", attempt + 1, count)
        if count >= 3:
            break
    else:
        logger.error("Form never loaded enough controls. Check browser.")
        await _pause(page, browser, pw)
        return

    # ── Check if formField-source exists ──
    source_visible = await page.locator("[data-automation-id='formField-source']").first.is_visible()
    logger.info("formField-source visible: %s", source_visible)
    if not source_visible:
        logger.error("'How Did You Hear' field not on this page. Pausing for inspection.")
        await _pause(page, browser, pw)
        return

    # ── Run the actual fill logic ──
    logger.info("=" * 60)
    logger.info("Running _fill_how_did_you_hear ...")
    logger.info("=" * 60)

    from openclaw.ats.workday import WorkdayHandler
    from openclaw.utils import maybe_await

    handler = WorkdayHandler(ats_name="workday")
    result = await handler._fill_how_did_you_hear(page)

    logger.info("=" * 60)
    logger.info("_fill_how_did_you_hear returned: %s", result)
    logger.info("=" * 60)

    # ── Final check ──
    sel_text = await page.locator(
        "[data-automation-id='formField-source'] "
        "[data-automation-id='promptAriaInstruction']"
    ).first.text_content()
    logger.info("Final selection state: '%s'", (sel_text or "").strip())

    # ── Run "Previously Worked" -> No ──
    logger.info("=" * 60)
    logger.info("Running _click_previously_worked_no ...")
    logger.info("=" * 60)

    # Dump the DOM for the "previously worked" field BEFORE clicking
    pw_dom = await page.evaluate("""
        () => {
            const container = document.querySelector(
                '[data-automation-id="formField-candidateIsPreviousWorker"]'
            );
            if (!container) return { exists: false };

            // Get the full HTML (truncated)
            const html = container.outerHTML.slice(0, 2000);

            // Get all children with details
            const children = [];
            container.querySelectorAll('*').forEach(el => {
                const rect = el.getBoundingClientRect();
                const vis = rect.width > 0 && rect.height > 0;
                children.push({
                    tag: el.tagName,
                    id: el.id || '',
                    aid: el.getAttribute('data-automation-id') || '',
                    role: el.getAttribute('role') || '',
                    type: el.getAttribute('type') || '',
                    value: el.getAttribute('value') || '',
                    name: el.getAttribute('name') || '',
                    checked: el.checked || false,
                    ariaChecked: el.getAttribute('aria-checked') || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    cls: (el.className || '').toString().slice(0, 50),
                    text: (el.innerText || '').trim().slice(0, 60),
                    visible: vis,
                    w: Math.round(rect.width),
                    h: Math.round(rect.height),
                });
            });
            return { exists: true, html, children };
        }
    """)
    import json
    logger.info("[PreviouslyWorked DOM BEFORE]\n%s", json.dumps(pw_dom, indent=2))

    pw_result = await handler._click_previously_worked_no(page)

    # Dump AFTER clicking
    pw_dom_after = await page.evaluate("""
        () => {
            const container = document.querySelector(
                '[data-automation-id="formField-candidateIsPreviousWorker"]'
            );
            if (!container) return { exists: false };
            const children = [];
            container.querySelectorAll('*').forEach(el => {
                const rect = el.getBoundingClientRect();
                const vis = rect.width > 0 && rect.height > 0;
                children.push({
                    tag: el.tagName,
                    id: el.id || '',
                    aid: el.getAttribute('data-automation-id') || '',
                    role: el.getAttribute('role') || '',
                    type: el.getAttribute('type') || '',
                    value: el.getAttribute('value') || '',
                    checked: el.checked || false,
                    ariaChecked: el.getAttribute('aria-checked') || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    text: (el.innerText || '').trim().slice(0, 60),
                    visible: vis,
                });
            });
            return { exists: true, children };
        }
    """)
    logger.info("[PreviouslyWorked DOM AFTER]\n%s", json.dumps(pw_dom_after, indent=2))

    logger.info("=" * 60)
    logger.info("_click_previously_worked_no returned: %s", pw_result)
    logger.info("=" * 60)

    await _pause(page, browser, pw)


async def _pause(page, browser, pw):
    logger.info("Browser is open — inspect the result. Press Ctrl+C to close.")
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await browser.close()
        await pw.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDone.")
