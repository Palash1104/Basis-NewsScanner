"""The web UI (SPEC 11): FastAPI + Jinja2 + HTMX, plain CSS, localhost only.

It **reads** the database the scheduled pipeline writes, and never writes to it: the engine
sets `query_only`, sessions never autoflush, and nothing here runs `init_db`. Schema changes
stay with `newsdesk run`; restart the server after one.

Nothing in a request touches the network either: prices come from `price_cache`, never from
yfinance, so a page can't race the pipeline's price step or spend its rate limit.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import AssetConfig, Settings, load_assets, load_settings
from app.db import make_read_only_engine, make_read_only_session_factory
from app.models import Impact, Run, utcnow
from app.pipeline.prices import format_move
from app.pipeline.scoring import track_record
from app.web import palette

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
# How far back the "moving" strip looks for calls with a price attached.
TICKER_HOURS = 48
TICKER_LIMIT = 8
RAMP_STEPS = (100, 200, 300, 400, 500, 600, 700, 800, 900)

SEMANTIC_SWATCHES = (
    ("Page", "--color-bg", "the ground"),
    ("Surface", "--color-surface", "filled panels, inputs, chips"),
    ("Ink", "--color-text", "body text, and a price that rose"),
    ("Muted", "--color-muted", "meta lines, ink at 70%"),
    ("Divider", "--color-divider", "2px section rules"),
    ("Accent", "--color-accent", "primary button, focus ring"),
    ("Link", "--color-link", "links, and a price that fell"),
    ("Accent 100", "--color-accent-100", "tag background"),
)

NAV = (
    {"name": "today", "label": "Today", "href": "/", "note": ""},
    {"name": "track", "label": "Track record", "href": None, "note": "step 3"},
    {"name": "assets", "label": "Assets", "href": None, "note": "step 4"},
    {"name": "runs", "label": "Runs", "href": None, "note": "step 5"},
    {"name": "design", "label": "Design", "href": "/design", "note": ""},
)


@dataclass(frozen=True)
class TickerItem:
    """One asset's move since the story that called it broke. Not a live quote."""

    name: str
    move: str
    up: bool


def get_session(request: Request) -> Iterator[Session]:
    """One short-lived read-only session per request, closed before the page renders."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as session:
        yield session


# FastAPI resolves this per request; `get_session` closes it when the response is done.
ReadSession = Annotated[Session, Depends(get_session)]


def ticker_items(
    session: Session, assets: dict[str, AssetConfig], now: datetime, limit: int = TICKER_LIMIT
) -> list[TickerItem]:
    """The biggest moves since news among recent calls (design/NOTES.md GAP 16, redefined).

    One row per symbol, largest move first. Every number is a move since its story broke,
    which is why the strip says "since news" and carries the time the pipeline last ran.
    """
    since = now - timedelta(hours=TICKER_HOURS)
    query = (
        select(Impact)
        .where(
            Impact.created_at >= since,
            Impact.move_at_detection_pct.is_not(None),
            Impact.reference_price.is_not(None),
        )
        .order_by(Impact.created_at.desc())
    )
    best: dict[str, Impact] = {}
    for impact in session.scalars(query):
        current = best.get(impact.symbol)
        move = abs(impact.move_at_detection_pct or 0.0)
        if current is None or move > abs(current.move_at_detection_pct or 0.0):
            best[impact.symbol] = impact

    items = []
    ranked = sorted(best.values(), key=lambda i: abs(i.move_at_detection_pct or 0), reverse=True)
    for impact in ranked:
        asset = assets.get(impact.symbol)
        if asset is None or impact.reference_price is None or impact.move_at_detection_pct is None:
            continue
        items.append(
            TickerItem(
                name=asset.display_name,
                move=format_move(asset, impact.reference_price, impact.move_at_detection_pct),
                up=impact.move_at_detection_pct >= 0,
            )
        )
    return items[:limit]


def last_pipeline_finish(session: Session) -> datetime | None:
    query = (
        select(Run.finished_at)
        .where(Run.kind == "pipeline", Run.finished_at.is_not(None))
        .order_by(Run.finished_at.desc())
        .limit(1)
    )
    return session.scalars(query).first()


def base_context(
    request: Request, session: Session, active: str, now: datetime | None = None
) -> dict[str, object]:
    """What every page needs: nav, the moving strip, and the footer's time zone."""
    settings: Settings = request.app.state.settings
    assets: dict[str, AssetConfig] = request.app.state.assets
    now = now or utcnow()
    finished = last_pipeline_finish(session)
    as_of = (
        finished.astimezone(settings.tz).strftime("%d %b %H:%M %Z")
        if finished
        else "no pipeline run yet"
    )
    return {
        "request": request,
        "nav": NAV,
        "active": active,
        "ticker": ticker_items(session, assets, now),
        "ticker_hours": TICKER_HOURS,
        "ticker_as_of": as_of,
        "timezone": settings.timezone,
    }


def sample_track_rows(session: Session, limit: int = 4) -> list[dict[str, str]]:
    """A few real track-record rows, so the table styles are checked against real text."""
    rows = []
    for row in track_record(session, "rule_id")[:limit]:
        rate = f"{row.rate:.0%}" if row.rate is not None else "-"
        rows.append(
            {
                "rule": row.key,
                "horizon": f"{row.horizon_days}d",
                "hit": str(row.hits),
                "miss": str(row.misses),
                "rate": rate if row.judged else "not judged yet",
            }
        )
    return rows


def create_app(
    settings: Settings | None = None, session_factory: sessionmaker[Session] | None = None
) -> FastAPI:
    """Build the app. `init_db` is never called: the web UI does not migrate anything."""
    settings = settings or load_settings()
    if session_factory is None:
        engine = make_read_only_engine(settings.resolve_path(settings.paths.database))
        session_factory = make_read_only_session_factory(engine)

    web = FastAPI(title="Newsdesk", docs_url=None, redoc_url=None, openapi_url=None)
    web.state.settings = settings
    web.state.session_factory = session_factory
    web.state.assets = {asset.symbol: asset for asset in load_assets()}
    web.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=WEB_DIR / "templates")

    @web.get("/", response_class=HTMLResponse)
    @web.get("/design", response_class=HTMLResponse)
    def design(request: Request, session: ReadSession) -> HTMLResponse:
        """Step 0: the tokens, both themes and the contrast they achieve. The feed takes
        over `/` at step 1; this page stays at `/design` as the style guide."""
        context = base_context(request, session, active="design")
        context |= {
            "semantic": SEMANTIC_SWATCHES,
            "steps": RAMP_STEPS,
            "contrast": palette.rows(),
            "sample_rows": sample_track_rows(session),
        }
        return templates.TemplateResponse(request, "foundation.html", context)

    return web


def app_from_env() -> FastAPI:
    """Factory for `uvicorn --reload`, which needs an import string."""
    return create_app()
