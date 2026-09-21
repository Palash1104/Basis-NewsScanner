"""What a story's calls look like, decided once for every reader.

The digest (SPEC 10) and the web feed (SPEC 11) must say the same thing about a story: the
same ordering, the same grouping of rules that agree, the same "mixed signals", the same
"+N more". Only the rendering differs - Telegram HTML there, chips here - so the decisions
live in this module and each renderer formats what it is given.

Nothing here touches the database or the network: it takes stored impacts and returns what to
show.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.config import AssetConfig
from app.models import Impact, Story
from app.pipeline.prices import format_move

_ORDER_RANK = {"first": 0, "second": 1}
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}
ORDER_LABEL = {"first": "1st", "second": "2nd"}
ORDER_WORDS = {"first": "first order", "second": "second order"}
# Where the call came from: the rules, the model, or both agreeing.
ORIGIN_LABEL = {"playbook": "playbook", "llm": "LLM", "both": "both"}


@dataclass(frozen=True)
class AssetCall:
    """One story's call on one asset, ready to show.

    A symbol called both ways becomes a single `conflict` entry, because the story does not
    have a direction for it; its mechanisms are kept so the reader can see both sides.
    """

    symbol: str
    name: str  # display name, with `up_means` where an arrow could be misread
    direction: str  # up | down; empty when conflict
    mechanism: str  # "A vs B" when conflict
    order: str  # first | second
    confidence: str  # high | medium | low
    origins: tuple[str, ...]  # playbook, llm, both
    rule_count: int  # rules that agreed on this exact call
    conflict: bool = False
    move: str | None = None  # "+2.4%", "+0.10 pts"; None when not priced yet
    move_up: bool | None = None  # sign of the actual move, not of the call
    label: str | None = None  # "already moved" / "moving against this call"
    impact_ids: tuple[int, ...] = ()

    @property
    def priced(self) -> bool:
        return self.move is not None

    @property
    def origin_label(self) -> str:
        return "+".join(ORIGIN_LABEL.get(origin, origin) for origin in self.origins)


@dataclass(frozen=True)
class StoryCalls:
    """Everything a story says about the market: what to show, and what was cut."""

    shown: list[AssetCall] = field(default_factory=list)
    extra: list[AssetCall] = field(default_factory=list)

    @property
    def any_calls(self) -> bool:
        return bool(self.shown or self.extra)


# A story summarized this long after it broke is worth dating: the reserved slots and the
# carry-over of pending summaries both surface stories a day or more old, and a reader
# should not have to guess whether "Kerala floods" happened this morning.
AGE_WORTH_SAYING = timedelta(hours=12)
# Past this, hours stop being useful and days read better.
AGE_IN_DAYS_AFTER = timedelta(hours=48)


def story_age(story: Story, now: datetime) -> str | None:
    """How long ago a story was first reported ("14h ago", "3d ago"), or None when it broke
    and was summarized close enough together that saying so adds nothing."""
    if story.updated_at - story.first_seen_at <= AGE_WORTH_SAYING:
        return None
    age = now - story.first_seen_at
    if age < timedelta(0):
        return None
    if age >= AGE_IN_DAYS_AFTER:
        return f"{int(age.total_seconds() // 86400)}d ago"
    return f"{int(age.total_seconds() // 3600)}h ago"


def call_rank(impact: Impact) -> tuple[int, int]:
    """First-order before second-order, then by confidence: the order every reader sees."""
    return _ORDER_RANK[impact.order], _CONFIDENCE_RANK[impact.confidence]


_rank = call_rank


def _display(symbol: str, assets: dict[str, AssetConfig], direction: str) -> str:
    """The asset's short name, saying what "up" means where an arrow is easy to misread."""
    asset = assets.get(symbol)
    if asset is None:
        return symbol
    if direction == "up" and asset.up_means:
        return f"{asset.display_name} ({asset.up_means})"
    return asset.display_name


def _move(
    impact: Impact, assets: dict[str, AssetConfig], labels: dict[int, str]
) -> tuple[str | None, bool | None, str | None]:
    """The move since the story broke, or None where there is no price yet (SPEC 7.8's
    "price unavailable": the market may simply not have opened)."""
    asset = assets.get(impact.symbol)
    if asset is None or impact.reference_price is None or impact.move_at_detection_pct is None:
        return None, None, None
    move = format_move(asset, impact.reference_price, impact.move_at_detection_pct)
    return move, impact.move_at_detection_pct >= 0, labels.get(impact.id)


def story_calls(
    impacts: Sequence[Impact],
    assets: dict[str, AssetConfig],
    limit: int,
    labels: dict[int, str] | None = None,
) -> StoryCalls:
    """One entry per asset: first-order before second-order, then by confidence.

    Rules that agree on the same call are counted, not repeated, and anything past `limit`
    moves to `extra` so a renderer can summarise it as "+N more".
    """
    labels = labels or {}
    conflicted = sorted({impact.symbol for impact in impacts if impact.conflict})
    calls: list[AssetCall] = []

    for symbol in conflicted:
        same = [impact for impact in impacts if impact.symbol == symbol]
        mechanisms = dict.fromkeys(impact.mechanism for impact in same)
        best = min(same, key=_rank)
        move, move_up, label = _move(best, assets, labels)
        calls.append(
            AssetCall(
                symbol=symbol,
                name=assets[symbol].display_name if symbol in assets else symbol,
                direction="",
                mechanism=" vs ".join(mechanisms),
                order=best.order,
                confidence=best.confidence,
                origins=tuple(sorted({impact.origin for impact in same})),
                rule_count=len({impact.rule_id for impact in same if impact.rule_id}),
                conflict=True,
                move=move,
                move_up=move_up,
                label=label,
                impact_ids=tuple(impact.id for impact in same),
            )
        )

    grouped: dict[tuple[str, str], list[Impact]] = {}
    for impact in impacts:
        if impact.symbol not in conflicted:
            grouped.setdefault((impact.symbol, impact.direction), []).append(impact)

    agreed: list[AssetCall] = []
    for same in grouped.values():
        best = min(same, key=_rank)
        move, move_up, label = _move(best, assets, labels)
        agreed.append(
            AssetCall(
                symbol=best.symbol,
                name=_display(best.symbol, assets, best.direction),
                direction=best.direction,
                mechanism=best.mechanism,
                order=best.order,
                confidence=best.confidence,
                origins=tuple(sorted({impact.origin for impact in same})),
                rule_count=len({impact.rule_id for impact in same if impact.rule_id}),
                move=move,
                move_up=move_up,
                label=label,
                impact_ids=tuple(impact.id for impact in same),
            )
        )
    agreed.sort(key=lambda call: (_ORDER_RANK[call.order], _CONFIDENCE_RANK[call.confidence]))

    # Conflicts are shown first and are never cut: "mixed signals" is the strongest thing a
    # story can say about an asset.
    return StoryCalls(shown=calls + agreed[:limit], extra=agreed[limit:])
