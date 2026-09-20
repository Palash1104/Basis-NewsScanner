"""Layer B of impact mapping (SPEC 7.7): ask the reasoning model which assets could move.

Everything the model returns is checked before it counts:
- symbols must be in the validated universe; anything else is dropped and reported, so an
  invented ticker is visible rather than silently stored;
- second-order calls are capped at medium confidence in code as well as in the prompt;
- at most `impacts.max_impacts_per_story` calls, duplicates collapsed;
- a disagreement about a rule that didn't even match this event is dropped.

This layer is additive: if the call fails or its quota runs out, the playbook's impacts are
still written and the story is still analyzed.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.config import AssetConfig, Settings
from app.llm.client import LLMClient, StructuredResult
from app.llm.prompts import impact_system_prompt, impact_user_prompt
from app.llm.schemas import LLMImpact, LLMImpacts, RuleDisagreement
from app.models import Event, Story
from app.pipeline.playbook import Rule, RuleImpact

log = logging.getLogger(__name__)

# Gemini counts thinking tokens against the output cap, and this is the app's hardest task.
IMPACT_MAX_TOKENS = 4096


@dataclass
class ValidatedImpacts:
    impacts: list[LLMImpact] = field(default_factory=list)
    disagreements: list[RuleDisagreement] = field(default_factory=list)
    no_clear_impact: bool = False
    # What was thrown away and why, for the run output and the quality gate.
    dropped: list[str] = field(default_factory=list)


def event_fields(event: Event) -> dict[str, object]:
    return {
        "event_type": event.event_type,
        "countries": ", ".join(event.countries) or "(none)",
        "entities": ", ".join(event.entities) or "(none)",
        "companies": ", ".join(event.companies) or "(none)",
        "channels": ", ".join(event.channels),
        "severity": event.severity,
        "policy_stance": event.policy_stance,
        "policy_actor": event.policy_actor or "(none)",
    }


def playbook_lines(matched: Sequence[tuple[str, RuleImpact]]) -> list[str]:
    return [
        f"{rule_id}: {impact.symbol} {impact.direction} ({impact.order}, "
        f"{impact.confidence}) - {impact.mechanism}"
        for rule_id, impact in matched
    ]


def matched_impacts(rules: Sequence[Rule]) -> list[tuple[str, RuleImpact]]:
    return [(rule.id, impact) for rule in rules for impact in rule.impacts]


def validate_impacts(
    output: LLMImpacts,
    assets: dict[str, AssetConfig],
    matched_rule_ids: set[str],
    max_impacts: int,
) -> ValidatedImpacts:
    """Keep only what the universe and SPEC 7.7 allow. Never raises."""
    result = ValidatedImpacts(no_clear_impact=output.no_clear_impact)
    seen: set[tuple[str, str]] = set()
    for impact in output.impacts:
        if impact.symbol not in assets:
            result.dropped.append(f"invalid symbol {impact.symbol!r} (not in the universe)")
            continue
        key = (impact.symbol, impact.direction)
        if key in seen:
            result.dropped.append(f"duplicate call on {impact.symbol} {impact.direction}")
            continue
        seen.add(key)
        if impact.order == "second" and impact.confidence == "high":
            result.dropped.append(
                f"{impact.symbol}: second-order confidence lowered from high to medium"
            )
            impact = impact.model_copy(update={"confidence": "medium"})
        if len(result.impacts) >= max_impacts:
            result.dropped.append(f"over the cap of {max_impacts}: dropped {impact.symbol}")
            continue
        result.impacts.append(impact)

    for disagreement in output.rule_disagreements:
        if disagreement.rule_id not in matched_rule_ids:
            result.dropped.append(
                f"disagreement about {disagreement.rule_id!r}, which didn't match this event"
            )
            continue
        result.disagreements.append(disagreement)
    if result.impacts:
        result.no_clear_impact = False
    return result


def request_impacts(
    llm: LLMClient,
    story: Story,
    event: Event,
    matched: Sequence[tuple[str, RuleImpact]],
    assets: Sequence[AssetConfig],
    settings: Settings,
    model: str | None = None,
) -> StructuredResult[LLMImpacts]:
    """One impact-mapping call for a story. Raises the LLMClient errors."""
    assert story.summary is not None
    return llm.structured(
        model=model or settings.llm.summary_model,
        system=impact_system_prompt(assets, settings.impacts.max_impacts_per_story),
        user=impact_user_prompt(
            story.headline, story.summary, event_fields(event), playbook_lines(matched)
        ),
        schema=LLMImpacts,
        max_tokens=IMPACT_MAX_TOKENS,
        purpose=f"impacts for story {story.id}",
    )
