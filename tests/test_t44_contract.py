"""T44 tests: contract-clause protection ([CONTRACT_CLAUSE_n]).

All company names, people, emails and contract language below are
invented for tests. Numbered NDA excerpts redact to placeholders and
restore locally; a stamped confidential MSA holds; a general legal
question and short quotes stay clean.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from shadowfenced import holds as holds_module
from shadowfenced import tier1
from shadowfenced.engine import gate, user_message
from shadowfenced.policy import load_policy
from shadowfenced.redact import PLACEHOLDER_RE, rehydrate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def policy():
    return load_policy(ROOT / "policy.yaml")


NDA_EXCERPT = (
    "4.1 Non-Disclosure. The Receiving Party shall not disclose any private "
    "information received from the Disclosing Party to any third party "
    "without prior written consent.\n"
    "4.2 Termination. Either party may terminate this arrangement on thirty "
    "days written notice, notwithstanding any prior understanding between "
    "the parties.\n"
    "Section 7 \u2013 Return of Materials. Upon termination the Receiving "
    "Party shall return all private information received under this "
    "arrangement promptly."
)

MSA_CONFIDENTIAL = (
    "CONFIDENTIAL \u2014 DO NOT DISTRIBUTE\n"
    "\n"
    "Master Services Agreement\n"
    "This agreement is entered into by and between Northgate Systems "
    "(the Supplier) and Lakeside Retail (the Customer).\n"
    "1. Services. The Supplier shall provide the services described in the "
    "applicable statement of work.\n"
    "2. Indemnification. Each party shall indemnify the other against "
    "third-party claims arising from its own negligence.\n"
    "9. Governing Law. This agreement shall be governed by the laws of the "
    "State of Ohio, notwithstanding any conflict-of-law principles."
)

MSA_PLAIN = (
    "Master Services Agreement\n"
    "This agreement is entered into by and between Northgate Systems "
    "(the Supplier) and Lakeside Retail (the Customer).\n"
    "1. Services. The Supplier shall provide the services described in the "
    "applicable statement of work.\n"
    "2. Indemnification. Each party shall indemnify the other against "
    "third-party claims arising from its own negligence.\n"
    "9. Governing Law. This agreement shall be governed by the laws of the "
    "State of Ohio, notwithstanding any conflict-of-law principles."
)

GENERAL_QUESTION = "what does an indemnity clause usually mean?"

SHORT_QUOTE = "The parties shall cooperate in good faith"


def _contract_spans(text, policy):
    return [
        s for s in tier1.scan(text, policy, None)
        if s.cls == "business" and s.subclass == "contract_clause"
    ]


# --- detector ---------------------------------------------------------------


def test_numbered_clauses_detected(policy):
    spans = _contract_spans(NDA_EXCERPT, policy)
    assert len(spans) == 3
    assert all(s.status == "proven" for s in spans)
    assert all(s.suggested_action == "redact" for s in spans)
    assert all(s.can_keep for s in spans)


def test_defined_party_line_detected(policy):
    text = (
        "This arrangement is between Fernhill Goods (the Supplier) and "
        "Quartzline Studio (the Customer) for the term."
    )
    spans = _contract_spans(text, policy)
    assert len(spans) == 1


def test_general_question_is_clean(policy):
    assert tier1.scan(GENERAL_QUESTION, policy, None) == []


def test_short_quote_without_number_is_clean(policy):
    assert tier1.scan(SHORT_QUOTE, policy, None) == []


# --- gate: redact + restore --------------------------------------------------


def test_numbered_nda_redacts_and_restores(policy):
    decision = gate(NDA_EXCERPT, policy, None)
    assert decision.decision == "REDACT"
    assert decision.sent_text is not None
    for token in ("[CONTRACT_CLAUSE_1]", "[CONTRACT_CLAUSE_2]",
                  "[CONTRACT_CLAUSE_3]"):
        assert token in decision.sent_text
    assert "Receiving Party" not in decision.sent_text
    for clause in (
        "shall not disclose",
        "notwithstanding any prior understanding",
        "shall return all private information",
    ):
        assert clause not in decision.sent_text
    answer = (
        "noted [CONTRACT_CLAUSE_1] and [CONTRACT_CLAUSE_2] "
        "plus [CONTRACT_CLAUSE_3], thanks"
    )
    restored, highlights, unmatched = rehydrate(answer, decision.placeholders)
    assert unmatched == []
    assert len(highlights) == 3
    assert "shall not disclose" in restored
    assert "notwithstanding any prior understanding" in restored


def test_plain_contract_redacts_without_holding(policy):
    decision = gate(MSA_PLAIN, policy, None)
    assert decision.decision == "REDACT"
    assert decision.sent_text is not None
    assert "[CONTRACT_CLAUSE_1]" in decision.sent_text
    assert "Northgate Systems" not in decision.sent_text


# --- gate: confidential hold -------------------------------------------------


def test_confidential_msa_holds(policy):
    decision = gate(MSA_CONFIDENTIAL, policy, None)
    assert decision.decision == "HOLD"
    assert decision.sent_text is None
    assert any(
        "Confidential contract text" in reason for reason in decision.reasons
    )


def test_confidential_hold_redact_and_send(policy):
    decision = gate(MSA_CONFIDENTIAL, policy, None)
    assert decision.decision == "HOLD"
    holds_module.reset_for_tests()
    try:
        hold = holds_module.create_hold(
            decision, MSA_CONFIDENTIAL,
            [{"role": "user", "content": MSA_CONFIDENTIAL}], None,
        )
        result = holds_module.approve_hold(
            hold, sender=lambda sent: f"reviewed {sent[:20]} ok"
        )
    finally:
        holds_module.reset_for_tests()
    assert result is not None
    assert "[CONTRACT_CLAUSE_1]" in result["sent_text"]
    assert "Northgate Systems" not in result["sent_text"]


def test_general_question_allows(policy):
    decision = gate(GENERAL_QUESTION, policy, None)
    assert decision.decision == "ALLOW"
    assert decision.sent_text == GENERAL_QUESTION


# --- clause + PII ------------------------------------------------------------


def test_clause_plus_email_both_placeholders(policy):
    text = NDA_EXCERPT + "\n\nContact counsel@northgatesystems.com with questions."
    decision = gate(text, policy, None)
    assert decision.decision == "REDACT"
    assert decision.sent_text is not None
    assert "[CONTRACT_CLAUSE_1]" in decision.sent_text
    assert "[PII_EMAIL_1]" in decision.sent_text
    assert "counsel@northgatesystems.com" not in decision.sent_text
    restored, _, unmatched = rehydrate(
        "saw [CONTRACT_CLAUSE_1] and [PII_EMAIL_1]", decision.placeholders
    )
    assert unmatched == []
    assert "counsel@northgatesystems.com" in restored
    assert "shall not disclose" in restored


# --- receipt + descriptive trace ---------------------------------------------


def test_receipt_names_contract_clauses(policy):
    decision = gate(NDA_EXCERPT, policy, None)
    message = user_message(decision)
    assert "3 contract clauses" in message


def test_trace_explains_which_marker_fired(policy):
    trace: list = []
    gate(NDA_EXCERPT, policy, None, trace_out=trace)
    step = next(s for s in trace if s["step"] == "tier1.contract_scan")
    assert step["result"] == "hit"
    assert "markers:" in step["detail"]
    assert "shall" in step["detail"] or "clause heading" in step["detail"]
    assert "Receiving Party" not in json.dumps(trace)


def test_placeholder_shape_matches_and_rehydrates():
    assert PLACEHOLDER_RE.search("[CONTRACT_CLAUSE_1]") is not None
    text, highlights, unmatched = rehydrate(
        "see [CONTRACT_CLAUSE_2] today", {"[CONTRACT_CLAUSE_2]": "clause text"}
    )
    assert text == "see clause text today"
    assert unmatched == []


# --- proxy routes ------------------------------------------------------------


@pytest.fixture()
def mock_upstream():
    from shadowfenced import app as app_module

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    app_module.app.state.upstream_transport = httpx.MockTransport(handler)
    yield
    if hasattr(app_module.app.state, "upstream_transport"):
        delattr(app_module.app.state, "upstream_transport")


def test_send_redacts_clauses(mock_upstream):
    from shadowfenced.app import SendRequest, send

    body = send(SendRequest(messages=[{"role": "user", "content": NDA_EXCERPT}]))
    assert body["decision"] == "REDACT"
    assert "[CONTRACT_CLAUSE_1]" in body["sent_text"]
    assert "Receiving Party" not in body["sent_text"]
    assert "Receiving Party" not in json.dumps(body["trace"])


def test_send_holds_confidential_msa(mock_upstream):
    from shadowfenced.app import SendRequest, send

    body = send(
        SendRequest(messages=[{"role": "user", "content": MSA_CONFIDENTIAL}])
    )
    assert body["decision"] == "HOLD"
    assert body["sent_text"] is None
    assert body["answer"] is None
    assert body["review_url"].startswith("/api/holds/")
    assert any(
        "Confidential contract text" in reason
        for reason in body["evidence"]["reasons"]
    )


def test_short_quote_with_clause_number_redacts(policy):
    decision = gate("4.2 Termination.", policy, None)
    assert decision.decision == "REDACT"
    assert decision.sent_text == "[CONTRACT_CLAUSE_1]"


def test_chat_completions_redacts_clauses(mock_upstream):
    import json as json_module

    from shadowfenced import app as app_module
    from shadowfenced.app import ChatRequest, chat_completions

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "noted [CONTRACT_CLAUSE_1], thanks",
                        }
                    }
                ]
            },
        )

    app_module.app.state.upstream_transport = httpx.MockTransport(handler)
    response = chat_completions(
        ChatRequest(
            model="x",
            messages=[{"role": "user", "content": NDA_EXCERPT}],
        )
    )
    assert response.status_code == 200
    assert response.headers["x-shadowfenced-decision"] == "REDACT"
    body = json_module.loads(response.body.decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    assert "Receiving Party" in content
    assert "[CONTRACT_CLAUSE_1]" not in content
