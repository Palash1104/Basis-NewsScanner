"""Pydantic models for every LLM output. Rules JSON Schema can't express are validators here;
a failure triggers one retry that includes the validation error."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Category = Literal[
    "Politics",
    "Geopolitics",
    "Economy & Markets",
    "Business",
    "Tech",
    "Science & Health",
    "Other",
]
SummaryRegion = Literal["US", "India", "Global"]

MAX_HEADLINE_WORDS = 12

# Abbreviations whose trailing period does not end a sentence.
_ABBREVIATION = re.compile(
    r"\b(?:[A-Z]\.){2,}"  # U.S., U.K., E.U.
    r"|\b(?:Mr|Mrs|Ms|Dr|Prof|St|Gen|Sen|Rep|Gov|Lt|Col|Capt|Sgt|Jr|Sr|Inc|Ltd|Co|Corp|No|vs|approx|est)\."
    # Indian business writing: "Govt. Nominee Director", "Rs. 705.65", "Pvt. Ltd."
    r"|\b(?:Govt|Pvt|Dept|Rs)\."
    r"|\b\d+\.\d+"  # decimals: 3.5%
)
_SENTENCE_END = re.compile(r"[.!?]+[\"'”’)\]]*(?=\s|$)")


def count_sentences(text: str) -> int:
    masked = _ABBREVIATION.sub(lambda m: m.group(0).replace(".", "_"), text.strip())
    return len(_SENTENCE_END.findall(masked)) or (1 if masked else 0)


def count_words(text: str) -> int:
    return len([word for word in text.split() if any(ch.isalnum() for ch in word)])


class StorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(description="A neutral headline of at most 12 words.")
    summary: str = Field(
        description="2-3 short sentences in plain, simple words. Sentence 1: what happened. "
        "Then: why it matters."
    )
    category: Category
    regions: list[SummaryRegion] = Field(
        description="US or India only if the event happens there or directly involves that "
        "country's government, economy, companies or people; Global only with clear "
        "international consequences. Never based on where the outlet is. May be empty."
    )
    sources_disagree: bool = Field(description="True if the articles disagree on key facts.")
    disagreement_note: str | None = Field(
        description="If sources_disagree is true: one sentence describing the disagreement. "
        "Otherwise null."
    )

    @field_validator("headline", "summary")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("headline")
    @classmethod
    def _headline_length(cls, value: str) -> str:
        words = count_words(value)
        if words > MAX_HEADLINE_WORDS:
            raise ValueError(f"headline has {words} words; the maximum is {MAX_HEADLINE_WORDS}")
        return value

    @field_validator("summary")
    @classmethod
    def _summary_sentences(cls, value: str) -> str:
        sentences = count_sentences(value)
        if not 2 <= sentences <= 3:
            raise ValueError(f"summary has {sentences} sentences; it must have 2 or 3")
        return value

    @field_validator("regions")
    @classmethod
    def _regions(cls, value: list[str]) -> list[str]:
        # Empty is allowed: an event elsewhere without clear international consequences.
        return list(dict.fromkeys(value))  # de-duplicate, keep order

    @model_validator(mode="after")
    def _disagreement(self) -> "StorySummary":
        if not self.sources_disagree:
            self.disagreement_note = None
        elif not (self.disagreement_note or "").strip():
            raise ValueError("sources_disagree is true, so disagreement_note must describe it")
        return self


# ---------------------------------------------------------------- event extraction (SPEC 7.6)

EventType = Literal[
    "geopolitical_conflict",
    "sanctions_trade_policy",
    "central_bank_monetary",
    "fiscal_policy_budget",
    "macro_data_release",
    "election_political_change",
    "regulation_sector",
    "corporate_earnings_guidance",
    "corporate_deal",
    "commodity_supply_disruption",
    "weather_climate_agriculture",
    "natural_disaster",
    "public_health",
    "technology_ai",
    "legal_court_ruling",
    "other",
]
Channel = Literal[
    "oil_supply",
    "natural_gas_supply",
    "shipping_routes",
    "safe_haven_demand",
    "risk_sentiment",
    "us_interest_rates",
    "india_interest_rates",
    "inflation",
    "usd_strength",
    "inr_exchange_rate",
    "tariffs_trade",
    "defense_spending",
    "tech_regulation",
    "immigration_visas",
    "semiconductor_supply",
    "agriculture_supply",
    "metals_demand",
    "fiscal_spending",
    "banking_credit",
    "sector_specific",
    "company_specific",
    "none",
]
# One field, with a precedence rule in the prompt: easing developments are de_escalation and
# worsening ones escalation, whatever their size; minor/moderate/major only when neither.
Severity = Literal["minor", "moderate", "major", "escalation", "de_escalation"]
PolicyStance = Literal["hawkish", "dovish", "neutral", "not_applicable"]


def _clean_names(values: list[str]) -> list[str]:
    """Collapse whitespace, drop blanks and duplicates (case-insensitive), keep order."""
    seen: set[str] = set()
    cleaned = []
    for value in values:
        text = " ".join(value.split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            cleaned.append(text)
    return cleaned


# What happened, in fixed categories the playbook can match. The story's regions come from its
# summary, so they aren't asked for again. (A comment, not a docstring: pydantic would send a
# docstring to the model as the schema description.)
class EventExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: EventType
    countries: list[str] = Field(
        description="Countries directly involved, as standard English short names "
        "(e.g. United States, United Kingdom, China, India)."
    )
    entities: list[str] = Field(
        description="Organizations, places and people central to the event, e.g. "
        "Federal Reserve, RBI, OPEC, Strait of Hormuz."
    )
    companies: list[str] = Field(description="Companies directly named in the articles.")
    channels: list[Channel] = Field(
        description="How this event could reach financial markets. Only channels the articles "
        'give a concrete link for; ["none"] if there is no plausible market channel.'
    )
    severity: Severity
    policy_stance: PolicyStance = Field(
        description="For central bank or monetary-policy news only; otherwise not_applicable."
    )
    policy_actor: str | None = Field(
        default=None,
        description="The authority whose stance policy_stance describes, e.g. Federal Reserve, "
        "RBI, Bank of England. Null when policy_stance is not_applicable.",
    )
    is_new_development: bool = Field(
        description="False for opinion, analysis, explainers, or rehashes of older news."
    )

    @field_validator("countries", "entities", "companies")
    @classmethod
    def _names(cls, value: list[str]) -> list[str]:
        return _clean_names(value)

    @field_validator("policy_actor")
    @classmethod
    def _actor(cls, value: str | None) -> str | None:
        text = " ".join((value or "").split())
        return text or None

    @field_validator("channels")
    @classmethod
    def _channels(cls, value: list[str]) -> list[str]:
        # "none" mixed with real channels is fixed (and logged) by extract_event, not rejected.
        return list(dict.fromkeys(value))


# ---------------------------------------------------------------- LLM impacts (SPEC 7.7 B)

Direction = Literal["up", "down"]
Order = Literal["first", "second"]
Confidence = Literal["high", "medium", "low"]
Horizon = Literal["intraday", "days", "weeks"]


class LLMImpact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(description="A symbol from ALLOWED ASSETS. Never invent one.")
    direction: Direction
    mechanism: str = Field(description="One sentence stating the cause and effect chain.")
    order: Order = Field(
        description="first: directly exposed. second: exposed through a knock-on effect."
    )
    confidence: Confidence
    horizon: Horizon

    @field_validator("symbol", "mechanism")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("must not be empty")
        return value


class RuleDisagreement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    reason: str = Field(description="Why this rule doesn't fit this event, in one sentence.")


# The model's view of a story's market impact. Symbols are checked against the universe
# afterwards (invalid ones are dropped and logged), and the second-order confidence cap is
# enforced in code as well as asked for in the prompt.
class LLMImpacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    no_clear_impact: bool = Field(
        description="True when no asset in the list would plausibly move because of this."
    )
    impacts: list[LLMImpact]
    rule_disagreements: list[RuleDisagreement]

    @model_validator(mode="after")
    def _empty_when_no_impact(self) -> "LLMImpacts":
        if self.no_clear_impact and self.impacts:
            raise ValueError("no_clear_impact is true, so impacts must be empty")
        return self


# ---------------------------------------------------------------- rerank (SPEC 7.4, Phase 5)


# The reasoning model's ordering of candidate stories, most significant first. Ids are checked
# against the candidates afterwards: unknown ids are dropped, missing ones keep their place.
class StoryRanking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    story_ids: list[int] = Field(
        description="Every candidate story id, most significant first. Include them all."
    )


# ---------------------------------------------------------------- the watchlist call


WatchRelevance = Literal["primary", "secondary", "passing"]
WatchSentiment = Literal["positive", "negative", "neutral"]
WatchMateriality = Literal["high", "medium", "low"]
# The event types the user listed (2026-10-07).
WatchEventType = Literal[
    "results",
    "order_win",
    "regulatory",
    "management",
    "rating_change",
    "deal",
    "legal",
    "other",
]
MAX_REASON_WORDS = 30


class StockAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(description="The company's symbol exactly as given in <companies>.")
    relevance: WatchRelevance = Field(
        description="primary: the story is about this company. secondary: the company is "
        "materially involved but not the main subject. passing: only named in passing or in "
        "a list, or the story is about something else that shares the name."
    )
    sentiment: WatchSentiment = Field(
        description="What this news means for this company's share price. neutral is a "
        "normal answer."
    )
    materiality: WatchMateriality = Field(
        description="high: likely to move the share price noticeably. low: routine. low is "
        "a normal answer."
    )
    event_type: WatchEventType
    reason: str = Field(
        description="One short sentence, from the articles only, saying why: the fact that "
        "decides the sentiment and materiality."
    )

    @field_validator("reason")
    @classmethod
    def _one_sentence(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason is empty")
        if count_sentences(value) > 1:
            raise ValueError("reason must be one sentence")
        if count_words(value) > MAX_REASON_WORDS:
            raise ValueError(f"reason must be at most {MAX_REASON_WORDS} words")
        return value

    @model_validator(mode="after")
    def _passing_is_never_high(self) -> "StockAssessment":
        if self.relevance == "passing" and self.materiality == "high":
            raise ValueError("a passing mention can't be high materiality")
        return self


class WatchAnalysis(BaseModel):
    """One story, assessed for every watchlist company it names."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description="2-3 short sentences in plain words. Sentence 1: what happened. Then: "
        "why it matters for the company."
    )
    stocks: list[StockAssessment] = Field(
        description="One entry for every company in <companies>, in the same order."
    )

    @field_validator("summary")
    @classmethod
    def _short_summary(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= count_sentences(value) <= 3:
            raise ValueError("summary must be 1 to 3 sentences")
        return value
