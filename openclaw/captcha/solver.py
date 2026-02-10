from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request


def can_auto_solve() -> bool:
    return bool(os.getenv("TWOCAPTCHA_API_KEY"))


def try_solve_captcha(*, site_key: str | None, page_url: str, captcha_type: str) -> str | None:
    """
    Optional 2captcha integration.
    Returns a token string if solved, otherwise None.
    """
    api_key = os.getenv("TWOCAPTCHA_API_KEY")
    if not api_key or not site_key:
        return None

    if captcha_type not in {"recaptcha_v2", "hcaptcha"}:
        return None

    method = "userrecaptcha" if captcha_type == "recaptcha_v2" else "hcaptcha"
    submit_payload = {
        "key": api_key,
        "method": method,
        "googlekey": site_key,
        "pageurl": page_url,
        "json": 1,
    }

    request_id = _post_form("http://2captcha.com/in.php", submit_payload)
    if not request_id:
        return None

    for _ in range(24):
        time.sleep(5)
        result = _get_json(
            "http://2captcha.com/res.php"
            + "?"
            + urllib.parse.urlencode({"key": api_key, "action": "get", "id": request_id, "json": 1})
        )
        if not result:
            continue
        if result.get("status") == 1 and result.get("request"):
            return str(result["request"])
        if result.get("request") not in {"CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"}:
            return None
    return None


def _post_form(url: str, payload: dict[str, object]) -> str | None:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read()
            parsed = json.loads(body)
            if parsed.get("status") == 1:
                return str(parsed.get("request"))
    except Exception:
        return None
    return None


def _get_json(url: str) -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            body = response.read()
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        return None
    return None
