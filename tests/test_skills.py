#!/usr/bin/env python3
"""
Isolated test for the Skills section on Workday page 2.

Automates pages 1-2 (fills everything EXCEPT skills), then:
1. DOM dump of the Skills section BEFORE attempting to fill
2. Attempts to fill skills one at a time with DOM dumps between each
3. DOM dump AFTER all skills are attempted

Usage:
  python3 test_skills.py
"""

import asyncio
import json
import logging
import os

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
)
logger = logging.getLogger("test_skills")

JOB_URL = (
    "https://flextronics.wd1.myworkdayjobs.com/Careers/job/"
    "USA-UT-Salt-Lake-City/IT-ERP-Intern---Summer-2026_WD213943"
    "?utm_source=Simplify&ref=Simplify"
)

# We'll test with just 3 skills to keep it fast and debuggable
TEST_SKILLS = ["Python", "Java", "Docker"]


async def dump_skills_dom(page, label: str) -> dict:
    """Dump the DOM state of the Skills section and nearby multiselect areas."""
    result = await page.evaluate("""() => {
        const out = {};

        // 1. Find ALL visible textbox inputs and their state
        const allInputs = document.querySelectorAll('input[type="text"], input:not([type])');
        const inputStates = [];
        for (const inp of allInputs) {
            const r = inp.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) continue;
            const ariaLabel = inp.getAttribute('aria-label') || '';
            const placeholder = inp.getAttribute('placeholder') || '';
            const name = inp.getAttribute('name') || '';
            const id = inp.id || '';
            const value = inp.value || '';
            // Check if this looks like a skills or field-of-study input
            if (ariaLabel.toLowerCase().includes('skill') ||
                ariaLabel.toLowerCase().includes('field of study') ||
                ariaLabel.toLowerCase().includes('search') ||
                placeholder.toLowerCase().includes('search') ||
                id.toLowerCase().includes('skill')) {
                inputStates.push({
                    id, name, ariaLabel, placeholder, value,
                    tagName: inp.tagName,
                    type: inp.type,
                    role: inp.getAttribute('role'),
                    focused: document.activeElement === inp,
                    rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
                });
            }
        }
        out.relevantInputs = inputStates;

        // 2. All visible listboxes
        const listboxes = [];
        for (const lb of document.querySelectorAll('[role="listbox"]')) {
            const r = lb.getBoundingClientRect();
            const visible = r.width > 0 && r.height > 0;
            const optCount = lb.querySelectorAll('[role="option"]').length;
            listboxes.push({
                id: lb.id || '',
                visible,
                optionCount: optCount,
                rect: visible ? { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) } : null,
                firstOptions: Array.from(lb.querySelectorAll('[role="option"]')).slice(0, 3).map(o => o.textContent.trim().substring(0, 60)),
            });
        }
        out.listboxes = listboxes;

        // 3. Selected items / chips in Skills multiselect
        const chips = [];
        for (const chip of document.querySelectorAll('[data-automation-id="selectedItem"]')) {
            chips.push(chip.textContent.trim().substring(0, 80));
        }
        out.selectedChips = chips;

        // 4. The Skills section container state
        const skillsSections = [];
        for (const section of document.querySelectorAll('[aria-label*="Skill"], [data-automation-id*="skill"], [data-automation-id*="Skill"]')) {
            skillsSections.push({
                tagName: section.tagName,
                ariaLabel: section.getAttribute('aria-label') || '',
                automationId: section.getAttribute('data-automation-id') || '',
                childCount: section.children.length,
            });
        }
        out.skillsSections = skillsSections;

        // 5. Field of Study state
        const fosInputs = [];
        for (const inp of document.querySelectorAll('input')) {
            const al = (inp.getAttribute('aria-label') || '').toLowerCase();
            if (al.includes('field of study')) {
                fosInputs.push({
                    id: inp.id || '',
                    ariaLabel: inp.getAttribute('aria-label'),
                    value: inp.value || '',
                    focused: document.activeElement === inp,
                });
            }
        }
        out.fieldOfStudyInputs = fosInputs;

        // 6. Currently active/focused element
        const ae = document.activeElement;
        out.activeElement = ae ? {
            tagName: ae.tagName,
            id: ae.id || '',
            ariaLabel: ae.getAttribute('aria-label') || '',
            role: ae.getAttribute('role') || '',
            value: ae.value || '',
        } : null;

        return out;
    }""")
    logger.info("── %s ──", label)
    logger.info("  Relevant inputs: %s", json.dumps(result.get("relevantInputs", []), indent=2))
    logger.info("  Listboxes: %s", json.dumps(result.get("listboxes", []), indent=2))
    logger.info("  Selected chips: %s", result.get("selectedChips", []))
    logger.info("  Skills sections: %s", json.dumps(result.get("skillsSections", []), indent=2))
    logger.info("  Field of Study inputs: %s", json.dumps(result.get("fieldOfStudyInputs", []), indent=2))
    logger.info("  Active element: %s", json.dumps(result.get("activeElement"), indent=2))
    return result


async def main() -> None:
    from pathlib import Path
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

    # Handle "Apply Manually" dialog
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

    # ── Wait for page 1 form ──
    logger.info("Waiting for page 1 form...")
    for attempt in range(20):
        await asyncio.sleep(1.0)
        count = await page.evaluate("""() => {
            const els = document.querySelectorAll('input:not([type="hidden"]), select, textarea, [role="combobox"]');
            let n = 0;
            for (const el of els) { const r = el.getBoundingClientRect(); if (r.width >= 2 && r.height >= 2) n++; }
            return n;
        }""")
        if count >= 3:
            logger.info("  Page 1 ready: %d controls", count)
            break

    # ── Fill page 1 quickly ──
    from openclaw.ats.workday import WorkdayHandler
    from openclaw.utils import maybe_await
    handler = WorkdayHandler(ats_name="workday")

    logger.info("Filling page 1...")
    await handler._fill_how_did_you_hear(page)
    await handler._click_previously_worked_no(page)

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
        except Exception:
            pass

    # State dropdown
    try:
        state_btn = page.locator("#address--countryRegion").first
        if await state_btn.is_visible(timeout=1000):
            await state_btn.click(timeout=3000)
            await asyncio.sleep(0.5)
            opt = page.locator("[role='option']:has-text('California')").first
            if await opt.is_visible(timeout=3000):
                await opt.click(timeout=3000)
    except Exception:
        pass

    # Upload resume
    resume_path = os.path.join(os.path.dirname(__file__), "real_memory", "resume.pdf")
    try:
        file_input = page.locator("input[type='file']").first
        if await file_input.count() > 0:
            await file_input.set_input_files(resume_path)
            logger.info("Resume uploaded via file input")
            await asyncio.sleep(1.0)
        else:
            async with page.expect_file_chooser(timeout=5000) as fc_info:
                upload_btn = page.locator("button:has-text('Select file'), button:has-text('Upload')").first
                await upload_btn.click(timeout=3000)
            fc = await fc_info.value
            await fc.set_files(resume_path)
            logger.info("Resume uploaded via file chooser")
            await asyncio.sleep(1.0)
    except Exception as e:
        logger.warning("Resume upload failed: %s", e)

    # ── Click Next to page 2 ──
    logger.info("Clicking Next -> page 2...")
    await page.locator("button:has-text('Next')").first.click(timeout=5000)
    await asyncio.sleep(8.0)

    # Wait for page 2 to load
    for _ in range(15):
        await asyncio.sleep(1.0)
        count = await page.evaluate("""() => {
            const els = document.querySelectorAll('input:not([type="hidden"]), select, textarea, [role="combobox"]');
            let n = 0;
            for (const el of els) { const r = el.getBoundingClientRect(); if (r.width >= 2 && r.height >= 2) n++; }
            return n;
        }""")
        if count >= 3:
            logger.info("  Page 2 loaded: %d controls", count)
            break

    # ── Fill Work Experience + Education (but NOT skills) ──
    logger.info("Loading profile for pre-fill...")
    from openclaw.profile import load_user_profile

    real_profile = load_user_profile(Path("real_memory"))

    class FakeQuestionAnswerer:
        async def answer(self, *a, **kw):
            return ""
        async def cover_letter(self, *a, **kw):
            return ""
        async def tailor_resume(self, *a, **kw):
            return ""
        async def pick_option(self, *a, **kw):
            return None

    class FakeCtx:
        def __init__(self):
            self.profile = real_profile
            self.company = "Flextronics"
            self.role = "IT ERP Intern"
            self.job_description = ""
            self.source = "test"
            self.question_answerer = FakeQuestionAnswerer()

    ctx = FakeCtx()
    resume_data = ctx.profile.resume

    logger.info("Filling Work Experience...")
    await handler._fill_work_experience(page, resume_data)

    logger.info("Filling Education...")
    await handler._fill_education(page, ctx, resume_data)

    # ══════════════════════════════════════════════════════════════
    # SKILLS ISOLATION TEST
    # ══════════════════════════════════════════════════════════════

    logger.info("=" * 60)
    logger.info("SKILLS ISOLATION TEST")
    logger.info("=" * 60)

    # DOM DUMP 1: Before any skills interaction
    await dump_skills_dom(page, "DOM STATE: BEFORE SKILLS (post-education)")

    # Now try the existing dismissal logic that runs between education and skills
    logger.info("Running dropdown dismissal (same as main code)...")
    keyboard = page.keyboard
    mouse = page.mouse
    try:
        await keyboard.press("Escape")
        await asyncio.sleep(0.3)
        await mouse.click(10, 10)
        await asyncio.sleep(0.5)
        listbox_open = await page.evaluate("""() => {
            const lb = document.querySelector("[role='listbox']");
            if (!lb) return false;
            const r = lb.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        }""")
        logger.info("  Listbox still open after dismissal: %s", listbox_open)
        if listbox_open:
            await keyboard.press("Escape")
            await asyncio.sleep(0.3)
            await mouse.click(10, 10)
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.warning("Dismissal failed: %s", e)

    # DOM DUMP 2: After dismissal, before clicking skills input
    await dump_skills_dom(page, "DOM STATE: AFTER DISMISSAL, BEFORE SKILLS INPUT CLICK")

    # Find the skills input
    skills_input = page.get_by_role("textbox", name="Type to Add Skills")
    is_visible = await skills_input.is_visible(timeout=5000)
    logger.info("Skills input visible: %s", is_visible)

    if not is_visible:
        logger.error("Skills input not found — aborting skills test")
        await dump_skills_dom(page, "DOM STATE: SKILLS INPUT NOT FOUND")
        input("Press Enter to close browser...")
        await browser.close()
        await pw.stop()
        return

    # DOM DUMP 3: After clicking the skills input
    logger.info("Clicking skills input...")
    try:
        el_handle = await skills_input.element_handle(timeout=3000)
        if el_handle:
            await page.evaluate("(el) => { el.click(); el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); }", el_handle)
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.warning("Skills input click failed: %s", e)

    await dump_skills_dom(page, "DOM STATE: AFTER SKILLS INPUT CLICK")

    # Now try adding skills one by one with DOM dumps
    for i, skill in enumerate(TEST_SKILLS):
        logger.info("─" * 40)
        logger.info("ADDING SKILL %d/%d: '%s'", i + 1, len(TEST_SKILLS), skill)
        logger.info("─" * 40)

        # Step 1: Dismiss any dropdown, click neutral
        try:
            neutral = page.locator(
                "[data-automation-id='progressBar'], "
                "[data-automation-id='pageHeaderTitle'], h2"
            ).first
            await neutral.click(force=True, timeout=2000)
            await asyncio.sleep(0.3)
        except Exception:
            pass

        # Step 2: Clear and click skills input
        try:
            el_handle = await skills_input.element_handle(timeout=3000)
            if el_handle:
                await page.evaluate(
                    "(el) => { el.click(); el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); }",
                    el_handle,
                )
                await asyncio.sleep(0.3)
        except Exception as e:
            logger.warning("  Clear/click failed: %s — fallback triple click", e)
            await skills_input.click(click_count=3, force=True, timeout=3000)
            await asyncio.sleep(0.1)
            await keyboard.press("Backspace")
            await asyncio.sleep(0.2)

        await dump_skills_dom(page, f"SKILL '{skill}': AFTER CLEAR/CLICK INPUT")

        # Step 3: Type skill and press Enter
        logger.info("  Typing '%s' and pressing Enter...", skill)
        await skills_input.fill(skill)
        await asyncio.sleep(0.3)
        await keyboard.press("Enter")
        await asyncio.sleep(1.5)  # Wait for search results

        await dump_skills_dom(page, f"SKILL '{skill}': AFTER TYPE + ENTER (search results)")

        # Step 4: Try to click first visible option
        # Use the SEARCH RESULTS listbox selector (aria-label contains "Expanded")
        # to avoid picking up "selected items" display options (like "Computer Science")
        all_opts = page.locator("[role='listbox'][aria-label*='Expanded'] [role='option']")
        count = await all_opts.count()
        logger.info("  Visible listbox options: %d", count)

        # Also check global options for comparison
        global_opts = page.locator("[role='option']")
        global_count = await global_opts.count()
        logger.info("  Global [role='option'] count: %d", global_count)

        if count > 0:
            # Log first few option texts
            for opt_idx in range(min(count, 5)):
                opt = all_opts.nth(opt_idx)
                try:
                    vis = await opt.is_visible()
                    text = (await opt.text_content() or "").strip()[:60]
                    logger.info("    opt[%d] visible=%s text='%s'", opt_idx, vis, text)
                except Exception:
                    pass

            # Click first visible option
            clicked = False
            for opt_idx in range(min(count, 10)):
                opt = all_opts.nth(opt_idx)
                try:
                    if not await opt.is_visible():
                        continue
                    text = (await opt.text_content() or "").strip()
                    if "checked" in text.lower():
                        continue
                    await opt.click(timeout=3000)
                    logger.info("  CLICKED option[%d]: '%s'", opt_idx, text[:60])
                    clicked = True
                    break
                except Exception as e:
                    logger.warning("  Click option[%d] failed: %s", opt_idx, e)
            if not clicked:
                logger.warning("  NO option clicked for skill '%s'", skill)
        else:
            logger.warning("  NO visible listbox options found for skill '%s'", skill)
            # Check if we're typing into the wrong input
            active = await page.evaluate("""() => {
                const ae = document.activeElement;
                return ae ? { tag: ae.tagName, id: ae.id, ariaLabel: ae.getAttribute('aria-label'), value: ae.value } : null;
            }""")
            logger.info("  Active element during type: %s", json.dumps(active))

        await asyncio.sleep(0.5)
        await dump_skills_dom(page, f"SKILL '{skill}': AFTER CLICK ATTEMPT")

    # Final DOM dump
    logger.info("=" * 60)
    await dump_skills_dom(page, "FINAL STATE: AFTER ALL SKILLS ATTEMPTED")
    logger.info("=" * 60)

    # Keep browser open for inspection
    input("Press Enter to close browser...")
    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
