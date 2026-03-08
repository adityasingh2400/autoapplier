#!/usr/bin/env python3
"""
Isolated test for the Application Questions page (page 3) on Workday.

Automates pages 1-2 using the real handler, then on page 3 dumps the
full DOM of every form control — focusing on checkbox groups, date pickers,
text inputs, dropdowns, etc.

Usage:
  python3 test_app_questions.py
"""

import asyncio
import json
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
)
logger = logging.getLogger("test_appq")

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

    # ── Fill page 1 using the real handler's pre-fill ──
    from openclaw.ats.workday import WorkdayHandler
    handler = WorkdayHandler(ats_name="workday")

    logger.info("Filling 'How Did You Hear'...")
    await handler._fill_how_did_you_hear(page)

    logger.info("Clicking 'Previously Worked' -> No...")
    await handler._click_previously_worked_no(page)

    # Fill required text fields
    logger.info("Filling page 1 text fields...")
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

    # Fill state dropdown
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

    await asyncio.sleep(0.5)

    # ── Click Next to page 2 ──
    logger.info("Going to page 2...")
    await page.locator("button:has-text('Next')").first.click(timeout=5000)
    await asyncio.sleep(3.0)

    # Wait for page 2 to load
    for _ in range(15):
        await asyncio.sleep(1.0)
        has_edu = await page.evaluate("""() => {
            const el = document.querySelector('[data-automation-id*="workExperience"], [data-automation-id*="education"]');
            return el ? el.getBoundingClientRect().width > 0 : false;
        }""")
        if has_edu:
            logger.info("  Page 2 loaded")
            break

    # ── Upload resume on page 2 ──
    logger.info("Uploading resume...")
    import os
    resume_path = os.path.join(os.path.dirname(__file__), "real_memory", "resume.pdf")
    try:
        file_input = page.locator("input[type='file']").first
        if await file_input.count() > 0:
            await file_input.set_input_files(resume_path)
            logger.info("  Resume uploaded via file input")
            await asyncio.sleep(2.0)
        else:
            logger.info("  No file input found, trying data-automation-id")
            # Workday sometimes hides the input — try clicking the upload area
            upload_btn = page.locator("[data-automation-id='file-upload-drop-zone'], [data-automation-id='Browse']").first
            if await upload_btn.is_visible(timeout=3000):
                # Use file chooser pattern
                async with page.expect_file_chooser() as fc_info:
                    await upload_btn.click(timeout=3000)
                file_chooser = await fc_info.value
                await file_chooser.set_files(resume_path)
                logger.info("  Resume uploaded via file chooser")
                await asyncio.sleep(2.0)
    except Exception as e:
        logger.warning("  Resume upload failed: %s", e)

    # ── Fill page 2 using the real handler (work exp + education + skills) ──
    # Build a context object using the real profile loader
    from pathlib import Path
    from openclaw.profile import load_user_profile

    real_profile = load_user_profile(Path("real_memory"))

    class FakeQuestionAnswerer:
        async def answer(self, *args, **kwargs):
            return None
        async def cover_letter(self, *args, **kwargs):
            return ""
        async def tailor_resume(self, *args, **kwargs):
            return ""

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

    logger.info("Filling Skills...")
    await handler._fill_skills(page, ctx, resume_data)

    logger.info("Filling LinkedIn...")
    await handler._fill_social_urls(page, ctx, resume_data)

    await asyncio.sleep(1.0)

    # ── Click Next to page 3 (Application Questions) ──
    logger.info("Going to page 3 (Application Questions)...")
    await page.locator("button:has-text('Next')").first.click(timeout=5000)
    # Give Workday time to tear down page 2 and render page 3
    await asyncio.sleep(8.0)

    # Wait for page 3 to load
    for _ in range(15):
        await asyncio.sleep(1.0)
        count = await page.evaluate("""() => {
            const els = document.querySelectorAll('input:not([type="hidden"]), select, textarea, [role="combobox"], [role="radio"], [role="checkbox"]');
            let n = 0;
            for (const el of els) { const r = el.getBoundingClientRect(); if (r.width >= 2 && r.height >= 2) n++; }
            return n;
        }""")
        if count >= 3:
            logger.info("  Page 3 loaded: %d controls", count)
            break

    # ══════════════════════════════════════════════════════════════════
    # DOM DUMP: Complete inventory of all form elements on this page
    # ══════════════════════════════════════════════════════════════════
    logger.info("=" * 70)
    logger.info("DOM DUMP: Application Questions Page (Page 3)")
    logger.info("=" * 70)

    full_dump = await page.evaluate("""() => {
        function labelFor(el) {
            // Try aria-label, aria-labelledby, associated label, closest label, parent text
            const al = el.getAttribute('aria-label');
            if (al) return al.trim();
            const alb = el.getAttribute('aria-labelledby');
            if (alb) {
                const ref = document.getElementById(alb);
                if (ref) return (ref.innerText || '').trim();
            }
            if (el.id) {
                const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (lbl) return (lbl.innerText || '').trim();
            }
            const closestLabel = el.closest('label');
            if (closestLabel) return (closestLabel.innerText || '').trim();
            return '';
        }

        function parentContext(el, levels) {
            // Walk up N levels and collect text from heading/label/legend/strong siblings
            let ctx = [];
            let p = el.parentElement;
            for (let i = 0; i < (levels || 5); i++) {
                if (!p) break;
                // Check for heading, legend, label, or data-automation-id question
                for (const tag of ['legend', 'h2', 'h3', 'h4', 'label', 'strong', '[data-automation-id*="formLabel"]']) {
                    p.querySelectorAll(':scope > ' + tag).forEach(h => {
                        const t = (h.innerText || '').trim();
                        if (t && t.length < 200 && !ctx.includes(t)) ctx.push(t);
                    });
                }
                // Check the parent's own data-automation-id
                const aid = p.getAttribute('data-automation-id') || '';
                if (aid && aid.includes('formField')) {
                    ctx.push('PARENT_AID:' + aid);
                }
                p = p.parentElement;
            }
            return ctx;
        }

        const results = [];

        // 1. All inputs (not hidden)
        document.querySelectorAll('input:not([type="hidden"])').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return;
            results.push({
                category: 'input',
                type: el.type || 'text',
                tag: el.tagName,
                id: el.id || '',
                name: el.name || '',
                value: (el.value || '').slice(0, 50),
                checked: el.checked || false,
                disabled: el.disabled || false,
                required: el.required || el.getAttribute('aria-required') === 'true',
                label: labelFor(el),
                aid: el.getAttribute('data-automation-id') || '',
                role: el.getAttribute('role') || '',
                placeholder: el.placeholder || '',
                parentContext: parentContext(el, 8),
                selector: el.id ? '#' + CSS.escape(el.id) : '',
                w: Math.round(r.width),
                h: Math.round(r.height),
            });
        });

        // 2. All selects
        document.querySelectorAll('select').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return;
            const opts = Array.from(el.options).map(o => ({ text: o.text, value: o.value, selected: o.selected }));
            results.push({
                category: 'select',
                tag: el.tagName,
                id: el.id || '',
                name: el.name || '',
                label: labelFor(el),
                aid: el.getAttribute('data-automation-id') || '',
                options: opts,
                parentContext: parentContext(el, 8),
                w: Math.round(r.width),
                h: Math.round(r.height),
            });
        });

        // 3. All textareas
        document.querySelectorAll('textarea').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return;
            results.push({
                category: 'textarea',
                tag: el.tagName,
                id: el.id || '',
                name: el.name || '',
                value: (el.value || '').slice(0, 100),
                label: labelFor(el),
                aid: el.getAttribute('data-automation-id') || '',
                parentContext: parentContext(el, 8),
                w: Math.round(r.width),
                h: Math.round(r.height),
            });
        });

        // 4. ARIA comboboxes and listboxes
        document.querySelectorAll('[role="combobox"], [aria-haspopup="listbox"]').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return;
            results.push({
                category: 'aria_combobox',
                tag: el.tagName,
                id: el.id || '',
                role: el.getAttribute('role') || '',
                ariaExpanded: el.getAttribute('aria-expanded') || '',
                ariaLabel: el.getAttribute('aria-label') || '',
                text: (el.innerText || '').trim().slice(0, 100),
                aid: el.getAttribute('data-automation-id') || '',
                parentContext: parentContext(el, 8),
                w: Math.round(r.width),
                h: Math.round(r.height),
            });
        });

        // 5. ARIA radio groups
        document.querySelectorAll('[role="radiogroup"], [role="radio"]').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return;
            results.push({
                category: 'aria_radio',
                tag: el.tagName,
                id: el.id || '',
                role: el.getAttribute('role') || '',
                ariaChecked: el.getAttribute('aria-checked') || '',
                ariaLabel: el.getAttribute('aria-label') || '',
                text: (el.innerText || '').trim().slice(0, 150),
                aid: el.getAttribute('data-automation-id') || '',
                parentContext: parentContext(el, 8),
                w: Math.round(r.width),
                h: Math.round(r.height),
            });
        });

        // 6. All buttons that look like dropdowns
        document.querySelectorAll('button[aria-haspopup], button[aria-expanded]').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return;
            results.push({
                category: 'dropdown_button',
                tag: el.tagName,
                id: el.id || '',
                ariaExpanded: el.getAttribute('aria-expanded') || '',
                ariaHaspopup: el.getAttribute('aria-haspopup') || '',
                text: (el.innerText || '').trim().slice(0, 100),
                aid: el.getAttribute('data-automation-id') || '',
                label: labelFor(el),
                parentContext: parentContext(el, 8),
                w: Math.round(r.width),
                h: Math.round(r.height),
            });
        });

        // 7. All data-automation-id="formField*" containers (Workday question groups)
        document.querySelectorAll('[data-automation-id^="formField"]').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return;
            // Get all child labels and inputs
            const labels = Array.from(el.querySelectorAll('label, legend, h3, h4, [data-automation-id*="formLabel"]'))
                .map(l => (l.innerText || '').trim()).filter(t => t);
            const inputs = Array.from(el.querySelectorAll('input, select, textarea, [role="combobox"], [role="radio"], [role="checkbox"]'))
                .map(inp => ({
                    tag: inp.tagName,
                    type: inp.type || '',
                    id: inp.id || '',
                    name: inp.name || '',
                    role: inp.getAttribute('role') || '',
                    value: (inp.value || '').slice(0, 50),
                    checked: inp.checked || false,
                    label: labelFor(inp),
                }));
            results.push({
                category: 'formField_container',
                aid: el.getAttribute('data-automation-id') || '',
                id: el.id || '',
                tag: el.tagName,
                labels: labels,
                inputs: inputs,
                text: (el.innerText || '').trim().slice(0, 300),
                w: Math.round(r.width),
                h: Math.round(r.height),
            });
        });

        // 8. Date pickers (spinbutton, date inputs)
        document.querySelectorAll('[role="spinbutton"], input[type="date"], [data-automation-id*="date" i]').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return;
            results.push({
                category: 'date_element',
                tag: el.tagName,
                id: el.id || '',
                type: el.type || '',
                role: el.getAttribute('role') || '',
                aid: el.getAttribute('data-automation-id') || '',
                value: (el.value || el.getAttribute('aria-valuenow') || '').slice(0, 50),
                label: labelFor(el),
                ariaLabel: el.getAttribute('aria-label') || '',
                parentContext: parentContext(el, 8),
                w: Math.round(r.width),
                h: Math.round(r.height),
            });
        });

        return results;
    }""")

    # Print categorized results
    categories = {}
    for item in full_dump:
        cat = item.get('category', 'unknown')
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(item)

    for cat, items in categories.items():
        logger.info("")
        logger.info("── %s (%d items) ──", cat.upper(), len(items))
        for i, item in enumerate(items):
            logger.info("  [%d] %s", i, json.dumps(item, indent=4))

    # Also dump the page's visible text structure for context
    logger.info("")
    logger.info("── PAGE TEXT STRUCTURE ──")
    page_structure = await page.evaluate("""() => {
        const sections = [];
        // Find all question-like containers
        document.querySelectorAll('[data-automation-id^="formField"], fieldset, [role="group"]').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return;
            const text = (el.innerText || '').trim();
            if (text.length > 0 && text.length < 500) {
                sections.push({
                    aid: el.getAttribute('data-automation-id') || '',
                    role: el.getAttribute('role') || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    tag: el.tagName,
                    text: text.slice(0, 300),
                });
            }
        });
        return sections;
    }""")

    for i, section in enumerate(page_structure):
        logger.info("  [%d] %s", i, json.dumps(section, indent=4))

    logger.info("=" * 70)
    logger.info("DOM DUMP COMPLETE")
    logger.info("=" * 70)

    # ══════════════════════════════════════════════════════════════════
    # FILL TEST: Try filling the application questions using snapshot-and-fill
    # ══════════════════════════════════════════════════════════════════
    logger.info("")
    logger.info("=" * 70)
    logger.info("FILL TEST: Running snapshot-and-fill on Application Questions page")
    logger.info("=" * 70)

    # Run the snapshot-and-fill to see what it detects and fills
    try:
        fill_result = await handler._snapshot_and_fill_all_fields(page, ctx=ctx)
        logger.info("Snapshot-and-fill result: %s fields filled, %s custom Qs", fill_result[0], fill_result[1])
    except Exception as e:
        logger.error("Snapshot-and-fill failed: %s", e, exc_info=True)

    # Post-fill DOM dump: check which checkboxes are now checked
    post_fill = await page.evaluate("""() => {
        const results = [];
        document.querySelectorAll("input[type='checkbox']").forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return;
            results.push({
                id: el.id || '',
                checked: el.checked,
                label: el.closest('label')?.innerText?.trim() || '',
            });
        });
        // Also check date spinbuttons
        document.querySelectorAll("input[role='spinbutton']").forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return;
            results.push({
                id: el.id || '',
                type: 'spinbutton',
                value: el.value || '',
                ariaLabel: el.getAttribute('aria-label') || '',
            });
        });
        return results;
    }""")
    logger.info("")
    logger.info("── POST-FILL STATE ──")
    for item in post_fill:
        logger.info("  %s", json.dumps(item))

    # Pause for inspection
    await _pause(page, browser, pw)


async def _pause(page, browser, pw):
    logger.info("Browser is open — inspect the result. Press Ctrl+C to close.")
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
