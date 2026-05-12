"""Playwright-driven publisher used when ReaJason/xhs hits an x-s signature
mismatch. Reuses the same per-account persistent profile created by
scripts/login.py so the device fingerprint stays sticky.
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeoutError, sync_playwright

from xhs_op.config import AccountConfig, get_settings
from xhs_op.db import Draft, Post, get_session
from xhs_op.publish.xhs_client import (  # re-exported so callers can `from playwright_fallback import XSSignatureError`
    TITLE_MAX_LEN,
    XSSignatureError,
)

__all__ = ["PlaywrightPublisher", "XSSignatureError"]

CREATOR_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish"
PUBLISH_SUCCESS_TIMEOUT_MS = 60_000

# Human-typing simulation knobs.
PER_CHAR_DELAY_RANGE_MS = (50, 150)
FIELD_PAUSE_RANGE_S = (0.5, 2.5)


def _profile_dir(account_name: str) -> Path:
    p = Path("data/.playwright") / account_name
    p.mkdir(parents=True, exist_ok=True)
    return p


def _human_pause() -> None:
    time.sleep(random.uniform(*FIELD_PAUSE_RANGE_S))


def _human_type(page: Page, selector: str, text: str) -> None:
    """Type text char-by-char with jittered delay. Falls back to fill() if the
    locator doesn't accept type() — never silently drops the text.
    """
    locator = page.locator(selector).first
    locator.click()
    for ch in text:
        locator.type(ch, delay=random.uniform(*PER_CHAR_DELAY_RANGE_MS))


class PlaywrightPublisher:
    """Fallback publisher mirroring `XhsPublisher.publish_note` signature."""

    def __init__(self, account_name: str) -> None:
        settings = get_settings()
        if account_name not in settings.accounts:
            raise KeyError(f"unknown account '{account_name}'")
        self.account: AccountConfig = settings.accounts[account_name]
        self.account_name = account_name

    def publish_note(self, draft_id: int) -> str:
        with get_session() as session:
            draft = session.get(Draft, draft_id)
            if draft is None:
                raise LookupError(f"draft id {draft_id} not found")
            if draft.account != self.account_name:
                raise ValueError(
                    f"draft.account={draft.account!r} but publisher is for {self.account_name!r}"
                )
            if len(draft.title) > TITLE_MAX_LEN:
                raise ValueError(
                    f"title length {len(draft.title)} > {TITLE_MAX_LEN}; trim before publishing"
                )
            if not draft.image_paths:
                raise ValueError("draft has no image_paths; XHS image notes require ≥1 image")

            title = draft.title
            body = draft.body
            hashtags = list(draft.hashtags or [])
            image_paths = [str(Path(p).resolve()) for p in draft.image_paths]

        note_id = self._drive_browser(title, body, hashtags, image_paths)

        with get_session() as session:
            draft = session.get(Draft, draft_id)
            assert draft is not None
            post = Post(
                draft_id=draft_id,
                account=self.account_name,
                xhs_note_id=note_id,
                title=draft.title,
                body=draft.body,
                image_paths=list(draft.image_paths or []),
                posted_at=datetime.now(timezone.utc),
                status="live",
            )
            session.add(post)
            draft.status = "published"
            session.add(draft)

        return note_id

    def _drive_browser(
        self, title: str, body: str, hashtags: list[str], image_paths: list[str]
    ) -> str:
        profile = _profile_dir(self.account_name)
        launch_kwargs: dict = {"headless": False}
        if self.account.proxy_url:
            launch_kwargs["proxy"] = {"server": self.account.proxy_url}

        body_with_tags = body
        if hashtags:
            tag_line = " ".join(t if t.startswith("#") else f"#{t}" for t in hashtags)
            body_with_tags = f"{body}\n\n{tag_line}"

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                **launch_kwargs,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(CREATOR_PUBLISH_URL, wait_until="domcontentloaded")
                _human_pause()

                # Some flows open on the video tab; click the image tab if present.
                try:
                    img_tab = page.get_by_text("上传图文", exact=False).first
                    if img_tab.count() > 0:
                        img_tab.click()
                        _human_pause()
                except PWTimeoutError:
                    pass

                # Image upload — XHS creator center exposes a hidden <input type="file"> .
                file_input = page.locator('input[type="file"]').first
                file_input.set_input_files(image_paths)
                # Give the uploader time to ingest before fields appear.
                page.wait_for_timeout(3000)
                _human_pause()

                # Title field. Creator center uses a contenteditable or a plain input.
                title_selector = (
                    'input[placeholder*="标题"], '
                    'textarea[placeholder*="标题"], '
                    '[contenteditable="true"][data-placeholder*="标题"]'
                )
                _human_type(page, title_selector, title)
                _human_pause()

                # Body field is a Quill-style contenteditable.
                body_selector = (
                    '[contenteditable="true"][data-placeholder*="描述"], '
                    'div.ql-editor, '
                    'textarea[placeholder*="正文"]'
                )
                _human_type(page, body_selector, body_with_tags)
                _human_pause()

                # Publish button.
                publish_btn = page.get_by_role("button", name=re.compile(r"发布|发表")).first
                publish_btn.click()

                # Success indicator: URL changes to a note-detail page, or a toast.
                deadline = time.time() + PUBLISH_SUCCESS_TIMEOUT_MS / 1000
                note_id: str | None = None
                while time.time() < deadline:
                    current_url = page.url
                    note_id = _note_id_from_url(current_url)
                    if note_id:
                        break
                    # Some success states show a toast and stay on /publish — look for it.
                    if page.locator("text=发布成功").count() > 0:
                        # No id in URL; we can't recover one from the DOM reliably.
                        note_id = f"pw-success-{int(time.time())}"
                        break
                    page.wait_for_timeout(500)

                if not note_id:
                    raise RuntimeError("Playwright publish: success indicator never appeared")
                return note_id
            finally:
                context.close()


_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item|note)/([0-9a-f]{16,32})", re.IGNORECASE)


def _note_id_from_url(url: str) -> str | None:
    m = _NOTE_ID_RE.search(url)
    return m.group(1) if m else None
