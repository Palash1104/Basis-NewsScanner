# BASIS

Personal news digest: fetch world/US/India news, group into stories, summarize, flag market
impact. Full spec in `SPEC.md`; work proceeds one phase at a time (section 13) with approval
between phases.

**The product is BASIS; the internal name stays `newsdesk`** (user, 2026-09-21). BASIS appears
wherever a person sees it: the web header and page titles, the Telegram digest header, the
`serve` and `health` output, and the README and SPEC headings. Everything else keeps
`newsdesk`: the CLI command, the Python package, `data/newsdesk.db`, the log files, the
scheduled task names and folder, and the `newsdesk-theme` storage key. Renaming those would
break a running install for no gain.

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

**Phase 6 complete (2026-09-22):** the web UI.
- 512 tests pass.
- All five SPEC 11 pages against real data, plus `/design` as the style guide:
  `/` (feed, filters, search, 1D/1W/1M), `/story/{id}`, `/track-record`, `/assets` +
  `/asset/{symbol}`, `/runs`.
- `newsdesk serve` on 127.0.0.1:8787, read-only, light and dark.
- Checked against an empty database: every page returns 200 (or 404 for a missing story or
  symbol) and says what it has nothing of.
- India went from 0 stories on the feed to 9 after the reserved slots landed.

**Phase 5 complete (2026-09-20):** LLM impact layer and rerank.
- 375 tests pass.
- Quality gate before wiring in (`scripts/impact_gate.py`, 20 fixtures on Flash-Lite): 13 of
  20 returned `no_clear_impact`, 0 invented symbols, 1 rule disagreement. A 5-fixture spot
  check on 3.6 Flash was worse (missed crude on an oil story, invented an Iran war on a Fed
  story), so layer B stays on Flash-Lite.
- Live run 20: 5 layer-B calls, 9 impacts with `origin=both`, 2 rule disagreements. One
  caught `us_nato_defense_spending` firing on a monument-zoning story; the other demoted
  `geopolitical_risk_off` on routine North Korean missile launches.
- Event prompt v3 fixed the over-inclusion: the Fed story's countries went from
  `[United States, Iran, India]` to `[United States]`.
- New playbook rule 16, `visa_policy_india_it`, from a gap the gate found.

**Phase 4 complete (2026-09-20):** scoring and track record (SPEC 7.9).
- 354 tests pass.
- First live scoring: 24 calls judged at horizon 1 (3 hit, 11 miss, 10 no-move); horizon 5
  not due yet. A second run wrote nothing (idempotent).
- The digest shows track-record lines for `geopolitical_risk_off` (n=7); rules at n=4 and
  n=3 show none.
- Breaking alerts were moved out of Phase 4 (user, 2026-09-20): a later phase or a
  standalone task, once real importance scores have been watched for a few weeks.

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

Every phase in SPEC 13 is complete. Next work is the user's call: breaking alerts were
deferred out of Phase 4, and the grouping thresholds are due a retune from
`data/logs/grouping_borderline.jsonl`.

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
- The rank stage commits before the rerank's LLM call: ranking writes every story's score,
  and holding that transaction open locked the rate limiter out of its own session
  ("database is locked", run 24 on 2026-09-21).
- SQLite writes: never hold a write transaction across an LLM call. The rate limiter writes
  from its own session, so an open transaction locks it out ("database is locked", twice on
  2026-09-20). Every per-story loop commits before the next call, and connections set
  `PRAGMA busy_timeout=10000`.

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
uv run newsdesk score [--rescore]              # judge due calls, print the track record
uv run python scripts/impact_gate.py [--model summary|reasoning] [--story ID...]
uv run newsdesk health [--days 7]              # scheduler slots, LLM budgets, layer B, rules at n>=5
powershell -ExecutionPolicy Bypass -File scripts/install_tasks.ps1 [-Remove]   # Windows tasks
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
- `app/pipeline/scoring.py` sessions and trading-day counting, scoring, the track record
- `app/pipeline/impact_llm.py` layer B: the call, universe validation, caps
- `app/pipeline/merge_impacts.py` merging layer A and layer B (SPEC 7.7)
- `app/llm/client.py` `LLMClient` (the only thing the pipeline calls: rate limiting, transient
  retries, validation retry, token counts), the `LLMProvider` protocol, `GeminiProvider` (REST
  via httpx), `AnthropicProvider` (SDK), `make_llm_client` factory
- `app/llm/ratelimit.py` per-model RPM / input TPM / RPD limiter; daily counts in
  `llm_daily_usage` · `prompts.py` prompt text + `*_PROMPT_VERSION` · `schemas.py` output models
- `app/delivery/format.py` Telegram HTML digest + splitting · `telegram.py` Bot API calls
- `app/cli.py` typer commands plus `run_pipeline` / `run_digest` / `run_once` /
  `build_scheduler` (tested directly)
- `app/schedule.py` `pipeline_hours` (shared by the scheduler and health, no CLI import)
- `app/health.py` `newsdesk health`: slot coverage, quota-day usage, rerank fallbacks, layer
  B's decline rate, rules at n>=5. Database only, so it can run while the scheduler runs.
- `app/locks.py` `job_lock`: one pipeline / digest / score job at a time (OS file lock)
- `app/presentation.py` what a story says about the market, decided once for the digest and
  the web: asset calls, "mixed signals", "N rules", "+N more", story age
- `app/pipeline/sections.py` URL sections, and which stories may take a reserved slot
- `app/web/main.py` routes and the app factory · `queries.py` every read the pages make ·
  `palette.py` the colours and their measured contrast · `templates/` · `static/`
  (`design-system.css` is the export, vendored unmodified; `theme.css` is dark mode and the
  mockup's inline values; `app.css` is screen layout)
- `scripts/install_tasks.ps1` registers the Windows tasks · `scripts/run_task.ps1` is what
  they execute (venv, working directory, `data/logs/tasks/<job>.log`)
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
  that reject it (claude-sonnet-5).
- `gemini-3.5-flash-lite` runs at temperature 0 with a fixed `llm.seed` (user, 2026-09-21),
  although Google advises leaving Gemini 3 at 1.0 because lower values can cause looping.
  Reason: summaries and event extraction are classification tasks, and their variance changed
  which rules fired, which pollutes the track record.
  - **Temperature 0 alone is not reproducible on Gemini.** Extracting the same 20 fixtures
    twice at temperature 0 gave 7/20 identical events and 0/3 identical generated summaries;
    severity flipped between `moderate` and `escalation` on three stories, which changes
    which rules fire. Adding the seed gave 20/20 events and 20/20 summaries identical.
  - Google doesn't promise determinism even with a seed, so treat it as best effort and
    re-check after any model change.
  - No looping or truncation appeared in 40 extractions at temperature 0.
  - Temperature and seed are per provider and per model, so the LLM impact layer (same
    flash-lite model) is at 0 with the same seed. All three tasks are kept on the same
    settings on purpose (user, 2026-09-21): there is no per-call override.
  - Anthropic has no seed parameter; the setting is ignored there, and no seed is stored.
  - Every row holding model output records `model`, `prompt_version`, `temperature` and
    `seed`: `stories` (summary), `events` (extraction), `impacts` and `rule_disagreements`
    (layer B). They come from `settings.llm.provenance(model, prompt_version)`, the only
    place that decides them, so a row can't claim settings other than the ones sent.
    - On `impacts` the four columns are layer B's and are null on `origin=playbook`, which
      no model touched. The extraction's own provenance is on the event.
    - `temperature` and `seed` are track-record groups, taken from the impact's event,
      because that extraction decided which rules fired. Rows written before 2026-09-21
      group as `(none)`.
    - `newsdesk score` prints the temperature and seed tables only when more than one value
      has been judged, so they stay out of the way while the settings are fixed.
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

- Follow the visual design in `design/` and the decisions in `design/NOTES.md`, in the spec's
  stack: FastAPI + Jinja2 + HTMX + plain CSS, no React, no JS build step (SPEC §11).
- Primary mockup: `design/Basis - Commodity News App.dc.html`. Tokens:
  `design/_ds/modernist-3dfd6d1f-f6ac-418e-8f3e-37cf9f987647/styles.css`.
- `design/support.js` and `design/browser-window.jsx` only preview the mockup; don't use them in
  the app.
- `app/web/static/css/design-system.css` **is** that stylesheet, vendored byte for byte; a test
  compares the two. Never edit it: it is token-driven, so `theme.css` adds dark mode by
  overriding its custom properties.
- Colours are the export's, including the primary button (3.76:1 light, 4.18:1 dark - below
  AA, kept on the user's instruction and reported on `/design`). Muted text is ink at 70%, as
  the mockup writes it, not the system's 55%.
- The web app never writes: `make_read_only_engine` sets `query_only` and deliberately not
  `journal_mode` (that takes a write lock). It never calls `init_db`, so restart the server
  after a schema change. No request touches the network.
- Search is SQLite FTS5 (`db.SEARCH_TABLE`), created and kept current by `init_db` and its
  triggers, never by the web app. Typed words are quoted before they reach FTS5 so its own
  operators can't break a query. A database it can't index logs a warning and carries on.
- Sparklines come from `price_cache` only, sampled to ~24 points: 60-minute bars for 1D and
  1W, daily for 1M. An asset with no bars gets no line, never a made-up one.
- **A line beside a call is always one week** (`queries.RAIL_WINDOW`, and `STORY_WINDOW` which
  points at it): the feed's chips, the story page's rows and both rail lists, so a shape on
  one page means the same as a shape on another (user, 2026-09-22). The story page was on a
  month before that. The 1D/1W/1M control still moves the feed's chips, and it starts on 1W;
  `/asset/{symbol}` keeps its month, since that page is about the asset and not a call. The
  moves printed beside these lines are their own thing - "since news" on a call, 24h in the
  rail - and each is labelled where it appears.
- `/design` is the style guide and stays reachable, but it is not in the nav, because it is
  not in the design.
- The feed page ignores the mockup's 1280px canvas and fills the window (user, 2026-09-22):
  the masthead and the ticker are full-bleed, so capping the page under them left the rail
  stranded mid-screen with dead space to its right. The extra width goes to the stories - and
  their text can't sprawl, since headlines, summaries and notes are all capped in `ch`, so
  what grows is the room the chip strip scrolls in. `/track-record`, `/assets`, `/runs` and a
  story page keep the canvas: text and tables, where a 2000px line is not a better line.
- The feed is ordered **newest first** (`first_seen_at desc`), not by importance (user,
  2026-09-22): it is read several times a day, and a big story held the top of it for two
  days. The digest keeps the importance order - it is sent twice a day, and ranking is its
  job. Every filter and the search share the one order.
- The logo is the mockup's flat Archivo 800 wordmark **plus** a mark beside it (user,
  2026-09-22; the extruded wordmark that came first was reverted): a newspaper - a masthead
  bar over three columns of type - extruded four hard `box-shadow` steps, 1px apart, down the
  accent ramp (`--logo-1..4` in `theme.css`, moved up the ramp for dark mode). All CSS: the
  sheet is a border and a background, the type is three gradients, and a shadow costs no
  layout, so the masthead's height is unchanged. No image, no icon font, no request. The
  favicon is the same mark as an inline `data:image/svg+xml`, which needs no font at all.
- Headlines are ink, bold and unlined, as in the design, where the whole row is the link and
  the words are never tinted; the underline is the hover state, which the design doesn't
  define. Every other link takes the mockup's own colour (`--color-link`, accent-700 on
  paper, accent-400 on ink), not the system's brighter `--color-accent`.
- The feed's "assets affected" chips are **one horizontally scrolling row** (user,
  2026-09-22), not a wrapping grid: once the rail took its 320px, two 420px chips no longer
  fit side by side, so a story with six calls became a column a screenful tall. The chips are
  a fixed `--chip-width` (280px, about three in view), never shrink (`flex: none`), and the
  strip hides its scrollbar like the ticker; it carries `tabindex="0"` so it can be scrolled
  from the keyboard, and a button at each end (`static/js/chips.js`) says there is more. The
  buttons ship `hidden` in the template and the script reveals them only for a row that
  overflows, so a page without JavaScript shows no dead control; clicks are caught on the
  document, because HTMX replaces the whole feed on every filter and search keystroke and
  listeners bound to a row would go with it. The story page is unaffected: it lists every
  call in full-width rows.
- The rail's watchlist (the design's watchlist card, `web.watchlist` in settings.yaml):
  - Four assets to start (Brent, gold, copper, USD/INR), above the movers. Each row is the
    mockup's, on two lines: name and 24-hour move, then the last cached price and the week's
    sparkline. The price is 12px in ink at 85% (`--color-body-dim`, 9.52:1), not the mockup's
    11px at 70% (5.79:1), which was the lightest text on the page and hard to read (user,
    2026-09-22); its digits are tabular so the column lines up. Prices are written as the design writes them - `$71.40`,
    `₹1,402`, `412¢` - with the code kept for a currency we have no mark for, never a guessed
    symbol.
  - **"Edit watchlist" saves in the browser, not the database** (`static/js/watchlist.js`,
    `newsdesk-watchlist` in localStorage): the web app opens the database read-only, and a
    watchlist is one person's on one machine, not a fact about the news. settings.yaml holds
    the default, which is what the page renders server-side - so it reads correctly with no
    JavaScript at all and for a browser that has never edited it.
  - The editor is a native `<dialog>` over the whole universe, capped at
    `web.watchlist_max` (12): at the cap the unchecked boxes go disabled, so the limit is
    visible before it bites.
  - `GET /watchlist?symbols=...` renders the rows alone, for the browser's own list. It
    re-validates every symbol against assets.yaml and drops what it doesn't know, so nothing
    a browser has stored - stale, hand-edited, from an older universe - can put a made-up
    asset on the page. An empty or wholly unknown list falls back to the default.
- Biggest movers - 24h (`queries.movers`, the design's right rail):
  - The window is the last 24 hours from the moment the page is opened. An asset needs a bar
    inside it *and* one at or before it starts; a market shut all day is left out rather than
    shown against a stale price. For a US stock opened on a weekday morning IST, that is the
    previous session's close-to-close move.
  - Rates are in points, like everywhere else, and down is red. Each row draws a sparkline
    too, fetched after the ranking so a page reads bars for the six shown, not all 82.
  - It is stamped "as of HH:MM IST" with the last pipeline finish, for the same reason the
    ticker is: nothing here is live.
  - The rail is 350px (user, 2026-09-22, settled after 320, 260, 210 and 250): it now holds
    what the mockup's rail holds - a name, a price, a sparkline and a move. Every name in the
    universe fits; a longer one would be cut with an ellipsis and keep its full text in a
    `title`, so neither list can go ragged.
  - **Both lists draw the same window, a week** (`queries.RAIL_WINDOW`), so their sparklines
    can be read against each other, while the numbers beside them stay 24-hour. The rail
    labels both ("24h move - one-week line") and carries one "Prices as of HH:MM IST" stamp
    for the pair, since they read the same cache filled by the same run.
- The pipeline caches 60-minute bars for the **whole** universe each run
  (`prices.refresh_universe`), because the price check only fetches what a story called and
  the rail ranks all 82. One batched `yf.download`: measured live on 2026-09-22 at **2.6s**
  for 82 symbols and 3,723 bars written (1.6s of it the fetch), against a run of several
  minutes. No LLM call, and no new dependency.
  - It runs **after** `price_impacts`, never before: `refresh_symbol` skips a symbol whose
    newest cached bar is under an hour old, so filling the cache first would stop it
    backfilling the older history a new impact on an older story needs.
  - A Yahoo failure is recorded in `runs.errors` and the run carries on.

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
- Scheduling (2026-09-21): the Windows Task Scheduler runs the jobs, not `newsdesk
  scheduler`, which only lives as long as its terminal.
  - Three tasks in the `\Newsdesk\` task folder: pipeline (`newsdesk run`), digest
    (`--send`), score. `scripts/install_tasks.ps1` registers them and is idempotent;
    `-Remove` deletes them.
  - Times come from `newsdesk schedule-times` (JSON from settings.yaml), so the tasks
    can't drift from the app's schedule. Re-run the installer after changing it.
  - `StartWhenAvailable` (run after a missed start) and `WakeToRun` are on; battery
    limits are off; `MultipleInstances IgnoreNew`. They run as the logged-on user
    (`.env` is theirs), so no admin rights and no stored password.
  - Verified 2026-09-21: `Start-ScheduledTask Newsdesk-score` gave `LastTaskResult 0`
    and scored 66 calls through the wrapper.
  - `newsdesk scheduler` still exists for other platforms and for a foreground run.
  - `-WithWeb` adds a fourth task, `Newsdesk-web`: `newsdesk serve` at logon, one minute in
    (user, 2026-09-22 - they wanted BASIS up as soon as the laptop is on). It runs until
    logoff, so it has no `ExecutionTimeLimit`, restarts up to 3 times, and is
    `MultipleInstances IgnoreNew`. No `WakeToRun` and no `StartWhenAvailable`: nobody is
    reading a page while the lid is shut. `run_task.ps1` streams its output straight to
    `data/logs/tasks/serve.log` (truncated per logon) instead of the batch jobs' collect-at-
    exit, which would show nothing until the server stopped.
  - A long-running task reports `LastTaskResult 267009` ("currently running"), which is
    success, not an error.
- Job locks (`app/locks.py`): `run`, `digest --send` and `score` each hold an OS file
  lock in `data/locks/`, in both the CLI and the scheduler jobs. A job that can't take
  its lock logs and exits 0, so a slow run and the next scheduled one never overlap.
  One lock per job kind, so the 07:30 digest isn't blocked by a slow 07:00 run. A
  dry-run digest takes no lock: it writes nothing. Windows locks are mandatory, so the
  lock file can't even be read while it is held.
- Scheduler:
  - APScheduler 3.x `BlockingScheduler`, in `settings.timezone`.
  - Nothing is caught up after downtime: the job store is in memory, and a cron job missed
    while the process was stopped never runs later. Misfire grace only covers a process that
    was running but busy or suspended: 15 min for the pipeline, 30 for a digest, 60 for
    scoring. `coalesce` means several missed fires become one.
  - That is safe because the pipeline recovers by itself: the next run re-fetches the whole
    `pipeline.lookback_hours` window, `summary_pending` / `event_pending` stories go first,
    and the digest window starts at the last digest sent without errors. Downtime longer than
    the lookback window is what actually loses stories.
  - `newsdesk health` is how missed slots are seen after the fact.
  - The pipeline cron hours are `pipeline_hours()`, aligned to the first digest's hour.
  - Jobs have `max_instances=1` and `coalesce`, and catch their own exceptions.
  - Settings load once at start, so restart the scheduler after editing `settings.yaml`
    (`feeds.yaml` reloads every run).
- Summary failures: API/network errors leave the story's status unchanged (retried next run);
  a quota error stops the whole summarize step for the run;
  refused, truncated or still-invalid output marks it `failed` and records the processed
  article count, so it is retried only after it gains 2+ articles or a new region.
- Ranking: `mean(source_weight)` is over the story's articles, as written in SPEC 7.4.
- Reserved slots (`pipeline.reserved_slots`, `{IN: 5}` since 2026-09-21):
  - Why: in 48h, **0 of 524 India-only stories were summarized**. They top out at 3.60
    importance against a top-20 cutoff of 4.69, because both the source count and the region
    term are capped when only Indian outlets carry a story. Removing the region term entirely
    moved one story, so reweighting was not the answer.
  - The slots sit *inside* `max_stories_per_run` (15 general + 5 India), so they cost no LLM
    calls. Unused slots go back to the general list.
  - The region is the **source** region (`app/pipeline/sections.py` `only_from`), not the
    summary's `regions` tag, which doesn't exist until after summarizing.
  - `reserved_candidate_pool` (10) joins the rerank's candidates, so the model can order them
    before the slots are filled; the rerank runs over the whole candidate list.
  - `may_take_reserved_slot` keeps a slot away from sport, entertainment, lifestyle and viral
    filler by reading URL **path sections**, never substrings ("sport" is inside passport,
    "ipl" inside diplomats, "celeb" inside celebrate). Unknown sections (Google News
    redirects, ~10% of Indian articles) are not eligible: candidates are many, slots are few.
  - It also skips stories that don't need a summary, so a slot is never spent on a no-op.
  - `reserved_max_age_hours` (48, from `first_seen_at`) stops the reserve working through the
    backlog instead of covering today. Measured on run 25: a 24h cap would have dropped 3 of
    the 5 picks (Sensex 34.9h, SBI strike 26.2h, BRICS exports 56.2h) for an FTA ratification,
    a comedian's cancelled shows and a railway reshuffle, so 48h was chosen (user,
    2026-09-22). The pool that run held 326 stories under 24h old and 26 older.
  - First live run (25, 2026-09-21): the rerank 503'd, so the fallback path ran, and the five
    slots went to Sensex/Nifty, BRICS exports, the SBI strike advisory, Kerala floods and
    Trump's India tariffs. All five were tagged `India` by the summary; no sport.
  - **US has no reserve** (user, 2026-09-21): only 73 US-only stories in 48h, max importance
    2.88, all single-outlet features, and 21 of 30 summarized stories already carry a `US`
    tag. **Re-check after a week of scheduled runs** with the same analysis before deciding.
- Story age (`presentation.story_age`): a story summarized more than 12 hours after it was
  first reported carries its age, because the reserved slots and carried-over summaries both
  surface stories a day or more old. Hours up to 48, then days. The digest's meta line reads
  "first reported 3d ago"; the feed puts "3d ago" on the timestamp line, which is the time it
  describes; the story page spells out the phrase.
- Daily price listings (`classify.py` `_PRICE_LISTING`): "Petrol, diesel prices today…",
  "Gold rate today", "Check rates in Delhi, Mumbai" are non-news, like roundups. A price
  *event* (a hike, a duty cut, a 50-month high) and a market preview ("Will Nifty extend
  gains…") stay news. Checked against 1,097 real headlines: 2 matched, both templates.
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
- Scoring (SPEC 7.9, `newsdesk score`, daily at `schedule.score_time` 03:30 IST):
  - Trading days are the asset's own sessions, dated in its exchange's time zone
    (`timezone` in assets.yaml). Holiday filler bars are excluded the same way as in Phase 3.
  - Horizon N is the Nth session on or after the reference session: a mid-session story is
    judged from that session's close.
  - The benchmark uses the same reference rule and the same sessions, so both sides cover the
    same window. Only stocks have one.
  - Volatility comes from the sessions *before* the reference, so the judged move can't raise
    its own threshold. Too little history is `unscorable` immediately, since it can't grow.
  - Idempotent: unique (impact_id, horizon_days); rows are written only when every input is
    present, and never rewritten except by `--rescore`. A late bar is picked up next run.
  - Giving up is explicit: no reference after `reference_grace_days`, or a horizon overdue by
    `score_grace_days`, becomes a final `unscorable` row so the job stops retrying.
  - The track record groups by rule_id, event_type, origin, confidence, horizon_days,
    prompt_version, temperature and seed (the last four come through `impacts.event_id`). Rates are hidden below
    `min_samples_to_show_rate` (5) judged calls.
  - n counts asset-calls, not independent events, so the distinct story count is reported
    next to it. The digest shows at most one track-record line per story.
  - A rate is shown only at `min_samples_to_show_rate` (5) judged calls **and**
    `min_stories_to_show_rate` (3) distinct stories (user, 2026-09-22): five calls from one
    story is one observation, not five. Below either gate the page prints
    "n=18, 2 stories, too few". The same gate applies everywhere a rate is printed - the
    digest's track line, `newsdesk score`, `newsdesk health` and the web - because a rate
    that is wrong to show on the page is wrong to send to Telegram.
  - Between that gate and `early_rate_below_stories` (10) the rate is shown muted with an
    "early" tag: a direction, not a measurement.
- LLM impact layer (SPEC 7.7 B) and rerank:
  - Layer B runs on `summary_model`, not `reasoning_model`: this project's `gemini-3.6-flash`
    allows only 20 requests a day, and `gemini-3.8-flash` is listed by the API but has no
    quota here (like `gemini-3.1-pro-preview` in Phase 1). Always check the AI Studio
    rate-limit page, not the model list.
  - It runs on the top `impacts.llm_max_stories_per_run` (5) stories per run, in post-rerank
    order, and only where the playbook step would map impacts at all.
  - The rerank runs on Flash-Lite too, since 2026-09-22 (user): `gemini-3.6-flash` failed
    7 of 21 reranks with 503s, including the last two runs, and the Phase 5 gate found its
    reasoning no better than Flash-Lite's. `reasoning_model` now points at Flash-Lite;
    3.6 Flash keeps its `rate_limits` entry so it can be used again without guessing.
    - Budget after the move, 8 runs a day: 206 summaries+extraction + 40 layer B + 8 rerank
      = 254 typical, 320 on a busy day, against the 350 budget (headroom 30).
    - Run 27 (2026-09-22) was the first after the move: the rerank succeeded, no fallback,
      26 calls for the run (1 rerank + 10 summaries + 10 extractions + 5 layer B).
  - Any rerank failure still falls back to the computed importance order, and one call per
    run is all it ever makes.
  - The rerank retries once, not `llm.max_retries` (`RERANK_MAX_RETRIES`): retries spend
    the reasoning model's 20-request day, and the fallback is fine. On 2026-09-20,
    retries of 503s pushed it to 18 requests against a budget of 15.
    `LLMClient.structured(max_retries=...)` is the per-call override; every other call
    keeps the default.
  - Merging happens before writing, so impacts stay written-once. A call both layers make is
    one row with `origin=both`.
  - Known limitation: a rule disagreement only demotes impacts written in the same run. For a
    story analyzed earlier the reason is recorded but its rows keep their confidence, because
    an impact is the call as it was made (story 975, 2026-09-20).
  - Known gap, left open on purpose (user, 2026-09-20): the model declines on corporate
    governance stories even when a group company is in the universe (a Tata Sons board row,
    where TCS and Tata Steel are both listed). A prompt line would fix it, but a high decline
    rate is worth more for now. Revisit once there is track-record data by origin.
- Ticker validation (`newsdesk validate-tickers`):
  - One yfinance `history(period="5d")` call per symbol. Yahoo's name, currency, exchange and
    instrument type come from the same request's metadata.
  - Failures (`empty`, `stale`, `error`) are never replaced automatically.
  - Name, type, currency and exchange mismatches are review flags, not failures.
    `approved_yahoo_names` in assets.yaml silences a confirmed name (e.g. `^TNX`).
  - Every check is stored in `ticker_checks`. `run` and `scheduler` warn about symbols never
    validated, failed, or last checked over 30 days ago.
  - `yf.config.debug.hide_exceptions = False` replaces the deprecated `raise_errors`.
