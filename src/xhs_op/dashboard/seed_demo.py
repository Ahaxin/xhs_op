"""Seed 3 fake Idea + Draft rows so the dashboard has content to render.

Idempotent: skips if any Idea with source_id starting 'seed-' already exists.
Run via: ``python -m xhs_op.dashboard.seed_demo``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from xhs_op.db import Draft, Idea, get_session, init_db


# Three demo records covering both accounts and the main personas.
_SEED_RECORDS: list[dict[str, object]] = [
    {
        "idea": {
            "source": "xhs_competitor",
            "source_url": "https://www.xiaohongshu.com/explore/seed-banna-1",
            "source_id": "seed-banna-1",
            "raw_title": "雨林里的傣家小院",
            "raw_body": "在西双版纳的雨林边住了三天,推开门就是芭蕉叶和虫鸣。",
            "raw_lang": "zh",
            "engagement_score": 78.5,
            "category": "banna_villa",
            "target_account": "banna",
            "extra": {"hashtags": ["西双版纳", "民宿"], "author": "demo_user_1"},
        },
        "draft": {
            "account": "banna",
            "persona": "villa",
            "title": "雨林边的慢生活",
            "body": (
                "推开木门就是雨林。\n\n"
                "晨光透过芭蕉叶洒进院子,泡一壶普洱,听虫鸣鸟语。"
                "傣味早餐已经摆好——糯米饭、菠萝饭、还有现摘的香茅。\n\n"
                "院子里有秋千、有吊床,慢慢晃一下午也不腻。"
                "晚上点起篝火,一边烤罗非鱼一边看星星。\n\n"
                "雨林边的日子,被风、被光、被雨水慢慢洗过一遍。"
            ),
            "hashtags": ["西双版纳", "民宿", "傣家小院", "慢生活", "雨林"],
            "image_paths": [],
            "inspiration_note": "Inspired by competitor xhs note seed-banna-1.",
        },
    },
    {
        "idea": {
            "source": "x",
            "source_url": "https://x.com/seed/status/seed-ai-1",
            "source_id": "seed-ai-1",
            "raw_title": "Anthropic ships new agent SDK",
            "raw_body": "Anthropic just released a new agent SDK that …",
            "raw_lang": "en",
            "engagement_score": 91.0,
            "category": "ai",
            "target_account": "stock",
            "extra": {"author": "anthropic", "retweets": 1200},
        },
        "draft": {
            "account": "stock",
            "persona": "stock_digest",
            "title": "Anthropic 智能体 SDK",
            "body": (
                "Anthropic 刚刚发布了一套新的 Agent SDK,值得关注。\n\n"
                "1. 内置工具调用循环,开发者只写业务逻辑。\n"
                "2. 支持多模型路由,Claude 之外也能跑。\n"
                "3. 定价对个人开发者友好。\n\n"
                "我的看法:Agent 这条赛道还在早期,SDK 谁先成熟谁就掌握生态入口。"
                "\n\n免责声明:内容仅供学习,非投资建议。"
            ),
            "hashtags": ["AI", "Anthropic", "智能体", "Claude", "科技投资"],
            "image_paths": [],
            "inspiration_note": "Translated from X post seed-ai-1.",
        },
    },
    {
        "idea": {
            "source": "xhs_competitor",
            "source_url": "https://www.xiaohongshu.com/explore/seed-luxury-1",
            "source_id": "seed-luxury-1",
            "raw_title": "悦榕庄三天两晚体验",
            "raw_body": "全家入住悦榕庄,池景房真的太治愈了……",
            "raw_lang": "zh",
            "engagement_score": 82.3,
            "category": "luxury_hotel",
            "target_account": "banna",
            "extra": {"hashtags": ["悦榕庄", "亲子度假"], "author": "demo_user_2"},
        },
        "draft": {
            "account": "banna",
            "persona": "villa",
            "title": "私享池景的一夜",
            "body": (
                "这次住进了带私汤的院子,夜里整片星空都是自己的。\n\n"
                "傍晚池水温热,远处的雨林开始降温,鸟声慢慢退场,只剩水声。"
                "院子里的小桌已经摆好了傣味晚餐——舂鸡脚、香茅烤鱼、菠萝饭。\n\n"
                "适合带爸妈、带孩子,也适合两个人发呆。"
            ),
            "hashtags": ["西双版纳", "度假", "亲子", "傣味", "私汤"],
            "image_paths": [],
            "inspiration_note": "Inspired by luxury-hotel benchmark seed-luxury-1.",
        },
    },
]


def seed() -> None:
    """Insert demo Idea + Draft rows if none with `source_id LIKE 'seed-%'` exist."""
    init_db()
    with get_session() as session:
        existing = session.exec(select(Idea).where(Idea.source_id.like("seed-%"))).first()  # type: ignore[attr-defined]
        if existing is not None:
            print(f"seed_demo: already seeded (found idea id={existing.id}); nothing to do.")
            return

        now = datetime.now(timezone.utc)
        for offset, rec in enumerate(_SEED_RECORDS):
            idea_kwargs = dict(rec["idea"])  # type: ignore[arg-type]
            idea_kwargs.setdefault("fetched_at", now - timedelta(hours=offset + 1))
            idea = Idea(**idea_kwargs)  # type: ignore[arg-type]
            session.add(idea)
            session.flush()  # populate idea.id

            draft_kwargs = dict(rec["draft"])  # type: ignore[arg-type]
            draft_kwargs["idea_id"] = idea.id
            draft_kwargs.setdefault(
                "suggested_publish_at", now + timedelta(hours=2 * (offset + 1))
            )
            draft_kwargs.setdefault("status", "pending_review")
            draft = Draft(**draft_kwargs)  # type: ignore[arg-type]
            session.add(draft)

        print(f"seed_demo: inserted {len(_SEED_RECORDS)} idea+draft pairs.")


if __name__ == "__main__":
    seed()
