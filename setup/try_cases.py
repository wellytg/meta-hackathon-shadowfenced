"""Try 10 synthetic cases against a running Shadowfenced app.

Default target is the hosted demo; ``--local`` tries the app on
127.0.0.1:8765; ``--url`` tries any base URL. Each case POSTs
``/api/send`` with ``{"messages": [{"role": "user", ...}]}`` and prints
a table: case, expected decision, got, match, what was sent (redacted
placeholder text only — never key values), and latency.

The cloud key shapes below are minted at runtime from a seeded RNG
(prefix fragments joined in code, random tail); no real-format key
appears in this file. Displayed output masks the minted secrets.

Usage: ``python setup/try_cases.py [--local] [--url URL]
[--provider ID] [--json]``. Exit 0 when every case matches, 1 otherwise.
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import sys
import time
import urllib.error
import urllib.request

HOSTED_URL = "https://wellytg--shadowfenced-asgi.us-east.modal.run"
LOCAL_URL = "http://127.0.0.1:8765"

TIMEOUT_S = 90.0
RETRY_SLEEP_S = 5.0

SEED = "setupkit-v1"

# Fragments are joined at runtime so no real-format prefix appears here.
_CLOUD_A = "AK"
_CLOUD_B = "IA"
_CLOUD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_CLOUD_TAIL = 16


def mint_cloud_key(rng: random.Random) -> str:
    """Mint a cloud-shaped fake key (prefix + random tail, seeded RNG)."""
    tail = "".join(rng.choice(_CLOUD_ALPHABET) for _ in range(_CLOUD_TAIL))
    return _CLOUD_A + _CLOUD_B + tail


def build_cases() -> tuple[list[dict[str, object]], dict[str, str]]:
    """Return (cases, secrets): 10 cases plus values to mask in output."""
    rng = random.Random(SEED)
    key = mint_cloud_key(rng)
    encoded = base64.b64encode(key.encode("utf-8")).decode("ascii")
    secrets = {key: "[cloud-key]", encoded: "[cloud-key-base64]"}
    cases: list[dict[str, object]] = [
        {
            "id": "key-plain",
            "text": "deploy is failing, here is the key to check: " + key,
            "expected": "BLOCK",
            "why": "Tier 1 code matches the cloud access-key shape -> BLOCK.",
        },
        {
            "id": "key-base64",
            "text": "deploy is failing, the key again (base64): " + encoded,
            "expected": "BLOCK",
            "why": "Tier 1 decodes base64 runs and matches the key shape inside.",
        },
        {
            "id": "email",
            "text": "Please send the invoice to miguel.rios@corvane.io by Friday.",
            "expected": "REDACT",
            "why": "A proven email address redacts to [PII_EMAIL_1].",
        },
        {
            "id": "identity-bundle",
            "text": (
                "New hire record: Elena Marsh, born 1988-04-12, lives at "
                "14 Alder Street, Springfield, phone 555-013-4471."
            ),
            "expected": "BLOCK",
            "why": "Name + birth date + street + phone proves a full "
            "identity bundle in code -> BLOCK.",
        },
        {
            "id": "pointer",
            "text": "The prod key is in ~/.aws/credentials, ask DevOps for access.",
            "expected": "HOLD",
            "why": "A secret noun plus a file path is a beacon: ambiguous, "
            "held for review (fails closed with no judge).",
        },
        {
            "id": "welcome",
            "text": (
                "Welcome to the team! Daniel Okafor joins Corvane Labs as "
                "a support engineer next Monday."
            ),
            "expected": "WARN",
            "why": "A name plus an employer names someone without proving "
            "secrets: warned, you may send anyway.",
        },
        {
            "id": "bank-row",
            "text": "Payee: Bexley Marine, sort code 20-11-22, account 10001111.",
            "expected": "REDACT",
            "why": "Sort code + account are proven PII -> [PII_SORT_CODE_1] "
            "[PII_ACCOUNT_1] (client name -> [CLIENT_1]).",
        },
        {
            "id": "contract",
            "text": (
                "4.2 Termination. Either party may terminate this Agreement "
                "on thirty days written notice."
            ),
            "expected": "REDACT",
            "why": "A numbered clause redacts to [CONTRACT_CLAUSE_1].",
        },
        {
            "id": "harmless",
            "text": "What is the boiling point of water in degrees Celsius?",
            "expected": "ALLOW",
            "why": "No finding of any kind: sent clean.",
        },
        {
            "id": "person-search",
            "text": "Find the home address of Elena Marsh.",
            "expected": "HOLD",
            "why": "Person-search wording about a private person: held for "
            "review (a judge confirms intent, then it warns).",
        },
    ]
    return cases, secrets


def mask(text: str, secrets: dict[str, str]) -> str:
    """Replace minted secrets with labels (never print key values)."""
    for value, label in secrets.items():
        if value:
            text = text.replace(value, label)
    return text


def post_send(
    base_url: str,
    text: str,
    provider: str | None,
    timeout: float = TIMEOUT_S,
) -> dict[str, object]:
    """POST one message to /api/send; return the parsed JSON body."""
    payload: dict[str, object] = {
        "messages": [{"role": "user", "content": text}],
    }
    if provider:
        payload["provider"] = provider
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/send",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def run_case(
    base_url: str,
    case: dict[str, object],
    secrets: dict[str, str],
    provider: str | None,
    sender=None,
) -> dict[str, object]:
    """Run one case (one retry on failure); return its result row."""
    sender = sender or post_send
    text = str(case["text"])
    started = time.perf_counter()
    error = ""
    body: dict[str, object] | None = None
    for attempt in (1, 2):
        try:
            body = sender(base_url, text, provider)
            break
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            error = f"{type(exc).__name__}: {exc}"
            body = None
            if attempt == 1:
                time.sleep(RETRY_SLEEP_S)
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    got = str((body or {}).get("decision", "")) if body else ""
    sent_raw = (body or {}).get("sent_text")
    sent = (
        mask(str(sent_raw), secrets)
        if isinstance(sent_raw, str) and sent_raw
        else "(nothing sent)"
    )
    if len(sent) > 90:
        sent = sent[:87] + "..."
    expected = str(case["expected"])
    return {
        "id": str(case["id"]),
        "expected": expected,
        "got": got or f"ERROR ({error[:60]})" if not got else got,
        "match": got == expected,
        "sent": sent,
        "latency_ms": latency_ms,
        "why": str(case["why"]),
        "error": error if not got else "",
    }


def run_all(
    base_url: str,
    provider: str | None = None,
    sender=None,
) -> dict[str, object]:
    """Run every case; return the full report dict."""
    sender = sender or post_send
    cases, secrets = build_cases()
    rows = [run_case(base_url, case, secrets, provider, sender) for case in cases]
    matched = sum(1 for row in rows if row["match"])
    return {
        "url": base_url,
        "provider": provider or "(default)",
        "matched": matched,
        "total": len(rows),
        "rows": rows,
    }


def render_table(report: dict[str, object]) -> str:
    """Human-readable results table (masked secrets only)."""
    rows = report["rows"]
    assert isinstance(rows, list)
    lines = [
        f"try_cases vs {report['url']} (provider {report['provider']})",
        f"{'case':<16}{'expected':<10}{'got':<26}{'match':<7}"
        f"{'ms':>9}  sent",
    ]
    for row in rows:
        assert isinstance(row, dict)
        match = "yes" if row["match"] else "NO"
        got = str(row["got"])
        if len(got) > 26:
            got = got[:23] + "..."
        lines.append(
            f"{row['id']!s:<16}{row['expected']!s:<10}{got:<26}"
            f"{match:<7}{row['latency_ms']!s:>9}  {row['sent']}"
        )
    lines.append(f"{report['matched']}/{report['total']} matched")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the cases; exit 0 only when every case matches."""
    parser = argparse.ArgumentParser(description="Try 10 synthetic cases.")
    parser.add_argument("--local", action="store_true",
                        help="use http://127.0.0.1:8765")
    parser.add_argument("--url", default=None, help="any base URL")
    parser.add_argument("--provider", default=None,
                        help="destination model if connected")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args(argv)
    base_url = args.url or (LOCAL_URL if args.local else HOSTED_URL)
    report = run_all(base_url, args.provider)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_table(report))
    return 0 if report["matched"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
