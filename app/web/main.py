"""The web UI (SPEC 11): FastAPI + Jinja2 + HTMX, plain CSS, localhost only.

It **reads** the database the scheduled pipeline writes, and never writes to it: the engine
sets `query_only`, sessions never autoflush, and nothing here runs `init_db`. Schema changes
stay with `newsdesk run`; restart the server after one.

Nothing in a request touches the network either: prices come from `price_cache`, never from
yfinance, so a page can't race the pipeline's price step or spend its rate limit.
"""

import logging
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import health
from app.config import AssetConfig, Settings, load_assets, load_settings
from app.db import make_read_only_engine, make_read_only_session_factory
from app.models import Impact, Run, utcnow
from app.pipeline.prices import format_move
from app.pipeline.scoring import track_record
from app.presentation import ORDER_WORDS, ORIGIN_LABEL, story_age
from app.schedule import pipeline_hours
from app.web import palette, queries

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
    {"name": "track", "label": "Track record", "href": "/track-record", "note": ""},
    {"name": "assets", "label": "Assets", "href": "/assets", "note": ""},
    {"name": "runs", "label": "Runs", "href": "/runs", "note": ""},
)
# The style guide stays reachable at /design, but it is not in the design's nav, so it is not
# in ours either (user, 2026-09-21).

REGION_FILTERS = (("", "All"), ("US", "US"), ("India", "India"), ("Global", "Global"))
RANGE_OPTIONS = (("1d", "1D"), ("1w", "1W"), ("1m", "1M"))
SPARK_WIDTH, SPARK_HEIGHT, SPARK_PAD = 68, 22, 2
# The mockup's story detail draws a bigger line, at 300x64 with a 2px stroke.
STORY_SPARK_WIDTH, STORY_SPARK_HEIGHT, STORY_SPARK_PAD = 300, 64, 6
# One week everywhere a line sits beside a call: the feed's chips (the 1D/1W/1M control still
# moves those), the story page's rows, and both rail lists (user, 2026-09-22). It is also what
# the mockup's story rail says over its own charts, "Commodities affected - 7 days".
STORY_WINDOW = queries.RAIL_WINDOW
# The mockup's watchlist card draws 300x72; the asset page is that card.
CARD_SPARK_WIDTH, CARD_SPARK_HEIGHT, CARD_SPARK_PAD = 300, 72, 7
# The rail is narrow, so its watchlist draws a smaller line than the mockup's 68x22.
RAIL_SPARK_WIDTH, RAIL_SPARK_HEIGHT, RAIL_SPARK_PAD = 68, 20, 2
ASSET_WINDOW = "1m"
ASSET_WINDOW_LABEL = "last month of daily bars"
RUN_WINDOWS = (3, 7, 14)


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


def _in_zone(value: datetime | None, settings: Settings) -> str:
    """Times are stored UTC and shown in settings.timezone (SPEC 0).

    Today's times are bare, as in the mockup ("06:20"); anything older carries its date,
    because a page can hold a story from last week next to one from this morning.
    """
    if value is None:
        return ""
    local = value.astimezone(settings.tz)
    today = datetime.now(settings.tz).date()
    return local.strftime("%H:%M" if local.date() == today else "%d %b %H:%M")


def _stamp(value: datetime | None, settings: Settings) -> str:
    """A clock time with its zone written out, for anything a reader might mistake for live
    data: "13:19 IST" today, "21 Sep 13:19 IST" if the last run was longer ago than that."""
    if value is None:
        return "never"
    local = value.astimezone(settings.tz)
    today = datetime.now(settings.tz).date()
    return local.strftime("%H:%M %Z" if local.date() == today else "%d %b %H:%M %Z")


def _watchlist_context(
    session: Session,
    assets: dict[str, AssetConfig],
    settings: Settings,
    now: datetime,
    wanted: Sequence[str],
) -> dict[str, object]:
    """The rail's watchlist, rendered the same way from the page and from the fragment."""
    symbols = queries.watchlist_symbols(wanted, assets, settings.web.watchlist_max)
    if not symbols and wanted != settings.web.watchlist:
        symbols = queries.watchlist_symbols(
            settings.web.watchlist, assets, settings.web.watchlist_max
        )
    return {
        "watchlist": queries.watchlist_rows(session, symbols, assets, now),
        "watchlist_symbols": symbols,
        "rail_points": lambda item: queries.sparkline_points(
            item, RAIL_SPARK_WIDTH, RAIL_SPARK_HEIGHT, RAIL_SPARK_PAD
        ),
        "rail_spark": (RAIL_SPARK_WIDTH, RAIL_SPARK_HEIGHT),
        "mover_hours": queries.MOVER_HOURS,
        "watchlist_max": settings.web.watchlist_max,
    }


def _rate_gates(settings: Settings) -> dict[str, int]:
    """The thresholds a rate must clear before it is shown, and before it stops being early."""
    return {
        "minimum": settings.scoring.min_samples_to_show_rate,
        "min_stories": settings.scoring.min_stories_to_show_rate,
        "early_below_stories": settings.scoring.early_rate_below_stories,
    }


def _unit(asset: AssetConfig | None) -> str:
    """The mockup's "LME 3M · $/t" line, from what assets.yaml actually knows."""
    if asset is None:
        return ""
    return " · ".join(part for part in (asset.exchange, asset.currency) if part)


def _next_digest(settings: Settings, now: datetime) -> str:
    """The next time a digest goes out, for the mockup's "next brief" stamp."""
    local = now.astimezone(settings.tz)
    times = sorted(settings.delivery.digest_times)
    for value in times:
        hour, minute = (int(part) for part in value.split(":"))
        if (local.hour, local.minute) < (hour, minute):
            return value
    return times[0] if times else ""


def create_app(
    settings: Settings | None = None, session_factory: sessionmaker[Session] | None = None
) -> FastAPI:
    """Build the app. `init_db` is never called: the web UI does not migrate anything."""
    settings = settings or load_settings()
    if session_factory is None:
        engine = make_read_only_engine(settings.resolve_path(settings.paths.database))
        session_factory = make_read_only_session_factory(engine)

    web = FastAPI(title="BASIS", docs_url=None, redoc_url=None, openapi_url=None)
    web.state.settings = settings
    web.state.session_factory = session_factory
    web.state.assets = {asset.symbol: asset for asset in load_assets()}
    web.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.filters["ist"] = lambda value: _in_zone(value, settings)

    @web.middleware("http")
    async def always_revalidate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Nothing here may be served from the browser's cache without asking first.

        Starlette's static files carry no `Cache-Control`, so a browser applies heuristic
        freshness and can hold a stylesheet for hours - edits showed up in one browser and
        not another (user, 2026-09-22). A page is a view of a database the pipeline rewrites
        every three hours, so a stale one is wrong for the same reason. `no-cache` still lets
        the browser keep the file; it just has to revalidate, and an unchanged file comes
        back as a 304.
        """
        response: Response = await call_next(request)
        response.headers.setdefault("cache-control", "no-cache")
        return response

    @web.get("/", response_class=HTMLResponse)
    def today(
        request: Request,
        session: ReadSession,
        q: str = "",
        region: str = "",
        category: str = "",
        range: str = queries.DEFAULT_WINDOW,  # noqa: A002 - the query parameter's name
        offset: int = 0,
    ) -> HTMLResponse:
        """The feed: today's stories, most important first, with what each one calls."""
        now = utcnow()
        window = range if range in queries.WINDOWS else queries.DEFAULT_WINDOW
        assets: dict[str, AssetConfig] = request.app.state.assets
        stories, more = queries.feed_page(
            session,
            now=now,
            region=region or None,
            category=category or None,
            query=q,
            offset=max(offset, 0),
        )
        items = queries.feed_stories(
            session, stories, assets, settings, now, track_record(session, "rule_id")
        )
        symbols = [call.symbol for item in items for call in item.shown]
        series = queries.series_for(session, symbols, window, now)

        context = base_context(request, session, active="today", now=now)
        context |= {
            "stories": items,
            "more": more,
            "offset": max(offset, 0),
            "page_size": queries.PAGE_SIZE,
            "hours": queries.FEED_HOURS,
            "query": q,
            "region": region,
            "category": category,
            "range": window,
            "filtered": bool(region or category),
            "regions": REGION_FILTERS,
            "ranges": RANGE_OPTIONS,
            "categories": queries.categories_in_use(session),
            "story_count": queries.story_count(session, now),
            "today": now.astimezone(settings.tz).strftime("%A %d %B %Y"),
            "sparklines": series,
            "units": {symbol: _unit(assets.get(symbol)) for symbol in symbols},
            "points": lambda item: queries.sparkline_points(
                item, SPARK_WIDTH, SPARK_HEIGHT, SPARK_PAD
            ),
            "order_words": ORDER_WORDS,
            "updated": context["ticker_as_of"],
            "next_digest": _next_digest(settings, now),
            "movers": queries.movers(session, assets, now),
            "mover_hours": queries.MOVER_HOURS,
            "prices_as_of": _stamp(last_pipeline_finish(session), settings),
            "universe": sorted(assets.values(), key=lambda asset: asset.display_name),
        }
        context |= _watchlist_context(session, assets, settings, now, settings.web.watchlist)
        # HTMX asks for the list alone; a plain visit gets the whole page.
        name = "_feed.html" if request.headers.get("hx-request") else "index.html"
        return templates.TemplateResponse(request, name, context)

    @web.get("/watchlist", response_class=HTMLResponse)
    def watchlist(request: Request, session: ReadSession, symbols: str = "") -> HTMLResponse:
        """The watchlist rows alone, for a browser that keeps its own list.

        The page ships with the settings.yaml default already rendered; this is what the
        editor asks for afterwards. Unknown symbols are dropped here, so whatever a browser
        has stored - stale, hand-edited, from an older universe - can only ever show assets
        that exist.
        """
        assets: dict[str, AssetConfig] = request.app.state.assets
        context = {"request": request}
        context |= _watchlist_context(
            session, assets, settings, utcnow(), symbols.split(",") if symbols else []
        )
        return templates.TemplateResponse(request, "_watchlist.html", context)

    @web.get("/story/{story_id}", response_class=HTMLResponse)
    def story(request: Request, session: ReadSession, story_id: int) -> HTMLResponse:
        """One story: what it says, every call it made, and how those calls are doing."""
        now = utcnow()
        assets: dict[str, AssetConfig] = request.app.state.assets
        found = queries.load_story(session, story_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"no story {story_id}")
        detail = queries.story_detail(
            session, found, assets, settings, now, track_record(session, "rule_id")
        )
        symbols = [call.symbol for call in detail.calls.shown]
        context = base_context(request, session, active="today", now=now)
        context |= {
            "detail": detail,
            "sparklines": queries.series_for(session, symbols, STORY_WINDOW, now),
            "units": {symbol: _unit(assets.get(symbol)) for symbol in symbols},
            "points": lambda item: queries.sparkline_points(
                item, STORY_SPARK_WIDTH, STORY_SPARK_HEIGHT, STORY_SPARK_PAD
            ),
            "order_words": ORDER_WORDS,
            "origin_label": ORIGIN_LABEL,
            "age": story_age(found, now),
        }
        return templates.TemplateResponse(request, "story.html", context)

    @web.get("/track-record", response_class=HTMLResponse)
    def track(request: Request, session: ReadSession, horizon: str = "") -> HTMLResponse:
        """How the calls have actually turned out (SPEC 7.9), by rule, event type, origin,
        confidence and horizon."""
        chosen = (
            horizon
            if horizon in {str(days) for days in settings.scoring.horizons_trading_days}
            else ""
        )
        summary, tables = queries.track_tables(session, settings, int(chosen) if chosen else None)
        context = base_context(request, session, active="track")
        context |= {
            "summary": summary,
            "tables": tables,
            "horizon": chosen,
            "horizons": [("", "All")]
            + [(str(days), f"{days}d") for days in settings.scoring.horizons_trading_days],
        }
        name = "_track_tables.html" if request.headers.get("hx-request") else "track_record.html"
        return templates.TemplateResponse(request, name, context)

    @web.get("/assets", response_class=HTMLResponse)
    def asset_index(request: Request, session: ReadSession) -> HTMLResponse:
        """Every universe asset that has been called, most recently called first."""
        assets: dict[str, AssetConfig] = request.app.state.assets
        context = base_context(request, session, active="assets")
        context |= {
            "rows": queries.asset_rows(session, assets, settings),
            "universe": len(assets),
        } | _rate_gates(settings)
        return templates.TemplateResponse(request, "assets.html", context)

    @web.get("/asset/{symbol}", response_class=HTMLResponse)
    def asset_page(request: Request, session: ReadSession, symbol: str) -> HTMLResponse:
        """One asset: what keeps moving it, and whether those calls were right."""
        now = utcnow()
        assets: dict[str, AssetConfig] = request.app.state.assets
        asset = assets.get(symbol)
        if asset is None:
            raise HTTPException(status_code=404, detail=f"{symbol} is not in the universe")
        detail = queries.asset_detail(session, asset, settings, now, window=ASSET_WINDOW)
        context = base_context(request, session, active="assets", now=now)
        context |= {
            "detail": detail,
            "window_label": ASSET_WINDOW_LABEL,
            "points": lambda item: queries.sparkline_points(
                item, CARD_SPARK_WIDTH, CARD_SPARK_HEIGHT, CARD_SPARK_PAD
            ),
            "order_words": ORDER_WORDS,
            "origin_label": ORIGIN_LABEL,
        } | _rate_gates(settings)
        return templates.TemplateResponse(request, "asset.html", context)

    @web.get("/runs", response_class=HTMLResponse)
    def runs(
        request: Request, session: ReadSession, days: int = health.DEFAULT_DAYS
    ) -> HTMLResponse:
        """Recent runs, errors and token usage (SPEC 11), from the same functions
        `newsdesk health` prints."""
        window = days if days in RUN_WINDOWS else health.DEFAULT_DAYS
        view = queries.runs_view(session, settings, utcnow(), window)
        context = base_context(request, session, active="runs")
        context |= {
            "view": view,
            "windows": [(value, f"{value}d") for value in RUN_WINDOWS],
            "slot_hours": ", ".join(
                f"{hour:02d}:00"
                for hour in pipeline_hours(
                    settings.schedule.pipeline_every_hours, settings.delivery.digest_times
                )
            ),
        }
        name = "_runs.html" if request.headers.get("hx-request") else "runs.html"
        return templates.TemplateResponse(request, name, context)

    @web.exception_handler(StarletteHTTPException)
    def not_found(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
        """A typed-in URL that doesn't exist gets a page, not a JSON blob."""
        if exc.status_code != 404:
            raise exc
        with session_factory() as session:  # type: ignore[misc]
            context = base_context(request, session, active="today")
        context |= {"detail_text": str(exc.detail)}
        return templates.TemplateResponse(request, "not_found.html", context, status_code=404)

    @web.get("/design", response_class=HTMLResponse)
    def design(request: Request, session: ReadSession) -> HTMLResponse:
        """The tokens, both themes and the contrast they achieve: the style guide the pages
        are built from."""
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
