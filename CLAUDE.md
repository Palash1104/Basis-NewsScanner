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

Phase 2 has not started; wait for the user.

Open follow-ups (not Phase 1 criteria):
- `llm.rate_limits` holds TEMPORARY conservative values (5 RPM, 100k input TPM, 30 RPD), not
  AI Studio's. At 30 RPD, hourly runs (~7 calls each, 20 on a cold start) don't fit. Replace
  them with the project's real limits before scheduling runs.
- Title matching mis-groups short headlines. "Two arrested on charges of rape" joined an ICE
  arrest story (score 78), "What are all the sanctions Iran is under?" joined the Russia
  sanctions bill story (78.3), and that bill story also split in two. A threshold change won't
  fix this; Phase 5 embeddings are meant to.
- `DEISGN.md` (the user's misspelled design-import notes) is untracked, left as the user's call.

## Commands

```
uv sync                                        # install deps (Python 3.12 via uv)
uv run newsdesk run                            # one pipeline pass
uv run newsdesk digest [--dry-run | --send]    # dry run is the default
uv run pytest -q                               # tests (network and LLM always mocked)
uv run python scripts/smoke_test.py [--send-test-message]   # live: feeds, 1 LLM call, Telegram
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
- `app/pipeline/rank.py` importance score (SPEC 7.4) · `app/pipeline/summarize.py` when to
  (re)summarize, article selection, story updates
- `app/llm/client.py` `LLMClient` (the only thing the pipeline calls: rate limiting, transient
  retries, validation retry, token counts), the `LLMProvider` protocol, `GeminiProvider` (REST
  via httpx), `AnthropicProvider` (SDK), `make_llm_client` factory
- `app/llm/ratelimit.py` per-model RPM / input TPM / RPD limiter; daily counts in
  `llm_daily_usage` · `prompts.py` prompt text + `*_PROMPT_VERSION` · `schemas.py` output models
- `app/delivery/format.py` Telegram HTML digest + splitting · `telegram.py` Bot API calls
- `app/cli.py` typer commands plus `run_pipeline` / `run_digest` (tested directly)
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
- Rate limits: RPM and input TPM use a sliding 60s window and wait; RPD is counted per quota day
  (midnight Pacific) in `llm_daily_usage` (extra table, not in SPEC §6) and raises
  `LLMQuotaError` when used up. A 429 that persists through retries also raises it.
  `summarize_stories` stops on `LLMQuotaError`, leaving the remaining stories `new`.
- Summary failures: API/network errors leave the story's status unchanged (retried next run);
  a quota error stops the whole summarize step for the run;
  refused, truncated or still-invalid output marks it `failed` and records the processed
  article count, so it is retried only after it gains 2+ articles or a new region.
- Ranking: `mean(source_weight)` is over the story's articles, as written in SPEC 7.4.
- Digest window: stories summarized since the start of the last digest sent without errors
  (or the lookback window if none). `--dry-run` records nothing; a failed send is recorded with
  errors and doesn't move the window.
- Grouping scorer/threshold chosen from `scripts/grouping_report.py` on real data (see comment in
  settings.yaml). Title-only fuzzy matching both over- and under-merges near the threshold;
  Phase 5 replaces it with embeddings.
