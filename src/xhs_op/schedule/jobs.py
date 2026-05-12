"""APScheduler job definitions for the XHS automation engine.

Three job groups:
  1. ``publish_due``   — every 5 min: publish approved drafts whose time has come.
  2. ``feeder_poll``   — every 30 min: pull RSS / X / XHS-competitor feeds.
  3. ``metrics_scrape``— three offset jobs (6 h / 24 h / 72 h after publish).

CLI:
  python -m xhs_op.schedule.jobs          # daemon mode
  python -m xhs_op.schedule.jobs --once   # run all three jobs once, then exit
  python -m xhs_op.schedule.jobs --help
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from xhs_op.db import Draft, Metric, Post, get_session
from xhs_op.publish.xhs_client import XSSignatureError, XhsPublisher
from xhs_op.schedule.cadence import can_publish_now, jitter_minutes
from xhs_op.sources.rss import fetch as rss_fetch
from xhs_op.sources.x_scraper import fetch as x_fetch
from xhs_op.sources.xhs_competitor import fetch as comp_fetch

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

# How far ahead of suggested_publish_at we start looking (covers jitter window).
_LOOKAHEAD_MINUTES: int = 15

# Metric age brackets (hours after post). We scrape at each bracket once.
_METRIC_BRACKETS_HOURS: list[int] = [6, 24, 72]

# Tolerance window around each bracket: accept a post that is within
# (bracket_h - 1) .. (bracket_h + 2) hours old to avoid double-counting.
_BRACKET_EARLY_SLACK_H: int = 1
_BRACKET_LATE_SLACK_H: int = 2


# ------------------------------------------------------------------
# Job 1: publish_due
# ------------------------------------------------------------------


def publish_due() -> None:
    """Publish all approved drafts whose scheduled time has arrived.

    Runs every 5 minutes. Groups drafts by account, checks cadence, calls
    XhsPublisher (falling back to Playwright on XSSignatureError). Errors
    on individual drafts are logged and swallowed so one bad draft cannot
    stall the rest.
    """
    now = datetime.now(timezone.utc)
    lookahead = now + timedelta(minutes=_LOOKAHEAD_MINUTES)

    with get_session() as session:
        due_drafts = session.exec(
            select(Draft).where(
                Draft.status == "scheduled",
                Draft.suggested_publish_at <= lookahead,
            )
        ).all()
        # Snapshot fields before the session closes — accessing ORM objects after
        # session.close() raises DetachedInstanceError.
        snapshots: list[tuple[int, str, datetime]] = [
            (d.id, d.account, d.suggested_publish_at)  # type: ignore[misc]
            for d in due_drafts
            if d.id is not None
        ]

    if not snapshots:
        logger.debug("publish_due: no scheduled drafts due")
        return

    logger.info("publish_due: found %d due draft(s)", len(snapshots))

    # Group by account so we do a single can_publish_now() check per account.
    by_account: dict[str, list[tuple[int, str, datetime]]] = {}
    for draft_id, account, suggested_at in snapshots:
        by_account.setdefault(account, []).append((draft_id, account, suggested_at))

    for account, account_snapshots in by_account.items():
        if not can_publish_now(account):
            logger.info(
                "publish_due: account=%s cadence check failed, skipping %d draft(s)",
                account,
                len(account_snapshots),
            )
            continue

        # Sort by suggested_publish_at so the earliest-due goes first.
        account_snapshots.sort(key=lambda t: t[2])

        # Only publish the first eligible draft per account per run
        # (can_publish_now re-evaluated after each successful publish would
        # block subsequent ones anyway — the 90-min rule enforces spacing).
        draft_id, _, suggested_at = account_snapshots[0]
        _publish_one(account, draft_id, suggested_at)


def _publish_one(account: str, draft_id: int, suggested_at: datetime) -> None:
    """Publish a single draft, falling back to Playwright on signature errors."""
    # Apply jitter: only publish if now is within the jitter window of the
    # suggested time. A draft that is more than 15 min *early* waits for the
    # next tick; one that is already past gets published immediately.
    now = datetime.now(timezone.utc)
    suggested = suggested_at
    if suggested.tzinfo is None:
        suggested = suggested.replace(tzinfo=timezone.utc)
    jitter = jitter_minutes()
    earliest = suggested + timedelta(minutes=jitter - _LOOKAHEAD_MINUTES)
    if now < earliest:
        logger.debug(
            "_publish_one: draft=%d too early (now=%s earliest=%s), skipping",
            draft_id,
            now.isoformat(),
            earliest.isoformat(),
        )
        return

    logger.info("_publish_one: publishing draft=%d account=%s", draft_id, account)
    try:
        publisher = XhsPublisher(account)
        note_id = publisher.publish_note(draft_id)
        logger.info(
            "_publish_one: draft=%d published as note_id=%s", draft_id, note_id
        )
    except XSSignatureError as exc:
        logger.warning(
            "_publish_one: draft=%d x-s signature error (%s); trying Playwright fallback",
            draft_id,
            exc,
        )
        try:
            from xhs_op.publish.playwright_fallback import PlaywrightPublisher

            fb = PlaywrightPublisher(account)
            note_id = fb.publish_note(draft_id)
            logger.info(
                "_publish_one: draft=%d Playwright fallback succeeded, note_id=%s",
                draft_id,
                note_id,
            )
        except Exception as fb_exc:
            logger.error(
                "_publish_one: draft=%d Playwright fallback also failed: %s",
                draft_id,
                fb_exc,
                exc_info=True,
            )
    except Exception as exc:
        logger.error(
            "_publish_one: draft=%d unexpected error: %s",
            draft_id,
            exc,
            exc_info=True,
        )


# ------------------------------------------------------------------
# Job 2: feeder_poll
# ------------------------------------------------------------------


def feeder_poll() -> None:
    """Pull all three feed sources. Runs every 30 minutes.

    Each feeder is called independently; errors on one do not prevent the
    others from running.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=30)
    logger.info("feeder_poll: pulling feeds since %s", since.isoformat())

    for name, fn in [
        ("rss", rss_fetch),
        ("x_scraper", x_fetch),
        ("xhs_competitor", comp_fetch),
    ]:
        try:
            count = fn(since)  # type: ignore[call-arg]
            logger.info("feeder_poll: %s inserted %d idea(s)", name, count)
        except Exception as exc:
            logger.error(
                "feeder_poll: %s raised %s — swallowing", name, exc, exc_info=True
            )


# ------------------------------------------------------------------
# Job 3: metrics_scrape
# ------------------------------------------------------------------


def metrics_scrape(bracket_hours: int) -> None:
    """Scrape engagement stats for posts that are ~bracket_hours old.

    Called separately for each bracket (6 h, 24 h, 72 h after publish).
    The bracket window is [bracket_hours - 1, bracket_hours + 2] hours to
    avoid double-counting across scheduler ticks.
    """
    now = datetime.now(timezone.utc)
    window_early = timedelta(hours=bracket_hours - _BRACKET_EARLY_SLACK_H)
    window_late = timedelta(hours=bracket_hours + _BRACKET_LATE_SLACK_H)

    # Posts whose age falls within the bracket window.
    cutoff_old = now - window_late   # posted_at must be >= this (not too old)
    cutoff_young = now - window_early  # posted_at must be <= this (old enough)

    with get_session() as session:
        candidate_posts = session.exec(
            select(Post).where(
                Post.posted_at >= cutoff_old,
                Post.posted_at <= cutoff_young,
            )
        ).all()

    if not candidate_posts:
        logger.debug("metrics_scrape(%dh): no posts in bracket window", bracket_hours)
        return

    logger.info(
        "metrics_scrape(%dh): checking %d post(s) in bracket window",
        bracket_hours,
        len(candidate_posts),
    )

    for post in candidate_posts:
        _scrape_post_metrics(post, bracket_hours=bracket_hours, now=now)


def _scrape_post_metrics(post: Post, *, bracket_hours: int, now: datetime) -> None:
    """Fetch engagement for one post and insert a Metric row if not already present."""
    assert post.id is not None

    # Check whether a Metric already exists for this post at this bracket.
    bracket_window_start = now - timedelta(hours=bracket_hours + _BRACKET_LATE_SLACK_H)
    bracket_window_end = now - timedelta(hours=bracket_hours - _BRACKET_EARLY_SLACK_H)

    with get_session() as session:
        existing = session.exec(
            select(Metric).where(
                Metric.post_id == post.id,
                Metric.measured_at >= bracket_window_start,
                Metric.measured_at <= bracket_window_end,
            )
        ).first()

    if existing is not None:
        logger.debug(
            "_scrape_post_metrics: post=%d already has %dh metric, skipping",
            post.id,
            bracket_hours,
        )
        return

    logger.info(
        "_scrape_post_metrics: scraping post=%d note_id=%s (%dh bracket)",
        post.id,
        post.xhs_note_id,
        bracket_hours,
    )

    try:
        publisher = XhsPublisher(post.account)
        raw: dict = publisher.client.get_note_by_id_from_html(post.xhs_note_id)
    except Exception as exc:
        logger.error(
            "_scrape_post_metrics: post=%d scrape failed: %s",
            post.id,
            exc,
            exc_info=True,
        )
        return

    # Extract metrics defensively — the response shape may vary across xhs lib versions.
    likes = _safe_int(raw, "likes", "like_count", "liked_count")
    saves = _safe_int(raw, "collected_count", "saves", "collect_count")
    comments = _safe_int(raw, "comments", "comment_count")
    shares = _safe_int(raw, "share_count", "shares", "shared_count")
    views = _safe_int(raw, "view_count", "views", "read_count") or None

    metric = Metric(
        post_id=post.id,
        measured_at=now,
        likes=likes,
        saves=saves,
        comments=comments,
        shares=shares,
        views=views,
    )

    try:
        with get_session() as session:
            session.add(metric)
        logger.info(
            "_scrape_post_metrics: post=%d (%dh) => likes=%d saves=%d comments=%d shares=%d views=%s",
            post.id,
            bracket_hours,
            likes,
            saves,
            comments,
            shares,
            views,
        )
    except Exception as exc:
        logger.error(
            "_scrape_post_metrics: post=%d DB write failed: %s",
            post.id,
            exc,
            exc_info=True,
        )


def _safe_int(d: dict, *keys: str) -> int:
    """Return the first int-castable value found under any of *keys*, else 0.

    Handles nested dicts one level deep (e.g. d['interact_info']['liked_count']).
    """
    for key in keys:
        val = d.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
        # One level of nesting.
        for sub_val in d.values():
            if isinstance(sub_val, dict):
                nested = sub_val.get(key)
                if nested is not None:
                    try:
                        return int(nested)
                    except (TypeError, ValueError):
                        pass
    return 0


# ------------------------------------------------------------------
# Daemon + CLI
# ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m xhs_op.schedule.jobs",
        description=(
            "XHS scheduler daemon. Runs three job groups: publish_due (5 min), "
            "feeder_poll (30 min), metrics_scrape (6/24/72 h brackets)."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run all jobs synchronously once and exit. "
            "Useful for smoke-testing without a long-running process."
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Set log level to DEBUG.",
    )
    return parser


def run_once() -> None:
    """Run all job functions exactly once, synchronously."""
    logger.info("run_once: publish_due")
    publish_due()

    logger.info("run_once: feeder_poll")
    feeder_poll()

    for bh in _METRIC_BRACKETS_HOURS:
        logger.info("run_once: metrics_scrape(%dh)", bh)
        metrics_scrape(bh)

    logger.info("run_once: complete")


def run_daemon() -> None:
    """Start the BackgroundScheduler and block until KeyboardInterrupt."""
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(timezone="UTC")

    # Job 1: publish every 5 minutes.
    scheduler.add_job(
        publish_due,
        trigger="interval",
        minutes=5,
        id="publish_due",
        max_instances=1,
        coalesce=True,
    )

    # Job 2: feed poll every 30 minutes.
    scheduler.add_job(
        feeder_poll,
        trigger="interval",
        minutes=30,
        id="feeder_poll",
        max_instances=1,
        coalesce=True,
    )

    # Job 3: metrics_scrape — one job per bracket, each runs every hour so
    # we catch posts that land in the bracket during any given tick.
    for bracket_h in _METRIC_BRACKETS_HOURS:
        scheduler.add_job(
            metrics_scrape,
            trigger="interval",
            hours=1,
            id=f"metrics_scrape_{bracket_h}h",
            kwargs={"bracket_hours": bracket_h},
            max_instances=1,
            coalesce=True,
        )

    scheduler.start()
    logger.info(
        "Scheduler started. Jobs: %s",
        [job.id for job in scheduler.get_jobs()],
    )

    try:
        while True:
            time.sleep(30)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler shutting down...")
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.once:
        run_once()
        return 0

    run_daemon()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
