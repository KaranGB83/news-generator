import enum
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Category(str, enum.Enum):
    WORLD = "world"
    INDIA = "india"
    MAHARASHTRA = "maharashtra"


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ArticleStatus(str, enum.Enum):
    SELECTED = "selected"
    GENERATED = "generated"
    POSTED = "posted"
    SKIPPED = "skipped"
    FAILED = "failed"


def _in_list(column: str, enum_cls: type[enum.Enum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({values})"


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_in_list("status", RunStatus), name="ck_runs_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), default=RunStatus.RUNNING.value, server_default=RunStatus.RUNNING.value
    )
    triggered_by: Mapped[str] = mapped_column(
        String(32), default="manual", server_default="manual"
    )

    articles: Mapped[list["Article"]] = relationship(back_populates="run")


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        CheckConstraint(_in_list("category", Category), name="ck_articles_category"),
        CheckConstraint(_in_list("status", ArticleStatus), name="ck_articles_status"),
        # At most one posted article per category per day, so a rerun can't double post.
        Index(
            "uq_articles_posted_per_category_day",
            "category",
            "post_date",
            unique=True,
            postgresql_where=text("status = 'posted'"),
        ),
        Index("ix_articles_run_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))

    category: Mapped[str] = mapped_column(String(16))
    language: Mapped[str] = mapped_column(String(8))
    post_date: Mapped[date] = mapped_column(Date)

    source_name: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str] = mapped_column(Text, unique=True)
    source_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    generated_title: Mapped[str | None] = mapped_column(Text)
    generated_body: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(16),
        default=ArticleStatus.SELECTED.value,
        server_default=ArticleStatus.SELECTED.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    kbnews_post_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[Run | None] = relationship(back_populates="articles")
