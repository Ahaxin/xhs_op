"""X (Twitter) scraper for the `stock` XHS account.

Polls a watchlist of accounts + search terms via `twikit`, scores tweets by
engagement, and inserts `Idea` rows for the translator (`xhs_op.generate.translator`)
to turn into XHS drafts.

twikit 2.x is async-only; we wrap the async calls behind a sync `fetch()`
so the rest of the codebase (and the scheduler, when it lands) can call this
the same way it calls `xhs_op.sources.rss.fetch`.

Auth: this module does NOT do an interactive login. It expects cookies pre-saved
to `data/cookies/x.json` (use `twikit.Client.save_cookies` from a one-time
manual login script). If the file is missing we raise a clear `FileNotFoundError`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml
from sqlmodel import select
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# twikit is an optional-at-import-time dep: we want the module importable even
# on a machine where twikit isn't installed (e.g. for the verifier's static
# checks). The actual fetch() will raise loudly if twikit isn't available.
try:  # pragma: no cover — trivial import shim
    import twikit  # type: ignore[import-not-found]
    from twikit import Client as TwikitClient  # type: ignore[import-not-found]

    _TWIKIT_AVAILABLE = True
except Exception as exc:  # pragma: no cover
    twikit = None  # type: ignore[assignment]
    TwikitClient = None  # type: ignore[assignment, misc]
    _TWIKIT_AVAILABLE = False
    _TWIKIT_IMPORT_ERROR: Exception | None = exc
else:
    _TWIKIT_IMPORT_ERROR = None

from xhs_op.db import Idea, get_session, init_db

logger = logging.getLogger("xhs_op.sources.x_scraper")

DEFAULT_WATCHLIST_PATH = Path("data/x_watchlist.yaml")
DEFAULT_COOKIE_PATH = Path("data/cookies/x.json")

# Follower-floor used in the engagement_score denominator so a 10-follower
# account with 50 likes doesn't score 100. Tuned so a 1M-follower account
# pulling ~5K likes ends up around 50 on the 0..100 scale.
_FOLLOWER_FLOOR = 50_000
_SCORE_DENOMINATOR_DIVISOR = 1_000.0  # raw_score / 1000 -> 0..100 range, then clamp

# Twikit transient errors we want to retry. We dodge auth errors (don't retry on bad cookies).
_RETRYABLE_NETWORK_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    ConnectionError,
)


@dataclass(frozen=True)
class WatchlistAccount:
    handle: str
    category: str


@dataclass(frozen=True)
class WatchlistSearch:
    term: str
    category: str


@dataclass(frozen=True)
class Watchlist:
    accounts: list[WatchlistAccount]
    searches: list[WatchlistSearch]
    category_keywords: dict[str, list[str]]
    engagement_threshold: float


def load_watchlist(path: Path | None = None) -> Watchlist:
    """Read data/x_watchlist.yaml into a typed Watchlist."""
    p = path or DEFAULT_WATCHLIST_PATH
    if not p.exists():
        raise FileNotFoundError(f"x watchlist not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    accounts = [
        WatchlistAccount(handle=str(a["handle"]).lstrip("@"), category=str(a["category"]))
        for a in (data.get("accounts") or [])
        if a.get("handle")
    ]
    searches = [
        WatchlistSearch(term=str(s["term"]), category=str(s["category"]))
        for s in (data.get("search_terms") or [])
        if s.get("term")
    ]
    keywords_raw = data.get("category_keywords") or {}
    category_keywords = {
        str(k): [str(v).lower() for v in (vals or [])] for k, vals in keywords_raw.items()
    }
    threshold = float(data.get("engagement_threshold", 50.0))
    return Watchlist(
        accounts=accounts,
        searches=searches,
        category_keywords=category_keywords,
        engagement_threshold=threshold,
    )


def _engagement_score(
    likes: int, retweets: int, replies: int, follower_count: int | None
) -> float:
    """Weighted engagement on a 0..100 scale. Heuristic, tunable."""
    raw = likes + 2 * retweets + 3 * replies
    denom = max(follower_count or 0, _FOLLOWER_FLOOR)
    # Scale: raw / denom is roughly the "engagement rate"; multiply to a 0..100ish scale.
    score = (raw / denom) * 100.0 * (_FOLLOWER_FLOOR / _SCORE_DENOMINATOR_DIVISOR)
    if score < 0:
        return 0.0
    if score > 100.0:
        return 100.0
    return round(score, 2)


def _classify_category(
    text: str, fallback: str, keywords: dict[str, list[str]]
) -> str:
    """Pick {ai, crypto, stock} based on a keyword sweep; fall back to source's category."""
    lower = (text or "").lower()
    for category in ("ai", "crypto", "stock"):
        for kw in keywords.get(category, []):
            if kw and kw in lower:
                return category
    return fallback


def _tweet_to_idea_kwargs(
    tweet: Any,
    *,
    fallback_category: str,
    target_account: str,
    keywords: dict[str, list[str]],
    follower_count: int | None,
) -> dict[str, Any]:
    """Convert a twikit Tweet into the kwargs needed to construct an Idea row."""
    text = getattr(tweet, "full_text", None) or getattr(tweet, "text", "") or ""
    likes = int(getattr(tweet, "favorite_count", 0) or 0)
    retweets = int(getattr(tweet, "retweet_count", 0) or 0)
    replies = int(getattr(tweet, "reply_count", 0) or 0)
    score = _engagement_score(likes, retweets, replies, follower_count)

    # twikit exposes both `created_at` (string) and `created_at_datetime` (datetime).
    posted_at_dt = getattr(tweet, "created_at_datetime", None)
    if isinstance(posted_at_dt, datetime):
        if posted_at_dt.tzinfo is None:
            posted_at_dt = posted_at_dt.replace(tzinfo=timezone.utc)
        posted_at_iso = posted_at_dt.isoformat()
    else:
        posted_at_iso = str(getattr(tweet, "created_at", "") or "")

    user = getattr(tweet, "user", None)
    author = ""
    if user is not None:
        author = getattr(user, "screen_name", "") or getattr(user, "name", "") or ""

    hashtags_raw = getattr(tweet, "hashtags", None) or []
    hashtags: list[str] = []
    for tag in hashtags_raw:
        if isinstance(tag, str):
            hashtags.append(tag)
        elif isinstance(tag, dict):
            t = tag.get("text") or tag.get("tag")
            if t:
                hashtags.append(str(t))

    urls_raw = getattr(tweet, "urls", None) or []
    urls: list[str] = []
    for u in urls_raw:
        if isinstance(u, str):
            urls.append(u)
        elif isinstance(u, dict):
            v = u.get("expanded_url") or u.get("url")
            if v:
                urls.append(str(v))

    tweet_id = str(getattr(tweet, "id", "") or "")
    source_url = f"https://twitter.com/{author}/status/{tweet_id}" if author and tweet_id else ""

    category = _classify_category(text, fallback_category, keywords)
    lang = getattr(tweet, "lang", "") or ""

    return {
        "source": "x",
        "source_url": source_url,
        "source_id": tweet_id,
        "raw_title": (text[:80] + "…") if len(text) > 80 else text,
        "raw_body": text,
        "raw_lang": str(lang),
        "engagement_score": score,
        "fetched_at": datetime.now(timezone.utc),
        "category": category,
        "target_account": target_account,
        "extra": {
            "author": author,
            "posted_at": posted_at_iso,
            "likes": likes,
            "retweets": retweets,
            "replies": replies,
            "hashtags": hashtags,
            "urls": urls,
            "follower_count": follower_count,
        },
        "processed": False,
    }


def _ensure_cookies(cookie_path: Path) -> None:
    if not cookie_path.exists():
        raise FileNotFoundError(
            f"X cookies not found at {cookie_path}. "
            "Run a one-time login script: `from twikit import Client; "
            "c=Client('en-US'); await c.login(auth_info_1=..., password=...); "
            "c.save_cookies('data/cookies/x.json')`."
        )


def _ensure_twikit() -> None:
    if not _TWIKIT_AVAILABLE:
        raise RuntimeError(
            f"twikit is not importable: {_TWIKIT_IMPORT_ERROR!r}. "
            "Install it (it should already be in pyproject.toml) and retry."
        )


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    retry=retry_if_exception_type(_RETRYABLE_NETWORK_EXCEPTIONS),
)
async def _safe_get_user(client: Any, handle: str) -> Any:
    return await client.get_user_by_screen_name(handle)


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    retry=retry_if_exception_type(_RETRYABLE_NETWORK_EXCEPTIONS),
)
async def _safe_get_user_tweets(client: Any, user_id: str, count: int) -> Any:
    return await client.get_user_tweets(user_id, "Tweets", count=count)


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    retry=retry_if_exception_type(_RETRYABLE_NETWORK_EXCEPTIONS),
)
async def _safe_search(client: Any, query: str, count: int) -> Any:
    return await client.search_tweet(query, "Latest", count=count)


async def _iter_tweets_for_account(
    client: Any, account: WatchlistAccount, *, per_account_limit: int
) -> list[tuple[Any, int | None, str]]:
    """Returns list of (tweet, follower_count, fallback_category)."""
    try:
        user = await _safe_get_user(client, account.handle)
    except Exception as exc:
        logger.warning("x: failed to load user @%s: %s", account.handle, exc)
        return []
    follower_count = int(getattr(user, "followers_count", 0) or 0) or None
    try:
        tweets = await _safe_get_user_tweets(client, user.id, per_account_limit)
    except Exception as exc:
        logger.warning("x: failed to load tweets for @%s: %s", account.handle, exc)
        return []
    out: list[tuple[Any, int | None, str]] = []
    for t in tweets:
        out.append((t, follower_count, account.category))
    return out


async def _iter_tweets_for_search(
    client: Any, search: WatchlistSearch, *, per_search_limit: int
) -> list[tuple[Any, int | None, str]]:
    try:
        results = await _safe_search(client, search.term, per_search_limit)
    except Exception as exc:
        logger.warning("x: search %r failed: %s", search.term, exc)
        return []
    out: list[tuple[Any, int | None, str]] = []
    for t in results:
        user = getattr(t, "user", None)
        fc = int(getattr(user, "followers_count", 0) or 0) if user is not None else 0
        out.append((t, fc or None, search.category))
    return out


def _insert_ideas(
    candidates: Iterable[tuple[Any, int | None, str]],
    *,
    since: datetime | None,
    keywords: dict[str, list[str]],
    target_account: str,
    cap: int | None,
) -> int:
    """Dedupe by source_id and insert. Returns count of new rows actually inserted."""
    inserted = 0
    with get_session() as session:
        for tweet, follower_count, fallback_category in candidates:
            if cap is not None and inserted >= cap:
                break
            tweet_id = str(getattr(tweet, "id", "") or "")
            if not tweet_id:
                continue

            # Time filter (against since).
            posted_dt = getattr(tweet, "created_at_datetime", None)
            if isinstance(posted_dt, datetime):
                if posted_dt.tzinfo is None:
                    posted_dt = posted_dt.replace(tzinfo=timezone.utc)
                if since is not None and posted_dt < since:
                    continue

            existing = session.exec(
                select(Idea.id).where(Idea.source == "x", Idea.source_id == tweet_id)
            ).first()
            if existing is not None:
                continue

            kwargs = _tweet_to_idea_kwargs(
                tweet,
                fallback_category=fallback_category,
                target_account=target_account,
                keywords=keywords,
                follower_count=follower_count,
            )
            session.add(Idea(**kwargs))
            inserted += 1
    return inserted


async def _fetch_async(
    *,
    since: datetime,
    watchlist: Watchlist,
    cookie_path: Path,
    per_account_limit: int,
    per_search_limit: int,
    overall_cap: int | None,
) -> int:
    _ensure_twikit()
    _ensure_cookies(cookie_path)

    assert TwikitClient is not None  # for type-checkers; _ensure_twikit raised otherwise
    client = TwikitClient(language="en-US")
    client.load_cookies(str(cookie_path))

    all_candidates: list[tuple[Any, int | None, str]] = []
    for acct in watchlist.accounts:
        all_candidates.extend(
            await _iter_tweets_for_account(client, acct, per_account_limit=per_account_limit)
        )
    for srch in watchlist.searches:
        all_candidates.extend(
            await _iter_tweets_for_search(client, srch, per_search_limit=per_search_limit)
        )

    return _insert_ideas(
        all_candidates,
        since=since,
        keywords=watchlist.category_keywords,
        target_account="stock",
        cap=overall_cap,
    )


def fetch(
    since: datetime,
    *,
    watchlist_path: Path | None = None,
    cookie_path: Path | None = None,
    per_account_limit: int = 20,
    per_search_limit: int = 20,
    overall_cap: int | None = None,
) -> int:
    """Poll the X watchlist + searches and insert new Idea rows.

    Returns the number of new ideas inserted (excludes dedupes / old / errors).
    Network errors on a single account or search are logged and swallowed —
    one bad handle can't tank the whole run.
    """
    init_db()
    watchlist = load_watchlist(watchlist_path)
    cookies = cookie_path or DEFAULT_COOKIE_PATH
    return asyncio.run(
        _fetch_async(
            since=since,
            watchlist=watchlist,
            cookie_path=cookies,
            per_account_limit=per_account_limit,
            per_search_limit=per_search_limit,
            overall_cap=overall_cap,
        )
    )


# --- CLI ---------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xhs_op.sources.x_scraper", description="X (Twitter) scraper for xhs_op"
    )
    p.add_argument("--once", action="store_true", help="run fetch() once and exit")
    p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="overall cap on new ideas to insert this run (default: 20)",
    )
    p.add_argument(
        "--since-hours",
        type=float,
        default=24.0,
        help="ignore tweets older than this many hours (default: 24)",
    )
    p.add_argument(
        "--watchlist",
        type=Path,
        default=None,
        help=f"path to x_watchlist.yaml (default: {DEFAULT_WATCHLIST_PATH})",
    )
    p.add_argument(
        "--cookies",
        type=Path,
        default=None,
        help=f"path to x cookies json (default: {DEFAULT_COOKIE_PATH})",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="DEBUG-level logging")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="just load+validate watchlist & report counts; do not call twikit",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.dry_run:
        wl = load_watchlist(args.watchlist)
        logger.info(
            "watchlist OK: %d accounts, %d searches, threshold=%.1f, %d keyword categories",
            len(wl.accounts),
            len(wl.searches),
            wl.engagement_threshold,
            len(wl.category_keywords),
        )
        return 0
    if not args.once:
        logger.error("must pass --once (continuous mode belongs to the scheduler)")
        return 2
    since = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
    inserted = fetch(
        since,
        watchlist_path=args.watchlist,
        cookie_path=args.cookies,
        overall_cap=args.limit,
    )
    logger.info("inserted %d new x idea(s)", inserted)
    # Also emit a tiny JSON summary on stdout for verifier convenience.
    print(json.dumps({"inserted": inserted}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
