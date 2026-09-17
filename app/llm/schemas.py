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
        description="Which of US, India, Global the story concerns (at least one)."
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
        if not value:
            raise ValueError("list at least one region")
        return list(dict.fromkeys(value))  # de-duplicate, keep order

    @model_validator(mode="after")
    def _disagreement(self) -> "StorySummary":
        if not self.sources_disagree:
            self.disagreement_note = None
        elif not (self.disagreement_note or "").strip():
            raise ValueError("sources_disagree is true, so disagreement_note must describe it")
        return self
