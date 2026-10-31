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

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import engine
from . import holds
from . import judge as judge_module
from .canary import CanaryRegistry
from .config import get_settings
from .extract import (
    FileTooLarge,
    Unscannable,
    UnsupportedType,
    extract_text,
)
from .policy import Policy, load_policy
from .redact import load_mapping, rehydrate
from .spans import Span, span_from_cp_range
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
    global _POLICY, _REGISTRY
    _POLICY = None
    _REGISTRY = None


# In-memory default provider override set by POST /api/provider
# (reset between tests via reset_provider_override()).
_PROVIDER_OVERRIDE: str | None = None


def reset_provider_override() -> None:
    """Forget the POST /api/provider override (tests only)."""
    global _PROVIDER_OVERRIDE
    _PROVIDER_OVERRIDE = None


def default_provider() -> str:
    """Effective default provider: explicit override, else settings."""
    if _PROVIDER_OVERRIDE in ("meta", "ollama"):
        return _PROVIDER_OVERRIDE
    return get_settings().upstream or "ollama"


def resolve_provider(provider: str | None) -> str:
    """Per-request provider: explicit choice, else the default."""
    name = (provider or "").strip().lower()
    if name in ("meta", "ollama"):
        return name
    return default_provider()


PROVIDER_LABELS = {
    "meta": "Meta Muse Spark (cloud)",
    "ollama": "Llama 3.2 3B via Ollama (local stand-in)",
}


def provider_list() -> list[dict[str, Any]]:
    """Serialise the provider catalogue for GET /api/providers."""
    settings = get_settings()
    return [
        {
            "id": "meta",
            "label": PROVIDER_LABELS["meta"],
            "available": settings.meta_available,
        },
        {"id": "ollama", "label": PROVIDER_LABELS["ollama"], "available": True},
    ]


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
    return {
        "decision": decision.decision,
        "decision_id": decision.decision_id,
        "resolved_tier": decision.resolved_tier,
        "latency_ms": decision.latency_ms,
    }


def _forward_messages(
    messages: list[dict[str, Any]],
    sent_text: str | None,
    decision: str | None = None,
) -> list[dict[str, Any]]:
    """Rebuild upstream messages with the gated user text only.

    On REDACT the redacted text goes upstream with a leading system note
    explaining the placeholders; ALLOW forwards plain user text.
    """
    kept = [m for m in messages if isinstance(m, dict) and m.get("role") != "user"]
    if decision == "REDACT" and sent_text is not None:
        kept.insert(0, {"role": "system", "content": REDACT_NOTE})
    if sent_text is not None:
        kept.append({"role": "user", "content": sent_text})
    return kept


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


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness probe plus the live-hints flag for the UI."""
    return {"status": "ok", "live_hints": get_settings().live_hints}


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
    spans = tier1.scan(request.text, get_policy(), get_registry())
    tier1_ms = (time.perf_counter() - started) * 1000
    latency_ms: dict[str, float] = {"tier1": tier1_ms}
    if request.mode == "live" and get_settings().live_hints:
        tier2_started = time.perf_counter()
        spans = _with_live_suspicions(request.text, spans)
        latency_ms["tier2"] = (time.perf_counter() - tier2_started) * 1000
    latency_ms["total"] = (time.perf_counter() - started) * 1000
    return {
        "spans": [span_to_dict(s) for s in spans],
        "latency_ms": latency_ms,
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
    text, clean = extract_user_text(body.messages)
    file_texts, file_error = _file_sections(body.file_ids)
    if file_error is not None:
        decision = engine.gate(text, get_policy(), get_registry(), mode="send")
        decision = _hold_for(decision, file_error)
        _log_decision(decision, text)
        _register_hold(decision, text, body.messages, provider, why=file_error)
        return _send_response(decision, None, [], [], provider)
    if file_texts:
        text = text + "".join(file_texts) if text else "".join(file_texts).lstrip("\n")
    decision = engine.gate(
        text, get_policy(), get_registry(),
        [c.model_dump() for c in body.choices],
        mode="send",
    )
    if not clean:
        decision = _hold_for(decision, "non_text_content")
        _log_decision(decision, text)
        _register_hold(
            decision, text, body.messages, provider,
            why="non-text content parts need review",
        )
        return _send_response(decision, None, [], [], provider)
    _log_decision(decision, text)
    if decision.decision in ("BLOCK", "HOLD"):
        if decision.decision == "HOLD":
            _register_hold(decision, text, body.messages, provider)
        return _send_response(decision, None, [], [], provider)
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
    return _send_response(decision, rehydrated, highlights, unmatched, provider)


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
        status=decision.status,
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
            )
    except Exception:
        pass


def _send_response(
    decision: engine.Decision,
    answer: str | None,
    highlights: list[dict],
    unmatched: list[str],
    provider: str | None = None,
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
        "evidence": evidence,
        "resolved_tier": decision.resolved_tier,
        "latency_ms": decision.latency_ms,
        "unmatched_placeholders": unmatched,
        "provider": resolve_provider(provider),
    }
    if decision.decision == "HOLD":
        response["review_url"] = f"/api/holds/{decision.decision_id}"
    return response


@app.post("/v1/chat/completions")
def chat_completions(body: ChatRequest) -> Any:
    """OpenAI-compatible completions behind the Shadowfenced gate."""
    text, clean = extract_user_text(body.messages)
    if not clean:
        decision = engine.gate(text, get_policy(), get_registry(), mode="send")
        decision = _hold_for(decision, "non-text content parts need review")
        _log_decision(decision, text)
        _register_hold(
            decision, text, body.messages, None,
            why="non-text content parts need review",
        )
        return _hold_response(decision, "non-text content parts need review")
    decision = engine.gate(text, get_policy(), get_registry(), mode="send")
    _log_decision(decision, text)
    if decision.decision == "HOLD":
        _register_hold(decision, text, body.messages, None)
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
    if decision.decision == "HOLD":
        return _hold_response(decision, "ambiguous spans need human review")
    try:
        answer = _send_upstream(
            _forward_messages(
                body.messages, decision.sent_text, decision.decision
            ),
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


def _hold_response(decision: engine.Decision, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "shadowfenced_hold",
                "type": "shadowfenced_hold",
                "message": message,
            },
            "review_url": f"/api/holds/{decision.decision_id}",
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


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    """Decision counts and per-tier latency summaries from the decision log."""
    from . import stats as stats_module

    return stats_module.snapshot(get_settings().log_path)


def handle_upload(filename: str, data: bytes) -> dict[str, Any]:
    """Validate, extract, scan, and store one uploaded file.

    Returns the ``/api/upload`` payload. Raises ``HTTPException`` 415
    for an unsupported suffix; oversized or unparseable input stores an
    ``unscannable`` record (the send path HOLDs on those).
    """
    from . import tier1

    try:
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
        }
    spans = tier1.scan(text, get_policy(), get_registry())
    status = "flagged" if spans else "clean"
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
    else:
        summary_text = "clean"
    return {
        "file_id": record.file_id,
        "name": filename,
        "kind": kind,
        "chars": len(text),
        "status": status,
        "spans": [span_to_dict(s) for s in spans],
        "summary": summary,
        "summary_text": summary_text,
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Accept one multipart file, extract its text, and scan it."""
    data = await file.read()
    return handle_upload(file.filename or "upload", data)


@app.get("/api/providers")
def providers() -> dict[str, Any]:
    """List the upstream providers with availability and the default."""
    return {"providers": provider_list(), "default": default_provider()}


@app.post("/api/provider")
def set_provider(body: ProviderRequest) -> dict[str, Any]:
    """Set the default upstream provider (``meta`` or ``ollama``)."""
    global _PROVIDER_OVERRIDE
    name = (body.id or "").strip().lower()
    if name not in ("meta", "ollama"):
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


@app.get("/api/log/verify")
def log_verify() -> dict[str, Any]:
    """Verify the hash-chained decision log; return ``{ok, events}``."""
    from . import ecs_log

    ok, count = ecs_log.verify(get_settings().log_path)
    return {"ok": ok, "events": count}


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


for _candidate in (
    Path("static"),
    Path(__file__).resolve().parent / "static",
):
    if _candidate.is_dir():
        app.mount("/", StaticFiles(directory=str(_candidate), html=True), name="static")
        break


def main() -> None:
    """Run the proxy on 127.0.0.1:8765."""
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
