"""T69 habits: HMAC fingerprints, choice events, suggestions, history panel.

The decision log learns habits (kinds, rules, decisions, choices, sites,
cue words) and never values: fingerprints are HMAC-SHA256 under a
per-install secret, and choice events carry fields only.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from conftest import route_methods

from shadowfenced import ecs_log, engine, suggest
from shadowfenced import ext_api as ext_api_module
from shadowfenced import policy as policy_module
from shadowfenced import uploads as uploads_module
from shadowfenced.app import (
    ChoiceRequest,
    SuggestionApplyRequest,
    _decision_rule_ids,
    _maybe_auto_confirm_warn,
    _warn_store_clear,
    app,
    apply_suggestion,
    delete_history,
    dismiss_suggestion,
    history,
    list_suggestions,
    record_choice,
    reset_state,
    undo_suggestion,
    upload_redacted_copy,
    confirm_warn,
    get_policy,
)
from shadowfenced.canary import CanaryRegistry
from shadowfenced.ext_api import ChoiceBody, RedactBody, ext_choice, ext_redact
from shadowfenced.policy import load_policy


def _unsalted(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


@pytest.fixture()
def fp_secret(monkeypatch):
    """Fixed fingerprint secret so fingerprints are deterministic."""
    monkeypatch.setenv("SF_FINGERPRINT_SECRET", "t69-test-secret")
    ecs_log.reset_fingerprint_secret()
    yield
    ecs_log.reset_fingerprint_secret()


@pytest.fixture()
def habit_env(monkeypatch, tmp_path, fp_secret):
    """Isolated suggestions state + local policy for habit tests."""
    monkeypatch.setenv(
        "SF_SUGGESTIONS_PATH", str(tmp_path / "rule_suggestions.yaml")
    )
    monkeypatch.setenv("SF_LOCAL_POLICY", str(tmp_path / "policy.local.yaml"))
    reset_state()
    yield tmp_path
    reset_state()


@pytest.fixture()
def mock_upstream():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    app.state.upstream_transport = httpx.MockTransport(handler)
    yield
    if hasattr(app.state, "upstream_transport"):
        delattr(app.state, "upstream_transport")


def _gate(text):
    return engine.gate(
        text, load_policy(), CanaryRegistry("t69", ("db_password",))
    )


def _seed_choices(log, kind, n, choice="send_anyway", site="app", rule_ids=()):
    for i in range(n):
        ecs_log.append_choice(
            log,
            decision_id=f"d{i:04d}",
            choice=choice,
            kinds=[kind],
            rule_ids=list(rule_ids),
            site=site,
        )


# --- HMAC fingerprints ---------------------------------------------------


def test_fingerprint_is_stable_within_install(monkeypatch, tmp_path):
    """Same value maps to the same fingerprint under one secret."""
    monkeypatch.setenv("SF_FINGERPRINT_SECRET", "test-secret-alpha")
    ecs_log.reset_fingerprint_secret()
    try:
        assert ecs_log.fingerprint("ada@corvanelabs.com") == ecs_log.fingerprint(
            "ada@corvanelabs.com"
        )
    finally:
        ecs_log.reset_fingerprint_secret()


def test_fingerprint_differs_across_secrets(monkeypatch):
    """A different install secret yields a different fingerprint."""
    monkeypatch.setenv("SF_FINGERPRINT_SECRET", "test-secret-alpha")
    ecs_log.reset_fingerprint_secret()
    try:
        first = ecs_log.fingerprint("+44 7700 900077")
    finally:
        ecs_log.reset_fingerprint_secret()
    monkeypatch.setenv("SF_FINGERPRINT_SECRET", "test-secret-beta")
    ecs_log.reset_fingerprint_secret()
    try:
        second = ecs_log.fingerprint("+44 7700 900077")
    finally:
        ecs_log.reset_fingerprint_secret()
    assert first != second
    assert first != _unsalted("+44 7700 900077")


def test_decision_log_has_no_raw_or_unsalted_values(monkeypatch, tmp_path):
    """Low-entropy values are unrecoverable from the log file."""
    monkeypatch.setenv("SF_FINGERPRINT_SECRET", "test-secret-alpha")
    ecs_log.reset_fingerprint_secret()
    try:
        log = str(tmp_path / "decisions.jsonl")
        text = "call +44 7700 900077, born 1990-01-01"
        decision = engine.gate(
            text, load_policy(), CanaryRegistry("t69", ("db_password",))
        )
        ecs_log.append_decision(log, decision, text)
        raw = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8")
        assert "+44 7700 900077" not in raw
        assert "1990-01-01" not in raw
        assert _unsalted("+44 7700 900077") not in raw
        assert _unsalted("1990-01-01") not in raw
        assert ecs_log.verify(log) == (True, 1)
    finally:
        ecs_log.reset_fingerprint_secret()


def test_old_unsalted_chain_still_verifies(tmp_path, fp_secret):
    """Old events keep old fingerprints; the chain still verifies."""
    log = str(tmp_path / "decisions.jsonl")
    record = ecs_log.append_decision(log, _gate("hello there"), "hello there")
    record["fingerprints"] = [_unsalted("legacy")]
    lines = [
        json.dumps(record, sort_keys=True),
    ]
    # Re-chain by hand: recompute the single genesis-chained hash.
    stored = dict(record)
    stored.pop("hash", None)
    stored["hash"] = ecs_log._hash_record(
        {k: v for k, v in stored.items() if k != "hash"}
    )
    (tmp_path / "decisions.jsonl").write_text(
        json.dumps(stored, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert lines  # silence linters about the draft above
    assert ecs_log.verify(log) == (True, 1)


# --- choice events -------------------------------------------------------


def test_choice_event_shape_and_chain(tmp_path, fp_secret):
    log = str(tmp_path / "decisions.jsonl")
    text = "mail ada@corvanelabs.com now"
    decision = ecs_log.append_decision(log, _gate(text), text)
    picked = ecs_log.append_choice(
        log,
        decision_id=decision["decision_id"],
        choice="send_anyway",
        kinds=["pii:email"],
        rule_ids=["personal-data"],
        site="chatgpt.com",
        source="text",
    )
    assert set(picked) == {
        "event", "ts", "decision_id", "choice", "kinds", "rule_ids",
        "site", "source", "prev_hash", "hash",
    }
    assert picked["event"] == "choice"
    assert picked["prev_hash"] == decision["hash"]
    assert ecs_log.verify(log) == (True, 2)


def test_choice_fields_reject_values(tmp_path, fp_secret):
    log = str(tmp_path / "decisions.jsonl")
    with pytest.raises(ValueError):
        ecs_log.append_choice(
            log, decision_id="abc123", choice="send_anyway",
            kinds=["ada@corvanelabs.com"],
        )
    with pytest.raises(ValueError):
        ecs_log.append_choice(
            log, decision_id="abc123", choice="send anyway plz",
            kinds=["pii:email"],
        )
    with pytest.raises(ValueError):
        ecs_log.append_choice(
            log, decision_id="not an id!", choice="cancel", kinds=[],
        )
    assert ecs_log.clean_site("https://ChatGPT.com/foo?x=1") == "chatgpt.com"
    assert ecs_log.clean_site("chatgpt.com:443") == "chatgpt.com"
    assert ecs_log.clean_site("not a host!!") == "app"
    assert ecs_log.verify(log) == (True, 0)


def test_choice_fuzz_no_values_in_log(tmp_path, fp_secret):
    values = [
        "ada@corvanelabs.com",
        "bob.bexley@example.org",
        "+44 7700 900077",
        "020 7946 0018",
        "Ada Lovelace",
        "1990-01-01",
        "12 Holly Lane",
        "SW1A 1AA",
        "TALLOW",
        "card 4111 1111 1111 1111",
    ]
    log = str(tmp_path / "decisions.jsonl")
    for index, value in enumerate(values):
        text = f"note {index}: {value} here"
        decision = _gate(text)
        stored = ecs_log.append_decision(log, decision, text)
        kinds = [
            kind
            for kind in (ecs_log.span_kind(s) for s in decision.spans)
            if kind
        ]
        ecs_log.append_choice(
            log,
            decision_id=stored["decision_id"],
            choice="cancel" if index % 2 else "send_anyway",
            kinds=kinds,
            rule_ids=_decision_rule_ids(decision),
            site="gemini.google.com",
        )
    raw = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8")
    for value in values:
        assert value not in raw
        assert _unsalted(value) not in raw
    assert ecs_log.verify(log)[0] is True


def test_api_choice_roundtrip(tmp_path, fp_secret):
    body = record_choice(
        ChoiceRequest(
            decision_id="abc123",
            choice="cancel",
            kinds=["pii:email"],
            site="claude.ai",
        )
    )
    assert body["ok"] is True
    log = Path(ecs_log.resolve_path(None))
    raw = log.read_text(encoding="utf-8")
    assert "claude.ai" in raw
    assert '"personal-data"' in raw  # rule ids derived from kinds
    with pytest.raises(HTTPException) as exc:
        record_choice(
            ChoiceRequest(
                decision_id="abc123", choice="explode", kinds=[]
            )
        )
    assert exc.value.status_code == 422


def test_ext_choice_ok_and_bad(tmp_path, fp_secret):
    body = ext_choice(
        ChoiceBody(
            decision_id="ext123",
            choice="send_anyway",
            kinds=["pii:email"],
            site="chatgpt.com",
        ),
        None,
    )
    assert body["ok"] is True
    with pytest.raises(HTTPException) as exc:
        ext_choice(
            ChoiceBody(
                decision_id="ext123", choice="maybe", kinds=[]
            ),
            None,
        )
    assert exc.value.status_code == 422


def test_routes_registered():
    table = route_methods(app)
    assert table["/api/choice"] == ["POST"]
    assert table["/api/history"] == ["DELETE", "GET"]
    assert table["/api/suggestions"] == ["GET"]
    assert table["/api/suggestions/{suggestion_id}/apply"] == ["POST"]
    assert table["/api/suggestions/{suggestion_id}/dismiss"] == ["POST"]
    assert table["/api/suggestions/{suggestion_id}/undo"] == ["POST"]
    assert table["/api/ext/choice"] == ["POST"]
    assert table["/api/uploads/{file_id}/redacted-copy"] == ["POST"]


# --- suggestion rule: auto_redact ----------------------------------------


def test_auto_redact_triggers_at_8_of_10(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(8):
        ecs_log.append_choice(
            log, decision_id=f"s{i:04d}", choice="send_anyway",
            kinds=["pii:email"],
        )
    for i in range(2):
        ecs_log.append_choice(
            log, decision_id=f"c{i:04d}", choice="cancel",
            kinds=["pii:email"],
        )
    events = ecs_log.read_events(None)
    assert len(events) == 10
    found = suggest.collect_rules()
    assert [item["id"] for item in found] == ["auto-redact-pii-email"]
    item = found[0]
    assert item["type"] == "auto_redact"
    assert "8 of the last 10" in item["evidence"]
    assert "auto_redact" in item["diff"]
    assert "pii:email" in item["yaml_after"]
    assert item["needs_input"] is False


def test_auto_redact_not_below_threshold(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    _seed_choices(log, "pii:email", 7, "send_anyway")
    _seed_choices(log, "pii:email", 3, "cancel")
    assert suggest.collect_rules() == []


def test_auto_redact_needs_ten_samples(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    _seed_choices(log, "pii:email", 9, "send_anyway")
    assert suggest.collect_rules() == []


def test_auto_redact_site_scoped(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(8):
        ecs_log.append_choice(
            log, decision_id=f"s{i:04d}", choice="send_anyway",
            kinds=["pii:phone"], site="chatgpt.com",
        )
    for i in range(2):
        ecs_log.append_choice(
            log, decision_id=f"t{i:04d}", choice="send_anyway",
            kinds=["pii:phone"], site="app",
        )
    (found,) = suggest.collect_rules()
    assert found["site"] == "chatgpt.com"
    assert found["id"] == "auto-redact-pii-phone-at-chatgpt-com"


def test_auto_redact_global_when_sites_mixed(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(5):
        ecs_log.append_choice(
            log, decision_id=f"s{i:04d}", choice="send_anyway",
            kinds=["pii:phone"], site="chatgpt.com",
        )
    for i in range(5):
        ecs_log.append_choice(
            log, decision_id=f"t{i:04d}", choice="send_anyway",
            kinds=["pii:phone"], site="gemini.google.com",
        )
    (found,) = suggest.collect_rules()
    assert found["site"] == ""


# --- suggestion rule: cue_allow ------------------------------------------


def _seed_cue_pair(log, decision_id, text, with_spans=False):
    spans = []
    if with_spans:
        spans = _gate("mail ada@corvanelabs.com now").spans
    held = engine.Decision(decision="HOLD", decision_id=decision_id, spans=spans)
    ecs_log.append_decision(log, held, text)
    released = engine.Decision(decision="ALLOW", decision_id=decision_id, spans=[])
    ecs_log.append_decision(log, released, text)


def test_cue_allow_triggers_at_10(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(10):
        _seed_cue_pair(log, f"cue{i:02d}", "she was born here")
    (found,) = suggest.collect_rules()
    assert found["id"] == "cue-allow-born"
    assert found["type"] == "cue_allow"
    assert "'born'" in found["evidence"]
    assert "cue_allow" in found["diff"]


def test_cue_allow_not_below_threshold(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(9):
        _seed_cue_pair(log, f"cue{i:02d}", "she was born here")
    assert suggest.collect_rules() == []


def test_cue_allow_ignores_findings(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(10):
        _seed_cue_pair(log, f"cue{i:02d}", "she was born here", with_spans=True)
    assert suggest.collect_rules() == []


def test_cue_allow_ignores_unclean_release(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(10):
        held = engine.Decision(decision="HOLD", decision_id=f"cue{i:02d}")
        ecs_log.append_decision(log, held, "she was born here")
        redacted = engine.Decision(decision="REDACT", decision_id=f"cue{i:02d}")
        ecs_log.append_decision(log, redacted, "she was born here")
    assert suggest.collect_rules() == []


# --- suggestion rule: block_kind -----------------------------------------


def _seed_redact(log, email):
    text = f"mail {email} now"
    decision = _gate(text)
    assert decision.decision == "REDACT"
    return ecs_log.append_decision(log, decision, text)


def test_block_kind_triggers_at_5_all_cancel(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(5):
        # Distinct domains: the org-domain rule must not cross-fire here.
        stored = _seed_redact(log, f"user{i}@example{i}.com")
        ecs_log.append_choice(
            log, decision_id=stored["decision_id"], choice="cancel",
            kinds=["pii:email"],
        )
    (found,) = suggest.collect_rules()
    assert found["id"] == "block-pii-email"
    assert found["type"] == "block_kind"
    assert "cancelled every time" in found["evidence"]
    assert "block_kinds" in found["diff"]


def test_block_kind_not_below_threshold(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(4):
        stored = _seed_redact(log, f"user{i}@example{i}.com")
        ecs_log.append_choice(
            log, decision_id=stored["decision_id"], choice="cancel",
            kinds=["pii:email"],
        )
    assert suggest.collect_rules() == []


def test_block_kind_needs_always_cancel(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(5):
        stored = _seed_redact(log, f"user{i}@example{i}.com")
        ecs_log.append_choice(
            log, decision_id=stored["decision_id"],
            choice="send_anyway" if i == 4 else "cancel",
            kinds=["pii:email"],
        )
    assert suggest.collect_rules() == []


# --- suggestion rule: org_domain -----------------------------------------


def test_org_domain_triggers_at_5(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(5):
        _seed_redact(log, f"user{i}@example.com")
    (found,) = suggest.collect_rules()
    assert found["type"] == "org_domain"
    assert found["domain_fp"] == ecs_log.fingerprint("domain:example.com")
    assert found["needs_input"] is True
    assert "Is this email domain yours?" in found["title"]
    assert "org_domains" in found["diff"]


def test_org_domain_not_below_threshold(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(4):
        _seed_redact(log, f"user{i}@example.com")
    assert suggest.collect_rules() == []


def test_org_domain_skips_allowlisted(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(5):
        _seed_redact(log, f"user{i}@example.com")
    assert len(suggest.collect_rules()) == 1
    (habit_env / "policy.local.yaml").write_text(
        "org_domains:\n  - example.com\n", encoding="utf-8"
    )
    assert suggest.collect_rules() == []


# --- hard limits ---------------------------------------------------------


def test_no_suggestion_ever_relaxes_secrets(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(100):
        ecs_log.append_choice(
            log, decision_id=f"sec{i:04d}", choice="send_anyway",
            kinds=["secret:provider_key"],
        )
    assert suggest.collect_rules() == []


@pytest.mark.parametrize(
    "rule_id",
    ["canary-tokens", "api-keys", "full-identity-bundle", "person-search"],
)
def test_protected_rules_never_relaxed(habit_env, fp_secret, rule_id):
    log = ecs_log.resolve_path(None)
    _seed_choices(
        log, "pii:email", 10, "send_anyway", rule_ids=[rule_id]
    )
    assert suggest.collect_rules() == []


def test_secret_cues_never_suggested(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(10):
        _seed_cue_pair(log, f"cue{i:02d}", "my password is here")
    assert suggest.collect_rules() == []


def test_loader_enforces_hard_limits():
    with pytest.raises(ValueError):
        policy_module.LocalPolicy.model_validate(
            {"auto_redact": [{"kind": "secret:provider_key"}]}
        )
    with pytest.raises(ValueError):
        policy_module.LocalPolicy.model_validate(
            {"block_kinds": ["secret:provider_key"]}
        )
    with pytest.raises(ValueError):
        policy_module.LocalPolicy.model_validate({"cue_allow": ["password"]})
    with pytest.raises(ValueError):
        policy_module.LocalPolicy.model_validate(
            {"org_domains": ["not a domain"]}
        )
    local = policy_module.LocalPolicy.model_validate(
        {
            "auto_redact": [{"kind": "pii:email", "site": "chatgpt.com"}],
            "cue_allow": ["born"],
            "block_kinds": ["pii:phone", "business:codename"],
            "org_domains": ["example.com"],
        }
    )
    assert local.org_domains == ["example.com"]


def test_secret_cues_match_engine():
    for cue in sorted(policy_module.SECRET_CUES):
        assert engine.cue_word(f"the {cue} is here") == cue


# --- apply / dismiss / undo ----------------------------------------------


def test_apply_undo_roundtrip_no_previous(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    _seed_choices(log, "pii:email", 10, "send_anyway")
    (item,) = suggest.open_suggestions()
    assert item["yaml_before"] == ""
    assert item["diff"]
    result = suggest.apply_suggestion(item["id"])
    assert result["applied"] is True
    assert result["backup"] == ""
    local = policy_module.load_local_policy()
    assert [entry.kind for entry in local.auto_redact] == ["pii:email"]
    # Re-deriving skips the now-covered kind.
    assert suggest.open_suggestions() == []
    events = ecs_log.read_events(None)
    assert any(
        event.get("event") == "rule_change"
        and event.get("action") == "applied"
        for event in events
    )
    assert any(
        event.get("event") == "choice"
        and event.get("choice") == "apply_suggestion"
        for event in events
    )
    assert ecs_log.verify(None)[0] is True
    undone = suggest.undo_suggestion(item["id"])
    assert undone["undone"] is True
    assert not (habit_env / "policy.local.yaml").exists()
    assert any(
        event.get("event") == "rule_change"
        and event.get("action") == "undone"
        for event in ecs_log.read_events(None)
    )
    with pytest.raises(KeyError):
        suggest.undo_suggestion(item["id"])


def test_apply_undo_roundtrip_with_previous(habit_env, fp_secret):
    previous = (
        "custom_rules:\n"
        "  - id: abc123\n"
        "    kind: codename\n"
        "    value: AB\n"
        "    action: redact\n"
        "    note: ''\n"
    )
    (habit_env / "policy.local.yaml").write_text(previous, encoding="utf-8")
    log = ecs_log.resolve_path(None)
    _seed_choices(log, "pii:email", 10, "send_anyway")
    (item,) = suggest.open_suggestions()
    assert "AB" in item["yaml_before"]
    result = suggest.apply_suggestion(item["id"])
    assert ".bak-" in result["backup"]
    backup_text = Path(result["backup"]).read_text(encoding="utf-8")
    assert backup_text == previous
    merged = (habit_env / "policy.local.yaml").read_text(encoding="utf-8")
    assert "AB" in merged and "auto_redact" in merged
    suggest.undo_suggestion(item["id"])
    assert (habit_env / "policy.local.yaml").read_text(encoding="utf-8") == previous


def test_apply_org_domain_checks_person_typed_it(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    for i in range(5):
        _seed_redact(log, f"user{i}@example.com")
    (item,) = suggest.open_suggestions()
    with pytest.raises(ValueError):
        suggest.apply_suggestion(item["id"], domain="other.com")
    assert not (habit_env / "policy.local.yaml").exists()
    result = suggest.apply_suggestion(item["id"], domain="Example.COM")
    assert result["applied"] is True
    local = policy_module.load_local_policy()
    assert local.org_domains == ["example.com"]
    with pytest.raises(KeyError):
        suggest.apply_suggestion(item["id"], domain="example.com")


def test_dismiss_hides_and_logs(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    _seed_choices(log, "pii:email", 10, "send_anyway")
    (item,) = suggest.open_suggestions()
    assert suggest.dismiss_suggestion(item["id"])["dismissed"] is True
    assert suggest.open_suggestions() == []
    assert any(
        event.get("event") == "choice"
        and event.get("choice") == "dismiss_suggestion"
        for event in ecs_log.read_events(None)
    )
    with pytest.raises(KeyError):
        suggest.dismiss_suggestion(item["id"])
    with pytest.raises(KeyError):
        suggest.apply_suggestion("nope-missing")


def test_suggestions_state_file_written(habit_env, fp_secret):
    import yaml

    log = ecs_log.resolve_path(None)
    _seed_choices(log, "pii:email", 10, "send_anyway")
    suggest.open_suggestions()
    state_path = Path(suggest.resolve_suggestions_path(None))
    assert state_path.name == "rule_suggestions.yaml"
    data = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    assert [item["id"] for item in data["suggestions"]] == [
        "auto-redact-pii-email"
    ]
    assert data["dismissed"] == []
    assert data["applied"] == []


def test_apply_endpoint_resets_policy_cache(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    _seed_choices(log, "pii:email", 10, "send_anyway")
    (item,) = list_suggestions()["suggestions"]
    result = apply_suggestion(item["id"], SuggestionApplyRequest())
    assert result["applied"] is True
    assert [entry.kind for entry in get_policy().auto_redact] == ["pii:email"]
    assert undo_suggestion(item["id"])["undone"] is True
    assert get_policy().auto_redact == []
    with pytest.raises(HTTPException) as exc:
        dismiss_suggestion("nope-missing")
    assert exc.value.status_code == 404


# --- hosted mode ---------------------------------------------------------


def test_hosted_disables_suggestions_and_panel(monkeypatch, habit_env, fp_secret):
    from shadowfenced import hosted as hosted_module

    monkeypatch.setenv("SF_HOSTED", "1")
    try:
        payload = list_suggestions()
        assert payload["disabled"] is True
        assert payload["notice"] == hosted_module.HOSTED_BANNER
        assert payload["suggestions"] == []
        panel = history()
        assert panel["hosted"] is True
        assert panel["notice"] == hosted_module.HOSTED_BANNER
        assert panel["events"] == []
        with pytest.raises(HTTPException) as exc:
            apply_suggestion("whatever", SuggestionApplyRequest())
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            dismiss_suggestion("whatever")
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            undo_suggestion("whatever")
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            delete_history()
        assert exc.value.status_code == 403
    finally:
        policy_module.reset_hosted_rules()


# --- history panel -------------------------------------------------------


def test_history_joins_decisions_and_choices(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    first = _seed_redact(log, "user1@example.com")
    ecs_log.append_choice(
        log, decision_id=first["decision_id"], choice="send_anyway",
        kinds=["pii:email"],
    )
    second = _seed_redact(log, "user2@example.com")
    ecs_log.append_choice(
        log, decision_id=second["decision_id"], choice="cancel",
        kinds=["pii:email"],
    )
    clean = _gate("hello there")
    ecs_log.append_decision(log, clean, "hello there")
    panel = history()
    assert panel["hosted"] is False
    assert [row["decision"] for row in panel["events"]] == [
        "ALLOW", "REDACT", "REDACT",
    ]
    by_id = {row["decision_id"]: row for row in panel["events"]}
    assert by_id[first["decision_id"]]["choice"] == "send_anyway"
    assert by_id[second["decision_id"]]["choice"] == "cancel"
    row = by_id[first["decision_id"]]
    assert row["site"] == "app"
    assert row["kinds"] == ["pii:email"]
    assert row["rule_ids"] == ["personal-data"]
    assert panel["counts_7d"] == {"pii:email": 2}
    assert panel["counts_30d"] == {"pii:email": 2}
    assert panel["sites"] == ["app"]
    assert panel["kinds"] == ["pii:email"]
    assert len(history(kind="pii:email")["events"]) == 2
    assert history(kind="pii:phone")["events"] == []
    assert len(history(site="app")["events"]) == 3
    assert history(site="chatgpt.com")["events"] == []


def test_delete_history_truncates_to_marker(habit_env, fp_secret):
    log = ecs_log.resolve_path(None)
    text = "mail ada@corvanelabs.com now"
    stored = ecs_log.append_decision(log, _gate(text), text)
    tail = stored["hash"]
    result = delete_history()
    assert result == {"ok": True, "ts": result["ts"], "events": 1}
    lines = Path(log).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    marker = json.loads(lines[0])
    assert marker["event"] == "history_cleared"
    assert marker["prev_hash"] == tail
    raw = Path(log).read_text(encoding="utf-8")
    assert "ada@corvanelabs.com" not in raw
    assert ecs_log.verify(None) == (True, 1)
    ecs_log.append_decision(log, _gate("hello there"), "hello there")
    assert ecs_log.verify(None) == (True, 2)


def test_history_notice_copy():
    page = (
        Path(__file__).resolve().parents[1]
        / "src" / "shadowfenced" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert "Only the type of item is recorded — never what you typed." in page


# --- gate wiring ---------------------------------------------------------


def _registry():
    return CanaryRegistry("t69", ("db_password",))


def test_cue_allow_gate_passthrough():
    policy = load_policy()
    assert (
        engine.gate("she was born here", policy, _registry(), mode="send").decision
        == "HOLD"
    )
    policy.cue_allow = ["born"]
    decision = engine.gate("she was born here", policy, _registry(), mode="send")
    assert decision.decision == "ALLOW"
    still_held = engine.gate(
        "she was born here, mail ada@corvanelabs.com now",
        policy, _registry(), mode="send",
    )
    assert still_held.decision == "HOLD"


def test_block_kinds_gate():
    policy = load_policy()
    assert (
        engine.gate("mail ada@corvanelabs.com now", policy, _registry()).decision
        == "REDACT"
    )
    policy.block_kinds = ["pii:email"]
    assert (
        engine.gate("mail ada@corvanelabs.com now", policy, _registry()).decision
        == "BLOCK"
    )


def test_org_domain_allowlist_gate():
    policy = load_policy()
    policy.org_domains = ["example.com"]
    assert (
        engine.gate("mail a@example.com now", policy, _registry()).decision
        == "ALLOW"
    )
    assert (
        engine.gate("mail a@other.com now", policy, _registry()).decision
        == "REDACT"
    )


# --- auto-confirm --------------------------------------------------------


def _warn_with_email_spans():
    base = _gate("mail ada@corvanelabs.com now")
    assert base.decision == "REDACT"
    return engine.Decision(
        decision="WARN",
        decision_id=uuid.uuid4().hex,
        spans=base.spans,
        sent_text=base.sent_text,
        placeholders=dict(base.placeholders),
        resolved_tier=1,
        latency_ms={},
        reasons=["re-id risk"],
        evidence={
            "reid": {
                "banner": "risk",
                "generalised_text": "someone",
                "combination": ["email"],
                "outcome": "WARN",
            }
        },
        rule_ids=["personal-data"],
    )


def test_web_auto_confirm_warn(habit_env, fp_secret, mock_upstream):
    (habit_env / "policy.local.yaml").write_text(
        "auto_redact:\n  - kind: pii:email\n", encoding="utf-8"
    )
    reset_state()
    try:
        warn = _warn_with_email_spans()
        text = "mail ada@corvanelabs.com now"
        response = _maybe_auto_confirm_warn(
            warn, text, [{"role": "user", "content": text}], None
        )
        assert response is not None
        assert response["answer"] == "ok"
        with pytest.raises(HTTPException):
            confirm_warn(warn.decision_id, "send_anyway")
        events = ecs_log.read_events(None)
        assert [
            event for event in events if event.get("auto_rule") == "auto_redact"
        ]
        assert not [event for event in events if event.get("event") == "choice"]
    finally:
        _warn_store_clear()
        reset_state()


def test_web_no_auto_without_rule_or_when_protected(habit_env, fp_secret):
    text = "mail ada@corvanelabs.com now"
    warn = _warn_with_email_spans()
    assert (
        _maybe_auto_confirm_warn(
            warn, text, [{"role": "user", "content": text}], None
        )
        is None
    )
    (habit_env / "policy.local.yaml").write_text(
        "auto_redact:\n  - kind: pii:email\n", encoding="utf-8"
    )
    reset_state()
    try:
        warn.rule_ids = ["person-search"]
        assert (
            _maybe_auto_confirm_warn(
                warn, text, [{"role": "user", "content": text}], None
            )
            is None
        )
    finally:
        reset_state()


def test_ext_auto_convert_warn(habit_env, fp_secret, monkeypatch):
    (habit_env / "policy.local.yaml").write_text(
        "auto_redact:\n  - kind: pii:email\n    site: chatgpt.com\n",
        encoding="utf-8",
    )
    reset_state()
    try:
        warn = _warn_with_email_spans()
        monkeypatch.setattr(ext_api_module, "_live_gate", lambda text: warn)
        body = ext_redact(
            RedactBody(
                text="mail ada@corvanelabs.com now", site="chatgpt.com"
            ),
            None,
        )
        assert body["decision"] == "REDACT"
        assert "Hidden automatically" in body["notes"]
        assert "[PII_EMAIL_1]" in body["safe_text"]
        events = ecs_log.read_events(None)
        assert events[0]["site"] == "chatgpt.com"
        assert events[0]["auto_rule"] == "auto_redact"
    finally:
        reset_state()


def test_ext_redact_logs_and_returns_extras(habit_env, fp_secret):
    body = ext_redact(
        RedactBody(text="mail ada@corvanelabs.com now", site="gemini.google.com"),
        None,
    )
    assert body["decision"] == "REDACT"
    assert body["kinds"] == ["pii:email"]
    assert body["rule_ids"] == ["personal-data"]
    assert body["decision_id"]
    events = ecs_log.read_events(None)
    assert len(events) == 1
    assert events[0]["site"] == "gemini.google.com"
    assert events[0]["decision_id"] == body["decision_id"]


# --- redacted copy -------------------------------------------------------


def test_redacted_copy_roundtrip(habit_env, fp_secret):
    uploads_module.reset_for_tests()
    try:
        record = uploads_module.UPLOADS.save(
            "notes.txt", "document", "mail ada@corvanelabs.com now", "flagged"
        )
        body = upload_redacted_copy(record.file_id)
        assert "[PII_EMAIL_1]" in body["redacted_text"]
        assert "ada@corvanelabs.com" not in body["redacted_text"]
        assert body["placeholders"] == ["[PII_EMAIL_1]"]
        assert any(
            event.get("event") == "choice"
            and event.get("choice") == "redacted_copy"
            and event.get("source") == "file"
            for event in ecs_log.read_events(None)
        )
        with pytest.raises(HTTPException) as exc:
            upload_redacted_copy("missing-file-id")
        assert exc.value.status_code == 404
        image = uploads_module.UPLOADS.save("pic.png", "image", "", "flagged")
        with pytest.raises(HTTPException) as exc:
            upload_redacted_copy(image.file_id)
        assert exc.value.status_code == 409
    finally:
        uploads_module.reset_for_tests()

