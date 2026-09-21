# BASIS

The product is **BASIS**. Everything internal keeps the original name `newsdesk`: the CLI
command, the Python package, the database file and the scheduled tasks.

A personal news digest. It fetches important news from the US, India and the rest of the
world, groups articles about the same event into stories, ranks them, explains the top stories
in 2–3 plain sentences, and sends a digest to Telegram. Later phases add market-impact notes,
price checks and a track record (see `SPEC.md`).

Research notes, not financial advice.

**Status:** Phase 5 complete (fetch → group → rank and rerank → summarize → extract the
event → apply the playbook and the LLM impact layer → check whether the market already moved
→ score the calls afterwards → Telegram digest). Market
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
   BASIS won't call Gemini without limits and never exceeds them. If your project's limits
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
| `uv run newsdesk scheduler` | Keep running: a pipeline pass every `schedule.pipeline_every_hours` (every 3 hours), and a digest at each `delivery.digest_times` (07:30 and 19:30 IST). Stop with Ctrl+C; restart it after editing `settings.yaml`. |
| `uv run python scripts/verify_feeds.py [--include-disabled]` | Check every feed responds and has recent entries. |
| `uv run newsdesk score` | Judge every call whose horizon is complete and print the track record. Safe to re-run: scores are written once. |
| `uv run newsdesk validate-tickers` | Check every symbol in `config/assets.yaml` has recent prices on Yahoo (writes `data/ticker_report.md`). |
| `uv run newsdesk health [--days 7]` | Whether the scheduler kept its slots, what the LLM layers used against their budgets, and which rules have enough judged calls to mean anything. Reads the database only, so it is safe to run while the scheduler is running. |
| `uv run python scripts/embedding_report.py [--refresh]` | Compare embedding grouping thresholds on real samples and the regression fixtures (writes `data/embedding_report.md`). |
| `uv run python scripts/regroup.py [--dry-run]` | Regroup every stored article with embeddings (no LLM calls). Changed stories that had a summary become `needs_resummary`. |
| `uv run python scripts/grouping_report.py --refresh` | The old title-matcher threshold report (the title matcher is now only a fallback). |
| `uv run pytest -q` | Run the tests (network and LLM calls are mocked). |

Logs go to the console and `data/logs/newsdesk.log`. The SQLite database is
`data/newsdesk.db`. Every `run` and every sent digest is recorded in the `runs` table, with
errors and token counts.

## Scheduling

### Windows: the scheduled tasks (recommended)

`uv run newsdesk scheduler` only runs while that terminal is open, so a reboot or a closed
window silently stops the digest. Install the Windows tasks instead:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
```

That creates three tasks under `\Newsdesk\`, with the times read from `settings.yaml` (via
`newsdesk schedule-times`), so re-run it after changing the schedule:

| Task | Command | When |
|---|---|---|
| `Newsdesk-pipeline` | `newsdesk run` | 01:00, 04:00, 07:00 … 22:00 |
| `Newsdesk-digest` | `newsdesk digest --send` | 07:30 and 19:30 |
| `Newsdesk-score` | `newsdesk score` | 03:30 |

They run as the logged-on user with the project's own virtualenv, need no administrator
rights, start again after a reboot and login with no terminal open, run as soon as possible
after a missed start, and may wake the machine. Output goes to `data\logs\tasks\<job>.log`
(and, as always, `data\logs\newsdesk.log`).

```powershell
# what is installed, and when each task runs next
Get-ScheduledTask -TaskPath "\Newsdesk\" | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskPath "\Newsdesk\" -TaskName Newsdesk-pipeline |
  Select-Object LastRunTime, LastTaskResult, NextRunTime   # LastTaskResult 0 = success

# run one now, without waiting for its time
Start-ScheduledTask -TaskPath "\Newsdesk\" -TaskName Newsdesk-pipeline
Get-Content data\logs\tasks\run.log -Tail 20

# pause and resume (a holiday, or while editing config)
Get-ScheduledTask -TaskPath "\Newsdesk\" | Disable-ScheduledTask
Get-ScheduledTask -TaskPath "\Newsdesk\" | Enable-ScheduledTask

# remove them
powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Remove
```

`uv run newsdesk health` afterwards says whether the slots were actually kept.

**Two runs never overlap.** Each job kind holds a lock file in `data/locks/` while it runs, so
a pipeline run still going when the next one starts makes the new one log "another pipeline
run is still going" and exit cleanly. The locks are per job kind, so a digest is never held up
by a slow run.

### Other platforms

`uv run newsdesk scheduler` runs the same three jobs in one foreground process (Ctrl+C stops
it; restart it after editing `settings.yaml`). For cron, the equivalent lines are:

```
CRON_TZ=Asia/Kolkata
0 1-22/3 * * * cd /path/to/newsdesk && uv run newsdesk run >> data/logs/cron.log 2>&1   # every 3h
30 7,19 * * * cd /path/to/newsdesk && uv run newsdesk digest --send >> data/logs/cron.log 2>&1
```

`CRON_TZ` is supported by cronie (most Linux distributions). If your cron doesn't support it,
convert the times to the machine's time zone (07:30 IST is 02:00 UTC). Add `newsdesk score`
daily as well, and note that cron, like the scheduled tasks, does not catch up on runs missed
while the machine was off (`anacron` does).

### Sleep, shutdown and missed runs

| | Windows tasks | `newsdesk scheduler` |
|---|---|---|
| Asleep at the run time | runs on wake, or wakes the machine | runs on wake if inside the misfire grace (15 min for a run, 30 for a digest, 60 for scoring), otherwise skipped |
| Off, or logged out | runs once, as soon as possible after login | skipped: the process wasn't there |
| After a reboot | starts by itself | must be started by hand |
| Two at once | the task waits; a run started by hand exits cleanly | the lock makes the second exit cleanly |

**Skipped slots rarely lose stories.** The next run re-fetches the whole
`pipeline.lookback_hours` window (12 hours), summaries and events skipped for quota are carried
over, and the digest covers everything summarized since the last digest that was sent without
errors. Downtime longer than the lookback window is what actually loses stories, and
`uv run newsdesk health` lists the slots that were missed.

## Configuration

- `config/settings.yaml`: LLM provider and models, rate limits, lookback window, grouping
  method and threshold, ranking weights, digest size and times.

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
- The per-minute limits count every call made through this app's database, including other
  processes (a smoke test, a manual run while the scheduler is running).
- The daily quota resets at **midnight Pacific time**: 12:30 IST during US daylight time, and
  13:30 IST otherwise. It does not reset at midnight IST.
- `newsdesk run` prints the day's usage and the next reset, e.g.
  `quota: 24/500 requests used on quota day 2026-09-17 (budget 350), resets 18 Sep 12:30 IST`.

**If the quota runs out mid-run,** the run skips the remaining summaries but still fetches,
groups and ranks, and finishes normally. The skipped stories are marked pending and are
summarized first on the next run once quota is available, even if newer stories have pushed
them out of the top 20.

**Calls per day.** From replaying 2,337 real articles run by run with the current grouping;
"busy" assumes every run is as busy as the busiest one seen. Phase 2 makes one event-extraction
call per summary:

| Schedule | Summaries only: typical / busy | Summaries + event extraction |
|---|---|---|
| Hourly | 126 / 240 | 253 / 480 |
| Every 2 hours | 109 / 168 | 218 / 336 |
| Every 3 hours (current) | 103 / 136 | 206 / 272 |

Every 3 hours is set: it stays under the 350 budget even on busy days, and each digest still
has a run 30 minutes before it. A first run on an empty database makes about 40 calls (20
summaries and 20 extractions).

On Gemini's free tier, calls cost nothing, but Google's pricing page says free-tier content is
used to improve Google's products. On Anthropic, `claude-haiku-4-5` costs a few cents for a
20-story run. Token counts are printed after every run and stored in `runs`.

## Known limitations

- **Grouping uses local embeddings** (`all-MiniLM-L6-v2`, cosine similarity to each story's
  centroid, threshold 0.55). An article must also resemble the story's first article (seed
  check, 0.45), so a story can't drift into a loose topic. The cost: a story whose first
  article is unusual can split. The Tata Sons chairman dispute split into three this way.
  - The model downloads once, about 90 MB, and after that runs offline.
  - Importing it adds roughly 40 seconds to a cold start on Windows.
  - Broad topics can still merge loosely related articles; a live run joined an NSE IPO story
    with other Indian IPO news.
  - Scores between 0.45 and 0.65 are logged to `data/logs/grouping_borderline.jsonl` for
    retuning.
  - If the model can't load, the run falls back to title matching and records an error.
- **A live blog can attach to the wrong story** (seen: an Iran war live blog on the Russia
  sanctions bill story). It doesn't affect summaries, sources or ranking.
- **Explainers, roundups and live blogs** are detected by headline patterns. They can attach to
  a story but never start one or count as a source. Each run prints every flagged headline, so
  misfires are visible.
- **The track record is a sanity check, not a backtest.** Overlapping news on the same asset
  makes attribution noisy: when two stories touch crude on the same day, neither can claim the
  move. Worse, n counts asset-calls rather than events — one story can produce a dozen
  correlated impacts — so the digest and the tables report how many distinct stories are behind
  each number. Read a hit rate as a hint about a rule, not a measurement.
- **Moves need an open market.** News that breaks after a close has no reference price until
  the next session, so those impacts show no move until then. That is normal, not an error.
- **Market impacts come from two layers.** Hand-written rules (`config/playbook.yaml`) run on
  every analysed story; a model adds to them on the five most significant stories of each run,
  and can flag a rule that doesn't fit. Each call records which layer it came from, so the
  track record can compare them. They are hypotheses: scoring says how often each is right.
- **Impacts are kept forever once written.** Re-analysing a story adds new calls but never
  edits old ones, so the track record reflects what was said at the time.
- **Google News links** are Google redirect URLs, labelled with the real outlet's name.
- **The digest holds at most 15 stories** (`delivery.max_stories_per_digest`). Summarized
  stories that don't make the cut aren't carried into the next digest unless they're
  summarized again.

## What is stored

Only headline, snippet (up to about 500 characters), URL, outlet name and publish time for each
article, plus the generated story summaries. Full article text is never fetched or stored.
