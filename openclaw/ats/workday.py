from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .base import BaseATSHandler
from openclaw.utils import smart_click, maybe_await

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Workday page-heading → handler mapping
# ---------------------------------------------------------------------------
# Workday multi-page applications have a progress bar whose heading
# identifies each step (e.g. "My Information", "My Experience", …).
# We detect the heading text and dispatch to page-specific pre-fill hooks.
WORKDAY_PAGE_HEADINGS: dict[str, str] = {
    "my information": "page1",
    "my experience": "page2",
    "application questions": "page3",
    "voluntary disclosures": "page4",
    "self identify": "page5",
    "review": "page6",
}


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

    async def apply(self, page: Any, ctx: Any) -> dict:
        """Override to bump max_form_pages for Workday's 6-step flow."""
        if hasattr(ctx, "max_form_pages") and ctx.max_form_pages < 6:
            ctx.max_form_pages = 6
            logger.debug("[Workday] Bumped max_form_pages to 6")
        return await super(WorkdayHandler, self).apply(page, ctx)

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

    async def _fill_workday_directory_picker(
        self, page: Any, *, target_category: str = "Job Board",
        sub_option: str | None = None,
    ) -> bool:
        """
        Handle Workday's 'How Did You Hear About Us?' multi-select directory picker.

        Reverse-engineered from live Workday DOM observation:
        1. Click the promptIcon SPAN inside [data-automation-id="formField-source"]
           → A listbox "Options Expanded" appears with category options
             (Agency, Job Board, etc.) — NO radio buttons.
        2. Click the option matching target_category (e.g. "Job Board")
           → That listbox replaces itself with a sub-options listbox
             that HAS radio buttons (Bizjetjobs, Indeed, etc.)
        3. Click the desired sub-option
           → Auto-selects, popup closes. No OK button needed.
           → Field shows "1 item selected, Indeed"
        """
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return False

        # ── Step 1: Click the promptIcon inside formField-source ──
        icon = locator_fn(
            "[data-automation-id='formField-source'] [data-automation-id='promptIcon']"
        ).first
        try:
            if not await maybe_await(icon.is_visible()):
                logger.debug("Workday picker: promptIcon not visible")
                return False
            await maybe_await(icon.click(timeout=3000))
            logger.debug("Workday picker: Step 1 — clicked promptIcon")
        except Exception as e:
            logger.debug("Workday picker: Step 1 failed: %s", e)
            return False

        await asyncio.sleep(1.0)

        # ── Step 2: Click the category option (e.g. "Job Board") ──
        # The listbox that appears has options WITHOUT radio buttons.
        try:
            cat_option = locator_fn(
                f"[role='option']:has-text('{target_category}')"
            ).first
            await maybe_await(cat_option.click(timeout=3000))
            logger.debug("Workday picker: Step 2 — clicked category '%s'", target_category)
        except Exception as e:
            logger.debug("Workday picker: Step 2 failed: %s", e)
            keyboard = getattr(page, "keyboard", None)
            if keyboard:
                try:
                    await maybe_await(keyboard.press("Escape"))
                except Exception:
                    pass
            return False

        await asyncio.sleep(1.0)

        # ── Step 3: Click a sub-option from the new listbox (has radio buttons) ──
        # Try the preferred sub_option first, then fallback to first visible option.
        preferred = sub_option or "Indeed"
        selected_text = None

        for attempt in range(3):
            try:
                # Try preferred option first
                pref_option = locator_fn(
                    f"[role='option']:has-text('{preferred}')"
                ).first
                if await maybe_await(pref_option.is_visible()):
                    await maybe_await(pref_option.click(timeout=3000))
                    selected_text = preferred
                    break
            except Exception:
                pass

            # Fallback: click the first visible option that has a radio button
            try:
                all_options = locator_fn("[role='option']")
                count = await maybe_await(all_options.count())
                for idx in range(count):
                    opt = all_options.nth(idx)
                    try:
                        if not await maybe_await(opt.is_visible()):
                            continue
                        # Check if this option contains a radio (sub-option, not category)
                        radio = opt.locator("[role='radio']")
                        radio_count = await maybe_await(radio.count())
                        if radio_count == 0:
                            continue
                        opt_text = (await maybe_await(opt.text_content()) or "").strip()
                        await maybe_await(opt.click(timeout=3000))
                        selected_text = opt_text
                        break
                    except Exception:
                        continue
                if selected_text:
                    break
            except Exception:
                pass

            await asyncio.sleep(0.8)

        if not selected_text:
            logger.debug("Workday picker: Step 3 — couldn't select a sub-option")
            keyboard = getattr(page, "keyboard", None)
            if keyboard:
                try:
                    await maybe_await(keyboard.press("Escape"))
                except Exception:
                    pass
            return False

        logger.info("Workday directory picker: selected '%s' under '%s'",
                    selected_text[:50], target_category)
        return True

    # ------------------------------------------------------------------
    # Page detection
    # ------------------------------------------------------------------
    async def _detect_workday_page(self, page: Any) -> str:
        """Return a page key like 'page1', 'page2', … based on the active step heading."""
        evaluate_fn = getattr(page, "evaluate", None)
        if not evaluate_fn:
            return "unknown"
        try:
            heading_text: str = await maybe_await(evaluate_fn("""
                () => {
                    // Strategy 1: Workday marks the current step with aria-current="step"
                    // or aria-current="true" in the progress bar
                    const currentStep = document.querySelector(
                        '[aria-current="step"], [aria-current="true"], ' +
                        '[data-automation-id="progressBar"] [aria-selected="true"], ' +
                        '.css-1xkhfkz [aria-current]'
                    );
                    if (currentStep) {
                        const text = (currentStep.textContent || '').trim();
                        if (text) return text;
                    }

                    // Strategy 2: Look for the active step by class (bold/highlighted)
                    const steps = document.querySelectorAll('[data-automation-id="progressBar"] li, [data-automation-id="progressBar"] div[role="listitem"]');
                    for (const step of steps) {
                        const style = window.getComputedStyle(step);
                        // Active steps typically have bold font or different color
                        if (style.fontWeight >= 700 || step.classList.contains('css-1wc5nlo')) {
                            const text = (step.textContent || '').trim();
                            if (text) return text;
                        }
                    }

                    // Strategy 3: Find the first heading that's NOT in the progress bar
                    // (the page content heading)
                    const progressBar = document.querySelector('[data-automation-id="progressBar"]');
                    const allH2 = document.querySelectorAll('h2');
                    for (const h2 of allH2) {
                        if (progressBar && progressBar.contains(h2)) continue;
                        if (h2.offsetParent !== null) {
                            const text = (h2.textContent || '').trim();
                            if (text) return text;
                        }
                    }

                    // Strategy 4: Last resort — try data-automation-id for section identifiers
                    const formContent = document.querySelector('[data-automation-id="formContent"]');
                    if (formContent) {
                        const firstH2 = formContent.querySelector('h2');
                        if (firstH2) return (firstH2.textContent || '').trim();
                    }

                    return '';
                }
            """))
            heading_lower = heading_text.lower().strip()
            for key, page_id in WORKDAY_PAGE_HEADINGS.items():
                if key in heading_lower:
                    logger.debug("[Workday] Detected page heading '%s' → %s", heading_text, page_id)
                    return page_id
        except Exception as exc:
            logger.debug("[Workday] Page detection failed: %s", exc)

        # Content-based fallback: detect by page-specific elements
        try:
            page_type: str = await maybe_await(evaluate_fn("""
                () => {
                    // Check for Work Experience section (page 2)
                    if (document.querySelector('[data-automation-id*="workExperience"], [data-automation-id*="education"]'))
                        return 'page2';
                    // Check for My Information fields (page 1)
                    if (document.querySelector('#emailAddress--emailAddress, #address--addressLine1, [data-automation-id*="legalName"]'))
                        return 'page1';
                    // Check for Application Questions (page 3+)
                    if (document.querySelector('[data-automation-id*="questionnaire"], [data-automation-id*="primaryQuestionnaire"]'))
                        return 'page3';
                    // Check for Review/Summary page
                    if (document.querySelector('[data-automation-id*="review"], [data-automation-id*="summary"]'))
                        return 'review';
                    return '';
                }
            """))
            if page_type:
                logger.debug("[Workday] Detected page by content: %s", page_type)
                return page_type
        except Exception:
            pass
        return "unknown"

    # ------------------------------------------------------------------
    # Pre-fill dispatcher
    # ------------------------------------------------------------------
    async def _pre_fill_special_controls(self, page: Any, ctx: Any) -> None:
        """Handle Workday-specific widgets before standard form filling."""
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return

        # Wait for Workday SPA page transition to settle.
        # After clicking "Next", the DOM heading may take 1-3s to update.
        # We poll until the heading changes from the previous page's heading.
        prev_page = self._last_detected_page
        current_page = "unknown"
        # First poll after 2s (give SPA time to render), then 0.5s intervals
        await asyncio.sleep(2.0)
        current_page = await self._detect_workday_page(page)
        if current_page == "unknown" or (prev_page is not None and current_page == prev_page):
            # Heading hasn't changed yet — poll more aggressively
            for _poll in range(6):  # up to 3s more (6 × 0.5s)
                await asyncio.sleep(0.5)
                current_page = await self._detect_workday_page(page)
                if current_page != "unknown" and (prev_page is None or current_page != prev_page):
                    break
        self._last_detected_page = current_page
        logger.info("[Workday] Current page: %s", current_page)

        # Track which pages we've already pre-filled to avoid duplicate work.
        # _pages_filled is a set[str] field on BaseATSHandler.
        if current_page == "page1" and "page1" not in self._pages_filled:
            self._pages_filled.add("page1")
            await self._pre_fill_page1(page, ctx)
        elif current_page == "page2" and "page2" not in self._pages_filled:
            self._pages_filled.add("page2")
            await self._pre_fill_page2_my_experience(page, ctx)
        elif current_page not in self._pages_filled:
            if current_page != "unknown":
                self._pages_filled.add(current_page)
            # For pages other than page1/page2, no special pre-fill needed
            # (application questions, review, etc. are handled by snapshot-and-fill)

    async def _pre_fill_page1(self, page: Any, ctx: Any) -> None:
        """Page 1 ('My Information'): How Did You Hear directory picker."""
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return
        try:
            heard_label = locator_fn("text=/How Did You Hear/i").first
            # Wait for the label to appear (may still be loading after page transition)
            try:
                await maybe_await(heard_label.wait_for(state="visible", timeout=5000))
            except Exception:
                logger.debug("[Workday] 'How Did You Hear' label not found after 5s wait.")
                return
            logger.info("[Workday] Attempting 'How Did You Hear' directory picker...")
            ok = await self._fill_workday_directory_picker(page, target_category="Job Board")
            if ok:
                logger.info("[Workday] 'How Did You Hear' filled via directory picker.")
            else:
                logger.debug("[Workday] 'How Did You Hear' directory picker failed.")
        except Exception:
            pass

    # ------------------------------------------------------------------
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
        await self._fill_skills(page, ctx, resume_data)
        await self._fill_websites(page, ctx, resume_data)
        await self._fill_social_urls(page, ctx, resume_data)

    def _get_snapshot_skip_labels(self) -> list[str]:
        """Labels the snapshot-and-fill should skip on Workday (handled by pre-fill)."""
        return [
            "how did you hear",
            "job title",
            "company",
            "location",
            "role description",
            "i currently work here",
            "school or university",
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
        Scroll an element into view via JS (works inside nested scroll containers
        that Playwright's built-in scroll_into_view_if_needed misses) and focus it.
        Uses JS scrollIntoView + focus as a reliable alternative to click for
        elements (like spinbuttons) trapped in nested scrollable containers.
        """
        try:
            element_handle = await maybe_await(locator.element_handle(timeout=5000))
            if element_handle:
                await maybe_await(page.evaluate(
                    "(el) => { el.scrollIntoView({block: 'center', behavior: 'instant'}); el.focus(); }",
                    element_handle,
                ))
                await asyncio.sleep(0.3)
                return
        except Exception:
            pass
        # Fallback: try Playwright's focus method
        try:
            await maybe_await(locator.focus(timeout=5000))
            await asyncio.sleep(0.2)
        except Exception:
            # Last resort: force click
            await maybe_await(locator.click(force=True, timeout=5000))

    async def _fill_spinbutton(self, page: Any, label_text: str, value: str) -> bool:
        """
        Fill a Workday spinbutton field by locating it via its label.
        Workday dates use <input role='spinbutton'> for Month and Year.
        ``label_text`` should be something like 'Month' or 'Year'.
        ``value`` is the numeric string to enter (e.g. '08' for August, '2025').
        """
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return False
        try:
            sb = locator_fn(f"input[role='spinbutton'][aria-label='{label_text}']").first
            if not await maybe_await(sb.is_visible()):
                return False
            await maybe_await(sb.click())
            await maybe_await(sb.fill(value))
            return True
        except Exception as exc:
            logger.debug("[Workday] spinbutton '%s' fill failed: %s", label_text, exc)
            return False

    async def _set_workday_date_mmyyyy(
        self, page: Any, container_selector: str, month: str, year: str,
    ) -> bool:
        """
        Fill a Workday MM/YYYY date field inside a container.
        ``container_selector`` should scope to the specific date-group
        (e.g. ``[data-automation-id='formField-startDate']`` or a
        section-scoped selector like ``#workExp [data-automation-id='formField-startDate']``).
        ``month`` = '01'..'12', ``year`` = '2025'.
        """
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return False
        ok = True
        try:
            container = locator_fn(container_selector).first
            month_sb = container.locator("input[role='spinbutton'][aria-label='Month']").first
            year_sb = container.locator("input[role='spinbutton'][aria-label='Year']").first
            await maybe_await(month_sb.click())
            await maybe_await(month_sb.fill(month))
            await asyncio.sleep(0.2)
            await maybe_await(year_sb.click())
            await maybe_await(year_sb.fill(year))
        except Exception as exc:
            logger.debug("[Workday] date MM/YYYY fill failed for %s: %s", container_selector, exc)
            ok = False
        return ok

    async def _set_workday_date_yyyy(
        self, page: Any, container_selector: str, year: str,
    ) -> bool:
        """Fill a Workday YYYY-only date (education From/To)."""
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return False
        try:
            container = locator_fn(container_selector).first
            year_sb = container.locator("input[role='spinbutton'][aria-label='Year']").first
            await maybe_await(year_sb.click())
            await maybe_await(year_sb.fill(year))
            return True
        except Exception as exc:
            logger.debug("[Workday] date YYYY fill failed for %s: %s", container_selector, exc)
            return False

    async def _set_workday_date_in_section(
        self, page: Any, section_locator: Any,
        date_automation_id: str, month: str | None, year: str,
    ) -> bool:
        """
        Fill a date within a specific section locator scope.
        This avoids conflicts when multiple sections have the same
        data-automation-id (e.g. both Work Experience and Education have
        formField-startDate).
        Scrolls the element into view first to avoid viewport issues.
        """
        try:
            container = section_locator.locator(
                f"[data-automation-id='{date_automation_id}']"
            ).first
            if month:
                month_sb = container.locator("input[role='spinbutton'][aria-label='Month']").first
                await maybe_await(month_sb.scroll_into_view_if_needed(timeout=5000))
                await asyncio.sleep(0.2)
                await maybe_await(month_sb.click(timeout=5000))
                await maybe_await(month_sb.fill(month))
                await asyncio.sleep(0.2)
            year_sb = container.locator("input[role='spinbutton'][aria-label='Year']").first
            await maybe_await(year_sb.scroll_into_view_if_needed(timeout=5000))
            await asyncio.sleep(0.2)
            await maybe_await(year_sb.click(timeout=5000))
            await maybe_await(year_sb.fill(year))
            logger.debug("[Workday] Filled date %s: month=%s year=%s", date_automation_id, month, year)
            return True
        except Exception as exc:
            logger.debug("[Workday] section-scoped date fill failed (%s): %s", date_automation_id, exc)
            return False

    async def _click_add_button(self, page: Any, section_label: str) -> bool:
        """
        Click the Workday 'Add' button for an expandable section.
        ``section_label`` like 'Work Experience' or 'Education'.
        """
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return False
        try:
            # Workday Add buttons: button[aria-label='Add Work Experience'] or
            # a generic 'Add' inside a labelled section
            add_btn = locator_fn(
                f"button[aria-label*='Add {section_label}'], "
                f"button[aria-label*='Add']:near(:text('{section_label}'))"
            ).first
            if await maybe_await(add_btn.is_visible()):
                await maybe_await(add_btn.click(timeout=3000))
                await asyncio.sleep(1.0)
                logger.debug("[Workday] Clicked 'Add' for %s", section_label)
                return True
            # Fallback: look for the exact text pattern
            add_generic = locator_fn(f"button:has-text('Add'):near(:text('{section_label}'))").first
            if await maybe_await(add_generic.is_visible()):
                await maybe_await(add_generic.click(timeout=3000))
                await asyncio.sleep(1.0)
                logger.debug("[Workday] Clicked generic 'Add' for %s", section_label)
                return True
        except Exception as exc:
            logger.debug("[Workday] _click_add_button(%s) failed: %s", section_label, exc)
        return False

    async def _fill_textbox_by_automation_id(
        self, page: Any, automation_id: str, value: str,
    ) -> bool:
        """Fill a Workday textbox identified by data-automation-id on its form field."""
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return False
        try:
            inp = locator_fn(
                f"[data-automation-id='{automation_id}'] input, "
                f"[data-automation-id='{automation_id}'] textarea, "
                f"input[data-automation-id='{automation_id}'], "
                f"textarea[data-automation-id='{automation_id}']"
            ).first
            if await maybe_await(inp.is_visible()):
                await maybe_await(inp.click())
                await maybe_await(inp.fill(value))
                logger.debug("[Workday] Filled %s = '%s'", automation_id, value[:40])
                return True
        except Exception as exc:
            logger.debug("[Workday] fill %s failed: %s", automation_id, exc)
        return False

    async def _fill_textbox_by_label(
        self, page: Any, label_pattern: str, value: str,
    ) -> bool:
        """Fill a textbox found near a label matching ``label_pattern`` (case-insensitive)."""
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return False
        try:
            label_loc = locator_fn(f"text=/{label_pattern}/i").first
            if not await maybe_await(label_loc.is_visible()):
                return False
            # Walk up to the form-field container, then find the input
            form_field = label_loc.locator("xpath=ancestor::*[@data-automation-id and contains(@data-automation-id,'formField')]").first
            inp = form_field.locator("input, textarea").first
            if await maybe_await(inp.is_visible()):
                await maybe_await(inp.click())
                await maybe_await(inp.fill(value))
                logger.debug("[Workday] Filled label '%s' = '%s'", label_pattern, value[:40])
                return True
        except Exception:
            pass
        # Fallback: use Playwright's getByLabel
        try:
            inp = page.get_by_label(re.compile(label_pattern, re.IGNORECASE)).first
            if await maybe_await(inp.is_visible()):
                await maybe_await(inp.click())
                await maybe_await(inp.fill(value))
                logger.debug("[Workday] Filled (getByLabel) '%s' = '%s'", label_pattern, value[:40])
                return True
        except Exception:
            pass
        return False

    async def _select_workday_dropdown(
        self, page: Any, container_selector: str, option_text: str,
    ) -> bool:
        """
        Select an option from a Workday dropdown button.
        Clicks the dropdown button, waits for the listbox, then clicks the matching option.
        """
        locator_fn = getattr(page, "locator", None)
        if not locator_fn:
            return False
        try:
            btn = locator_fn(f"{container_selector} button[aria-haspopup='listbox']").first
            if not await maybe_await(btn.is_visible()):
                # Try the container itself as the button
                btn = locator_fn(f"{container_selector} [role='button']").first
            await maybe_await(btn.click(timeout=3000))
            await asyncio.sleep(0.8)

            # Look for the option in the listbox
            opt = locator_fn(f"[role='option']:has-text('{option_text}')").first
            if await maybe_await(opt.is_visible()):
                await maybe_await(opt.click(timeout=3000))
                logger.debug("[Workday] Selected dropdown option '%s'", option_text)
                return True

            # Try partial match
            options = locator_fn("[role='option']")
            count = await maybe_await(options.count())
            target_lower = option_text.lower()
            for i in range(count):
                opt_el = options.nth(i)
                text = (await maybe_await(opt_el.text_content()) or "").strip().lower()
                if target_lower in text:
                    await maybe_await(opt_el.click(timeout=3000))
                    logger.debug("[Workday] Selected partial-match dropdown '%s'", text[:40])
                    return True

            # Escape if nothing matched
            keyboard = getattr(page, "keyboard", None)
            if keyboard:
                await maybe_await(keyboard.press("Escape"))
        except Exception as exc:
            logger.debug("[Workday] dropdown select failed for %s: %s", container_selector, exc)
        return False

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

        # -- School or University (plain textbox) --
        school = entry.get("institution") or ""
        if school:
            try:
                inp = section.get_by_role("textbox", name="School or University")
                await maybe_await(inp.scroll_into_view_if_needed(timeout=5000))
                await maybe_await(inp.click(timeout=3000))
                await maybe_await(inp.fill(school))
                logger.debug("[Workday] Edu: School = '%s'", school)
            except Exception as exc:
                logger.debug("[Workday] Edu: School failed: %s", exc)

        # -- Degree dropdown (button → listbox → click option) --
        # From manual mapping: the button's accessible name includes "Degree"
        # (e.g. "Degree Select One Required"). Click it to open a listbox,
        # then directly click the matching option — no scrolling needed.
        degree = entry.get("studyType") or entry.get("degree") or ""
        if degree:
            # Map common values to exact Workday dropdown text (no apostrophes!)
            degree_map: dict[str, str] = {
                "bachelor of science": "Bachelor Degree",
                "bachelor of arts": "Bachelor Degree",
                "bs": "Bachelor Degree",
                "ba": "Bachelor Degree",
                "bachelor": "Bachelor Degree",
                "master of science": "Master Degree",
                "master of arts": "Master Degree",
                "ms": "Master Degree",
                "ma": "Master Degree",
                "master": "Master Degree",
                "phd": "PHD",
                "doctor of philosophy": "PHD",
                "doctorate": "PHD",
                "associate": "Associate Degree/College Diploma (DEC)",
                "associate of science": "Associate Degree/College Diploma (DEC)",
                "associate of arts": "Associate Degree/College Diploma (DEC)",
                "high school": "High School Diploma or Equivalent",
                "ged": "High School Diploma or Equivalent",
                "mba": "MBA",
            }
            wd_degree = degree_map.get(degree.lower(), degree)

            try:
                # Find the Degree button via role — its accessible name contains "Degree"
                degree_btn = section.get_by_role("button", name=re.compile(r"Degree", re.IGNORECASE)).first
                await maybe_await(degree_btn.scroll_into_view_if_needed(timeout=5000))
                await maybe_await(degree_btn.click(timeout=5000))
                await asyncio.sleep(0.8)

                # Click the matching option in the listbox directly (no scroll needed)
                opt = locator_fn(f"[role='option']:has-text('{wd_degree}')").first
                if await maybe_await(opt.is_visible(timeout=3000)):
                    await maybe_await(opt.click(timeout=3000))
                    logger.debug("[Workday] Edu: Degree = '%s'", wd_degree)
                else:
                    # Partial match fallback
                    options = locator_fn("[role='listbox'] [role='option']")
                    count = await maybe_await(options.count())
                    target_lower = wd_degree.lower()
                    for i in range(count):
                        opt_el = options.nth(i)
                        text = (await maybe_await(opt_el.text_content()) or "").strip().lower()
                        if target_lower in text:
                            await maybe_await(opt_el.click(timeout=3000))
                            logger.debug("[Workday] Edu: Degree partial match = '%s'", text)
                            break
                    else:
                        # Escape if nothing matched
                        keyboard = getattr(page, "keyboard", None)
                        if keyboard:
                            await maybe_await(keyboard.press("Escape"))
                        logger.debug("[Workday] Edu: Degree option '%s' not found", wd_degree)
            except Exception as exc:
                logger.debug("[Workday] Edu: Degree dropdown failed: %s", exc)

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
            # Try exact-ish match first, then first visible option
            options = locator_fn("[role='option']")
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

            # Dismiss the Field of Study dropdown by clicking a neutral element
            # (Escape alone doesn't close this Workday dropdown reliably)
            # Target: progress bar heading — always visible, never interactive
            try:
                neutral = locator_fn(
                    "[data-automation-id='progressBar'], "
                    "[data-automation-id='pageHeaderTitle'], h2"
                ).first
                await maybe_await(neutral.click(force=True, timeout=2000))
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
                            "(el) => { el.scrollIntoView({block: 'center'}); el.focus(); el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); }",
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
                all_opts = locator_fn("[role='option']")
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
                await maybe_await(page.evaluate("(el) => el.scrollIntoView({block: 'center'})", handle))
                await asyncio.sleep(0.3)
                # Use JS click — Workday React handlers sometimes don't respond to Playwright's click
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

        # ---- Step 4: Scroll, focus, and fill ----
        try:
            await maybe_await(url_inp.wait_for(state="visible", timeout=5000))
        except Exception:
            pass
        try:
            handle = await maybe_await(url_inp.element_handle(timeout=3000))
            if handle:
                await maybe_await(page.evaluate("(el) => { el.scrollIntoView({block: 'center'}); el.focus(); }", handle))
                await asyncio.sleep(0.3)
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
