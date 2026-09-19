"""Live smoke test against real services: a few feeds, one LLM call, Telegram getMe.

The LLM call uses the configured provider (llm.provider in config/settings.yaml).

Checks whose secrets are missing from .env are reported as SKIP. The LLM check costs a fraction
of a cent on paid tiers. Only the LLM daily usage counter is written to the database.

Usage:
    uv run python scripts/smoke_test.py
    uv run python scripts/smoke_test.py --send-test-message   # also posts one Telegram message
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from types import SimpleNamespace

from app.config import ROOT_DIR, get_secret, load_env, load_feeds, load_settings
from app.db import init_db, make_engine, make_session_factory
from app.delivery.telegram import TelegramError, get_me, send_messages
from app.llm.client import LLMConfigError, LLMError, make_llm_client
from app.llm.prompts import SUMMARY_SYSTEM, summary_user_prompt
from app.llm.schemas import StorySummary
from app.pipeline.fetch import SourceResolver, fetch_all
from app.pipeline.summarize import SUMMARY_MAX_TOKENS


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--send-test-message", action="store_true")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    load_env()
    settings = load_settings()
    all_feeds = load_feeds(include_disabled=True)
    outcomes: dict[str, str] = {}

    # 1. Feeds: one direct feed and one Google News feed.
    enabled = [feed for feed in all_feeds if feed.enabled]
    sample = [
        next(f for f in enabled if not f.is_google_news),
        next(f for f in enabled if f.is_google_news),
    ]
    results = asyncio.run(fetch_all(sample, settings, resolver=SourceResolver(all_feeds)))
    articles = []
    for result in results:
        print(
            f"feed {result.feed.name}: {'OK' if result.ok else 'FAIL'} "
            f"{len(result.articles)} entries {result.error or ''}"
        )
        articles.extend(result.articles)
    outcomes["feeds"] = "OK" if all(r.ok and r.articles for r in results) else "FAIL"

    # 2. LLM: summarize the newest article from the direct feed.
    engine = make_engine(settings.resolve_path(settings.paths.database))
    init_db(engine)
    try:
        llm = make_llm_client(settings.llm, make_session_factory(engine))
    except LLMConfigError as exc:
        llm = None
        outcomes["llm"] = f"SKIP ({exc})"
    if llm is not None and not articles:
        outcomes["llm"] = "SKIP (no articles fetched)"
    elif llm is not None:
        article = max(results[0].articles or articles, key=lambda a: a.published_at)
        try:
            output = llm.structured(
                model=settings.llm.summary_model,
                system=SUMMARY_SYSTEM,
                user=summary_user_prompt([article]),
                schema=StorySummary,
                max_tokens=SUMMARY_MAX_TOKENS,
                purpose="smoke test",
            )
            print(
                f"\nLLM ({settings.llm.provider} {output.model}): {output.value.headline}\n"
                f"  {output.value.summary}"
            )
            print(f"  tokens: {output.input_tokens} input, {output.output_tokens} output")
            outcomes["llm"] = "OK"
        except LLMError as exc:
            print(f"\nLLM failed: {exc}")
            outcomes["llm"] = "FAIL"

    # 2b. Regions: the Fiji HIV story (real articles) must not be tagged US or India just
    # because of where the outlets are based.
    if llm is not None:
        fixtures = json.loads(
            (ROOT_DIR / "tests/fixtures/grouping_regressions.json").read_text(encoding="utf-8")
        )
        fiji = [
            SimpleNamespace(
                source_name=a["source_name"],
                title=a["title"],
                snippet=a["snippet"],
                published_at=datetime.fromisoformat(a["published_at"]),
            )
            for a in fixtures["fiji_regions"]["articles"]
        ]
        try:
            output = llm.structured(
                model=settings.llm.summary_model,
                system=SUMMARY_SYSTEM,
                user=summary_user_prompt(fiji),
                schema=StorySummary,
                max_tokens=SUMMARY_MAX_TOKENS,
                purpose="smoke test regions",
            )
            regions = output.value.regions
            print(f"\nFiji HIV story: {output.value.headline}\n  regions: {regions}")
            outcomes["regions"] = (
                "OK" if not {"US", "India"} & set(regions) else f"FAIL (tagged {regions})"
            )
        except LLMError as exc:
            print(f"\nRegions check failed: {exc}")
            outcomes["regions"] = "FAIL"

    # 3. Telegram: getMe, and optionally one message.
    token = get_secret("TELEGRAM_BOT_TOKEN")
    chat_id = get_secret("TELEGRAM_CHAT_ID")
    if not token:
        outcomes["telegram"] = "SKIP (TELEGRAM_BOT_TOKEN not set)"
    else:
        try:
            bot = asyncio.run(get_me(token, settings.http))
            print(f"\nTelegram bot: @{bot.get('username')}")
            outcomes["telegram"] = "OK"
            if args.send_test_message:
                if not chat_id:
                    outcomes["telegram"] = "FAIL (TELEGRAM_CHAT_ID not set)"
                else:
                    text = "<b>Newsdesk smoke test</b>\nTelegram delivery works."
                    asyncio.run(send_messages([text], token, chat_id, settings.http))
                    print("Telegram test message sent.")
        except TelegramError as exc:
            print(f"\nTelegram failed: {exc}")
            outcomes["telegram"] = "FAIL"

    print("\nSummary:")
    for check, outcome in outcomes.items():
        print(f"  {check:<9} {outcome}")
    return 1 if any(outcome.startswith("FAIL") for outcome in outcomes.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
