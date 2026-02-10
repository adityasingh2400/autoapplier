from __future__ import annotations

import json
import logging
import os
import re
import secrets
import string
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openclaw.utils import (
    capture_step,
    human_pause,
    maybe_await,
    smart_click,
    smart_fill,
)
from openclaw.gmail import fetch_recent_verification_email, GmailAuthError


logger = logging.getLogger(__name__)


AUTH_URL_FRAGMENTS = (
    "/login",
    "/log-in",
    "/signin",
    "/sign-in",
    "/auth",
    "/account/login",
    "/accounts/login",
    "/users/sign_in",
    "/session",
    "/sso",
)


@dataclass(slots=True)
class PasswordPolicy:
    min_length: int = 12
    require_upper: bool = True
    require_lower: bool = True
    require_digit: bool = True
    require_special: bool = True
    special_chars: str = "!@#$%^&*()-_=+[]{}:,.?/"


@dataclass(slots=True)
class AuthDetection:
    detected: bool
    kind: str = ""  # login|signup|two_factor|email_verification|oauth_only|unknown
    reason: str = ""
    url: str = ""
    host: str = ""
    has_oauth: bool = False


def _credentials_path() -> Path:
    return Path.home() / ".openclaw" / "credentials.json"


def _load_credentials() -> dict[str, dict[str, str]]:
    path = _credentials_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_credentials(data: dict[str, dict[str, str]]) -> None:
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Best-effort tighten perms. Ignore on platforms where it fails.
        path.parent.chmod(0o700)
    except Exception:
        pass

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except Exception:
        pass
    tmp.replace(path)


def _upsert_credentials(host: str, *, email: str, password: str) -> None:
    host = (host or "").strip()
    if not host:
        return
    data = _load_credentials()
    data[host] = {
        "email": email,
        "password": password,
        "created": date.today().isoformat(),
    }
    try:
        _write_credentials(data)
    except Exception as exc:
        logger.warning("Failed to persist credentials for %s: %s", host, exc)


def _get_credentials_for_host(host: str) -> dict[str, str] | None:
    host = (host or "").strip()
    if not host:
        return None
    data = _load_credentials()
    entry = data.get(host)
    return entry if isinstance(entry, dict) else None


def _safe_page_url(page: Any) -> str:
    try:
        value = getattr(page, "url", "")
        if callable(value):
            return str(value())
        return str(value or "")
    except Exception:
        return ""


def _host_for_url(url: str) -> str:
    try:
        return urlparse(url).netloc.strip().lower()
    except Exception:
        return ""


def _looks_like_auth_url(url: str) -> bool:
    url_lower = (url or "").lower()
    return any(fragment in url_lower for fragment in AUTH_URL_FRAGMENTS)


async def detect_auth_wall(page: Any, *, job_url: str | None = None) -> AuthDetection:
    url = _safe_page_url(page) or (job_url or "")
    host = _host_for_url(url)

    evaluate_fn = getattr(page, "evaluate", None)
    if evaluate_fn is None:
        if _looks_like_auth_url(url):
            return AuthDetection(
                detected=True,
                kind="unknown",
                reason="Auth URL detected",
                url=url,
                host=host,
            )
        return AuthDetection(detected=False, url=url, host=host)

    script = r"""
    () => {
      const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ").trim();
      const href = String(location.href || "");
      const path = norm(location.pathname || "");

      const pwInputs = Array.from(document.querySelectorAll("input[type='password']"));
      const pwCount = pwInputs.length;
      const hasPassword = pwCount > 0;

      const emailSel = [
        "input[type='email']",
        "input[name*='email' i]",
        "input[id*='email' i]",
        "input[autocomplete='username']",
        "input[name*='username' i]",
        "input[id*='username' i]"
      ].join(",");
      const hasEmail = !!document.querySelector(emailSel);

      const oauthWords = ["google", "linkedin", "github"];
      const hasOAuth = Array.from(document.querySelectorAll("button,a"))
        .slice(0, 200)
        .some((el) => {
          const t = norm(el?.innerText || "");
          if (!t) return false;
          const mentionsProvider = oauthWords.some((w) => t.includes(w));
          const mentionsAuth = (
            t.includes("sign in")
            || t.includes("log in")
            || t.includes("login")
            || t.includes("continue")
            || t.includes("sign up")
            || t.includes("register")
          );
          return mentionsProvider && mentionsAuth;
        });

      // Prefer scanning form text; fall back to a small excerpt from the page.
      const sources = Array.from(document.querySelectorAll("form")).slice(0, 2);
      let root = sources[0] || document.querySelector("main") || document.body || document.documentElement;
      let rawText = String(root?.textContent || "");
      if (rawText.length > 60000) rawText = rawText.slice(0, 60000);
      const text = norm(rawText);

      const hasConfirmPassword = (
        pwCount >= 2
        || text.includes("confirm password")
        || text.includes("re-enter password")
        || text.includes("reenter password")
      );

      const loginHints = (
        text.includes("sign in")
        || text.includes("log in")
        || text.includes("login")
        || text.includes("forgot password")
      );
      const signupHints = (
        text.includes("create account")
        || text.includes("sign up")
        || text.includes("signup")
        || text.includes("register")
        || text.includes("new user")
      );

      // 2FA / verification heuristics.
      const needs2fa = (
        text.includes("two-factor")
        || text.includes("two factor")
        || text.includes("2fa")
        || text.includes("authenticator")
        || text.includes("authentication code")
        || (text.includes("verification code") && (text.includes("sms") || text.includes("text message")))
      );

      const needsEmailVerify = (
        text.includes("check your email")
        || text.includes("verify your email")
        || text.includes("email verification")
        || text.includes("confirm your email")
        || (text.includes("we sent") && text.includes("email") && text.includes("code"))
      );

      return {
        href,
        path,
        hasPassword,
        hasEmail,
        hasConfirmPassword,
        loginHints,
        signupHints,
        hasOAuth,
        needs2fa,
        needsEmailVerify
      };
    }
    """

    try:
        raw = await maybe_await(evaluate_fn(script))
        if not isinstance(raw, dict):
            return AuthDetection(detected=_looks_like_auth_url(url), kind="unknown", url=url, host=host)
        has_password = bool(raw.get("hasPassword"))
        has_email = bool(raw.get("hasEmail"))
        has_confirm = bool(raw.get("hasConfirmPassword"))
        has_oauth = bool(raw.get("hasOAuth"))
        needs_2fa = bool(raw.get("needs2fa"))
        needs_email = bool(raw.get("needsEmailVerify"))
        login_hints = bool(raw.get("loginHints"))
        signup_hints = bool(raw.get("signupHints"))
        href = str(raw.get("href") or url)

        if needs_email:
            return AuthDetection(
                detected=True,
                kind="email_verification",
                reason="Email verification required",
                url=href,
                host=_host_for_url(href) or host,
                has_oauth=has_oauth,
            )
        if needs_2fa:
            return AuthDetection(
                detected=True,
                kind="two_factor",
                reason="Two-factor authentication required",
                url=href,
                host=_host_for_url(href) or host,
                has_oauth=has_oauth,
            )

        looks_url = _looks_like_auth_url(href)
        is_auth = looks_url or (has_password and (login_hints or signup_hints))
        if not is_auth:
            return AuthDetection(detected=False, url=href, host=_host_for_url(href) or host, has_oauth=has_oauth)

        if has_oauth and not has_password and not has_email:
            return AuthDetection(
                detected=True,
                kind="oauth_only",
                reason="OAuth-only login/signup detected",
                url=href,
                host=_host_for_url(href) or host,
                has_oauth=has_oauth,
            )

        if has_password and (signup_hints or has_confirm):
            return AuthDetection(
                detected=True,
                kind="signup",
                reason="Account creation required",
                url=href,
                host=_host_for_url(href) or host,
                has_oauth=has_oauth,
            )
        if has_password and login_hints:
            return AuthDetection(
                detected=True,
                kind="login",
                reason="Login required",
                url=href,
                host=_host_for_url(href) or host,
                has_oauth=has_oauth,
            )

        return AuthDetection(
            detected=True,
            kind="unknown",
            reason="Authentication wall detected",
            url=href,
            host=_host_for_url(href) or host,
            has_oauth=has_oauth,
        )
    except Exception:
        if _looks_like_auth_url(url):
            return AuthDetection(detected=True, kind="unknown", reason="Auth URL detected", url=url, host=host)
        return AuthDetection(detected=False, url=url, host=host)


def _infer_password_policy(text: str) -> PasswordPolicy:
    raw = (text or "").strip()
    lower = raw.lower()

    mins: list[int] = []
    for m in re.finditer(r"(?:at\s+least|min(?:imum)?)\s*(\d{1,2})\s*(?:characters|chars)", lower):
        try:
            mins.append(int(m.group(1)))
        except Exception:
            pass
    for m in re.finditer(r"(\d{1,2})\s*(?:characters|chars)\s*(?:minimum|min)", lower):
        try:
            mins.append(int(m.group(1)))
        except Exception:
            pass
    for m in re.finditer(r"(\d{1,2})\+\s*(?:characters|chars)", lower):
        try:
            mins.append(int(m.group(1)))
        except Exception:
            pass

    min_length = max(mins) if mins else 12
    require_upper = "uppercase" in lower or "capital letter" in lower
    # Many password policies implicitly require lowercase even if they don't spell it out.
    # Including at least one lowercase character is extremely unlikely to violate requirements.
    require_lower = True
    require_digit = "number" in lower or "digit" in lower
    require_special = "special character" in lower or "symbol" in lower or "special" in lower

    # If the page provides no explicit requirements, default to a strong password.
    if not any([require_upper, require_lower, require_digit, require_special]) and min_length <= 0:
        min_length = 12
        require_upper = True
        require_lower = True
        require_digit = True
        require_special = True

    # If nothing is mentioned but min length exists, still include digit+special by default to satisfy most sites.
    if not any([require_upper, require_lower, require_digit, require_special]):
        require_upper = True
        require_lower = True
        require_digit = True
        require_special = True

    return PasswordPolicy(
        min_length=max(8, int(min_length)),
        require_upper=require_upper,
        require_lower=require_lower,
        require_digit=require_digit,
        require_special=require_special,
    )


def _generate_password(policy: PasswordPolicy) -> str:
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    special = policy.special_chars

    required: list[str] = []
    if policy.require_upper:
        required.append(secrets.choice(upper))
    if policy.require_lower:
        required.append(secrets.choice(lower))
    if policy.require_digit:
        required.append(secrets.choice(digits))
    if policy.require_special:
        required.append(secrets.choice(special))

    # Allow all common character classes. We only control what is required,
    # since most sites accept extra variety in passwords.
    alphabet = upper + lower + digits + special

    target_len = max(policy.min_length, len(required), 12)
    remaining = [secrets.choice(alphabet) for _ in range(target_len - len(required))]
    chars = required + remaining
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


# ---------------------------------------------------------------------------
# Universal password pool  (12 chars, upper + lower + digit + special).
# These satisfy virtually every site's requirements on the first try.
# Only fall back to dynamic _generate_password if ALL of these fail.
# ---------------------------------------------------------------------------
_UNIVERSAL_PASSWORDS: list[str] = [
    "Kx9$mTqL2wZp",
    "Rv3!nBhJ7eFd",
    "Wz8@cYgP4sQm",
    "Hj5#kDxN1vLt",
    "Bf6&rUmC9wXa",
]

_pw_index = 0


def _next_universal_password() -> str:
    """Return the next password from the universal pool (round-robin)."""
    global _pw_index
    pw = _UNIVERSAL_PASSWORDS[_pw_index % len(_UNIVERSAL_PASSWORDS)]
    _pw_index += 1
    return pw


async def _extract_password_rule_text(page: Any) -> str:
    evaluate_fn = getattr(page, "evaluate", None)
    if evaluate_fn is None:
        return ""
    script = r"""
    () => {
      const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
      const pw = document.querySelector("input[type='password']");
      let root = pw?.closest("form") || pw?.closest("section") || pw?.parentElement || document.body;
      let text = norm(root?.textContent || document.body?.textContent || "");
      if (text.length > 12000) text = text.slice(0, 12000);
      return text;
    }
    """
    try:
        out = await maybe_await(evaluate_fn(script))
        return str(out or "")
    except Exception:
        return ""


async def _fill_all_matching(page: Any, selector: str, value: str) -> int:
    evaluate_fn = getattr(page, "evaluate", None)
    if evaluate_fn is None:
        return 0
    script = r"""
    (selector, value) => {
      const setNativeValue = (el, v) => {
        const tag = (el.tagName || "").toLowerCase();
        const proto = tag === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, "value");
        if (desc && typeof desc.set === "function") desc.set.call(el, v);
        else el.value = v;
      };
      const els = Array.from(document.querySelectorAll(selector));
      let count = 0;
      for (const el of els) {
        if (!el) continue;
        if (el.disabled || el.readOnly) continue;
        try {
          setNativeValue(el, value);
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          count += 1;
        } catch (e) {
          continue;
        }
      }
      return count;
    }
    """
    try:
        result = await maybe_await(evaluate_fn(script, selector, value))
        return int(result) if isinstance(result, int | float) else 0
    except Exception:
        return 0


async def _click_terms_checkboxes(page: Any) -> int:
    evaluate_fn = getattr(page, "evaluate", None)
    if evaluate_fn is None:
        return 0
    script = r"""
    () => {
      const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ").trim();
      const matches = (t) => {
        if (!t) return false;
        return (
          t.includes("terms")
          || t.includes("privacy")
          || t.includes("i agree")
          || t.includes("agree to")
          || t.includes("consent")
        );
      };

      let clicked = 0;
      const boxes = Array.from(document.querySelectorAll("input[type='checkbox']"));
      for (const cb of boxes) {
        if (!cb || cb.disabled) continue;
        if (cb.checked) continue;
        let label = "";
        if (cb.id) {
          const byFor = document.querySelector(`label[for="${CSS.escape(cb.id)}"]`);
          if (byFor?.innerText) label = byFor.innerText;
        }
        if (!label) {
          label = cb.closest("label")?.innerText || "";
        }
        const t = norm(label);
        if (!matches(t)) continue;
        try {
          cb.click();
          clicked += 1;
        } catch (e) {
          continue;
        }
      }
      return clicked;
    }
    """
    try:
        result = await maybe_await(evaluate_fn(script))
        return int(result) if isinstance(result, int | float) else 0
    except Exception:
        return 0


async def _wait_brief(page: Any, ms: int = 900) -> None:
    wait_fn = getattr(page, "wait_for_timeout", None)
    if wait_fn is not None:
        try:
            await maybe_await(wait_fn(ms))
            return
        except Exception:
            pass
    import asyncio

    await asyncio.sleep(ms / 1000)


async def _has_password_inputs(page: Any) -> bool:
    evaluate_fn = getattr(page, "evaluate", None)
    if evaluate_fn is None:
        return False
    try:
        raw = await maybe_await(evaluate_fn("() => document.querySelectorAll(\"input[type='password']\").length"))
        return int(raw) > 0
    except Exception:
        return False


def _default_gmail_client_secret_path() -> Path:
    """
    User-provided Google OAuth client secret file.
    We do not bundle it in-repo.
    """
    env = (os.getenv("OPENCLAW_GMAIL_CLIENT_SECRET_PATH") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".openclaw" / "gmail_client_secret.json"


async def _page_expects_verification_code(page: Any) -> bool:
    evaluate_fn = getattr(page, "evaluate", None)
    if evaluate_fn is None:
        return False
    script = r"""
    () => {
      const norm = (s) => (s || "").toLowerCase();
      const text = norm(document.body?.innerText || "");
      const hints = (
        text.includes("verification code") ||
        text.includes("enter code") ||
        text.includes("one-time") ||
        text.includes("one time") ||
        text.includes("otp") ||
        text.includes("passcode")
      );

      const inputs = Array.from(document.querySelectorAll("input"));
      const candidates = inputs.filter((el) => {
        const t = norm(el.getAttribute("type") || "");
        const n = norm(el.getAttribute("name") || "");
        const i = norm(el.getAttribute("id") || "");
        const a = norm(el.getAttribute("aria-label") || "");
        const p = norm(el.getAttribute("placeholder") || "");
        const hay = `${t} ${n} ${i} ${a} ${p}`;
        const looks = (
          hay.includes("otp") ||
          hay.includes("code") ||
          hay.includes("verify") ||
          hay.includes("passcode")
        );
        const typ = (t === "text" || t === "tel" || t === "number" || t === "");
        return typ && looks && !el.disabled && !el.readOnly;
      });
      const multiDigit = inputs.filter((el) => {
        const ml = Number(el.getAttribute("maxlength") || "0");
        return ml === 1;
      }).length >= 4;
      return hints || candidates.length > 0 || multiDigit;
    }
    """
    try:
        raw = await maybe_await(evaluate_fn(script))
        return bool(raw)
    except Exception:
        return False


async def _fill_verification_code(page: Any, code: str) -> bool:
    code = (code or "").strip()
    if not code:
        return False

    # Try multi-field OTP (4-8 inputs maxlength=1).
    evaluate_fn = getattr(page, "evaluate", None)
    if evaluate_fn is not None and code.isdigit() and len(code) >= 4:
        script = r"""
        (code) => {
          const digits = String(code).replace(/\D+/g, "").split("");
          const inputs = Array.from(document.querySelectorAll("input"))
            .filter((el) => {
              const ml = Number(el.getAttribute("maxlength") || "0");
              return ml === 1 && !el.disabled && !el.readOnly;
            })
            .slice(0, digits.length);
          if (inputs.length >= 4) {
            for (let i = 0; i < inputs.length && i < digits.length; i++) {
              const el = inputs[i];
              el.focus();
              el.value = digits[i];
              el.dispatchEvent(new Event("input", { bubbles: true }));
              el.dispatchEvent(new Event("change", { bubbles: true }));
            }
            return true;
          }
          return false;
        }
        """
        try:
            ok = await maybe_await(evaluate_fn(script, code))
            if bool(ok):
                # Submit/continue if present.
                await smart_click(
                    page,
                    prompt=None,
                    selectors=["button[type='submit']", "input[type='submit']"],
                    text_candidates=["Verify", "Continue", "Submit", "Next"],
                    prefer_prompt=False,
                )
                await _wait_brief(page, 1200)
                return True
        except Exception:
            pass

    # Try single input.
    filled = await smart_fill(
        page,
        prompt="",
        value=code,
        selectors=[
            "input[name*='otp' i]",
            "input[id*='otp' i]",
            "input[name*='code' i]",
            "input[id*='code' i]",
            "input[autocomplete='one-time-code']",
            "input[type='tel']",
            "input[type='text']",
        ],
        prefer_prompt=False,
    )
    if not filled:
        return False

    await smart_click(
        page,
        prompt=None,
        selectors=["button[type='submit']", "input[type='submit']"],
        text_candidates=["Verify", "Continue", "Submit", "Next"],
        prefer_prompt=False,
    )
    await _wait_brief(page, 1200)
    return True


async def _attempt_email_verification_via_gmail(
    page: Any,
    *,
    host: str,
    primary_email: str,
    alternate_email: str,
) -> bool:
    """
    Best-effort: pull a recent verification email and either:
    - fill a verification code, or
    - open a verification link
    """
    secret_path = _default_gmail_client_secret_path()
    if not secret_path.exists():
        return False

    # Use small hint set to keep Gmail query valid.
    hints = [host]
    try:
        parsed = urlparse(_safe_page_url(page))
        if parsed.netloc:
            hints.append(parsed.netloc)
    except Exception:
        pass

    # Prefer checking the primary mailbox, then alternate.
    accounts = [primary_email, alternate_email]
    for account in accounts:
        if not account:
            continue
        try:
            import asyncio

            msg = await asyncio.to_thread(
                fetch_recent_verification_email,
                client_secret_path=secret_path,
                account_email=account,
                hints=hints,
                max_age_sec=3600,
                max_results=12,
            )
        except Exception:
            msg = None

        if not msg:
            continue

        expects_code = await _page_expects_verification_code(page)
        codes = msg.extract_codes()
        links = msg.extract_links()

        if expects_code and codes:
            if await _fill_verification_code(page, codes[0]):
                return True

        # If it doesn't look like a code screen (or code fill failed), try link.
        if links:
            # Navigate in the same tab.
            goto_fn = getattr(page, "goto", None)
            if goto_fn:
                try:
                    await maybe_await(goto_fn(links[0], wait_until="domcontentloaded", timeout=60_000))
                except TypeError:
                    await maybe_await(goto_fn(links[0]))
                await _wait_brief(page, 1500)
                return True

        # Last chance: try code anyway.
        if codes:
            if await _fill_verification_code(page, codes[0]):
                return True

    return False


async def _attempt_login(page: Any, *, email: str, password: str) -> bool:
    # ---- Workday fast-path ----
    workday = await page.locator('[data-automation-id="email"]').count() > 0

    if workday:
        # Make sure we're on the Sign In form, not the Create Account form.
        # The Create Account form has a verifyPassword field.
        on_signup = await page.locator('[data-automation-id="verifyPassword"]').count() > 0
        if on_signup:
            logger.warning("Login: Workday layout detected but page is the Create Account form, not Sign In")
            # Try clicking Sign In link to switch views
            sign_in_link = page.locator('[data-automation-id="signInLink"]')
            if await sign_in_link.count() > 0:
                await sign_in_link.first.click(force=True, timeout=3000)
                await _wait_brief(page, 1000)
            else:
                return False

        logger.info("Login: detected Workday layout, using data-automation-id selectors")
        email_field = page.locator('[data-automation-id="email"]')
        await email_field.click(timeout=2000)
        await email_field.fill(email, timeout=2000)

        pw_field = page.locator('[data-automation-id="password"]')
        await pw_field.click(timeout=2000)
        await pw_field.fill("", timeout=1000)
        await pw_field.type(password, delay=30, timeout=10000)

        # Workday login submit — try signInSubmitButton first, fall back to generic
        for aid in ["signInSubmitButton", "signInLink"]:
            btn = page.locator(f'[data-automation-id="{aid}"]')
            if await btn.count() > 0:
                await btn.first.click(force=True, timeout=3000)
                break
    else:
        # ---- Generic path ----
        await _fill_all_matching(
            page,
            "input[type='email'], input[name*='email' i], input[id*='email' i], input[autocomplete='username'], input[name*='username' i], input[id*='username' i]",
            email,
        )
        await smart_fill(
            page,
            prompt="",
            value=email,
            selectors=["input[type='email']", "input[name*='email' i]", "input[id*='email' i]"],
            prefer_prompt=False,
        )

        if not await _has_password_inputs(page):
            await smart_click(
                page,
                prompt=None,
                selectors=["button:has-text('Continue')", "button:has-text('Next')"],
                text_candidates=["Continue", "Next"],
                prefer_prompt=False,
            )
            await _wait_brief(page, 650)

        await _fill_all_matching(page, "input[type='password']", password)
        await smart_fill(
            page,
            prompt="",
            value=password,
            selectors=["input[type='password']"],
            prefer_prompt=False,
        )

        await smart_click(
            page,
            prompt=None,
            selectors=[
                "button:has-text('Sign in')",
                "button:has-text('Log in')",
                "button:has-text('Login')",
                "input[type='submit']",
            ],
            text_candidates=["Sign in", "Log in", "Login", "Continue", "Next"],
            prefer_prompt=False,
        )

    await _wait_brief(page, 1100)
    state = await detect_auth_wall(page)
    return not state.detected


async def _switch_to_signup_if_possible(page: Any) -> bool:
    return await smart_click(
        page,
        prompt=None,
        selectors=[
            "a:has-text('Create account')",
            "a:has-text('Sign up')",
            "button:has-text('Create account')",
            "button:has-text('Sign up')",
            "a:has-text('Register')",
            "button:has-text('Register')",
        ],
        text_candidates=["Create account", "Sign up", "Register"],
        prefer_prompt=False,
    )


async def _switch_to_login_if_possible(page: Any) -> bool:
    """Switch from a signup page to the login/sign-in view."""
    return await smart_click(
        page,
        prompt=None,
        selectors=[
            "a:has-text('Sign in')",
            "a:has-text('Log in')",
            "a:has-text('Login')",
            "button:has-text('Sign in')",
            "button:has-text('Log in')",
        ],
        text_candidates=["Sign in", "Log in", "Login", "Already have an account"],
        prefer_prompt=False,
    )


async def _attempt_signup(page: Any, *, standard_fields: dict[str, str], email: str, password: str) -> bool:
    """
    Fast, direct account creation.

    Strategy:
    1.  Try Workday data-automation-id selectors first (instant, exact).
    2.  Fall back to generic selectors for non-Workday sites.
    """
    # ---- Workday fast-path (data-automation-id selectors) ----
    workday = await page.locator('[data-automation-id="email"]').count() > 0

    if workday:
        logger.info("Signup: detected Workday layout, using data-automation-id selectors")
        # Email
        email_field = page.locator('[data-automation-id="email"]')
        await email_field.click(timeout=2000)
        await email_field.fill(email, timeout=2000)

        # Password (type char-by-char so React validates each keystroke)
        pw_field = page.locator('[data-automation-id="password"]')
        await pw_field.click(timeout=2000)
        await pw_field.fill("", timeout=1000)
        await pw_field.type(password, delay=30, timeout=10000)

        # Verify password
        vpw_field = page.locator('[data-automation-id="verifyPassword"]')
        await vpw_field.click(timeout=2000)
        await vpw_field.fill("", timeout=1000)
        await vpw_field.type(password, delay=30, timeout=10000)

        # Consent checkbox
        cb = page.locator('[data-automation-id="createAccountCheckbox"]')
        if await cb.count() > 0:
            checked = await cb.is_checked()
            if not checked:
                await cb.click(timeout=2000)

        # Submit — force=True bypasses Workday's click_filter overlay interception
        await page.locator('[data-automation-id="createAccountSubmitButton"]').click(force=True, timeout=3000)

    else:
        # ---- Generic path for non-Workday sites ----
        logger.info("Signup: using generic selectors")
        await _switch_to_signup_if_possible(page)
        await _wait_brief(page, 600)

        # Name fields (best-effort, skip if not present)
        first_name = str(standard_fields.get("first_name") or "").strip()
        last_name = str(standard_fields.get("last_name") or "").strip()
        for sel, val in [
            ("input[name*='first' i], input[id*='first' i]", first_name),
            ("input[name*='last' i], input[id*='last' i]", last_name),
        ]:
            if val:
                loc = page.locator(sel).first
                try:
                    if await loc.count() > 0:
                        await loc.fill(val, timeout=2000)
                except Exception:
                    pass

        # Email
        await _fill_all_matching(
            page,
            "input[type='email'], input[name*='email' i], input[id*='email' i], input[autocomplete='email']",
            email,
        )

        # Passwords (fill all visible ones)
        pw_locs = page.locator("input[type='password']:visible")
        pw_count = await pw_locs.count()
        for i in range(pw_count):
            field = pw_locs.nth(i)
            try:
                await field.click(timeout=2000)
                await field.fill("", timeout=1000)
                await field.type(password, delay=30, timeout=10000)
            except Exception:
                pass

        # Terms / consent checkboxes
        await _click_terms_checkboxes(page)

        # Submit (avoid generic button[type='submit'] which matches nav buttons)
        submitted = False
        for sel in [
            "button:has-text('Create Account')",
            "button:has-text('Sign up')",
            "button:has-text('Register')",
            "input[type='submit']",
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click(timeout=3000)
                    submitted = True
                    break
            except Exception:
                continue
        if not submitted:
            return False

    # ---- Wait for server response ----
    await _wait_brief(page, 2500)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    await _wait_brief(page, 1500)
    state = await detect_auth_wall(page)
    return not state.detected or state.kind in {"email_verification", "two_factor"}


async def maybe_auto_authenticate(
    page: Any,
    *,
    job_url: str,
    standard_fields: dict[str, str],
    output_dir: Path,
    screenshots: list[str],
    human_in_loop: bool,
    pause_on_auth: bool,
    stage: str = "auth",
    max_wait_sec: int = 60,
) -> dict[str, Any]:
    """
    Best-effort authentication handler. It should be cheap when no auth wall is present.
    Returns:
      - ok: bool
      - performed: bool
      - reason: str (when not ok)
      - kind: str (when auth detected)
      - host: str
    """
    detection = await detect_auth_wall(page, job_url=job_url)
    if not detection.detected:
        logger.debug("No auth wall detected at stage %s.", stage)
        return {"ok": True, "performed": False, "kind": "", "host": detection.host}

    logger.info("Auth wall detected at stage %s: kind=%s, host=%s", stage, detection.kind, detection.host)
    host = detection.host or _host_for_url(job_url)
    email = str(standard_fields.get("email") or "").strip()
    if not email:
        return {
            "ok": False,
            "performed": False,
            "kind": detection.kind,
            "host": host,
            "reason": "Auth wall detected, but profile email is missing",
        }

    # Manual-only gates (we don't automate these).
    manual_kinds = {"two_factor", "email_verification", "oauth_only"}
    if detection.kind in manual_kinds:
        # Attempt Gmail-based email verification automatically when possible.
        if detection.kind == "email_verification":
            alternate_email = str(standard_fields.get("alternate_email") or "").strip()
            try:
                ok = await _attempt_email_verification_via_gmail(
                    page,
                    host=host,
                    primary_email=email,
                    alternate_email=alternate_email,
                )
            except GmailAuthError:
                ok = False
            except Exception:
                ok = False
            if ok:
                # If we're still stuck, fall through to normal manual pausing logic.
                now = await detect_auth_wall(page, job_url=job_url)
                if not now.detected:
                    return {"ok": True, "performed": True, "kind": "", "host": host}
                detection = now

        shot = await capture_step(page, output_dir, f"{stage}-{detection.kind}", screenshots)
        if human_in_loop and pause_on_auth and sys.stdin.isatty():
            msg = {
                "two_factor": "2FA required. Complete it manually (SMS/authenticator), then press Enter.",
                "email_verification": "Email verification required. If auto-verification didn't work, complete it (check email / enter code), then press Enter.",
                "oauth_only": "OAuth sign-in required. Complete it (popup), then press Enter.",
            }.get(detection.kind, "Authentication step required. Complete it, then press Enter.")
            print(
                f"\n{msg}\n- URL: {detection.url or job_url}\n- Screenshot: {shot or 'n/a'}\n- Artifacts: {output_dir}\n",
                file=sys.stderr,
            )
            user_input = (await human_pause("Continue after auth> ")).strip().lower()
            if user_input == "abort":
                return {
                    "ok": False,
                    "performed": False,
                    "kind": detection.kind,
                    "host": host,
                    "reason": "User aborted during authentication",
                }

            # Poll until auth wall clears (or we hit timeout).
            import asyncio

            deadline = asyncio.get_event_loop().time() + float(max_wait_sec)
            while asyncio.get_event_loop().time() < deadline:
                await _wait_brief(page, 900)
                now = await detect_auth_wall(page, job_url=job_url)
                if not now.detected:
                    return {"ok": True, "performed": True, "kind": "", "host": host}
                if now.kind in manual_kinds:
                    continue
                # If it turns into a plain login/signup wall, fall through to automation.
                detection = now
                break
        else:
            return {
                "ok": False,
                "performed": False,
                "kind": detection.kind,
                "host": host,
                "reason": detection.reason or "Manual authentication required",
            }

    performed = False
    creds = _get_credentials_for_host(host)
    if creds and creds.get("password"):
        performed = True

        # If we're on a signup page but have creds, switch to the login view first.
        if detection.kind == "signup":
            logger.info("Have saved creds but on signup page — switching to sign-in view first")
            is_wd = "workday" in host or "myworkdayjobs" in host
            switched = False
            if is_wd:
                sign_in_link = page.locator('[data-automation-id="signInLink"]')
                if await sign_in_link.count() > 0:
                    await sign_in_link.first.click(force=True, timeout=3000)
                    await _wait_brief(page, 1000)
                    switched = True
            if not switched:
                switched = await _switch_to_login_if_possible(page)
                if switched:
                    await _wait_brief(page, 650)
            if not switched:
                logger.warning("Could not switch to login view; will try login on current page")

        ok = await _attempt_login(page, email=creds.get("email") or email, password=creds["password"])
        if ok:
            return {"ok": True, "performed": True, "kind": "", "host": host}

        # If login leads to 2FA/email verification, we can pause.
        after = await detect_auth_wall(page, job_url=job_url)
        if after.detected and after.kind in manual_kinds:
            return await maybe_auto_authenticate(
                page,
                job_url=job_url,
                standard_fields=standard_fields,
                output_dir=output_dir,
                screenshots=screenshots,
                human_in_loop=human_in_loop,
                pause_on_auth=pause_on_auth,
                stage=stage,
                max_wait_sec=max_wait_sec,
            )
        return {
            "ok": False,
            "performed": True,
            "kind": detection.kind,
            "host": host,
            "reason": "Auto-login failed (credentials may be invalid or additional fields are required)",
        }

    # No credentials yet: attempt account creation.
    if detection.kind == "login":
        # Avoid submitting a random password into a login form (can trigger lockouts).
        switched = await _switch_to_signup_if_possible(page)
        if switched:
            await _wait_brief(page, 650)
            detection = await detect_auth_wall(page, job_url=job_url)
        if not switched and detection.kind != "signup":
            shot = await capture_step(page, output_dir, f"{stage}-login-no-creds", screenshots)
            return {
                "ok": False,
                "performed": False,
                "kind": detection.kind,
                "host": host,
                "reason": "Login required but no saved credentials were found for this domain",
                "screenshot": shot,
            }

    if detection.kind not in {"signup"}:
        shot = await capture_step(page, output_dir, f"{stage}-auth-unsupported", screenshots)
        return {
            "ok": False,
            "performed": False,
            "kind": detection.kind,
            "host": host,
            "reason": detection.reason or "Auth wall detected but automated sign-up is not supported for this page",
            "screenshot": shot,
        }

    performed = True

    # --- Try each universal password, fall back to dynamic generation ---
    password: str | None = None
    ok = False
    for _idx in range(len(_UNIVERSAL_PASSWORDS)):
        candidate = _next_universal_password()
        logger.info("Signup attempt %d/%d with universal password", _idx + 1, len(_UNIVERSAL_PASSWORDS))
        ok = await _attempt_signup(page, standard_fields=standard_fields, email=email, password=candidate)
        if ok:
            password = candidate
            break
        # Still on signup? Might be a validation error — try next password.
        after = await detect_auth_wall(page, job_url=job_url)
        if not after.detected or after.kind != "signup":
            password = candidate
            ok = True
            break
        logger.debug("Universal password %d rejected, trying next", _idx + 1)

    if not ok:
        # Last resort: dynamic password from site-specific rules.
        logger.info("All universal passwords failed; falling back to dynamic generation")
        policy_text = await _extract_password_rule_text(page)
        policy = _infer_password_policy(policy_text)
        password = _generate_password(policy)
        ok = await _attempt_signup(page, standard_fields=standard_fields, email=email, password=password)

    if ok:
        # Persist once signup appears to have succeeded (or advanced into verification/2FA).
        _upsert_credentials(host, email=email, password=password)

        # Workday post-signup: three possible outcomes —
        #   1) Direct to app (no auth wall)
        #   2) Sign-in page (just login, no verification needed)
        #   3) Verification required (check Gmail, then login)
        is_workday = "workday" in host or "myworkdayjobs" in host
        if is_workday:
            import asyncio
            logger.info("Workday signup succeeded — checking post-signup state...")
            await _wait_brief(page, 2000)

            after = await detect_auth_wall(page, job_url=job_url)

            # Case 1: Already in the app (no auth wall).
            if not after.detected:
                logger.info("Workday: no auth wall after signup — already in the app")
                return {"ok": True, "performed": True, "kind": "", "host": host}

            # Case 2: Sign-in page — try logging in directly first.
            if after.kind in {"login", "signup"}:
                logger.info("Workday: sign-in page after signup — attempting login...")
                login_ok = await _attempt_login(page, email=email, password=password)
                if login_ok:
                    return {"ok": True, "performed": True, "kind": "", "host": host}
                logger.info("Workday: direct login failed — may need email verification")

            # Case 3: Check Gmail for verification email (~6s window).
            alternate_email = str(standard_fields.get("alternate_email") or "").strip()
            await _wait_brief(page, 3000)  # give Workday time to send the email
            verified = False
            try:
                verified = await _attempt_email_verification_via_gmail(
                    page, host=host, primary_email=email, alternate_email=alternate_email,
                )
            except Exception:
                verified = False

            if verified:
                logger.info("Workday email verification succeeded — attempting login...")
                await _wait_brief(page, 2000)
                after = await detect_auth_wall(page, job_url=job_url)
                if not after.detected:
                    return {"ok": True, "performed": True, "kind": "", "host": host}
                if after.kind in {"login", "signup"}:
                    login_ok = await _attempt_login(page, email=email, password=password)
                    if login_ok:
                        return {"ok": True, "performed": True, "kind": "", "host": host}

            # If nothing worked automatically, pause for human.
            if human_in_loop and pause_on_auth and sys.stdin.isatty():
                shot = await capture_step(page, output_dir, f"{stage}-verify-email", screenshots)
                print(
                    f"\nWorkday account created. If email verification is needed,\n"
                    f"check {email}, click the link, then sign in.\n"
                    f"Press Enter when done.\n",
                    file=sys.stderr,
                )
                user_input = (await human_pause("Continue after auth> ")).strip().lower()
                if user_input == "abort":
                    return {
                        "ok": False, "performed": True, "kind": "email_verification",
                        "host": host, "reason": "User aborted during authentication",
                    }
                deadline = asyncio.get_event_loop().time() + float(max_wait_sec)
                while asyncio.get_event_loop().time() < deadline:
                    await _wait_brief(page, 900)
                    now = await detect_auth_wall(page, job_url=job_url)
                    if not now.detected:
                        return {"ok": True, "performed": True, "kind": "", "host": host}
                return {"ok": True, "performed": True, "kind": "", "host": host}

            return {
                "ok": False, "performed": True, "kind": "email_verification",
                "host": host, "reason": "Workday account created but could not complete authentication",
            }

        after = await detect_auth_wall(page, job_url=job_url)
        if after.detected and after.kind in manual_kinds:
            return await maybe_auto_authenticate(
                page,
                job_url=job_url,
                standard_fields=standard_fields,
                output_dir=output_dir,
                screenshots=screenshots,
                human_in_loop=human_in_loop,
                pause_on_auth=pause_on_auth,
                stage=stage,
                max_wait_sec=max_wait_sec,
            )
        return {"ok": True, "performed": True, "kind": "", "host": host}

    after = await detect_auth_wall(page, job_url=job_url)
    reason = after.reason or detection.reason or "Account creation failed"
    if after.detected and after.kind in manual_kinds:
        return await maybe_auto_authenticate(
            page,
            job_url=job_url,
            standard_fields=standard_fields,
            output_dir=output_dir,
            screenshots=screenshots,
            human_in_loop=human_in_loop,
            pause_on_auth=pause_on_auth,
            stage=stage,
            max_wait_sec=max_wait_sec,
        )

    return {
        "ok": False,
        "performed": performed,
        "kind": after.kind or detection.kind,
        "host": host,
        "reason": reason,
    }
