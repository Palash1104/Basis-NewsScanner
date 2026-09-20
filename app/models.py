"""SQLAlchemy tables. Phase 1: articles, stories, runs."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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
    # Not in SPEC section 6: set when a summary was skipped because the LLM quota ran out, so the
    # next run summarizes it first even if it has dropped out of the top stories.
    summary_pending: Mapped[bool] = mapped_column(default=False)
    # Not in SPEC section 6 originally: set when event extraction was skipped (quota, or an API
    # error), so the next run extracts it first.
    event_pending: Mapped[bool] = mapped_column(default=False)

    articles: Mapped[list["Article"]] = relationship(back_populates="story")
    events: Mapped[list["Event"]] = relationship(
        back_populates="story", order_by="Event.id", cascade="all, delete-orphan"
    )
    impacts: Mapped[list["Impact"]] = relationship(
        back_populates="story", order_by="Impact.id", cascade="all, delete-orphan"
    )

    @property
    def latest_event(self) -> "Event | None":
        return self.events[-1] if self.events else None


class Event(Base):
    """One event extraction for a story (SPEC 7.6). A story gets a new row each time it's
    re-extracted; the latest row is its current event."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    countries: Mapped[list[str]] = mapped_column(JSON, default=list)  # canonical names
    regions: Mapped[list[str]] = mapped_column(JSON, default=list)  # copied from the story
    entities: Mapped[list[str]] = mapped_column(JSON, default=list)
    companies: Mapped[list[str]] = mapped_column(JSON, default=list)
    channels: Mapped[list[str]] = mapped_column(JSON, default=list)
    severity: Mapped[str] = mapped_column(String(16))
    policy_stance: Mapped[str] = mapped_column(String(16))
    # The authority whose stance policy_stance describes, so a Fed decision can't match an RBI
    # rule (added with event prompt v2; not in SPEC section 6 originally).
    policy_actor: Mapped[str | None] = mapped_column(String(64))
    is_new_development: Mapped[bool] = mapped_column()
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    story: Mapped[Story] = relationship(back_populates="events")
    impacts: Mapped[list["Impact"]] = relationship(back_populates="event")


class Impact(Base):
    """One call on one asset from one story (SPEC 7.7). Written once and never edited: it is
    the call as it was made, which the scoring phase judges."""

    __tablename__ = "impacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("stories.id"), index=True)
    # Which extraction produced it: the track record groups by event type and prompt version,
    # and a story has several events once it is re-extracted. Not in SPEC section 6.
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # up | down
    mechanism: Mapped[str] = mapped_column(Text)
    order: Mapped[str] = mapped_column(String(8))  # first | second
    confidence: Mapped[str] = mapped_column(String(8))  # high | medium | low
    # Playbook rules don't state a horizon; the Phase 5 LLM layer does.
    horizon: Mapped[str | None] = mapped_column(String(16))
    origin: Mapped[str] = mapped_column(String(16))  # playbook | llm | both
    rule_id: Mapped[str | None] = mapped_column(String(64), index=True)
    conflict: Mapped[bool] = mapped_column(default=False)
    # Filled in by the Phase 3 price check.
    reference_time: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reference_price: Mapped[float | None] = mapped_column(Float)
    move_at_detection_pct: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    story: Mapped[Story] = relationship(back_populates="impacts")
    event: Mapped[Event | None] = relationship(back_populates="impacts")
    scores: Mapped[list["ImpactScore"]] = relationship(
        back_populates="impact", cascade="all, delete-orphan"
    )


class ImpactScore(Base):
    """How one call turned out at one horizon (SPEC 7.9). Written once, when every input is
    present; a late bar just means the next run writes it."""

    __tablename__ = "impact_scores"
    __table_args__ = (UniqueConstraint("impact_id", "horizon_days"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    impact_id: Mapped[int] = mapped_column(ForeignKey("impacts.id"), index=True)
    horizon_days: Mapped[int] = mapped_column(Integer)  # trading days after the reference
    asset_return: Mapped[float | None] = mapped_column(Float)
    benchmark_symbol: Mapped[str | None] = mapped_column(String(32))
    benchmark_return: Mapped[float | None] = mapped_column(Float)
    excess_return: Mapped[float | None] = mapped_column(Float)
    threshold: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str] = mapped_column(String(16))  # hit | miss | no_move | unscorable
    scored_at: Mapped[datetime] = mapped_column(UTCDateTime)

    impact: Mapped["Impact"] = relationship(back_populates="scores")


class PriceBar(Base):
    """One cached price bar (SPEC section 6 `price_cache`). `volume` is an extra column: a
    zero-volume daily bar is how Yahoo marks an exchange holiday for stocks, and those bars
    must be left out of the volatility baseline."""

    __tablename__ = "price_cache"
    __table_args__ = (UniqueConstraint("symbol", "interval", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    interval: Mapped[str] = mapped_column(String(8))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)  # bar start, stored UTC
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)  # normalized
    source_name: Mapped[str] = mapped_column(String(120), index=True)
    source_region: Mapped[str] = mapped_column(String(8))
    source_weight: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    snippet: Mapped[str] = mapped_column(Text, default="")
    # Not in SPEC section 6: explainer/roundup headline (app.pipeline.classify). Non-news
    # articles never start a story or count as a source.
    non_news: Mapped[bool] = mapped_column(default=False)
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


class LLMRequest(Base):
    """One row per LLM request in roughly the last hour, shared by every process using this
    database, so the per-minute limits count calls made by other runs too (e.g. the smoke test
    just before a pipeline run). Not in SPEC section 6; added for the LLM rate limiter."""

    __tablename__ = "llm_requests"
    __table_args__ = (Index("ix_llm_requests_window", "provider", "model", "requested_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime)
    input_tokens: Mapped[int] = mapped_column(Integer)  # estimate, replaced by the real count


class TickerCheck(Base):
    """One validate-tickers result per symbol per check (SPEC §8), so the app can warn about
    symbols that were never validated, failed, or were last validated over 30 days ago.
    Not in SPEC section 6 originally; added for ticker validation."""

    __tablename__ = "ticker_checks"
    __table_args__ = (Index("ix_ticker_checks_symbol_time", "symbol", "checked_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16))  # ok | empty | stale | error
    rows: Mapped[int] = mapped_column(Integer, default=0)
    last_bar_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_close: Mapped[float | None] = mapped_column(Float)
    yahoo_name: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str | None] = mapped_column(String(8))
    exchange: Mapped[str | None] = mapped_column(String(16))
    timezone: Mapped[str | None] = mapped_column(String(48))  # the exchange's time zone
    instrument_type: Mapped[str | None] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[list[str]] = mapped_column(JSON, default=list)  # review items, not failures
