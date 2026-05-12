"""XHS competitor-inspired draft generator for the @banna-villa account.

Reads an `Idea` row produced by `xhs_op.sources.xhs_competitor`, extracts
a structural skeleton from the competitor post, then calls
`xhs_op.generate.llm.complete(persona='villa')` to produce a legally-distinct
inspired draft.

Plagiarism guard: 5-gram character-level Jaccard between draft body and source
body must be < 0.15. One retry with a stronger paraphrase instruction; if it
still fails, `PlagiarismGuardFailed` is raised rather than silently writing a
bad draft.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xhs_op.db import Draft, Idea, get_session, init_db
from xhs_op.generate import llm

logger = logging.getLogger("xhs_op.generate.inspirer")

PLAGIARISM_THRESHOLD = 0.15
VILLA_PHOTO_DIR = Path("data/assets/villa_photos")

# Max real photos to attach per draft (deterministic by idea_id hash).
_MAX_REAL_PHOTOS = 4


class PlagiarismGuardFailed(RuntimeError):
    def __init__(self, idea_id: int, score: float) -> None:
        super().__init__(
            f"idea_id={idea_id}: draft body 5-gram Jaccard={score:.3f} "
            f">= threshold={PLAGIARISM_THRESHOLD} after two attempts"
        )
        self.idea_id = idea_id
        self.score = score


# ---------------------------------------------------------------------------
# Plagiarism helpers
# ---------------------------------------------------------------------------


def _ngrams(text: str, n: int = 5) -> set[str]:
    """Character-level n-gram set. Chinese has no whitespace word boundaries."""
    t = text or ""
    if len(t) < n:
        return set()
    return {t[i : i + n] for i in range(len(t) - n + 1)}


def jaccard(a: str, b: str, n: int = 5) -> float:
    """5-gram Jaccard similarity between two strings. Returns 0.0 if both empty."""
    sa, sb = _ngrams(a, n), _ngrams(b, n)
    if not sa and not sb:
        return 0.0
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


# ---------------------------------------------------------------------------
# Skeleton extraction (heuristic, deterministic)
# ---------------------------------------------------------------------------


def _extract_skeleton(idea: Idea) -> dict[str, Any]:
    """Parse a competitor body into structural patterns.

    Deliberately heuristic so the verifier can read it without running the LLM.

    Returns a dict:
        hook_pattern       : first sentence / first ≤60 chars of body
        body_shape         : paragraph_count, total_chars, has_bullets
        hashtag_cluster    : tags from idea.extra['hashtags']
        image_layout_archetype : 'single' | 'pair' | 'grid' | 'carousel'
    """
    body = idea.raw_body or ""
    title = idea.raw_title or ""
    extra = idea.extra or {}

    # Hook: first sentence (split on 。！？… or newline).
    first_sentence_match = re.split(r"[。！？…\n]", body.lstrip())
    hook = first_sentence_match[0].strip()[:60] if first_sentence_match else ""
    if not hook:
        hook = title[:40]

    paragraphs = [p.strip() for p in re.split(r"\n+", body) if p.strip()]
    has_bullets = any(
        re.match(r"^[\d一二三四五六七八九十·•\-\*]", p) for p in paragraphs
    )

    hashtags: list[str] = [str(t) for t in (extra.get("hashtags") or [])]

    image_count = len(extra.get("image_urls") or [])
    if image_count <= 1:
        image_archetype = "single"
    elif image_count <= 2:
        image_archetype = "pair"
    elif image_count <= 4:
        image_archetype = "grid"
    else:
        image_archetype = "carousel"

    return {
        "hook_pattern": hook,
        "body_shape": {
            "paragraph_count": len(paragraphs),
            "total_chars": len(body),
            "has_bullets": has_bullets,
        },
        "hashtag_cluster": hashtags,
        "image_layout_archetype": image_archetype,
    }


# ---------------------------------------------------------------------------
# LLM prompt assembly
# ---------------------------------------------------------------------------


def _build_user_msg(idea: Idea, skeleton: dict[str, Any], *, stronger: bool = False) -> str:
    """Compose the user message for the villa persona."""
    lines: list[str] = []

    lines.append("# 竞品笔记结构参考")
    lines.append(f"来源：XHS note {idea.source_id}（{idea.category}）")
    lines.append(f"原标题: {idea.raw_title or '(无)'}")
    lines.append("")
    lines.append("## 结构骨架（仅供参考，用自己的内容重写）")
    lines.append(f"- 钩子模式: {skeleton['hook_pattern']}")
    shape = skeleton["body_shape"]
    lines.append(
        f"- 正文结构: {shape['paragraph_count']} 段，"
        f"约 {shape['total_chars']} 字，"
        f"{'有列表/分点' if shape['has_bullets'] else '纯段落叙述'}"
    )
    lines.append(f"- 竞品话题标签参考: {', '.join(skeleton['hashtag_cluster']) or '(无)'}")
    lines.append(f"- 图片排版: {skeleton['image_layout_archetype']}")
    lines.append("")

    # Real photos available?
    real_photos = _list_real_photos()
    if real_photos and idea.category == "banna_villa":
        photo_names = [p.name for p in real_photos[:6]]
        lines.append(
            f"## 实拍照片（可以在描述中自然提及，但不要编造照片里没有的细节）"
        )
        lines.append(f"文件: {', '.join(photo_names)}")
    else:
        lines.append("## 图片说明")
        lines.append(
            "本次没有实拍照片，请把正文写得独立完整——"
            "搭配 AI 生成的生活方式图片同样好看。"
        )
    lines.append("")

    lines.append("## 你的任务")
    if stronger:
        lines.append(
            "⚠️ 第二次尝试：上一版草稿与竞品正文重复度太高（5-gram 相似度 ≥ 0.15）。"
            "请大幅改变开头意象、调整结构顺序、换用不同的词汇和具体细节，"
            "确保文本与竞品原文有明显区别。"
        )
    lines.append(
        "用上面的结构骨架作为灵感，为 @banna-villa 写一条全新的小红书笔记。"
        "角度、句子、细节必须完全原创——不要抄原文任何句子。"
        "按 persona 系统提示的 JSON 格式输出。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM output parsing
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)


def _extract_json_blob(raw: str) -> str:
    s = raw.strip()
    m = _JSON_FENCE_RE.search(s)
    if m:
        return m.group(1).strip()
    if not s.startswith("{"):
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end > start:
            return s[start : end + 1]
    return s


def _parse_llm_output(raw: str) -> tuple[str, str, list[str]]:
    blob = _extract_json_blob(raw)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM did not return valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"LLM JSON was not an object: {type(data).__name__}")
    title = data.get("title")
    body = data.get("body")
    hashtags = data.get("hashtags")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("LLM JSON missing/empty 'title'")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("LLM JSON missing/empty 'body'")
    if not isinstance(hashtags, list):
        raise ValueError("LLM JSON 'hashtags' must be list[str]")
    cleaned: list[str] = []
    for t in hashtags:
        t = str(t).strip()
        if t:
            cleaned.append(t if t.startswith("#") else "#" + t)
    return title.strip(), body.strip(), cleaned


# ---------------------------------------------------------------------------
# Image plan
# ---------------------------------------------------------------------------


def _list_real_photos() -> list[Path]:
    if not VILLA_PHOTO_DIR.is_dir():
        return []
    exts = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
    photos = sorted(p for p in VILLA_PHOTO_DIR.iterdir() if p.suffix.lower() in exts)
    return photos


def _select_image_paths(idea: Idea, real_photos: list[Path]) -> list[str]:
    """Return a deterministic subset of real photos, or empty list."""
    if not real_photos or idea.category != "banna_villa":
        return []
    idea_id = idea.id or 0
    start = idea_id % max(len(real_photos), 1)
    selected: list[Path] = []
    for i in range(_MAX_REAL_PHOTOS):
        selected.append(real_photos[(start + i) % len(real_photos)])
    return [str(p) for p in selected]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def draft(idea_id: int, *, model_hint: str | None = None) -> int:
    """Generate an inspired Draft for the given competitor Idea. Returns new draft id.

    Raises:
        ValueError: if the idea is not found or not from xhs_competitor source.
        PlagiarismGuardFailed: if both LLM passes produce a body with
            5-gram Jaccard >= PLAGIARISM_THRESHOLD vs the source.
    """
    init_db()

    with get_session() as session:
        idea = session.get(Idea, idea_id)
        if idea is None:
            raise ValueError(f"Idea id={idea_id} not found")
        if idea.source != "xhs_competitor":
            logger.warning(
                "inspirer.draft called on non-xhs_competitor idea (source=%s); proceeding",
                idea.source,
            )

        skeleton = _extract_skeleton(idea)
        source_body = idea.raw_body or ""
        real_photos = _list_real_photos()

        logger.info(
            "inspirer: idea_id=%d category=%s skeleton_hook=%r",
            idea_id,
            idea.category,
            skeleton["hook_pattern"][:40],
        )

        # --- First LLM attempt ---
        user_msg = _build_user_msg(idea, skeleton, stronger=False)
        raw = llm.complete("villa", user_msg, model_hint=model_hint)
        title, body, hashtags = _parse_llm_output(raw)
        score = jaccard(body, source_body)
        logger.info("inspirer: idea_id=%d first-pass jaccard=%.3f", idea_id, score)

        if score >= PLAGIARISM_THRESHOLD:
            logger.warning(
                "inspirer: idea_id=%d jaccard=%.3f >= %.2f, retrying with stronger paraphrase",
                idea_id,
                score,
                PLAGIARISM_THRESHOLD,
            )
            user_msg2 = _build_user_msg(idea, skeleton, stronger=True)
            raw2 = llm.complete("villa", user_msg2, model_hint=model_hint)
            title, body, hashtags = _parse_llm_output(raw2)
            score = jaccard(body, source_body)
            logger.info("inspirer: idea_id=%d second-pass jaccard=%.3f", idea_id, score)
            if score >= PLAGIARISM_THRESHOLD:
                raise PlagiarismGuardFailed(idea_id, score)

        image_paths = _select_image_paths(idea, real_photos)
        has_real_photos = bool(image_paths)

        ai_note = ""
        if not has_real_photos:
            ai_note = " | image_plan: AI filler — generate via image.generate_image later"

        inspiration = (
            f"inspired by xhs note {idea.source_id} "
            f"(5gram_jaccard={score:.3f})"
            f"{ai_note}"
        )

        new_draft = Draft(
            idea_id=idea.id,
            account="banna",
            persona="villa",
            title=title,
            body=body,
            hashtags=hashtags,
            image_paths=image_paths,
            suggested_publish_at=datetime.now(timezone.utc),
            status="pending_review",
            inspiration_note=inspiration,
        )
        session.add(new_draft)
        idea.processed = True
        session.add(idea)
        session.flush()
        draft_id = int(new_draft.id or 0)

    logger.info("inspirer: created draft id=%d for idea_id=%d (jaccard=%.3f)", draft_id, idea_id, score)
    return draft_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xhs_op.generate.inspirer",
        description="XHS competitor-inspired draft generator for @banna-villa",
    )
    p.add_argument("--idea-id", type=int, required=True, help="Idea row id to inspire from")
    p.add_argument("--model-hint", default=None, help="override model routing")
    p.add_argument("--verbose", "-v", action="store_true", help="DEBUG-level logging")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    draft_id = draft(args.idea_id, model_hint=args.model_hint)
    with get_session() as session:
        d = session.get(Draft, draft_id)
        if d is None:
            print(json.dumps({"draft_id": draft_id, "error": "draft vanished"}))
            return 1
        src_idea_id = d.idea_id
        src_jaccard: float | None = None
        m = re.search(r"5gram_jaccard=([0-9.]+)", d.inspiration_note or "")
        if m:
            try:
                src_jaccard = float(m.group(1))
            except ValueError:
                pass
        body_preview = (d.body or "")[:160].replace("\n", " ⏎ ")
        print(
            json.dumps(
                {
                    "draft_id": draft_id,
                    "idea_id": src_idea_id,
                    "jaccard_score": src_jaccard,
                    "title": d.title,
                    "hashtags": d.hashtags,
                    "image_paths": d.image_paths,
                    "inspiration_note": d.inspiration_note,
                    "body_preview": body_preview,
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
