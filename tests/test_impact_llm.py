import pytest
from pydantic import ValidationError

from app.config import load_assets
from app.llm.prompts import allowed_assets_block, impact_system_prompt, impact_user_prompt
from app.llm.schemas import LLMImpact, LLMImpacts
from app.pipeline.impact_llm import matched_impacts, playbook_lines, validate_impacts
from app.pipeline.merge_impacts import ORIGIN_BOTH, ORIGIN_LLM, merge_impacts
from app.pipeline.playbook import ORIGIN_PLAYBOOK, RuleImpact, load_playbook

ASSETS = {asset.symbol: asset for asset in load_assets()}
RULES = {rule.id: rule for rule in load_playbook(assets=load_assets())}


def _impact(symbol: str, direction: str = "up", **fields) -> dict:
    return {
        "symbol": symbol,
        "direction": direction,
        "mechanism": "Cause leads to effect",
        "order": "first",
        "confidence": "medium",
        "horizon": "days",
        **fields,
    }


def _output(**fields) -> LLMImpacts:
    data = {"no_clear_impact": False, "impacts": [], "rule_disagreements": [], **fields}
    return LLMImpacts.model_validate(data)


def _rule_impact(symbol: str, direction: str, **fields) -> RuleImpact:
    values = {
        "symbol": symbol,
        "direction": direction,
        "order": "first",
        "confidence": "medium",
        "mechanism": "Playbook reason",
        **fields,
    }
    return RuleImpact.model_validate(values)


# ---------------------------------------------------------------- prompt and schema


def test_the_universe_block_lists_every_validated_asset() -> None:
    block = allowed_assets_block(list(ASSETS.values()))
    assert block.count("\n") == len(ASSETS) + 2  # the header row plus the two delimiters
    assert "BZ=F\tBrent crude oil futures\tcommodity\tGlobal\tEnergy\toil,crude" in block
    system = impact_system_prompt(list(ASSETS.values()), max_impacts=8)
    assert "Maximum 8 impacts" in system and "no_clear_impact=true" in system


def test_the_user_prompt_carries_the_event_and_what_the_playbook_said() -> None:
    lines = playbook_lines(matched_impacts([RULES["us_tariffs_on_india"]]))
    prompt = impact_user_prompt(
        "US signs tariff law", "One. Two.", {"event_type": "sanctions_trade_policy"}, lines
    )
    assert "event_type: sanctions_trade_policy" in prompt
    assert "us_tariffs_on_india: ^NSEI down (second, low)" in prompt
    assert impact_user_prompt("h", "s", {}, []).count("(none matched)") == 1


def test_no_clear_impact_cannot_come_with_impacts() -> None:
    with pytest.raises(ValidationError, match="must be empty"):
        _output(no_clear_impact=True, impacts=[_impact("BZ=F")])


# ---------------------------------------------------------------- validation


def test_invented_symbols_are_dropped_and_reported() -> None:
    output = _output(impacts=[_impact("BZ=F"), _impact("NIFTY50.NS"), _impact("TSLA")])
    checked = validate_impacts(output, ASSETS, set(), max_impacts=8)
    assert [impact.symbol for impact in checked.impacts] == ["BZ=F"]
    assert checked.dropped == [
        "invalid symbol 'NIFTY50.NS' (not in the universe)",
        "invalid symbol 'TSLA' (not in the universe)",
    ]


def test_second_order_high_confidence_is_capped_in_code() -> None:
    output = _output(impacts=[_impact("ONGC.NS", order="second", confidence="high")])
    checked = validate_impacts(output, ASSETS, set(), max_impacts=8)
    assert checked.impacts[0].confidence == "medium"
    assert "lowered from high to medium" in checked.dropped[0]


def test_duplicates_and_anything_over_the_cap_are_dropped() -> None:
    impacts = [_impact("BZ=F"), _impact("BZ=F"), _impact("CL=F"), _impact("GC=F")]
    checked = validate_impacts(_output(impacts=impacts), ASSETS, set(), max_impacts=2)
    assert [impact.symbol for impact in checked.impacts] == ["BZ=F", "CL=F"]
    assert "duplicate call on BZ=F up" in checked.dropped
    assert "over the cap of 2: dropped GC=F" in checked.dropped


def test_a_disagreement_about_a_rule_that_never_matched_is_dropped() -> None:
    output = _output(
        rule_disagreements=[
            {"rule_id": "oil_supply_shock", "reason": "this is a de-escalation"},
            {"rule_id": "fed_hawkish", "reason": "no central bank here"},
        ]
    )
    checked = validate_impacts(output, ASSETS, {"oil_supply_shock"}, max_impacts=8)
    assert [d.rule_id for d in checked.disagreements] == ["oil_supply_shock"]
    assert "didn't match this event" in checked.dropped[0]


def test_no_clear_impact_is_cleared_when_valid_impacts_survive() -> None:
    checked = validate_impacts(_output(impacts=[_impact("BZ=F")]), ASSETS, set(), max_impacts=8)
    assert checked.no_clear_impact is False
    empty = validate_impacts(_output(no_clear_impact=True), ASSETS, set(), max_impacts=8)
    assert empty.no_clear_impact is True and empty.impacts == []


# ---------------------------------------------------------------- merge


def test_agreement_becomes_one_call_from_both_layers() -> None:
    playbook = [("oil_supply_shock", _rule_impact("BZ=F", "up", confidence="high"))]
    model = [LLMImpact.model_validate(_impact("BZ=F", "up", confidence="medium"))]
    (call,) = merge_impacts(playbook, model)
    assert call.origin == ORIGIN_BOTH and call.rule_id == "oil_supply_shock"
    assert call.confidence == "high"  # both call it first-order: keep the higher
    assert call.horizon == "days"  # only the model states one
    assert call.mechanism == "Playbook reason"  # the rule's wording is kept


def test_agreement_on_a_knock_on_keeps_the_lower_confidence() -> None:
    playbook = [("oil_supply_shock", _rule_impact("ONGC.NS", "up", order="second"))]
    model = [LLMImpact.model_validate(_impact("ONGC.NS", "up", confidence="high"))]
    (call,) = merge_impacts(playbook, model)
    assert call.confidence == "medium"  # not both first-order, so the lower of the two


def test_opposite_calls_are_both_kept_and_marked_as_a_conflict() -> None:
    playbook = [("oil_supply_shock", _rule_impact("BZ=F", "up"))]
    model = [LLMImpact.model_validate(_impact("BZ=F", "down"))]
    calls = merge_impacts(playbook, model)
    assert len(calls) == 2 and all(call.conflict for call in calls)
    assert {call.origin for call in calls} == {ORIGIN_PLAYBOOK, ORIGIN_LLM}


def test_a_disputed_rule_keeps_its_impacts_at_low_confidence() -> None:
    playbook = [
        ("oil_supply_shock", _rule_impact("BZ=F", "up", confidence="high")),
        ("geopolitical_risk_off", _rule_impact("GC=F", "up", confidence="medium")),
    ]
    calls = merge_impacts(playbook, [], disagreed_rules={"oil_supply_shock"})
    by_rule = {call.rule_id: call for call in calls}
    assert by_rule["oil_supply_shock"].confidence == "low"
    assert by_rule["oil_supply_shock"].origin == ORIGIN_PLAYBOOK  # the call still stands
    assert by_rule["geopolitical_risk_off"].confidence == "medium"


def test_calls_the_playbook_missed_are_kept_as_llm_only() -> None:
    model = [LLMImpact.model_validate(_impact("SMH", "down", order="second", confidence="low"))]
    (call,) = merge_impacts([], model)
    assert call.origin == ORIGIN_LLM and call.rule_id is None
    assert (call.symbol, call.direction, call.horizon) == ("SMH", "down", "days")


def test_two_rules_agreeing_with_the_model_both_become_both() -> None:
    """One row per rule is kept, so the per-rule track record still works."""
    playbook = [
        ("oil_supply_shock", _rule_impact("INR=X", "up")),
        ("us_tariffs_on_india", _rule_impact("INR=X", "up", order="second", confidence="low")),
    ]
    model = [LLMImpact.model_validate(_impact("INR=X", "up"))]
    calls = merge_impacts(playbook, model)
    assert len(calls) == 2 and {call.origin for call in calls} == {ORIGIN_BOTH}
    assert {call.rule_id for call in calls} == {"oil_supply_shock", "us_tariffs_on_india"}
