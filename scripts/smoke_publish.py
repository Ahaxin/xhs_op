"""Smoke test: publish a throwaway note then delete it.

Builds the Draft row in-memory only — we still need a row in the DB because
`XhsPublisher.publish_note` reads by id and writes a Post FK back to it, but
we delete the row in a `finally` so nothing leaks.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a plain script (scripts/ is not a package).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from xhs_op.db import Draft, Post, get_session, init_db  # noqa: E402
from xhs_op.publish.xhs_client import XhsPublisher, XSSignatureError  # noqa: E402

GENERATED_DIR = Path("data/assets/generated")
SMOKE_IMAGE_NAME = "smoke_1x1.png"


def _ensure_image() -> Path:
    """Return any existing PNG under data/assets/generated/, else create a 1x1 white PNG."""
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(GENERATED_DIR.glob("*.png"))
    if existing:
        print(f"[smoke] reusing existing image: {existing[0]}")
        return existing[0]

    from PIL import Image  # local import — pillow is in deps but lazy-load for fast --help

    out = GENERATED_DIR / SMOKE_IMAGE_NAME
    Image.new("RGB", (1, 1), color=(255, 255, 255)).save(out, format="PNG")
    print(f"[smoke] generated 1x1 white PNG: {out}")
    return out


def _create_throwaway_draft(account: str, image_path: Path) -> int:
    """Insert a minimal pending Draft row, return its id. Caller is responsible for deletion."""
    draft = Draft(
        idea_id=None,
        account=account,
        persona="villa" if account == "banna" else "stock_digest",
        title="test ignore",  # ≤20 char
        body="automated smoke test - will be deleted",
        hashtags=[],
        image_paths=[str(image_path)],
        suggested_publish_at=datetime.now(timezone.utc),
        status="approved",
        inspiration_note="smoke_publish.py throwaway",
    )
    with get_session() as session:
        session.add(draft)
        session.flush()
        draft_id = draft.id
        assert draft_id is not None
    print(f"[smoke] created throwaway draft id={draft_id} for account={account}")
    return draft_id


def _cleanup_draft_row(draft_id: int) -> None:
    """Remove the smoke draft (and any Post row pointing at it) from the DB."""
    with get_session() as session:
        for post in list(session.exec(_select_posts_for_draft(draft_id))):  # type: ignore[arg-type]
            session.delete(post)
        draft = session.get(Draft, draft_id)
        if draft is not None:
            session.delete(draft)
    print(f"[smoke] cleaned up DB rows for draft id={draft_id}")


def _select_posts_for_draft(draft_id: int):
    from sqlmodel import select

    return select(Post).where(Post.draft_id == draft_id)


def run(account_name: str) -> int:
    print(f"[smoke] starting smoke publish for account={account_name}")
    init_db()  # idempotent — guarantees tables exist before insert.
    image_path = _ensure_image()
    draft_id = _create_throwaway_draft(account_name, image_path)

    note_id: str | None = None
    try:
        publisher = XhsPublisher(account_name)
        print("[smoke] publishing note...")
        try:
            note_id = publisher.publish_note(draft_id)
        except XSSignatureError as exc:
            print(f"[smoke] x-s signature failure: {exc}; falling back to Playwright")
            from xhs_op.publish.playwright_fallback import PlaywrightPublisher

            note_id = PlaywrightPublisher(account_name).publish_note(draft_id)
        print(f"[smoke] published note_id={note_id}")

        print("[smoke] sleeping 5s before delete...")
        time.sleep(5)

        print(f"[smoke] deleting note_id={note_id}...")
        publisher.delete_note(note_id)
        print("[smoke] delete OK")
        return 0
    except Exception as exc:  # surface as non-zero exit
        print(f"[smoke] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            _cleanup_draft_row(draft_id)
        except Exception as exc:
            print(f"[smoke] cleanup warning: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="XHS smoke publish (throwaway note + delete).")
    parser.add_argument("--account", required=True, choices=["banna", "stock"])
    args = parser.parse_args(argv)
    return run(args.account)


if __name__ == "__main__":
    raise SystemExit(main())
