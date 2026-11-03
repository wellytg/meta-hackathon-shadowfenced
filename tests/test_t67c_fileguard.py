"""T67c file guard: exact CSV redaction, structure kept, no override for
financial data, server deep-check estimate.

Every person, place and value below is invented for the test.
"""

from __future__ import annotations

import base64
import csv
import io
import json

import pytest

from pathlib import Path

from shadowfenced import fileguard
from shadowfenced.policy import load_policy
from shadowfenced.canary import CanaryRegistry, make_canary

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "extension" / "testpage" / "samples"


@pytest.fixture(autouse=True)
def _clean_state():
    fileguard.reset_for_tests()
    yield
    fileguard.reset_for_tests()


@pytest.fixture()
def policy():
    return load_policy(ROOT / "policy.yaml")


@pytest.fixture()
def registry():
    return CanaryRegistry("test-seed-t67c", ("db_password", "api_key"))


def _sample(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


def _redacted_text(scan_id: str) -> str:
    copy = fileguard.build_redacted_copy(scan_id)
    assert copy["clean"] is True
    return base64.b64decode(copy["content_base64"]).decode("utf-8")


# --- CSV exact redaction -----------------------------------------------------


def test_bank_csv_first_row_redacts_exactly(policy, registry):
    payload = fileguard.scan_upload(
        "bank_statement_synthetic.csv",
        _sample("bank_statement_synthetic.csv"), policy, registry,
    )
    assert payload["decision"] == "BLOCK"
    redacted = _redacted_text(payload["scan_id"])
    rows = redacted.splitlines()
    assert rows[0] == "name,email,phone,notes"
    assert rows[1] == (
        "[PII_NAME_1],[PII_EMAIL_1],[PII_PHONE_1],"
        "Corvane Credit Union sort code [PII_SORT_CODE_1] "
        "account [PII_ACCOUNT_1]"
    )
    assert "SECRET" not in redacted  # PII is never labelled SECRET


def test_bank_csv_header_and_column_count_preserved(policy, registry):
    payload = fileguard.scan_upload(
        "bank_statement_synthetic.csv",
        _sample("bank_statement_synthetic.csv"), policy, registry,
    )
    redacted = _redacted_text(payload["scan_id"])
    original_rows = list(csv.reader(io.StringIO(
        _sample("bank_statement_synthetic.csv").decode("utf-8"))))
    redacted_rows = list(csv.reader(io.StringIO(redacted)))
    # Header kept, same number of data rows, same column counts.
    assert redacted_rows[0] == ["name", "email", "phone", "notes"]
    data_orig = [r for r in original_rows[1:] if r and not r[0].startswith("#")]
    data_red = [r for r in redacted_rows[1:] if r and not r[0].startswith("#")]
    assert len(data_red) == len(data_orig) == 6
    for orig, red in zip(data_orig, data_red):
        assert len(red) == len(orig) == 4
    # Same value -> same placeholder within the file.
    assert redacted_rows[1][3] == redacted_rows[2][3].replace(
        "[PII_ACCOUNT_2]", "[PII_ACCOUNT_1]")


def test_bank_csv_redacted_copy_scans_clean(policy, registry):
    payload = fileguard.scan_upload(
        "bank_statement_synthetic.csv",
        _sample("bank_statement_synthetic.csv"), policy, registry,
    )
    redacted = _redacted_text(payload["scan_id"])
    rescan = fileguard.scan_upload(
        "bank_statement_synthetic.redacted.csv",
        redacted.encode("utf-8"), policy, registry,
    )
    assert rescan["decision"] == "CLEAN"


def test_csv_quoted_name_with_comma_keeps_columns(policy, registry):
    text = (
        'name,email,phone,notes\n'
        '"Okafor, Amara",amara.okafor@northwind-mail.co,07700 900111,'
        '"Corvane Credit Union, sort code 20-11-22 account 10001111"\n'
    )
    payload = fileguard.scan_upload("q.csv", text.encode("utf-8"),
                                    policy, registry)
    assert payload["decision"] == "BLOCK"
    redacted = _redacted_text(payload["scan_id"])
    rows = list(csv.reader(io.StringIO(redacted)))
    assert rows[0] == ["name", "email", "phone", "notes"]
    assert len(rows[1]) == 4
    assert rows[1][0] == "[PII_NAME_1]"
    assert rows[1][1] == "[PII_EMAIL_1]"
    assert rows[1][2] == "[PII_PHONE_1]"
    assert rows[1][3] == (
        "Corvane Credit Union, sort code [PII_SORT_CODE_1] "
        "account [PII_ACCOUNT_1]"
    )


# --- JSON structure ----------------------------------------------------------


def test_json_keys_kept_values_redacted(policy, registry):
    doc = {
        "name": "Amara Okafor",
        "email": "amara.okafor@northwind-mail.co",
        "phone": "07700 900111",
        "notes": "Corvane Credit Union sort code 20-11-22 account 10001111",
    }
    raw = json.dumps(doc, indent=2)
    payload = fileguard.scan_upload("data.json", raw.encode("utf-8"),
                                    policy, registry)
    assert payload["decision"] == "BLOCK"
    redacted = _redacted_text(payload["scan_id"])
    parsed = json.loads(redacted)  # still valid JSON
    assert sorted(parsed) == ["email", "name", "notes", "phone"]
    assert parsed["name"] == "[PII_NAME_1]"
    assert parsed["email"] == "[PII_EMAIL_1]"
    assert parsed["phone"] == "[PII_PHONE_1]"
    assert parsed["notes"] == (
        "Corvane Credit Union sort code [PII_SORT_CODE_1] "
        "account [PII_ACCOUNT_1]"
    )
    rescan = fileguard.scan_upload("data.redacted.json",
                                   redacted.encode("utf-8"),
                                   policy, registry)
    assert rescan["decision"] == "CLEAN"


# --- .env structure ----------------------------------------------------------


def test_env_keys_kept_values_redacted(policy, registry):
    raw = (
        "DB_HOST=db.internal.example.test\n"
        "DB_USER=deploy_ro\n"
        "DB_PASSWORD=C0rvane-Deploy-7h9s\n"
        "CONTACT=ops@northwind-mail.co\n"
    )
    payload = fileguard.scan_upload("app.env", raw.encode("utf-8"),
                                    policy, registry)
    assert payload["decision"] == "BLOCK"
    redacted = _redacted_text(payload["scan_id"])
    lines = dict(
        line.split("=", 1) for line in redacted.splitlines() if "=" in line
    )
    assert sorted(lines) == ["CONTACT", "DB_HOST", "DB_PASSWORD", "DB_USER"]
    assert lines["DB_PASSWORD"] == "[SECRET_1]"
    assert lines["CONTACT"] == "[PII_EMAIL_1]"
    assert lines["DB_HOST"] == "db.internal.example.test"
    assert lines["DB_USER"] == "deploy_ro"
    rescan = fileguard.scan_upload("app.redacted.env",
                                   redacted.encode("utf-8"),
                                   policy, registry)
    assert rescan["decision"] == "CLEAN"


# --- upload-anyway visibility ------------------------------------------------


@pytest.mark.parametrize("text,name,overridable", [
    ("sort code 20-11-22", "sort.txt", False),
    ("account 10001111", "acct.txt", False),
    ("card 4111 1111 1111 1111", "card.txt", False),
    ("IBAN GB82 WEST 1234 5698 7654 32", "iban.txt", False),
    ("routing 021000021", "rtn.txt", False),
    ("Name: Amara Okafor", "name.txt", True),
    ("Email: amara.okafor@northwind-mail.co", "email.txt", True),
    ("Phone: 07700 900111", "phone.txt", True),
])
def test_overridable_visibility_table(policy, registry, text, name,
                                      overridable):
    payload = fileguard.scan_upload(name, text.encode("utf-8"),
                                    policy, registry)
    assert payload["decision"] == "BLOCK"
    assert payload["overridable"] is overridable


def test_overridable_false_for_secret_canary_bundle(policy, registry):
    canary = make_canary("fileguard-sample-v1", "deploy_token")
    secret = fileguard.scan_upload("s.txt", f"token: {canary}".encode(),
                                   policy, registry)
    assert secret["has_secret"] is True
    assert secret["overridable"] is False
    bundle = fileguard.scan_upload(
        "b.txt",
        ("Priya Ramanathan, born 3 May 1991, 27 Willow Crescent, "
         "Leeds LS6 2QT, phone 07700 900461, "
         "email priya.ramanathan@northwind-mail.co.").encode(),
        policy, registry)
    assert bundle["identity_bundle"] is True
    assert bundle["overridable"] is False
