"""Local HTTP API for the browser extension (Grammarly-style checking).

All checking runs on this machine via Tier 1 code detectors plus the local
gate: no cloud calls, no network. Endpoints:

- ``POST /api/ext/scan``: spans with class, label and placeholder suggestion.
- ``POST /api/ext/redact``: gate decision plus safe (placeholder) text; the
  placeholder map stays in server memory under a short id (15 minute TTL).
- ``POST /api/ext/rehydrate``: restore placeholders to real values for
  display only.
- ``GET /api/ext/pairing``: show the pairing token in the local console
  ("Pair browser extension" card). Same localhost page only.

Pairing: callers send the server pairing token in ``X-Ext-Token`` (alias
``X-Pairing-Token``). Only ``chrome-extension://`` / ``moz-extension://``
origins (or no ``Origin``) are accepted. Nothing value-bearing is ever
logged: logs carry ids and counts only.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import engine
from . import tier1
from .redact import base_label, load_mapping, redact, rehydrate, save_mapping

log = logging.getLogger("shadowfenced.ext_api")

router = APIRouter(prefix="/api/ext", tags=["ext"])

_PAIRING_PATH = "/api/ext/pairing"


class ScanBody(BaseModel):
    """Text to scan for sensitive spans."""

    text: str = Field(default="")


class RedactBody(BaseModel):
    """Text to gate before send."""

    text: str = Field(default="")


class RehydrateBody(BaseModel):
    """Placeholder text plus the mapping id from ``redact``."""

    id: str = Field(default="")
    text: str = Field(default="")


def expected_token() -> str:
    """Server pairing token, generated and persisted on first use."""
    from . import config

    return config.load_or_create_ext_token()


def check_origin(request: Request) -> None:
    """Allow only extension origins or no ``Origin`` (loopback console)."""
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return
    lowered = origin.lower()
    if lowered.startswith("chrome-extension://"):
        return
    if lowered.startswith("moz-extension://"):
        return
    log.warning("ext_api: rejected request from unexpected origin")
    raise HTTPException(
        status_code=403,
        detail={
            "code": "shadowfenced_ext_forbidden_origin",
            "type": "shadowfenced_ext_forbidden_origin",
            "message": "extension origin not allowed",
        },
    )


def check_token(request: Request) -> None:
    """Require the pairing-token header (two accepted spellings)."""
    presented = (
        request.headers.get("x-ext-token")
        or request.headers.get("x-pairing-token")
        or ""
    ).strip()
    if presented and secrets.compare_digest(presented, expected_token()):
        return
    log.warning("ext_api: rejected request with missing or wrong token")
    raise HTTPException(
        status_code=401,
        detail={
            "code": "shadowfenced_ext_unauthorized",
            "type": "shadowfenced_ext_unauthorized",
            "message": "pairing token required",
        },
    )


async def ext_guard(request: Request) -> None:
    """Origin check first, then the pairing token.

    ``GET /api/ext/pairing`` skips the token check: it is how the local
    console ("Pair browser extension" card) displays the token on first
    setup. It stays origin-checked above and the service binds loopback.
    """
    check_origin(request)
    if request.method == "GET" and request.url.path == _PAIRING_PATH:
        return
    check_token(request)


def _live_scan(text: str) -> list:
    """Tier 1 scan reusing the app's live policy and canary registry."""
    from .app import get_policy, get_registry

    return tier1.scan(text, get_policy(), get_registry())


def _live_gate(text: str) -> Any:
    """Local gate (Tier 1 + code re-id scoring; never a cloud model)."""
    from .app import get_policy, get_registry

    return engine.gate_reid(text, get_policy(), get_registry(), mode="tier1")


def _refuse_hosted_pairing() -> None:
    try:
        from . import hosted as hosted_module

        if hosted_module.is_hosted():
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "shadowfenced_hosted_pairing_disabled",
                    "type": "shadowfenced_hosted_pairing_disabled",
                    "message": "pairing is disabled in the public demo",
                },
            )
    except HTTPException:
        raise
    except Exception:
        pass


@router.get("/pairing")
def pairing_status(_guard: None = Depends(ext_guard)) -> dict[str, Any]:
    """Return the pairing token for the local console setup card."""
    _refuse_hosted_pairing()
    return {"token": expected_token()}


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "testclient"}


def check_local(request: Request) -> None:
    _refuse_hosted_pairing()
    """Allow pairing rotation only from this machine (loopback)."""
    host = request.client.host if request.client else ""
    if host in _LOOPBACK_HOSTS:
        return
    log.warning("ext_api: rejected pairing rotation from non-loopback host")
    raise HTTPException(
        status_code=403,
        detail={
            "code": "shadowfenced_ext_local_only",
            "type": "shadowfenced_ext_local_only",
            "message": "pairing rotation is allowed only on this machine",
        },
    )


@router.post("/pairing/regenerate")
def pairing_regenerate(
    request: Request, _guard: None = Depends(ext_guard)
) -> dict[str, Any]:
    """Create a fresh pairing token, invalidating the previous one.

    Local-only (loopback) and requires the current token header, like
    every other ``POST`` here. The new token value is never logged.
    """
    check_local(request)
    from . import config

    return {"token": config.rotate_ext_token()}


@router.post("/scan")
def ext_scan(body: ScanBody, _guard: None = Depends(ext_guard)) -> dict[str, Any]:
    """Scan text locally; suggest one placeholder per span (no state)."""
    text = body.text or ""
    spans = _live_scan(text)
    counters: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for span in spans:
        label = base_label(span.cls, span.subclass)
        counters[label] = counters.get(label, 0) + 1
        out.append(
            {
                "start": span.start,
                "end": span.end,
                "class": span.cls,
                "label": label,
                "subclass": span.subclass,
                "placeholder": f"[{label}_{counters[label]}]",
            }
        )
    log.info("ext_api scan: spans=%d chars=%d", len(out), len(text))
    return {"spans": out, "count": len(out)}


@router.post("/redact")
def ext_redact(body: RedactBody, _guard: None = Depends(ext_guard)) -> dict[str, Any]:
    """Gate text: decision plus safe placeholder text and mapping id."""
    text = body.text or ""
    decision = _live_gate(text)
    notes = engine.user_message(decision)
    log.info(
        "ext_api redact: decision=%s spans=%d placeholders=%d",
        decision.decision,
        len(decision.spans),
        len(decision.placeholders),
    )
    if decision.decision == "BLOCK":
        return {"decision": "BLOCK", "safe_text": "", "notes": notes}
    if decision.decision == "ALLOW":
        safe = decision.sent_text if decision.sent_text is not None else text
        return {"decision": "ALLOW", "safe_text": safe, "notes": notes}
    if decision.sent_text is not None and decision.placeholders:
        # REDACT, or WARN layered over a redaction: the map is already
        # stored server-side under this decision id by the gate.
        return {
            "decision": decision.decision,
            "safe_text": decision.sent_text,
            "notes": notes,
            "id": decision.decision_id,
        }
    # HOLD without a redacted send: still offer the proven-span redaction
    # so the extension can show safe text; the map lives 15 minutes.
    proven = [s for s in decision.spans if s.status == "proven"]
    if proven:
        safe_text, mapping = redact(text, proven)
        mapping_id = secrets.token_urlsafe(12)
        save_mapping(mapping_id, mapping)
        log.info(
            "ext_api redact: hold-with-redaction placeholders=%d", len(mapping)
        )
        return {
            "decision": decision.decision,
            "safe_text": safe_text,
            "notes": notes,
            "id": mapping_id,
        }
    return {"decision": decision.decision, "safe_text": text, "notes": notes}


@router.post("/rehydrate")
def ext_rehydrate(
    body: RehydrateBody, _guard: None = Depends(ext_guard)
) -> dict[str, Any]:
    """Restore placeholders to real values for display only."""
    mapping = load_mapping((body.id or "").strip())
    if not mapping:
        log.info("ext_api rehydrate: unknown or expired id")
        raise HTTPException(
            status_code=404,
            detail={
                "code": "shadowfenced_ext_unknown_id",
                "type": "shadowfenced_ext_unknown_id",
                "message": "unknown or expired id",
            },
        )
    restored, _highlights, _unmatched = rehydrate(body.text or "", mapping)
    log.info("ext_api rehydrate: placeholders=%d", len(mapping))
    return {"text": restored}


def mount_ext_api(app: Any) -> None:
    """Mount the extension router on ``app`` (one-line wiring)."""
    app.include_router(router)
    # include_router wraps the app lifespan even when the router has none;
    # restore the original so startup behaviour is unchanged.
    try:
        from .app import lifespan
    except Exception:
        return
    if getattr(app.router, "lifespan_context", lifespan) is not lifespan:
        app.router.lifespan_context = lifespan
