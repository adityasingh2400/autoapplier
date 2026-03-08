from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .base import BaseATSHandler
from openclaw.utils import smart_click, maybe_await

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WorkdayHandler(BaseATSHandler):
    ats_name: str = "workday"
    apply_prompts: list[str] = field(
        default_factory=lambda: ["Click Apply in this Workday job posting"]
    )
    apply_selectors: list[str] = field(
        default_factory=lambda: [
            "button:has-text('Apply')",
            "a:has-text('Apply')",
            "button:has-text('Apply Manually')",
        ]
    )
    submit_prompts: list[str] = field(
        default_factory=lambda: ["Submit this Workday application"]
    )
    submit_selectors: list[str] = field(
        default_factory=lambda: [
            "button:has-text('Submit')",
            "button:has-text('Review and Submit')",
            "button[type='submit']",
        ]
    )
    next_selectors: list[str] = field(
        default_factory=lambda: [
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button:has-text('Review')",
            "button:has-text('Save and Continue')",
        ]
    )
    resume_selectors: list[str] = field(
        default_factory=lambda: [
            "input[type='file'][name*='resume']",
            "input[type='file'][id*='resume']",
            "input[type='file']",
        ]
    )
    field_prompts: dict[str, str] = field(
        default_factory=lambda: {
            "full_name": "Full Name",
            "first_name": "First Name",
            "last_name": "Last Name",
            "email": "Email",
            "phone": "Phone",
            "linkedin": "LinkedIn URL",
            "github": "GitHub URL",
            "school": "School",
            "degree": "Degree",
            "gpa": "GPA",
            "graduation": "Graduation Date",
        }
    )
    field_selectors: dict[str, list[str]] = field(
        default_factory=lambda: {
            "full_name": ["input[name*='fullName']", "input[id*='fullName']", "input[name='name']"],
            "first_name": ["input[name*='firstName']", "input[id*='firstName']"],
            "last_name": ["input[name*='lastName']", "input[id*='lastName']"],
            "email": ["input[type='email']", "input[name*='email']"],
            "phone": ["input[type='tel']", "input[name*='phone']"],
            "linkedin": ["input[name*='linkedin']", "input[id*='linkedin']"],
            "github": ["input[name*='github']", "input[id*='github']"],
            "school": ["input[name*='school']", "input[name*='institution']"],
            "degree": ["input[name*='degree']"],
            "gpa": ["input[name*='gpa']"],
            "graduation": ["input[name*='graduation']", "input[name*='grad']"],
        }
    )

    # Extend the base protocol table for Workday-specific control types.
    # New protocols can be added here as they're discovered.
    _FILL_PROTOCOLS: ClassVar[dict[str, str]] = {
        **BaseATSHandler._FILL_PROTOCOLS,
        # Future Workday-specific protocols go here, e.g.:
        # "workday_date_spinbutton": "_proto_workday_date_spinbutton",
    }

    async def apply(self, page: Any, ctx: Any) -> dict:
        """Override to bump max_form_pages for Workday's 6-step flow."""
        if hasattr(ctx, "max_form_pages") and ctx.max_form_pages < 6:
            ctx.max_form_pages = 6
            logger.debug("[Workday] Bumped max_form_pages to 6")
        return await super(WorkdayHandler, self).apply(page, ctx)

    # ------------------------------------------------------------------
    # Unified "page ready" gate — call before ANY form interaction
    # ------------------------------------------------------------------
    async def _wait_for_page_ready(self, page: Any, *, timeout_sec: float = 10.0) -> bool:
        """
        Poll until the Workday page is stable and ready for interaction.

        Checks:
          1. No loading spinners / overlays visible
          2. At least 3 form controls visible (rules out just the language selector)
          3. DOM has stopped changing (two consecutive snapshots match)

        Returns True if the page is ready, False if we timed out.
        """
        import time
        deadline = time.monotonic() + timeout_sec
        locator_fn = getattr(page, "locator", None)
        evaluate_fn = getattr(page, "evaluate", None)
        if not locator_fn or not evaluate_fn:
            await asyncio.sleep(3.0)  # blind fallback
            return True

        MIN_CONTROLS = 3  # A real Workday form page has many more than 1-2 controls
        prev_control_count = -1
        stable_ticks = 0  # how many consecutive polls showed the same control count

        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)

            # Check 1: loading spinners gone?
            try:
                loading = await maybe_await(evaluate_fn("""
                    () => {
                        const spinners = document.querySelectorAll(
                            '[data-automation-id="wd-Loading"], ' +
                            '[data-automation-id="LoadingPanel"], ' +
                            '.wd-LoadingPanel, ' +
                            '[role="progressbar"], ' +
                            '[aria-busy="true"]'
                        );
                        for (const s of spinners) {
                            const rect = s.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) return true;
                        }
                        return false;
                    }
                """))
                if loading:
                    stable_ticks = 0
                    prev_control_count = -1
                    continue
            except Exception:
                pass

            # Check 2: count visible form controls
            try:
                control_count = await maybe_await(evaluate_fn("""
                    () => {
                        const els = document.querySelectorAll(
                            'input:not([type="hidden"]), select, textarea, ' +
                            '[role="combobox"], [role="radio"], [role="checkbox"]'
                        );
                        let n = 0;
                        for (const el of els) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width >= 2 && rect.height >= 2) n++;
                        }
                        return n;
                    }
                """))
                control_count = int(control_count or 0)
            except Exception:
                control_count = 0

            # Not enough controls yet — page is still loading
            if control_count < MIN_CONTROLS:
                stable_ticks = 0
                prev_control_count = control_count
                continue

            # Check 3: DOM stability — same control count for 2 consecutive polls
            if control_count == prev_control_count:
                stable_ticks += 1
            else:
                stable_ticks = 0
            prev_control_count = control_count

            if stable_ticks >= 2:
                elapsed = timeout_sec - (deadline - time.monotonic())
                logger.debug("[Workday] Page ready: %d controls visible (%.1fs)", control_count, elapsed)
                return True

        elapsed = timeout_sec - (deadline - time.monotonic())
        logger.warning("[Workday] Page ready timeout after %.1fs (%d controls)", timeout_sec, prev_control_count)
        return False

    async def _open_apply_form(self, page: Any) -> None:
        await super(WorkdayHandler, self)._open_apply_form(page)

        # Workday apps typically have 6 steps; ensure we iterate enough pages.
        # (The base class default is 3.)

        # Workday sometimes shows a modal with application entry choices.
        # Wait briefly for it to appear, then try to click through.
        import asyncio as _aio
        await _aio.sleep(1.5)  # Give modal time to render if it exists

        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return

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
                    loc = locator_fn(sel).first
                    if await maybe_await(loc.is_visible()):
                        await maybe_await(loc.click(timeout=2000))
                        clicked = True
                        logger.debug("[Workday] Clicked dialog button: %s", sel[:50])
                        await _aio.sleep(1.0)
                        break
                except Exception:
                    continue
            if not clicked:
                break

    async def _page2_controls_present(self, page: Any) -> bool:
        """True if page 2 elements (Work Experience, Education, Skills, Websites) are visible."""
        evaluate_fn = getattr(page, "evaluate", None)
        if not evaluate_fn:
            return False
        try:
            ok: bool = await maybe_await(evaluate_fn("""
                () => {
                    const el = document.querySelector(
                        '[data-automation-id*="workExperience"], ' +
                        '[data-automation-id*="education"], ' +
                        '#Websites-section, [aria-labelledby="Websites-section"], ' +
                        '[data-automation-id*="skills"]'
                    );
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width >= 2 && rect.height >= 2;
                }
            """))
            return bool(ok)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Pre-fill dispatcher (content-based, no heading polling)
    # ------------------------------------------------------------------
    async def _pre_fill_special_controls(self, page: Any, ctx: Any) -> None:
        """Handle Workday-specific widgets before snapshot-fill runs."""
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return

        # 1. Wait for the page to be fully loaded and stable.
        await self._wait_for_page_ready(page)

        # 2. "How Did You Hear About Us?" — do this first since it clicks around.
        if "how_did_you_hear" not in self._pages_filled:
            try:
                source_field = locator_fn("[data-automation-id='formField-source']").first
                if await maybe_await(source_field.is_visible(timeout=1500)):
                    self._pages_filled.add("how_did_you_hear")
                    await self._fill_how_did_you_hear(page)
            except Exception:
                pass

        # 3. "Previously Worked" → No (every page, short-circuits if not visible).
        await self._click_previously_worked_no(page)

        # 4. Page 2 structured data (work experience, education, skills, etc.)
        if "page2" not in self._pages_filled and await self._page2_controls_present(page):
            self._pages_filled.add("page2")
            logger.info("[Workday] Filling page 2 (My Experience) controls...")
            await self._pre_fill_page2_my_experience(page, ctx)

    async def _click_previously_worked_no(self, page: Any) -> bool:
        """
        Explicitly click the "No" radio for any "Have you previously worked for [company]?"
        question. The answer is ALWAYS "No" across all job boards.
        """
        evaluate_fn = getattr(page, "evaluate", None)
        if not evaluate_fn:
            return False

        # Strategy 0 (fastest): Click the <label> for the "No" radio inside the correct container.
        # Clicking the label triggers native form association (for="...") which fires React events.
        # force=True on the hidden <input> does NOT work — React's synthetic events don't fire.
        try:
            container = page.locator('[data-automation-id="formField-candidateIsPreviousWorker"]')
            if await maybe_await(container.count()) > 0:
                no_label = container.locator('label:has-text("No")').first
                if await maybe_await(no_label.is_visible(timeout=1500)):
                    await maybe_await(no_label.click(timeout=2000))
                    await asyncio.sleep(0.3)
                    logger.info("[Workday] Clicked 'No' for 'previously worked' (via label click)")
                    return True
        except Exception:
            pass

        # Strategy 1: Use Playwright get_by_role for ARIA radiogroups
        try:
            rg = page.get_by_role("radiogroup", name=re.compile(r"previously\s+worked", re.I)).first
            if await maybe_await(rg.is_visible(timeout=1500)):
                no_radio = rg.get_by_role("radio", name=re.compile(r"^no\s*$", re.I)).first
                if await maybe_await(no_radio.is_visible(timeout=1000)):
                    handle = await maybe_await(no_radio.element_handle(timeout=1500))
                    if handle:
                        # Click the label instead of the radio input for React compatibility
                        await maybe_await(page.evaluate(
                            """(el) => {
                                // If this is a native input, click its label instead
                                if (el.tagName === 'INPUT' && el.type === 'radio') {
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
                                        return;
                                    }
                                }
                                // Fallback for ARIA radios or if no label found
                                el.scrollIntoView({block: 'center'});
                                el.click();
                            }""",
                            handle,
                        ))
                        await asyncio.sleep(0.3)
                        logger.info("[Workday] Clicked 'No' for 'previously worked' (via ARIA radiogroup)")
                        return True
        except Exception:
            pass

        # Strategy 2: JS-based — find native radio inputs near "previously worked" text
        # Works when there's no role="radiogroup" wrapper (common in Workday)
        try:
            clicked: bool = await maybe_await(evaluate_fn("""
                () => {
                    // Find any label/legend/text that mentions "previously worked"
                    const allText = document.body.querySelectorAll('label, legend, p, span, div');
                    let container = null;
                    for (const el of allText) {
                        const t = (el.innerText || '').toLowerCase();
                        if (t.includes('previously worked') && t.length < 500) {
                            container = el.closest('fieldset, [role="group"], [role="radiogroup"], div');
                            if (container) break;
                        }
                    }
                    if (!container) return false;

                    // Find all radio inputs in/near this container
                    const radios = container.querySelectorAll('input[type="radio"]');
                    if (!radios.length) {
                        // Also check for ARIA radios
                        const ariaRadios = container.querySelectorAll('[role="radio"]');
                        for (const r of ariaRadios) {
                            const label = (r.getAttribute('aria-label') || r.innerText || '').trim().toLowerCase();
                            if (label === 'no') {
                                r.scrollIntoView({block: 'center'});
                                r.click();
                                return true;
                            }
                        }
                        return false;
                    }

                    // For native radios: find the one labeled "No"
                    for (const r of radios) {
                        // Check the label associated with this radio
                        let lbl = '';
                        if (r.id) {
                            const labelEl = document.querySelector('label[for="' + CSS.escape(r.id) + '"]');
                            if (labelEl) lbl = (labelEl.innerText || '').trim().toLowerCase();
                        }
                        if (!lbl) {
                            const wrap = r.closest('label');
                            if (wrap) lbl = (wrap.innerText || '').trim().toLowerCase();
                        }
                        if (!lbl) {
                            // Check next sibling text
                            const next = r.nextElementSibling || r.parentElement;
                            if (next) lbl = (next.innerText || '').trim().toLowerCase();
                        }
                        if (lbl === 'no' || r.value.toLowerCase() === 'no') {
                            // Click the label instead of the radio input for React compatibility
                            let clickTarget = null;
                            if (r.id) {
                                clickTarget = document.querySelector('label[for="' + CSS.escape(r.id) + '"]');
                            }
                            if (!clickTarget) {
                                clickTarget = r.closest('label');
                            }
                            if (clickTarget) {
                                clickTarget.scrollIntoView({block: 'center'});
                                clickTarget.click();
                            } else {
                                // Fallback to clicking the radio directly if no label found
                                r.scrollIntoView({block: 'center'});
                                r.click();
                            }
                            return true;
                        }
                    }
                    return false;
                }
            """))
            if clicked:
                await asyncio.sleep(0.3)
                logger.info("[Workday] Clicked 'No' for 'previously worked' (via JS fallback)")
                return True
        except Exception as exc:
            logger.debug("[Workday] 'previously worked' JS fallback failed: %s", exc)

        return False

    # ------------------------------------------------------------------
    # "How Did You Hear" — simple inline handler (no pre-fill)
    # ------------------------------------------------------------------
    async def _dump_dom_around_source(self, page: Any, step_label: str) -> None:
        """Dump detailed DOM info around the 'How Did You Hear' field for debugging."""
        evaluate_fn = getattr(page, "evaluate", None)
        if not evaluate_fn:
            return
        try:
            dump = await maybe_await(evaluate_fn("""
                (stepLabel) => {
                    const out = { step: stepLabel };

                    // 1. The formField-source container
                    const src = document.querySelector('[data-automation-id="formField-source"]');
                    out.sourceExists = !!src;
                    if (src) {
                        out.sourceHTML = src.outerHTML.slice(0, 2000);
                        out.sourceChildren = Array.from(src.querySelectorAll('*')).slice(0, 50).map(el => ({
                            tag: el.tagName,
                            id: el.id || '',
                            aid: el.getAttribute('data-automation-id') || '',
                            role: el.getAttribute('role') || '',
                            cls: (el.className || '').toString().slice(0, 80),
                            text: (el.innerText || '').slice(0, 60),
                            visible: el.getBoundingClientRect().width > 0
                        }));
                    }

                    // 2. All menuItem elements on the page
                    const menuItems = document.querySelectorAll('[data-automation-id="menuItem"]');
                    out.menuItemCount = menuItems.length;
                    out.menuItems = Array.from(menuItems).slice(0, 20).map(el => ({
                        text: (el.innerText || '').slice(0, 80),
                        visible: el.getBoundingClientRect().width > 0,
                        parent: el.parentElement ? {
                            tag: el.parentElement.tagName,
                            id: el.parentElement.id || '',
                            aid: el.parentElement.getAttribute('data-automation-id') || '',
                            role: el.parentElement.getAttribute('role') || ''
                        } : null
                    }));

                    // 3. Any popups/overlays/dialogs currently visible
                    const popupSels = [
                        '[data-automation-id*="popup"]',
                        '[data-automation-id*="Popup"]',
                        '[role="dialog"]',
                        '[role="listbox"]',
                        '[data-automation-id*="menuItemGroup"]',
                        '[data-automation-id*="promptSearch"]'
                    ];
                    out.popups = [];
                    for (const sel of popupSels) {
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                out.popups.push({
                                    selector: sel,
                                    tag: el.tagName,
                                    id: el.id || '',
                                    aid: el.getAttribute('data-automation-id') || '',
                                    role: el.getAttribute('role') || '',
                                    childCount: el.children.length,
                                    html: el.outerHTML.slice(0, 500)
                                });
                            }
                        }
                    }

                    // 4. All elements with data-automation-id containing "source" or "prompt"
                    const related = document.querySelectorAll(
                        '[data-automation-id*="source"], [data-automation-id*="prompt"], [data-automation-id*="selectedItem"]'
                    );
                    out.relatedElements = Array.from(related).slice(0, 30).map(el => ({
                        tag: el.tagName,
                        aid: el.getAttribute('data-automation-id') || '',
                        id: el.id || '',
                        text: (el.innerText || '').slice(0, 60),
                        visible: el.getBoundingClientRect().width > 0
                    }));

                    return JSON.stringify(out, null, 2);
                }
            """, step_label))
            logger.info("[HowDidYouHear DOM @ %s]\n%s", step_label, dump)
        except Exception as exc:
            logger.debug("[HowDidYouHear DOM dump failed @ %s]: %s", step_label, exc)

    async def _fill_how_did_you_hear(self, page: Any) -> bool:
        """
        Dead-simple handler for 'How Did You Hear About Us?'

        The answer does not matter. Steps:
        1. Click the prompt icon to open the dropdown
        2. Click ANY visible promptOption (the actual dropdown items)
        3. Click neutral whitespace to close

        IMPORTANT: The dropdown options use data-automation-id="promptOption"
        (and "promptLeafNode"), NOT "menuItem". "menuItem" belongs to the
        phone-country-code multiselect and will click the wrong thing.
        """
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return False

        logger.info("[HowDidYouHear] Starting")

        # ── Step 1: Click the prompt icon to open the dropdown ──
        try:
            icon = locator_fn(
                "[data-automation-id='formField-source'] [data-automation-id='promptIcon']"
            ).first
            visible = await maybe_await(icon.is_visible())
            logger.info("[HowDidYouHear] Step 1: promptIcon visible=%s", visible)
            if not visible:
                logger.warning("[HowDidYouHear] promptIcon not visible — aborting")
                return False
            await maybe_await(icon.click(force=True, timeout=3000))
            logger.info("[HowDidYouHear] Step 1: clicked promptIcon")
        except Exception as e:
            logger.error("[HowDidYouHear] Step 1 FAILED: %s", e)
            return False

        await asyncio.sleep(1.5)

        # ── Step 2: Click ANY visible promptOption ──
        # Use "div[...]" to exclude the phone-country-code's <P> tag which
        # also has data-automation-id="promptOption" but is NOT a dropdown option.
        # The real "How Did You Hear" options are always <DIV> tags.
        try:
            options = locator_fn("div[data-automation-id='promptOption']")
            count = await maybe_await(options.count())
            logger.info("[HowDidYouHear] Step 2: found %d div promptOption elements", count)

            if count == 0:
                logger.warning("[HowDidYouHear] Step 2: no promptOption found — aborting")
                await maybe_await(page.mouse.click(10, 10))
                return False

            # Click the first visible one
            clicked = False
            for idx in range(min(count, 20)):
                opt = options.nth(idx)
                try:
                    vis = await maybe_await(opt.is_visible())
                    text = (await maybe_await(opt.text_content()) or "").strip()
                    logger.info(
                        "[HowDidYouHear] Step 2: promptOption[%d] visible=%s text='%s'",
                        idx, vis, text[:60],
                    )
                    if vis:
                        await maybe_await(opt.click(force=True, timeout=3000))
                        logger.info(
                            "[HowDidYouHear] Step 2: CLICKED promptOption[%d] '%s'",
                            idx, text[:60],
                        )
                        clicked = True
                        break
                except Exception as e:
                    logger.debug("[HowDidYouHear] Step 2: promptOption[%d] error: %s", idx, e)
                    continue

            if not clicked:
                logger.warning("[HowDidYouHear] Step 2: could not click any promptOption")
                await maybe_await(page.mouse.click(10, 10))
                return False
        except Exception as e:
            logger.error("[HowDidYouHear] Step 2 FAILED: %s", e)
            await maybe_await(page.mouse.click(10, 10))
            return False

        await asyncio.sleep(1.0)

        # ── Step 3: Check if this was a category (has sub-options) or a leaf ──
        # If new promptOptions appeared, click one of them too.
        try:
            options = locator_fn("div[data-automation-id='promptOption']")
            count = await maybe_await(options.count())
            logger.info("[HowDidYouHear] Step 3: %d div promptOption elements after click", count)

            if count > 0:
                # There are still options visible — could be sub-options.
                # Check if the field already has a selection.
                sel_label = locator_fn(
                    "[data-automation-id='formField-source'] "
                    "[data-automation-id='promptAriaInstruction']"
                ).first
                sel_text = (await maybe_await(sel_label.text_content()) or "").strip()
                logger.info("[HowDidYouHear] Step 3: selection state = '%s'", sel_text)

                # If still "0 items selected" or "Expanded", we need to click a sub-option
                if "0 items" in sel_text.lower() or "expanded" in sel_text.lower():
                    for idx in range(min(count, 20)):
                        opt = options.nth(idx)
                        try:
                            vis = await maybe_await(opt.is_visible())
                            text = (await maybe_await(opt.text_content()) or "").strip()
                            if vis:
                                await maybe_await(opt.click(force=True, timeout=3000))
                                logger.info(
                                    "[HowDidYouHear] Step 3: CLICKED sub-option[%d] '%s'",
                                    idx, text[:60],
                                )
                                break
                        except Exception:
                            continue
                    await asyncio.sleep(0.5)
        except Exception as e:
            logger.debug("[HowDidYouHear] Step 3 error: %s", e)

        # ── Step 4: Click neutral whitespace to close ──
        try:
            await maybe_await(page.mouse.click(10, 10))
            logger.info("[HowDidYouHear] Step 4: clicked whitespace to close")
        except Exception:
            pass

        await asyncio.sleep(0.5)

        # Verify selection
        try:
            sel_label = locator_fn(
                "[data-automation-id='formField-source'] "
                "[data-automation-id='promptAriaInstruction']"
            ).first
            sel_text = (await maybe_await(sel_label.text_content()) or "").strip()
            if "0 items" in sel_text.lower():
                logger.warning("[HowDidYouHear] FAILED — still 0 items selected")
                return False
            else:
                logger.info("[HowDidYouHear] SUCCESS — selection: '%s'", sel_text)
                return True
        except Exception:
            pass

        logger.info("[HowDidYouHear] Done")
        return True
    # Page 2: "My Experience"
    # ------------------------------------------------------------------
    async def _pre_fill_page2_my_experience(self, page: Any, ctx: Any) -> None:
        """
        Fill the entire 'My Experience' page:
          1. Work Experience (expand + fill structured data)
          2. Education (expand + fill structured data)
          3. Skills (JD keywords + profile skills)
          4. Websites (GitHub/portfolio URL)
          5. Social Network URLs (LinkedIn)
        Resume upload is handled by the base class.
        """
        resume_data = getattr(ctx.profile, "resume", {}) or {}

        await self._fill_work_experience(page, resume_data)
        await self._fill_education(page, ctx, resume_data)

        # Ensure any lingering dropdowns (Field of Study, Degree) are fully
        # dismissed before opening the Skills multiselect picker.
        # Workday's React can leave popups open despite Escape + whitespace clicks.
        try:
            keyboard = getattr(page, "keyboard", None)
            mouse = getattr(page, "mouse", None)
            if keyboard:
                await maybe_await(keyboard.press("Escape"))
                await asyncio.sleep(0.3)
            if mouse:
                await maybe_await(mouse.click(10, 10))
                await asyncio.sleep(0.5)
            # Verify no listbox is still open
            listbox_open = await maybe_await(page.evaluate("""() => {
                const lb = document.querySelector("[role='listbox']");
                if (!lb) return false;
                const r = lb.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }"""))
            if listbox_open:
                if keyboard:
                    await maybe_await(keyboard.press("Escape"))
                    await asyncio.sleep(0.3)
                if mouse:
                    await maybe_await(mouse.click(10, 10))
                    await asyncio.sleep(0.5)
        except Exception:
            pass

        await self._fill_skills(page, ctx, resume_data)
        await self._fill_websites(page, ctx, resume_data)
        await self._fill_social_urls(page, ctx, resume_data)

    def _get_snapshot_skip_labels(self) -> list[str]:
        """Labels the snapshot-and-fill should skip on Workday (handled by pre-fill).

        NOTE: 'how did you hear' and 'school or university' are NOT in this list.
        When the specialized handlers fail (e.g. different Workday layout), snapshot-fill
        must still try via combobox/text protocol + standard fields.
        """
        return [
            "job title",
            "company",
            "location",
            "role description",
            "i currently work here",
            "degree",
            "field of study",
            "overall result",
            "skills",
            "social network",
            "facebook",
            "twitter",
            "linkedin",
            "url",
        ]

    # ==================================================================
    # Helpers for Workday spinbutton date fields and dropdowns
    # ==================================================================

    async def _js_scroll_and_click(self, page: Any, locator: Any) -> None:
        """
        Click an element via JS el.click() — bypasses Playwright's viewport
        assertion which fails on Workday's nested scroll containers.
        """
        try:
            element_handle = await maybe_await(locator.element_handle(timeout=5000))
            if element_handle:
                await maybe_await(page.evaluate("(el) => el.click()", element_handle))
                await asyncio.sleep(0.3)
                return
        except Exception:
            pass
        # Fallback: force click via Playwright
        try:
            await maybe_await(locator.click(force=True, timeout=5000))
        except Exception:
            pass

    # ==================================================================
    # Work Experience
    # ==================================================================

    async def _fill_work_experience(self, page: Any, resume_data: dict) -> None:
        """Fill ALL work experience entries from resume.json."""
        work_entries = resume_data.get("work") or []
        if not work_entries:
            logger.info("[Workday] No work experience in resume.json — skipping.")
            return

        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return

        for idx, entry in enumerate(work_entries):
            entry_num = idx + 1
            logger.info("[Workday] Filling Work Experience %d/%d...", entry_num, len(work_entries))

            # Check if this Work Experience slot already exists
            section = page.get_by_role("group", name=f"Work Experience {entry_num}", exact=True)
            try:
                visible = await maybe_await(section.is_visible(timeout=2000))
            except Exception:
                visible = False

            if not visible:
                # Click "Add" or "Add Another" within Work Experience parent group
                try:
                    work_parent = page.get_by_role("group", name="Work Experience", exact=True)
                    if entry_num == 1:
                        add_btn = work_parent.get_by_role("button", name="Add")
                    else:
                        add_btn = work_parent.get_by_role("button", name="Add Another")
                    await maybe_await(add_btn.scroll_into_view_if_needed(timeout=5000))
                    await maybe_await(add_btn.click(timeout=3000))
                    await asyncio.sleep(1.0)
                except Exception as exc:
                    logger.debug("[Workday] Could not click Add for Work Experience %d: %s", entry_num, exc)
                    continue

                # Re-locate the section after clicking Add
                section = page.get_by_role("group", name=f"Work Experience {entry_num}", exact=True)

            # -- Job Title --
            title = entry.get("position") or ""
            if title:
                try:
                    inp = section.get_by_role("textbox", name="Job Title")
                    await maybe_await(inp.scroll_into_view_if_needed(timeout=5000))
                    await maybe_await(inp.click(timeout=3000))
                    await maybe_await(inp.fill(title))
                    logger.debug("[Workday] WE%d: Job Title = '%s'", entry_num, title)
                except Exception as exc:
                    logger.debug("[Workday] WE%d: Job Title failed: %s", entry_num, exc)

            # -- Company --
            company = entry.get("name") or ""
            if company:
                try:
                    inp = section.get_by_role("textbox", name="Company")
                    await maybe_await(inp.scroll_into_view_if_needed(timeout=5000))
                    await maybe_await(inp.click(timeout=3000))
                    await maybe_await(inp.fill(company))
                    logger.debug("[Workday] WE%d: Company = '%s'", entry_num, company)
                except Exception as exc:
                    logger.debug("[Workday] WE%d: Company failed: %s", entry_num, exc)

            # -- Location --
            location = entry.get("location") or ""
            if location:
                try:
                    inp = section.get_by_role("textbox", name="Location")
                    await maybe_await(inp.scroll_into_view_if_needed(timeout=5000))
                    await maybe_await(inp.click(timeout=3000))
                    await maybe_await(inp.fill(location))
                    logger.debug("[Workday] WE%d: Location = '%s'", entry_num, location)
                except Exception as exc:
                    logger.debug("[Workday] WE%d: Location failed: %s", entry_num, exc)

            # -- "I currently work here" checkbox --
            is_current = entry.get("endDate") is None
            if is_current:
                try:
                    cb = section.get_by_role("checkbox", name="I currently work here")
                    checked = await maybe_await(cb.is_checked())
                    if not checked:
                        await maybe_await(cb.scroll_into_view_if_needed(timeout=5000))
                        await maybe_await(cb.click(timeout=3000))
                        logger.debug("[Workday] WE%d: Checked 'I currently work here'", entry_num)
                        await asyncio.sleep(0.5)  # Wait for "To" field to disappear
                except Exception as exc:
                    logger.debug("[Workday] WE%d: Checkbox failed: %s", entry_num, exc)

            # -- From date (MM/YYYY) --
            # Interaction: click the Month spinbutton and type MMYYYY as one
            # continuous string — Workday auto-tabs from Month to Year.
            start_date = entry.get("startDate") or ""  # e.g. '2025-08'
            if start_date:
                parts = start_date.split("-")
                if len(parts) >= 2:
                    year_val, month_val = parts[0], parts[1]
                    try:
                        from_group = section.get_by_role("group", name="From")
                        month_sb = from_group.get_by_role("spinbutton", name="Month")
                        await self._js_scroll_and_click(page, month_sb)
                        # Type MMYYYY as single string — auto-fills both Month and Year
                        keyboard = getattr(page, "keyboard", None)
                        if keyboard:
                            await maybe_await(keyboard.type(f"{month_val}{year_val}"))
                        else:
                            await maybe_await(month_sb.fill(f"{month_val}{year_val}"))
                        logger.debug("[Workday] WE%d: From = %s/%s", entry_num, month_val, year_val)
                    except Exception as exc:
                        logger.debug("[Workday] WE%d: From date failed: %s", entry_num, exc)

            # -- To date (MM/YYYY) — only if not current job --
            end_date = entry.get("endDate")  # e.g. '2024-01' or None
            if end_date:
                parts = end_date.split("-")
                if len(parts) >= 2:
                    year_val, month_val = parts[0], parts[1]
                    try:
                        to_group = section.get_by_role("group", name="To")
                        month_sb = to_group.get_by_role("spinbutton", name="Month")
                        await self._js_scroll_and_click(page, month_sb)
                        # Type MMYYYY as single string — auto-fills both Month and Year
                        keyboard = getattr(page, "keyboard", None)
                        if keyboard:
                            await maybe_await(keyboard.type(f"{month_val}{year_val}"))
                        else:
                            await maybe_await(month_sb.fill(f"{month_val}{year_val}"))
                        logger.debug("[Workday] WE%d: To = %s/%s", entry_num, month_val, year_val)
                    except Exception as exc:
                        logger.debug("[Workday] WE%d: To date failed: %s", entry_num, exc)

            # -- Role Description --
            highlights = entry.get("highlights") or []
            if highlights:
                desc = "\n".join(f"• {h}" for h in highlights)
                if len(desc) > 2000:
                    desc = desc[:2000].rsplit("\n", 1)[0]
                try:
                    inp = section.get_by_role("textbox", name="Role Description")
                    await maybe_await(inp.scroll_into_view_if_needed(timeout=5000))
                    await maybe_await(inp.click(timeout=3000))
                    await maybe_await(inp.fill(desc))
                    logger.debug("[Workday] WE%d: Role Description filled (%d chars)", entry_num, len(desc))
                except Exception as exc:
                    logger.debug("[Workday] WE%d: Role Description failed: %s", entry_num, exc)

            logger.info("[Workday] Filled Work Experience %d: %s at %s", entry_num, title, company)
            await asyncio.sleep(0.5)

    # ==================================================================
    # Education
    # ==================================================================

    async def _fill_education(self, page: Any, ctx: Any, resume_data: dict) -> None:
        """Fill the first education entry using group-scoped selectors."""
        edu_entries = resume_data.get("education") or []
        if not edu_entries:
            logger.info("[Workday] No education in resume.json — skipping.")
            return

        entry = edu_entries[0]
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return

        # Check if Education 1 already exists
        section = page.get_by_role("group", name="Education 1", exact=True)
        try:
            visible = await maybe_await(section.is_visible(timeout=2000))
        except Exception:
            visible = False

        if not visible:
            # Click "Add" within Education parent group
            try:
                edu_parent = page.get_by_role("group", name="Education", exact=True)
                add_btn = edu_parent.get_by_role("button", name="Add")
                await maybe_await(add_btn.scroll_into_view_if_needed(timeout=5000))
                await maybe_await(add_btn.click(timeout=3000))
                await asyncio.sleep(1.0)
            except Exception as exc:
                logger.debug("[Workday] Could not click Add for Education: %s", exc)
                return
            section = page.get_by_role("group", name="Education 1", exact=True)

        # -- School or University --
        # Workday varies: (A) plain textbox — fill() works; (B) searchable picker — type, Enter, select.
        # Use one flow that handles both: fill, Enter, if listbox appears then click option.
        school = entry.get("institution") or ""
        if school:
            await self._fill_school_or_university(page, section, school, locator_fn)

        # -- Degree dropdown (button → listbox → click option) --
        # From manual mapping: the button's accessible name includes "Degree"
        # (e.g. "Degree Select One Required"). Click it to open a listbox,
        # then use fuzzy matching to pick the closest option.
        degree = entry.get("studyType") or entry.get("degree") or ""
        if degree:
            # Map common values to a keyword for fuzzy-matching in the dropdown.
            # We no longer need exact text — just a keyword to score against.
            degree_keyword_map: dict[str, str] = {
                "bachelor of science": "bachelor",
                "bachelor of arts": "bachelor",
                "bs": "bachelor",
                "ba": "bachelor",
                "bachelor": "bachelor",
                "master of science": "master",
                "master of arts": "master",
                "ms": "master",
                "ma": "master",
                "master": "master",
                "phd": "phd",
                "doctor of philosophy": "phd",
                "doctorate": "doctorate",
                "associate": "associate",
                "associate of science": "associate",
                "associate of arts": "associate",
                "high school": "high school",
                "ged": "high school",
                "mba": "mba",
            }
            degree_keyword = degree_keyword_map.get(degree.lower(), degree.lower())

            try:
                # Find the Degree button via role — its accessible name contains "Degree"
                degree_btn = section.get_by_role("button", name=re.compile(r"Degree", re.IGNORECASE)).first
                await maybe_await(degree_btn.scroll_into_view_if_needed(timeout=5000))
                await maybe_await(degree_btn.click(timeout=5000))
                await asyncio.sleep(0.8)

                # Collect all options and their text
                # Use :visible to avoid hidden listboxes; Degree dropdown doesn't
                # use the "Expanded" aria-label pattern, it's a standard dropdown.
                options = locator_fn("[role='listbox']:visible [role='option']")
                count = await maybe_await(options.count())
                option_texts: list[tuple[int, str]] = []
                for i in range(count):
                    opt_el = options.nth(i)
                    text = (await maybe_await(opt_el.text_content()) or "").strip()
                    if text:
                        option_texts.append((i, text))

                logger.debug(
                    "[Workday] Edu: Degree options (%d): %s",
                    len(option_texts),
                    [t for _, t in option_texts],
                )

                # Fuzzy scoring: find option whose text best matches the keyword.
                # Score = number of keyword chars that appear as a subsequence,
                # with bonus for exact substring containment.
                best_idx = -1
                best_score = -1
                best_text = ""
                keyword_lower = degree_keyword.lower()

                for i, text in option_texts:
                    text_lower = text.lower()
                    score = 0
                    # Big bonus for exact substring match
                    if keyword_lower in text_lower:
                        score += 1000
                    # Bonus for each keyword word found in the option text
                    for word in keyword_lower.split():
                        if word in text_lower:
                            score += 100
                    # Small bonus for shorter options (prefer "Bachelor's Degree"
                    # over "Bachelor's Degree in Applied Science")
                    score -= len(text) * 0.1

                    if score > best_score:
                        best_score = score
                        best_idx = i
                        best_text = text

                if best_idx >= 0 and best_score > 0:
                    await maybe_await(options.nth(best_idx).click(timeout=3000))
                    logger.debug(
                        "[Workday] Edu: Degree = '%s' (score=%.1f, keyword='%s')",
                        best_text, best_score, keyword_lower,
                    )
                else:
                    # Nothing matched at all — close dropdown
                    keyboard = getattr(page, "keyboard", None)
                    if keyboard:
                        await maybe_await(keyboard.press("Escape"))
                    logger.debug(
                        "[Workday] Edu: Degree option not found for keyword '%s'",
                        keyword_lower,
                    )
            except Exception as exc:
                logger.debug("[Workday] Edu: Degree dropdown failed: %s", exc)

            # Ensure degree dropdown is fully dismissed
            try:
                mouse = getattr(page, "mouse", None)
                if mouse:
                    await maybe_await(mouse.click(10, 10))
                    await asyncio.sleep(0.5)
            except Exception:
                pass

        # -- Field of Study (search + radio picker, single-select) --
        area = entry.get("area") or ""
        if area:
            await self._fill_field_of_study_picker(page, section, area)

        # -- Overall Result (GPA) — strip "/X.XX" suffix --
        gpa = entry.get("gpa") or ""
        if gpa:
            # "3.97/4.00" → "3.97"
            gpa_value = gpa.split("/")[0].strip()
            try:
                inp = section.get_by_role("textbox", name="Overall Result (GPA)")
                await maybe_await(inp.scroll_into_view_if_needed(timeout=5000))
                await maybe_await(inp.click(timeout=3000))
                await maybe_await(inp.fill(gpa_value))
                logger.debug("[Workday] Edu: GPA = '%s'", gpa_value)
            except Exception as exc:
                logger.debug("[Workday] Edu: GPA failed: %s", exc)

        # -- From year (YYYY only) --
        # Education dates only have a Year spinbutton (no Month).
        # Find via group "From" → spinbutton "Year", click and type YYYY.
        start_date = entry.get("startDate") or ""
        if not start_date:
            # Try profile-level schoolStart
            profile_data = getattr(ctx.profile, "profile", {}) or {}
            school_start = profile_data.get("schoolStart") or ""
            if school_start:
                year_match = re.search(r"(\d{4})", school_start)
                if year_match:
                    start_date = year_match.group(1)
        if start_date:
            year_match = re.search(r"(\d{4})", start_date)
            if year_match:
                try:
                    from_group = section.get_by_role("group", name="From")
                    year_sb = from_group.get_by_role("spinbutton", name="Year")
                    await self._js_scroll_and_click(page, year_sb)
                    keyboard = getattr(page, "keyboard", None)
                    if keyboard:
                        await maybe_await(keyboard.type(year_match.group(1)))
                    else:
                        await maybe_await(year_sb.fill(year_match.group(1)))
                    logger.debug("[Workday] Edu: From year = %s", year_match.group(1))
                except Exception as exc:
                    logger.debug("[Workday] Edu: From year failed: %s", exc)

        # -- To year (YYYY only) --
        # Education "To" group is named "To (Actual or Expected)" in Workday.
        end_date_str = entry.get("endDate") or ""
        if not end_date_str:
            profile_data = getattr(ctx.profile, "profile", {}) or {}
            school_end = profile_data.get("schoolEnd") or ""
            if school_end:
                year_match = re.search(r"(\d{4})", school_end)
                if year_match:
                    end_date_str = year_match.group(1)
        if end_date_str:
            year_match = re.search(r"(\d{4})", end_date_str)
            if year_match:
                try:
                    # Try the exact name first, then a partial match
                    to_group = None
                    for to_name in ["To (Actual or Expected)", "To"]:
                        try:
                            candidate = section.get_by_role("group", name=to_name)
                            if await maybe_await(candidate.is_visible(timeout=2000)):
                                to_group = candidate
                                break
                        except Exception:
                            continue
                    if to_group is None:
                        to_group = section.get_by_role("group", name=re.compile(r"^To", re.IGNORECASE))

                    year_sb = to_group.get_by_role("spinbutton", name="Year")
                    await self._js_scroll_and_click(page, year_sb)
                    keyboard = getattr(page, "keyboard", None)
                    if keyboard:
                        await maybe_await(keyboard.type(year_match.group(1)))
                    else:
                        await maybe_await(year_sb.fill(year_match.group(1)))
                    logger.debug("[Workday] Edu: To year = %s", year_match.group(1))
                except Exception as exc:
                    logger.debug("[Workday] Edu: To year failed: %s", exc)

        logger.info("[Workday] Filled education: %s (%s)", school, degree)

    # ------------------------------------------------------------------
    # Field of Study: search + radio single-select picker
    # ------------------------------------------------------------------
    async def _fill_field_of_study_picker(
        self, page: Any, section: Any, school: str, locator_fn: Any,
    ) -> None:
        """
        Fill School or University. Handles both plain textbox and searchable picker.

        Workday varies: (A) plain textbox — fill() is enough; (B) searchable picker —
        type, Enter, wait for listbox, click option. We do fill + Enter; if listbox
        appears we select, else the fill alone sufficed.
        """
        keyboard = getattr(page, "keyboard", None)
        if not keyboard:
            return
        try:
            # Try multiple label variants — Workday forms differ
            for label_pattern in ["School or University", "School", "University", re.compile(r"School|University", re.I)]:
                try:
                    inp = section.get_by_role("textbox", name=label_pattern)
                    if await maybe_await(inp.count()) == 0:
                        continue
                    inp = inp.first
                    await maybe_await(inp.scroll_into_view_if_needed(timeout=5000))
                    await maybe_await(inp.click(timeout=3000))
                    await maybe_await(inp.fill(school))
                    logger.debug("[Workday] Edu: School typed = '%s'", school)
                    break
                except Exception:
                    continue
            else:
                logger.debug("[Workday] Edu: School textbox not found")
                return

            # If it's a searchable picker, Enter opens a listbox. Only press Enter when
            # the input looks like a combobox (has listbox popup) — otherwise we might
            # accidentally submit a plain text form.
            inp_handle = await maybe_await(inp.element_handle(timeout=2000))
            is_combobox = False
            if inp_handle:
                try:
                    is_combobox = await maybe_await(page.evaluate("""
                        (el) => {
                            if (!el) return false;
                            const root = el.closest('[role="combobox"], [aria-haspopup="listbox"], [aria-autocomplete="list"]');
                            return !!root;
                        }
                    """, inp_handle))
                except Exception:
                    pass
            if is_combobox:
                await asyncio.sleep(0.3)
                await maybe_await(keyboard.press("Enter"))
                await asyncio.sleep(1.2)

            options = locator_fn("[role='listbox']:visible [role='option']")
            count = await maybe_await(options.count())
            if count > 0:
                target_lower = school.lower()
                clicked = False
                for i in range(min(count, 15)):
                    opt = options.nth(i)
                    try:
                        if not await maybe_await(opt.is_visible()):
                            continue
                        text = (await maybe_await(opt.text_content()) or "").strip()
                        if target_lower in text.lower() or text.lower() in target_lower:
                            await maybe_await(opt.click(timeout=3000))
                            logger.debug("[Workday] Edu: School selected '%s'", text)
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    # Fallback: first option
                    try:
                        await maybe_await(options.first.click(timeout=3000))
                        logger.debug("[Workday] Edu: School selected first option")
                    except Exception:
                        pass
                # Dismiss dropdown
                try:
                    await maybe_await(keyboard.press("Escape"))
                    await asyncio.sleep(0.3)
                    mouse = getattr(page, "mouse", None)
                    if mouse:
                        await maybe_await(mouse.click(10, 10))
                        await asyncio.sleep(0.3)
                except Exception:
                    pass
            # If no listbox appeared, plain fill was enough — done.
        except Exception as exc:
            logger.debug("[Workday] Edu: School failed: %s", exc)

    async def _fill_field_of_study_picker(
        self, page: Any, section: Any, search_term: str,
    ) -> bool:
        """
        Fill the Field of Study picker inside an Education section.

        Interaction pattern (from manual snapshot mapping):
        1. Click the Field of Study textbox (placeholder: Search)
        2. Type the search term (e.g. "Computer Science")
        3. Press Enter — search results appear as options with radio buttons
        4. Click the first matching option — auto-selects, dropdown closes
        """
        locator_fn = getattr(page, "locator", None)
        keyboard = getattr(page, "keyboard", None)
        if not locator_fn or not keyboard:
            return False

        try:
            search_input = section.get_by_role("textbox", name="Field of Study")
            await maybe_await(search_input.scroll_into_view_if_needed(timeout=5000))
            await maybe_await(search_input.click(timeout=3000))
            await asyncio.sleep(0.3)
            await maybe_await(search_input.fill(search_term))
            await asyncio.sleep(0.3)
            await maybe_await(keyboard.press("Enter"))
            await asyncio.sleep(1.5)  # Wait for search results to load

            # Click the first matching option from the results
            # FoS runs before skills are filled, so no conflicting "selected items"
            # listbox exists yet — safe to use the broad :visible selector here.
            options = locator_fn("[role='listbox']:visible [role='option']")
            count = await maybe_await(options.count())
            target_lower = search_term.lower()
            clicked = False

            for i in range(min(count, 10)):
                opt = options.nth(i)
                try:
                    if not await maybe_await(opt.is_visible()):
                        continue
                    text = (await maybe_await(opt.text_content()) or "").strip().lower()
                    if target_lower in text:
                        await maybe_await(opt.click(timeout=3000))
                        logger.debug("[Workday] Field of Study: selected '%s'", text)
                        clicked = True
                        break
                except Exception:
                    continue

            # Fallback: click the first visible option
            if not clicked and count > 0:
                try:
                    first_opt = options.first
                    if await maybe_await(first_opt.is_visible()):
                        text = (await maybe_await(first_opt.text_content()) or "").strip()
                        await maybe_await(first_opt.click(timeout=3000))
                        logger.debug("[Workday] Field of Study: fallback selected '%s'", text)
                        clicked = True
                except Exception:
                    pass

            if not clicked:
                # Close dropdown
                await maybe_await(keyboard.press("Escape"))
                logger.debug("[Workday] Field of Study: no matching option for '%s'", search_term)

            # Dismiss the Field of Study dropdown fully before moving on.
            # The popup can intercept clicks on subsequent fields (e.g. GPA).
            # Strategy: Escape first, then click neutral whitespace via mouse.
            try:
                await maybe_await(keyboard.press("Escape"))
                await asyncio.sleep(0.3)
            except Exception:
                pass
            try:
                mouse = getattr(page, "mouse", None)
                if mouse:
                    await maybe_await(mouse.click(10, 10))
                    await asyncio.sleep(0.5)
            except Exception:
                pass

            return clicked

        except Exception as exc:
            logger.debug("[Workday] Field of Study picker failed: %s", exc)
            return False

    # ==================================================================
    # Multi-select search (Skills, Field of Study, etc.)
    # ==================================================================

    async def _fill_workday_multiselect_search(
        self,
        page: Any,
        *,
        container_selector: str = "",
        search_input: Any = None,
        search_terms: list[str],
        max_terms: int = 25,
    ) -> int:
        """
        Type each term into a Workday multi-select search box, hit Enter,
        and click the first matching checkbox option.

        Returns the count of successfully added terms.

        Interaction pattern (reverse-engineered):
          1. Click the search textbox
          2. Type the term
          3. Hit Enter → listbox with checkbox options appears
          4. Click the first matching option (auto-checks)
          5. Clear the textbox (triple-click + Delete) for the next term
        """
        locator_fn = getattr(page, "locator", None)
        keyboard = getattr(page, "keyboard", None)
        if not locator_fn or not keyboard:
            return 0

        # Use provided search_input, or find it from container_selector
        if search_input is None:
            try:
                # Try direct role-based lookup
                container = locator_fn(container_selector).first
                search_input = container.locator("input").first
                if not await maybe_await(search_input.is_visible(timeout=2000)):
                    logger.debug("[Workday] multiselect search input not found in %s", container_selector)
                    return 0
            except Exception:
                logger.debug("[Workday] multiselect search input not found in %s", container_selector)
                return 0

        added = 0
        for term in search_terms[:max_terms]:
            try:
                # Dismiss any open dropdown by clicking a neutral element
                # (Escape alone doesn't reliably close Workday search dropdowns)
                # Target: progress bar heading — always visible, never interactive
                try:
                    neutral = locator_fn(
                        "[data-automation-id='progressBar'], "
                        "[data-automation-id='pageHeaderTitle'], h2"
                    ).first
                    await maybe_await(neutral.click(force=True, timeout=2000))
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

                # Clear input via JS to avoid overlay issues with triple-click
                try:
                    el_handle = await maybe_await(search_input.element_handle(timeout=3000))
                    if el_handle:
                        await maybe_await(page.evaluate(
                            "(el) => { el.click(); el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); }",
                            el_handle,
                        ))
                        await asyncio.sleep(0.3)
                except Exception:
                    # Fallback: triple-click + backspace with force
                    await maybe_await(search_input.click(click_count=3, force=True, timeout=3000))
                    await asyncio.sleep(0.1)
                    await maybe_await(keyboard.press("Backspace"))
                    await asyncio.sleep(0.2)

                # Type the search term and hit Enter to trigger search
                # Scope to the SEARCH RESULTS listbox only — Workday renders
                # "selected items" as a separate [role='listbox'] with
                # aria-label="items selected". The search results listbox has
                # aria-label containing "Expanded". Using the global
                # [role='listbox']:visible selector would pick up options from
                # BOTH, causing us to click "Computer Science" (a FoS selected
                # item) instead of the actual skill option.
                all_opts = locator_fn("[role='listbox'][aria-label*='Expanded'] [role='option']")
                stale_count = await maybe_await(all_opts.count())
                await maybe_await(search_input.fill(term))
                await asyncio.sleep(0.3)
                await maybe_await(keyboard.press("Enter"))

                # Wait for search results to load: poll until option count changes
                # from the stale count (max 2.5s).
                for _poll in range(10):
                    await asyncio.sleep(0.25)
                    count = await maybe_await(all_opts.count())
                    if count != stale_count and count > 0:
                        break

                # Look for matching option in the listbox
                # Strategy: exact match first, then partial "contains" match, then first visible
                target_lower = term.lower()
                count = await maybe_await(all_opts.count())
                clicked = False

                # Pass 1: exact match (option text equals the search term)
                for opt_idx in range(min(count, 30)):
                    opt = all_opts.nth(opt_idx)
                    try:
                        if not await maybe_await(opt.is_visible()):
                            continue
                        text = (await maybe_await(opt.text_content()) or "").strip().lower()
                        if "checked" in text:
                            continue
                        if text == target_lower:
                            await maybe_await(opt.click(timeout=3000))
                            added += 1
                            clicked = True
                            logger.debug("[Workday] Added skill (exact): '%s'", term)
                            break
                    except Exception:
                        continue

                # Pass 2: partial "contains" match
                if not clicked:
                    for opt_idx in range(min(count, 30)):
                        opt = all_opts.nth(opt_idx)
                        try:
                            if not await maybe_await(opt.is_visible()):
                                continue
                            text = (await maybe_await(opt.text_content()) or "").strip().lower()
                            if "checked" in text:
                                continue
                            if target_lower in text:
                                await maybe_await(opt.click(timeout=3000))
                                added += 1
                                clicked = True
                                logger.debug("[Workday] Added skill: '%s' (matched '%s')", term, text[:40])
                                break
                        except Exception:
                            continue

                # Pass 3: fallback — click first visible unchecked option
                if not clicked:
                    for opt_idx in range(min(count, 30)):
                        opt = all_opts.nth(opt_idx)
                        try:
                            if not await maybe_await(opt.is_visible()):
                                continue
                            await maybe_await(opt.click(timeout=2000))
                            added += 1
                            logger.debug("[Workday] Added first-match skill for '%s'", term)
                            break
                        except Exception:
                            continue

                await asyncio.sleep(0.3)

            except Exception as exc:
                logger.debug("[Workday] multiselect search for '%s' failed: %s", term, exc)
                continue

        # Close any open dropdown
        try:
            await maybe_await(keyboard.press("Escape"))
        except Exception:
            pass

        logger.info("[Workday] Multiselect search: added %d / %d terms", added, len(search_terms[:max_terms]))
        return added

    # ==================================================================
    # Skills
    # ==================================================================

    async def _fill_skills(self, page: Any, ctx: Any, resume_data: dict) -> None:
        """
        Fill the Skills multi-select by combining:
          1. Skills extracted from the job description (ATS keyword matching)
          2. Skills from the user's profile/resume
        """
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return

        # Find the skills search input directly by role
        try:
            skills_input = page.get_by_role("textbox", name="Type to Add Skills")
            if not await maybe_await(skills_input.is_visible(timeout=3000)):
                logger.debug("[Workday] Skills input not visible — skipping.")
                return
        except Exception:
            logger.debug("[Workday] Skills section not found — skipping.")
            return

        # Extra safety: dismiss any lingering Field of Study dropdown before we start.
        # The Field of Study and Skills inputs share similar DOM patterns
        # (both are multiselect search boxes), so a leftover open Field of Study
        # listbox can interfere with Skills selection.
        keyboard = getattr(page, "keyboard", None)
        mouse = getattr(page, "mouse", None)
        try:
            fos_input = page.get_by_role("textbox", name="Field of Study")
            if await maybe_await(fos_input.count()) > 0:
                # Click away from Field of Study to ensure it's not focused
                if keyboard:
                    await maybe_await(keyboard.press("Escape"))
                    await asyncio.sleep(0.2)
                if mouse:
                    await maybe_await(mouse.click(10, 10))
                    await asyncio.sleep(0.3)
        except Exception:
            pass

        # Build skill list from profile + JD
        skill_set: list[str] = []
        seen: set[str] = set()

        # From resume.json skills
        resume_skills = resume_data.get("skills") or []
        for skill_group in resume_skills:
            if isinstance(skill_group, dict):
                for kw in skill_group.get("keywords") or []:
                    kw_lower = kw.strip().lower()
                    if kw_lower not in seen and len(kw) > 1:
                        seen.add(kw_lower)
                        skill_set.append(kw.strip())
            elif isinstance(skill_group, str):
                sg_lower = skill_group.strip().lower()
                if sg_lower not in seen:
                    seen.add(sg_lower)
                    skill_set.append(skill_group.strip())

        # From job description keywords
        jd = getattr(ctx, "job_description", "") or ""
        if jd:
            jd_skills = self._extract_skills_from_jd(jd, resume_skills)
            for s in jd_skills:
                s_lower = s.lower()
                if s_lower not in seen:
                    seen.add(s_lower)
                    skill_set.append(s)

        if not skill_set:
            logger.debug("[Workday] No skills to add.")
            return

        logger.info("[Workday] Adding %d skills: %s", len(skill_set), ", ".join(skill_set[:10]))

        # Use the skills input directly instead of searching by container
        await self._fill_workday_multiselect_search(
            page,
            search_input=skills_input,
            search_terms=skill_set,
            max_terms=15,
        )

    def _extract_skills_from_jd(
        self, jd: str, resume_skills: list[Any],
    ) -> list[str]:
        """
        Extract skill keywords from the JD that overlap with common tech terms.
        Returns a list of matched skill names.
        """
        # Build a lookup of known skills from the resume
        known: set[str] = set()
        for sg in resume_skills:
            if isinstance(sg, dict):
                for kw in sg.get("keywords") or []:
                    known.add(kw.strip().lower())

        # Common tech keywords to search for in JDs
        common_tech = {
            "python", "java", "javascript", "typescript", "c++", "c#", "go",
            "rust", "ruby", "scala", "kotlin", "swift", "r", "matlab",
            "sql", "nosql", "mongodb", "postgresql", "mysql", "redis",
            "aws", "azure", "gcp", "docker", "kubernetes", "terraform",
            "react", "angular", "vue", "node.js", "express", "fastapi",
            "django", "flask", "spring", "rails",
            "git", "ci/cd", "github actions", "jenkins",
            "machine learning", "deep learning", "nlp", "computer vision",
            "tensorflow", "pytorch", "scikit-learn", "pandas", "numpy",
            "linux", "unix", "bash", "shell scripting",
            "rest api", "graphql", "microservices", "agile", "scrum",
            "html", "css", "tailwind", "sass",
            "data structures", "algorithms", "system design",
            "jira", "confluence", "figma",
        }
        all_searchable = known | common_tech

        jd_lower = jd.lower()
        matched: list[str] = []
        for skill in sorted(all_searchable):
            # Only match whole words (bounded by non-alpha)
            pattern = r"(?<![a-z])" + re.escape(skill) + r"(?![a-z])"
            if re.search(pattern, jd_lower):
                # Use the properly-cased version if available
                matched.append(skill.title() if skill in common_tech else skill)

        return matched

    # ==================================================================
    # Websites
    # ==================================================================

    async def _fill_websites(self, page: Any, ctx: Any, resume_data: dict) -> None:
        """Click 'Add' in the Websites section, then fill the URL textbox.

        Workday DOM structure (observed via inspection):
          - Parent section: <div aria-labelledby="Websites-section"> containing an <h3 id="Websites-section">
          - Add button: <button data-automation-id="Add"> inside the parent
          - After clicking Add, a sub-panel appears: <div aria-labelledby="Websites-1-panel">
          - URL input: <input name="url" id="webAddress-XX--url"> inside
            a wrapper with data-automation-id="formField-url"
        """
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return

        identity = getattr(ctx.profile, "profile", {}).get("identity") or {}
        github_url = identity.get("github") or ""
        if not github_url:
            basics = resume_data.get("basics") or {}
            for p in basics.get("profiles") or []:
                if (p.get("network") or "").lower() == "github":
                    github_url = p.get("url") or ""
                    break

        if not github_url:
            logger.debug("[Workday] No website URL to add — skipping.")
            return

        # ---- Step 1: Locate the Websites parent section ----
        # The section uses aria-labelledby="Websites-section" (pointing to an h3)
        websites_parent = None
        for sel in [
            "[aria-labelledby='Websites-section']",
            "#Websites-section",
        ]:
            loc = page.locator(sel)
            try:
                if await maybe_await(loc.count()) > 0:
                    # For the #id selector, go up to the parent section
                    if sel.startswith("#"):
                        websites_parent = loc.locator("..")
                    else:
                        websites_parent = loc.first
                    break
            except Exception:
                continue

        if websites_parent is None:
            logger.debug("[Workday] Could not find Websites section — skipping.")
            return

        # ---- Step 2: Click the "Add" button ----
        add_btn = None
        for btn_sel in [
            websites_parent.locator("button[data-automation-id='add-button']"),
            websites_parent.locator("button").filter(has_text="Add"),
            websites_parent.get_by_role("button", name="Add"),
        ]:
            try:
                if await maybe_await(btn_sel.count()) > 0:
                    add_btn = btn_sel.first
                    break
            except Exception:
                continue

        if add_btn is None:
            logger.debug("[Workday] No 'Add' button found in Websites section.")
            return

        try:
            handle = await maybe_await(add_btn.element_handle(timeout=3000))
            if handle:
                await maybe_await(page.evaluate("(el) => el.click()", handle))
            else:
                await maybe_await(add_btn.click(timeout=3000))
            logger.debug("[Workday] Clicked 'Add' for Websites.")
            # Wait for the Websites-1 panel to render (poll up to 5s)
            # Also check for input[name='url'] appearing anywhere — Workday
            # may not use aria-labelledby at all on this particular instance
            panel_appeared = False
            for _wait_i in range(20):
                await asyncio.sleep(0.25)
                try:
                    panel_count = await maybe_await(
                        page.locator(
                            "[aria-labelledby*='Websites-1'], "
                            "[data-automation-id='formField-url'], "
                            "input[name='url'], "
                            "input[id*='webAddress']"
                        ).count()
                    )
                    if panel_count > 0:
                        logger.debug("[Workday] Websites-1 panel appeared after %.1fs", (_wait_i + 1) * 0.25)
                        panel_appeared = True
                        break
                except Exception:
                    pass
            if not panel_appeared:
                # Retry: try Playwright click as fallback
                logger.debug("[Workday] Panel didn't appear after JS click, retrying with Playwright click...")
                try:
                    await maybe_await(add_btn.click(timeout=3000, force=True))
                    await asyncio.sleep(1.5)
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("[Workday] Could not click Add for Websites: %s", exc)
            return

        # ---- Step 3: Find the URL input inside the new Websites-1 panel ----
        url_inp = None

        # Strategy A: data-automation-id="formField-url" input (most reliable)
        try:
            da_inp = page.locator("[data-automation-id='formField-url'] input[name='url']")
            await maybe_await(da_inp.first.wait_for(state="attached", timeout=3000))
            if await maybe_await(da_inp.count()) > 0:
                url_inp = da_inp.first
        except Exception:
            pass

        # Strategy B: input with name="url" inside the Websites-1 panel
        if url_inp is None:
            try:
                panel = page.locator("[aria-labelledby='Websites-1-panel']")
                if await maybe_await(panel.count()) > 0:
                    inp = panel.locator("input[name='url']")
                    if await maybe_await(inp.count()) > 0:
                        url_inp = inp.first
            except Exception:
                pass

        # Strategy C: any input whose id matches webAddress-*--url
        if url_inp is None:
            try:
                wa_inp = page.locator("input[id*='webAddress'][id$='--url']")
                if await maybe_await(wa_inp.count()) > 0:
                    url_inp = wa_inp.first
            except Exception:
                pass

        # Strategy D: fallback — any visible input inside the panel
        if url_inp is None:
            try:
                panel = page.locator("[aria-labelledby='Websites-1-panel']")
                if await maybe_await(panel.count()) > 0:
                    any_inp = panel.locator("input[type='text'], input:not([type])")
                    if await maybe_await(any_inp.count()) > 0:
                        url_inp = any_inp.first
            except Exception:
                pass

        # Strategy E: broad fallback — any input[name='url'] on the page
        if url_inp is None:
            try:
                broad = page.locator("input[name='url']")
                if await maybe_await(broad.count()) > 0:
                    url_inp = broad.first
            except Exception:
                pass

        if url_inp is None:
            logger.debug("[Workday] Website URL: no input found after clicking Add.")
            return

        # ---- Step 4: Click and fill ----
        try:
            await maybe_await(url_inp.wait_for(state="visible", timeout=5000))
        except Exception:
            pass
        try:
            await maybe_await(url_inp.click(timeout=3000))
            await maybe_await(url_inp.fill(github_url))
            logger.info("[Workday] Filled website URL: %s", github_url)
        except Exception as exc:
            logger.debug("[Workday] Website URL fill failed: %s", exc)

    # ==================================================================
    # Social Network URLs
    # ==================================================================

    async def _fill_social_urls(self, page: Any, ctx: Any, resume_data: dict) -> None:
        """Fill LinkedIn textbox in the Social Network URLs section. Skip Twitter entirely."""
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return

        identity = getattr(ctx.profile, "profile", {}).get("identity") or {}
        basics = resume_data.get("basics") or {}
        profiles = basics.get("profiles") or []

        linkedin_url = identity.get("linkedin") or ""
        facebook_url = ""

        for p in profiles:
            network = (p.get("network") or "").lower()
            url = p.get("url") or ""
            if network == "linkedin" and not linkedin_url:
                linkedin_url = url
            elif network == "facebook":
                facebook_url = url

        # Fill LinkedIn
        if linkedin_url:
            # Workday requires URLs to start with https://www.
            if linkedin_url.startswith("https://linkedin.com"):
                linkedin_url = linkedin_url.replace("https://linkedin.com", "https://www.linkedin.com", 1)
            elif linkedin_url.startswith("http://linkedin.com"):
                linkedin_url = linkedin_url.replace("http://linkedin.com", "https://www.linkedin.com", 1)
            elif linkedin_url.startswith("linkedin.com"):
                linkedin_url = "https://www." + linkedin_url
            try:
                inp = page.get_by_role("textbox", name="LinkedIn")
                await maybe_await(inp.scroll_into_view_if_needed(timeout=5000))
                await maybe_await(inp.click(timeout=3000))
                await maybe_await(inp.fill(linkedin_url))
                logger.debug("[Workday] Filled LinkedIn URL")
            except Exception as exc:
                logger.debug("[Workday] LinkedIn fill failed: %s", exc)

        # Fill Facebook (skip if no data)
        if facebook_url:
            try:
                inp = page.get_by_role("textbox", name="Facebook")
                await maybe_await(inp.scroll_into_view_if_needed(timeout=5000))
                await maybe_await(inp.click(timeout=3000))
                await maybe_await(inp.fill(facebook_url))
                logger.debug("[Workday] Filled Facebook URL")
            except Exception as exc:
                logger.debug("[Workday] Facebook fill failed: %s", exc)

        # NOTE: Twitter field is intentionally NEVER filled.
        # Workday's Twitter validation is buggy (rejects both URLs and usernames).

        logger.info("[Workday] Social URLs filled (LinkedIn=%s)", bool(linkedin_url))
