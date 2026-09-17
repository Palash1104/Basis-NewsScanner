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
4. Enter your Gemini rate limits. Google doesn't publish free-tier limits in its docs; they
   are shown per project at https://aistudio.google.com/rate-limit. Copy the summary model's
   requests per minute, input tokens per minute and requests per day into `llm.rate_limits` in
   `config/settings.yaml`. Newsdesk won't call Gemini without limits, and never exceeds them.
   **The file currently has temporary conservative values** (5 requests/min, 100,000 input
   tokens/min, 30 requests/day), which are not Google's numbers. At 30 requests a day, hourly
   runs won't fit (see Cost and limits), so replace them with your project's values before
   scheduling runs.
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
| `uv run python scripts/verify_feeds.py [--include-disabled]` | Check every feed responds and has recent entries. |
| `uv run python scripts/grouping_report.py --refresh` | Compare grouping thresholds on a fresh sample (writes `data/grouping_report.md`). |
| `uv run pytest -q` | Run the tests (network and LLM calls are mocked). |

Logs go to the console and `data/logs/newsdesk.log`. The SQLite database is
`data/newsdesk.db`. Every `run` and every sent digest is recorded in the `runs` table, with
errors and token counts.

## Scheduling

A built-in scheduler (`newsdesk scheduler`) comes in a later phase. Until then, use cron. Run
the pipeline hourly and send digests at the times in `config/settings.yaml` (07:30 and 19:30 IST):

```
CRON_TZ=Asia/Kolkata
0 * * * *     cd /path/to/newsdesk && uv run newsdesk run >> data/logs/cron.log 2>&1
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

Each story summary is one LLM call (two if the first answer fails validation), roughly 1,000
input tokens. Replaying real fetched news hour by hour gave:

- **First run** (empty database): 20 calls, one per top story.
- **Hourly runs after that:** about 7 calls on average, at most 10 in the replay, since only new
  stories and stories that gained 2+ articles or a new region are summarized again.
- **Per day with hourly runs:** about 170 calls typically, about 240 on a busy day. With
  validation retries on every story it could reach twice that, which is unlikely.

Compare that with the requests-per-day limit AI Studio shows for your project. The limiter stops
LLM calls for the rest of a run once the daily limit is reached, and those stories are picked
up on the next run after the quota resets (midnight Pacific).

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
