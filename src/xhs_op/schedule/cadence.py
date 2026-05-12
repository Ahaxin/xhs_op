"""Cadence enforcement for the XHS scheduler.

Exposes:
- ``DAILY_QUOTA`` — max posts per account per UTC calendar day (patch in tests).
- ``can_publish_now(account)`` — anti-ban predicate.
- ``jitter_minutes()`` — ±15-minute random offset for scheduled times.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from xhs_op.db import Post, get_session

logger = logging.getLogger(__name__)

# First-month warmup: at most 1 post per account per UTC calendar day.
# Named constant so verifiers can patch it: `cadence.DAILY_QUOTA = 2`.
DAILY_QUOTA: int = 1

# Minimum gap between consecutive posts on the same account (minutes).
_MIN_GAP_MINUTES: int = 90


def can_publish_now(account: str) -> bool:
    """Return True when it is safe to publish for *account* right now.

    Rules (all must pass):
    1. The most-recent post on this account must be ≥ 90 minutes old.
    2. The number of posts published today (UTC) must be < DAILY_QUOTA.
    """
    now = datetime.now(timezone.utc)

    with get_session() as session:
        # --- Rule 1: 90-minute gap ---
        recent_post = session.exec(
            select(Post)
            .where(Post.account == account)
            .order_by(Post.posted_at.desc())  # type: ignore[attr-defined]
            .limit(1)
        ).first()

        if recent_post is not None:
            # posted_at may be stored without timezone info (SQLite strips it);
            # treat naive datetimes as UTC.
            posted_at = recent_post.posted_at
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
            age = now - posted_at
            if age < timedelta(minutes=_MIN_GAP_MINUTES):
                logger.debug(
                    "can_publish_now(%s)=False: last post %.1f min ago (need %d)",
                    account,
                    age.total_seconds() / 60,
                    _MIN_GAP_MINUTES,
                )
                return False

        # --- Rule 2: daily quota ---
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)

        today_count = len(
            session.exec(
                select(Post).where(
                    Post.account == account,
                    Post.posted_at >= today_start,
                    Post.posted_at < today_end,
                )
            ).all()
        )

        if today_count >= DAILY_QUOTA:
            logger.debug(
                "can_publish_now(%s)=False: daily quota reached (%d/%d)",
                account,
                today_count,
                DAILY_QUOTA,
            )
            return False

    logger.debug("can_publish_now(%s)=True", account)
    return True


def jitter_minutes() -> int:
    """Return a random integer offset in [-15, +15] minutes.

    Jobs add this to the scheduled time so posts don't hit at exactly
    the same clock time every day, which looks more human to XHS.
    """
    return random.randint(-15, 15)
