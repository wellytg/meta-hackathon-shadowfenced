"""Document-type classification (T29): weighted signals, never blocking.

``classify`` looks at a message or uploaded-file text and returns one of
``bank_statement``, ``medical_record``, ``payslip``, ``contract`` or
``None``. Every signal below carries a fixed integer weight; the type
with the highest total wins when it reaches ``THRESHOLD``. Ties resolve
in the fixed order ``bank_statement``, ``medical_record``, ``payslip``,
``contract``. Classification never changes the gate decision: it only
adds a plain user-facing warning plus evidence and a ``doc.classify``
trace step.

Signal weights (documented here so the classifier stays auditable):

bank_statement (threshold 4):
  statement period/date/billing period +3, opening/closing balance +2
  each, available/current balance +1, transaction(s) +2, routing/ABA/RTN
  cue +2, IBAN/sort-code/SWIFT/BIC cue +2, account-number/acct/a/c cue
  +2, deposit/withdrawal/direct deposit +1.
medical_record (threshold 4):
  MRN/medical-record cue +3, diagnos* +2, medication/prescription/dosage
  +2, insurance/member-ID/group-number cue +2, patient/DOB/allergies/
  vitals/symptoms cue +1 each.
payslip (threshold 4):
  payslip/pay-slip/pay-stub +3, gross pay +2, net pay +2, tax-code/PAYE/
  national-insurance cue +2, employer +2, pay period/date +2,
  deductions/withholding +1, salary/wages +1.
contract (threshold 4):
  "this agreement" +3, governing law +2, indemnif* +2, confident* +1,
  party/parties +1, terminat* +1, whereas/hereby/witnesseth +1.
"""

from __future__ import annotations

import re

THRESHOLD = 4

DOC_TYPES = ("bank_statement", "medical_record", "payslip", "contract")

WARNINGS = {
    "bank_statement": (
        "This looks like a bank statement. It usually contains account "
        "numbers, balances and your address."
    ),
    "medical_record": (
        "This looks like a medical record. It usually contains health "
        "details, medications and insurance IDs."
    ),
    "payslip": (
        "This looks like a payslip. It usually contains pay, tax details "
        "and employer information."
    ),
    "contract": (
        "This looks like a contract. It may contain confidential terms "
        "and party names."
    ),
}

# doc_type -> [(compiled pattern, weight, signal name)]
SIGNALS: dict[str, list[tuple[re.Pattern[str], int, str]]] = {
    "bank_statement": [
        (re.compile(r"(?i)\b(statement\s+(?:period|date)|billing\s+period)\b"), 3, "statement-period"),
        (re.compile(r"(?i)\bopening\s+balance\b"), 2, "opening-balance"),
        (re.compile(r"(?i)\bclosing\s+balance\b"), 2, "closing-balance"),
        (re.compile(r"(?i)\b(available|current)\s+balance\b"), 1, "current-balance"),
        (re.compile(r"(?i)\btransactions?\b"), 2, "transactions"),
        (re.compile(r"(?i)\b(?:routing(?:\s+number)?|ABA|RTN)\b"), 2, "routing-cue"),
        (re.compile(r"(?i)\b(?:IBAN|sort\s*code|SWIFT|BIC)\b"), 2, "iban-swift-cue"),
        (re.compile(r"(?i)\b(?:account\s+(?:number|no\.?)|acct(?:\.|\s+no\.?)?|a/c)\b"), 2, "account-cue"),
        (re.compile(r"(?i)\b(?:deposits?|withdrawals?|direct\s+deposit)\b"), 1, "deposit-cue"),
    ],
    "medical_record": [
        (re.compile(r"(?i)\b(?:MRN|medical\s+record(?:\s+number)?)\b"), 3, "mrn"),
        (re.compile(r"(?i)\bdiagnos(?:is|ed|tic)\b"), 2, "diagnosis"),
        (re.compile(r"(?i)\b(?:medication|prescription|dosage)\b"), 2, "medication"),
        (re.compile(r"(?i)\b(?:insurance|member\s*ID|group\s*(?:number|no\.?))\b"), 2, "insurance"),
        (re.compile(r"(?i)\bpatient\b"), 1, "patient"),
        (re.compile(r"(?i)\b(?:DOB|date\s+of\s+birth|allergies|vitals|symptoms|blood\s+pressure)\b"), 1, "clinical-cue"),
    ],
    "payslip": [
        (re.compile(r"(?i)\b(?:payslip|pay[-\s]?slip|pay\s+stub)\b"), 3, "payslip-word"),
        (re.compile(r"(?i)\bgross\s+pay\b"), 2, "gross-pay"),
        (re.compile(r"(?i)\bnet\s+pay\b"), 2, "net-pay"),
        (re.compile(r"(?i)\b(?:tax\s*code|PAYE|national\s+insurance)\b"), 2, "tax-cue"),
        (re.compile(r"(?i)\bemployer\b"), 2, "employer"),
        (re.compile(r"(?i)\bpay\s+(?:period|date)\b"), 2, "pay-period"),
        (re.compile(r"(?i)\b(?:deductions?|withholding)\b"), 1, "deductions"),
        (re.compile(r"(?i)\b(?:salary|wages)\b"), 1, "salary"),
    ],
    "contract": [
        (re.compile(r"(?i)\bthis\s+agreement\b"), 3, "this-agreement"),
        (re.compile(r"(?i)\bgoverning\s+law\b"), 2, "governing-law"),
        (re.compile(r"(?i)\bindemnif\w*\b"), 2, "indemnity"),
        (re.compile(r"(?i)\bconfident\w*\b"), 1, "confidential"),
        (re.compile(r"(?i)\bpart(?:y|ies)\b"), 1, "party"),
        (re.compile(r"(?i)\bterminat\w*\b"), 1, "termination"),
        (re.compile(r"(?i)\b(?:whereas|hereby|witnesseth)\b"), 1, "legal-form"),
    ],
}


def classify(text: str) -> dict[str, object]:
    """Classify text into a document type with a user-facing warning.

    Returns ``{"doc_type": ...|None, "warning": ...|None,
    "scores": {type: int}, "signals": [(type, name, weight)]}``.
    ``signals`` holds each fired signal once (presence, not count), so
    repeated words never inflate a score.
    """
    scores: dict[str, int] = {doc: 0 for doc in DOC_TYPES}
    fired: list[tuple[str, str, int]] = []
    for doc in DOC_TYPES:
        for pattern, weight, name in SIGNALS[doc]:
            if pattern.search(text):
                scores[doc] += weight
                fired.append((doc, name, weight))
    best = max(DOC_TYPES, key=lambda doc: scores[doc])
    if scores[best] < THRESHOLD:
        return {"doc_type": None, "warning": None, "scores": scores, "signals": fired}
    return {
        "doc_type": best,
        "warning": WARNINGS[best],
        "scores": scores,
        "signals": fired,
    }
