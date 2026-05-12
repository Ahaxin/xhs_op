from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, Column
from sqlmodel import Field, Session, SQLModel, create_engine

DB_PATH = Path("data/xhs_op.db")
DB_URL = f"sqlite:///{DB_PATH.as_posix()}"

engine = create_engine(DB_URL, connect_args={"check_same_thread": False})


class Idea(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    source_url: str
    source_id: str = Field(index=True)
    raw_title: str
    raw_body: str
    raw_lang: str
    engagement_score: float = 0.0
    fetched_at: datetime
    category: str = Field(index=True)
    target_account: str = Field(index=True)
    # Named `extra` (not `metadata`) because SQLModel/SQLAlchemy reserves `metadata`.
    extra: dict = Field(default_factory=dict, sa_column=Column(JSON))
    processed: bool = Field(default=False, index=True)


class Draft(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    idea_id: int | None = Field(default=None, foreign_key="idea.id")
    account: str = Field(index=True)
    persona: str
    title: str
    body: str
    hashtags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    image_paths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    suggested_publish_at: datetime
    status: str = Field(default="pending_review", index=True)
    inspiration_note: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Post(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    draft_id: int = Field(foreign_key="draft.id", index=True)
    account: str = Field(index=True)
    xhs_note_id: str = Field(index=True)
    title: str
    body: str
    image_paths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    posted_at: datetime
    status: str = Field(default="live")


class Comment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="post.id", index=True)
    xhs_comment_id: str = Field(index=True)
    author: str
    text: str
    intent: str | None = None
    drafted_reply: str | None = None
    status: str = Field(default="pending_approval", index=True)
    received_at: datetime
    replied_at: datetime | None = None


class Metric(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="post.id", index=True)
    measured_at: datetime
    likes: int = 0
    saves: int = 0
    comments: int = 0
    shares: int = 0
    views: int | None = None


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
