"""X → XHS translator.

Takes an `Idea` row produced by `xhs_op.sources.x_scraper`, picks the right
persona (`stock_digest` vs `stock_hottake`), calls `xhs_op.generate.llm.complete`,
parses the JSON envelope, and writes a `Draft` row in `pending_review`.

Image generation is intentionally out of scope here — Task 6's image worker
can fill `draft.image_paths` later. We leave `image_paths=[]`.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from xhs_op.db import Draft, Idea, get_session, init_db
from xhs_op.generate import llm

logger = logging.getLogger("xhs_op.generate.translator")

DEFAULT_WATCHLIST_PATH = Path("data/x_watchlist.yaml")
DEFAULT_THRESHOLD = 50.0
HOT_TAKE_WINDOW = timedelta(hours=2)

# Disclaimer the persona prompts demand and that the verifier will regex for.
DISCLAIMER = "本文仅供学习交流，不构成任何投资建议。"


class TranslatorError(RuntimeError):
    """Raised when the translator cannot produce a valid draft."""


def _load_threshold(watchlist_path: Path | None = None) -> float:
    p = watchlist_path or DEFAULT_WATCHLIST_PATH
    if not p.exists():
        logger.warning("watchlist missing at %s — using default threshold %.1f", p, DEFAULT_THRESHOLD)
        return DEFAULT_THRESHOLD
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return float(data.get("engagement_threshold", DEFAULT_THRESHOLD))
    except (OSError, yaml.YAMLError, TypeError, ValueError) as exc:
        logger.warning("watchlist parse error %s — using default threshold %.1f", exc, DEFAULT_THRESHOLD)
        return DEFAULT_THRESHOLD


def _parse_posted_at(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    s = value.strip()
    # twikit's `created_at` raw string format: 'Wed Oct 10 20:19:24 +0000 2018'
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    # ISO 8601 (covers what we emit ourselves).
    try:
        # Python 3.11+ fromisoformat handles 'Z'; we're on 3.13.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pick_persona(idea: Idea, threshold: float) -> str:
    """Hot-take if recent AND high-engagement; otherwise digest."""
    posted_at = _parse_posted_at((idea.extra or {}).get("posted_at"))
    fetched_at = idea.fetched_at
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)

    if posted_at is None:
        return "stock_digest"

    age = fetched_at - posted_at
    if age <= HOT_TAKE_WINDOW and (idea.engagement_score or 0.0) > threshold:
        return "stock_hottake"
    return "stock_digest"


def _build_user_msg(idea: Idea) -> str:
    """Compose the user message the persona prompt expects."""
    extra = idea.extra or {}
    lines: list[str] = []
    lines.append("# 原始推文 (X / Twitter)")
    author = extra.get("author") or "(unknown)"
    lines.append(f"作者: @{author}")
    if extra.get("posted_at"):
        lines.append(f"发布时间 (UTC): {extra['posted_at']}")
    lines.append(f"语言: {idea.raw_lang or 'unknown'}")
    lines.append("")
    lines.append("## 推文正文")
    lines.append((idea.raw_body or idea.raw_title or "").strip())
    lines.append("")
    lines.append("## 关键互动指标")
    lines.append(f"- likes: {extra.get('likes', 0)}")
    lines.append(f"- retweets: {extra.get('retweets', 0)}")
    lines.append(f"- replies: {extra.get('replies', 0)}")
    lines.append(f"- engagement_score (0-100): {idea.engagement_score:.2f}")
    if extra.get("hashtags"):
        lines.append(f"- 原推话题标签: {', '.join(map(str, extra['hashtags']))}")
    if extra.get("urls"):
        lines.append(f"- 推文里的链接: {', '.join(map(str, extra['urls']))}")
    lines.append("")
    lines.append(
        "请按系统提示中的 JSON 结构输出一条中文小红书笔记。"
        "记住：不要逐句翻译，要重构信息；body 必须以免责声明结尾。"
    )
    return "\n".join(lines)


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)


def _extract_json_blob(raw: str) -> str:
    """LLMs occasionally wrap JSON in ```json fences despite being told not to."""
    s = raw.strip()
    m = _JSON_FENCE_RE.search(s)
    if m:
        return m.group(1).strip()
    # If the model preambled with text, look for first '{' and last '}'.
    if not s.startswith("{"):
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            return s[start : end + 1]
    return s


def _parse_llm_output(raw: str) -> tuple[str, str, list[str]]:
    blob = _extract_json_blob(raw)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise TranslatorError(f"LLM did not return valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TranslatorError(f"LLM JSON was not an object: {type(data).__name__}")

    title = data.get("title")
    body = data.get("body")
    hashtags = data.get("hashtags")
    if not isinstance(title, str) or not title.strip():
        raise TranslatorError("LLM JSON missing/empty 'title'")
    if not isinstance(body, str) or not body.strip():
        raise TranslatorError("LLM JSON missing/empty 'body'")
    if not isinstance(hashtags, list) or not all(isinstance(t, str) for t in hashtags):
        raise TranslatorError("LLM JSON 'hashtags' must be list[str]")

    # Best-effort cleanup. Strip surrounding whitespace; ensure hashtags start with '#'.
    cleaned_tags: list[str] = []
    for t in hashtags:
        t = t.strip()
        if not t:
            continue
        if not t.startswith("#"):
            t = "#" + t
        cleaned_tags.append(t)
    return title.strip(), body.strip(), cleaned_tags


def _ensure_disclaimer(body: str) -> str:
    """Append the disclaimer line if the LLM forgot it. Idempotent."""
    if DISCLAIMER in body:
        return body
    sep = "" if body.endswith("\n") else "\n"
    return f"{body}{sep}\n{DISCLAIMER}"


def draft(
    idea_id: int,
    *,
    watchlist_path: Path | None = None,
    model_hint: str | None = None,
) -> int:
    """Generate a Draft for the given Idea. Returns the new draft id.

    - Picks persona via the hot-take fork (recent + high-engagement → hottake).
    - Calls llm.complete and parses the JSON envelope.
    - Marks the Idea processed=True on success.
    """
    init_db()
    threshold = _load_threshold(watchlist_path)

    with get_session() as session:
        idea = session.get(Idea, idea_id)
        if idea is None:
            raise TranslatorError(f"Idea id={idea_id} not found")
        if idea.source != "x":
            logger.warning(
                "translator.draft called on non-x idea (source=%s); proceeding anyway",
                idea.source,
            )

        persona = _pick_persona(idea, threshold)
        user_msg = _build_user_msg(idea)
        author = (idea.extra or {}).get("author") or "unknown"
        logger.info(
            "translator: idea_id=%d persona=%s engagement=%.2f threshold=%.2f",
            idea_id,
            persona,
            idea.engagement_score or 0.0,
            threshold,
        )
        logger.debug("translator: user_msg=%s", user_msg)

        raw = llm.complete(persona, user_msg, model_hint=model_hint)
        title, body, hashtags = _parse_llm_output(raw)
        body = _ensure_disclaimer(body)

        # suggested_publish_at: leave it as 'now' for the scheduler to override later.
        suggested = datetime.now(timezone.utc)
        new_draft = Draft(
            idea_id=idea.id,
            account="stock",
            persona=persona,
            title=title,
            body=body,
            hashtags=hashtags,
            image_paths=[],
            suggested_publish_at=suggested,
            status="pending_review",
            inspiration_note=f"translated from X tweet {idea.source_id} by @{author}",
        )
        session.add(new_draft)
        idea.processed = True
        session.add(idea)
        session.flush()  # populate new_draft.id before we leave the session
        draft_id = int(new_draft.id or 0)

    logger.info("translator: created draft id=%d for idea_id=%d", draft_id, idea_id)
    return draft_id


# --- CLI ---------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xhs_op.generate.translator", description="X→XHS translator"
    )
    p.add_argument("--idea-id", type=int, required=True, help="Idea row id to translate")
    p.add_argument(
        "--watchlist",
        type=Path,
        default=None,
        help=f"path to x_watchlist.yaml for threshold (default: {DEFAULT_WATCHLIST_PATH})",
    )
    p.add_argument("--model-hint", default=None, help="override model routing")
    p.add_argument("--verbose", "-v", action="store_true", help="DEBUG-level logging")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    draft_id = draft(
        args.idea_id, watchlist_path=args.watchlist, model_hint=args.model_hint
    )
    # Preview from the DB.
    with get_session() as session:
        d = session.get(Draft, draft_id)
        if d is None:
            print(json.dumps({"draft_id": draft_id, "error": "draft vanished"}))
            return 1
        body_preview = (d.body or "")[:160].replace("\n", " ⏎ ")
        print(
            json.dumps(
                {
                    "draft_id": draft_id,
                    "persona": d.persona,
                    "title": d.title,
                    "hashtags": d.hashtags,
                    "body_preview": body_preview,
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
