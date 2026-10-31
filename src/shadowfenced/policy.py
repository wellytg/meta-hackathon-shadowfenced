"""Org policy file: load and validate ``policy.yaml`` with pydantic."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

SpanClass = Literal["secret", "pii", "business", "beacon"]
GateAction = Literal["block", "redact", "review", "allow"]

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "policy.yaml"


class Codename(BaseModel):
    name: str
    context: Literal["project_or_caps"] = "project_or_caps"


class Allowlist(BaseModel):
    email_domains: list[str] = Field(default_factory=list)
    shapes: list[str] = Field(default_factory=list)


class Policy(BaseModel):
    org: str
    codenames: list[Codename] = Field(default_factory=list)
    clients: list[str] = Field(default_factory=list)
    figure_cues: list[str] = Field(default_factory=list)
    actions: dict[str, GateAction] = Field(default_factory=dict)
    user_can_keep: dict[str, bool] = Field(default_factory=dict)
    allowlist: Allowlist = Field(default_factory=Allowlist)

    @field_validator("actions", "user_can_keep")
    @classmethod
    def _must_cover_classes(cls, value: dict) -> dict:
        missing = {"secret", "pii", "business", "beacon"} - set(value)
        if missing:
            raise ValueError(f"policy must cover classes: {sorted(missing)} missing")
        return value

    def action_for(self, span_cls: SpanClass) -> GateAction:
        """Gate action for one span class."""
        return self.actions[span_cls]

    def keepable(self, span_cls: SpanClass) -> bool:
        """Whether the user may keep (send anyway) a span of one class."""
        return self.user_can_keep[span_cls]

    def email_allowlisted(self, domain: str) -> bool:
        """True when an email domain is on the allowlist."""
        domain = domain.lower()
        return any(
            domain == allowed.lower() or domain.endswith("." + allowed.lower())
            for allowed in self.allowlist.email_domains
        )

    def codename_names(self) -> list[str]:
        """Upper-case codename words."""
        return [item.name.upper() for item in self.codenames]


def load_policy(path: str | Path = _DEFAULT_PATH) -> Policy:
    """Load and validate the policy file."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"policy file {path} must contain a mapping")
    return Policy.model_validate(raw)
