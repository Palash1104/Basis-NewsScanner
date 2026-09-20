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
