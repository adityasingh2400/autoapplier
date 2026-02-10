from __future__ import annotations

import argparse
import json
from pathlib import Path

from openclaw.gmail import GmailAuthError, interactive_oauth_setup


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="OpenClaw Gmail OAuth setup helper")
    p.add_argument(
        "--client-secret",
        required=True,
        help="Path to Google OAuth client secret JSON (Desktop app recommended).",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        token = interactive_oauth_setup(client_secret_path=Path(args.client_secret).expanduser())
        print(
            json.dumps(
                {
                    "status": "ok",
                    "email": token.email,
                    "stored": True,
                },
                indent=2,
            )
        )
        return 0
    except GmailAuthError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

