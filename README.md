# Newsdesk

A personal news digest. It fetches important news from the US, India and the rest of the
world, groups articles about the same event into stories, ranks them, explains the top stories
in 2–3 plain sentences, and sends a digest to Telegram. Later phases add market-impact notes,
price checks and a track record (see `SPEC.md`).

Research notes, not financial advice.

**Status:** Phase 1 complete (fetch → group → rank → summarize → Telegram digest). Market
impact notes start in Phase 2.

## Setup

1. Install [uv](https://docs.astral.sh/uv/). It installs Python 3.12 for the project.
2. Install dependencies:
   ```
   uv sync
   ```
3. Copy `.env.example` to `.env` and fill it in. `.env` is gitignored.
   - `GEMINI_API_KEY` (required with the default Gemini provider): create one at
     https://aistudio.google.com/apikey.
   - `ANTHROPIC_API_KEY` (optional): only needed if you set `llm.provider: anthropic`.
   - `TELEGRAM_BOT_TOKEN`: in Telegram, message **@BotFather**, send `/newbot`, and copy the
     token it gives you.
   - `TELEGRAM_CHAT_ID`: send any message to your new bot, then open
     `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
     `result[].message.chat.id`.
4. Check your Gemini rate limits. Google doesn't publish them in its docs; they're shown per
   project at https://aistudio.google.com/rate-limit. `llm.rate_limits` in
   `config/settings.yaml` holds this project's values for `gemini-3.5-flash-lite`: 15
   requests/min, 250,000 input tokens/min and 500 requests/day, plus a daily budget of 350.
   Newsdesk won't call Gemini without limits and never exceeds them. If your project's limits
   change, update them there.
5. Check everything works (a few feeds, one small LLM call, and the bot):
   ```
   uv run python scripts/smoke_test.py
   uv run python scripts/smoke_test.py --send-test-message
   ```

Without the LLM key (or Gemini rate limits), `newsdesk run` still fetches, groups and ranks,
but skips summaries and records why. The digest only includes summarized stories.

## Commands

| Command | What it does |
|---|---|
| `uv run newsdesk run` | One pipeline pass: fetch feeds, drop duplicates, group into stories, rank, summarize the top stories that are new or changed. Prints article, story and token counts. |
| `uv run newsdesk digest --dry-run` | Print the digest of stories summarized since the last sent digest. Doesn't count as a send. This is the default. |
| `uv run newsdesk digest --send` | Send that digest to Telegram. |
| `uv run newsdesk scheduler` | Keep running: a pipeline pass every `schedule.pipeline_every_hours` (hourly), and a digest at each `delivery.digest_times` (07:30 and 19:30 IST). Stop with Ctrl+C; restart it after editing `settings.yaml`. |
| `uv run python scripts/verify_feeds.py [--include-disabled]` | Check every feed responds and has recent entries. |
| `uv run python scripts/grouping_report.py --refresh` | Compare grouping thresholds on a fresh sample (writes `data/grouping_report.md`). |
| `uv run pytest -q` | Run the tests (network and LLM calls are mocked). |

Logs go to the console and `data/logs/newsdesk.log`. The SQLite database is
`data/newsdesk.db`. Every `run` and every sent digest is recorded in the `runs` table, with
errors and token counts.

## Scheduling

The simplest option is to leave `uv run newsdesk scheduler` running. It runs the pipeline every
hour on the hour and sends digests at 07:30 and 19:30 IST. To use cron instead, the equivalent
lines are:

```
CRON_TZ=Asia/Kolkata
0 * * * *     cd /path/to/newsdesk && uv run newsdesk run >> data/logs/cron.log 2>&1   # hourly
30 7,19 * * * cd /path/to/newsdesk && uv run newsdesk digest --send >> data/logs/cron.log 2>&1
```

`CRON_TZ` is supported by cronie (most Linux distributions). If your cron doesn't support it,
convert the times to the machine's time zone (07:30 IST is 02:00 UTC). On Windows, create two
Task Scheduler tasks running `uv run newsdesk run` and `uv run newsdesk digest --send`, with
the project folder as the start directory.

## Configuration

- `config/settings.yaml`: LLM provider and models, rate limits, lookback window, grouping
  threshold, ranking weights, digest size and times.

### LLM provider

The default is Gemini (`llm.provider: gemini`) with `gemini-3.5-flash-lite` for summaries and
`gemini-3.8-flash` as the reasoning model for later phases. Both are stable models on Gemini's
free tier. To use Anthropic instead, set `llm.provider: anthropic`, change the model IDs to
`claude-haiku-4-5` / `claude-sonnet-5` (see the comment in settings.yaml), and set
`ANTHROPIC_API_KEY`. The rest of the app doesn't change.
- `config/feeds.yaml`: news sources. Every enabled feed was checked with
  `scripts/verify_feeds.py`; see the header comment there for what was verified and why some
  outlets are disabled or missing.

## Cost and limits

Each story summary is one call to `gemini-3.5-flash-lite` (two if the first answer fails
validation, which hadn't happened in 24 live calls), about 450 input tokens each.

**Limits** (per Google Cloud project, from AI Studio): 15 requests/min, 250,000 input
tokens/min, 500 requests/day.
- New work stops at **350 requests a day**. The other 150 are kept for retries.
- The daily quota resets at **midnight Pacific time**: 12:30 IST during US daylight time, and
  13:30 IST otherwise. It does not reset at midnight IST.
- `newsdesk run` prints the day's usage and the next reset, e.g.
  `quota: 24/500 requests used on quota day 2026-09-17 (budget 350), resets 18 Sep 12:30 IST`.

**If the quota runs out mid-run,** the run skips the remaining summaries but still fetches,
groups and ranks, and finishes normally. The skipped stories are marked pending and are
summarized first on the next run once quota is available, even if newer stories have pushed
them out of the top 20.

**Calls per day.** From replaying real fetched news hour by hour; "busy" assumes every run is as
busy as the busiest one seen:

| Schedule | Now: typical / busy | After Phase 2 adds event extraction (about 2×) |
|---|---|---|
| Hourly (current) | 157 / 264 | 314 / 528 |
| Every 2 hours | 128 / 192 | 257 / 384 |
| Every 3 hours | 109 / 152 | 219 / 304 |

Hourly is the most frequent schedule that stays under the 350 budget today, so it's set. After
Phase 2, hourly would go over on busy days (the extra stories would be deferred, not lost), so
the schedule should be revisited then. A first run on an empty database makes about 20 calls.

On Gemini's free tier, calls cost nothing, but Google's pricing page says free-tier content is
used to improve Google's products. On Anthropic, `claude-haiku-4-5` costs a few cents for a
20-story run. Token counts are printed after every run and stored in `runs`.

## Known limitations

- **Grouping is title-based** (fuzzy word overlap), so short headlines can join the wrong story
  or one event can split in two. Seen in live runs: "Two arrested on charges of rape" grouped
  with an unrelated ICE arrest story, and US House passage of a Russia sanctions bill and
  India's reaction to it became separate stories. Embedding-based grouping is planned for
  Phase 5.
- **Google News links** are Google redirect URLs, labelled with the real outlet's name.
- **The digest holds at most 15 stories** (`delivery.max_stories_per_digest`). Summarized
  stories that don't make the cut aren't carried into the next digest unless they're
  summarized again.

## What is stored

Only headline, snippet (up to about 500 characters), URL, outlet name and publish time for each
article, plus the generated story summaries. Full article text is never fetched or stored.
