#!/usr/bin/env python3
"""Schémas Pydantic pour les réponses Claude."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class SourceRef(BaseModel):
    type: Literal["doc_chunk", "similar_qa", "web_search", "knowledge"]
    ref: str = Field(min_length=1)


class AnswerSuggestion(BaseModel):
    reponse: str = Field(min_length=1)
    justification: str = Field(min_length=1)
    confiance: Literal["haute", "moyenne", "basse"]
    sources: list[SourceRef] = Field(default_factory=list)
    alerte_version: bool = False
    divergence_versions: Optional[dict[str, str]] = None

    @field_validator("reponse", "justification", mode="before")
    @classmethod
    def strip_strings(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("divergence_versions", mode="before")
    @classmethod
    def normalize_divergence(cls, v: object) -> object:
        if v is None or v == "":
            return None
        if not isinstance(v, dict):
            return v
        out: dict[str, str] = {}
        for k, val in v.items():
            key = str(k).strip()
            if key in ("18.0", "19.0") and val is not None:
                out[key] = str(val).strip()
        return out or None

    @model_validator(mode="after")
    def divergence_only_with_alert(self) -> AnswerSuggestion:
        if not self.alerte_version and self.divergence_versions:
            return self.model_copy(update={"divergence_versions": None})
        return self


class VersionClassification(BaseModel):
    target_version: Literal["18.0", "19.0", "both"]
    confiance: Literal["haute", "moyenne", "basse"]
    raisonnement: str = Field(min_length=1)

    @field_validator("raisonnement", mode="before")
    @classmethod
    def strip_raisonnement(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v
