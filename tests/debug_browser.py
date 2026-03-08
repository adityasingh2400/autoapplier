"""Debug: navigate to Flex job, interactively inspect form controls."""
import asyncio
from playwright.async_api import async_playwright

JOB_URL = "https://flextronics.wd1.myworkdayjobs.com/Careers/job/USA-UT-Salt-Lake-City/IT-ERP-Intern---Summer-2026_WD213943?utm_source=Simplify&ref=Simplify"

SNAP_N = 0

async def snapshot(page, label):
    global SNAP_N
    SNAP_N += 1
    path = f"/tmp/debug_{SNAP_N:02d}_{label}.png"
    await page.screenshot(path=path, full_page=True)
    print(f"\n{'='*60}")
    print(f"SNAPSHOT {SNAP_N}: {label}  ->  {path}")
    print(f"URL: {page.url}")
    print(f"{'='*60}")

    elements = await page.evaluate(r"""
    () => {
        const vis = (el) => el && el.offsetParent !== null;
        const results = [];
        document.querySelectorAll('input, select, textarea, button, [role="checkbox"], [role="radio"], [role="combobox"], [role="radiogroup"], [role="listbox"], [role="option"]').forEach(el => {
            if (!vis(el)) return;
            const labelEl = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
            const labelText = labelEl?.textContent?.trim()?.slice(0, 80) || '';
            const ariaLabel = el.getAttribute('aria-label') || '';
            const aid = el.getAttribute('data-automation-id') || '';
            results.push({
                tag: el.tagName.toLowerCase(),
                type: el.type || el.getAttribute('role') || '',
                id: el.id || '',
                aid: aid,
                value: el.value?.slice(0, 40) || '',
                text: el.textContent?.trim()?.slice(0, 80) || '',
                label: labelText,
                ariaLabel: ariaLabel.slice(0, 80),
                checked: String(el.checked ?? el.getAttribute('aria-checked') ?? ''),
            });
        });
        return results;
    }
    """)

    for i, el in enumerate(elements):
        parts = [f"  [{i:3d}] <{el['tag']}"]
        if el['type']: parts.append(f" type={el['type']}")
        if el['id']: parts.append(f" id={el['id']}")
        if el['aid']: parts.append(f" aid={el['aid']}")
        parts.append(">")
        if el['text']: parts.append(f" text='{el['text'][:60]}'")
        if el['value']: parts.append(f" val='{el['value']}'")
        if el['label']: parts.append(f" label='{el['label']}'")
        if el['ariaLabel']: parts.append(f" aria='{el['ariaLabel']}'")
        if el['checked'] and el['checked'] not in ('', 'null', 'undefined'):
            parts.append(f" checked={el['checked']}")
        print("".join(parts))
    print(f"  --- {len(elements)} total ---")


async def deep_inspect(page, label, selector):
    """Dump detailed DOM tree around a specific element."""
    print(f"\n{'~'*60}")
    print(f"DEEP INSPECT: {label}")
    print(f"Selector: {selector}")
    print(f"{'~'*60}")
    info = await page.evaluate(r"""
    (sel) => {
        const el = document.querySelector(sel);
        if (!el) return { found: false };

        let container = el;
        // Walk up a few levels to get meaningful context
        for (let i = 0; i < 3; i++) {
            if (container.parentElement) container = container.parentElement;
        }

        const children = [];
        container.querySelectorAll('input, select, button, [role="radio"], [role="checkbox"], [role="combobox"], [role="listbox"], [role="option"], span[data-automation-id], label, [data-automation-id]').forEach(child => {
            children.push({
                tag: child.tagName.toLowerCase(),
                type: child.type || child.getAttribute('role') || '',
                id: child.id || '',
                aid: child.getAttribute('data-automation-id') || '',
                ariaLabel: (child.getAttribute('aria-label') || '').slice(0, 80),
                ariaChecked: child.getAttribute('aria-checked') || '',
                text: child.textContent?.trim()?.slice(0, 60) || '',
                value: child.value?.slice?.(0, 40) || '',
                checked: String(child.checked ?? ''),
            });
        });

        return {
            found: true,
            container_tag: container.tagName.toLowerCase(),
            container_aid: container.getAttribute('data-automation-id') || '',
            container_role: container.getAttribute('role') || '',
            container_outerHTML_prefix: container.outerHTML.slice(0, 600),
            children: children.slice(0, 60),
        };
    }
    """, selector)

    if not info.get('found'):
        print(f"  NOT FOUND with selector: {selector}")
        return

    print(f"  Container: <{info['container_tag']} aid='{info['container_aid']}' role='{info['container_role']}'>")
    print(f"  HTML prefix:\n    {info['container_outerHTML_prefix'][:500]}")
    print(f"\n  Children ({len(info['children'])}):")
    for i, c in enumerate(info['children']):
        parts = [f"    [{i}] <{c['tag']}"]
        if c['type']: parts.append(f" type={c['type']}")
        if c['id']: parts.append(f" id={c['id']}")
        if c['aid']: parts.append(f" aid={c['aid']}")
        parts.append(">")
        if c['text']: parts.append(f" text='{c['text'][:50]}'")
        if c['ariaLabel']: parts.append(f" aria='{c['ariaLabel']}'")
        if c['ariaChecked']: parts.append(f" aria-checked={c['ariaChecked']}")
        if c['value']: parts.append(f" val='{c['value']}'")
        if c['checked'] and c['checked'] not in ('', 'undefined'):
            parts.append(f" checked={c['checked']}")
        print("".join(parts))


def wait(prompt="Press Enter to continue..."):
    input(f"\n>>> {prompt}")


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False, slow_mo=100)
    ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await ctx.new_page()

    print("Step 1: Navigate to job page")
    await page.goto(JOB_URL, wait_until="domcontentloaded")
    await asyncio.sleep(3)

    # Accept cookies
    try:
        accept = page.locator('[data-automation-id="legalNoticeAcceptButton"]')
        if await accept.count() > 0 and await accept.is_visible():
            await accept.click(timeout=2000)
            print("  Accepted cookies")
            await asyncio.sleep(1)
    except:
        pass

    wait("Page loaded. Press Enter to click Apply...")

    # Click Apply
    print("\nStep 2: Click Apply")
    try:
        await page.locator("button:has-text('Apply'), a:has-text('Apply')").first.click(timeout=5000)
        print("  Clicked Apply")
        await asyncio.sleep(2)
    except Exception as e:
        print(f"  Error: {e}")

    # Click Apply Manually
    print("Step 3: Click Apply Manually")
    try:
        dialog = page.locator('[data-automation-id*="applyManually"]')
        if await dialog.count() > 0:
            await dialog.first.click(timeout=3000)
            print("  Clicked Apply Manually")
        await asyncio.sleep(3)
    except Exception as e:
        print(f"  No dialog or error: {e}")

    wait("Form should be loaded now. Press Enter to take snapshot + deep inspect...")

    # ═══════════════════════════════════════════════════════════
    # PHASE A: Initial form state
    # ═══════════════════════════════════════════════════════════
    await snapshot(page, "form_initial")

    print("\n\n" + "="*60)
    print("DEEP INSPECTING: How Did You Hear + Previously Worked")
    print("="*60)

    await deep_inspect(page, "How Did You Hear About Us?", "[data-automation-id='formField-source']")
    await deep_inspect(page, "Previously Worked Radio", "[data-automation-id='formField-previousWorker']")

    # Broader search for these if data-automation-id selectors miss
    broad = await page.evaluate(r"""
    () => {
        const results = [];
        document.querySelectorAll('[data-automation-id]').forEach(el => {
            if (el.offsetParent === null) return;
            const aid = el.getAttribute('data-automation-id');
            const text = (el.textContent || '').trim().toLowerCase();
            if (text.includes('how did you hear') || text.includes('previously worked') ||
                aid.includes('source') || aid.includes('previousWorker') || aid.includes('previous')) {
                results.push({
                    aid, tag: el.tagName.toLowerCase(),
                    text: el.textContent?.trim()?.slice(0, 80) || '',
                    role: el.getAttribute('role') || '',
                });
            }
        });
        return results;
    }
    """)
    if broad:
        print(f"\n  Broad search results ({len(broad)}):")
        for m in broad:
            print(f"    <{m['tag']} aid='{m['aid']}' role='{m['role']}'> '{m['text']}'")
    else:
        print("\n  Broad search: NO matches. Trying to find ALL data-automation-id values...")
        all_aids = await page.evaluate(r"""
        () => {
            const aids = new Set();
            document.querySelectorAll('[data-automation-id]').forEach(el => {
                if (el.offsetParent !== null) aids.add(el.getAttribute('data-automation-id'));
            });
            return [...aids].sort();
        }
        """)
        print(f"  All visible data-automation-ids ({len(all_aids)}):")
        for a in all_aids:
            print(f"    {a}")

    # ═══════════════════════════════════════════════════════════
    # PHASE B: Click "How Did You Hear" and inspect the dropdown
    # ═══════════════════════════════════════════════════════════
    wait("Press Enter to click 'How Did You Hear' promptIcon and inspect dropdown...")

    print("\nStep 4: Clicking 'How Did You Hear' promptIcon")
    clicked_prompt = False
    try:
        icon = page.locator("[data-automation-id='formField-source'] [data-automation-id='promptIcon']").first
        if await icon.count() > 0:
            await icon.click(force=True, timeout=3000)
            print("  Clicked promptIcon via formField-source")
            clicked_prompt = True
        else:
            print("  promptIcon not found inside formField-source, trying standalone...")
            icon2 = page.locator("[data-automation-id='promptIcon']").first
            if await icon2.count() > 0:
                await icon2.click(force=True, timeout=3000)
                print("  Clicked standalone promptIcon")
                clicked_prompt = True
    except Exception as e:
        print(f"  Error clicking promptIcon: {e}")

    if clicked_prompt:
        await asyncio.sleep(2)
        await snapshot(page, "how_hear_dropdown_open")

        # Dump all visible options
        options = await page.evaluate(r"""
        () => {
            const results = [];
            document.querySelectorAll('[role="option"], [role="listbox"] li, [data-automation-id*="option"], [data-automation-id*="promptOption"]').forEach(el => {
                if (el.offsetParent === null) return;
                results.push({
                    tag: el.tagName.toLowerCase(),
                    role: el.getAttribute('role') || '',
                    text: el.textContent?.trim()?.slice(0, 80) || '',
                    aid: el.getAttribute('data-automation-id') || '',
                    id: el.id || '',
                });
            });
            return results;
        }
        """)
        print(f"\n  Dropdown options ({len(options)}):")
        for i, o in enumerate(options):
            print(f"    [{i}] <{o['tag']} role={o['role']} aid={o['aid']}> '{o['text']}'")

        wait("Dropdown is open. Inspect it, then press Enter to close and continue...")
        # Press Escape to close dropdown
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

    # ═══════════════════════════════════════════════════════════
    # PHASE C: Click "Previously Worked" No and inspect after
    # ═══════════════════════════════════════════════════════════
    wait("Press Enter to click 'Previously Worked' No radio and inspect after state...")

    print("\nStep 5: Clicking 'Previously Worked' No")

    # Try multiple strategies and report which works
    strategies = [
        ("formField-previousWorker + role=radio No (force=True)",
         lambda: page.locator('[data-automation-id="formField-previousWorker"]').locator('[role="radio"]').filter(has_text="No").first),
        ("get_by_role radiogroup + radio No",
         lambda: page.get_by_role("radiogroup", name="previously worked").get_by_role("radio", name="No").first if True else None),
    ]

    for name, get_loc in strategies:
        try:
            loc = get_loc()
            if loc and await loc.count() > 0:
                print(f"  Trying: {name}")
                await loc.click(force=True, timeout=3000)
                await asyncio.sleep(1)
                print(f"  SUCCESS: {name}")
                await snapshot(page, "after_previously_worked_no")
                await deep_inspect(page, "Previously Worked AFTER click", "[data-automation-id='formField-previousWorker']")
                break
            else:
                print(f"  SKIP (not found): {name}")
        except Exception as e:
            print(f"  FAIL: {name} -> {e}")

    # ═══════════════════════════════════════════════════════════
    # PHASE D: Keep browser open for manual inspection
    # ═══════════════════════════════════════════════════════════
    wait("All done. Press Enter to keep browser open for 300 seconds, or Ctrl+C to exit...")
    print("\nBrowser stays open for 300 seconds for manual inspection.")
    print("Screenshots saved to /tmp/debug_*.png")
    await asyncio.sleep(300)

    await browser.close()
    await pw.stop()

asyncio.run(main())
