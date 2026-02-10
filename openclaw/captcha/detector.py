from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from openclaw.utils import maybe_await


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CaptchaDetection:
    detected: bool
    captcha_type: str | None = None
    reason: str | None = None
    site_key: str | None = None


async def detect_captcha(page: Any) -> CaptchaDetection:
    logger.debug("Scanning page for CAPTCHA...")
    url = _safe_get_current_url(page)
    url_lower = url.lower()
    if "challenge" in url_lower and "cloudflare" in url_lower:
        return CaptchaDetection(
            detected=True,
            captcha_type="cloudflare_challenge",
            reason="Cloudflare challenge page URL detected",
        )

    scan = await _scan_dom_for_captcha(page)
    if scan.get("recaptcha"):
        return CaptchaDetection(
            detected=True,
            captcha_type="recaptcha_v2",
            reason="reCAPTCHA markers detected in page",
            site_key=scan.get("site_key"),
        )
    if scan.get("hcaptcha"):
        return CaptchaDetection(
            detected=True,
            captcha_type="hcaptcha",
            reason="hCaptcha markers detected in page",
            site_key=scan.get("site_key"),
        )
    if scan.get("cloudflare"):
        return CaptchaDetection(
            detected=True,
            captcha_type="cloudflare_challenge",
            reason="Cloudflare challenge markers detected in page",
        )
    if scan.get("verify_human"):
        return CaptchaDetection(
            detected=True,
            captcha_type="human_verification",
            reason="Human verification text detected in page",
        )

    return CaptchaDetection(detected=False)


def _safe_get_current_url(page: Any) -> str:
    try:
        value = getattr(page, "url", "")
        if callable(value):
            return str(value())
        return str(value or "")
    except Exception:
        return ""


async def _scan_dom_for_captcha(page: Any) -> dict[str, Any]:
    evaluate_fn = getattr(page, "evaluate", None)
    if evaluate_fn is None:
        return {}

    script = """
    () => {
      const bodyText = (document.body?.innerText || "").toLowerCase();
      const frameSrcs = Array.from(document.querySelectorAll("iframe"))
        .map((f) => (f.src || "").toLowerCase());
      const allHtml = (document.documentElement?.outerHTML || "").toLowerCase();

      const recaptchaPresent = frameSrcs.some((s) => s.includes("recaptcha")) || allHtml.includes("g-recaptcha");
      const hcaptchaPresent = frameSrcs.some((s) => s.includes("hcaptcha")) || allHtml.includes("h-captcha");

      // If a response token exists, treat CAPTCHA as solved to enable human-in-loop continuation.
      const recaptchaResponse = (
        document.getElementById("g-recaptcha-response")
        || document.querySelector("textarea[name='g-recaptcha-response']")
      )?.value || "";
      const hcaptchaResponse = (document.querySelector("textarea[name='h-captcha-response']")?.value) || "";
      const recaptchaSolved = recaptchaResponse.trim().length > 0;
      const hcaptchaSolved = hcaptchaResponse.trim().length > 0;

      const recaptcha = recaptchaPresent && !recaptchaSolved;
      const hcaptcha = hcaptchaPresent && !hcaptchaSolved;
      const cloudflare = allHtml.includes("cf-challenge") || allHtml.includes("challenge-platform");
      const verifyHuman = bodyText.includes("verify you're human")
        || bodyText.includes("verify you are human")
        || bodyText.includes("prove you are human")
        || bodyText.includes("security check");

      const recaptchaEl = document.querySelector(".g-recaptcha");
      const hcaptchaEl = document.querySelector(".h-captcha");
      const siteKey = recaptchaEl?.getAttribute("data-sitekey")
        || hcaptchaEl?.getAttribute("data-sitekey")
        || null;

      return {
        recaptcha,
        hcaptcha,
        recaptcha_present: recaptchaPresent,
        hcaptcha_present: hcaptchaPresent,
        recaptcha_solved: recaptchaSolved,
        hcaptcha_solved: hcaptchaSolved,
        cloudflare,
        verify_human: verifyHuman,
        site_key: siteKey
      };
    }
    """

    try:
        result = await maybe_await(evaluate_fn(script))
        if isinstance(result, dict):
            return result
    except Exception:
        return {}
    return {}
