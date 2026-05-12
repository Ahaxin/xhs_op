from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# Ensure src/ is importable when running as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from xhs_op.config import AccountConfig, get_settings  # type: ignore[import-not-found]  # noqa: E402

XHS_URL = "https://www.xiaohongshu.com/"
LOGIN_TIMEOUT_SEC = 600  # 10 minutes
POLL_INTERVAL_SEC = 2


def _profile_dir(account_name: str) -> Path:
    p = Path("data/.playwright") / account_name
    p.mkdir(parents=True, exist_ok=True)
    return p


def login(account: AccountConfig) -> Path:
    account.cookie_path.parent.mkdir(parents=True, exist_ok=True)
    profile = _profile_dir(account.name)

    launch_kwargs: dict = {"headless": False}
    if account.proxy_url:
        launch_kwargs["proxy"] = {"server": account.proxy_url}

    with sync_playwright() as p:
        # Persistent context keeps the fingerprint sticky across runs.
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            **launch_kwargs,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(XHS_URL)

        print(f"[{account.name}] Opened XHS. Scan the QR code on your phone to log in.")
        print(f"[{account.name}] Waiting up to {LOGIN_TIMEOUT_SEC}s for web_session cookie...")

        deadline = time.time() + LOGIN_TIMEOUT_SEC
        cookies: list = []
        while time.time() < deadline:
            cookies = list(context.cookies())
            if any(c.get("name") == "web_session" for c in cookies):
                break
            time.sleep(POLL_INTERVAL_SEC)
        else:
            context.close()
            raise TimeoutError(f"[{account.name}] login timed out; web_session not found")

        # Cookies are TypedDicts; serialize via dict() to be safe.
        serializable = [dict(c) for c in cookies]
        account.cookie_path.write_text(
            json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[{account.name}] Saved {len(cookies)} cookies -> {account.cookie_path}")
        context.close()
        return account.cookie_path


def main() -> None:
    parser = argparse.ArgumentParser(description="XHS QR-code login per account")
    parser.add_argument("--account", required=True, choices=["banna", "stock"])
    args = parser.parse_args()

    settings = get_settings()
    account = settings.accounts[args.account]
    login(account)


if __name__ == "__main__":
    main()
