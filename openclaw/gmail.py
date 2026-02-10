from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"


class GmailAuthError(RuntimeError):
    pass


def _openclaw_dir() -> Path:
    return Path.home() / ".openclaw"


def _tokens_dir() -> Path:
    return _openclaw_dir() / "gmail_tokens"


def _seen_path() -> Path:
    return _openclaw_dir() / "gmail_seen.json"


def _tighten_path_permissions(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except Exception:
        pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _tighten_path_permissions(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    _tighten_path_permissions(tmp, 0o600)
    tmp.replace(path)


def _urlsafe_b64decode(data: str) -> bytes:
    s = (data or "").encode("ascii", "ignore")
    # Gmail uses base64url without padding.
    pad = b"=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sha256_b64url(raw: str) -> str:
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


@dataclass(slots=True)
class OAuthClient:
    client_id: str
    client_secret: str | None


def load_google_oauth_client(client_secret_path: Path) -> OAuthClient:
    """
    Accepts the OAuth client secret JSON that Google provides for "Desktop app" (installed) or "Web application".
    We only need client_id and client_secret.
    """
    data = _read_json(client_secret_path)
    root: dict[str, Any] = {}
    if isinstance(data.get("installed"), dict):
        root = data["installed"]
    elif isinstance(data.get("web"), dict):
        root = data["web"]
    else:
        root = data

    client_id = str(root.get("client_id") or "").strip()
    client_secret = str(root.get("client_secret") or "").strip() if root.get("client_secret") else None
    if not client_id:
        raise GmailAuthError(f"Invalid Google OAuth client secret file: {client_secret_path}")
    return OAuthClient(client_id=client_id, client_secret=client_secret)


@dataclass(slots=True)
class GmailToken:
    email: str
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds
    scope: str


def _token_path_for_email(email: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.@+-]+", "_", (email or "").strip())
    return _tokens_dir() / f"token_{safe}.json"


def list_configured_gmail_accounts() -> list[str]:
    out: list[str] = []
    d = _tokens_dir()
    if not d.exists():
        return out
    for p in d.glob("token_*.json"):
        data = _read_json(p)
        email = str(data.get("email") or "").strip()
        if email:
            out.append(email)
    return sorted(set(out))


def load_token_for_email(email: str) -> GmailToken | None:
    path = _token_path_for_email(email)
    data = _read_json(path)
    if not data:
        return None
    try:
        return GmailToken(
            email=str(data.get("email") or email).strip(),
            access_token=str(data.get("access_token") or "").strip(),
            refresh_token=str(data.get("refresh_token") or "").strip(),
            expires_at=float(data.get("expires_at") or 0),
            scope=str(data.get("scope") or "").strip() or GMAIL_READONLY_SCOPE,
        )
    except Exception:
        return None


def save_token(token: GmailToken) -> None:
    path = _token_path_for_email(token.email)
    _write_json_atomic(
        path,
        {
            "email": token.email,
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": token.expires_at,
            "scope": token.scope,
        },
    )


def _refresh_access_token(client: OAuthClient, refresh_token: str, scope: str) -> dict[str, Any]:
    payload: dict[str, str] = {
        "client_id": client.client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if client.client_secret:
        payload["client_secret"] = client.client_secret
    resp = requests.post(GOOGLE_TOKEN_URL, data=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("access_token"):
        raise GmailAuthError("Failed to refresh Gmail access token.")
    return data


def _get_gmail_profile(access_token: str) -> dict[str, Any]:
    resp = requests.get(
        f"{GMAIL_API_BASE}/users/me/profile",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def ensure_access_token(client: OAuthClient, token: GmailToken) -> GmailToken:
    now = time.time()
    # Refresh if within 60s of expiry.
    if token.access_token and token.expires_at and (token.expires_at - now) > 60:
        return token

    refreshed = _refresh_access_token(client, token.refresh_token, token.scope)
    expires_in = float(refreshed.get("expires_in") or 3600)
    token.access_token = str(refreshed.get("access_token") or token.access_token)
    token.expires_at = now + max(60.0, expires_in)
    return token


def interactive_oauth_setup(
    *,
    client_secret_path: Path,
    scope: str = GMAIL_READONLY_SCOPE,
    timeout_sec: int = 300,
) -> GmailToken:
    """
    One-time Gmail OAuth setup. Opens a browser for user consent and saves a refresh token.
    The resulting token is stored under ~/.openclaw/gmail_tokens/ token_<email>.json
    """
    client = load_google_oauth_client(client_secret_path)
    state = secrets.token_urlsafe(20)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _sha256_b64url(code_verifier)

    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query or "")
                if qs.get("state", [""])[0] != state:
                    raise GmailAuthError("OAuth state mismatch.")
                code = qs.get("code", [""])[0]
                if not code:
                    raise GmailAuthError("OAuth code missing.")
                result["code"] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body>Gmail connected. You can close this tab.</body></html>")
            except Exception as exc:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(str(exc).encode("utf-8", "ignore"))
            finally:
                done.set()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Silence noisy HTTP server logs.
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = int(server.server_address[1])
    redirect_uri = f"http://127.0.0.1:{port}/"

    auth_params = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(auth_params)}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            webbrowser.open(auth_url, new=1, autoraise=True)
        except Exception:
            pass

        if not done.wait(timeout=float(timeout_sec)):
            raise GmailAuthError("Timed out waiting for Gmail OAuth callback.")

        code = result.get("code", "").strip()
        if not code:
            raise GmailAuthError("OAuth authorization failed (no code received).")

        payload: dict[str, str] = {
            "client_id": client.client_id,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if client.client_secret:
            payload["client_secret"] = client.client_secret
        resp = requests.post(GOOGLE_TOKEN_URL, data=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise GmailAuthError("Unexpected OAuth token response.")

        access_token = str(data.get("access_token") or "").strip()
        refresh_token = str(data.get("refresh_token") or "").strip()
        expires_in = float(data.get("expires_in") or 3600)
        if not access_token or not refresh_token:
            raise GmailAuthError(
                "OAuth did not return required tokens. Make sure you granted consent and requested offline access."
            )

        profile = _get_gmail_profile(access_token)
        email = str(profile.get("emailAddress") or "").strip()
        if not email:
            # Fallback: store under a generic placeholder.
            email = "unknown@gmail"

        token = GmailToken(
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + max(60.0, expires_in),
            scope=scope,
        )
        save_token(token)
        return token
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass


def _load_seen() -> dict[str, list[str]]:
    data = _read_json(_seen_path())
    out: dict[str, list[str]] = {}
    for k, v in (data or {}).items():
        if isinstance(v, list):
            out[str(k)] = [str(x) for x in v if str(x)]
    return out


def _mark_seen(email: str, message_id: str) -> None:
    email = (email or "").strip()
    message_id = (message_id or "").strip()
    if not email or not message_id:
        return
    data = _load_seen()
    items = data.get(email, [])
    if message_id not in items:
        items.append(message_id)
    # Keep this bounded.
    data[email] = items[-200:]
    _write_json_atomic(_seen_path(), data)


@dataclass(slots=True)
class VerificationEmail:
    account_email: str
    message_id: str
    subject: str
    from_email: str
    received_ts: int
    body_text: str

    def extract_codes(self) -> list[str]:
        text = self.body_text or ""
        candidates: list[str] = []
        # Contextual patterns first.
        for m in re.finditer(r"(?i)(?:verification|confirm|one[- ]time|otp|code)\D{0,25}(\d{4,8})", text):
            candidates.append(m.group(1))
        # Fallback: any 6-digit number.
        for m in re.finditer(r"(?<!\d)(\d{6})(?!\d)", text):
            candidates.append(m.group(1))
        # De-dupe preserve order.
        seen: set[str] = set()
        out: list[str] = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def extract_links(self) -> list[str]:
        text = self.body_text or ""
        links: list[str] = []
        for m in re.finditer(r"https?://[^\s<>()\"']+", text):
            url = m.group(0).rstrip(".,);]")
            if len(url) < 12:
                continue
            links.append(url)
        # Prefer links that look like verification/magic/login links.
        def score(u: str) -> int:
            ul = u.lower()
            s = 0
            for w in ("verify", "verification", "confirm", "activate", "magic", "login", "sign-in", "signin"):
                if w in ul:
                    s += 10
            return s

        links.sort(key=score, reverse=True)
        # De-dupe.
        seen: set[str] = set()
        out: list[str] = []
        for u in links:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out


def _decode_payload_text(payload: dict[str, Any]) -> str:
    """
    Decode a Gmail message payload into best-effort plain text.
    """
    if not isinstance(payload, dict):
        return ""

    mime = str(payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    data = body.get("data")
    if isinstance(data, str) and data:
        try:
            raw = _urlsafe_b64decode(data).decode("utf-8", "ignore")
        except Exception:
            raw = ""
        if "text/html" in mime:
            return _html_to_text(raw)
        return raw

    parts = payload.get("parts") or []
    if not isinstance(parts, list):
        return ""

    # Prefer text/plain parts, then html.
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        txt = _decode_payload_text(part)
        pm = str(part.get("mimeType") or "").lower()
        if "text/plain" in pm:
            if txt.strip():
                plain_chunks.append(txt)
        elif "text/html" in pm:
            if txt.strip():
                html_chunks.append(txt)
        else:
            # Recurse into nested multiparts.
            if txt.strip():
                plain_chunks.append(txt)

    if plain_chunks:
        return "\n\n".join(plain_chunks)
    if html_chunks:
        return "\n\n".join(html_chunks)
    return ""


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _html_to_text(html: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", html or "")
    s = _TAG_RE.sub(" ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = _WS_RE.sub(" ", s)
    return s.strip()


def _headers_map(payload: dict[str, Any]) -> dict[str, str]:
    headers = payload.get("headers") or []
    out: dict[str, str] = {}
    if not isinstance(headers, list):
        return out
    for h in headers:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "").strip().lower()
        value = str(h.get("value") or "").strip()
        if name and value:
            out[name] = value
    return out


def _gmail_get(access_token: str, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{GMAIL_API_BASE}{path}"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params or None,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def fetch_recent_verification_email(
    *,
    client_secret_path: Path,
    account_email: str,
    hints: list[str],
    max_age_sec: int = 3600,
    max_results: int = 10,
) -> VerificationEmail | None:
    """
    Pull recent messages and pick the best candidate that looks like a verification email.
    Requires that OAuth has been set up for the account and a token exists.
    """
    token = load_token_for_email(account_email)
    if token is None:
        return None
    client = load_google_oauth_client(client_secret_path)
    token = ensure_access_token(client, token)
    save_token(token)

    # Keep query broad: verification emails vary widely by sender.
    q_parts = [
        f"newer_than:{max(1, int(max_age_sec // 60))}m",
        "(subject:verify OR subject:verification OR subject:confirm OR subject:code OR subject:otp OR subject:\"one time\" OR subject:\"one-time\")",
    ]
    # Add hint keywords (best-effort; must be small or Gmail query rejects it).
    compact_hints: list[str] = []
    for h in hints or []:
        s = str(h or "").strip()
        if not s:
            continue
        s = s[:60]
        # Avoid quote breaking.
        s = s.replace('"', "")
        compact_hints.append(s)
        if len(compact_hints) >= 3:
            break
    for h in compact_hints:
        q_parts.append(f"({h})")
    q = " ".join(q_parts)

    data = _gmail_get(token.access_token, "/users/me/messages", params={"q": q, "maxResults": max_results})
    msgs = data.get("messages") or []
    if not isinstance(msgs, list):
        return None

    seen = _load_seen().get(account_email, [])
    candidates: list[VerificationEmail] = []

    for item in msgs:
        if not isinstance(item, dict):
            continue
        msg_id = str(item.get("id") or "").strip()
        if not msg_id or msg_id in seen:
            continue

        full = _gmail_get(token.access_token, f"/users/me/messages/{msg_id}", params={"format": "full"})
        payload = full.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        headers = _headers_map(payload)
        subject = headers.get("subject", "")
        from_email = headers.get("from", "")
        internal_ts = int(full.get("internalDate") or 0)
        received_ts = int(internal_ts // 1000) if internal_ts else 0
        body_text = _decode_payload_text(payload)
        if not body_text.strip():
            # Fall back to snippet if payload decoding fails.
            body_text = str(full.get("snippet") or "")
        candidates.append(
            VerificationEmail(
                account_email=account_email,
                message_id=msg_id,
                subject=subject,
                from_email=from_email,
                received_ts=received_ts,
                body_text=body_text,
            )
        )

    if not candidates:
        return None

    def score(msg: VerificationEmail) -> int:
        text = f"{msg.subject}\n{msg.from_email}\n{msg.body_text}".lower()
        s = 0
        for w in ("verify", "verification", "confirm", "otp", "one-time", "one time", "code"):
            if w in text:
                s += 5
        for h in compact_hints:
            if h.lower() in text:
                s += 8
        # Prefer newest.
        s += int(msg.received_ts / 60)
        return s

    candidates.sort(key=score, reverse=True)
    best = candidates[0]
    _mark_seen(best.account_email, best.message_id)
    return best

