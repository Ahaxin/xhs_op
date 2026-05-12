"""Comment engagement: poll XHS notifications, classify intent, draft replies.

This module is **read-and-draft-only**.  It never calls any write method on
XhsPublisher.  All replies stay at status='pending_approval' until the
dashboard explicitly sends them.

Public API
----------
poll_and_classify(account)  -> int          # new Comment rows inserted
run_poll_loop(interval_seconds)  -> None    # continuous loop over all accounts

CLI
---
python -m xhs_op.engage.reply [--once] [--account {banna,stock}] [--interval SECONDS]
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from sqlmodel import select

from xhs_op.config import get_settings
from xhs_op.db import Comment, Post, get_session
from xhs_op.generate.llm import complete
from xhs_op.publish.xhs_client import XSSignatureError, XhsPublisher

logger = logging.getLogger(__name__)

# XHS comment character limit.
_COMMENT_MAX_CHARS = 150
_TRUNCATION_SUFFIX = "…"  # ellipsis "…"

# Valid intent labels.
_VALID_INTENTS = {"question", "compliment", "rental_intent", "report_intent", "spam"}

# Safety guard: rental replies must not contain price patterns.
_PRICE_RE = re.compile(r"\d+元|\d+块|¥\d+|\$\d+")

# Notification kinds that carry comment/mention text we can act on.
_COMMENT_KINDS = {"get_mention_notifications"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _account_persona(account: str) -> str:
    """Map account name to the LLM persona used for drafting replies."""
    if account == "banna":
        return "villa"
    if account == "stock":
        return "stock_digest"
    # Fallback: caller should always pass a known account, but be defensive.
    return "bulk"


def _classify_intent(text: str, account: str) -> str:
    """Ask the LLM to classify a comment into one of the five intent labels.

    Returns one of: 'question', 'compliment', 'rental_intent',
    'report_intent', 'spam'.
    """
    if account == "banna":
        account_hint = (
            "This comment is on a villa / holiday-rental account.  "
            "Lean toward 'rental_intent' when the user asks about booking, "
            "availability, location, or price."
        )
    else:
        account_hint = (
            "This comment is on a stock/AI/crypto digest account.  "
            "Lean toward 'report_intent' when the user asks for a source, "
            "questions the accuracy of the post, or flags a potential error."
        )

    prompt = (
        "Classify the following XHS comment into exactly ONE of these labels:\n"
        "  question | compliment | rental_intent | report_intent | spam\n\n"
        "Rules:\n"
        "- 'question': the reader is asking for information or advice.\n"
        "- 'compliment': the reader is expressing appreciation or agreement.\n"
        "- 'rental_intent': the reader shows interest in renting / booking.\n"
        "- 'report_intent': the reader is questioning accuracy or asking for a source.\n"
        "- 'spam': off-topic, promotional, or abusive.\n\n"
        f"Comment:\n{text}\n\n"
        "Respond with exactly one label word (lowercase, no punctuation)."
    )

    raw = complete("bulk", prompt, extra_context=account_hint)
    first_word = raw.strip().split()[0].lower().rstrip(".,;:") if raw.strip() else ""
    return first_word if first_word in _VALID_INTENTS else "question"


def _draft_reply(comment_text: str, intent: str, account: str) -> str:
    """Generate a reply for the given comment and intent.

    Always returns a string of ≤150 characters (XHS limit).
    Never called for 'spam' intent.
    """
    persona = _account_persona(account)

    # Build intent-specific instructions.
    if intent == "rental_intent":
        intent_instructions = (
            "The reader is interested in renting / booking.  "
            "Your reply MUST include the call-to-action '私信我' (DM me).  "
            "NEVER mention any price, amount, or fee in ANY form.  "
            "NEVER include a phone number."
        )
    elif intent == "report_intent":
        intent_instructions = (
            "The reader is questioning accuracy or requesting a source.  "
            "Your reply MUST include the stock disclaimer: "
            "'本文仅供学习交流，不构成任何投资建议。'  "
            "Invite them to DM for more details."
        )
    elif intent == "question":
        intent_instructions = (
            "The reader asked a question.  Answer helpfully and concisely in "
            "1-2 sentences, staying in your persona voice."
        )
    elif intent == "compliment":
        intent_instructions = (
            "The reader complimented or agreed with the post.  Reply with a "
            "warm, short thank-you that stays in your persona voice."
        )
    else:
        intent_instructions = "Respond helpfully and briefly."

    prompt = (
        f"Write a reply to the following XHS comment.\n\n"
        f"Comment:\n{comment_text}\n\n"
        f"Instructions:\n{intent_instructions}\n\n"
        f"IMPORTANT: The reply must be no longer than {_COMMENT_MAX_CHARS} characters total.  "
        "Output only the reply text — no labels, no quotes, no explanations."
    )

    reply = complete(persona, prompt)

    # Enforce the character limit.
    if len(reply) > _COMMENT_MAX_CHARS:
        reply = reply[: _COMMENT_MAX_CHARS - 1] + _TRUNCATION_SUFFIX

    return reply


def _check_rental_safety(reply: str) -> bool:
    """Return True if the reply is safe to use for rental_intent (no price)."""
    return _PRICE_RE.search(reply) is None


def _extract_comments_from_notification(notification: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract individual comment records from a raw notification dict.

    The XHS notification payload is nested and undocumented; we walk the
    common shapes and collect whatever comment-like entries we find.

    Each returned dict has at a minimum:
        xhs_comment_id, author, text, xhs_note_id
    """
    kind = notification.get("kind", "")
    data = notification.get("data", {})

    # Only process mention-style notifications (which carry comment text).
    if kind not in _COMMENT_KINDS:
        return []

    # The ReaJason library may return the raw API response nested under 'data'.
    # Common shapes: top-level list, or dict with 'comments', 'notes', 'items',
    # 'mention_comment_list', etc.
    comments: list[dict[str, Any]] = []

    def _try_extract(obj: Any) -> None:  # noqa: ANN401
        """Recursively pull comment-shaped dicts from the payload."""
        if isinstance(obj, list):
            for item in obj:
                _try_extract(item)
            return
        if not isinstance(obj, dict):
            return

        # A comment-shaped dict has at least a comment id + text.
        comment_id = (
            obj.get("comment_id")
            or obj.get("id")
            or obj.get("noteid")
        )
        text = obj.get("content") or obj.get("text") or obj.get("comment_content") or ""
        author_info = obj.get("user_info") or obj.get("author") or {}
        if isinstance(author_info, str):
            author = author_info
        else:
            author = (
                author_info.get("nickname")
                or author_info.get("name")
                or author_info.get("user_id")
                or "unknown"
            )

        # The note id the comment belongs to.
        note_id = (
            obj.get("note_id")
            or obj.get("target_note_id")
            or obj.get("subject_note_id")
            or ""
        )

        if comment_id and text:
            comments.append(
                {
                    "xhs_comment_id": str(comment_id),
                    "author": str(author),
                    "text": str(text),
                    "xhs_note_id": str(note_id),
                }
            )
            return  # don't recurse deeper into a matched record

        # Not a leaf comment — recurse into children.
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _try_extract(v)

    _try_extract(data)
    return comments


def _find_post_id(xhs_note_id: str) -> int | None:
    """Look up Post.id by xhs_note_id; returns None if not found."""
    if not xhs_note_id:
        return None
    with get_session() as session:
        stmt = select(Post).where(Post.xhs_note_id == xhs_note_id)
        post = session.exec(stmt).first()
        return post.id if post else None


def _comment_exists(xhs_comment_id: str) -> bool:
    """Return True if a Comment row with this xhs_comment_id already exists."""
    with get_session() as session:
        stmt = select(Comment).where(Comment.xhs_comment_id == xhs_comment_id)
        return session.exec(stmt).first() is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def poll_and_classify(account: str) -> int:
    """Poll XHS for new comments on *account*, classify, draft replies, persist.

    Returns the number of new Comment rows inserted.
    """
    logger.info("[%s] polling notifications …", account)

    pub = XhsPublisher(account)
    notifications = pub.fetch_notifications()

    inserted = 0

    for notification in notifications:
        raw_comments = _extract_comments_from_notification(notification)
        for raw in raw_comments:
            xhs_comment_id = raw["xhs_comment_id"]
            author = raw["author"]
            text = raw["text"]
            xhs_note_id = raw["xhs_note_id"]

            # --- match to a Post row ---
            post_id = _find_post_id(xhs_note_id)
            if post_id is None:
                logger.debug(
                    "[%s] skipping comment %s — note_id %r not in DB",
                    account,
                    xhs_comment_id,
                    xhs_note_id,
                )
                continue

            # --- dedup ---
            if _comment_exists(xhs_comment_id):
                logger.debug(
                    "[%s] skipping comment %s — already in DB",
                    account,
                    xhs_comment_id,
                )
                continue

            # --- classify ---
            intent = _classify_intent(text, account)
            logger.info(
                "[%s] comment %s intent=%s author=%r",
                account,
                xhs_comment_id,
                intent,
                author,
            )

            # --- draft reply (skip for spam) ---
            drafted_reply: str | None = None
            if intent != "spam":
                drafted_reply = _draft_reply(text, intent, account)

                # Safety guard for rental_intent.
                if intent == "rental_intent" and not _check_rental_safety(drafted_reply):
                    logger.warning(
                        "[%s] rental reply for comment %s contained a price pattern — "
                        "stripping draft and falling back to a safe prompt",
                        account,
                        xhs_comment_id,
                    )
                    # Re-draft with an even stricter prompt.
                    safe_prompt = (
                        "Write a short, friendly reply to this XHS comment inviting the reader "
                        "to DM you ('私信我').  Do NOT mention any price, fee, or amount.  "
                        f"Comment:\n{text}"
                    )
                    drafted_reply = complete(_account_persona(account), safe_prompt)
                    if len(drafted_reply) > _COMMENT_MAX_CHARS:
                        drafted_reply = drafted_reply[: _COMMENT_MAX_CHARS - 1] + _TRUNCATION_SUFFIX
                    # If still failing the safety check, log and set to None so staff can draft.
                    if not _check_rental_safety(drafted_reply):
                        logger.error(
                            "[%s] comment %s rental reply still contains price after retry — "
                            "leaving drafted_reply=None for manual review",
                            account,
                            xhs_comment_id,
                        )
                        drafted_reply = None
            else:
                logger.info(
                    "[%s] comment %s classified as spam — no reply drafted",
                    account,
                    xhs_comment_id,
                )

            # --- persist ---
            comment = Comment(
                post_id=post_id,
                xhs_comment_id=xhs_comment_id,
                author=author,
                text=text,
                intent=intent,
                drafted_reply=drafted_reply,
                status="pending_approval",
                received_at=datetime.now(timezone.utc),
                replied_at=None,
            )
            with get_session() as session:
                session.add(comment)

            inserted += 1
            logger.info(
                "[%s] inserted Comment for xhs_comment_id=%s intent=%s",
                account,
                xhs_comment_id,
                intent,
            )

    logger.info("[%s] poll complete — %d new comment(s) inserted", account, inserted)
    return inserted


def run_poll_loop(interval_seconds: int = 900) -> None:
    """Continuously poll all accounts for new comments, sleeping between rounds.

    Catches KeyboardInterrupt for a clean exit.
    """
    settings = get_settings()
    accounts = list(settings.accounts.keys())
    logger.info("starting poll loop — accounts=%s interval=%ds", accounts, interval_seconds)

    try:
        while True:
            for account in accounts:
                try:
                    count = poll_and_classify(account)
                    logger.info("[%s] poll: %d new comment(s)", account, count)
                except XSSignatureError as exc:
                    logger.error("[%s] x-s signature error during poll: %s", account, exc)
                except Exception as exc:  # noqa: BLE001
                    logger.error("[%s] unexpected error during poll: %s", account, exc, exc_info=True)

            logger.debug("sleeping %ds before next poll …", interval_seconds)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        logger.info("poll loop interrupted — exiting cleanly")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m xhs_op.engage.reply",
        description=(
            "Poll XHS notifications, classify comment intent, and draft replies "
            "(read-and-draft-only — never publishes anything)."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll per account then exit (default: continuous loop).",
    )
    parser.add_argument(
        "--account",
        choices=["banna", "stock"],
        default=None,
        help="Poll only this account (default: all accounts).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=900,
        metavar="SECONDS",
        help="Poll interval in seconds when running in loop mode (default: 900 = 15 min).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    settings = get_settings()
    accounts_to_poll: list[str] = (
        [args.account] if args.account else list(settings.accounts.keys())
    )

    if args.once:
        for account in accounts_to_poll:
            try:
                count = poll_and_classify(account)
                logger.info("[%s] done — %d new comment(s)", account, count)
            except XSSignatureError as exc:
                logger.error("[%s] x-s signature error: %s", account, exc)
            except Exception as exc:  # noqa: BLE001
                logger.error("[%s] error: %s", account, exc, exc_info=True)
        return 0

    # Continuous loop (only makes sense for all accounts; if --account is set
    # we still loop, just over that single account).
    if args.account:
        # Override run_poll_loop's "all accounts" behaviour by calling
        # poll_and_classify in a hand-rolled loop for the single account.
        logger.info(
            "starting single-account poll loop — account=%s interval=%ds",
            args.account,
            args.interval,
        )
        try:
            while True:
                try:
                    count = poll_and_classify(args.account)
                    logger.info("[%s] poll: %d new comment(s)", args.account, count)
                except XSSignatureError as exc:
                    logger.error("[%s] x-s signature error: %s", args.account, exc)
                except Exception as exc:  # noqa: BLE001
                    logger.error("[%s] error: %s", args.account, exc, exc_info=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("poll loop interrupted — exiting cleanly")
        return 0

    run_poll_loop(interval_seconds=args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
