"""Quality gate for the LLM impact layer (SPEC 7.7 layer B), before it goes into live runs.

Runs layer B over the 20 stored event fixtures and shows, per fixture: what the playbook says,
what the model says, which symbols were invented and dropped, which rules it disputes and why,
and whether it returned no_clear_impact. Nothing is written to the database.

One call per fixture on the reasoning model (llm.reasoning_model). That model needs an entry in
llm.rate_limits, copied from https://aistudio.google.com/rate-limit; without one the LLM client
refuses to call it, by design.

Usage:
    uv run python scripts/impact_gate.py [--limit N]
"""

import argparse
import json
import logging
import sys
from types import SimpleNamespace

from app.config import ROOT_DIR, load_assets, load_env, load_settings
from app.db import init_db, make_engine, make_session_factory
from app.llm.client import LLMConfigError, LLMError, make_llm_client
from app.llm.prompts import IMPACT_PROMPT_VERSION
from app.pipeline.impact_llm import matched_impacts, request_impacts, validate_impacts
from app.pipeline.merge_impacts import merge_impacts
from app.pipeline.playbook import load_playbook, matching_rules

FIXTURES = ROOT_DIR / "tests" / "fixtures" / "event_extractions.json"
REPORT = ROOT_DIR / "data" / "impact_gate.md"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--limit", type=int, default=None, help="only the first N fixtures")
    parser.add_argument(
        "--model",
        default="summary",
        help="summary (default), reasoning, or an explicit model id",
    )
    parser.add_argument("--out", default=None, help="report path")
    parser.add_argument("--story", type=int, nargs="*", help="only these story ids")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    load_env()
    settings = load_settings()
    assets = load_assets()
    by_symbol = {asset.symbol: asset for asset in assets}
    rules = load_playbook(assets=assets)
    engine = make_engine(settings.resolve_path(settings.paths.database))
    init_db(engine)
    try:
        llm = make_llm_client(settings.llm, make_session_factory(engine), settings.tz)
    except LLMConfigError as exc:
        print(f"cannot run the gate: {exc}")
        return 1
    model = {
        "summary": settings.llm.summary_model,
        "reasoning": settings.llm.reasoning_model,
    }.get(args.model, args.model)
    if settings.llm.provider == "gemini" and model not in settings.llm.rate_limits:
        print(
            f"cannot run the gate: llm.rate_limits has no entry for {model}. Copy its requests "
            f"per minute, input tokens per minute and requests per day from "
            f"https://aistudio.google.com/rate-limit into config/settings.yaml."
        )
        return 1
    report_path = ROOT_DIR / args.out if args.out else REPORT

    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))["events"]
    if args.story:
        fixtures = [f for f in fixtures if f["story_id"] in set(args.story)]
    if args.limit:
        fixtures = fixtures[: args.limit]

    lines: list[str] = [
        "# LLM impact layer: quality gate",
        "",
        f"Model: {model} · prompt {IMPACT_PROMPT_VERSION} · "
        f"{len(fixtures)} fixtures · max {settings.impacts.max_impacts_per_story} impacts each",
        "",
    ]
    no_clear = 0
    invalid: list[str] = []
    capped: list[str] = []
    disagreements: list[str] = []
    origins: dict[str, int] = {}

    for fixture in fixtures:
        event = SimpleNamespace(**fixture["event"])
        story = SimpleNamespace(
            id=fixture["story_id"], headline=fixture["headline"], summary=fixture["summary"]
        )
        matched = matching_rules(rules, event)
        playbook = matched_impacts(matched)
        try:
            output = request_impacts(llm, story, event, playbook, assets, settings, model)
        except LLMError as exc:
            lines += [f"## {fixture['label']}", "", f"call failed: {exc}", ""]
            print(f"#{fixture['story_id']}: {exc}")
            continue

        checked = validate_impacts(
            output.value,
            by_symbol,
            {rule.id for rule in matched},
            settings.impacts.max_impacts_per_story,
        )
        merged = merge_impacts(
            playbook, checked.impacts, {d.rule_id for d in checked.disagreements}
        )
        for call in merged:
            origins[call.origin] = origins.get(call.origin, 0) + 1
        no_clear += checked.no_clear_impact
        invalid += [note for note in checked.dropped if note.startswith("invalid symbol")]
        capped += [note for note in checked.dropped if "lowered from high" in note]
        disagreements += [
            f"{fixture['story_id']} {d.rule_id}: {d.reason}" for d in checked.disagreements
        ]

        lines += [
            f"## #{fixture['story_id']} {fixture['label']}",
            "",
            f"*{fixture['headline']}*",
            "",
            f"event: {event.event_type} · {event.severity} · channels {', '.join(event.channels)}",
            "",
            "| layer | asset | dir | order | conf | mechanism |",
            "|---|---|---|---|---|---|",
        ]
        for rule_id, impact in playbook:
            name = by_symbol[impact.symbol].display_name
            lines.append(
                f"| playbook `{rule_id}` | {name} | {impact.direction} | {impact.order} | "
                f"{impact.confidence} | {impact.mechanism} |"
            )
        for impact in checked.impacts:
            name = by_symbol[impact.symbol].display_name
            lines.append(
                f"| **LLM** | {name} | {impact.direction} | {impact.order} | "
                f"{impact.confidence} ({impact.horizon}) | {impact.mechanism} |"
            )
        if not playbook and not checked.impacts:
            lines.append("| — | — | — | — | — | no impacts from either layer |")
        lines.append("")
        if checked.no_clear_impact:
            lines.append("**no_clear_impact**: the model saw nothing worth calling.")
        if checked.dropped:
            lines += ["", "Dropped:"] + [f"- {note}" for note in checked.dropped]
        if checked.disagreements:
            lines += ["", "Rule disagreements:"] + [
                f"- `{d.rule_id}`: {d.reason}" for d in checked.disagreements
            ]
        merged_counts = ", ".join(
            f"{sum(1 for c in merged if c.origin == origin)} {origin}"
            for origin in ("playbook", "both", "llm")
            if any(c.origin == origin for c in merged)
        )
        lines += ["", f"Merged: {merged_counts or 'nothing'}", ""]
        print(
            f"#{fixture['story_id']:<4} playbook {len(playbook):>2} · llm {len(checked.impacts):>2}"
            f" · {'no_clear_impact' if checked.no_clear_impact else 'impacts'}"
            f"{' · dropped ' + str(len(checked.dropped)) if checked.dropped else ''}"
        )

    summary = [
        "## Summary",
        "",
        f"- no_clear_impact: **{no_clear} of {len(fixtures)}** fixtures",
        f"- invented symbols dropped: **{len(invalid)}**"
        + ("".join(f"\n  - {note}" for note in invalid) if invalid else ""),
        f"- second-order calls capped at medium: **{len(capped)}**"
        + ("".join(f"\n  - {note}" for note in capped) if capped else ""),
        f"- rule disagreements: **{len(disagreements)}**"
        + ("".join(f"\n  - {note}" for note in disagreements) if disagreements else ""),
        "- merged calls by origin: "
        + (", ".join(f"{count} {origin}" for origin, count in sorted(origins.items())) or "none"),
        f"- LLM calls: {llm.usage.calls}, tokens {llm.usage.input_tokens} in / "
        f"{llm.usage.output_tokens} out",
        "",
    ]
    report_path.write_text("\n".join([*lines[:3], *summary, *lines[3:]]) + "\n", encoding="utf-8")
    print("\n".join(summary))
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
