"""T57: cue-word background Glimmer review shows a CHECKING card.

Live case: "Summarise the life of Ada Lovelace, born 10 December 1815 in
London." The word "born" is a sensitivity cue, so /api/send HOLDs and Muse
Glimmer reviews in the background. While the review runs the risk card must
show the compact CHECKING layout (message text as Sending, the named cue in
Why, "Checking... (Ns)" as What) with no choice buttons. When the review
releases, the card is replaced by the quiet receipt and the drawer moves
from HOLD to the final decision; when it ends in a real hold the card
switches to the normal HOLD card with its choices. The server reason names
only the cue word, never other message text.
"""

from __future__ import annotations

from pathlib import Path

from shadowfenced import engine
from shadowfenced.canary import CanaryRegistry
from shadowfenced.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "shadowfenced" / "static"
SEED = "test-seed-t57"
KINDS = ("db_password", "api_key")

ADA = "Summarise the life of Ada Lovelace, born 10 December 1815 in London."


def _policy():
    return load_policy(ROOT / "policy.yaml")


def _registry():
    return CanaryRegistry(SEED, KINDS)


def _js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


# --- server: the reason names the cue word only -------------------------------


def test_cue_hold_reason_names_born_cue():
    decision = engine.gate(
        ADA, _policy(), _registry(), mode="send",
        person_search_judge_fn=lambda _text: None,
    )
    assert decision.decision == "HOLD"
    cue_reasons = [r for r in decision.reasons if "cue=" in r]
    assert len(cue_reasons) == 1
    assert cue_reasons[0] == (
        "sensitivity cue word needs Glimmer review (cue='born') -> HOLD"
    )


def test_cue_reason_echoes_no_other_message_text():
    decision = engine.gate(
        ADA, _policy(), _registry(), mode="send",
        person_search_judge_fn=lambda _text: None,
    )
    cue_reasons = [r for r in decision.reasons if "cue=" in r]
    assert cue_reasons, decision.reasons
    for fragment in ("Ada Lovelace", "10 December", "1815", "London", "Summarise"):
        assert fragment not in cue_reasons[0], fragment


def test_cue_word_normalises_case_and_spacing():
    assert engine.cue_word("BORN in Leeds") == "born"
    assert engine.cue_word("date  of   birth: 1990") == "date of birth"
    assert engine.cue_word("nothing sensitive here") is None
    assert engine.cue_word(r"open \\db01\export") == "unc path"


# --- UI state model: CHECKING while the background review runs ----------------


def test_checking_card_shows_message_cue_and_elapsed():
    js = _js()
    assert "function isCueCheckingHold(resp)" in js
    assert "function holdCueWord(resp)" in js
    assert "function checkingCardModel(resp, elapsedSec)" in js
    assert '"CHECKING"' in js
    assert "holdPendingMessage(resp)" in js
    assert "Contains a word that can point to personal data" in js
    assert "Muse Glimmer is checking it on this PC" in js
    assert "Checking\u2026 (" in js


def test_checking_card_offers_no_choices_until_a_real_hold():
    js = _js()
    # No approve/deny buttons while checking: choices are offered only via
    # offerChoice, which runs solely on timeout and needs_human.
    assert js.count("offerChoice();") == 2
    assert "cols.hidden = !!checkingCue;" in js
    assert "previewWrap.hidden = !!checkingCue;" in js
    # A real hold (needs_human or timeout) repaints the normal HOLD card.
    assert js.count("paintHoldModel(card, riskCardModel(holdBody)") == 2
    assert '"Send without flagged part"' in js
    assert '"Don\'t send"' in js


def test_released_replaced_by_quiet_receipt_and_drawer_updates():
    js = _js()
    assert (
        'QUIET_RELEASE_LABEL = "\u2713 Checked by Muse Glimmer '
        "\u00b7 nothing sensitive\""
    ) in js
    assert "if (card.parentNode) card.parentNode.removeChild(card);" in js
    assert (
        "updateHoldTurn(turnEntry, banner, releaseFinal, releaseLabel, body);"
    ) in js
    # The drawer entry moves from HOLD to the final decision and repaints.
    assert "turn.decision = key;" in js
    assert "if (selectedTurn === turn) paintTurn(turn);" in js
    assert "renderHoldCard(body.decision_id, banner, body, turnEntry);" in js


def test_checking_card_amber_and_documented():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert '.risk-card[data-decision="CHECKING"]' in css
    docs = (ROOT / "docs" / "UI_STATES.md").read_text(encoding="utf-8")
    assert "CHECKING" in docs
    assert "can point to personal data" in docs
    assert "Checked by Muse Glimmer" in docs
