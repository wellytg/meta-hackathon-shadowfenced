"""T29 tests: financial account detectors, holder names, doc-type warnings.

All account numbers, routing numbers, IBANs, names and phones below are
fictional. Checksum-passing values (routing 021000021, IBAN
GB82WEST12345698765432) are computed test numbers, not real accounts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shadowfenced import docclass, tier1
from shadowfenced.engine import gate, user_message
from shadowfenced.policy import load_policy
from shadowfenced.redact import PLACEHOLDER_RE, redact, rehydrate

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ROOT / "corpus" / "documents"


@pytest.fixture()
def policy():
    return load_policy(ROOT / "policy.yaml")


def _subclasses(text, policy):
    return {(s.subclass, text[s.cp_start:s.cp_end]) for s in tier1.scan(text, policy, None)}


# --- bank account numbers ---------------------------------------------------


@pytest.mark.parametrize("label", ["Account number", "account no.", "acct", "a/c", "checking", "savings"])
def test_account_labels_catch_plain_numbers(policy, label):
    spans = tier1.scan(f"{label}: 84729105", policy, None)
    hit = [s for s in spans if s.subclass == "account"]
    assert len(hit) == 1
    assert hit[0].cls == "pii" and hit[0].status == "proven"
    assert hit[0].suggested_action == "redact"


def test_account_number_length_bounds(policy):
    assert any(s.subclass == "account" for s in tier1.scan("acct 123456", policy, None))
    assert any(s.subclass == "account" for s in tier1.scan("acct 12345678901234567", policy, None))
    assert not any(s.subclass == "account" for s in tier1.scan("acct 12345", policy, None))
    assert not any(s.subclass == "account" for s in tier1.scan("acct 123456789012345678", policy, None))


def test_account_needs_label(policy):
    assert tier1.scan("84729105 with no label", policy, None) == []
    assert tier1.scan("the account team yet", policy, None) == []


def test_masked_account_proven_and_redacted(policy):
    spans = tier1.scan("Our a/c ****8841 is overdrawn", policy, None)
    hit = [s for s in spans if s.subclass == "account"]
    assert len(hit) == 1 and hit[0].status == "proven"
    redacted, mapping = redact("Our a/c ****8841 is overdrawn", hit)
    assert redacted == "Our a/c [PII_ACCOUNT_1] is overdrawn"
    assert mapping == {"[PII_ACCOUNT_1]": "****8841"}


def test_masked_account_never_blocks(policy):
    decision = gate("checking ****8841 for the refund", policy, None)
    assert decision.decision == "REDACT"
    assert decision.sent_text is not None and "****8841" not in decision.sent_text


# --- routing numbers ----------------------------------------------------------


def test_routing_valid_checksum(policy):
    assert tier1._aba_valid("021000021")
    assert tier1._aba_valid("011401533")
    spans = tier1.scan("Routing number 021000021", policy, None)
    hit = [s for s in spans if s.subclass == "routing"]
    assert len(hit) == 1 and hit[0].cls == "pii" and hit[0].status == "proven"


def test_routing_bad_checksum_rejected(policy):
    assert not tier1._aba_valid("021000022")
    spans = tier1.scan("RTN 021000022 is wrong", policy, None)
    assert not [s for s in spans if s.subclass == "routing"]


def test_routing_needs_label(policy):
    assert not [s for s in tier1.scan("021000021 alone", policy, None) if s.subclass == "routing"]


# --- IBAN + sort code -----------------------------------------------------------


def test_iban_valid_accepted(policy):
    assert tier1._iban_mod97_ok("GB82WEST12345698765432")
    for text in (
        "transfer to IBAN GB82 WEST 1234 5698 7654 32 today",
        "transfer to GB82WEST12345698765432 today",
    ):
        spans = tier1.scan(text, policy, None)
        hit = [s for s in spans if s.subclass == "iban"]
        assert len(hit) == 1, text
        assert hit[0].cls == "pii" and hit[0].status == "proven"


def test_iban_bad_check_digits_rejected(policy):
    bad = "GB27WEST12345698765432"
    assert not tier1._iban_mod97_ok(bad)
    spans = tier1.scan(f"transfer to {bad} today", policy, None)
    assert not [s for s in spans if s.subclass == "iban"]


def test_sort_code_pair(policy):
    spans = tier1.scan("sort code 20-00-00, account 12345678", policy, None)
    by_sub = {s.subclass: s for s in spans}
    assert by_sub["sort_code"].cls == "pii" and by_sub["sort_code"].status == "proven"
    assert by_sub["account"].cls == "pii" and by_sub["account"].status == "proven"


# --- SWIFT/BIC ------------------------------------------------------------------


@pytest.mark.parametrize("code", ["NWBKGB2L", "DEUTDEFF500"])
def test_swift_with_label(policy, code):
    spans = tier1.scan(f"SWIFT {code} handles it", policy, None)
    hit = [s for s in spans if s.subclass == "swift"]
    assert len(hit) == 1 and hit[0].cls == "pii" and hit[0].status == "proven"


def test_swift_needs_label(policy):
    assert not [s for s in tier1.scan("DEUTDEFF alone", policy, None) if s.subclass == "swift"]


# --- holder names -----------------------------------------------------------------


@pytest.mark.parametrize(
    "label", ["Account holder", "Name", "Patient", "Employee", "Cardholder", "Account Holder"]
)
def test_holder_labels(policy, label):
    text = f"{label}: Mara Ellison"
    spans = tier1.scan(text, policy, None)
    hit = [s for s in spans if s.subclass in ("holder_name", "name")]
    assert len(hit) >= 1
    assert hit[0].cls == "pii" and hit[0].status == "proven"
    assert any(text[s.cp_start:s.cp_end] == "Mara Ellison" for s in hit)


def test_holder_needs_colon(policy):
    assert tier1.scan("patients table is large", policy, None) == []


def test_holder_placeholders(policy):
    text = "Account holder: Mara Ellison\nCardholder: Tomas Reyes"
    spans = [s for s in tier1.scan(text, policy, None) if s.subclass == "holder_name"]
    assert len(spans) == 2
    redacted, mapping = redact(text, spans)
    assert "[PII_HOLDER_NAME_1]" in redacted and "[PII_HOLDER_NAME_2]" in redacted
    assert "Mara Ellison" not in redacted and "Tomas Reyes" not in redacted


# --- placeholder style --------------------------------------------------------------


def test_new_placeholders_match_and_rehydrate():
    for token in (
        "[PII_ACCOUNT_1]",
        "[PII_ROUTING_1]",
        "[PII_IBAN_1]",
        "[PII_SWIFT_1]",
        "[PII_SORT_CODE_1]",
        "[PII_HOLDER_NAME_1]",
    ):
        assert PLACEHOLDER_RE.search(token) is not None, token
    mapping = {"[PII_ACCOUNT_1]": "84729105", "[PII_HOLDER_NAME_1]": "Mara Ellison"}
    text, highlights, unmatched = rehydrate("acct [PII_ACCOUNT_1] for [PII_HOLDER_NAME_1]", mapping)
    assert text == "acct 84729105 for Mara Ellison"
    assert unmatched == []
    assert len(highlights) == 2


def test_placeholder_bases():
    assert "[PII_ACCOUNT_1]" in gate("Account number: 84729105", load_policy(ROOT / "policy.yaml"), None).sent_text


# --- document-type classifier ------------------------------------------------------------


def test_classify_each_type():
    assert docclass.classify("Statement period March. Opening balance 1. Closing balance 2. Transactions listed. "
                             "Routing number 021000021.").get("doc_type") == "bank_statement"
    assert docclass.classify("Patient visit. MRN 482913. Diagnosis noted. Medication prescribed. "
                             "Insurance member ID on file.").get("doc_type") == "medical_record"
    assert docclass.classify("Payslip. Gross pay 3000. Net pay 2400. Tax code 1257L. Employer Harborline. "
                             "Pay period April.").get("doc_type") == "payslip"
    assert docclass.classify("This agreement binds each party. Governing law applies. Each party shall indemnify. "
                             "Confidential termination terms.").get("doc_type") == "contract"


def test_classify_none_for_benign():
    for text in ("hello world, nothing sensitive", "mail ada@corvanelabs.com about Project Tallow",
                 "tomasz from accounts called about medical leave"):
        assert docclass.classify(text)["doc_type"] is None
        assert docclass.classify(text)["warning"] is None


def test_warnings_are_plain_and_typed():
    assert docclass.WARNINGS["bank_statement"].startswith("This looks like a bank statement.")
    assert "account numbers, balances and your address" in docclass.WARNINGS["bank_statement"]
    assert docclass.WARNINGS["contract"].startswith("This looks like a contract.")
    assert "confidential terms and party names" in docclass.WARNINGS["contract"]


def test_warning_rides_along_without_blocking(policy):
    text = ("Statement period: March 2026. Opening balance $100. Closing balance $200. "
            "Transactions below. Routing number 021000021. Account number 84729105.")
    trace = []
    decision = gate(text, policy, None, trace_out=trace)
    assert decision.decision == "REDACT"
    assert decision.doc_type == "bank_statement"
    assert decision.doc_warning == docclass.WARNINGS["bank_statement"]
    assert decision.evidence["doc_type"] == "bank_statement"
    assert decision.evidence["doc_warning"] == docclass.WARNINGS["bank_statement"]
    assert user_message(decision).startswith(docclass.WARNINGS["bank_statement"])
    assert "REDACT" in user_message(decision) or "redaction" in user_message(decision)
    kinds = [step["step"] for step in trace]
    assert "doc.classify" in kinds
    classify = next(step for step in trace if step["step"] == "doc.classify")
    assert classify["result"] == "bank_statement"
    assert "021000021" not in classify["detail"] and "84729105" not in classify["detail"]


def test_no_warning_leaves_message_unchanged(policy):
    decision = gate("hello world, nothing sensitive", policy, None)
    assert decision.decision == "ALLOW"
    assert decision.doc_type is None
    assert user_message(decision) == "Sent clean"
    trace = []
    gate("mail ada@corvanelabs.com now", policy, None, trace_out=trace)
    assert "doc.classify" in [step["step"] for step in trace]


# --- six fictional corpus documents --------------------------------------------------


_DOCS = {
    "bank_statement_1.txt": ("bank_statement", ["84729105", "021000021", "Mara Ellison"]),
    "bank_statement_2.txt": ("bank_statement", ["****8841", "GB82 WEST 1234 5698 7654 32",
                                                "20-00-00", "12345678", "NWBKGB2L", "Tomas Reyes"]),
    "medical_record_1.txt": ("medical_record", ["June Okafor"]),
    "medical_record_2.txt": ("medical_record", ["Ellis Grant"]),
    "payslip_1.txt": ("payslip", ["Nadia Shah", "73910428", "011401533"]),
    "contract_1.txt": ("contract", ["Bluepine Traders", "Harborline Studio",
                                    "DE89 3704 0044 0532 0130 00", "DEUTDEFF"]),
}


def test_send_response_carries_doc_warning(policy):
    import httpx

    from shadowfenced import app as app_module
    from shadowfenced.app import SendRequest, app, send

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    app.state.upstream_transport = httpx.MockTransport(handler)
    try:
        body = send(SendRequest(messages=[{"role": "user", "content": (
            "Statement period: March 2026. Opening balance $100. Closing balance $200. "
            "Transactions below. Routing number 021000021. Account number 84729105.")}]))
    finally:
        if hasattr(app.state, "upstream_transport"):
            delattr(app.state, "upstream_transport")
    assert body["doc_type"] == "bank_statement"
    assert body["doc_warning"] == docclass.WARNINGS["bank_statement"]
    assert body["user_message"].startswith(docclass.WARNINGS["bank_statement"])
    assert body["evidence"]["doc_type"] == "bank_statement"
    assert "doc.classify" in [step["step"] for step in body["trace"]]


def test_upload_reports_doc_type():
    from shadowfenced import holds, uploads
    from shadowfenced.app import handle_upload

    uploads.reset_for_tests()
    holds.reset_for_tests()
    try:
        text = (DOCUMENTS / "bank_statement_1.txt").read_bytes()
        payload = handle_upload("bank_statement_1.txt", text)
        assert payload["doc_type"] == "bank_statement"
        assert payload["doc_warning"] == docclass.WARNINGS["bank_statement"]
    finally:
        uploads.reset_for_tests()
        holds.reset_for_tests()


def test_app_js_renders_doc_warning_above_banner():
    js = (ROOT / "src" / "shadowfenced" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'data-testid", "doc-warning"' in js
    assert "body.doc_warning" in js
    assert js.find("thread.appendChild(docWarning);") < js.find("thread.appendChild(banner);")


def test_corpus_documents_classified_and_redacted(policy):
    files = sorted(DOCUMENTS.glob("*.txt"))
    assert [f.name for f in files] == sorted(_DOCS), [f.name for f in files]
    for name, (doc_type, secrets) in _DOCS.items():
        text = (DOCUMENTS / name).read_text(encoding="utf-8")
        info = docclass.classify(text)
        assert info["doc_type"] == doc_type, (name, info["scores"])
        decision = gate(text, policy, None)
        assert decision.decision == "REDACT", (name, decision.reasons)
        assert decision.doc_type == doc_type, name
        assert decision.sent_text is not None
        for secret in secrets:
            assert secret not in decision.sent_text, (name, secret)
            assert secret in decision.placeholders.values(), (name, secret)
