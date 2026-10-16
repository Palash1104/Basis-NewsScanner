"""The web UI (SPEC 11): FastAPI + Jinja2 + HTMX, plain CSS, localhost only.

It **reads** the database the scheduled pipeline writes, and never writes to it: the engine
sets `query_only`, sessions never autoflush, and nothing here runs `init_db`. Schema changes
stay with `newsdesk run`; restart the server after one.

Nothing in a request touches the network either: prices come from `price_cache`, never from
yfinance, so a page can't race the pipeline's price step or spend its rate limit.

Two exceptions, both on /watchlist and both the user's own action (2026-10-16):

* "Preview" for a stock about to be added asks Yahoo for its ticker and Google News for its
  headlines (app/watch/preview.py), so its aliases can be judged before it is added.
* "Add to watchlist" and "Remove" write config/watchlist.yaml (app/watch/watchlist_edit.py) -
  never the database. They are the only POST routes, and each checks that the request came
  from this app's own page: the Host is localhost, the Origin (or Referer) is this app, and
  the form carries a token only this app's pages hold. Another website open in the same
  browser can't change the watchlist by posting to localhost.
"""

import logging
import secrets
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import health
from app.config import (
    CONFIG_DIR,
    AssetConfig,
    Settings,
    WatchAliases,
    WatchlistFile,
    load_assets,
    load_settings,
    load_watchlist_file,
)
from app.db import make_read_only_engine, make_read_only_session_factory
from app.feed_health import feed_health
from app.models import Impact, Run, utcnow
from app.pipeline.prices import format_move
from app.pipeline.scoring import track_record
from app.presentation import ORDER_WORDS, ORIGIN_LABEL, story_age
from app.schedule import pipeline_hours, start_of_news_day
from app.watch import companies as company_list
from app.watch import watchlist_edit
from app.watch.preview import preview as preview_headlines
from app.watch.scoring import lead_times
from app.web import palette, queries, watch_queries

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
    # The design's header has "Today" and "Watchlist" side by side.
    {"name": "watchlist", "label": "Watchlist", "href": "/watchlist", "note": ""},
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


def _day_start(settings: Settings, now: datetime) -> datetime:
    """Where the feed's first page reaches back to: the same "today" the digest uses (22:00
    the night before, `delivery.day_starts_at`), so the page and the alert agree on what
    today's news is. Midnight if that setting is off."""
    return start_of_news_day(settings, now) or now.astimezone(settings.tz).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _stamp(value: datetime | None, settings: Settings) -> str:
    """A clock time with its zone written out, for anything a reader might mistake for live
    data: "13:19 IST" today, "21 Sep 13:19 IST" if the last run was longer ago than that."""
    if value is None:
        return "never"
    local = value.astimezone(settings.tz)
    today = datetime.now(settings.tz).date()
    return local.strftime("%H:%M %Z" if local.date() == today else "%d %b %H:%M %Z")


def _rail_watchlist(
    request: Request, session: Session, settings: Settings, now: datetime
) -> dict[str, object]:
    """The rail's watchlist: config/watchlist.yaml, stocks and commodities, rendered on the
    server. The list a browser used to keep in localStorage is gone (user, 2026-10-07)."""
    watchlist = current_watchlist(request)
    assets: dict[str, AssetConfig] = request.app.state.assets
    return {
        "rail_watch": watch_queries.watch_cards(session, watchlist, assets, settings, now),
        "rail_points": lambda item: queries.sparkline_points(
            item, RAIL_SPARK_WIDTH, RAIL_SPARK_HEIGHT, RAIL_SPARK_PAD
        ),
        "rail_spark": (RAIL_SPARK_WIDTH, RAIL_SPARK_HEIGHT),
        "mover_hours": queries.MOVER_HOURS,
    }


def _load_watchlist(path: Path | None = None) -> WatchlistFile:
    """The watchlist, or an empty one: a broken watchlist.yaml must not take the pages down."""
    try:
        return load_watchlist_file(path)
    except Exception as exc:
        log.warning("watchlist.yaml could not be loaded: %s", exc)
        return WatchlistFile(watchlist=[])


def _file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def current_watchlist(request: Request) -> WatchlistFile:
    """config/watchlist.yaml as it is now: re-read whenever it changed, so an edit made on
    the page (or by hand) shows on the next request without restarting the server."""
    state = request.app.state
    mtime = _file_mtime(state.watchlist_path)
    if mtime != state.watchlist_mtime:
        state.watchlist = _load_watchlist(state.watchlist_path)
        state.watchlist_mtime = mtime
    return state.watchlist


LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def _host_name(host: str) -> str:
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0]


def write_refused(request: Request, form: dict[str, str]) -> str | None:
    """Why a write request must be refused, or None when it came from this app's own page:
    the Host is one of ours (a site can't rebind its name to 127.0.0.1 and pass), the Origin
    - or the Referer, when a browser sends no Origin - is this app, the browser doesn't call
    it cross-site, and the form carries this app's token."""
    state = request.app.state
    host = request.headers.get("host", "")
    if _host_name(host) not in state.allowed_hosts:
        return "not a local request"
    expected = f"{request.url.scheme}://{host}"
    origin = request.headers.get("origin")
    if origin is not None:
        if origin != expected:
            return "sent from another site"
    elif not request.headers.get("referer", "").startswith(f"{expected}/"):
        return "not sent from this app's page"
    if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
        return "sent from another site"
    if not secrets.compare_digest(form.get("token", ""), state.write_token):
        return "the page's token is missing or out of date; reload the page"
    return None


async def read_form(request: Request) -> dict[str, str]:
    """An urlencoded form, read by hand: FastAPI's Form() needs python-multipart, and a
    plain HTML form is all these two routes take."""
    body = (await request.body()).decode("utf-8", errors="replace")
    return {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}


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
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    watchlist_path: Path | None = None,
    companies_path: Path | None = None,
    backup_dir: Path | None = None,
    allowed_hosts: frozenset[str] = LOCAL_HOSTS,
    preview_transport: object | None = None,
    ticker_fetch: Callable | None = None,
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
    web.state.watchlist_path = watchlist_path or CONFIG_DIR / "watchlist.yaml"
    web.state.watchlist = _load_watchlist(web.state.watchlist_path)
    web.state.watchlist_mtime = _file_mtime(web.state.watchlist_path)
    web.state.companies_path = companies_path or settings.resolve_path(company_list.LIST_FILE)
    web.state.backup_dir = backup_dir or watchlist_edit.BACKUP_DIR
    web.state.allowed_hosts = allowed_hosts
    # Tests stand in for Google News and Yahoo here; None means the real ones.
    web.state.preview_transport = preview_transport
    web.state.ticker_fetch = ticker_fetch
    # A new token each time the server starts: a page from an older run has to be reloaded.
    web.state.write_token = secrets.token_urlsafe(32)
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
            day_start=_day_start(settings, now),
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
            # What "Earlier stories" skips past: the page that was actually rendered, which
            # on the first page is however many stories today produced.
            "page_size": len(stories) or queries.PAGE_SIZE,
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
        }
        context |= _rail_watchlist(request, session, settings, now)
        # HTMX asks for the list alone; a plain visit gets the whole page.
        name = "_feed.html" if request.headers.get("hx-request") else "index.html"
        return templates.TemplateResponse(request, name, context)

    @web.get("/watchlist", response_class=HTMLResponse)
    def watchlist(
        request: Request, session: ReadSession, added: str = "", removed: str = "", error: str = ""
    ) -> HTMLResponse:
        """The design's watchlist screen: a card per entry, then the week's news touching
        the list, and what the watchlist sent to Telegram. Its "Add a stock" button opens
        the editor (user, 2026-10-16); each stock card can be removed."""
        now = utcnow()
        assets: dict[str, AssetConfig] = request.app.state.assets
        watched = current_watchlist(request)
        news = watch_queries.watch_news(session, watched, assets, settings, now)
        context = base_context(request, session, active="watchlist", now=now)
        context |= {
            "cards": watch_queries.watch_cards(session, watched, assets, settings, now),
            "news": news,
            "alerts": watch_queries.recent_alerts(session, now),
            "stocks": len(watched.stocks),
            "commodities": len(watched.watchlist) - len(watched.stocks),
            "news_days": watch_queries.NEWS_DAYS,
            "card_points": lambda item: queries.sparkline_points(
                item, CARD_SPARK_WIDTH, CARD_SPARK_HEIGHT, CARD_SPARK_PAD
            ),
            "points": lambda item: queries.sparkline_points(
                item, SPARK_WIDTH, SPARK_HEIGHT, SPARK_PAD
            ),
            "prices_as_of": _stamp(last_pipeline_finish(session), settings),
        }
        context |= _editor_context(request, now) | {
            "added": added,
            "removed": removed,
            "error": error,
        }
        return templates.TemplateResponse(request, "watchlist.html", context)

    def _editor_context(request: Request, now: datetime) -> dict[str, object]:
        """What the editor needs: the token, the version of the file the page shows, the
        peer groups, and whether NSE's company list has been downloaded yet."""
        state = request.app.state
        try:
            version: str | None = watchlist_edit.read(state.watchlist_path).version
            unreadable = None
        except Exception as exc:  # a broken file is shown, never edited
            version, unreadable = None, f"{type(exc).__name__}: {exc}"
        age = company_list.list_age(state.companies_path, now)
        return {
            "token": state.write_token,
            "version": version,
            "unreadable": unreadable,
            "groups": current_watchlist(request).groups,
            "companies_age_days": None if age is None else age.days,
        }

    @web.get("/watchlist/companies", response_class=HTMLResponse)
    def watch_companies(request: Request, q: str = "") -> HTMLResponse:
        """Type-ahead for "Add a stock": NSE-listed companies whose name or symbol matches,
        from the list the scanner keeps (no request is made here)."""
        found = company_list.load_companies(request.app.state.companies_path)
        on_list = {item.symbol for item in current_watchlist(request).watchlist}
        context = {
            "request": request,
            "query": q,
            "ready": bool(found),
            "matches": company_list.search(found, q) if found else [],
            "on_list": on_list,
        }
        return templates.TemplateResponse(request, "_watch_companies.html", context)

    @web.get("/watchlist/preview", response_class=HTMLResponse)
    def watch_preview(
        request: Request,
        session: ReadSession,
        symbol: str,
        name: str = "",
        strong: str | None = None,
        weak: str | None = None,
        group: str = "",
    ) -> HTMLResponse:
        """A draft entry for one company, its Yahoo check, and what its aliases would keep
        and drop among real headlines - before it is added."""
        now = utcnow()
        found = company_list.find(
            company_list.load_companies(request.app.state.companies_path), symbol
        )
        if found is None:
            raise HTTPException(status_code=404, detail=f"{symbol} is not in NSE's list")
        watched = current_watchlist(request)
        draft = company_list.draft(found, group if group in watched.groups else None)
        if strong is not None or weak is not None or name:
            aliases = WatchAliases(
                strong=company_list.parse_aliases(strong or ""),
                weak=company_list.parse_aliases(weak or ""),
            )
            draft = draft.model_copy(
                update={"name": name.strip() or draft.name, "aliases": aliases}
            )
        problem = None if draft.aliases.strong else "a stock needs at least one strong name"
        result = preview_headlines(
            draft,
            session,
            settings,
            now,
            live=problem is None,
            transport=request.app.state.preview_transport,
            fetch=request.app.state.ticker_fetch,
        )
        context = base_context(request, session, active="watchlist", now=now) | {
            "company": found,
            "draft": draft,
            "problem": problem,
            "result": result,
            "on_list": any(item.symbol == draft.symbol for item in watched.watchlist),
        }
        context |= _editor_context(request, now)
        return templates.TemplateResponse(request, "_watch_preview.html", context)

    def _back(**message: str) -> RedirectResponse:
        key, value = next(iter(message.items()))
        return RedirectResponse(f"/watchlist?{key}={quote(value)}#watch-add", status_code=303)

    @web.post("/watchlist/add")
    async def watch_add(request: Request) -> Response:
        """Add a stock to config/watchlist.yaml, as previewed. Only from this app's page."""
        form = await read_form(request)
        if why := write_refused(request, form):
            raise HTTPException(status_code=403, detail=why)
        state = request.app.state
        found = company_list.find(
            company_list.load_companies(state.companies_path), form.get("symbol", "")
        )
        if found is None:
            return _back(error="that company is not in NSE's list")
        watched = current_watchlist(request)
        group = form.get("group", "")
        item = company_list.draft(found, group if group in watched.groups else None)
        item = item.model_copy(
            update={
                "name": form.get("name", "").strip() or item.name,
                "aliases": WatchAliases(
                    strong=company_list.parse_aliases(form.get("strong", "")),
                    weak=company_list.parse_aliases(form.get("weak", "")),
                ),
            }
        )
        try:
            item = type(item).model_validate(item.model_dump())
            watchlist_edit.add_stock(
                state.watchlist_path,
                item,
                form.get("version", ""),
                utcnow(),
                assets=list(state.assets.values()),
                backup_dir=state.backup_dir,
            )
        except watchlist_edit.EditRefused as exc:
            return _back(error=str(exc))
        except ValueError as exc:
            return _back(error=f"not added: {exc}")
        log.info("watchlist: added %s from the page", item.symbol)
        return _back(added=item.name or item.symbol)

    @web.post("/watchlist/remove")
    async def watch_remove(request: Request) -> Response:
        """Take an entry off config/watchlist.yaml. Only from this app's page."""
        form = await read_form(request)
        if why := write_refused(request, form):
            raise HTTPException(status_code=403, detail=why)
        state = request.app.state
        symbol = form.get("symbol", "")
        names = {
            item.symbol: item.name or item.symbol for item in current_watchlist(request).watchlist
        }
        try:
            watchlist_edit.remove_entry(
                state.watchlist_path,
                symbol,
                form.get("version", ""),
                utcnow(),
                assets=list(state.assets.values()),
                backup_dir=state.backup_dir,
            )
        except watchlist_edit.EditRefused as exc:
            return _back(error=str(exc))
        log.info("watchlist: removed %s from the page", symbol)
        return _back(removed=names.get(symbol, symbol))

    @web.get("/watchlist/story/{story_id}", response_class=HTMLResponse)
    def watch_story(request: Request, session: ReadSession, story_id: int) -> HTMLResponse:
        """One watchlist story: each company's call, how the news arrived source by source
        and when the exchange came in, and each company's price since it broke."""
        now = utcnow()
        assets: dict[str, AssetConfig] = request.app.state.assets
        watched = current_watchlist(request)
        view = watch_queries.watch_story(session, story_id, watched, assets, settings, now)
        if view is None:
            raise HTTPException(status_code=404, detail=f"no watchlist story {story_id}")
        names = {item.symbol: item.name or item.symbol for item in watched.watchlist}
        context = base_context(request, session, active="watchlist", now=now)
        context |= {
            "view": view,
            "names": names,
            "marks": watch_queries.MARK,
            "points": lambda item: queries.sparkline_points(
                item, STORY_SPARK_WIDTH, STORY_SPARK_HEIGHT, STORY_SPARK_PAD
            ),
        }
        return templates.TemplateResponse(request, "watch_story.html", context)

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
        watched = current_watchlist(request)
        assets: dict[str, AssetConfig] = request.app.state.assets
        leads, lead_stories, lead_skipped = lead_times(session, settings, utcnow())
        context = base_context(request, session, active="track")
        context |= {
            "summary": summary,
            "tables": tables,
            "watch_tables": watch_queries.watch_track_tables(
                session, int(chosen) if chosen else None
            ),
            "benchmark_names": watch_queries.benchmark_names(watched, assets, settings),
            "leads": leads,
            "lead_stories": lead_stories,
            "lead_skipped": lead_skipped,
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
        now = utcnow()
        view = queries.runs_view(session, settings, now, window)
        # Feed health lives here, not in Telegram (user, 2026-10-16). The Google News
        # searches are one per batch of stocks, so they are summed up in one line.
        statuses = feed_health(session, settings, now)
        searches = [s for s in statuses if "/rss/search" in s.url]
        context = base_context(request, session, active="runs")
        context |= {
            "view": view,
            "feeds": [s for s in statuses if "/rss/search" not in s.url],
            "searches": searches,
            "searches_failing": [s for s in searches if s.state == "failing"],
            "feed_problems": [s for s in statuses if s.problem],
            "settings": settings,
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
            # A refused write (403) or a wrong method says so in plain words, with its code.
            return HTMLResponse(
                f"<p>{html_escape(str(exc.detail))}</p>",
                status_code=exc.status_code,
                headers=getattr(exc, "headers", None),
            )
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
