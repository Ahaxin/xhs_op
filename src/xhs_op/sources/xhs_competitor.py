"""XHS competitor / banna-villa tracker.

Pulls top XHS posts under a curated watchlist (banna villas + luxury hotels),
writes them as `Idea` rows tagged `source='xhs_competitor'`. The downstream
`xhs_op.generate.inspirer` reads these rows and produces *inspired* drafts for
the @banna-villa account.

Two client backends:

- ``MediaCrawlerXhsClient`` — wraps NanmiCoder/MediaCrawler. MediaCrawler is
  NOT a pip package; the user must clone it under ``external/MediaCrawler/``
  and follow its own setup. We import it lazily so this module can be imported
  on a fresh checkout. If the clone is missing, instantiating the client raises
  a clean ``RuntimeError`` with install instructions.
- ``StubXhsClient`` — reads canned rows from
  ``data/fixtures/xhs_competitor_stub.json``. Selected automatically when
  ``XHS_COMPETITOR_STUB=1`` is set in the environment. Used by the verifier to
  exercise the pipeline without MediaCrawler.

Both backends implement:

    _search_keyword(keyword, limit, lookback_days) -> list[dict]
    _fetch_creator_posts(handle, limit) -> list[dict]

Each post dict has the keys documented in
``data/fixtures/xhs_competitor_stub.json`` under ``_schema``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml
from sqlmodel import select

from xhs_op.db import Idea, get_session, init_db

logger = logging.getLogger("xhs_op.sources.xhs_competitor")

DEFAULT_WATCHLIST_PATH = Path("data/competitor_watchlist.yaml")
DEFAULT_STUB_FIXTURE_PATH = Path("data/fixtures/xhs_competitor_stub.json")

# Engagement normalization caps per category. Picked so that the very best
# posts in each category map roughly to 100 and the median sits well below.
# Used by _engagement_score() to clamp to 0..100.
_CATEGORY_ENGAGEMENT_CAP: dict[str, float] = {
    "banna_villa": 20_000.0,
    "luxury_hotel": 40_000.0,
}
_DEFAULT_ENGAGEMENT_CAP = 20_000.0


# ----- watchlist parsing ----------------------------------------------------


@dataclass(frozen=True)
class WatchGroup:
    """One group out of competitor_watchlist.yaml."""

    name: str  # 'banna_villa' or 'luxury_hotel' — used as Idea.category
    target_account: str
    limit_per_keyword: int
    lookback_days: int
    keywords: list[str] = field(default_factory=list)
    creators: list[str] = field(default_factory=list)


def _load_watchlist(path: Path) -> list[WatchGroup]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    raw_groups = (data.get("groups") or {}) if isinstance(data, dict) else {}
    groups: list[WatchGroup] = []
    for gname, body in raw_groups.items():
        if not isinstance(body, dict):
            logger.warning("watchlist group %r malformed, skipping", gname)
            continue
        try:
            groups.append(
                WatchGroup(
                    name=str(gname),
                    target_account=str(body.get("target_account") or "banna"),
                    limit_per_keyword=int(body.get("limit_per_keyword", 50)),
                    lookback_days=int(body.get("lookback_days", 7)),
                    keywords=[str(k) for k in (body.get("keywords") or [])],
                    creators=[str(c) for c in (body.get("creators") or [])],
                )
            )
        except (TypeError, ValueError) as exc:
            logger.warning("watchlist group %r could not be parsed: %s", gname, exc)
    return groups


def _find_group(groups: list[WatchGroup], name: str) -> WatchGroup | None:
    for g in groups:
        if g.name == name:
            return g
    return None


# ----- engagement scoring ---------------------------------------------------


def _engagement_score(post: dict, category: str) -> float:
    """Normalize (likes + 2*comments + 3*collects) to 0..100 against a per-category cap."""
    likes = int(post.get("likes") or 0)
    comments = int(post.get("comments_count") or 0)
    collects = int(post.get("collects") or 0)
    raw = float(likes + 2 * comments + 3 * collects)
    cap = _CATEGORY_ENGAGEMENT_CAP.get(category, _DEFAULT_ENGAGEMENT_CAP)
    if cap <= 0:
        return 0.0
    return max(0.0, min(100.0, (raw / cap) * 100.0))


# ----- client backends ------------------------------------------------------


class XhsClient(Protocol):
    """Backend contract. Both MediaCrawlerXhsClient and StubXhsClient match this."""

    def _search_keyword(
        self, keyword: str, limit: int, lookback_days: int
    ) -> list[dict[str, Any]]: ...

    def _fetch_creator_posts(self, handle: str, limit: int) -> list[dict[str, Any]]: ...


_MEDIACRAWLER_MISSING_MSG = (
    "MediaCrawler not installed; clone https://github.com/NanmiCoder/MediaCrawler "
    "into external/MediaCrawler and follow its setup, "
    "or set XHS_COMPETITOR_STUB=1 to use the canned fixture."
)


class MediaCrawlerXhsClient:
    """Real backend wrapping NanmiCoder/MediaCrawler's xhs module.

    MediaCrawler is a Playwright-based project, not a pip package. The expected
    layout is::

        F:\\PROJECTS\\xhs_op\\external\\MediaCrawler\\media_platform\\xhs\\...

    We add ``external/MediaCrawler`` to ``sys.path`` lazily, then import its xhs
    crawler entry-points. If anything goes wrong (clone missing, deps not
    installed, signature drift), we raise ``RuntimeError(_MEDIACRAWLER_MISSING_MSG)``.

    NOTE: this v1 implementation defines the wrapper shape but DOES NOT do the
    actual scraping — running MediaCrawler requires Playwright contexts,
    cookies, and async orchestration that belong in a separate dedicated job,
    not in a synchronous feeder call. The stub backend covers all current
    test/demo needs; flipping to the real backend is a follow-up wiring task
    once the user has cloned the repo.
    """

    def __init__(self, repo_root: Path | None = None) -> None:
        self._repo_root = repo_root or Path("external/MediaCrawler")
        if not self._repo_root.is_dir():
            raise RuntimeError(_MEDIACRAWLER_MISSING_MSG)
        # Defer the actual import to keep import-time cheap and to surface a
        # clean error message if MediaCrawler's own deps aren't installed.
        try:
            import sys

            sys.path.insert(0, str(self._repo_root.resolve()))
            # We don't bind the module yet — the real implementation would
            # instantiate MediaCrawler's XiaoHongShuCrawler here. v1 stops at
            # the path-insert and raises a NotImplemented when called.
        except Exception as exc:  # noqa: BLE001 — translate to a clean error
            raise RuntimeError(_MEDIACRAWLER_MISSING_MSG) from exc

    def _search_keyword(
        self, keyword: str, limit: int, lookback_days: int
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "MediaCrawler search wiring is a follow-up task; use XHS_COMPETITOR_STUB=1 "
            "to run the pipeline against the canned fixture."
        )

    def _fetch_creator_posts(self, handle: str, limit: int) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "MediaCrawler creator wiring is a follow-up task; use XHS_COMPETITOR_STUB=1 "
            "to run the pipeline against the canned fixture."
        )


class StubXhsClient:
    """Reads canned rows from a JSON fixture. Selected when XHS_COMPETITOR_STUB=1."""

    def __init__(self, fixture_path: Path | None = None) -> None:
        path = fixture_path or DEFAULT_STUB_FIXTURE_PATH
        if not path.is_file():
            raise RuntimeError(f"stub fixture not found: {path}")
        with path.open("r", encoding="utf-8") as fh:
            self._data = json.load(fh)
        self._by_keyword: dict[str, list[dict[str, Any]]] = (
            self._data.get("by_keyword") or {}
        )
        self._by_creator: dict[str, list[dict[str, Any]]] = (
            self._data.get("by_creator") or {}
        )

    def _search_keyword(
        self, keyword: str, limit: int, lookback_days: int
    ) -> list[dict[str, Any]]:
        rows = list(self._by_keyword.get(keyword) or [])
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        fresh: list[dict[str, Any]] = []
        for row in rows:
            posted_at = _parse_iso(row.get("posted_at"))
            # Keep rows with no/invalid timestamp — caller can decide.
            if posted_at is not None and posted_at < cutoff:
                continue
            fresh.append(row)
        return fresh[:limit]

    def _fetch_creator_posts(self, handle: str, limit: int) -> list[dict[str, Any]]:
        rows = list(self._by_creator.get(handle) or [])
        return rows[:limit]


def _make_client() -> XhsClient:
    if os.environ.get("XHS_COMPETITOR_STUB") == "1":
        logger.info("xhs_competitor: using StubXhsClient (XHS_COMPETITOR_STUB=1)")
        return StubXhsClient()
    return MediaCrawlerXhsClient()


# ----- helpers --------------------------------------------------------------


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        # Accept trailing Z.
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _post_to_idea_kwargs(
    post: dict[str, Any], *, group: WatchGroup, keyword: str | None
) -> dict[str, Any] | None:
    note_id = post.get("note_id")
    url = post.get("url")
    if not note_id or not url:
        return None
    posted_at = _parse_iso(post.get("posted_at"))
    extra = {
        "author": post.get("author") or "",
        "author_id": post.get("author_id") or "",
        "posted_at": posted_at.isoformat() if posted_at else "",
        "likes": int(post.get("likes") or 0),
        "comments_count": int(post.get("comments_count") or 0),
        "collects": int(post.get("collects") or 0),
        "shares": int(post.get("shares") or 0),
        "cover_image_url": post.get("cover_image_url") or "",
        "hashtags": list(post.get("hashtags") or []),
        "image_urls": list(post.get("image_urls") or []),
        "keyword": keyword or "",
    }
    return {
        "source": "xhs_competitor",
        "source_url": str(url),
        "source_id": str(note_id),
        "raw_title": str(post.get("title") or ""),
        "raw_body": str(post.get("body") or ""),
        "raw_lang": "zh",
        "engagement_score": _engagement_score(post, group.name),
        "fetched_at": datetime.now(timezone.utc),
        "category": group.name,
        "target_account": group.target_account,
        "extra": extra,
        "processed": False,
    }


# ----- public fetch ---------------------------------------------------------


def _insert_ideas(rows: list[dict[str, Any]]) -> int:
    """Insert idea kwargs lists, deduping on (source, source_id). Returns inserted count."""
    if not rows:
        return 0
    inserted = 0
    with get_session() as session:
        for kwargs in rows:
            sid = kwargs["source_id"]
            existing = session.exec(
                select(Idea.id).where(
                    Idea.source == "xhs_competitor", Idea.source_id == sid
                )
            ).first()
            if existing is not None:
                continue
            session.add(Idea(**kwargs))
            inserted += 1
    return inserted


def fetch(
    since: datetime | None = None,
    *,
    watchlist_path: Path | None = None,
    only_keyword: str | None = None,
    only_group: str | None = None,
    limit: int | None = None,
    client: XhsClient | None = None,
) -> int:
    """Iterate the watchlist, fetch top posts per keyword + creator, write Ideas.

    Returns total inserted. `since` overrides per-group `lookback_days` when set.
    """
    init_db()
    wl_path = watchlist_path or DEFAULT_WATCHLIST_PATH
    if not wl_path.exists():
        raise FileNotFoundError(f"watchlist not found: {wl_path}")
    groups = _load_watchlist(wl_path)
    if only_group:
        groups = [g for g in groups if g.name == only_group]
        if not groups:
            logger.warning("no watchlist group named %r", only_group)
            return 0
    if not groups:
        logger.warning("watchlist empty, nothing to do")
        return 0

    xhs_client = client or _make_client()
    total_inserted = 0

    for group in groups:
        # `since` overrides lookback_days when given.
        effective_lookback = group.lookback_days
        if since is not None:
            effective_lookback = max(
                0, (datetime.now(timezone.utc) - since).days
            )
        per_keyword_limit = limit or group.limit_per_keyword

        # Keyword scrape.
        keywords = (
            [only_keyword]
            if only_keyword and only_keyword in group.keywords
            else group.keywords
        )
        if only_keyword and only_keyword not in group.keywords:
            # If a keyword was passed that isn't in this group, skip the group.
            continue

        seen_in_run: set[str] = set()
        candidate_rows: list[dict[str, Any]] = []
        for kw in keywords:
            try:
                posts = xhs_client._search_keyword(
                    kw, per_keyword_limit, effective_lookback
                )
            except NotImplementedError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad keyword shouldn't tank the run
                logger.warning("group %s keyword %r: search failed: %s", group.name, kw, exc)
                continue
            logger.info(
                "group %s keyword %r: %d posts from backend",
                group.name,
                kw,
                len(posts),
            )
            for post in posts:
                nid = str(post.get("note_id") or "")
                if not nid or nid in seen_in_run:
                    continue
                seen_in_run.add(nid)
                kwargs = _post_to_idea_kwargs(post, group=group, keyword=kw)
                if kwargs:
                    candidate_rows.append(kwargs)

        # Creator scrape.
        for handle in group.creators:
            try:
                posts = xhs_client._fetch_creator_posts(handle, per_keyword_limit)
            except NotImplementedError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "group %s creator %r: fetch failed: %s", group.name, handle, exc
                )
                continue
            for post in posts:
                nid = str(post.get("note_id") or "")
                if not nid or nid in seen_in_run:
                    continue
                seen_in_run.add(nid)
                kwargs = _post_to_idea_kwargs(post, group=group, keyword=None)
                if kwargs:
                    candidate_rows.append(kwargs)

        inserted = _insert_ideas(candidate_rows)
        logger.info(
            "group %s: candidates=%d inserted=%d",
            group.name,
            len(candidate_rows),
            inserted,
        )
        total_inserted += inserted

    logger.info("xhs_competitor.fetch done: %d new ideas", total_inserted)
    return total_inserted


# ----- CLI ------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xhs_op.sources.xhs_competitor",
        description="XHS competitor / banna-villa tracker (writes ideas rows)",
    )
    p.add_argument(
        "--keyword",
        type=str,
        default=None,
        help="only fetch this keyword (must appear in the watchlist)",
    )
    p.add_argument(
        "--group",
        type=str,
        default=None,
        choices=["banna_villa", "luxury_hotel"],
        help="restrict to one watchlist group",
    )
    p.add_argument(
        "--all", action="store_true", help="walk the full watchlist (default behavior)"
    )
    p.add_argument(
        "--limit", type=int, default=None, help="override per-keyword post limit"
    )
    p.add_argument(
        "--watchlist-path",
        type=Path,
        default=None,
        help=f"watchlist YAML path (default: {DEFAULT_WATCHLIST_PATH})",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="DEBUG-level logging")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    inserted = fetch(
        watchlist_path=args.watchlist_path,
        only_keyword=args.keyword,
        only_group=args.group,
        limit=args.limit,
    )
    logging.info("inserted %d new idea(s)", inserted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
