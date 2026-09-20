"""Extract events for a hand-picked set of real stored stories and save them as test fixtures
(tests/fixtures/event_extractions.json), for checking the event prompt and testing the playbook
against real events.

Nothing is written to the stories or events tables. Stories without a summary are summarized in
memory first (one extra call each). Every call goes through the normal rate limiter and daily
budget.

Usage:
    uv run python scripts/event_fixtures.py            # the default set below (~23 calls)
    uv run python scripts/event_fixtures.py 2 63 121   # specific story ids
"""

import argparse
import json
import logging
import sys
from datetime import UTC

from app.config import ROOT_DIR, load_env, load_settings
from app.db import init_db, make_engine, make_session_factory
from app.llm.client import LLMError, make_llm_client
from app.llm.prompts import (
    EVENT_PROMPT_VERSION,
    EVENT_SYSTEM,
    SUMMARY_SYSTEM,
    event_user_prompt,
    summary_user_prompt,
)
from app.llm.schemas import EventExtraction, StorySummary
from app.models import Story, utcnow
from app.pipeline.extract_event import EVENT_MAX_TOKENS, event_articles, normalize_event
from app.pipeline.summarize import SUMMARY_MAX_TOKENS

OUT = ROOT_DIR / "tests" / "fixtures" / "event_extractions.json"

# Stored stories chosen for variety (2026-09-19): geopolitics, central banks and currency,
# corporate/legal/policy, tech, and stories with no market angle.
DEFAULT_STORIES = {
    2: "geopolitics: US sanctions law allowing tariffs on India",
    111: "geopolitics: Saudi-Houthi strikes, Riyadh air-raid alerts",
    317: "geopolitics: China presses Iran to curb Houthi attacks",
    8: "geopolitics: India-Pakistan naval collision",
    612: "geopolitics: US-Denmark security deal on Greenland",
    700: "defence: US clears $2.7bn defence package for Ukraine",
    621: "geopolitics: North Korea rejects IAEA criticism",
    63: "central bank: Federal Reserve raises rates",
    132: "central bank: Bank of England seen holding rates",
    39: "currency: rupee slips below 96 per dollar",
    716: "analysis: high oil prices could be worse thanks to China",
    179: "corporate: Tata Sons board row over chairman",
    392: "corporate: CoreWeave $3bn convertible debt sale",
    635: "legal: Uber $40m award in passenger death case",
    616: "policy: US extends $100,000 H-1B visa fee",
    637: "tech: Gemini AI hacked three companies in testing",
    121: "non-market: Fiji HIV crisis",
    47: "non-market: Buckingham Palace answers memoir claims",
    181: "non-market: Kennedy Center protests",
    29: "politics: Russian parliamentary elections",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("story_ids", nargs="*", type=int)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    load_env()
    settings = load_settings()
    engine = make_engine(settings.resolve_path(settings.paths.database))
    init_db(engine)
    session_factory = make_session_factory(engine)
    llm = make_llm_client(settings.llm, session_factory, settings.tz)
    model = settings.llm.summary_model
    labels = {story_id: DEFAULT_STORIES.get(story_id, "") for story_id in args.story_ids}

    fixtures = []
    with session_factory() as session:
        for story_id, label in (labels or DEFAULT_STORIES).items():
            story = session.get(Story, story_id)
            if story is None:
                print(f"story {story_id}: not found")
                continue
            articles = event_articles(story, settings)
            headline, summary, source = story.headline, story.summary, "stored"
            try:
                if summary is None:
                    written = llm.structured(
                        model=model,
                        system=SUMMARY_SYSTEM,
                        user=summary_user_prompt(articles),
                        schema=StorySummary,
                        max_tokens=SUMMARY_MAX_TOKENS,
                        purpose=f"fixture summary story {story_id}",
                    ).value
                    headline, summary, source = written.headline, written.summary, "generated"
                output = llm.structured(
                    model=model,
                    system=EVENT_SYSTEM,
                    user=event_user_prompt(headline, summary, articles),
                    schema=EventExtraction,
                    max_tokens=EVENT_MAX_TOKENS,
                    purpose=f"fixture event story {story_id}",
                )
            except LLMError as exc:
                print(f"story {story_id}: {exc}")
                continue
            event, notes = normalize_event(output.value)
            fixtures.append(
                {
                    "story_id": story_id,
                    "label": label,
                    "headline": headline,
                    "summary": summary,
                    "summary_source": source,
                    "regions": list(story.regions or []),
                    "articles": [
                        {
                            "source_name": a.source_name,
                            "title": a.title,
                            "snippet": a.snippet,
                            "published_at": a.published_at.astimezone(UTC).isoformat(),
                        }
                        for a in articles
                    ],
                    "event": event.model_dump(),
                    "raw_event": output.value.model_dump(),
                    "notes": notes,
                    "tokens": [output.input_tokens, output.output_tokens],
                }
            )
            print(f"story {story_id}: {event.event_type} {event.channels} {event.severity}")
        session.rollback()  # nothing about the stories changes

    OUT.write_text(
        json.dumps(
            {
                "_about": "Real event extractions for stored stories, made by "
                "scripts/event_fixtures.py. Used to check the event prompt and to test the "
                "playbook against real events.",
                "model": model,
                "prompt_version": EVENT_PROMPT_VERSION,
                "extracted_at": utcnow().isoformat(),
                "events": fixtures,
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"{len(fixtures)} fixtures → {OUT} · LLM calls {llm.usage.calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
