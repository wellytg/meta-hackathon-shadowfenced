# P1 — Design gate: Shadowfenced

**Status:** approved for build on 2026-10-31 ("run the full design protocol ... one continuous run").
**One line:** a local layer between any person and any AI. It underlines sensitive text as you type
(like a grammar checker), checks uploaded files the same way, and lets only clean or redacted text cross
the organisation's boundary.

## View 1 — Outside (black box)

```
   INSIDE THE ORGANISATION                 │ BOUNDARY │          OUTSIDE
  People typing prompts ───┐               │ ┌──────┐ │   ┌──────────────────────┐
  Files being uploaded ────┼──────────────►│ │SHADOW│ │──►│ Any AI over an API:  │
  Pasted text / logs ──────┘               │ │FENCED│ │   │ Meta, or any         │
                                           │ └──────┘ │   │ OpenAI-compatible    │
                                           │          │   └──────────────────────┘
  MUST NOT CROSS: passwords, API keys, tokens · personal data · internal codenames and
  client names · confidential documents · beacons (text that points to where sensitive data is)
```

## View 2 — Inside the organisation (what is protected)

| Class | Examples (synthetic only) | Detected by |
|---|---|---|
| Secrets | API keys, tokens, passwords, canaries | Tier 1 patterns, entropy, canary match |
| People data | name + ID, SSN-shaped numbers, emails, phones | Tier 1 patterns, Tier 2 judgement |
| Business data | codenames, client names, unreleased figures | Org policy word list, Tier 2 judgement |
| Beacons | "removed the keys from config.py" | Tier 2 judgement, verified against context |

The org policy file (`policy.yaml`) lists codenames, client names, patterns and the action for each class.
Canaries are planted fake secrets with the prefix `SF_CANARY_`, generated at runtime from a seed. No string in a
real provider's key format is ever committed.

## View 3 — Process

```
 1. USER TYPES ──► 2. LIVE SCAN (debounced) ──► 3. UNDERLINE + TOOLTIP ("Looks like an API key. Redact / Keep / Why?")
                     Tier 1 on every pause (<20 ms); Tier 2-live on a longer pause
 4. USER UPLOADS FILE ──► text extracted ──► same scan ──► same warning on the file chip
 5. USER PRESSES SEND ──► 6. FINAL GATE (authoritative, fails closed)
        ALLOW (clean) │ REDACT ([SECRET_1] placeholders) │ BLOCK (proven) │ HOLD (ask a human, evidence card)
 7. ANY AI ANSWERS ──► 8. placeholders swapped back locally (the AI never saw the real values)
```

## View 4 — Engine (System One-style typed decisions)

```
 text ─► TIER 1  code: patterns, canaries, entropy, encodings, policy words ──clean──► ALLOW
           │ hit = proven ────────────────────────────────────────────────────────► REDACT / BLOCK
           │ ambiguous spans only
           ▼
         TIER 2  local Meta model, typed answer (strict JSON schema):
                 live: Llama 3.2 3B (fast)   ·   at send: Muse Glimmer 30B (stronger, slower, partly on CPU)
                 {class: Choice, sensitivity: Score 0-1, is_beacon: true/false, span, confidence}
           ▼
         TIER 3  code verifies the model's claim (span exists, matches a class rule, decodes to a canary ...)
           │ confirmed ──────────────────────────────────────────────────────────► REDACT / BLOCK
           ▼ unconfirmed but suspicious
         TIER 4  human: evidence card separates "proven by code" from "model suspicion only"

 Every decision is logged (tier, latency, outcome) to a hash-chained JSONL file.
 Any tier down or timed out ─► HOLD. Never allow by default.
```

**Typed-decision pattern:** borrowed from TypeSafe's System One / Jev idea (Choice, Score, true/false with
confidence). Jev itself is not used: it is a hosted service, so the text being checked would leave the machine.

## Model routing

| Role | Model | Where | Why |
|---|---|---|---|
| Live underline (Tier 2-live) | Llama 3.2 3B | Ollama, local GPU | Fast enough for typing |
| Send-time judge (Tier 2-send) | Muse Glimmer 30B (Q4) | Ollama, local GPU + CPU | Stronger judgement; Open Weight bonus |
| Upstream "any AI" | Muse Spark via Meta Model API (key in `.env`) | Cloud, sees redacted text only | Shows the layer works in front of a hosted AI |
| Stand-in upstream | Any Ollama model via the OpenAI-compatible endpoint | Local | Used when no key is present; demo shows the swap |

## Delivery surface
A local proxy (FastAPI) with an OpenAI-compatible `/v1/chat/completions` endpoint, so any API client can point at
it, plus a demo web chat with live underlines and file upload. A browser extension is out of scope (roadmap slide).

## Measures
Leak rate, false-escalation rate, per-tier latency and the share of inputs resolved by each tier, computed on a
synthetic corpus. Catch rate is always reported next to false alarms.

## Out of scope
Browser extension, response scanning, the coding-agent egress gate (closing slide: "the same engine guards agents").
