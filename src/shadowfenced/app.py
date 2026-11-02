"""Shadowfenced proxy: scan, gate, forward, re-hydrate.

Endpoints are plain ``def`` so they run anywhere; the upstream call
goes through the synchronous client while ``upstream.chat`` offers the
same request/response code as an async API. Tests inject an httpx
``MockTransport`` via ``app.state.upstream_transport``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import engine
from . import holds
from . import hosted as hosted_module
from . import keyguard as keyguard_module
from . import providers as providers_registry
from . import judge as judge_module
from .canary import CanaryRegistry
from .config import Settings, get_settings
from .extract import (
    FileTooLarge,
    Unscannable,
    UnsupportedType,
    extract_text,
    is_audio_file,
)
from .policy import Policy, load_policy
from .redact import load_mapping, rehydrate, save_mapping
from .spans import Span, cp_to_utf16, make_span_id, span_from_cp_range
from .transcribe import TranscribeError
from .uploads import UPLOADS
from .upstream import UpstreamError, chat_sync

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Warm both resident models in a background thread.

    The thread is daemonic and every failure is swallowed, so startup
    never blocks and never fails because of the models.
    """
    try:
        import threading

        thread = threading.Thread(
            target=judge_module.warmup,
            name="sf-judge-warmup",
            daemon=True,
        )
        thread.start()
    except Exception:
        pass
    yield


app = FastAPI(title="shadowfenced", lifespan=lifespan)


def hosted_limit_response(request: Request) -> JSONResponse | None:
    """429 response when the hosted per-IP bucket is empty (None = pass)."""
    if hosted_module.is_hosted():
        client = request.client.host if request.client else "unknown"
        if not hosted_module.allow_request(client):
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "shadowfenced_rate_limited",
                        "type": "shadowfenced_rate_limited",
                        "message": "rate limit exceeded; try again shortly",
                    },
                },
            )
    return None


@app.middleware("http")
async def _hosted_rate_limit(request: Request, call_next):
    """Hosted demo: per-IP token bucket; local mode passes through."""
    limited = hosted_limit_response(request)
    if limited is not None:
        return limited
    return await call_next(request)

_POLICY: Policy | None = None
_REGISTRY: CanaryRegistry | None = None

from shadowfenced.canary import DEFAULT_KINDS as REGISTRY_KINDS

# Prepended (as a system message) to upstream requests that carry redacted
# text so small local models still complete the request instead of
# refusing on the bracketed placeholder labels.
REDACT_NOTE = (
    "Privacy notice from the user's organisation: items in square "
    "brackets like [PII_EMAIL_1] are harmless placeholder labels "
    "standing in for values that were removed on purpose. They are not "
    "private data. Complete the user's request normally and copy the "
    "labels exactly where the values belong. Do not mention this notice "
    "or the placeholders in your answer."
)


def _log_decision(decision: engine.Decision, text: str) -> None:
    """Append one gate decision to the hash-chained log; never fail the gate."""
    try:
        from . import ecs_log

        ecs_log.append_decision(get_settings().log_path, decision, text)
    except Exception:
        pass


def get_policy() -> Policy:
    """Load the org policy once and reuse it."""
    global _POLICY
    if _POLICY is None:
        _POLICY = load_policy()
    return _POLICY


def get_registry() -> CanaryRegistry:
    """Build the canary registry once from the configured seed."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = CanaryRegistry(get_settings().canary_seed, REGISTRY_KINDS)
    return _REGISTRY


def reset_state() -> None:
    """Forget cached policy/registry (tests only)."""
    global _POLICY, _REGISTRY, _IMAGE_REDACTION_OVERRIDE
    _POLICY = None
    _REGISTRY = None
    _IMAGE_REDACTION_OVERRIDE = None


# In-memory default provider override set by POST /api/provider
# (reset between tests via reset_provider_override()).
_PROVIDER_OVERRIDE: str | None = None

# In-memory image-redaction choice from the consent card
# ("on" = Glimmer + cloud SAM masks, "local" = Glimmer + local box fill,
# "off" = ask first). Set by POST /api/image-redaction/consent, default
# from SF_IMAGE_REDACTION ("off"); reset between tests via reset_state().
_IMAGE_REDACTION_OVERRIDE: str | None = None


def image_redaction_mode(settings: Settings | None = None) -> str:
    """Effective image-redaction mode: consent override, else settings."""
    if _IMAGE_REDACTION_OVERRIDE in ("on", "local", "off"):
        return _IMAGE_REDACTION_OVERRIDE
    try:
        cfg = settings or get_settings()
    except Exception:
        return "off"
    mode = (cfg.image_redaction or "off").strip().lower()
    return mode if mode in ("on", "local") else "off"


def reset_image_redaction() -> None:
    """Forget the consent-card override (tests only)."""
    global _IMAGE_REDACTION_OVERRIDE
    _IMAGE_REDACTION_OVERRIDE = None

# --- WARN confirmations (T37) -------------------------------------------------
# WARN decisions send nothing upstream. The warning (banner, combination,
# inferred labels, generalised option) is returned with the decision id;
# a follow-up POST /api/warns/{id}/confirm with the same id forwards
# either the original text ("send_anyway") or the generalised text
# ("send_generalised"). Entries expire after 15 minutes like holds/uploads.
import threading as _threading

_WARNS: dict[str, dict[str, Any]] = {}
_WARNS_LOCK = _threading.Lock()
WARN_TTL_S = 15 * 60


def _warn_store_save(
    decision: "engine.Decision", text: str,
    messages: list[Any], provider: str | None,
) -> None:
    record = {
        "decision": decision,
        "text": text,
        "messages": [m for m in messages if isinstance(m, dict)],
        "provider": provider,
        "created_at": time.time(),
    }
    with _WARNS_LOCK:
        _WARNS[decision.decision_id] = record


def _warn_store_load(decision_id: str) -> dict[str, Any] | None:
    with _WARNS_LOCK:
        record = _WARNS.get(decision_id)
        if record is None:
            return None
        if time.time() - record.get("created_at", 0) > WARN_TTL_S:
            _WARNS.pop(decision_id, None)
            return None
        return record


def _warn_store_clear() -> None:
    with _WARNS_LOCK:
        _WARNS.clear()


def reset_provider_override() -> None:
    """Forget the POST /api/provider override (tests only)."""
    global _PROVIDER_OVERRIDE
    _PROVIDER_OVERRIDE = None


def default_provider() -> str:
    """Effective default provider: explicit override, else settings."""
    if _PROVIDER_OVERRIDE is not None and providers_registry.get_provider(
        _PROVIDER_OVERRIDE
    ) is not None:
        return _PROVIDER_OVERRIDE
    return get_settings().upstream or "ollama"


def resolve_provider(provider: str | None) -> str:
    """Per-request provider: explicit choice, else the default."""
    name = (provider or "").strip().lower()
    if providers_registry.get_provider(name) is not None:
        return name
    return default_provider()


PROVIDER_LABELS = {
    "meta": "Meta Muse Spark (cloud)",
    "ollama": "Llama 3.2 3B via Ollama (local stand-in)",
}


def provider_list() -> list[dict[str, Any]]:
    """Serialise the provider catalogue for GET /api/providers."""
    return providers_registry.describe_all()


def _doc_fields(text: str) -> dict[str, Any]:
    """Document-type classification for one upload payload (T29).

    Never raises and never blocks: unscannable input classifies as
    no document type.
    """
    try:
        from . import docclass as docclass_module

        info = docclass_module.classify(text)
        return {"doc_type": info.get("doc_type"),
                "doc_warning": info.get("warning")}
    except Exception:
        return {"doc_type": None, "doc_warning": None}


def span_to_dict(span: Span) -> dict[str, Any]:
    """Serialise one span per the API contract (UTF-16 + code points)."""
    return {
        "span_id": span.span_id,
        "start": span.start,
        "end": span.end,
        "cp_start": span.cp_start,
        "cp_end": span.cp_end,
        "cls": span.cls,
        "subclass": span.subclass,
        "tier": span.tier,
        "proof": span.proof,
        "status": span.status,
        "message": span.message,
        "why": span.why,
        "suggested_action": span.suggested_action,
        "can_keep": span.can_keep,
        "confidence": span.confidence,
    }


def _part_text(part: Any) -> tuple[str, bool]:
    """Return (text, is_text) for one message content part."""
    if isinstance(part, str):
        return part, True
    if isinstance(part, dict):
        if part.get("type") == "text":
            content = part.get("text", "")
            return (content if isinstance(content, str) else ""), True
        return "", False
    return "", False


def extract_user_text(messages: list[Any]) -> tuple[str, bool]:
    """Concatenate user text; flag is False when non-text parts appear."""
    chunks: list[str] = []
    clean = True
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                text, is_text = _part_text(part)
                if not is_text:
                    clean = False
                chunks.append(text)
        elif content is None:
            continue
        else:
            clean = False
    return "\n".join(chunks), clean


def _shadowfenced_field(decision: engine.Decision) -> dict[str, Any]:
    field: dict[str, Any] = {
        "decision": decision.decision,
        "decision_id": decision.decision_id,
        "resolved_tier": decision.resolved_tier,
        "latency_ms": decision.latency_ms,
        "rule_ids": _decision_rule_ids(decision),
    }
    if decision.decision == "WARN":
        field["warning"] = _warn_payload(decision)
    return field


def _forward_messages(
    messages: list[dict[str, Any]],
    sent_text: str | list[Any] | None,
    decision: str | None = None,
) -> list[dict[str, Any]]:
    """Rebuild upstream messages with the gated user text only.

    On REDACT the redacted text goes upstream with a leading system note
    explaining the placeholders; ALLOW forwards plain user text.
    """
    if isinstance(sent_text, list):
        return _forward_chat_messages(messages, sent_text, decision)
    kept = [m for m in messages if isinstance(m, dict) and m.get("role") != "user"]
    if decision == "REDACT" and sent_text is not None:
        kept.insert(0, {"role": "system", "content": REDACT_NOTE})
    if sent_text is not None:
        kept.append({"role": "user", "content": sent_text})
    return kept


def _forward_chat_messages(
    messages: list[Any],
    redacted: list[Any],
    decision: str | None = None,
) -> list[dict[str, Any]]:
    """Forward per-message chat items in their original order and roles.

    ``redacted`` is aligned with ``messages``: dict items are used as-is,
    str items replace a string content, and None keeps the original
    message. On REDACT a leading system note explains the placeholders.
    """
    out: list[dict[str, Any]] = []
    for original, item in zip(messages, redacted):
        if isinstance(item, dict):
            out.append(item)
        elif (
            isinstance(item, str)
            and isinstance(original, dict)
            and isinstance(original.get("content"), str)
        ):
            new_message = dict(original)
            new_message["content"] = item
            out.append(new_message)
        elif isinstance(original, dict):
            out.append(original)
        else:
            out.append({"role": "user", "content": str(original)})
    if decision == "REDACT":
        out.insert(0, {"role": "system", "content": REDACT_NOTE})
    return out


def _content_text_parts(content: Any) -> tuple[list[str], bool]:
    """Gateable text pieces of one message content, plus the clean flag.

    String content gates as one piece; a list gates each text part and
    sets clean False when non-text parts appear; anything else is
    unclean with no gateable text.
    """
    if content is None:
        return [], True
    if isinstance(content, str):
        return [content], True
    if isinstance(content, list):
        pieces: list[str] = []
        clean = True
        for part in content:
            text, is_text = _part_text(part)
            if not is_text:
                clean = False
            else:
                pieces.append(text)
        return pieces, clean
    return [], False


def _rebuild_chat_message(
    message: dict[str, Any],
    content_pieces: list[str],
    arg_redacted: dict[int, str],
) -> dict[str, Any]:
    """Write redacted strings back into a copy of one message."""
    new_message = dict(message)
    content = message.get("content", "")
    if isinstance(content, str):
        new_message["content"] = content_pieces[0] if content_pieces else ""
    elif isinstance(content, list):
        parts: list[Any] = []
        iterator = iter(content_pieces)
        for part in content:
            _, is_text = _part_text(part)
            if is_text and isinstance(part, dict):
                part = dict(part)
                part["text"] = next(iterator, "")
            parts.append(part)
        new_message["content"] = parts
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        rebuilt: list[Any] = []
        for index, call in enumerate(tool_calls):
            if isinstance(call, dict) and index in arg_redacted:
                call = dict(call)
                function = call.get("function")
                if isinstance(function, dict):
                    function = dict(function)
                else:
                    function = {}
                function["arguments"] = arg_redacted[index]
                call["function"] = function
            rebuilt.append(call)
        new_message["tool_calls"] = rebuilt
    return new_message


def _gate_chat_messages(
    messages: list[Any],
) -> tuple[engine.Decision, list[Any] | None, str]:
    """Gate every chat message role plus assistant tool-call arguments.

    Each message's content text parts and tool-call arguments are gated
    separately (Tier 1 plus protected keys); redaction rewrites only the
    sensitive spans inside each message. Returns ``(decision, items,
    text)`` where ``items`` is aligned with ``messages`` (redacted dicts
    for REDACT/ALLOW, None for BLOCK/HOLD) and ``text`` is the combined
    gated text for the decision log. A BLOCK in any message blocks the
    request; severity otherwise ranks BLOCK > HOLD > REDACT > ALLOW.
    """
    import dataclasses
    import json as _json
    import uuid as _uuid

    policy = get_policy()
    registry = get_registry()
    order = {"ALLOW": 0, "WARN": 1, "REDACT": 2, "HOLD": 3, "BLOCK": 4}

    roles: list[str] = []
    raw_messages: list[Any] = list(messages)
    content_lists: list[list[str]] = []
    args_lists: list[dict[int, str]] = []
    unclean_flags: list[bool] = []
    for message in raw_messages:
        if not isinstance(message, dict):
            roles.append("user")
            content_lists.append([])
            args_lists.append({})
            unclean_flags.append(False)
            continue
        role = message.get("role")
        if not isinstance(role, str) or not role:
            role = "user"
        roles.append(role)
        pieces, clean = _content_text_parts(message.get("content", ""))
        arg_texts: dict[int, str] = {}
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for index, call in enumerate(tool_calls):
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict) or "arguments" not in function:
                    continue
                arguments = function["arguments"]
                if isinstance(arguments, str):
                    arg_texts[index] = arguments
                elif isinstance(arguments, (dict, list)):
                    arg_texts[index] = _json.dumps(arguments)
                elif arguments is not None:
                    arg_texts[index] = str(arguments)
        content_lists.append(pieces)
        args_lists.append(arg_texts)
        unclean_flags.append(not clean)

    records: list[dict[str, Any]] = []
    cursor = 0
    for info_index, (pieces, arg_texts) in enumerate(
        zip(content_lists, args_lists)
    ):
        for piece in pieces:
            records.append(
                {"info": info_index, "kind": "content", "text": piece,
                 "start": cursor}
            )
            cursor += len(piece) + 1
        for arg_index in sorted(arg_texts):
            records.append(
                {"info": info_index, "kind": "args", "arg": arg_index,
                 "text": arg_texts[arg_index], "start": cursor}
            )
            cursor += len(arg_texts[arg_index]) + 1
    full = "\n".join(record["text"] for record in records)

    spans_all: list[Span] = []
    reasons_all: list[str] = []
    merged: dict[str, str] = {}
    counters: dict[str, int] = {}
    key_hits: list[tuple[str, str, int, int, int]] = []
    worst = "ALLOW"
    latency_total = 0.0
    arg_redacted_all: list[dict[int, str]] = [{} for _ in raw_messages]
    content_redacted_all: list[list[str]] = [[] for _ in raw_messages]

    for rec_index, record in enumerate(records):
        text = record["text"]
        info_index = record["info"]
        role = roles[info_index]
        hit = keyguard_module.find_in_text(text)
        prior_texts = [r["text"] for r in records[:rec_index]]
        string_decision = engine.gate(
            text, policy, registry, mode="send",
            conversation=prior_texts or None,
        )
        latency_total += float(
            (string_decision.latency_ms or {}).get("total", 0.0)
        )
        record["engine"] = string_decision
        outcome = string_decision.decision
        redacted: str | None = None
        if hit is not None:
            variable, _encoding, local_start, local_end = hit
            key_hits.append(
                (variable, role, record["start"] + local_start,
                 record["start"] + local_end, info_index)
            )
            reasons_all.append(f"{variable} appeared in a {role} message")
            outcome = "BLOCK"
        elif outcome in ("REDACT", "WARN"):
            redacted = string_decision.sent_text or ""
            for placeholder, value in (
                string_decision.placeholders or {}
            ).items():
                if placeholder in merged:
                    if merged[placeholder] == value:
                        continue
                    base, _, _ = placeholder.strip().strip("[]").rpartition("_")
                    counters[base] = counters.get(base, 0) + 1
                    while f"[{base}_{counters[base]}]" in merged:
                        counters[base] += 1
                    fresh = f"[{base}_{counters[base]}]"
                    redacted = redacted.replace(placeholder, fresh)
                    merged[fresh] = value
                else:
                    base, _, number = (
                        placeholder.strip().strip("[]").rpartition("_")
                    )
                    try:
                        counters[base] = max(
                            counters.get(base, 0), int(number)
                        )
                    except ValueError:
                        pass
                    merged[placeholder] = value
        elif outcome == "ALLOW":
            redacted = text
        if order[outcome] > order[worst]:
            worst = outcome
        reasons_all.extend(string_decision.reasons)
        record["evidence"] = dict(string_decision.evidence or {})
        for span in string_decision.spans:
            shifted = dataclasses.replace(
                span,
                cp_start=span.cp_start + record["start"],
                cp_end=span.cp_end + record["start"],
                start=0,
                end=0,
                span_id="",
            )
            spans_all.append(shifted)
        record["redacted"] = redacted
        if redacted is not None:
            if record["kind"] == "content":
                content_redacted_all[info_index].append(redacted)
            else:
                arg_redacted_all[info_index][record["arg"]] = redacted

    if any(unclean_flags) and order[worst] < order["HOLD"]:
        worst = "HOLD"
    if any(unclean_flags):
        reasons_all.append("non-text content parts need review")

    for span in spans_all:
        span.start = cp_to_utf16(full, span.cp_start)
        span.end = cp_to_utf16(full, span.cp_end)
        span.span_id = make_span_id(span.cls, span.cp_start, span.cp_end)

    for variable, role, start, end, _info_index in key_hits:
        spans_all.append(
            span_from_cp_range(
                full,
                start,
                end,
                cls="secret",
                subclass="protected_key",
                status="proven",
                message="Looks like a protected API key.",
                why=f"{variable} value (or an encoding of it) in gated text",
                suggested_action="block",
                can_keep=False,
                confidence=0.99,
            )
        )
    for _variable in dict.fromkeys(hit[0] for hit in key_hits):
        keyguard_module.note_hit(_variable)

    decision_id = _uuid.uuid4().hex
    latency_ms: dict[str, float] = {"total": latency_total}
    if worst == "BLOCK":
        evidence: dict[str, Any] = {}
        if key_hits:
            evidence = {
                "protected_key": key_hits[0][0],
                "role": key_hits[0][1],
                "left_machine": False,
            }
        for record in records:
            record_evidence = record.get("evidence") or {}
            for key in ("reid", "person_search"):
                if key in record_evidence and key not in evidence:
                    evidence[key] = record_evidence[key]
        return (
            engine.Decision(
                decision="BLOCK",
                decision_id=decision_id,
                spans=spans_all,
                sent_text=None,
                placeholders={},
                resolved_tier=1,
                latency_ms=latency_ms,
                reasons=reasons_all,
                evidence=evidence,
            ),
            None,
            full,
        )
    if worst == "HOLD":
        proven_ids = [
            span.span_id for span in spans_all if span.status == "proven"
        ]
        ambiguous = [s for s in spans_all if s.status == "ambiguous"]
        pending: list[dict[str, Any]] = [
            {"kind": "message", "text": full[:2000]}
        ]
        for span in ambiguous:
            pending.append(
                {
                    "kind": "span",
                    "span_id": span.span_id,
                    "text": full[span.cp_start:span.cp_end],
                }
            )
        return (
            engine.Decision(
                decision="HOLD",
                decision_id=decision_id,
                spans=spans_all,
                sent_text=None,
                placeholders={},
                resolved_tier=4,
                latency_ms=latency_ms,
                reasons=reasons_all,
                evidence={
                    "proven_by_code": proven_ids,
                    "pending": pending,
                    **{
                        key: (record.get("evidence") or {})[key]
                        for record in records
                        for key in ("reid", "person_search")
                        if key in (record.get("evidence") or {})
                    },
                },
                status="reviewing",
            ),
            None,
            full,
        )
    items: list[Any] = []
    for index, message in enumerate(raw_messages):
        if not isinstance(message, dict):
            items.append(message)
        else:
            items.append(
                _rebuild_chat_message(
                    message,
                    content_redacted_all[index],
                    arg_redacted_all[index],
                )
            )
    def _merged_warn_evidence() -> dict[str, Any]:
        merged_evidence: dict[str, Any] = {}
        for record in records:
            evidence = record.get("evidence") or {}
            for key in ("reid", "person_search", "doc_type",
                        "doc_warning", "doc_signals"):
                if key in evidence and key not in merged_evidence:
                    merged_evidence[key] = evidence[key]
        return merged_evidence

    if worst == "WARN":
        if merged:
            save_mapping(decision_id, merged)
        return (
            engine.Decision(
                decision="WARN",
                decision_id=decision_id,
                spans=spans_all,
                sent_text="\n".join(
                    record.get("redacted") or "" for record in records
                ),
                placeholders=dict(merged),
                resolved_tier=1,
                latency_ms=latency_ms,
                reasons=reasons_all,
                evidence=_merged_warn_evidence(),
            ),
            items,
            full,
        )
    if worst == "REDACT":
        save_mapping(decision_id, merged)
        return (
            engine.Decision(
                decision="REDACT",
                decision_id=decision_id,
                spans=spans_all,
                sent_text="\n".join(
                    record.get("redacted") or "" for record in records
                ),
                placeholders=dict(merged),
                resolved_tier=1,
                latency_ms=latency_ms,
                reasons=reasons_all,
                evidence=_merged_warn_evidence(),
            ),
            items,
            full,
        )
    reasons_all.append("no spans -> ALLOW")
    return (
        engine.Decision(
            decision="ALLOW",
            decision_id=decision_id,
            spans=spans_all,
            sent_text=full,
            placeholders={},
            resolved_tier=1,
            latency_ms=latency_ms,
            reasons=reasons_all,
        ),
        items,
        full,
    )


def _transport() -> Any:
    """Injected httpx transport for tests (None in production)."""
    return getattr(app.state, "upstream_transport", None)


def _send_upstream(
    messages: list[dict[str, Any]], provider: str | None
) -> str:
    """Forward gated messages; honour the injected test transport."""
    return chat_sync(
        messages,
        provider,
        settings=get_settings(),
        transport=_transport(),
    )


class ScanRequest(BaseModel):
    text: str = ""
    mode: Literal["tier1", "live"] = "tier1"


class ChoiceItem(BaseModel):
    span_id: str
    choice: str  # redact | keep


class SendRequest(BaseModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)
    file_ids: list[str] = Field(default_factory=list)
    choices: list[ChoiceItem] = Field(default_factory=list)
    provider: str | None = None


class ChatRequest(BaseModel):
    model: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False


class RuleRequest(BaseModel):
    approve: bool = False
    reason: str | None = None


class ProviderRequest(BaseModel):
    id: str = ""


class ProviderKeyRequest(BaseModel):
    key: str = ""


class ProviderOAuthExchange(BaseModel):
    code: str = ""
    session_id: str = ""


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness probe plus the live-hints flag for the UI."""
    return {"status": "ok", "live_hints": get_settings().live_hints}


@app.get("/api/health/components")
def health_components() -> dict[str, Any]:
    """Per-component status from cheap local checks only.

    Never calls a paid API and never returns secrets: the Meta and
    Voice checks report key presence only, and the Ollama check only
    asks loopback ``/api/version`` and ``/api/tags``. Tests inject an
    httpx transport via ``app.state.components_transport``.
    """
    from . import health as health_module

    transport = getattr(app.state, "components_transport", None)
    return {"components": health_module.collect_components(transport=transport)}


@app.get("/api/hosted")
def hosted_status() -> dict[str, Any]:
    """Hosted demo flag plus the public-demo banner (shown when hosted)."""
    hosted = hosted_module.is_hosted()
    return {
        "hosted": hosted,
        "banner": hosted_module.HOSTED_BANNER if hosted else None,
    }


@app.post("/api/scan")
def scan(request: ScanRequest) -> dict[str, Any]:
    """Run the Tier 1 scan and return spans plus latency.

    ``mode="live"`` additionally asks the live model about each
    ambiguous span and attaches non-benign answers as extra Tier 2
    ``suspicion`` spans, but only when ``SF_LIVE_HINTS=on`` (off by
    default: live mode returns Tier 1 only). A judge failure leaves
    just the Tier 1 spans.
    """
    from . import tier1

    started = time.perf_counter()
    trace: list[dict[str, Any]] = []
    spans = tier1.scan(
        request.text, get_policy(), get_registry(), trace_out=trace
    )
    tier1_ms = (time.perf_counter() - started) * 1000
    latency_ms: dict[str, float] = {"tier1": tier1_ms}
    if request.mode == "live" and get_settings().live_hints:
        tier2_started = time.perf_counter()
        live_before = len(spans)
        spans = _with_live_suspicions(request.text, spans)
        latency_ms["tier2"] = (time.perf_counter() - tier2_started) * 1000
        trace.append(
            {
                "step": "live.hints",
                "tool": "live",
                "detail": (
                    f"{len(spans) - live_before} model suspicion(s)"
                ),
                "result": "hit" if len(spans) > live_before else "clean",
                "ms": round(latency_ms["tier2"], 3),
            }
        )
    latency_ms["total"] = (time.perf_counter() - started) * 1000
    if request.mode == "live" and not get_settings().live_hints:
        trace.append(
            {
                "step": "live.hints",
                "tool": "live",
                "detail": "disabled (SF_LIVE_HINTS=off); Tier 1 only",
                "result": "skipped",
                "ms": 0.0,
            }
        )
    try:
        reid_payload = _reid_scan_payload(request.text, trace)
    except Exception:
        reid_payload = {
            "score": 0, "combination": [], "outcome": "OK",
            "banner": "", "generalised_text": "",
            "findings": [],
        }
    trace.append(
        {
            "step": "scan.complete",
            "tool": "tier1",
            "detail": f"{len(spans)} span(s)",
            "result": "done",
            "ms": round(latency_ms["total"], 3),
        }
    )
    return {
        "spans": [span_to_dict(s) for s in spans],
        "reid": reid_payload,
        "latency_ms": latency_ms,
        "trace": trace,
    }


def _reid_scan_payload(
    text: str, trace_out: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Re-identification findings for the live composer (no raw values).

    Returns score, kind combination, outcome, banner, generalised text
    plus offset-only findings ``[{kind, start, end}]`` (UTF-16 units, the
    scan contract) so the composer can underline combinations as you
    type. Raw values never leave this payload, the trace, or the log.
    """
    from . import reid as reid_module
    from . import tier1 as tier1_module

    findings = tier1_module.scan_reid(text, trace_out=trace_out)
    result = reid_module.assess(
        text, current_findings=findings, generalise_offer=True,
    )
    reid_module.trace_combine(
        trace_out, result.score, result.combination, result.outcome,
    )
    from .spans import cp_to_utf16 as _cp_to_utf16
    offset_findings: list[dict[str, Any]] = []
    for item in findings:
        try:
            start, end = item.get("span", (0, 0))
            kind = str(item.get("kind", ""))
            offset_findings.append({
                "kind": kind,
                "start": _cp_to_utf16(text, int(start)),
                "end": _cp_to_utf16(text, int(end)),
            })
        except (TypeError, ValueError, AttributeError):
            continue
    return {
        "score": result.score,
        "combination": list(result.combination),
        "outcome": result.outcome,
        "banner": result.banner,
        "generalised_text": result.generalised_text,
        "findings": offset_findings,
    }


def _with_live_suspicions(text: str, spans: list[Span]) -> list[Span]:
    """Attach live-model suspicions for ambiguous spans (never raises)."""
    try:
        model = get_settings().live_model
        extras: list[Span] = []
        for span in spans:
            if span.status != "ambiguous":
                continue
            try:
                window = judge_module.make_window(
                    text, span.cp_start, span.cp_end
                )
                judgement = judge_module.judge(window, model, live=True)
            except Exception:
                continue
            if judgement is None or judgement.cls == "benign":
                continue
            if not judgement.span or judgement.span not in text:
                continue
            at = text.find(judgement.span)
            extras.append(
                span_from_cp_range(
                    text,
                    at,
                    at + len(judgement.span),
                    cls=judgement.cls,
                    subclass="model_suspicion",
                    tier=2,
                    proof="model",
                    status="suspicion",
                    message=(
                        f"model suspicion: {judgement.cls} "
                        f"(confidence {judgement.confidence})"
                    ),
                    why=(
                        f"live model suspicion: {judgement.cls} "
                        f"(confidence {judgement.confidence})"
                    ),
                    suggested_action="review",
                    can_keep=True,
                    confidence=judgement.confidence,
                )
            )
        if extras:
            return sorted(spans + extras, key=lambda s: (s.cp_start, s.cp_end))
        return spans
    except Exception:
        return spans


@app.post("/api/send")
def send(body: SendRequest) -> dict[str, Any]:
    """Gate the user text, forward it when allowed, re-hydrate the answer."""
    provider = resolve_provider(body.provider)
    trace: list[dict[str, Any]] = []
    text, clean = extract_user_text(body.messages)
    file_texts, file_error = _file_sections(body.file_ids)
    if file_error is not None:
        decision = engine.gate(text, get_policy(), get_registry(), mode="send", trace_out=trace)
        decision = _hold_for(decision, file_error)
        trace.append(
            {
                "step": "gate.hold_reason",
                "tool": "gate",
                "detail": "attached file needs review; send held",
                "result": "HOLD",
                "ms": 0.0,
            }
        )
        _log_decision(decision, text)
        _register_hold(decision, text, body.messages, provider, why=file_error)
        return _send_response(decision, None, [], [], provider, trace)
    if file_texts:
        text = text + "".join(file_texts) if text else "".join(file_texts).lstrip("\n")
    decision = engine.gate(
        text, get_policy(), get_registry(),
        [c.model_dump() for c in body.choices],
        trace_out=trace,
        mode="send",
    )
    if not clean:
        decision = _hold_for(decision, "non_text_content")
        trace.append(
            {
                "step": "gate.hold_reason",
                "tool": "gate",
                "detail": "non-text content parts need review; send held",
                "result": "HOLD",
                "ms": 0.0,
            }
        )
        _log_decision(decision, text)
        _register_hold(
            decision, text, body.messages, provider,
            why="non-text content parts need review",
        )
        return _send_response(decision, None, [], [], provider, trace)
    _log_decision(decision, text)
    if decision.decision in ("BLOCK", "HOLD", "WARN"):
        if decision.decision == "HOLD":
            _register_hold(decision, text, body.messages, provider)
        if decision.decision == "WARN":
            _warn_store_save(decision, text, body.messages, provider)
        return _send_response(decision, None, [], [], provider, trace)
    try:
        answer = _send_upstream(
            _forward_messages(
                body.messages, decision.sent_text, decision.decision
            ),
            provider,
        )
    except UpstreamError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "shadowfenced_upstream",
                "type": "shadowfenced_upstream",
                "message": str(exc),
            },
        ) from exc
    mapping = load_mapping(decision.decision_id)
    rehydrated, highlights, unmatched = rehydrate(answer, mapping)
    return _send_response(decision, rehydrated, highlights, unmatched, provider, trace)


def _file_sections(file_ids: list[str]) -> tuple[list[str], str | None]:
    """Build the send-time text sections for uploaded files.

    Returns ``(sections, error)``: each section is
    ``"\\nAttached file <name>:\\n<text>"`` so the gate redacts file
    content with placeholders shared with the message. ``error`` names
    an unscannable, unknown, or expired file, which HOLDs the send.
    """
    sections: list[str] = []
    for file_id in file_ids:
        record = UPLOADS.load(file_id)
        if record is None:
            return [], f"unknown or expired file {file_id}"
        if record.kind == "image":
            if record.status == "unscannable" or not record.image_png:
                return [], f"file {record.name} is unscannable and needs review"
            note = record.text.strip() or f"({record.regions} regions redacted)"
            sections.append(f"\nAttached image {record.name}:\n{note}")
            continue
        if record.status == "unscannable" or not record.text.strip():
            return [], f"file {record.name} is unscannable and needs review"
        sections.append(f"\nAttached file {record.name}:\n{record.text}")
    return sections, None


def _hold_for(decision: engine.Decision, reason: str) -> engine.Decision:
    return engine.Decision(
        decision="HOLD",
        decision_id=decision.decision_id,
        spans=decision.spans,
        sent_text=None,
        placeholders={},
        resolved_tier=decision.resolved_tier,
        latency_ms=decision.latency_ms,
        reasons=[*decision.reasons, reason],
        evidence=dict(decision.evidence or {}),
        status=decision.status,
        doc_type=decision.doc_type,
        doc_warning=decision.doc_warning,
    )


def _review_sender(
    messages: list[dict[str, Any]], provider: str | None
) -> holds.Sender:
    """Build the auto-send/approve sender for one hold's messages."""

    def _send(sent_text: str) -> str:
        return _send_upstream(
            _forward_messages(messages, sent_text, "REDACT"), provider
        )

    return _send


def _register_hold(
    decision: engine.Decision,
    text: str,
    messages: list[Any],
    provider: str | None,
    *,
    why: str | None = None,
) -> None:
    """Register a HOLD for background Glimmer review; never fail sends."""
    try:
        hold = holds.create_hold(
            decision, text, messages, provider, needs_human_why=why,
        )
        if hold.status == "reviewing":
            holds.start_review(
                hold.hold_id,
                sender=_review_sender(
                    [m for m in messages if isinstance(m, dict)], provider
                ),
                meta_transport=_transport(),
                # Pin the log path now: the review thread may outlive
                # this request and must not resolve it lazily later.
                log_path=get_settings().log_path,
            )
    except Exception:
        pass


def _warn_payload(decision: engine.Decision) -> dict[str, Any]:
    """User-facing WARN details without raw values (banner, kinds, options)."""
    evidence = decision.evidence or {}
    reid = evidence.get("reid") or {}
    person = evidence.get("person_search") or {}
    banner = person.get("banner") or reid.get("banner") or engine.user_message(
        decision
    )
    generalised = person.get("generalised_text") or reid.get(
        "generalised_text"
    ) or ""
    return {
        "banner": banner,
        "combination": list(reid.get("combination") or []),
        "inferred": list(person.get("inferred") or []),
        "generalised_text": generalised,
        "has_generalise_option": bool(generalised),
        "confirm_url": f"/api/warns/{decision.decision_id}/confirm",
    }


def _decision_rule_ids(decision: engine.Decision) -> list[str]:
    """Catalogue ids that fired (mapped server-side, never guessed by UI)."""
    try:
        current = list(decision.rule_ids or [])
    except Exception:
        current = []
    if current:
        return current
    try:
        from . import rule_catalog as _catalog

        return list(_catalog.rule_ids_for_decision(decision))
    except Exception:
        return []


def _send_response(
    decision: engine.Decision,
    answer: str | None,
    highlights: list[dict],
    unmatched: list[str],
    provider: str | None = None,
    trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "spans": [span_to_dict(s) for s in decision.spans],
        "reasons": decision.reasons,
    }
    evidence.update(decision.evidence or {})
    response: dict[str, Any] = {
        "decision": decision.decision,
        "decision_id": decision.decision_id,
        "sent_text": decision.sent_text,
        "answer": answer,
        "answer_highlights": highlights,
        "user_message": engine.user_message(decision),
        "doc_warning": decision.doc_warning,
        "doc_type": decision.doc_type,
        "evidence": evidence,
        "resolved_tier": decision.resolved_tier,
        "latency_ms": decision.latency_ms,
        "unmatched_placeholders": unmatched,
        "provider": resolve_provider(provider),
        "trace": trace if trace is not None else [],
        "rule_ids": _decision_rule_ids(decision),
    }
    if decision.decision == "HOLD":
        response["review_url"] = f"/api/holds/{decision.decision_id}"
    if decision.decision == "WARN":
        response["warn"] = _warn_payload(decision)
        response["confirm_url"] = (
            f"/api/warns/{decision.decision_id}/confirm"
        )
    return response


@app.post("/v1/chat/completions")
def chat_completions(
    body: ChatRequest,
    x_shadowfenced_accept_warn: str | None = Header(default=None),
) -> Any:
    """OpenAI-compatible completions behind the Shadowfenced gate.

    Every forwarded message role (system, user, assistant, tool) and
    assistant tool-call arguments are gated, not only user messages.
    Messages forward in their original order with their original roles;
    redaction rewrites only the sensitive spans inside each message. A
    BLOCK in any message blocks the request. WARN proceeds only when
    the client sets ``X-Shadowfenced-Accept-Warn: 1``; otherwise it
    returns 409 with the warning, like a hold.
    """
    decision, redacted, text = _gate_chat_messages(body.messages)
    _log_decision(decision, text)
    if decision.decision == "WARN":
        accept_raw = (x_shadowfenced_accept_warn
                      if isinstance(x_shadowfenced_accept_warn, str)
                      else "")
        if accept_raw.strip() != "1":
            return _warn_response(decision)
    if decision.decision == "HOLD":
        if "non-text content parts need review" in decision.reasons:
            _register_hold(
                decision, text, body.messages, None,
                why="non-text content parts need review",
            )
            return _hold_response(decision, "non-text content parts need review")
        _register_hold(decision, text, body.messages, None)
        return _hold_response(decision, "ambiguous spans need human review")
    if decision.decision == "BLOCK":
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "shadowfenced_block",
                    "type": "shadowfenced_block",
                    "message": "blocked: proven secret would leak",
                },
                "shadowfenced": _shadowfenced_field(decision),
            },
            headers={"X-Shadowfenced-Decision": "BLOCK"},
        )
    try:
        answer = _send_upstream(
            _forward_messages(body.messages, redacted, decision.decision),
            None,
        )
    except UpstreamError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "shadowfenced_upstream",
                    "type": "shadowfenced_upstream",
                    "message": str(exc),
                },
                "shadowfenced": _shadowfenced_field(decision),
            },
            headers={"X-Shadowfenced-Decision": decision.decision},
        )
    mapping = load_mapping(decision.decision_id)
    rehydrated, _, _ = rehydrate(answer, mapping)
    model = body.model or get_settings().local_model
    if body.stream:
        return _stream_response(decision, model, rehydrated)
    return JSONResponse(
        status_code=200,
        content={
            "id": "chatcmpl-" + uuid.uuid4().hex[:12],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": rehydrated},
                    "finish_reason": "stop",
                }
            ],
            "shadowfenced": _shadowfenced_field(decision),
        },
        headers={"X-Shadowfenced-Decision": decision.decision},
    )


def _warn_response(decision: engine.Decision) -> JSONResponse:
    evidence: dict[str, Any] = {
        "spans": [span_to_dict(s) for s in decision.spans],
        "reasons": decision.reasons,
    }
    evidence.update(decision.evidence or {})
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "shadowfenced_warn",
                "type": "shadowfenced_warn",
                "message": engine.user_message(decision),
            },
            "warning": _warn_payload(decision),
            "evidence": evidence,
            "rule_ids": _decision_rule_ids(decision),
            "shadowfenced": _shadowfenced_field(decision),
        },
        headers={"X-Shadowfenced-Decision": "WARN"},
    )


def _hold_response(decision: engine.Decision, message: str) -> JSONResponse:
    evidence: dict[str, Any] = {
        "spans": [span_to_dict(s) for s in decision.spans],
        "reasons": decision.reasons,
    }
    evidence.update(decision.evidence or {})
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "shadowfenced_hold",
                "type": "shadowfenced_hold",
                "message": message,
            },
            "review_url": f"/api/holds/{decision.decision_id}",
            "evidence": evidence,
            "rule_ids": _decision_rule_ids(decision),
            "shadowfenced": _shadowfenced_field(decision),
        },
        headers={"X-Shadowfenced-Decision": "HOLD"},
    )


def _hold_not_found(hold_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "shadowfenced_unknown_hold",
                "type": "shadowfenced_unknown_hold",
                "message": f"unknown hold {hold_id}",
            },
        },
    )


@app.get("/api/holds/{hold_id}")
def hold_status(hold_id: str) -> Any:
    """Return one hold's evidence card."""
    hold = holds.HOLDS.get(hold_id)
    if hold is None:
        return _hold_not_found(hold_id)
    return holds.hold_card(hold)


@app.post("/api/holds/{hold_id}/rule")
def hold_rule(hold_id: str, body: RuleRequest) -> Any:
    """Rule on a hold: approve forwards the redacted text only, deny
    sends nothing. An approve returns the /api/send shape."""
    hold = holds.HOLDS.get(hold_id)
    if hold is None:
        return _hold_not_found(hold_id)
    if not body.approve:
        denied = holds.deny_hold(hold, body.reason)
        if denied is None:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "shadowfenced_hold_resolved",
                        "type": "shadowfenced_hold_resolved",
                        "message": f"hold {hold_id} is already {hold.status}",
                    },
                },
            )
        return holds.hold_card(denied)
    try:
        result = holds.approve_hold(
            hold,
            sender=_review_sender(
                [m for m in hold.messages if isinstance(m, dict)],
                hold.provider,
            ),
        )
    except UpstreamError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "shadowfenced_upstream",
                "type": "shadowfenced_upstream",
                "message": str(exc),
            },
        ) from exc
    if result is None:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "shadowfenced_hold_resolved",
                    "type": "shadowfenced_hold_resolved",
                    "message": f"hold {hold_id} is already {hold.status}",
                },
            },
        )
    decision = engine.Decision(
        decision="REDACT" if result["placeholders"] else "ALLOW",
        decision_id=result["mapping_id"],
        spans=hold.spans,
        sent_text=result["sent_text"],
        placeholders=result["placeholders"],
        resolved_tier=4,
        latency_ms={},
        reasons=["human approved redacted text -> send"],
        status="complete",
    )
    return _send_response(
        decision, result["answer"], result["highlights"], result["unmatched"],
        hold.provider,
    )


class WarnConfirmRequest(BaseModel):
    choice: str = ""  # send_anyway | send_generalised


def confirm_warn(decision_id: str, choice: str) -> dict[str, Any]:
    """Confirm a WARN: forward the original or generalised text upstream.

    Reuses the WARN decision id so the follow-up is tied to the warning
    the user saw. ``send_anyway`` forwards the gated ``sent_text``
    (redacted when placeholders exist); ``send_generalised`` forwards
    the coarsened generalised text. Nothing is forwarded for any other
    choice (422). The WARN mapping is consumed once.
    """
    record = _warn_store_load((decision_id or "").strip())
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "shadowfenced_unknown_warn",
                "type": "shadowfenced_unknown_warn",
                "message": f"unknown or expired warn {decision_id}",
            },
        )
    decision = record["decision"]
    if decision.decision != "WARN":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "shadowfenced_warn_resolved",
                "type": "shadowfenced_warn_resolved",
                "message": f"warn {decision_id} is already {decision.decision}",
            },
        )
    mode = (choice or "").strip().lower()
    if mode not in ("send_anyway", "send_generalised", "send-generalised",
                    "sendanyway", "generalised", "generalized"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "shadowfenced_bad_confirm",
                "type": "shadowfenced_bad_confirm",
                "message": "choice must be send_anyway or send_generalised",
            },
        )
    evidence = decision.evidence or {}
    reid = evidence.get("reid") or {}
    person = evidence.get("person_search") or {}
    generalised = person.get("generalised_text") or reid.get(
        "generalised_text") or ""
    provider = record.get("provider")
    generalise_modes = ("send_generalised", "send-generalised",
                        "generalised", "generalized")
    send_decision = "WARN"
    if mode in generalise_modes:
        if not generalised:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "shadowfenced_no_generalise",
                    "type": "shadowfenced_no_generalise",
                    "message": "no generalised text for this warning",
                },
            )
        messages = [{"role": "user", "content": generalised}]
        send_decision = "WARN"
        sent_text: str | None = generalised
        placeholders: dict[str, str] = {}
        mapping_id = decision.decision_id
    else:
        sent_text = decision.sent_text
        placeholders = dict(decision.placeholders or {})
        messages = _forward_messages(
            record.get("messages") or [], sent_text, "REDACT"
            if placeholders else None,
        )
        mapping_id = decision.decision_id
    try:
        answer = _send_upstream(messages, provider)
    except UpstreamError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "shadowfenced_upstream",
                "type": "shadowfenced_upstream",
                "message": str(exc),
            },
        ) from exc
    if placeholders:
        rehydrated, highlights, unmatched = rehydrate(answer, placeholders)
    else:
        rehydrated, highlights, unmatched = answer, [], []
    with _WARNS_LOCK:
        _WARNS.pop(decision.decision_id, None)
    confirmed = engine.Decision(
        decision=send_decision,
        decision_id=mapping_id,
        spans=decision.spans,
        sent_text=sent_text,
        placeholders=placeholders,
        resolved_tier=decision.resolved_tier,
        latency_ms=dict(decision.latency_ms),
        reasons=[*decision.reasons, f"user confirmed warn -> {mode}"],
        evidence=dict(evidence),
        status="complete",
        doc_type=decision.doc_type,
        doc_warning=decision.doc_warning,
    )
    try:
        from . import ecs_log
        ecs_log.append_decision(
            get_settings().log_path, confirmed, record.get("text") or "")
    except Exception:
        pass
    return _send_response(
        confirmed, rehydrated, highlights, unmatched, provider, [],
    )


@app.post("/api/warns/{decision_id}/confirm")
def warn_confirm(decision_id: str, body: WarnConfirmRequest) -> Any:
    """Confirm a WARN decision: send anyway or send generalised."""
    return confirm_warn(decision_id, body.choice)


@app.get("/api/keyguard")
def keyguard_status() -> dict[str, Any]:
    """Protected variable names and per-variable hit counts (no values)."""
    entries = keyguard_module.snapshot()
    return {
        "keys": entries,
        "protected_keys": [entry["name"] for entry in entries],
        "hits": {entry["name"]: entry["hits"] for entry in entries},
    }


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    """Decision counts and per-tier latency summaries from the decision log."""
    from . import stats as stats_module

    return stats_module.snapshot(get_settings().log_path)


def _handle_audio_upload(
    filename: str,
    data: bytes,
    *,
    transport: Any | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Transcribe one audio file, then scan the transcript like any upload.

    The transcript is stored and gated exactly like a document; any
    transcription failure stores an ``unscannable`` record so the send
    path HOLDs instead of passing unchecked audio.
    """
    from . import tier1
    from . import transcribe as transcribe_module
    from .extract import MAX_AUDIO_BYTES

    if transport is None:
        transport = getattr(app.state, "transcribe_transport", None)
    cfg = settings or get_settings()
    try:
        if len(data) > MAX_AUDIO_BYTES:
            raise FileTooLarge(f"file exceeds {MAX_AUDIO_BYTES} bytes")
        text = transcribe_module.transcribe_audio_sync(
            filename, data, settings=cfg, transport=transport
        )
    except (FileTooLarge, TranscribeError) as exc:
        record = UPLOADS.save(
            filename, "audio", "", status="unscannable",
        )
        return {
            "file_id": record.file_id,
            "name": filename,
            "kind": "audio",
            "chars": 0,
            "status": "unscannable",
            "spans": [],
            "summary": {},
            "summary_text": "Transcribed by Muse Voice · could not read file",
            "error": str(exc),
            "doc_type": None,
            "doc_warning": None,
        }
    if not text.strip():
        record = UPLOADS.save(
            filename, "audio", "", status="unscannable",
        )
        return {
            "file_id": record.file_id,
            "name": filename,
            "kind": "audio",
            "chars": 0,
            "status": "unscannable",
            "spans": [],
            "summary": {},
            "summary_text": "Transcribed by Muse Voice · could not read file",
            "error": "transcription returned no text",
            "doc_type": None,
            "doc_warning": None,
        }
    spans = tier1.scan(text, get_policy(), get_registry())
    try:
        audio_reid = _reid_scan_payload(text, None)
    except Exception:
        audio_reid = {"score": 0, "combination": [], "outcome": "OK",
                      "banner": "", "generalised_text": "",
                      "findings": []}
    status = ("flagged" if spans or audio_reid.get("outcome") != "OK"
              else "clean")
    record = UPLOADS.save(filename, "audio", text, status=status)
    summary: dict[str, int] = {}
    for span in spans:
        summary[span.cls] = summary.get(span.cls, 0) + 1
    if spans:
        from .redact import redact as _redact

        _, mapping = _redact(text, [s for s in spans if s.status == "proven"])
        summary_text = ", ".join(engine.describe_placeholders(mapping))
        if not summary_text:
            summary_text = f"{len(spans)} possible issue(s) need review"
    elif audio_reid.get("outcome") != "OK" and audio_reid.get("banner"):
        summary_text = str(audio_reid["banner"])
    else:
        summary_text = "clean"
    return {
        "file_id": record.file_id,
        "name": filename,
        "kind": "audio",
        "chars": len(text),
        "status": status,
        "spans": [span_to_dict(s) for s in spans],
        "reid": audio_reid,
        "summary": summary,
        "summary_text": "Transcribed by Muse Voice · " + summary_text,
        **_doc_fields(text),
    }


#: Honest "clean" wording when Glimmer saw text but neither pass found
#: anything sensitive (T55): an empty region answer must not mean "clean".
HONEST_CLEAN_TEXT = "clean — checked by Muse Glimmer on this PC"


def _handle_image_upload(filename: str, data: bytes, *, settings: Settings | None = None, glimmer_transport: Any | None = None, sam_transport: Any | None = None) -> dict[str, Any]:
    """Redact one screenshot/photo, or ask for consent first (T43).

    Hosted mode refuses (no local Glimmer). With redaction off, no
    bytes are analysed or stored: the payload carries the consent
    notice instead. Otherwise Glimmer finds regions locally, SAM 3.1
    sharpens them to masks when enabled ("on"), boxes are filled when
    SAM is off ("local") or fails, and only the redacted PNG is kept.
    Any text Glimmer read goes through the Tier 1 detectors. Failures
    store an ``unscannable`` record so the send path HOLDs.
    """
    from . import image_redact as image_redact_module
    from . import tier1
    from .extract import max_file_bytes

    cfg = settings or get_settings()
    if hosted_module.is_hosted():
        raise HTTPException(
            status_code=403,
            detail={
                "code": "shadowfenced_image_unavailable",
                "type": "shadowfenced_image_unavailable",
                "message": "image attachments are disabled in hosted demo mode",
            },
        )
    mode = image_redaction_mode(cfg)
    if mode == "off":
        return {
            "file_id": "",
            "name": filename,
            "kind": "image",
            "chars": 0,
            "status": "consent_required",
            "consent_required": True,
            "notice": image_redact_module.NOTICE_TEXT,
            "choices": ["on", "local", "off"],
            "spans": [],
            "summary": {},
            "summary_text": "image needs your choice before attaching",
            "regions": 0,
            "method": "none",
            "doc_type": None,
            "doc_warning": None,
        }
    if glimmer_transport is None:
        glimmer_transport = getattr(app.state, "glimmer_transport", None)
    if sam_transport is None:
        sam_transport = getattr(app.state, "sam_transport", None)
    try:
        if len(data) > max_file_bytes():
            raise FileTooLarge(f"file exceeds {max_file_bytes()} bytes")
        redacted = image_redact_module.redact_image(
            data,
            settings=cfg,
            glimmer_transport=glimmer_transport,
            sam_transport=sam_transport,
            sam_enabled=(mode == "on"),
            policy=get_policy(),
            registry=get_registry(),
        )
    except (FileTooLarge, image_redact_module.ImageRedactError) as exc:
        record = UPLOADS.save(
            filename, "image", "", status="unscannable",
        )
        return {
            "file_id": record.file_id,
            "name": filename,
            "kind": "image",
            "chars": 0,
            "status": "unscannable",
            "spans": [],
            "summary": {},
            "summary_text": "could not read image",
            "error": str(exc),
            "regions": 0,
            "method": "none",
            "doc_type": None,
            "doc_warning": None,
        }
    text = "\n".join(redacted.texts)
    spans = tier1.scan(text, get_policy(), get_registry())
    status = "flagged" if spans else "clean"
    record = UPLOADS.save(filename, "image", text, status=status)
    record.image_png = redacted.png
    record.regions = len(redacted.regions)
    record.method = redacted.method
    try:
        record.thumbnail = image_redact_module.png_data_url(
            image_redact_module.thumbnail_png(redacted.png)
        )
    except Exception:
        record.thumbnail = ""
    count = len(redacted.regions)
    region_word = f"{count} region{'s' if count != 1 else ''} hidden"
    summary: dict[str, int] = {}
    for span in spans:
        summary[span.cls] = summary.get(span.cls, 0) + 1
    if spans:
        from .redact import redact as _redact

        _, mapping = _redact(text, [s for s in spans if s.status == "proven"])
        extra = ", ".join(engine.describe_placeholders(mapping))
        summary_text = region_word + (f" ({extra})" if extra else " (needs review)")
    else:
        summary_text = region_word if count else HONEST_CLEAN_TEXT if redacted.saw_text else "clean"
    return {
        "file_id": record.file_id,
        "name": filename,
        "kind": "image",
        "chars": len(text),
        "status": status,
        "spans": [span_to_dict(s) for s in spans],
        "summary": summary,
        "summary_text": summary_text,
        "regions": count,
        "method": redacted.method,
        "method_label": redacted.method_label,
        "thumbnail": record.thumbnail,
        "doc_type": None,
        "doc_warning": None,
    }


def handle_upload(filename: str, data: bytes, *, transport: Any | None = None, settings: Settings | None = None, glimmer_transport: Any | None = None, sam_transport: Any | None = None) -> dict[str, Any]:
    """Validate, extract, scan, and store one uploaded file.

    Audio files are transcribed by Muse Voice Transcribe first; the
    transcript is then scanned like any other document. Images go
    through the T43 redaction flow (consent, Glimmer, SAM 3.1).

    Returns the ``/api/upload`` payload. Raises ``HTTPException`` 415
    for an unsupported suffix; oversized or unparseable input stores an
    ``unscannable`` record (the send path HOLDs on those).
    """
    from . import tier1

    try:
        if is_audio_file(filename):
            return _handle_audio_upload(
                filename, data, transport=transport, settings=settings
            )
        from . import image_redact as image_redact_module
        if image_redact_module.is_image_file(filename):
            return _handle_image_upload(
                filename, data, settings=settings,
                glimmer_transport=glimmer_transport,
                sam_transport=sam_transport,
            )
        kind, text = extract_text(filename, data)
    except UnsupportedType as exc:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "shadowfenced_unsupported_file",
                "type": "shadowfenced_unsupported_file",
                "message": str(exc),
            },
        ) from exc
    except (FileTooLarge, Unscannable) as exc:
        record = UPLOADS.save(
            filename, "unknown", "", status="unscannable",
        )
        return {
            "file_id": record.file_id,
            "name": filename,
            "kind": "unknown",
            "chars": 0,
            "status": "unscannable",
            "spans": [],
            "summary": {},
            "summary_text": "could not read file",
            "error": str(exc),
            "doc_type": None,
            "doc_warning": None,
        }
    spans = tier1.scan(text, get_policy(), get_registry())
    try:
        upload_reid = _reid_scan_payload(text, None)
    except Exception:
        upload_reid = {"score": 0, "combination": [], "outcome": "OK",
                       "banner": "", "generalised_text": "",
                       "findings": []}
    status = ("flagged" if spans or upload_reid.get("outcome") != "OK"
              else "clean")
    record = UPLOADS.save(filename, kind, text, status=status)
    summary: dict[str, int] = {}
    for span in spans:
        summary[span.cls] = summary.get(span.cls, 0) + 1
    if spans:
        from .redact import redact as _redact

        _, mapping = _redact(text, [s for s in spans if s.status == "proven"])
        summary_text = ", ".join(engine.describe_placeholders(mapping))
        if not summary_text:
            summary_text = f"{len(spans)} possible issue(s) need review"
    elif upload_reid.get("outcome") != "OK" and upload_reid.get("banner"):
        summary_text = str(upload_reid["banner"])
    else:
        summary_text = "clean"
    return {
        "file_id": record.file_id,
        "name": filename,
        "kind": kind,
        "chars": len(text),
        "status": status,
        "spans": [span_to_dict(s) for s in spans],
        "reid": upload_reid,
        "summary": summary,
        "summary_text": summary_text,
        **_doc_fields(text),
    }


@app.get("/api/image-redaction")
def image_redaction_status() -> dict[str, Any]:
    """Current image-redaction mode (T43 opt-in, off by default)."""
    from . import image_redact as image_redact_module

    hosted = hosted_module.is_hosted()
    mode = "off" if hosted else image_redaction_mode()
    return {
        "mode": mode,
        "hosted": hosted,
        "notice": image_redact_module.NOTICE_TEXT,
        "choices": ["on", "local", "off"],
    }


@app.post("/api/image-redaction/consent")
def image_redaction_consent(payload: dict[str, Any]) -> dict[str, Any]:
    """Store the consent-card choice for image attachments.

    ``"on"`` runs Glimmer locally plus cloud SAM 3.1 masks; ``"local"``
    keeps everything on this PC (Glimmer boxes only); ``"off"`` keeps
    asking on every image attach.
    """
    global _IMAGE_REDACTION_OVERRIDE
    choice = payload.get("choice") if isinstance(payload, dict) else None
    if choice not in ("on", "local", "off"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "shadowfenced_bad_choice",
                "type": "shadowfenced_bad_choice",
                "message": "choice must be one of: on, local, off",
            },
        )
    _IMAGE_REDACTION_OVERRIDE = choice
    return {"mode": choice}


@app.get("/api/uploads/{file_id}/image")
def upload_image(file_id: str) -> Response:
    """Return the redacted PNG bytes for one image upload.

    Only the redacted image is ever served; the original is never kept.
    """
    record = UPLOADS.load(file_id)
    if record is None or record.kind != "image" or not record.image_png:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "shadowfenced_no_image",
                "type": "shadowfenced_no_image",
                "message": "no redacted image for this file",
            },
        )
    return Response(content=record.image_png, media_type="image/png")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Accept one multipart file, extract its text, and scan it."""
    data = await file.read()
    return handle_upload(file.filename or "upload", data)


# --- voice dictation (T34) --------------------------------------------------


DICTATION_MAX_BYTES = 5 * 1024 * 1024
DICTATION_MAX_SECONDS = 60.0
DICTATION_SUFFIXES = (".webm", ".ogg", ".wav", ".m4a")


def _dictation_error(status_code: int, code: str, message: str) -> HTTPException:
    """Build a dictation error that never carries transcript text."""
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "type": code,
            "message": message,
        },
    )


def handle_transcribe(
    filename: str,
    data: bytes,
    duration: str | float | None = None,
    *,
    transport: Any | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Transcribe one short voice clip and return ``{"text": ...}``.

    Sync core of ``POST /api/transcribe`` so tests can call it
    without an event loop (mirrors :func:`handle_upload`). Raises
    ``HTTPException`` on every failure; the error never carries
    transcript text.
    """
    if hosted_module.is_hosted():
        cfg = settings or get_settings()
        if not cfg.hosted_dictation:
            raise _dictation_error(
                403,
                "shadowfenced_dictation_disabled",
                "dictation is disabled in hosted demo mode",
            )
    if duration not in (None, ""):
        try:
            seconds = float(duration)
        except (TypeError, ValueError):
            raise _dictation_error(
                400,
                "shadowfenced_bad_duration",
                "duration must be a number of seconds",
            ) from None
        if seconds > DICTATION_MAX_SECONDS:
            raise _dictation_error(
                413,
                "shadowfenced_audio_too_long",
                "audio clip exceeds 60 seconds",
            )
    if not data:
        raise _dictation_error(
            400,
            "shadowfenced_empty_audio",
            "audio clip is empty",
        )
    if len(data) > DICTATION_MAX_BYTES:
        raise _dictation_error(
            413,
            "shadowfenced_audio_too_large",
            "audio clip exceeds 5 MB",
        )
    lowered = (filename or "").lower()
    if not any(lowered.endswith(suffix) for suffix in DICTATION_SUFFIXES):
        raise _dictation_error(
            415,
            "shadowfenced_unsupported_audio",
            "unsupported audio type; use webm, ogg, wav, or m4a",
        )
    from . import transcribe as transcribe_module

    if transport is None:
        transport = getattr(app.state, "transcribe_transport", None)
    try:
        text_out = transcribe_module.transcribe_audio_sync(
            filename, data, settings=settings or get_settings(),
            transport=transport,
        )
    except TranscribeError as exc:
        raise _dictation_error(
            502,
            "shadowfenced_transcribe_failed",
            f"transcription failed: {exc}",
        ) from exc
    return {"text": text_out}


@app.post("/api/transcribe")
async def transcribe_clip(
    file: UploadFile = File(...),
    duration: str | None = Form(default=None),
) -> dict[str, Any]:
    """Transcribe one short voice clip with Muse Voice Transcribe.

    Accepts a single audio clip (``webm``/``ogg``/``wav``/``m4a``),
    at most 60 seconds and 5 MB, and returns ``{"text": ...}``.
    The transcript is never forwarded to any chat model here; the
    caller inserts it into the composer so the normal live scan
    runs before anything is sent. Every failure (including
    timeouts) raises a clear error with no ``text`` field.

    Disabled in hosted demo mode unless ``SF_HOSTED_DICTATION=1``.
    """
    data = await file.read()
    return handle_transcribe(file.filename or "", data, duration)


@app.get("/api/providers")
def providers() -> dict[str, Any]:
    """List the upstream providers with availability and the default."""
    return {"providers": provider_list(), "default": default_provider()}


@app.post("/api/provider")
def set_provider(body: ProviderRequest) -> dict[str, Any]:
    """Set the default upstream provider (any registry id)."""
    global _PROVIDER_OVERRIDE
    name = (body.id or "").strip().lower()
    if providers_registry.get_provider(name) is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "shadowfenced_unknown_provider",
                "type": "shadowfenced_unknown_provider",
                "message": f"unknown provider {body.id!r}",
            },
        )
    _PROVIDER_OVERRIDE = name
    return {"default": name, "providers": provider_list()}


def _provider_or_404(provider_id: str) -> Any:
    """Registry entry for one provider id (404 when unknown)."""
    provider = providers_registry.get_provider(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "shadowfenced_unknown_provider",
                "type": "shadowfenced_unknown_provider",
                "message": f"unknown provider {provider_id!r}",
            },
        )
    return provider


def _refuse_hosted_storage() -> None:
    """Reject key storage in hosted demo mode (storing keys is disabled)."""
    if hosted_module.is_hosted():
        raise HTTPException(
            status_code=403,
            detail={
                "code": "shadowfenced_hosted_keys_disabled",
                "type": "shadowfenced_hosted_keys_disabled",
                "message": "key storage is disabled in the hosted demo",
            },
        )


def _probe_provider(provider: Any) -> tuple[bool, str]:
    """One tiny request against a provider; returns (ok, message).

    Only ok/error text is returned: fixed strings, status codes, and
    exception type names -- never the key and never response bodies.
    """
    import httpx

    from .upstream import _verify_for as _upstream_verify

    transports = getattr(app.state, "provider_test_transports", None) or {}
    transport = transports.get(provider.id) or _transport()
    if provider.needs_key and not providers_registry.is_connected(provider.id):
        return False, "no API key stored"
    if provider.id == "ollama":
        url = get_settings().ollama_url.rstrip("/") + "/api/version"
        headers: dict[str, str] = {}
    else:
        url = provider.base_url.rstrip("/") + "/models"
        key = providers_registry.get_key(provider.id)
        headers = {"Authorization": "Bearer " + key} if key else {}
    try:
        with httpx.Client(
            transport=transport, timeout=15.0, verify=_upstream_verify(url)
        ) as client:
            response = client.get(url, headers=headers)
    except Exception as exc:
        return False, f"connection failed: {type(exc).__name__}"
    if response.status_code == 200:
        return True, "connection ok"
    if response.status_code in (401, 403):
        return False, "provider rejected the key"
    return False, f"provider returned status {response.status_code}"


@app.post("/api/providers/{provider_id}/key")
def store_provider_key(provider_id: str, body: ProviderKeyRequest) -> dict[str, Any]:
    """Store one provider's API key in the OS credential store.

    Falls back to the git-ignored ``.env`` file only when no keyring
    backend exists (see the ``storage`` field). The key is never
    returned, logged, or placed in traces.
    """
    _refuse_hosted_storage()
    provider = _provider_or_404(provider_id)
    if not provider.needs_key:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "shadowfenced_no_key_needed",
                "type": "shadowfenced_no_key_needed",
                "message": f"provider {provider.id!r} needs no key",
            },
        )
    if not (body.key or "").strip():
        raise HTTPException(
            status_code=422,
            detail={
                "code": "shadowfenced_empty_key",
                "type": "shadowfenced_empty_key",
                "message": "empty key",
            },
        )
    backend = providers_registry.store_key(provider.id, body.key)
    return {"ok": True, "connected": True, "storage": backend}


@app.delete("/api/providers/{provider_id}/key")
def delete_provider_key(provider_id: str) -> dict[str, Any]:
    """Forget one provider's stored key (OS store and ``.env`` fallback)."""
    _refuse_hosted_storage()
    provider = _provider_or_404(provider_id)
    providers_registry.delete_key(provider.id)
    return {
        "ok": True,
        "connected": providers_registry.is_connected(provider.id),
    }


@app.post("/api/providers/{provider_id}/test")
def check_provider_connection(provider_id: str) -> dict[str, Any]:
    """One tiny request against a provider; ok/error text only."""
    provider = _provider_or_404(provider_id)
    ok, message = _probe_provider(provider)
    return {"ok": ok, "message": message}


@app.post("/api/providers/openrouter/oauth/start")
def start_openrouter_oauth() -> dict[str, Any]:
    """Begin the OpenRouter PKCE login (callback stays on 127.0.0.1)."""
    _refuse_hosted_storage()
    try:
        return providers_registry.start_oauth_session()
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "shadowfenced_hosted_keys_disabled",
                "type": "shadowfenced_hosted_keys_disabled",
                "message": str(exc),
            },
        ) from exc


def _finish_openrouter_oauth(code: str, session_id: str) -> dict[str, Any]:
    """Exchange one OAuth code and store the returned key (never echoed)."""
    _refuse_hosted_storage()
    transports = getattr(app.state, "provider_test_transports", None) or {}
    transport = transports.get("openrouter") or _transport()
    try:
        backend = providers_registry.exchange_code(
            code, session_id, transport=transport
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "shadowfenced_hosted_keys_disabled",
                "type": "shadowfenced_hosted_keys_disabled",
                "message": str(exc),
            },
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "shadowfenced_unknown_oauth_session",
                "type": "shadowfenced_unknown_oauth_session",
                "message": "unknown or expired login session",
            },
        ) from exc
    except ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "shadowfenced_upstream",
                "type": "shadowfenced_upstream",
                "message": "key exchange unreachable",
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "shadowfenced_oauth_exchange_failed",
                "type": "shadowfenced_oauth_exchange_failed",
                "message": "key exchange failed",
            },
        ) from exc
    return {"ok": True, "connected": True, "storage": backend}


@app.get("/api/providers/openrouter/oauth/callback")
def openrouter_oauth_callback(
    code: str = "", session_id: str = ""
) -> dict[str, Any]:
    """Loopback OAuth callback (127.0.0.1): swap ``code`` for a key."""
    return _finish_openrouter_oauth(code, session_id)


@app.post("/api/providers/openrouter/oauth/exchange")
def exchange_openrouter_oauth(body: ProviderOAuthExchange) -> dict[str, Any]:
    """Headless variant: paste the code OpenRouter shows on screen."""
    return _finish_openrouter_oauth(body.code, body.session_id)


@app.get("/api/log/verify")
def log_verify() -> dict[str, Any]:
    """Verify the hash-chained decision log; return ``{ok, events}``."""
    from . import ecs_log

    ok, count = ecs_log.verify(get_settings().log_path)
    return {"ok": ok, "events": count}


@app.get("/api/log/events")
def log_events(limit: int = 50) -> dict[str, Any]:
    """Return recent decision-log entries plus the chain-verified status.

    Each entry carries ``ts``, ``decision_id``, ``decision`` (outcome),
    ``tier``, ``reasons`` (class labels) and ``fingerprints``
    (``sha256(value)[:12]``) only -- raw values are never written to
    the log and never returned here.
    """
    import json as _json

    from . import ecs_log

    ok, _count = ecs_log.verify(get_settings().log_path)
    clamped = max(1, min(int(limit), 200))
    path = Path(ecs_log.resolve_path(get_settings().log_path))
    rows: list[dict[str, Any]] = []
    if path.is_file():
        try:
            with path.open(encoding="utf-8") as handle:
                lines = [line.strip() for line in handle if line.strip()]
        except OSError:
            lines = []
        for line in lines[-clamped:]:
            try:
                record = _json.loads(line)
            except ValueError:
                continue
            rows.append(
                {
                    "ts": record.get("ts"),
                    "decision_id": record.get("decision_id"),
                    "decision": record.get("decision"),
                    "tier": record.get("tier"),
                    "reasons": record.get("classes", []),
                    "fingerprints": record.get("fingerprints", []),
                }
            )
    return {"ok": ok, "events": rows}


@app.get("/api/guard")
def guard_status() -> dict[str, Any]:
    """Report whether the local guard model is reachable.

    ``glimmer`` is True when the local model endpoint answers for the
    configured judge model; otherwise the gate still runs its code
    checks (``glimmer`` False). Never raises: any failure means
    code-checks-only.
    """
    import httpx

    settings = get_settings()
    reachable = False
    try:
        url = settings.ollama_url.rstrip("/") + "/api/show"
        with httpx.Client(timeout=2.0) as client:
            response = client.post(url, json={"name": settings.judge_model})
        reachable = response.status_code == 200
    except Exception:
        reachable = False
    return {"guard": "on", "glimmer": reachable}


def _stream_response(
    decision: engine.Decision, model: str, content: str
) -> StreamingResponse:
    import json

    chunk = {
        "id": "chatcmpl-" + uuid.uuid4().hex[:12],
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "shadowfenced": _shadowfenced_field(decision),
    }
    body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
    return StreamingResponse(
        iter([body]),
        media_type="text/event-stream",
        headers={"X-Shadowfenced-Decision": decision.decision},
    )


from .ext_api import mount_ext_api
from .rules import router as rules_router

app.include_router(rules_router)
mount_ext_api(app)
# include_router wraps the app lifespan even when the router has none;
# restore the original only when it was actually wrapped, so startup
# behaviour (and its identity) is unchanged on old and new FastAPI.
if getattr(app.router, "lifespan_context", lifespan) is not lifespan:
    app.router.lifespan_context = lifespan

for _candidate in (
    Path("static"),
    Path(__file__).resolve().parent / "static",
):
    if _candidate.is_dir():
        app.mount("/", StaticFiles(directory=str(_candidate), html=True), name="static")
        break


def print_startup_banner() -> None:
    """Print the hosted demo banner once at startup (hosted mode only)."""
    if hosted_module.is_hosted():
        print(f"Shadowfenced: {hosted_module.HOSTED_BANNER}")


def main() -> None:
    """Run the proxy on 127.0.0.1:8765."""
    import uvicorn

    print_startup_banner()
    uvicorn.run(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
