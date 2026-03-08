#!/usr/bin/env python3
"""
Isolated test for the Degree dropdown on Workday page 2 (Education).

Automates everything up to the degree dropdown, then dumps its DOM
before and after attempting to fill it.

Usage:
  python3 test_degree_dropdown.py
"""

import asyncio
import json
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
)
logger = logging.getLogger("test_degree")

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

    # ── Navigate & open form ──
    logger.info("Navigating to job page...")
    await page.goto(JOB_URL, wait_until="domcontentloaded")

    logger.info("Clicking Apply...")
    apply_btn = page.locator("button:has-text('Apply'), a:has-text('Apply')").first
    await apply_btn.click(timeout=10_000)
    await asyncio.sleep(2.0)

    # Handle dialogs
    for sel in [
        "button:has-text('Apply Manually')",
        "button[data-automation-id*='applyManually']",
        "[data-automation-id*='applyManually']",
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                await loc.click(timeout=3000)
                logger.info("Clicked dialog: %s", sel[:50])
                await asyncio.sleep(1.5)
                break
        except Exception:
            continue

    # ── Handle auth if needed ──
    try:
        from openclaw.auth import handle_auth_check
        await handle_auth_check(page, stage="test", job_url=JOB_URL)
    except Exception:
        logger.info("(No auth wall)")

    # ── Wait for page 1 form ──
    logger.info("Waiting for page 1 form...")
    for attempt in range(20):
        await asyncio.sleep(1.0)
        count = await page.evaluate("""() => {
            const els = document.querySelectorAll('input:not([type="hidden"]), select, textarea, [role="combobox"], [role="radio"], [role="checkbox"]');
            let n = 0;
            for (const el of els) { const r = el.getBoundingClientRect(); if (r.width >= 2 && r.height >= 2) n++; }
            return n;
        }""")
        if count >= 3:
            logger.info("  Page 1 ready: %d controls", count)
            break

    # ── Fill page 1 pre-fill controls ──
    from openclaw.ats.workday import WorkdayHandler
    handler = WorkdayHandler(ats_name="workday")

    logger.info("Filling 'How Did You Hear'...")
    await handler._fill_how_did_you_hear(page)

    logger.info("Clicking 'Previously Worked' -> No...")
    await handler._click_previously_worked_no(page)

    # ── Fill page 1 snapshot fields (name, address, etc) via the real applier ──
    # We'll just click Next and let validation tell us if something's missing.
    # Actually, let's fill the required fields manually for speed.
    logger.info("Filling page 1 fields...")
    field_fills = {
        "#name--legalName--firstName": "Aditya",
        "#name--legalName--lastName": "Singh",
        "#address--addressLine1": "266 Mayten Way",
        "#address--city": "Fremont",
        "#address--postalCode": "94539",
        "#emailAddress--emailAddress": "adisin650@gmail.com",
        "#phoneNumber--phoneNumber": "5104581848",
    }
    for sel, val in field_fills.items():
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1000):
                current = await loc.input_value()
                if not current.strip():
                    await loc.fill(val)
                    logger.info("  Filled %s", sel)
        except Exception:
            pass

    # Fill state dropdown
    try:
        state_btn = page.locator("#address--countryRegion").first
        if await state_btn.is_visible(timeout=1000):
            await state_btn.click(timeout=3000)
            await asyncio.sleep(0.5)
            opt = page.locator("[role='option']:has-text('California')").first
            if await opt.is_visible(timeout=3000):
                await opt.click(timeout=3000)
                logger.info("  Selected California")
    except Exception as e:
        logger.debug("State dropdown: %s", e)

    await asyncio.sleep(0.5)

    # ── Click Next to go to page 2 ──
    logger.info("Clicking Next to go to page 2...")
    try:
        next_btn = page.locator("button:has-text('Next')").first
        await next_btn.click(timeout=5000)
    except Exception as e:
        logger.error("Could not click Next: %s", e)
        await _pause(page, browser, pw)
        return

    # ── Wait for page 2 ──
    logger.info("Waiting for page 2 to load...")
    await asyncio.sleep(3.0)
    for attempt in range(15):
        await asyncio.sleep(1.0)
        has_edu = await page.evaluate("""() => {
            const el = document.querySelector('[data-automation-id*="workExperience"], [data-automation-id*="education"]');
            if (!el) return false;
            const r = el.getBoundingClientRect();
            return r.width >= 2 && r.height >= 2;
        }""")
        if has_edu:
            logger.info("  Page 2 loaded (attempt %d)", attempt + 1)
            break
    else:
        logger.warning("Page 2 may not have loaded fully")

    # ── Find the Degree dropdown and dump its DOM ──
    logger.info("=" * 60)
    logger.info("Looking for Degree dropdown...")
    logger.info("=" * 60)

    # First, find the Education section
    edu_section = page.get_by_role("group", name="Education 1", exact=True)
    edu_visible = False
    try:
        edu_visible = await edu_section.is_visible(timeout=3000)
    except Exception:
        pass
    logger.info("Education 1 section visible: %s", edu_visible)

    if not edu_visible:
        # Try to find any education section
        logger.info("Trying broader search for education/degree elements...")

    # Dump all elements with "degree" in their attributes or text
    degree_dom = await page.evaluate("""() => {
        const results = [];

        // Find by data-automation-id containing 'degree'
        document.querySelectorAll('[data-automation-id*="degree" i]').forEach(el => {
            const r = el.getBoundingClientRect();
            results.push({
                source: 'data-automation-id',
                tag: el.tagName,
                id: el.id || '',
                aid: el.getAttribute('data-automation-id') || '',
                role: el.getAttribute('role') || '',
                cls: (el.className || '').toString().slice(0, 60),
                text: (el.innerText || '').trim().slice(0, 100),
                visible: r.width > 0 && r.height > 0,
                w: Math.round(r.width),
                h: Math.round(r.height),
            });
        });

        // Find buttons with "Degree" in name/text
        document.querySelectorAll('button').forEach(el => {
            const text = (el.innerText || '').trim();
            const label = el.getAttribute('aria-label') || '';
            if (text.toLowerCase().includes('degree') || label.toLowerCase().includes('degree')) {
                const r = el.getBoundingClientRect();
                results.push({
                    source: 'button-with-degree-text',
                    tag: el.tagName,
                    id: el.id || '',
                    aid: el.getAttribute('data-automation-id') || '',
                    role: el.getAttribute('role') || '',
                    ariaLabel: label,
                    ariaExpanded: el.getAttribute('aria-expanded') || '',
                    ariaHaspopup: el.getAttribute('aria-haspopup') || '',
                    text: text.slice(0, 100),
                    visible: r.width > 0 && r.height > 0,
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                });
            }
        });

        // Find labels with "Degree"
        document.querySelectorAll('label').forEach(el => {
            const text = (el.innerText || '').trim();
            if (text.toLowerCase().includes('degree')) {
                const r = el.getBoundingClientRect();
                results.push({
                    source: 'label-with-degree',
                    tag: el.tagName,
                    id: el.id || '',
                    for: el.getAttribute('for') || '',
                    text: text.slice(0, 100),
                    visible: r.width > 0 && r.height > 0,
                });
            }
        });

        // Find combobox roles near Education section
        document.querySelectorAll('[role="combobox"], [role="listbox"], [aria-haspopup="listbox"]').forEach(el => {
            const r = el.getBoundingClientRect();
            const vis = r.width > 0 && r.height > 0;
            if (vis) {
                results.push({
                    source: 'combobox-or-listbox',
                    tag: el.tagName,
                    id: el.id || '',
                    aid: el.getAttribute('data-automation-id') || '',
                    role: el.getAttribute('role') || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    ariaExpanded: el.getAttribute('aria-expanded') || '',
                    text: (el.innerText || '').trim().slice(0, 100),
                    visible: vis,
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                });
            }
        });

        return results;
    }""")
    logger.info("[Degree DOM elements]\n%s", json.dumps(degree_dom, indent=2))

    # ── Now try clicking the degree button to open it ──
    logger.info("=" * 60)
    logger.info("Trying to open the Degree dropdown...")
    logger.info("=" * 60)

    try:
        import re
        degree_btn = page.get_by_role("button", name=re.compile(r"Degree", re.IGNORECASE)).first
        btn_visible = await degree_btn.is_visible(timeout=3000)
        logger.info("Degree button visible: %s", btn_visible)

        if btn_visible:
            await degree_btn.scroll_into_view_if_needed(timeout=5000)
            await degree_btn.click(timeout=5000)
            logger.info("Clicked degree button, waiting for options...")
            await asyncio.sleep(1.0)

            # Dump all options that appeared
            options_dom = await page.evaluate("""() => {
                const results = [];
                document.querySelectorAll("[role='option']").forEach(el => {
                    const r = el.getBoundingClientRect();
                    results.push({
                        tag: el.tagName,
                        id: el.id || '',
                        aid: el.getAttribute('data-automation-id') || '',
                        role: el.getAttribute('role') || '',
                        text: (el.innerText || '').trim().slice(0, 100),
                        visible: r.width > 0 && r.height > 0,
                    });
                });
                return results;
            }""")
            logger.info("[Degree dropdown OPTIONS after click]\n%s", json.dumps(options_dom, indent=2))
        else:
            logger.warning("Degree button not found via get_by_role")
    except Exception as e:
        logger.error("Degree button interaction failed: %s", e)

    await _pause(page, browser, pw)


async def _pause(page, browser, pw):
    logger.info("Browser open — inspect the result. Ctrl+C to close.")
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
