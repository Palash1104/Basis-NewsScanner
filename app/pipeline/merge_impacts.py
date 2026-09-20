"""Merge the playbook's impacts with the model's (SPEC 7.7).

- Both layers make the same call (same symbol, same direction) -> one impact, `origin=both`,
  keeping the rule id so the per-rule track record still works. Confidence is the higher of
  the two only when both call it first-order, otherwise the lower.
- They disagree on direction -> both are kept and marked `conflict`, shown as "mixed signals".
- The model says a rule doesn't fit this event -> the rule's impacts are still written, but at
  low confidence, and the reason is stored in `rule_disagreements`.
- The model adds a call the playbook missed -> `origin=llm`, no rule id.

Merging happens before anything is written, so impacts are still written once and never
edited: what Phase 4 scores is the call exactly as it was made.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.llm.schemas import LLMImpact
from app.pipeline.playbook import ORIGIN_PLAYBOOK, RuleImpact

ORIGIN_LLM = "llm"
ORIGIN_BOTH = "both"
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class MergedCall:
    symbol: str
    direction: str
    mechanism: str
    order: str
    confidence: str
    origin: str
    rule_id: str | None = None
    horizon: str | None = None  # only the model states one
    conflict: bool = False


def _combine_confidence(playbook: RuleImpact, model: LLMImpact) -> str:
    """SPEC 7.7: the higher of the two only if both agree it's first-order, else the lower."""
    pair = sorted([playbook.confidence, model.confidence], key=lambda c: _CONFIDENCE_RANK[c])
    both_first = playbook.order == "first" and model.order == "first"
    return pair[-1] if both_first else pair[0]


def merge_impacts(
    playbook: Sequence[tuple[str, RuleImpact]],
    model: Sequence[LLMImpact],
    disagreed_rules: set[str] | None = None,
) -> list[MergedCall]:
    """One call per (rule, symbol, direction) plus the model's own, with conflicts marked."""
    disagreed = disagreed_rules or set()
    by_call = {(impact.symbol, impact.direction): impact for impact in model}
    agreed: set[tuple[str, str]] = set()
    calls: list[MergedCall] = []

    for rule_id, template in playbook:
        key = (template.symbol, template.direction)
        match = by_call.get(key)
        confidence, horizon, origin = template.confidence, None, ORIGIN_PLAYBOOK
        if match is not None:
            agreed.add(key)
            origin = ORIGIN_BOTH
            horizon = match.horizon
            confidence = _combine_confidence(template, match)
        if rule_id in disagreed:
            confidence = "low"  # the rule still stands, but the model doubts it here
        if template.order == "second" and confidence == "high":
            confidence = "medium"
        calls.append(
            MergedCall(
                template.symbol,
                template.direction,
                template.mechanism,
                template.order,
                confidence,
                origin,
                rule_id,
                horizon,
            )
        )

    for impact in model:
        if (impact.symbol, impact.direction) in agreed:
            continue
        calls.append(
            MergedCall(
                impact.symbol,
                impact.direction,
                impact.mechanism,
                impact.order,
                impact.confidence,
                ORIGIN_LLM,
                None,
                impact.horizon,
            )
        )

    directions: dict[str, set[str]] = {}
    for call in calls:
        directions.setdefault(call.symbol, set()).add(call.direction)
    for call in calls:
        call.conflict = len(directions[call.symbol]) > 1
    return calls
