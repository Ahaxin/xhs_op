"""ReaJason/xhs publisher wrapper.

Per-account: loads cookies from data/cookies/<account>.json, attaches the
account's proxy to the underlying requests session, and exposes publish /
delete / notification APIs. x-s signature failures surface as
`XSSignatureError` so callers can fall back to the Playwright path.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xhs import XhsClient
from xhs.exception import DataFetchError, SignError

from xhs_op.config import AccountConfig, get_settings
from xhs_op.db import Draft, Post, get_session

# Hard XHS title cap.
TITLE_MAX_LEN = 20


class XSSignatureError(RuntimeError):
    """Raised when ReaJason/xhs reports an x-s signature mismatch.

    The CLI / scheduler catches this and switches to the Playwright fallback.
    """


def _cookie_str_from_file(cookie_path: Path) -> str:
    """Convert the Playwright cookies JSON dump into a `k=v; k=v` cookie header."""
    if not cookie_path.exists():
        raise FileNotFoundError(
            f"cookie file not found: {cookie_path}. Run scripts/login.py --account <name>."
        )
    raw = json.loads(cookie_path.read_text(encoding="utf-8"))
    pairs: list[str] = []
    for c in raw:
        name = c.get("name")
        value = c.get("value")
        if name and value is not None:
            pairs.append(f"{name}={value}")
    if not pairs:
        raise ValueError(f"cookie file {cookie_path} contained no usable cookies")
    return "; ".join(pairs)


def _proxies_dict(proxy_url: str | None) -> dict[str, str] | None:
    if not proxy_url:
        return None
    # requests-style mapping; supports http/https/socks5 schemes via httpx[socks] backend.
    return {"http": proxy_url, "https": proxy_url}


class XhsPublisher:
    """Per-account XHS publisher backed by ReaJason/xhs."""

    def __init__(self, account_name: str) -> None:
        settings = get_settings()
        if account_name not in settings.accounts:
            raise KeyError(f"unknown account '{account_name}'")
        self.account: AccountConfig = settings.accounts[account_name]
        self.account_name = account_name

        cookie_header = _cookie_str_from_file(self.account.cookie_path)
        proxies = _proxies_dict(self.account.proxy_url)

        # ReaJason/xhs builds its own requests.Session inside; we pass cookies + proxies.
        self.client = XhsClient(cookie=cookie_header, proxies=proxies)
        # Belt-and-braces: also push proxies onto the session in case the lib forgets.
        if proxies is not None:
            self.client.session.proxies.update(proxies)

    # ----- publish flow -----

    def publish_note(self, draft_id: int) -> str:
        """Publish an approved draft. Returns the xhs note id.

        On success: inserts a Post row and flips drafts.status to 'published'.
        Raises XSSignatureError when the upstream x-s signature is rejected.
        """
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

            # Snapshot fields while session is open; we'll write back after the API call.
            title = draft.title
            body = draft.body
            hashtags = list(draft.hashtags or [])
            image_paths = [str(Path(p)) for p in draft.image_paths]

        try:
            result: dict[str, Any] = self.client.create_image_note(
                title=title,
                desc=_compose_desc(body, hashtags),
                files=image_paths,
                topics=_topics_from_hashtags(hashtags),
                is_private=False,
            )
        except SignError as exc:
            raise XSSignatureError(str(exc) or "x-s signature mismatch") from exc
        except DataFetchError as exc:
            # SIGN_FAULT may surface as DataFetchError with code 300015 in the body.
            if "300015" in str(exc) or "x-s" in str(exc).lower():
                raise XSSignatureError(str(exc)) from exc
            raise

        note_id = _extract_note_id(result)
        if not note_id:
            raise RuntimeError(f"publish returned no note id; raw={result!r}")

        with get_session() as session:
            draft = session.get(Draft, draft_id)
            assert draft is not None  # checked above; row cannot vanish in normal flow
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

    # ----- delete -----

    def delete_note(self, note_id: str) -> None:
        """Delete a published note. ReaJason/xhs has no public `delete_note`
        method as of v0.2.x, so we call the underlying creator endpoint directly.
        If the endpoint shape changes upstream this will raise loudly — that's
        the desired signal to switch to the Playwright fallback for cleanup.
        """
        try:
            # Creator-center delete uri (matches the path used by create_note's web_api family).
            self.client.post(
                "/web_api/sns/capa/postnote/delete",
                {"note_id": note_id},
                headers={"Referer": "https://creator.xiaohongshu.com/"},
            )
        except SignError as exc:
            raise XSSignatureError(str(exc) or "x-s signature mismatch") from exc

    # ----- notifications (used by Task 8) -----

    def fetch_notifications(self) -> list[dict]:
        """Pull inbound comment / mention / like notifications.

        Returns the raw merged list from ReaJason/xhs notification endpoints so
        Task 8 can classify per-intent. We do not normalize here.
        """
        out: list[dict] = []
        for getter_name in (
            "get_mention_notifications",
            "get_like_notifications",
            "get_follow_notifications",
        ):
            getter = getattr(self.client, getter_name, None)
            if getter is None:
                continue
            try:
                raw = getter()
            except SignError as exc:
                raise XSSignatureError(str(exc) or "x-s signature mismatch") from exc
            if isinstance(raw, dict):
                out.append({"kind": getter_name, "data": raw})
            elif isinstance(raw, list):
                out.extend({"kind": getter_name, "data": item} for item in raw)
        return out


def _compose_desc(body: str, hashtags: list[str]) -> str:
    """XHS rendering convention: hashtags appended to the body separated by spaces."""
    if not hashtags:
        return body
    tag_str = " ".join(t if t.startswith("#") else f"#{t}" for t in hashtags)
    return f"{body}\n\n{tag_str}".strip()


def _topics_from_hashtags(hashtags: list[str]) -> list[dict]:
    """ReaJason/xhs accepts `topics=[{...}]`; we leave it empty when we don't
    have topic ids — appending `#tag` to desc still surfaces them in the feed.
    """
    return []


def _extract_note_id(result: dict[str, Any]) -> str | None:
    """Walk the create_image_note response for a note id under any of the
    known shapes ReaJason has returned across versions.
    """
    if not isinstance(result, dict):
        return None
    # Direct keys.
    for k in ("id", "note_id", "noteId"):
        if k in result and result[k]:
            return str(result[k])
    # Nested under `data` / `note`.
    for outer in ("data", "note"):
        sub = result.get(outer)
        if isinstance(sub, dict):
            for k in ("id", "note_id", "noteId"):
                if k in sub and sub[k]:
                    return str(sub[k])
    return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_smoke(args: argparse.Namespace) -> int:
    # scripts/ isn't a package — load the smoke module by path so this CLI
    # works whether invoked from the repo root or via `uv run`.
    import importlib.util

    repo_root = Path(__file__).resolve().parents[3]
    smoke_path = repo_root / "scripts" / "smoke_publish.py"
    spec = importlib.util.spec_from_file_location("smoke_publish", smoke_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load smoke module at {smoke_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return int(mod.run(account_name=args.account))


def _cmd_publish(args: argparse.Namespace) -> int:
    pub = XhsPublisher(args.account)
    try:
        note_id = pub.publish_note(args.draft_id)
    except XSSignatureError as exc:
        print(f"[publish] x-s signature failure: {exc}; falling back to Playwright", file=sys.stderr)
        # Lazy import — Playwright fallback can be heavy.
        from xhs_op.publish.playwright_fallback import PlaywrightPublisher

        fb = PlaywrightPublisher(args.account)
        note_id = fb.publish_note(args.draft_id)
    print(note_id)
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    pub = XhsPublisher(args.account)
    pub.delete_note(args.note_id)
    print(f"deleted {args.note_id}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m xhs_op.publish.xhs_client",
        description="XHS publisher CLI (ReaJason/xhs wrapper).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_smoke = sub.add_parser("smoke", help="post a throwaway test note then delete it")
    p_smoke.add_argument("--account", required=True, choices=["banna", "stock"])
    p_smoke.set_defaults(func=_cmd_smoke)

    p_pub = sub.add_parser("publish", help="publish an approved draft by id")
    p_pub.add_argument("--account", required=True, choices=["banna", "stock"])
    p_pub.add_argument("--draft-id", type=int, required=True)
    p_pub.set_defaults(func=_cmd_publish)

    p_del = sub.add_parser("delete", help="delete a published note by xhs note id")
    p_del.add_argument("--account", required=True, choices=["banna", "stock"])
    p_del.add_argument("--note-id", required=True)
    p_del.set_defaults(func=_cmd_delete)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
