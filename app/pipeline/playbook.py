"""Playbook rules: the deterministic half of impact mapping (SPEC 7.7 layer A, rules in §9).

`config/playbook.yaml` holds hand-written cause -> effect rules. A rule matches an extracted
event when every condition it lists matches (AND across fields, OR within a field's list), and
each matched rule contributes its impacts to the story.

Matching notes:
- Entity and policy-actor conditions match whole words, case-insensitively, so "RBI" doesn't
  match "Herbie" and "Fed" doesn't match "FedEx". Rules list the aliases they accept.
- `policy_actor_any` matches only the authority whose stance the event describes, so a Fed
  decision can't trigger an RBI rule just because the articles mention the RBI reacting.
- Country names are canonical (see countries.py).

Impacts are written once and never edited: they are the call as it was made, which Phase 4
scores. Re-extracting a story adds only impacts it doesn't already have.
"""

import logging
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.config import CONFIG_DIR, AssetConfig, CallProvenance
from app.llm.schemas import Channel, EventType, PolicyStance, Severity
from app.models import Event, Impact, Story

if TYPE_CHECKING:
    from app.pipeline.merge_impacts import MergedCall

log = logging.getLogger(__name__)

Direction = Literal["up", "down"]
Order = Literal["first", "second"]
Confidence = Literal["high", "medium", "low"]
ORIGIN_PLAYBOOK = "playbook"
ANALYZED_STATUS = "analyzed"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuleImpact(_Strict):
    symbol: str
    direction: Direction
    order: Order
    confidence: Confidence
    mechanism: str = Field(min_length=1)

    @model_validator(mode="after")
    def _second_order_cap(self) -> "RuleImpact":
        # SPEC 7.7: second-order calls are capped at medium confidence.
        if self.order == "second" and self.confidence == "high":
            raise ValueError(f"{self.symbol}: second-order impacts can be medium at most")
        return self


class RuleConditions(_Strict):
    """Every condition given must match. Omitted conditions don't constrain anything."""

    event_types: list[EventType] | None = None
    channels_any: list[Channel] | None = None
    countries_any: list[str] | None = None
    countries_all: list[str] | None = None
    entities_any: list[str] | None = None
    policy_actor_any: list[str] | None = None
    severity_any: list[Severity] | None = None
    policy_stance_any: list[PolicyStance] | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> "RuleConditions":
        if not any(value for value in self.__dict__.values()):
            raise ValueError("a rule needs at least one condition")
        return self


class Rule(_Strict):
    id: str
    description: str
    when: RuleConditions
    impacts: list[RuleImpact] = Field(min_length=1)


class MatchableEvent(Protocol):
    """What matching needs: a stored Event row or any object with these fields."""

    event_type: str
    countries: list[str]
    entities: list[str]
    channels: list[str]
    severity: str
    policy_stance: str
    policy_actor: str | None
    is_new_development: bool


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def phrase_matches(alias: str, text: str) -> bool:
    """True if `alias` appears in `text` as whole words, in order."""
    needle, haystack = _words(alias), _words(text)
    if not needle:
        return False
    return any(
        haystack[start : start + len(needle)] == needle
        for start in range(len(haystack) - len(needle) + 1)
    )


def _any_alias(aliases: Sequence[str], values: Iterable[str]) -> bool:
    return any(phrase_matches(alias, value) for alias in aliases for value in values)


def matches(rule: Rule, event: MatchableEvent) -> bool:
    when = rule.when
    if when.event_types is not None and event.event_type not in when.event_types:
        return False
    if when.channels_any is not None and not set(when.channels_any) & set(event.channels):
        return False
    if when.countries_any is not None and not set(when.countries_any) & set(event.countries):
        return False
    if when.countries_all is not None and not set(when.countries_all) <= set(event.countries):
        return False
    if when.severity_any is not None and event.severity not in when.severity_any:
        return False
    if when.policy_stance_any is not None and event.policy_stance not in when.policy_stance_any:
        return False
    if when.entities_any is not None and not _any_alias(when.entities_any, event.entities):
        return False
    return when.policy_actor_any is None or _any_alias(
        when.policy_actor_any, [event.policy_actor or ""]
    )


def maps_to_impacts(event: MatchableEvent) -> bool:
    """SPEC 7.7: skip impact mapping for old news and events with no market channel."""
    return event.is_new_development and list(event.channels) != ["none"]


def matching_rules(rules: Sequence[Rule], event: MatchableEvent) -> list[Rule]:
    if not maps_to_impacts(event):
        return []
    return [rule for rule in rules if matches(rule, event)]


def load_playbook(
    path: Path | None = None, assets: Iterable[AssetConfig] | None = None
) -> list[Rule]:
    """Load and check the rules. Raises ValueError on a duplicate id, or (when `assets` is
    given) on any symbol that isn't in the validated universe."""
    raw = yaml.safe_load((path or CONFIG_DIR / "playbook.yaml").read_text(encoding="utf-8"))
    rules = [Rule.model_validate(item) for item in raw]
    ids = [rule.id for rule in rules]
    duplicates = {rule_id for rule_id in ids if ids.count(rule_id) > 1}
    if duplicates:
        raise ValueError(f"duplicate playbook rule ids: {sorted(duplicates)}")
    if assets is not None:
        known = {asset.symbol for asset in assets}
        unknown = {
            impact.symbol for rule in rules for impact in rule.impacts if impact.symbol not in known
        }
        if unknown:
            raise ValueError(
                f"playbook uses symbols that aren't in config/assets.yaml: {sorted(unknown)}"
            )
    return rules


def stories_awaiting_impacts(session: Session, since: datetime) -> list[Story]:
    """Stories that have a recent event but were never analyzed: the impacts step didn't
    finish (a crash, or the process stopped). Without this they would keep their event and
    never get impacts, because the step otherwise only looks at this run's extractions."""
    from sqlalchemy import select

    return list(
        session.scalars(
            select(Story)
            .join(Event, Event.story_id == Story.id)
            .where(Story.status != ANALYZED_STATUS, Event.created_at >= since)
            .distinct()
        )
    )


def apply_rules(
    session: Session, story: Story, event: Event, rules: Sequence[Rule], now: datetime
) -> list[Impact]:
    """Store the impacts of every rule matching `event` (layer A only), and mark the story
    analyzed. With the LLM layer on, `store_calls` is given merged calls instead."""
    from app.pipeline.merge_impacts import merge_impacts

    matched = [
        (rule.id, impact) for rule in matching_rules(rules, event) for impact in rule.impacts
    ]
    return store_calls(session, story, event, merge_impacts(matched, []), now)


def store_calls(
    session: Session,
    story: Story,
    event: Event,
    calls: Sequence["MergedCall"],
    now: datetime,
    llm: CallProvenance | None = None,
) -> list[Impact]:
    """Write calls the story doesn't already have, and mark it analyzed.

    A call the story already has (same rule, symbol and direction) is left alone, so
    re-analysis adds rather than rewrites. Any symbol called both ways is marked as a
    conflict ("mixed signals" in the digest).

    `llm` is the layer-B call this run made, recorded on the calls it produced (origin `llm`
    or `both`) and left null on the playbook's own, which no model touched.
    """
    existing = {(impact.rule_id, impact.symbol, impact.direction) for impact in story.impacts}
    created: list[Impact] = []
    for call in calls:
        key = (call.rule_id, call.symbol, call.direction)
        if key in existing:
            continue
        existing.add(key)
        from_model = llm if call.origin != ORIGIN_PLAYBOOK else None
        impact = Impact(
            story=story,
            event=event,
            symbol=call.symbol,
            direction=call.direction,
            mechanism=call.mechanism,
            order=call.order,
            confidence=call.confidence,
            horizon=call.horizon,
            origin=call.origin,
            rule_id=call.rule_id,
            model=from_model.model if from_model else None,
            prompt_version=from_model.prompt_version if from_model else None,
            temperature=from_model.temperature if from_model else None,
            seed=from_model.seed if from_model else None,
            created_at=now,
        )
        session.add(impact)
        created.append(impact)
    if created:
        log.info(
            "story %d: %d impacts (%s)",
            story.id,
            len(created),
            ", ".join(sorted({impact.origin for impact in created})),
        )
    mark_conflicts([*story.impacts, *created])
    story.status = ANALYZED_STATUS
    return created


def mark_conflicts(impacts: Sequence[Impact]) -> None:
    """Flag every symbol called in both directions on the same story."""
    directions: dict[str, set[str]] = {}
    for impact in impacts:
        directions.setdefault(impact.symbol, set()).add(impact.direction)
    for impact in impacts:
        impact.conflict = len(directions[impact.symbol]) > 1
