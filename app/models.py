"""SQLAlchemy tables. Phase 1: articles, stories, runs."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Stores datetimes as naive UTC in SQLite and returns timezone-aware UTC values.

    Naive datetimes are rejected on write so local times can never sneak in.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime column")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Story(Base):
    __tablename__ = "stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    # Wall-clock time the story was created or its summary was last written. Attaching an
    # article does not change it, so digests only re-send stories whose summary changed.
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(40))
    regions: Mapped[list[str]] = mapped_column(JSON, default=list)
    sources_disagree: Mapped[bool] = mapped_column(default=False)
    disagreement_note: Mapped[str | None] = mapped_column(Text)
    importance_score: Mapped[float] = mapped_column(Float, default=0.0)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    region_diversity: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    processed_article_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_source_regions: Mapped[list[str]] = mapped_column(JSON, default=list)
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    # Not in SPEC section 6, but section 14 requires every stored LLM output to record its model.
    model: Mapped[str | None] = mapped_column(String(64))

    articles: Mapped[list["Article"]] = relationship(back_populates="story")


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)  # normalized
    source_name: Mapped[str] = mapped_column(String(120), index=True)
    source_region: Mapped[str] = mapped_column(String(8))
    source_weight: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    snippet: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)
    story_id: Mapped[int | None] = mapped_column(ForeignKey("stories.id"), index=True)

    story: Mapped[Story | None] = relationship(back_populates="articles")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # pipeline | digest | score
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    articles_fetched: Mapped[int] = mapped_column(Integer, default=0)
    stories_processed: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class LLMDailyUsage(Base):
    """Requests and tokens per provider/model per quota day, so daily limits hold across runs.
    Not in SPEC section 6; added for the LLM rate limiter."""

    __tablename__ = "llm_daily_usage"
    __table_args__ = (UniqueConstraint("day", "provider", "model"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD in the quota time zone
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    requests: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
