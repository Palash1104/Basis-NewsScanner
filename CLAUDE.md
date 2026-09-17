# Newsdesk

Personal news digest: fetch world/US/India news, group into stories, summarize, flag market
impact. Full spec in `SPEC.md`; work proceeds one phase at a time (section 13) with approval
between phases.

## Status

Phase 1 in progress. Done: config, DB models, fetch, dedupe, grouping, feed verification,
grouping report. Next: ranking, summarization, `run`/`digest` CLI, Telegram, smoke test, README.

## Commands

```
uv sync                                        # install deps (Python 3.12 via uv)
uv run pytest -q                               # tests (network and LLM always mocked)
uv run ruff check . && uv run ruff format .    # lint + format
uv run python scripts/verify_feeds.py [--include-disabled] [--file other.yaml]
uv run python scripts/grouping_report.py [--refresh --lookback-hours 24] [--detail title_token_set:64]
```

## Layout

- `config/settings.yaml` tunables · `config/feeds.yaml` verified feeds (see header comment)
- `app/config.py` pydantic models for config, `.env` loading
- `app/models.py` SQLAlchemy tables (`UTCDateTime` rejects naive datetimes) · `app/db.py` engine
- `app/net.py` shared HTTP retry/backoff (feeds, Telegram)
- `app/pipeline/fetch.py` concurrent fetch + parse; `SourceResolver` maps Google News outlet names
- `app/pipeline/dedupe.py` URL/title/source normalization, dedupe, independent-source count
- `app/pipeline/cluster.py` incremental `Grouper` + `assign_to_stories`
- `scripts/` one-off tools · `tests/fixtures/` synthetic feeds · `data/` DB, logs, reports (gitignored)
- `design/` Claude Design export for the Phase 6 web UI (reference only) · `design/NOTES.md` maps
  its screens to SPEC pages and data, and lists gaps and design tokens

## Conventions

- Type hints everywhere; Python 3.12 syntax (PEP 695 generics ok). Line length 100.
- All datetimes stored UTC and timezone-aware; display in `settings.timezone` (Asia/Kolkata).
- Every external call has a timeout, retries with backoff, and logging. One broken feed, story,
  or LLM call must never crash a run: record it in `runs.errors` and continue.
- Model IDs only from `settings.yaml`. Temperature is per model ID (`llm.temperature`); omit
  models that reject it (claude-sonnet-5 does).
- Never invent feed URLs, tickers, or API details; verify or flag. Ask before adding dependencies
  not listed in SPEC.md section 3 (approved extras: `tzdata`, `python-dotenv`).
- Tests use `httpx.MockTransport` and `asyncio.run` (no pytest-asyncio/respx).
- Scripts call `sys.stdout.reconfigure(encoding="utf-8")`: the Windows console codepage can't
  print ₹ and similar characters.

## Web UI (Phase 6)

- The web UI must follow the visual design in `design/` and the decisions in `design/NOTES.md`,
  implemented in the spec's stack: FastAPI + Jinja2 + HTMX + plain CSS, no React, no JS build
  step (SPEC §11).
- Primary mockup: `design/Basis - Commodity News App.dc.html`. Tokens:
  `design/_ds/modernist-3dfd6d1f-f6ac-418e-8f3e-37cf9f987647/styles.css`.
- `design/support.js` and `design/browser-window.jsx` only preview the mockup; don't use them in
  the app.

## Decisions worth knowing

- Several `feeds.yaml` entries may share `name`: they are one outlet (counted once).
- Google News entries: outlet from the entry's `source`; if it matches a configured outlet name or
  alias (including disabled feeds) it takes that outlet's region/weight, else the edition's region
  and weight 1. Title suffix " - Outlet" is stripped; snippet left empty (the description is a
  list of related headlines).
- `normalize_source` ignores case, a leading "The", punctuation, and domain suffixes
  (`cnbc.com` == `CNBC`).
- Entries dated more than 10 minutes in the future are clamped to fetch time (mislabelled zones).
- Same-source dedupe: `token_set_ratio` >= 90 within 24h (per spec). Syndication (cross-source
  near-identical titles) uses `token_sort_ratio` >= 90, because `token_set_ratio` scores a short
  headline contained in a longer different one as 100.
- Story attach window is measured from the story's latest article `published_at`;
  `stories.updated_at` is wall-clock time of the last change (used for digests).
- Grouping scorer/threshold chosen from `scripts/grouping_report.py` on real data (see comment in
  settings.yaml). Title-only fuzzy matching both over- and under-merges near the threshold;
  Phase 5 replaces it with embeddings.
