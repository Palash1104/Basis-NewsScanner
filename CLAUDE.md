# Newsdesk

Personal news digest: fetch world/US/India news, group into stories, summarize, flag market
impact. Full spec in `SPEC.md`; work proceeds one phase at a time (section 13) with approval
between phases.

## Status

**Phase 1 complete (2026-09-17).** Every SPEC §13 Phase 1 criterion was checked live:
- 129 tests pass.
- Run 4: 26/26 feeds, 20 Gemini calls, 8,353 input / 2,361 output tokens.
- Run 5, straight after: 0 calls, 20 unchanged stories.
- Digest: 15 stories in 4 Telegram messages, sent without errors.
- Grouping threshold 64 approved by the user.

**Phase 1 fixes (2026-09-19):** embedding grouping, non-news flagging, region tagging.
- 189 tests pass, including the real-model grouping regressions.
- All 717 stored articles regrouped: 595 stories became 440. 15 summarized stories became
  `needs_resummary`; 5 were unchanged.
- Run 8: 26/26 feeds, 20 calls, 11,452 input / 2,279 output tokens, 11 non-news headlines,
  109 borderline pairs logged.
- Fiji check (smoke test): regions `[]`.
- Digest dry run: 15 stories.

**Drift fix and shared limiter (2026-09-19):**
- Grouping seed check at 0.45; 196 tests pass.
- The seed check only affects new placements. Decision (user, 2026-09-19): don't re-run
  `scripts/regroup.py` for it. The stored blob stories (110 IPO, 316 troop deaths) are left
  as they are and will age out of the digest window.
- The minute window is shared across processes via `llm_requests`.
- Run 9: 16 new articles, 1 call.

**Phase 3 complete (2026-09-20):** price check (SPEC 7.8).
- 336 tests pass.
- First live run: 24 of 90 impacts priced, 66 waiting for Monday's open (the news broke over
  the weekend), 0 errors, 22 symbols, 2,330 bars cached.
- The digest shows moves and labels, e.g. Brent -5.3% (moving against this call) on the
  Houthi story, Gold +1.9% (already moved).

**Phase 2 complete (2026-09-20):** event extraction, asset universe, playbook.
- All three SPEC §13 criteria checked: 82/82 symbols validate; all 15 rules have match and
  near-miss tests; the digest shows impacts with mechanisms in a live dry run.
- 316 tests pass.
- Run 12: 16 events extracted, 90 impacts from 4 rules; 56/500 requests used that day.
- 20 real extractions are kept as fixtures (`tests/fixtures/event_extractions.json`).

Phase 4 has not started; wait for the user.

Since then (2026-09-17):
- Real rate limits for `gemini-3.5-flash-lite` are set (15 RPM, 250k input TPM, 500 RPD), with
  a daily budget of 350 for new work.
- Stories skipped for quota carry over to the next run.
- `newsdesk scheduler` exists. It ran the pipeline hourly in Phase 1; from Phase 2 it runs
  every 3 hours (01, 04, 07 … 22 IST), because summaries plus event extraction would exceed
  the 350/day budget hourly on busy days.
- A test of `gemini-3.1-pro-preview` was cancelled: the key's project is on the free tier, where
  Pro has a limit of 0. Nothing from that test is stored.

Open follow-ups (not Phase 1 criteria):
- Phase 3 handled these Yahoo quirks (kept here because Phase 4 scoring hits them again):
  - Daily bars are stamped at exchange-local midnight. Friday's NSE bar is 00:00 IST on 18 Sep,
    which is 17 Sep 18:30 UTC. Take trading dates in the exchange's time zone, never after
    converting to UTC. The Stop 1 report had this bug and showed every NSE bar a day early.
  - Mon 14 Sep 2026 looks like an NSE holiday: no hourly bars, no `^NSEI` or `^BSESN` bar.
    Yet `.NS` stocks (e.g. RELIANCE.NS, TCS.NS) have a filler daily bar that day with volume
    0 and open = close = the previous close. Indices have no filler bar. The reference-bar
    and trading-day logic must skip zero-volume filler bars, or holidays count as sessions.
  - There was no Yahoo lag on `.NS`: Friday's bar was there when validation first ran.
  - Intraday bars are the reliable "was there a session?" test. Zero volume is not: FX, rates
    and indices report zero volume every day.
- Grouping drift: the seed check (0.45) splits the troop-deaths reports from the war-crimes
  story and keeps unrelated IPO pieces out of the NSE IPO story. It does not separate "Hero
  Motors, NSE…" or "Tata Sons IPO…" from NSE; 0.50+ would, but breaks case (a).
- The seed check's cost: in the 717-article regroup it changes 17 of 440 stories. Most are
  good blob splits, but the Tata Sons chairman row splits into three stories, because its seed
  ("Tata Sons listing…") doesn't resemble the "Noel Tata / Trusts call it illegal" articles.
  Retune both thresholds from `data/logs/grouping_borderline.jsonl` after a few days. The
  centroid threshold bounds: 0.60 fails case (a); 0.45 fails (d).
- Known limitation, left as is: a non-news live blog can attach to the wrong story. For example,
  "US-Iran war LIVE Updates: … Riyadh" attached to the Russia sanctions bill story (2). This is
  harmless for output, since non-news articles never feed summaries, source counts, ranking,
  centroids or digest links, but it appears in that story's article list.
- The shared minute window only sees calls made through this app's database. Calls made with
  the same key elsewhere (AI Studio, another machine) can still cause a 429, which is retried.

## Commands

```
uv sync                                        # install deps (Python 3.12 via uv)
uv run newsdesk run                            # one pipeline pass
uv run newsdesk digest [--dry-run | --send]    # dry run is the default
uv run newsdesk scheduler                      # runs every 3h + digests at delivery.digest_times
uv run pytest -q                               # tests (network and LLM always mocked)
uv run python scripts/smoke_test.py [--send-test-message]   # live: feeds, 1 LLM call, Telegram
uv run ruff check . && uv run ruff format .    # lint + format
uv run newsdesk validate-tickers                # check assets.yaml symbols on Yahoo (= scripts/validate_tickers.py)
uv run python scripts/verify_feeds.py [--include-disabled] [--file other.yaml]
uv run python scripts/embedding_report.py [--refresh] [--candidate 0.55]   # embedding threshold tuning
uv run python scripts/regroup.py [--dry-run]    # regroup all stored articles, no LLM calls
uv run python scripts/grouping_report.py [--refresh --lookback-hours 24] [--detail title_token_set:64]
uv run python scripts/event_fixtures.py [story_ids...]   # live extractions -> test fixtures
```

## Layout

- `config/settings.yaml` tunables · `config/feeds.yaml` verified feeds (see header comment)
- `config/assets.yaml` asset universe (82 SPEC §8 starter symbols, all validated)
- `config/playbook.yaml` 15 cause → effect rules (SPEC §9)
- `app/assets.py` ticker validation (yfinance), stored checks, the unvalidated-symbol warning,
  `benchmark_for` (by exchange)
- `app/config.py` pydantic models for config, `.env` loading
- `app/models.py` SQLAlchemy tables (`UTCDateTime` rejects naive datetimes) · `app/db.py` engine
- `app/net.py` shared HTTP retry/backoff (feeds, Telegram)
- `app/pipeline/fetch.py` concurrent fetch + parse; `SourceResolver` maps Google News outlet names
- `app/pipeline/dedupe.py` URL/title/source normalization, dedupe, independent-source count
- `app/pipeline/cluster.py` `EmbeddingGrouper` (centroids), title `Grouper` (fallback),
  `group_embeddings` (pure), `assign_to_stories` (returns per-article `Placement`s)
- `app/pipeline/embed.py` `Embedder` protocol, `SentenceTransformerEmbedder`, `load_embedder`
- `app/pipeline/classify.py` non-news headline patterns (explainer, roundup, live blog)
- `app/pipeline/regroup.py` one-off regroup of stored articles (`scripts/regroup.py`)
- `app/pipeline/rank.py` importance score (SPEC 7.4) · `app/pipeline/summarize.py` when to
  (re)summarize, article selection, story updates
- `app/pipeline/extract_event.py` event extraction (SPEC 7.6), normalization, pending handling
- `app/pipeline/countries.py` canonical country names and aliases for event matching
- `app/pipeline/playbook.py` rule models, loading and validation, matching, storing impacts
- `app/pipeline/prices.py` `PriceProvider` + yfinance, the price cache, reference selection,
  volatility baseline, move labels
- `app/llm/client.py` `LLMClient` (the only thing the pipeline calls: rate limiting, transient
  retries, validation retry, token counts), the `LLMProvider` protocol, `GeminiProvider` (REST
  via httpx), `AnthropicProvider` (SDK), `make_llm_client` factory
- `app/llm/ratelimit.py` per-model RPM / input TPM / RPD limiter; daily counts in
  `llm_daily_usage` · `prompts.py` prompt text + `*_PROMPT_VERSION` · `schemas.py` output models
- `app/delivery/format.py` Telegram HTML digest + splitting · `telegram.py` Bot API calls
- `app/cli.py` typer commands plus `run_pipeline` / `run_digest` / `run_once` /
  `build_scheduler` (tested directly)
- `scripts/` one-off tools · `tests/fixtures/` synthetic feeds · `data/` DB, logs, reports (gitignored)
- `design/` Claude Design export for the Phase 6 web UI (reference only) · `design/NOTES.md` maps
  its screens to SPEC pages and data, and lists gaps and design tokens

## Conventions

- Type hints everywhere; Python 3.12 syntax (PEP 695 generics ok). Line length 100.
- All datetimes stored UTC and timezone-aware; display in `settings.timezone` (Asia/Kolkata).
- Every external call has a timeout, retries with backoff, and logging. One broken feed, story,
  or LLM call must never crash a run: record it in `runs.errors` and continue.
- Model IDs and provider only from `settings.yaml` (`llm.provider`: gemini | anthropic; model IDs
  must match the provider). Temperature is per model ID (`llm.temperature`); leave out models
  that reject it (claude-sonnet-5) or where the provider advises against it (Gemini 3: keep 1.0).
- Pipeline code never imports a provider or SDK: it calls `LLMClient.structured()` only.
- Never call Gemini without rate limits: `make_llm_client` refuses if `llm.rate_limits` has no
  entry for the summary model. Values come from https://aistudio.google.com/rate-limit (Google's
  docs don't publish free-tier numbers); never guess them.
- Never invent feed URLs, tickers, or API details; verify or flag. Ask before adding dependencies
  not listed in SPEC.md section 3 (approved extras: `tzdata`, `python-dotenv`).
- Tests use `httpx.MockTransport` and `asyncio.run` (no pytest-asyncio/respx). Pipeline and
  summarize tests use `tests/fakes.py` `FakeProvider`; provider tests use MockTransport (Gemini)
  and `FakeAnthropic`. Inject `sleep`/`clock` so retries and rate limits never really wait.
- Bump `SUMMARY_PROMPT_VERSION` in `app/llm/prompts.py` whenever the summary prompt changes.
- Never log secrets: the Telegram token is in its URLs, so Telegram calls pass `log_label`, and
  the `httpx`/`httpcore` loggers are held at WARNING (they log full request URLs at INFO). The
  Gemini key goes in the `x-goog-api-key` header, never the URL.
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
- Story attach window is measured from the story's latest article `published_at`.
  `stories.updated_at` is set when a story is created or its summary is written, not when an
  article attaches, so digests re-send a story only when its summary changed.
- `stories.model` is an extra column (not in SPEC §6) because §14 requires every stored LLM
  output to record its model.
- Gemini: `models.generateContent` with `responseMimeType: application/json` and
  `responseJsonSchema` (the pydantic schema filtered to the keywords Gemini supports). Output
  tokens = `candidatesTokenCount + thoughtsTokenCount`; thinking tokens count against
  `maxOutputTokens`, so the summary cap is 2048. Thinking level is left at the model default
  (`gemini-3.5-flash-lite`: minimal). The Interactions API is newer, but generateContent is
  current and simpler for single requests.
- Anthropic: `messages.create` with `output_config` JSON schema (`anthropic.transform_schema`)
  and our own validation, not `messages.parse` (it raises before usage and raw text can be
  read). SDK retries are off (`max_retries=0`) so every attempt passes the rate limiter; SDK 1.x
  removed `temperature`, so it goes via `extra_body`.
- The validation retry is a single user turn (original prompt + rejected answer + errors), so it
  works the same for every provider. Every attempt, including retries, goes through the limiter.
- Rate limits:
  - RPM and input TPM use a sliding 60s window and wait for room.
  - The window is shared by every process on the database via the `llm_requests` table (an
    extra table: one row per request, pruned after an hour).
    - A caller reserves a row first, then counts the rows ahead of it in the window. The lower
      id wins, so two processes can't both take the last slot. The loser deletes its row and
      waits.
    - The limiter clock is wall time (`time.time`), not monotonic, so it can be compared across
      processes.
  - Daily requests are counted per *Pacific* quota day in `llm_daily_usage` (an extra table,
    not in SPEC §6). The quota resets at midnight Pacific, which is 12:30/13:30 IST, never
    midnight IST.
  - The first attempt of new work stops at `requests_per_day_budget` (350). Retries (transient
    or validation) may go up to `requests_per_day` (500).
  - Hitting either limit raises `LLMQuotaError`, as does a 429 that persists through retries.
- Quota exhaustion mid-run:
  - `summarize_stories` stops, and every remaining story that still needs a summary gets
    `stories.summary_pending = True` (an extra column).
  - The run itself finishes normally.
  - `run_pipeline` puts `pending_stories()` (still in the lookback window) ahead of the top-N
    on the next run.
  - Pending is cleared on success, failure, or when a story no longer needs a summary.
- Schema additions to existing databases go through `db.ADDED_COLUMNS` (`ALTER TABLE` in
  `init_db`); `create_all` alone doesn't add columns. Don't add indexes there, since a
  migrated DB wouldn't get them.
- Scheduler:
  - APScheduler 3.x `BlockingScheduler`, in `settings.timezone`.
  - The pipeline cron hours are `pipeline_hours()`, aligned to the first digest's hour.
  - Jobs have `max_instances=1` and `coalesce`, and catch their own exceptions.
  - Settings load once at start, so restart the scheduler after editing `settings.yaml`
    (`feeds.yaml` reloads every run).
- Summary failures: API/network errors leave the story's status unchanged (retried next run);
  a quota error stops the whole summarize step for the run;
  refused, truncated or still-invalid output marks it `failed` and records the processed
  article count, so it is retried only after it gains 2+ articles or a new region.
- Ranking: `mean(source_weight)` is over the story's articles, as written in SPEC 7.4.
- Digest window: stories summarized since the start of the last digest sent without errors
  (or the lookback window if none). `--dry-run` records nothing; a failed send is recorded with
  errors and doesn't move the window.
- Grouping (SPEC 7.3):
  - Local `all-MiniLM-L6-v2` embeddings of title + snippet.
  - Cosine similarity to each story's centroid (the normalized sum of its news articles'
    vectors), threshold 0.55. Chosen with `scripts/embedding_report.py`; the regression cases
    are in `tests/fixtures/grouping_regressions.json`.
  - The title matcher (`token_set_ratio` 64) is only used if the model fails to load.
  - The model loads with `local_files_only=True` first, so there are no Hub calls after the
    first download. Cold import takes about 40s on Windows.
  - Tests use `tests/fakes.py` `FakeEmbedder`. `test_grouping_regressions.py` uses the real
    model and skips if it's unavailable.
- Non-news articles (`articles.non_news`, an extra column):
  - They attach to a story if they match, never start one, and never move the centroid.
  - They are excluded from source counts, ranking, summary input and digest source links.
  - Every one is listed in `newsdesk run` output.
  - Removed as too broad: "live coverage", "timeline", "what you need to know".
- Seed check (`grouping.seed_threshold`, 0.45, null disables):
  - An article joins a story only if it clears the centroid threshold and scores at least
    this against the story's seed, its earliest news article.
  - Otherwise it goes to the next-best story passing both, or starts a new story.
  - `assign_to_stories` loads every news article of the stories active in the window, oldest
    first, so each seed is the real earliest article and centroids cover whole stories.
  - Regression cases e and f (IPO drift, troop deaths) use `tests/fixtures/grouping_regressions.json`.
- Borderline placements (score within `grouping.borderline_log_range`, or `seed_rejected`) are
  appended to `{log_dir}/grouping_borderline.jsonl` with `score` and `seed_score`.
- `needs_resummary` (set only by `regroup.py`) is a story status excluded from digests, since
  the digest selects `summarized`. The story is summarized again once it has more news
  articles than `processed_article_count`.
- Regions (`summary-v3`):
  - The prompt no longer shows the outlet's region.
  - The model tags US/India/Global only by where the event happens or who it involves.
  - An empty list is valid, and the digest meta line then shows only the category.
- `DESIGN.md` is the user's design-import notes (renamed from the misspelled `DEISGN.md`).
- Asset universe (SPEC §8):
  - One `sector` per asset from a fixed, validated list (see `app/config.py` `Sector`).
  - `exchange` and `currency` are copied from Yahoo's metadata in the validation report.
  - The scoring benchmark is chosen by exchange, not country: `NSI` → `^NSEI`, `NYQ`/`NMS` →
    `^GSPC`, stocks only (TSM → `^GSPC`). A stock on another exchange gets no benchmark until
    its code is seen in a report and added to `EXCHANGE_BENCHMARKS`.
- Event extraction (SPEC 7.6, prompt `event-v2`):
  - Runs right after each successful summary, on the same articles, so the re-processing rule
    is the summary's. Separate call from the summary: a bad event never costs a summary, and
    the event prompt can change without rewriting summaries.
  - Writing a summary sets `stories.event_pending`; extraction clears it. A run that stops
    between the two (quota, API error, crash) is picked up by the next run.
  - `policy_actor` (added in v2) is the authority whose stance the event gives, so a Fed
    decision can't match an RBI rule because the articles quote the RBI reacting.
  - Country names are canonicalized; unmapped ones are kept as written and printed in the run
    output. `none` arriving with real channels is dropped and logged.
  - A story keeps its summary but gets no event when output stays invalid after the retry;
    it's retried only at its next re-summary.
- Playbook (SPEC §9, `config/playbook.yaml`):
  - Severity precedence in the prompt is what makes several rules work: easing is
    `de_escalation` whatever its size, worsening is `escalation`. The oil-easing, rupee,
    monsoon and US-tariff rules say so in a comment.
  - `entities_any` and `policy_actor_any` match whole words with listed aliases, so "RBI"
    doesn't match "Herbie" and "Fed" doesn't match "FedEx".
  - SPEC's "US or NATO" rule is two rules sharing impacts through a YAML anchor, because
    conditions can only be ANDed. The NATO member list is dated in a comment.
  - `geopolitical_risk_off` is expected to fire broadly (any escalating conflict with
    risk_sentiment or safe_haven_demand). That's deliberate: Phase 4's track record is what
    judges whether those calls are worth keeping.
  - Impacts are written once and never edited; re-extraction only adds. One row per rule, so
    the track record stays per rule; the digest shows an asset once and says "2 rules".
  - An asset called both ways on one story is marked `conflict` and shown as "mixed signals".
  - Rules with an `UNSURE` comment (bank stocks on RBI moves, TSMC on chip risk, Tata Steel on
    Chinese stimulus) are held at low confidence on purpose.
- Price check (SPEC 7.8):
  - 60-minute bars for the reference and latest price, daily bars for the volatility baseline.
    Bars carry the exchange's time zone and only exist for real sessions, so reference
    selection needs no market-hours table: a 23:00 IST story references the next NSE morning,
    but the next hourly bar for crude.
  - `reference_time` is the first bar at or after the story; `reference_price` is the close of
    the bar before it.
  - "Waiting for their market to open" (no bar after the story yet) is counted separately from
    failures and is not an error: the first run after the open prices it.
  - `impacts.moved_vol_multiple` (1.0) must stay separate from
    `scoring.hit_threshold_vol_multiple` (0.5): different questions, deliberately different.
  - Rates are judged and shown in points ("+0.10 pts"), with the unit always printed; their
    typical move is the standard deviation of daily point changes.
  - Grains quote in US cents (`USX`): percentages and volatility are unit-free, so nothing
    converts. `reference_price` is stored in the asset's native unit.
  - `price_stale_days` (5) must exceed a normal closure (Friday to Monday is ~2.7 days, ~3.7
    with a holiday Monday).
  - `run_pipeline` only prices when a provider is passed, so tests never reach the network.
- Ticker validation (`newsdesk validate-tickers`):
  - One yfinance `history(period="5d")` call per symbol. Yahoo's name, currency, exchange and
    instrument type come from the same request's metadata.
  - Failures (`empty`, `stale`, `error`) are never replaced automatically.
  - Name, type, currency and exchange mismatches are review flags, not failures.
    `approved_yahoo_names` in assets.yaml silences a confirmed name (e.g. `^TNX`).
  - Every check is stored in `ticker_checks`. `run` and `scheduler` warn about symbols never
    validated, failed, or last checked over 30 days ago.
  - `yf.config.debug.hide_exceptions = False` replaces the deprecated `raise_errors`.
