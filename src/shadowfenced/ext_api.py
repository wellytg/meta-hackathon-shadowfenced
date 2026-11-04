"""Local HTTP API for the browser extension (Grammarly-style checking).

All checking runs on this machine via Tier 1 code detectors plus the local
gate: no cloud calls, no network. Endpoints:

- ``POST /api/ext/scan``: spans with class, label and placeholder suggestion.
- ``POST /api/ext/redact``: gate decision plus safe (placeholder) text; the
  placeholder map stays in server memory under a short id (15 minute TTL).
- ``POST /api/ext/rehydrate``: restore placeholders to real values for
  display only.
- ``POST /api/ext/choice``: record one card choice (fields only) in the
  chained decision log.
- ``GET /api/ext/pairing``: show the pairing token in the local console
  ("Pair browser extension" card). Same localhost page only.
- ``POST /api/ext/scan-file``: guard one attached file (multipart, 25 MB
  max); answers CLEAN | BLOCK | UNSCANNABLE plus label/count kinds.
- ``POST /api/ext/redact-file``: a redacted copy of a blocked scan.
- ``POST /api/ext/deep-check`` + ``GET /api/ext/deep-check/<id>``:
  optional Glimmer deep check over a scan's text (adds findings only).

Pairing: callers send the server pairing token in ``X-Ext-Token`` (alias
``X-Pairing-Token``). Only ``chrome-extension://`` / ``moz-extension://``
origins (or no ``Origin``) are accepted. Nothing value-bearing is ever
logged: logs carry ids and counts only.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from . import engine
from . import fileguard
from . import tier1
from .redact import base_label, load_mapping, redact, rehydrate, save_mapping

log = logging.getLogger("shadowfenced.ext_api")

router = APIRouter(prefix="/api/ext", tags=["ext"])

_PAIRING_PATH = "/api/ext/pairing"


class ScanBody(BaseModel):
    """Text to scan for sensitive spans."""

    text: str = Field(default="")


class ChoiceBody(BaseModel):
    """One extension card choice (fields only; never values)."""

    decision_id: str = Field(default="")
    choice: str = Field(default="")
    kinds: list[str] = Field(default_factory=list)
    rule_ids: list[str] | None = None
    site: str = Field(default="")
    source: str = Field(default="text")


class RedactBody(BaseModel):
    """Text to gate before send."""

    site: str = Field(default="")
    text: str = Field(default="")


class ScanIdBody(BaseModel):
    """A file-guard scan id from ``scan-file``."""

    scan_id: str = Field(default="")


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


@router.get("/health")
def ext_health(_guard: None = Depends(ext_guard)) -> dict[str, Any]:
    """Liveness probe for the extension popup (origin + token guarded)."""
    return {"ok": True, "paired": True}


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
    from . import ecs_log as _ecs_log

    site = _ecs_log.clean_site(getattr(body, "site", "") or "")
    kinds = [kind for kind in (_ecs_log.span_kind(s) for s in decision.spans) if kind]
    rule_ids = list(decision.rule_ids or [])
    try:
        from .app import get_policy as _get_policy

        from . import suggest as _suggest

        auto = decision.decision == "WARN" and _suggest.auto_redact_covers(
            _get_policy(), kinds, site, rule_ids
        )
    except Exception:
        auto = False
    try:
        from .config import get_settings as _get_settings

        if auto:
            decision.evidence["auto_rule"] = "auto_redact"
        _ecs_log.append_decision(
            _get_settings().log_path, decision, text, site=site, source="text"
        )
        _ecs_log.clear_last_write_error()
    except Exception as exc:
        try:
            _ecs_log.note_write_failure(exc, _ecs_log.resolve_path(None))
        except Exception:
            pass
    if auto:
        proven = [s for s in decision.spans if s.status == "proven"]
        safe_text, mapping = redact(text, proven) if proven else (text, {})
        mapping_id = decision.decision_id
        if mapping:
            save_mapping(mapping_id, mapping)
        log.info("ext_api redact: auto rule hid kinds without asking")
        return {
            "decision": "REDACT",
            "safe_text": safe_text,
            "notes": notes + " Hidden automatically per your rule.",
            "id": mapping_id,
            "decision_id": decision.decision_id,
            "kinds": kinds,
            "rule_ids": rule_ids,
        }
    log.info(
        "ext_api redact: decision=%s spans=%d placeholders=%d",
        decision.decision,
        len(decision.spans),
        len(decision.placeholders),
    )
    if decision.decision == "BLOCK":
        _reid = (decision.evidence or {}).get("reid", {}) or {}
        _payload: dict[str, Any] = {"decision": "BLOCK", "safe_text": "", "notes": notes}
        _payload.update(
            {
                "decision_id": decision.decision_id,
                "kinds": kinds,
                "rule_ids": rule_ids,
            }
        )
        if isinstance(_reid, dict) and _reid.get("outcome") == "BLOCK":
            _payload["combination"] = list(_reid.get("combination") or [])
            _payload["generalised_text"] = str(_reid.get("generalised_text") or "")
        return _payload
    if decision.decision == "ALLOW":
        safe = decision.sent_text if decision.sent_text is not None else text
        return {
            "decision": "ALLOW",
            "safe_text": safe,
            "notes": notes,
            "decision_id": decision.decision_id,
            "kinds": kinds,
            "rule_ids": rule_ids,
        }
    if decision.sent_text is not None and decision.placeholders:
        # REDACT, or WARN layered over a redaction: the map is already
        # stored server-side under this decision id by the gate.
        return {
            "decision": decision.decision,
            "safe_text": decision.sent_text,
            "notes": notes,
            "id": decision.decision_id,
            "decision_id": decision.decision_id,
            "kinds": kinds,
            "rule_ids": rule_ids,
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
            "decision_id": decision.decision_id,
            "kinds": kinds,
            "rule_ids": rule_ids,
        }
    return {
        "decision": decision.decision,
        "safe_text": text,
        "notes": notes,
        "decision_id": decision.decision_id,
        "kinds": kinds,
        "rule_ids": rule_ids,
    }


@router.post("/choice")
def ext_choice(body: ChoiceBody, _guard: None = Depends(ext_guard)) -> dict[str, Any]:
    """Record one extension card choice (fields only; token guarded)."""
    from . import ecs_log
    from . import rule_catalog as catalog_module
    from .config import get_settings

    try:
        kinds = ecs_log.clean_kinds(body.kinds)
        decision_id = ecs_log.clean_decision_id(body.decision_id)
        choice = ecs_log.clean_choice(body.choice)
        site = ecs_log.clean_site(body.site)
        source = ecs_log.clean_source(body.source)
        rule_ids = (
            ecs_log.clean_rule_ids(body.rule_ids)
            if body.rule_ids is not None
            else catalog_module.rule_ids_for_kinds(kinds)
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "shadowfenced_bad_choice",
                "type": "shadowfenced_bad_choice",
                "message": str(exc),
            },
        ) from exc
    try:
        record = ecs_log.append_choice(
            get_settings().log_path,
            decision_id=decision_id,
            choice=choice,
            kinds=kinds,
            rule_ids=rule_ids,
            site=site,
            source=source,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "shadowfenced_bad_choice",
                "type": "shadowfenced_bad_choice",
                "message": str(exc),
            },
        ) from exc
    except Exception as exc:
        try:
            ecs_log.note_write_failure(
                exc, ecs_log.resolve_path(get_settings().log_path)
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=503,
            detail={
                "code": "shadowfenced_log_unwritable",
                "type": "shadowfenced_log_unwritable",
                "message": "decision log not writable",
            },
        ) from exc
    log.info("ext_api choice: choice=%s site=%s", choice, site)
    return {"ok": True, "ts": record["ts"]}


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


@router.post("/scan-file")
async def ext_scan_file(
    file: UploadFile = File(...),
    _guard: None = Depends(ext_guard),
) -> dict[str, Any]:
    """Guard one attached file: CLEAN | BLOCK | UNSCANNABLE (25 MB max).

    Origin + token guarded like the other ext routes, and off in hosted
    mode. Oversized, unsupported, encrypted or unparseable input answers
    UNSCANNABLE (the file stays blocked), never an HTTP error. Labels
    and counts only — raw values never leave this machine.
    """
    _refuse_hosted_pairing()
    from .app import get_policy, get_registry

    # Sync read of the spooled upload: no await, so tests drive this
    # route without an event loop (socket creation is forbidden there).
    # The size is proven before the body is trusted (T81, mirrors
    # app._read_upload_capped): a declared size past the cap answers
    # without reading at all, otherwise one bounded read (cap + 1).
    declared = getattr(file, "size", None)
    if isinstance(declared, int) and declared >= 0 and declared > fileguard.EXT_SCAN_MAX_BYTES:
        data = bytes(fileguard.EXT_SCAN_MAX_BYTES + 1)
    else:
        data = bytes(file.file.read(fileguard.EXT_SCAN_MAX_BYTES + 1) or b"")
    payload = fileguard.scan_upload(
        file.filename or "upload",
        bytes(data or b""),
        get_policy(),
        get_registry(),
    )
    log.info(
        "ext_api scan-file: decision=%s kinds=%d chars=%d code_ms=%.1f",
        payload.get("decision"),
        len(payload.get("kinds") or []),
        payload.get("chars", 0),
        payload.get("code_ms", 0.0),
    )
    return payload


@router.post("/redact-file")
def ext_redact_file(
    body: ScanIdBody, _guard: None = Depends(ext_guard)
) -> dict[str, Any]:
    """Return a redacted copy of one BLOCK scan (base64 JSON).

    DOCX/PDF become ``<stem>.redacted.txt`` (``note`` says the layout
    is not kept); text-likes keep their format; images return the
    stored box-filled PNG. The copy always scans CLEAN. 404 for an
    unknown scan id, 409 when there is nothing to redact.
    """
    _refuse_hosted_pairing()
    try:
        result = fileguard.build_redacted_copy((body.scan_id or "").strip())
    except fileguard.UnknownScan as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "shadowfenced_ext_unknown_scan",
                "type": "shadowfenced_ext_unknown_scan",
                "message": str(exc),
            },
        ) from exc
    except fileguard.RedactRefused as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "shadowfenced_ext_no_redaction",
                "type": "shadowfenced_ext_no_redaction",
                "message": str(exc),
                "decision": exc.decision,
            },
        ) from exc
    log.info("ext_api redact-file: chars=%d", result.get("chars", 0))
    return result


@router.post("/deep-check")
def ext_deep_check_start(
    body: ScanIdBody, _guard: None = Depends(ext_guard)
) -> dict[str, Any]:
    """Start an optional Glimmer deep check over one scan's text.

    Runs in the background (poll the status route); adds findings
    only and can never clear a code finding. 404 for an unknown scan
    id, 409 when the scan holds no readable text.
    """
    _refuse_hosted_pairing()
    try:
        return fileguard.start_deep_check((body.scan_id or "").strip())
    except fileguard.UnknownScan as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "shadowfenced_ext_unknown_scan",
                "type": "shadowfenced_ext_unknown_scan",
                "message": str(exc),
            },
        ) from exc
    except fileguard.FileguardError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "shadowfenced_ext_no_deep_check",
                "type": "shadowfenced_ext_no_deep_check",
                "message": str(exc),
                "decision": exc.decision,
            },
        ) from exc


@router.get("/deep-check/{check_id}")
def ext_deep_check_status(
    check_id: str, _guard: None = Depends(ext_guard)
) -> dict[str, Any]:
    """Poll one deep-check job (status, progress, extra findings)."""
    _refuse_hosted_pairing()
    payload = fileguard.get_deep_check((check_id or "").strip())
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "shadowfenced_ext_unknown_check",
                "type": "shadowfenced_ext_unknown_check",
                "message": "unknown or expired check id",
            },
        )
    return payload


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
