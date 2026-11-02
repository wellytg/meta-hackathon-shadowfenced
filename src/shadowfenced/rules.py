"""Custom organisation rules (T21): list, add, and remove at runtime.

``GET /api/rules`` returns the read-only base rules from ``policy.yaml``
plus the custom rules from ``policy.local.yaml``. ``POST /api/rules``
validates and stores one custom rule; ``DELETE /api/rules/{id}``
removes one. Writes reset the cached app policy so rules apply
immediately with no restart.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import policy as policy_module
from .policy import base_rules

router = APIRouter()


def _app() -> Any:
    """Late import: the app module includes this router at load time."""
    from . import app as app_module

    return app_module


def _rule_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": code, "type": code, "message": message},
    )


def _custom_view(rule: Any) -> dict[str, Any]:
    return {
        "id": rule.id,
        "kind": rule.kind,
        "value": rule.value,
        "action": rule.action,
        "note": rule.note,
        "source": "custom",
        "readonly": False,
    }


class CreateRuleRequest(BaseModel):
    kind: Literal["codename", "client", "pattern"]
    value: str
    action: Literal["block", "redact", "review"]
    note: str = Field(default="")


@router.get("/api/rules")
def list_rules() -> dict[str, Any]:
    """Base rules (read-only) plus custom rules."""
    policy = _app().get_policy()
    return {
        "base": base_rules(policy),
        "custom": [_custom_view(rule) for rule in policy.custom_rules],
    }


@router.get("/api/rules/catalog")
def get_rule_catalog() -> dict[str, Any]:
    """What Shadowfenced protects: generated from what the code really does."""
    from . import rule_catalog as catalog_module

    policy = _app().get_policy()
    return {
        "entries": catalog_module.get_catalog(policy),
        "footer": catalog_module.FOOTER,
    }


@router.post("/api/rules", status_code=201)
def create_rule(body: CreateRuleRequest) -> dict[str, Any]:
    """Validate, store, and return one custom rule (applies immediately)."""
    try:
        rule = policy_module.add_custom_rule(
            body.kind, body.value, body.action, body.note
        )
    except ValueError as exc:
        raise _rule_error(
            422, "shadowfenced_bad_rule", str(exc)
        ) from exc
    _app().reset_state()
    return _custom_view(rule)


@router.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: str) -> dict[str, Any]:
    """Remove one custom rule by id (applies immediately)."""
    if not policy_module.delete_custom_rule(rule_id):
        raise _rule_error(
            404, "shadowfenced_unknown_rule", f"unknown rule {rule_id}"
        )
    _app().reset_state()
    return {"deleted": rule_id}
