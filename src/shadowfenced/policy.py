"""Org policy file: load and validate ``policy.yaml`` with pydantic."""

from __future__ import annotations

import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .hosted import MAX_HOSTED_RULES, is_hosted

SpanClass = Literal["secret", "pii", "business", "beacon"]
GateAction = Literal["block", "redact", "review", "allow"]

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "policy.yaml"
_LOCAL_DEFAULT = Path(__file__).resolve().parents[2] / "policy.local.yaml"
_LOCAL_ENV_VAR = "SF_LOCAL_POLICY"
_POLICY_ENV_VAR = "SF_POLICY_PATH"

# Hosted demo (SF_HOSTED=1) keeps custom rules in process memory only:
# they are never written to disk and are capped at MAX_HOSTED_RULES.
_HOSTED_RULES: list[CustomRule] = []
_HOSTED_LOCK = threading.Lock()


def reset_hosted_rules() -> None:
    """Drop every in-memory hosted custom rule (tests only)."""
    with _HOSTED_LOCK:
        _HOSTED_RULES.clear()


RuleKind = Literal["codename", "client", "pattern"]
RuleAction = Literal["block", "redact", "review"]

# A group whose body already holds a quantifier (``+ * ? {n}``), closed and
# followed by another quantifier: ``(a+)+``, ``(x*)*``, ``(a{2,3})+``.
# Such nested quantifiers risk catastrophic backtracking, so custom
# patterns holding them are rejected.
_NESTED_QUANT_RE = re.compile(
    r"\([^()]*(\+|\*|\?|\{\s*\d)[^()]*\)\s*(\*|\+|\?|\{)"
)


def validate_custom_rule_value(kind: str, value: str) -> str:
    """Check one custom rule value; return it stripped, else raise ValueError."""
    text = (value or "").strip()
    if kind in ("codename", "client"):
        if not 2 <= len(text) <= 64:
            raise ValueError(
                f"{kind} rule value must be 2 to 64 characters "
                f"(got {len(text)})"
            )
        return text
    if kind == "pattern":
        if not 1 <= len(value) <= 200:
            raise ValueError(
                "pattern rule value must be 1 to 200 characters "
                f"(got {len(value)})"
            )
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"pattern does not compile: {exc}") from exc
        if _NESTED_QUANT_RE.search(value):
            raise ValueError(
                "pattern holds nested quantifiers such as (a+)+ and is "
                "rejected to avoid catastrophic backtracking"
            )
        return value
    raise ValueError(f"unknown rule kind {kind!r}")


class CustomRule(BaseModel):
    """One runtime custom rule, stored in ``policy.local.yaml``."""

    id: str = ""
    kind: RuleKind = "codename"
    value: str = ""
    action: RuleAction = "redact"
    note: str = ""

    @model_validator(mode="after")
    def _check_value(self) -> CustomRule:
        validate_custom_rule_value(self.kind, self.value)
        if self.action not in ("block", "redact", "review"):
            raise ValueError(f"unknown rule action {self.action!r}")
        return self


# Suggestion-driven local sections (T69): auto_redact, cue_allow,
# block_kinds, org_domains. Hard limits are enforced here as well as in
# the suggester: auto_redact takes PII kinds only, block_kinds never
# takes secrets, and cue_allow never takes secret-ish cues.

#: Cue words that must never skip the model check: they point at
#: secrets, keys, or credentials. Mirrors the secret-ish alternatives
#: of ``engine._SCREEN_CUE_RE`` (a test keeps them in sync).
SECRET_CUES = frozenset({
    "password", "passwd", "creds", "credential", "credentials",
    "secret", "token", "key", "keys", "keystore", "vault",
    "ssn", "social security",
})

_KIND_RE = re.compile(r"[a-z0-9_]{1,32}:[a-z0-9_]{1,48}")
_DOMAIN_RE = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}"
)


def validate_local_kind(kind: str, *, classes: set[str]) -> str:
    """Check one ``cls:subclass`` kind for a habit section."""
    text = (kind or "").strip().lower()
    if not _KIND_RE.fullmatch(text):
        raise ValueError(f"bad kind {kind!r}: want cls:subclass")
    cls = text.split(":", 1)[0]
    if cls not in classes:
        raise ValueError(f"kind {text!r} is not allowed here")
    return text


def validate_org_domain(domain: str) -> str:
    """Check one organisation email domain (lowercased, no values)."""
    text = (domain or "").strip().lower().rstrip(".")
    if not _DOMAIN_RE.fullmatch(text):
        raise ValueError(f"bad domain {domain!r}")
    return text


def validate_cue_allow(cue: str) -> str:
    """Check one cue word allowed to pass without the model check."""
    text = re.sub(r"\s+", " ", (cue or "").strip().lower())
    if not text or len(text) > 64 or not re.fullmatch(r"[a-z ]+", text):
        raise ValueError(f"bad cue {cue!r}")
    if text in SECRET_CUES:
        raise ValueError(
            f"cue {text!r} points at secrets and cannot skip review"
        )
    return text


class AutoRedactEntry(BaseModel):
    """One auto-redact rule: hide KIND and send without asking."""

    kind: str = ""
    site: str = ""

    @model_validator(mode="after")
    def _check(self) -> AutoRedactEntry:
        self.kind = validate_local_kind(self.kind, classes={"pii"})
        site = (self.site or "").strip().lower()
        if site and site != "app" and not _DOMAIN_RE.fullmatch(site):
            raise ValueError(f"bad site {self.site!r}")
        self.site = site
        return self


class LocalPolicy(BaseModel):
    """Validated ``policy.local.yaml``: custom rules + habit sections."""

    custom_rules: list[CustomRule] = Field(default_factory=list)
    auto_redact: list[AutoRedactEntry] = Field(default_factory=list)
    cue_allow: list[str] = Field(default_factory=list)
    block_kinds: list[str] = Field(default_factory=list)
    org_domains: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> LocalPolicy:
        self.cue_allow = [validate_cue_allow(c) for c in (self.cue_allow or [])]
        self.block_kinds = [
            validate_local_kind(k, classes={"pii", "business"})
            for k in (self.block_kinds or [])
        ]
        self.org_domains = [
            validate_org_domain(d) for d in (self.org_domains or [])
        ]
        return self


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
    custom_rules: list[CustomRule] = Field(default_factory=list)
    auto_redact: list[AutoRedactEntry] = Field(default_factory=list)
    cue_allow: list[str] = Field(default_factory=list)
    block_kinds: list[str] = Field(default_factory=list)
    org_domains: list[str] = Field(default_factory=list)

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
            for allowed in [*self.allowlist.email_domains, *self.org_domains]
        )

    def codename_names(self) -> list[str]:
        """Upper-case codename words."""
        return [item.name.upper() for item in self.codenames]


def local_policy_path() -> Path:
    """Local custom-rule file: ``SF_LOCAL_POLICY`` wins, else the repo root."""
    override = os.environ.get(_LOCAL_ENV_VAR, "").strip()
    return Path(override) if override else _LOCAL_DEFAULT


#: Local-file sections in canonical order (custom rules first).
LOCAL_SECTIONS = (
    "custom_rules", "auto_redact", "cue_allow", "block_kinds", "org_domains",
)


def load_local_document(path: str | Path | None = None) -> dict[str, Any]:
    """Raw mapping from the local file ({} when missing)."""
    if is_hosted():
        # Hosted demo: rules live in memory only, never on disk.
        with _HOSTED_LOCK:
            return {
                "custom_rules": [rule.model_dump() for rule in _HOSTED_RULES]
            }
    target = Path(path) if path else local_policy_path()
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"local policy file {target} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if isinstance(data, dict):
        return dict(data)
    if isinstance(data, list):
        # Legacy: a bare rule list means custom_rules.
        return {"custom_rules": data}
    raise ValueError(f"local policy file {target} must hold a mapping")


def validate_local_document(doc: dict[str, Any]) -> LocalPolicy:
    """Validate a raw local-file mapping (raises ValueError when bad)."""
    return LocalPolicy.model_validate(dict(doc or {}))


def load_local_policy(path: str | Path | None = None) -> LocalPolicy:
    """Validated local file: custom rules plus habit sections."""
    doc = load_local_document(path)
    items = doc.get("custom_rules", [])
    if not isinstance(items, list):
        raise ValueError("local policy custom_rules must hold a rule list")
    return validate_local_document(doc)


def dump_local_document(doc: dict[str, Any]) -> str:
    """Serialise a raw local-file mapping (canonical section order)."""
    ordered: dict[str, Any] = {}
    for section in LOCAL_SECTIONS:
        if section in doc:
            ordered[section] = doc[section]
    for key, value in dict(doc).items():
        if key not in ordered:
            ordered[key] = value
    return yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True)


def load_custom_rules(path: str | Path | None = None) -> list[CustomRule]:
    """Read custom rules from the local file; missing file means no rules."""
    if is_hosted():
        # Hosted demo: rules live in memory only, never on disk.
        with _HOSTED_LOCK:
            return list(_HOSTED_RULES)
    target = Path(path) if path else local_policy_path()
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"local policy file {target} is not valid YAML: {exc}") from exc
    if data is None:
        return []
    if isinstance(data, dict):
        items = data.get("custom_rules", [])
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError(f"local policy file {target} must hold a mapping")
    if not isinstance(items, list):
        raise ValueError(f"local policy file {target} must hold a rule list")
    return [CustomRule.model_validate(item) for item in items]


def _write_local_document(doc: dict[str, Any], path: Path) -> None:
    """Persist a raw local-file mapping; the file itself stays git-ignored."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_local_document(doc), encoding="utf-8")


def _write_custom_rules(rules: list[CustomRule], path: Path) -> None:
    """Persist custom rules, keeping every other local section."""
    try:
        doc = load_local_document(path)
    except ValueError:
        doc = {}
    doc["custom_rules"] = [rule.model_dump() for rule in rules]
    _write_local_document(doc, path)


def add_custom_rule(kind: str, value: str, action: str, note: str = "") -> CustomRule:
    """Validate, store, and return one custom rule."""
    if kind not in ("codename", "client", "pattern"):
        raise ValueError(f"unknown rule kind {kind!r}")
    if action not in ("block", "redact", "review"):
        raise ValueError(f"unknown rule action {action!r}")
    clean = validate_custom_rule_value(kind, value)
    target = local_policy_path()
    if is_hosted():
        # Hosted demo: in-memory only, capped, never written to disk.
        with _HOSTED_LOCK:
            if len(_HOSTED_RULES) >= MAX_HOSTED_RULES:
                raise ValueError(
                    f"hosted demo keeps at most {MAX_HOSTED_RULES} custom rules"
                )
            taken = {rule.id for rule in _HOSTED_RULES}
            rule_id = uuid.uuid4().hex[:12]
            while rule_id in taken:
                rule_id = uuid.uuid4().hex[:12]
            rule = CustomRule(
                id=rule_id, kind=kind, value=clean, action=action, note=(note or "").strip()  # type: ignore[arg-type]
            )
            _HOSTED_RULES.append(rule)
            return rule
    rules = load_custom_rules(target)
    taken = {rule.id for rule in rules}
    rule_id = uuid.uuid4().hex[:12]
    while rule_id in taken:
        rule_id = uuid.uuid4().hex[:12]
    rule = CustomRule(
        id=rule_id, kind=kind, value=clean, action=action, note=(note or "").strip()  # type: ignore[arg-type]
    )
    rules.append(rule)
    _write_custom_rules(rules, target)
    return rule


def delete_custom_rule(rule_id: str) -> bool:
    """Remove one custom rule; True when it existed."""
    if is_hosted():
        with _HOSTED_LOCK:
            kept = [rule for rule in _HOSTED_RULES if rule.id != rule_id]
            if len(kept) == len(_HOSTED_RULES):
                return False
            _HOSTED_RULES[:] = kept
            return True
    target = local_policy_path()
    rules = load_custom_rules(target)
    kept = [rule for rule in rules if rule.id != rule_id]
    if len(kept) == len(rules):
        return False
    _write_custom_rules(kept, target)
    return True


def base_rules(policy: Policy) -> list[dict[str, object]]:
    """Read-only view of the base rules from ``policy.yaml``."""
    action = policy.actions.get("business", "redact")
    entries: list[dict[str, object]] = []
    for item in policy.codenames:
        entries.append(
            {"kind": "codename", "value": item.name, "action": action,
             "source": "base", "readonly": True}
        )
    for name in policy.clients:
        entries.append(
            {"kind": "client", "value": name, "action": action,
             "source": "base", "readonly": True}
        )
    return entries


def default_policy_path() -> Path:
    """Default policy file: `SF_POLICY_PATH` wins, else the repo root.

    Read at call time so the variable can point at a different file per
    process without reimporting this module.
    """
    override = os.environ.get(_POLICY_ENV_VAR, "").strip()
    return Path(override) if override else _DEFAULT_PATH


def load_policy(path: str | Path | None = None) -> Policy:
    """Load the policy file, merging local rules and habit sections."""
    target = Path(path) if path is not None else default_policy_path()
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"policy file {target} must contain a mapping")
    policy = Policy.model_validate(raw)
    local = load_local_policy()
    policy.custom_rules = list(local.custom_rules)
    policy.auto_redact = list(local.auto_redact)
    policy.cue_allow = list(local.cue_allow)
    policy.block_kinds = list(local.block_kinds)
    policy.org_domains = list(local.org_domains)
    return policy
