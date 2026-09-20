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
from typing import Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.config import CONFIG_DIR, AssetConfig
from app.llm.schemas import Channel, EventType, PolicyStance, Severity
from app.models import Event, Impact, Story

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


def apply_rules(
    session: Session, story: Story, event: Event, rules: Sequence[Rule], now: datetime
) -> list[Impact]:
    """Store the impacts of every rule matching `event`, and mark the story analyzed.

    Impacts already on the story (same rule, symbol and direction) are left alone, so
    re-extraction adds rather than rewrites. Any symbol that ends up with both directions is
    marked as a conflict ("mixed signals" in the digest).
    """
    existing = {(impact.rule_id, impact.symbol, impact.direction) for impact in story.impacts}
    created: list[Impact] = []
    for rule in matching_rules(rules, event):
        for template in rule.impacts:
            key = (rule.id, template.symbol, template.direction)
            if key in existing:
                continue
            existing.add(key)
            impact = Impact(
                story=story,
                event=event,
                symbol=template.symbol,
                direction=template.direction,
                mechanism=template.mechanism,
                order=template.order,
                confidence=template.confidence,
                origin=ORIGIN_PLAYBOOK,
                rule_id=rule.id,
                created_at=now,
            )
            session.add(impact)
            created.append(impact)
    if created:
        log.info(
            "story %d: %d impacts from %s",
            story.id,
            len(created),
            ", ".join(sorted({impact.rule_id for impact in created if impact.rule_id})),
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
