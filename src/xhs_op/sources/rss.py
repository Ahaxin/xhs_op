from __future__ import annotations

# RSS / news aggregator feeder for xhs_op.
# Loads data/feeds.yaml, pulls each feed via feedparser, inserts new `ideas` rows.

import argparse
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import mktime
from typing import Any

import feedparser
import yaml
from sqlmodel import select

from xhs_op.db import Idea, get_session, init_db

# Polite UA so feeds don't 403 us.
feedparser.USER_AGENT = "xhs_op/0.1 (+rss-aggregator)"

# Default catalog path relative to project root.
DEFAULT_FEEDS_PATH = Path("data/feeds.yaml")

logger = logging.getLogger("xhs_op.sources.rss")


@dataclass(frozen=True)
class FeedSpec:
    # One row from feeds.yaml.
    name: str
    url: str
    category: str
    target_account: str
    language: str


def _load_feeds(feeds_path: Path) -> list[FeedSpec]:
    # Parse the YAML catalog into FeedSpec rows; raises if malformed.
    with feeds_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    raw = data.get("feeds") or []
    specs: list[FeedSpec] = []
    for item in raw:
        try:
            specs.append(
                FeedSpec(
                    name=str(item["name"]),
                    url=str(item["url"]),
                    category=str(item["category"]),
                    target_account=str(item["target_account"]),
                    language=str(item.get("language", "")),
                )
            )
        except KeyError as exc:
            logger.warning("skipping malformed feed entry %r (missing %s)", item, exc)
    return specs


def _stable_source_id(link: str) -> str:
    # sha1 of the entry link gives us a deterministic dedupe key.
    return hashlib.sha1(link.encode("utf-8", errors="replace")).hexdigest()


def _entry_published(entry: Any) -> datetime | None:
    # feedparser exposes parsed time tuples on a handful of attrs.
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = getattr(entry, attr, None) or (entry.get(attr) if isinstance(entry, dict) else None)
        if parsed:
            try:
                return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def _entry_body(entry: Any) -> str:
    # Best-effort body extraction: summary, description, or first content blob.
    for attr in ("summary", "description"):
        val = getattr(entry, attr, None) or (entry.get(attr) if isinstance(entry, dict) else None)
        if val:
            return str(val)
    content = getattr(entry, "content", None) or (entry.get("content") if isinstance(entry, dict) else None)
    if content:
        try:
            return str(content[0].get("value", ""))
        except (AttributeError, IndexError, KeyError, TypeError):
            return ""
    return ""


def _fetch_one(
    spec: FeedSpec,
    *,
    since: datetime | None,
    limit: int | None,
) -> tuple[int, int, int]:
    # Returns (entries_seen, inserted, skipped_dup_or_old). One bad feed cannot tank the run.
    try:
        parsed = feedparser.parse(spec.url)
    except Exception as exc:
        logger.warning("feed %s: parse error %s", spec.name, exc)
        return (0, 0, 0)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        logger.warning("feed %s: bozo=%s no entries", spec.name, parsed.get("bozo_exception"))
        return (0, 0, 0)

    entries = parsed.entries or []
    if limit is not None:
        entries = entries[:limit]

    seen = len(entries)
    inserted = 0
    skipped = 0

    try:
        with get_session() as session:
            for entry in entries:
                link = getattr(entry, "link", None) or (entry.get("link") if isinstance(entry, dict) else None)
                if not link:
                    skipped += 1
                    continue
                published = _entry_published(entry)
                if since is not None and published is not None and published < since:
                    skipped += 1
                    continue

                source_id = _stable_source_id(link)
                # Dedupe on (source, source_id) — cheap query, indexed on source_id.
                existing = session.exec(
                    select(Idea.id).where(Idea.source == "rss", Idea.source_id == source_id)
                ).first()
                if existing is not None:
                    skipped += 1
                    continue

                title = getattr(entry, "title", None) or (entry.get("title") if isinstance(entry, dict) else "") or ""
                body = _entry_body(entry)
                published_str = ""
                raw_pub = getattr(entry, "published", None) or (entry.get("published") if isinstance(entry, dict) else None)
                if raw_pub:
                    published_str = str(raw_pub)
                elif published is not None:
                    published_str = published.isoformat()

                idea = Idea(
                    source="rss",
                    source_url=str(link),
                    source_id=source_id,
                    raw_title=str(title),
                    raw_body=body,
                    raw_lang=spec.language,
                    engagement_score=0.0,
                    fetched_at=datetime.now(timezone.utc),
                    category=spec.category,
                    target_account=spec.target_account,
                    extra={
                        "feed_name": spec.name,
                        "published": published_str,
                    },
                    processed=False,
                )
                session.add(idea)
                inserted += 1
    except Exception as exc:
        # Roll back is handled by get_session(); just log and report no inserts.
        logger.warning("feed %s: db error %s", spec.name, exc)
        return (seen, 0, seen)

    logger.info(
        "feed %s: seen=%d inserted=%d skipped=%d",
        spec.name,
        seen,
        inserted,
        skipped,
    )
    return (seen, inserted, skipped)


def fetch(
    since: datetime | None = None,
    *,
    feeds_path: Path | None = None,
    only_feed: str | None = None,
    limit: int | None = None,
) -> int:
    """Pull each feed, insert new Idea rows. Returns total count inserted.

    - Loads feeds.yaml (default: data/feeds.yaml).
    - For each feed, calls feedparser.parse, dedupes by sha1(link), skips entries
      older than `since` (if provided), inserts fresh ideas in one transaction per feed.
    - Network/parse failures on a single feed are logged and swallowed.
    """
    path = feeds_path or DEFAULT_FEEDS_PATH
    if not path.exists():
        raise FileNotFoundError(f"feeds catalog not found: {path}")

    # Make sure tables exist before we try to insert.
    init_db()

    specs = _load_feeds(path)
    if only_feed:
        specs = [s for s in specs if s.name == only_feed]
        if not specs:
            logger.warning("no feed named %r in %s", only_feed, path)
            return 0

    total_inserted = 0
    for spec in specs:
        _, inserted, _ = _fetch_one(spec, since=since, limit=limit)
        total_inserted += inserted
    logger.info("rss.fetch done: %d new ideas across %d feeds", total_inserted, len(specs))
    return total_inserted


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xhs_op.sources.rss", description="RSS feeder for xhs_op")
    p.add_argument("--once", action="store_true", help="run fetch() once and exit")
    p.add_argument("--limit", type=int, default=None, help="max entries to process per feed")
    p.add_argument("--feed", type=str, default=None, help="only fetch the feed with this name")
    p.add_argument(
        "--feeds-path",
        type=Path,
        default=None,
        help=f"path to feeds.yaml (default: {DEFAULT_FEEDS_PATH})",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="DEBUG-level logging")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.once:
        # Only --once is supported for now; spell it out so we don't silently no-op.
        logging.error("must pass --once (continuous mode is owned by the scheduler)")
        return 2
    inserted = fetch(feeds_path=args.feeds_path, only_feed=args.feed, limit=args.limit)
    logging.info("inserted %d new idea(s)", inserted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
