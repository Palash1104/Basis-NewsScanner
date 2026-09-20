import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from sqlalchemy.orm import Session

from app.config import load_assets
from app.models import Event, Story
from app.pipeline.playbook import (
    Rule,
    apply_rules,
    load_playbook,
    maps_to_impacts,
    matches,
    matching_rules,
    phrase_matches,
)
from tests.conftest import FIXTURES, NOW

RULES = {rule.id: rule for rule in load_playbook(assets=load_assets())}
CASES = yaml.safe_load((FIXTURES / "playbook_events.yaml").read_text(encoding="utf-8"))
REAL_EVENTS = {
    item["story_id"]: item
    for item in json.loads((FIXTURES / "event_extractions.json").read_text(encoding="utf-8"))[
        "events"
    ]
}

EVENT_DEFAULTS: dict[str, Any] = {
    "event_type": "other",
    "countries": [],
    "entities": [],
    "companies": [],
    "channels": [],
    "severity": "moderate",
    "policy_stance": "not_applicable",
    "policy_actor": None,
    "is_new_development": True,
}


def event(**fields: Any) -> SimpleNamespace:
    return SimpleNamespace(**(EVENT_DEFAULTS | fields))


def _cases(kind: str) -> list[tuple[str, str, SimpleNamespace]]:
    return [
        (rule_id, case["about"], event(**case["event"]))
        for rule_id, spec in CASES.items()
        for case in spec[kind]
    ]


def _ids(cases: list[tuple[str, str, SimpleNamespace]]) -> list[str]:
    return [f"{rule_id}: {about}" for rule_id, about, _ in cases]


# ---------------------------------------------------------------- the rules themselves


def test_playbook_loads_and_every_symbol_is_in_the_universe() -> None:
    assert len(RULES) == 15
    assert sum(len(rule.impacts) for rule in RULES.values()) == 72


def test_every_rule_has_a_match_and_a_no_match_fixture() -> None:
    """A new rule can't be added without tests for it."""
    assert set(CASES) == set(RULES)
    for rule_id, spec in CASES.items():
        assert spec.get("match") and spec.get("no_match"), rule_id


@pytest.mark.parametrize(("rule_id", "about", "item"), _cases("match"), ids=_ids(_cases("match")))
def test_rule_matches_its_event(rule_id: str, about: str, item: SimpleNamespace) -> None:
    assert matches(RULES[rule_id], item), f"{rule_id} should match: {about}"


@pytest.mark.parametrize(
    ("rule_id", "about", "item"), _cases("no_match"), ids=_ids(_cases("no_match"))
)
def test_rule_does_not_match_its_near_miss(rule_id: str, about: str, item: SimpleNamespace) -> None:
    assert not matches(RULES[rule_id], item), f"{rule_id} should not match: {about}"


def test_paired_rules_are_mirror_images() -> None:
    for up, down in (("fed_hawkish", "fed_dovish"), ("rbi_dovish", "rbi_hawkish")):
        flipped = {
            (impact.symbol, "up" if impact.direction == "down" else "down", impact.confidence)
            for impact in RULES[down].impacts
        }
        assert {
            (impact.symbol, impact.direction, impact.confidence) for impact in RULES[up].impacts
        } == flipped
    shock = {i.symbol: i.direction for i in RULES["oil_supply_shock"].impacts}
    easing = {i.symbol: i.direction for i in RULES["oil_supply_easing"].impacts}
    assert set(shock) == set(easing)
    assert all(easing[symbol] != direction for symbol, direction in shock.items())


# ---------------------------------------------------------------- matcher behaviour


def test_conditions_are_anded_and_lists_are_ored() -> None:
    rule = Rule.model_validate(
        {
            "id": "t",
            "description": "t",
            "when": {
                "event_types": ["public_health", "natural_disaster"],
                "countries_any": ["Fiji"],
            },
            "impacts": [
                {
                    "symbol": "GC=F",
                    "direction": "up",
                    "order": "first",
                    "confidence": "low",
                    "mechanism": "m",
                }
            ],
        }
    )
    assert matches(rule, event(event_type="public_health", countries=["Fiji"]))
    assert matches(rule, event(event_type="natural_disaster", countries=["Fiji", "Tonga"]))
    assert not matches(rule, event(event_type="public_health", countries=["Tonga"]))
    assert not matches(rule, event(event_type="other", countries=["Fiji"]))


def test_countries_all_needs_every_country() -> None:
    rule = RULES["us_tariffs_on_india"]
    both = event(
        event_type="sanctions_trade_policy",
        channels=["tariffs_trade"],
        severity="major",
        countries=["United States", "India"],
    )
    assert matches(rule, both)
    assert not matches(rule, event(**{**both.__dict__, "countries": ["India"]}))


@pytest.mark.parametrize(
    ("alias", "text", "expected"),
    [
        ("RBI", "RBI", True),
        ("RBI", "Reserve Bank of India (RBI)", True),
        ("RBI", "Herbie Capital", False),  # substring matching would say yes
        ("Fed", "the Fed", True),
        ("Fed", "FedEx Corporation", False),
        ("Federal Reserve", "US Federal Reserve", True),
        ("NATO", "Nationwide Building Society", False),
        ("North Atlantic Treaty Organization", "NATO", False),  # only the listed alias matches
    ],
)
def test_entity_matching_is_whole_word(alias: str, text: str, expected: bool) -> None:
    assert phrase_matches(alias, text) is expected


def test_no_rules_for_old_news_or_events_with_no_channel() -> None:
    live = event(event_type="geopolitical_conflict", channels=["oil_supply"], severity="escalation")
    assert maps_to_impacts(live) and matching_rules(list(RULES.values()), live)
    analysis = event(**{**live.__dict__, "is_new_development": False})
    assert not maps_to_impacts(analysis) and matching_rules(list(RULES.values()), analysis) == []
    no_channel = event(event_type="public_health", channels=["none"], severity="major")
    assert not maps_to_impacts(no_channel)


# ---------------------------------------------------------------- loading and validation


def _write_rules(tmp_path, rules: list[dict]) -> Any:
    path = tmp_path / "playbook.yaml"
    path.write_text(yaml.safe_dump(rules), encoding="utf-8")
    return path


def _rule(**changes: Any) -> dict:
    rule = {
        "id": "r",
        "description": "d",
        "when": {"channels_any": ["oil_supply"]},
        "impacts": [
            {
                "symbol": "BZ=F",
                "direction": "up",
                "order": "first",
                "confidence": "high",
                "mechanism": "m",
            }
        ],
    }
    return rule | changes


def test_unknown_symbols_are_rejected(tmp_path) -> None:
    impacts = [{**_rule()["impacts"][0], "symbol": "NOTREAL"}]
    path = _write_rules(tmp_path, [_rule(impacts=impacts)])
    with pytest.raises(ValueError, match="aren't in config/assets.yaml"):
        load_playbook(path, assets=load_assets())
    assert load_playbook(path)  # without the universe it still loads


def test_second_order_impacts_cannot_be_high_confidence(tmp_path) -> None:
    impacts = [{**_rule()["impacts"][0], "order": "second"}]
    with pytest.raises(ValueError, match="medium at most"):
        load_playbook(_write_rules(tmp_path, [_rule(impacts=impacts)]))


def test_duplicate_rule_ids_and_empty_conditions_are_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="duplicate playbook rule ids"):
        load_playbook(_write_rules(tmp_path, [_rule(), _rule()]))
    with pytest.raises(ValueError, match="at least one condition"):
        load_playbook(_write_rules(tmp_path, [_rule(when={})]))


# ---------------------------------------------------------------- storing impacts


def _story_with_event(session: Session, **event_fields: Any) -> tuple[Story, Event]:
    story = Story(
        first_seen_at=NOW,
        updated_at=NOW,
        headline="h",
        summary="One. Two.",
        status="summarized",
        regions=["Global"],
    )
    row = Event(
        story=story,
        regions=["Global"],
        model="m",
        prompt_version="event-v2",
        created_at=NOW,
        **(EVENT_DEFAULTS | event_fields),
    )
    session.add_all([story, row])
    session.flush()
    return story, row


def test_impacts_are_written_once_and_mark_the_story_analyzed(session: Session) -> None:
    story, row = _story_with_event(
        session,
        event_type="geopolitical_conflict",
        countries=["Saudi Arabia"],
        channels=["oil_supply"],
        severity="escalation",
    )
    created = apply_rules(session, story, row, list(RULES.values()), NOW)
    session.flush()  # assigns impact ids and event_id

    assert story.status == "analyzed"
    assert len(created) == len(RULES["oil_supply_shock"].impacts)
    brent = next(impact for impact in created if impact.symbol == "BZ=F")
    assert (brent.direction, brent.order, brent.confidence) == ("up", "first", "high")
    assert (brent.origin, brent.rule_id, brent.event_id) == ("playbook", "oil_supply_shock", row.id)
    assert brent.mechanism and brent.created_at == NOW
    assert brent.horizon is None  # playbook rules don't state one; Phase 5 does

    # Re-extraction of the same event adds nothing.
    again = apply_rules(session, story, row, list(RULES.values()), NOW + timedelta(hours=3))
    assert again == [] and len(story.impacts) == len(created)


def test_events_with_no_market_channel_still_analyze_the_story(session: Session) -> None:
    story, row = _story_with_event(session, event_type="public_health", channels=["none"])
    assert apply_rules(session, story, row, list(RULES.values()), NOW) == []
    assert story.status == "analyzed" and story.impacts == []


def test_opposite_calls_on_one_asset_are_marked_as_a_conflict(session: Session) -> None:
    story, risk_off = _story_with_event(
        session,
        event_type="geopolitical_conflict",
        countries=["Iran"],
        channels=["safe_haven_demand"],
        severity="escalation",
    )
    apply_rules(session, story, risk_off, list(RULES.values()), NOW)  # gold up
    hawkish = Event(
        story=story,
        regions=[],
        model="m",
        prompt_version="event-v2",
        created_at=NOW,
        **(
            EVENT_DEFAULTS
            | {
                "event_type": "central_bank_monetary",
                "channels": ["us_interest_rates"],
                "severity": "escalation",
                "policy_stance": "hawkish",
                "policy_actor": "Federal Reserve",
            }
        ),
    )
    session.add(hawkish)
    session.flush()
    apply_rules(session, story, hawkish, list(RULES.values()), NOW)  # gold down

    gold = [impact for impact in story.impacts if impact.symbol == "GC=F"]
    assert {impact.direction for impact in gold} == {"up", "down"}
    assert all(impact.conflict for impact in gold)
    assert not any(impact.conflict for impact in story.impacts if impact.symbol == "^VIX")


# ---------------------------------------------------------------- against real extractions


@pytest.mark.parametrize(
    ("story_id", "expected"),
    [
        (63, ["fed_hawkish"]),  # not rbi_hawkish (the actor is the Fed), not rupee_sharp_fall
        (612, []),  # Greenland: a de-escalation must not buy defence stocks
        (317, ["oil_supply_easing"]),  # shipping-route de-escalation
        # v2 tags the Russian-oil sanctions law with an oil_supply channel as well, so the
        # supply rule fires too.
        (2, ["oil_supply_shock", "us_tariffs_on_india"]),
        (111, ["oil_supply_shock", "geopolitical_risk_off"]),
        (700, ["us_nato_defense_spending"]),
        (39, []),  # the rupee recovered: no rule
        (121, []),  # Fiji HIV: no market channel
        (716, []),  # analysis piece: not a new development
    ],
)
def test_rules_against_real_extracted_events(story_id: int, expected: list[str]) -> None:
    item = REAL_EVENTS[story_id]
    fired = [rule.id for rule in matching_rules(list(RULES.values()), event(**item["event"]))]
    assert fired == expected, f"{item['label']} fired {fired}"
