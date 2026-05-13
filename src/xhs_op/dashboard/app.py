from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import streamlit as st
from sqlmodel import select

from xhs_op.config import get_settings
from xhs_op.db import Comment, Draft, Idea, Metric, Post, get_session, init_db

# Best-effort import of Task 4 LLM router; absence is non-fatal.
try:
    from xhs_op.generate import llm as _llm  # type: ignore

    _LLM_AVAILABLE = True
except Exception:
    _llm = None  # type: ignore
    _LLM_AVAILABLE = False

# Best-effort import of Task 3 inspirer; absence is non-fatal.
try:
    from xhs_op.generate import inspirer as _inspirer  # type: ignore

    _INSPIRER_AVAILABLE = True
except Exception:
    _inspirer = None  # type: ignore
    _INSPIRER_AVAILABLE = False



try:
    from xhs_op.generate import image as _image  # type: ignore

    _IMAGE_AVAILABLE = True
except Exception:
    _image = None  # type: ignore
    _IMAGE_AVAILABLE = False

logger = logging.getLogger(__name__)

# XHS limits.
TITLE_MAX = 20
BODY_MAX = 1000

# Cadence guard threshold (minutes).
CADENCE_MIN_GAP = 90

ACCOUNT_OPTIONS = ("all", "banna", "stock")


# Plain-Python DTOs so we can render after the SQLModel session closes.
@dataclass
class DraftView:
    id: int
    account: str
    persona: str
    title: str
    body: str
    hashtags: list[str]
    image_paths: list[str]
    suggested_publish_at: datetime
    inspiration_note: str


@dataclass
class IdeaView:
    id: int
    source_url: str
    raw_title: str
    raw_body: str
    engagement_score: float
    category: str


@dataclass
class CommentView:
    id: int
    post_title: str
    xhs_note_id: str
    author: str
    text: str
    intent: str | None
    drafted_reply: str


@dataclass
class PostView:
    id: int
    account: str
    title: str
    posted_at: datetime


def _draft_to_view(d: Draft) -> DraftView:
    return DraftView(
        id=int(d.id or 0),
        account=d.account,
        persona=d.persona,
        title=d.title,
        body=d.body,
        hashtags=list(d.hashtags or []),
        image_paths=list(d.image_paths or []),
        suggested_publish_at=d.suggested_publish_at,
        inspiration_note=d.inspiration_note,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_db() -> None:
    """Make sure schema exists; safe to call on every rerun."""
    init_db()


def _parse_hashtags(csv: str) -> list[str]:
    """Parse a comma-separated hashtag string into a clean list."""
    return [t.strip().lstrip("#") for t in csv.split(",") if t.strip()]


def _hashtags_to_csv(tags: list[str]) -> str:
    return ", ".join(tags or [])


def _account_filter(stmt, model_attr, account: str):  # type: ignore[no-untyped-def]
    """Apply the sidebar account filter if not 'all'."""
    if account == "all":
        return stmt
    return stmt.where(model_attr == account)


# ---------- Tab 1: Queue ----------


def _render_queue_tab(account: str, model_override: str | None) -> None:
    st.subheader("Pending review")
    with get_session() as session:
        stmt = select(Draft).where(Draft.status == "pending_review")
        stmt = _account_filter(stmt, Draft.account, account)
        stmt = stmt.order_by(Draft.created_at.desc())
        drafts = [_draft_to_view(d) for d in session.exec(stmt)]

    if not drafts:
        st.info("No drafts waiting for review. Seed some via `python -m xhs_op.dashboard.seed_demo`.")
        return

    for draft in drafts:
        _render_draft_card(draft, model_override)


def _render_draft_card(draft: DraftView, model_override: str | None) -> None:
    """Render one editable draft card with action buttons."""
    key_prefix = f"draft-{draft.id}"
    with st.container(border=True):
        # Header: account + persona + inspiration_note.
        head_cols = st.columns([1, 1, 4])
        head_cols[0].markdown(f"**Account:** `{draft.account}`")
        head_cols[1].markdown(f"**Persona:** `{draft.persona}`")
        if draft.inspiration_note:
            head_cols[2].markdown(f"*Why this draft exists:* {draft.inspiration_note}")

        # Title with char counter.
        title_val = st.text_input(
            f"Title (≤{TITLE_MAX})",
            value=draft.title,
            max_chars=TITLE_MAX,
            key=f"{key_prefix}-title",
        )
        st.caption(f"{len(title_val)}/{TITLE_MAX} characters")

        # Body with char counter.
        body_val = st.text_area(
            f"Body (≤{BODY_MAX})",
            value=draft.body,
            max_chars=BODY_MAX,
            key=f"{key_prefix}-body",
            height=200,
        )
        st.caption(f"{len(body_val)}/{BODY_MAX} characters")

        # Hashtags as csv input.
        hashtags_csv = st.text_input(
            "Hashtags (comma-separated, no # needed)",
            value=_hashtags_to_csv(draft.hashtags),
            key=f"{key_prefix}-hashtags",
        )

        # Image grid (thumbnails) + per-image regen button.
        if draft.image_paths:
            st.markdown("**Images**")
            img_cols = st.columns(min(len(draft.image_paths), 4))
            for idx, path in enumerate(draft.image_paths):
                col = img_cols[idx % len(img_cols)]
                with col:
                    try:
                        st.image(path, use_container_width=True)
                    except Exception:
                        st.caption(f"(missing) {path}")
                    if st.button("🔄 Regenerate image", key=f"{key_prefix}-regen-img-{idx}"):
                        _queue_image_regen(draft.id, idx, path)
        else:
            st.caption("No images yet.")

        # Suggested publish time (treat as UTC-naive in widget; store as UTC).
        local_dt = draft.suggested_publish_at
        if local_dt.tzinfo is not None:
            local_dt = local_dt.astimezone(timezone.utc).replace(tzinfo=None)
        scheduled_val = st.datetime_input(
            "Suggested publish at (UTC)",
            value=local_dt,
            key=f"{key_prefix}-when",
        ) if hasattr(st, "datetime_input") else _fallback_datetime_input(local_dt, key_prefix)

        # Action buttons.
        btn_cols = st.columns(4)
        approved = btn_cols[0].button("✅ Approve & Schedule", key=f"{key_prefix}-approve")
        saved = btn_cols[1].button("✏️ Save Edits", key=f"{key_prefix}-save")
        regen = btn_cols[2].button("🔄 Regenerate Text", key=f"{key_prefix}-regen-text")
        discarded = btn_cols[3].button("🗑️ Discard", key=f"{key_prefix}-discard")

        new_status: str | None = None
        if approved:
            new_status = "scheduled"
        elif discarded:
            new_status = "discarded"

        if approved or saved or discarded:
            _persist_draft_edits(
                draft_id=draft.id,
                title=title_val,
                body=body_val,
                hashtags=_parse_hashtags(hashtags_csv),
                suggested_publish_at=_to_utc(scheduled_val),
                status=new_status,
            )
            st.rerun()

        if regen:
            _regenerate_text(draft, body_val, model_override)

        with st.expander("🎨 Generate image"):
            prompt = st.text_area(
                "Image prompt",
                value=f"XHS cover image for: {title_val}",
                key=f"{key_prefix}-img-prompt",
                height=80,
            )
            make_image = st.button("Generate pic", key=f"{key_prefix}-make-pic")
            if make_image:
                _generate_and_attach_image(draft.id, prompt)
                st.rerun()


def _fallback_datetime_input(value: datetime, key_prefix: str) -> datetime:
    """Older Streamlit versions lack st.datetime_input — split into date + time."""
    cols = st.columns(2)
    d = cols[0].date_input("Date (UTC)", value=value.date(), key=f"{key_prefix}-date")
    t = cols[1].time_input("Time (UTC)", value=value.time(), key=f"{key_prefix}-time")
    return datetime.combine(d, t)


def _to_utc(dt: datetime) -> datetime:
    """Treat naive datetimes from widgets as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _persist_draft_edits(
    *,
    draft_id: int | None,
    title: str,
    body: str,
    hashtags: list[str],
    suggested_publish_at: datetime,
    status: str | None,
) -> None:
    if draft_id is None:
        return
    with get_session() as session:
        draft = session.get(Draft, draft_id)
        if draft is None:
            return
        draft.title = title[:TITLE_MAX]
        draft.body = body[:BODY_MAX]
        draft.hashtags = hashtags
        draft.suggested_publish_at = suggested_publish_at
        if status:
            draft.status = status
        session.add(draft)


def _queue_image_regen(draft_id: int | None, idx: int, path: str) -> None:
    """Record an in-session regen request; actual image gen lives in Task 4."""
    regen_requests: list[dict[str, Any]] = st.session_state.setdefault("regen_requests", [])
    regen_requests.append(
        {
            "draft_id": draft_id,
            "image_index": idx,
            "image_path": path,
            "queued_at": _utc_now().isoformat(),
        }
    )
    st.toast(f"Queued image regen for draft #{draft_id} (index {idx}).")


def _regenerate_text(draft: DraftView, current_body: str, model_override: str | None) -> None:
    """Best-effort call to Task 4 LLM router. Warn on failure, never crash."""
    if not _LLM_AVAILABLE or _llm is None:
        st.warning(
            "xhs_op.generate.llm is not importable yet (Task 4 in flight). Text regen skipped."
        )
        return
    try:
        new_text = _llm.complete(
            persona=draft.persona,
            user_msg=current_body or draft.title,
            model_hint=model_override,
        )
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Text regen failed: {exc}")
        return
    # Stash the new text so the operator can paste it in if they want.
    st.session_state[f"regen-text-{draft.id}"] = new_text
    st.success("Regenerated text (see expander).")
    with st.expander("Regenerated text — copy what you like"):
        st.write(new_text)




def _generate_and_attach_image(draft_id: int | None, prompt: str) -> None:
    if draft_id is None:
        return
    if not prompt.strip():
        st.warning("Please enter an image prompt.")
        return
    if not _IMAGE_AVAILABLE or _image is None:
        st.warning("xhs_op.generate.image is not importable yet. Image generation skipped.")
        return
    try:
        new_path = _image.generate_image(prompt)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Image generation failed: {exc}")
        return

    with get_session() as session:
        draft = session.get(Draft, draft_id)
        if draft is None:
            return
        paths = list(draft.image_paths or [])
        paths.append(new_path)
        draft.image_paths = paths
        session.add(draft)
    st.success("Image generated and attached to this draft.")

# ---------- Tab 2: Schedule ----------


def _render_schedule_tab(account: str) -> None:
    st.subheader("Scheduled drafts")
    with get_session() as session:
        stmt = select(Draft).where(Draft.status == "scheduled")
        stmt = _account_filter(stmt, Draft.account, account)
        stmt = stmt.order_by(Draft.suggested_publish_at.asc())
        drafts = [_draft_to_view(d) for d in session.exec(stmt)]

    if not drafts:
        st.info("Nothing scheduled. Approve drafts from the Queue tab.")
        return

    # Detect cadence violations: same-account pairs <90 min apart.
    violations = _detect_cadence_violations(drafts)
    if violations:
        st.warning(
            f"Cadence guardrail: {len(violations)} draft(s) violate the {CADENCE_MIN_GAP}-min "
            "gap on the same account."
        )

    for draft in drafts:
        with st.container(border=True):
            cols = st.columns([1, 3, 2, 2])
            cols[0].markdown(f"**{draft.account}**")
            cols[1].markdown(f"**{draft.title}**  \n*{draft.persona}*")
            cur = draft.suggested_publish_at
            if cur.tzinfo is not None:
                cur = cur.astimezone(timezone.utc).replace(tzinfo=None)
            new_when = cols[2].datetime_input(
                "Reschedule (UTC)",
                value=cur,
                key=f"sched-{draft.id}-when",
            ) if hasattr(st, "datetime_input") else _fallback_datetime_input(cur, f"sched-{draft.id}")
            new_when_utc = _to_utc(new_when)
            existing = draft.suggested_publish_at
            existing_utc = (
                existing.astimezone(timezone.utc)
                if existing.tzinfo is not None
                else existing.replace(tzinfo=timezone.utc)
            )
            if new_when_utc != existing_utc:
                _persist_draft_edits(
                    draft_id=draft.id,
                    title=draft.title,
                    body=draft.body,
                    hashtags=draft.hashtags,
                    suggested_publish_at=new_when_utc,
                    status=None,
                )
                st.rerun()
            if draft.id in violations:
                cols[3].error("⚠️ <90 min from another post")
            else:
                cols[3].success("OK")


def _detect_cadence_violations(drafts: list[DraftView]) -> set[int]:
    """Return draft ids that sit within CADENCE_MIN_GAP minutes of another same-account draft."""
    bad: set[int] = set()
    by_account: dict[str, list[DraftView]] = {}
    for d in drafts:
        by_account.setdefault(d.account, []).append(d)
    for account_drafts in by_account.values():
        ordered = sorted(account_drafts, key=lambda x: x.suggested_publish_at)
        for prev, cur in zip(ordered, ordered[1:]):
            gap = cur.suggested_publish_at - prev.suggested_publish_at
            if gap < timedelta(minutes=CADENCE_MIN_GAP):
                bad.add(prev.id)
                bad.add(cur.id)
    return bad


# ---------- Tab 3: Inspiration ----------


def _render_inspiration_tab(account: str) -> None:
    st.subheader("Competitor inspiration feed")
    with get_session() as session:
        stmt = select(Idea).where(Idea.source == "xhs_competitor")
        if account != "all":
            stmt = stmt.where(Idea.target_account == account)
        stmt = stmt.order_by(Idea.engagement_score.desc())
        ideas = [
            IdeaView(
                id=int(i.id or 0),
                source_url=i.source_url,
                raw_title=i.raw_title,
                raw_body=i.raw_body,
                engagement_score=i.engagement_score,
                category=i.category,
            )
            for i in session.exec(stmt)
        ]

    if not ideas:
        st.info(
            "No competitor ideas yet. Task 3 (XHS Competitor Tracker) fills this. "
            "Until then this stays empty."
        )
        return

    for idea in ideas:
        with st.container(border=True):
            cols = st.columns([3, 1])
            cols[0].markdown(f"**{idea.raw_title}**")
            cols[0].markdown(f"[source]({idea.source_url})")
            cols[0].caption(idea.raw_body[:200] + ("…" if len(idea.raw_body) > 200 else ""))
            cols[1].metric("Engagement", f"{idea.engagement_score:.1f}")
            cols[1].markdown(f"Category: `{idea.category}`")
            if cols[1].button("✨ Generate inspired draft", key=f"inspire-{idea.id}"):
                _trigger_inspirer(idea.id)


def _trigger_inspirer(idea_id: int | None) -> None:
    if idea_id is None:
        return
    if not _INSPIRER_AVAILABLE or _inspirer is None:
        st.warning(
            "xhs_op.generate.inspirer not importable yet (Task 3 in flight). Skipping."
        )
        return
    try:
        new_draft_id = _inspirer.draft(idea_id)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Inspirer call failed: {exc}")
        return
    st.success(f"Inspired draft #{new_draft_id} created — check the Queue tab.")


# ---------- Tab 4: Comments ----------


def _render_comments_tab(account: str) -> None:
    st.subheader("Pending comment replies")
    with get_session() as session:
        stmt = select(Comment, Post).join(Post, Comment.post_id == Post.id)
        stmt = stmt.where(Comment.status == "pending_approval")
        if account != "all":
            stmt = stmt.where(Post.account == account)
        views = [
            CommentView(
                id=int(comment.id or 0),
                post_title=post.title,
                xhs_note_id=post.xhs_note_id,
                author=comment.author,
                text=comment.text,
                intent=comment.intent,
                drafted_reply=comment.drafted_reply or "",
            )
            for comment, post in session.exec(stmt)
        ]

    if not views:
        st.info("No comments waiting for approval.")
        return

    for cv in views:
        key = f"cmt-{cv.id}"
        with st.container(border=True):
            cols = st.columns([3, 2])
            cols[0].markdown(f"**Post:** {cv.post_title}")
            cols[0].markdown(f"XHS note: `{cv.xhs_note_id}`")
            cols[0].markdown(f"**{cv.author}:** {cv.text}")
            if cv.intent:
                cols[1].markdown(f"Intent: `{cv.intent}`")
            reply_val = st.text_area(
                "Drafted reply",
                value=cv.drafted_reply,
                key=f"{key}-reply",
                height=100,
            )
            btn_cols = st.columns(3)
            approve = btn_cols[0].button("✅ Approve", key=f"{key}-approve")
            save = btn_cols[1].button("✏️ Save", key=f"{key}-save")
            skip = btn_cols[2].button("⏭️ Skip", key=f"{key}-skip")
            if approve or save or skip:
                new_status = (
                    "approved" if approve else ("skipped" if skip else "pending_approval")
                )
                _persist_comment(cv.id, reply_val, new_status)
                st.rerun()


def _persist_comment(comment_id: int | None, reply: str, status: str) -> None:
    if comment_id is None:
        return
    with get_session() as session:
        c = session.get(Comment, comment_id)
        if c is None:
            return
        c.drafted_reply = reply
        c.status = status
        session.add(c)


# ---------- Tab 5: Analytics ----------


def _render_analytics_tab(account: str) -> None:
    st.subheader("Posts per account per day (last 14 days)")
    since = _utc_now() - timedelta(days=14)
    with get_session() as session:
        stmt = select(Post).where(Post.posted_at >= since)
        if account != "all":
            stmt = stmt.where(Post.account == account)
        recent_views = [
            PostView(id=int(p.id or 0), account=p.account, title=p.title, posted_at=p.posted_at)
            for p in session.exec(stmt)
        ]

    if not recent_views:
        st.info("No posts in the last 14 days.")
    else:
        # Build chart data: dict[date_str, dict[account, count]].
        chart: dict[str, dict[str, int]] = {}
        for p in recent_views:
            day = p.posted_at.date().isoformat()
            row = chart.setdefault(day, {})
            row[p.account] = row.get(p.account, 0) + 1
        # Streamlit's st.bar_chart accepts a dict-of-dicts via DataFrame-like input.
        # We hand it a plain dict keyed by date with per-account counts.
        all_accounts = sorted({a for r in chart.values() for a in r})
        ordered_days = sorted(chart)
        data = {acc: [chart[d].get(acc, 0) for d in ordered_days] for acc in all_accounts}
        data["_day"] = ordered_days  # type: ignore[assignment]
        try:
            import pandas as pd  # streamlit ships pandas

            df = pd.DataFrame(data).set_index("_day")
            st.bar_chart(df)
        except Exception:
            # Fallback: skip pandas, render basic chart per account.
            for acc in all_accounts:
                st.caption(f"{acc}")
                st.bar_chart({d: chart[d].get(acc, 0) for d in ordered_days})

    st.subheader("Top posts by latest likes")
    with get_session() as session:
        stmt = select(Post)
        if account != "all":
            stmt = stmt.where(Post.account == account)
        all_posts = list(session.exec(stmt))
        if not all_posts:
            st.info("No posts yet.")
            return
        # Map post -> latest metric, materializing as DTOs while session is open.
        ranked_rows: list[dict[str, Any]] = []
        for p in all_posts:
            m_stmt = (
                select(Metric)
                .where(Metric.post_id == p.id)
                .order_by(Metric.measured_at.desc())
                .limit(1)
            )
            latest = session.exec(m_stmt).first()
            ranked_rows.append(
                {
                    "account": p.account,
                    "title": p.title,
                    "posted_at": p.posted_at.isoformat(),
                    "likes": latest.likes if latest else 0,
                    "comments": latest.comments if latest else 0,
                    "saves": latest.saves if latest else 0,
                }
            )

    ranked_rows.sort(key=lambda r: r["likes"], reverse=True)
    st.dataframe(ranked_rows[:10], use_container_width=True)


# ---------- App entry ----------


def main() -> None:
    st.set_page_config(page_title="XHS Approval Dashboard", layout="wide")
    _ensure_db()

    st.title("XHS Approval Dashboard")
    st.caption("Review drafts · schedule posts · approve comment replies.")

    with st.sidebar:
        st.header("Filters")
        account = st.selectbox("Account", ACCOUNT_OPTIONS, index=0)
        st.markdown("---")
        st.caption("Status flags")
        st.write(f"LLM router: {'✅' if _LLM_AVAILABLE else '⏳ Task 4'}")
        st.write(f"Inspirer: {'✅' if _INSPIRER_AVAILABLE else '⏳ Task 3'}")
        st.write(f"Image gen: {'✅' if _IMAGE_AVAILABLE else '⏳ unavailable'}")
        x_cookie_ok = __import__('pathlib').Path('data/cookies/x.json').exists()
        settings = get_settings()
        x_connected = x_cookie_ok and bool(settings.x_username)
        st.write(f"X connected: {'✅ connected' if x_connected else '❌ not connected'}")
        available_models = sorted(set(settings.model_routing.values()))
        model_override = st.selectbox(
            "Text model override",
            options=["(auto)", *available_models],
            index=0,
            help="Pick a model here to override persona routing during text regeneration.",
        )
        regen_q = st.session_state.get("regen_requests", [])
        st.write(f"Pending image regens this session: {len(regen_q)}")

    tab_queue, tab_schedule, tab_inspire, tab_comments, tab_analytics = st.tabs(
        ["Queue", "Schedule", "Inspiration", "Comments", "Analytics"]
    )

    with tab_queue:
        _render_queue_tab(account, None if model_override == "(auto)" else model_override)
    with tab_schedule:
        _render_schedule_tab(account)
    with tab_inspire:
        _render_inspiration_tab(account)
    with tab_comments:
        _render_comments_tab(account)
    with tab_analytics:
        _render_analytics_tab(account)


# Streamlit runs the module top-to-bottom, so just call main().
main()
