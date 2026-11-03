"""Rule suggestions from habit patterns (T69).

Derives suggestions only from log patterns (kinds, rules, decisions,
choices, sites, cue words), never from values. No model training.

Rules:

- ``auto_redact``: one PII kind sent anyway in >= 8 of the last 10
  choices (7 days) -> hide it automatically and send.
- ``cue_allow``: one cue word held and cleanly released >= 10 times
  (30 days) -> let it pass without the model check when no Tier 1
  finding is present.
- ``block_kind``: one kind hidden >= 5 times (30 days) where the
  person cancelled every time -> block it outright.
- ``org_domain``: one repeated email-domain fingerprint hidden >= 5
  times (30 days) -> ask "Is <domain> yours?" The domain text is
  entered by the person in the card; the suggester only knows the
  fingerprint.

Hard limits (also enforced by the policy loader): suggestions never
relax secrets, canaries, keys, the full identity bundle, or
person-search rules.
"""

from __future__ import annotations

import difflib
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from . import ecs_log
from . import policy as policy_module

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUGGESTIONS_PATH = "runs/rule_suggestions.yaml"

#: Rules that must never be relaxed by any suggestion.
PROTECTED_RULE_IDS = frozenset(
    {"canary-tokens", "api-keys", "full-identity-bundle", "person-search"}
)

#: Same kind sent anyway in >= 8 of the last 10 choices (7 days).
SEND_ANYWAY_WINDOW = 10
SEND_ANYWAY_MIN = 8
SEND_ANYWAY_DAYS = 7

#: Cue held and cleanly released >= 10 times (30 days).
CUE_RELEASES_MIN = 10
CUE_RELEASES_DAYS = 30

#: Kind hidden >= 5 times with every choice a cancel (30 days).
CANCEL_REDACTS_MIN = 5
CANCEL_REDACTS_DAYS = 30

#: One email-domain fingerprint hidden >= 5 times (30 days).
DOMAIN_HIDDEN_MIN = 5
DOMAIN_HIDDEN_DAYS = 30

KIND_LABELS = {
    "pii:email": "email addresses",
    "pii:phone": "phone numbers",
    "pii:name": "names",
    "pii:dob": "dates of birth",
    "pii:address": "addresses",
    "pii:street": "street addresses",
    "pii:postcode": "postcodes",
    "pii:city": "cities",
    "pii:card": "card numbers",
    "pii:account": "account numbers",
    "pii:routing": "routing numbers",
    "pii:iban": "IBANs",
    "pii:swift": "SWIFT codes",
    "pii:sort_code": "sort codes",
    "pii:ssn": "social security numbers",
    "business:codename": "codenames",
    "business:client": "client names",
    "business:figure": "unreleased figures",
    "business:custom_pattern": "custom-pattern matches",
    "business:contract_clause": "contract clauses",
}


def kind_label(kind: str) -> str:
    """Human plural for one kind label (fixed vocabulary only)."""
    if kind in KIND_LABELS:
        return KIND_LABELS[kind]
    _cls, _, sub = str(kind).partition(":")
    words = (sub or _cls).replace("_", " ").strip() or "items"
    return words if words.endswith("s") else words + "s"


def resolve_suggestions_path(explicit: str | Path | None = None) -> str:
    """State file: explicit arg, ``SF_SUGGESTIONS_PATH``, or the default."""
    if explicit:
        text = str(explicit)
    else:
        text = os.environ.get("SF_SUGGESTIONS_PATH", DEFAULT_SUGGESTIONS_PATH)
    if not text.strip():
        text = DEFAULT_SUGGESTIONS_PATH
    candidate = Path(text)
    if candidate.is_absolute():
        return str(candidate)
    return str(_PROJECT_ROOT / candidate)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _recent(events: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict[str, Any]] = []
    for event in events:
        moment = _parse_ts(event.get("ts"))
        if moment is not None and moment >= cutoff:
            out.append(event)
    return out


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("event") or "decision")


def _protected(event: dict[str, Any]) -> bool:
    rules = event.get("rule_ids") or []
    return any(str(item) in PROTECTED_RULE_IDS for item in rules)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")


# --- suggestion rules ----------------------------------------------------


def _auto_redact_rule(
    events: list[dict[str, Any]], local: policy_module.LocalPolicy
) -> list[dict[str, Any]]:
    """One PII kind sent anyway >= 8 of the last 10 choices (7 days)."""
    covered = {
        (entry.kind, entry.site) for entry in (local.auto_redact or [])
    }
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for event in _recent(events, SEND_ANYWAY_DAYS):
        if _event_type(event) != "choice":
            continue
        if _protected(event):
            continue
        for kind in event.get("kinds") or []:
            if str(kind).startswith("pii:"):
                by_kind.setdefault(str(kind), []).append(event)
    out: list[dict[str, Any]] = []
    for kind in sorted(by_kind):
        recent = by_kind[kind][-SEND_ANYWAY_WINDOW:]
        if len(recent) < SEND_ANYWAY_WINDOW:
            continue
        sent = sum(1 for item in recent if item.get("choice") == "send_anyway")
        if sent < SEND_ANYWAY_MIN:
            continue
        sites: dict[str, int] = {}
        for item in recent:
            site = str(item.get("site") or "app")
            sites[site] = sites.get(site, 0) + 1
        top_site, top_count = max(sites.items(), key=lambda kv: kv[1])
        # Per-site scoping is for chat sites; console patterns stay global.
        site = (
            top_site
            if top_count >= SEND_ANYWAY_MIN and top_site != "app"
            else ""
        )
        if (kind, site) in covered or (kind, "") in covered:
            continue
        slug = f"auto-redact-{_slug(kind)}"
        if site:
            slug += f"-at-{_slug(site)}"
        scope = f" on {site}" if site else ""
        out.append(
            {
                "id": slug,
                "type": "auto_redact",
                "kind": kind,
                "site": site,
                "title": f"Hide {kind_label(kind)} automatically and send",
                "detail": (
                    f"Future {kind_label(kind)}{scope} would be hidden "
                    "and sent without asking (REDACT instead of asking)."
                ),
                "evidence": (
                    f"you chose Send anyway {sent} of the last "
                    f"{SEND_ANYWAY_WINDOW} times in {SEND_ANYWAY_DAYS} days"
                ),
                "needs_input": False,
            }
        )
    return out


def _cue_allow_rule(
    events: list[dict[str, Any]], local: policy_module.LocalPolicy
) -> list[dict[str, Any]]:
    """One cue word held and cleanly released >= 10 times (30 days)."""
    allowed = set(local.cue_allow or [])
    held: dict[str, dict[str, Any]] = {}
    for event in events:
        if _event_type(event) != "decision":
            continue
        decision_id = event.get("decision_id")
        if not decision_id:
            continue
        if event.get("decision") == "HOLD" and not (event.get("classes") or []):
            cue = event.get("cue")
            if isinstance(cue, str) and cue:
                held.setdefault(str(decision_id), event)
    releases: dict[str, int] = {}
    for event in _recent(events, CUE_RELEASES_DAYS):
        if _event_type(event) != "decision":
            continue
        if event.get("decision") != "ALLOW":
            continue
        first = held.get(str(event.get("decision_id") or ""))
        if first is None:
            continue
        cue = str(first.get("cue") or "")
        if cue:
            releases[cue] = releases.get(cue, 0) + 1
    out: list[dict[str, Any]] = []
    for cue in sorted(releases):
        count = releases[cue]
        if count < CUE_RELEASES_MIN:
            continue
        if cue in policy_module.SECRET_CUES or cue in allowed:
            continue
        try:
            policy_module.validate_cue_allow(cue)
        except ValueError:
            continue
        out.append(
            {
                "id": f"cue-allow-{_slug(cue)}",
                "type": "cue_allow",
                "cue": cue,
                "site": "",
                "title": f"Let '{cue}' pass without the model check",
                "detail": (
                    f"'{cue}' would no longer hold the send for review "
                    "when no Tier 1 finding is present."
                ),
                "evidence": (
                    f"'{cue}' was held and released with nothing found "
                    f"{count} times in the last {CUE_RELEASES_DAYS} days"
                ),
                "needs_input": False,
            }
        )
    return out


def _block_kind_rule(
    events: list[dict[str, Any]], local: policy_module.LocalPolicy
) -> list[dict[str, Any]]:
    """One kind hidden >= 5 times where the person always cancelled."""
    blocked = set(local.block_kinds or [])
    latest_choice: dict[str, dict[str, Any]] = {}
    for event in events:
        if _event_type(event) != "choice":
            continue
        decision_id = event.get("decision_id")
        if decision_id:
            latest_choice[str(decision_id)] = event
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for event in _recent(events, CANCEL_REDACTS_DAYS):
        if _event_type(event) != "decision":
            continue
        if event.get("decision") != "REDACT":
            continue
        if _protected(event):
            continue
        choice = latest_choice.get(str(event.get("decision_id") or ""))
        if choice is None:
            continue
        for kind in event.get("kinds") or []:
            label = str(kind)
            if label.startswith("pii:") or label.startswith("business:"):
                by_kind.setdefault(label, []).append(choice)
    out: list[dict[str, Any]] = []
    for kind in sorted(by_kind):
        choices = by_kind[kind]
        if len(choices) < CANCEL_REDACTS_MIN:
            continue
        if any(item.get("choice") != "cancel" for item in choices):
            continue
        if kind in blocked:
            continue
        out.append(
            {
                "id": f"block-{_slug(kind)}",
                "type": "block_kind",
                "kind": kind,
                "site": "",
                "title": f"Block {kind_label(kind)} outright",
                "detail": (
                    f"Future {kind_label(kind)} would be blocked instead "
                    "of hidden and sent."
                ),
                "evidence": (
                    f"{kind_label(kind)} were hidden {len(choices)} times "
                    f"in the last {CANCEL_REDACTS_DAYS} days and you "
                    "cancelled every time"
                ),
                "needs_input": False,
            }
        )
    return out


def _known_domain_prints() -> set[str]:
    """HMAC prints of already-allowed domains (computed, never stored)."""
    try:
        policy = policy_module.load_policy()
    except Exception:
        return set()
    domains = list(policy.allowlist.email_domains) + list(policy.org_domains)
    prints: set[str] = set()
    for domain in domains:
        text = str(domain or "").strip().lower().rstrip(".")
        if not text:
            continue
        try:
            prints.add(ecs_log.fingerprint("domain:" + text))
        except Exception:
            continue
    return prints


def _org_domain_rule(
    events: list[dict[str, Any]], local: policy_module.LocalPolicy
) -> list[dict[str, Any]]:
    """One repeated email-domain fingerprint hidden >= 5 times."""
    del local
    known = _known_domain_prints()
    counts: dict[str, int] = {}
    for event in _recent(events, DOMAIN_HIDDEN_DAYS):
        if _event_type(event) != "decision":
            continue
        if event.get("decision") not in ("REDACT", "WARN"):
            continue
        for printed in event.get("domain_prints") or []:
            label = str(printed)
            if label:
                counts[label] = counts.get(label, 0) + 1
    out: list[dict[str, Any]] = []
    for printed in sorted(counts):
        count = counts[printed]
        if count < DOMAIN_HIDDEN_MIN:
            continue
        if printed in known:
            continue
        out.append(
            {
                "id": f"org-domain-{printed[:8]}",
                "type": "org_domain",
                "domain_fp": printed,
                "site": "",
                "title": "Is this email domain yours?",
                "detail": (
                    "One email domain keeps being hidden. Type it below "
                    "to add it as your organisation's domain; mail from "
                    "it will stop being hidden."
                ),
                "evidence": (
                    "emails from one repeated domain were hidden "
                    f"{count} times in the last {DOMAIN_HIDDEN_DAYS} days"
                ),
                "needs_input": True,
            }
        )
    return out


# --- YAML rendering ------------------------------------------------------


def _apply_to_document(
    doc: dict[str, Any], suggestion: dict[str, Any], domain: str = ""
) -> dict[str, Any]:
    """Return a copy of the local document with the suggestion merged."""
    merged: dict[str, Any] = {key: value for key, value in doc.items()}
    kind = str(suggestion.get("type") or "")
    if kind == "auto_redact":
        entries = list(merged.get("auto_redact") or [])
        entry: dict[str, Any] = {"kind": suggestion.get("kind")}
        if suggestion.get("site"):
            entry["site"] = suggestion.get("site")
        if entry not in entries and {"kind": entry["kind"]} not in entries:
            entries.append(entry)
        merged["auto_redact"] = entries
    elif kind == "cue_allow":
        cues = list(merged.get("cue_allow") or [])
        if suggestion.get("cue") not in cues:
            cues.append(suggestion.get("cue"))
        merged["cue_allow"] = cues
    elif kind == "block_kind":
        kinds = list(merged.get("block_kinds") or [])
        if suggestion.get("kind") not in kinds:
            kinds.append(suggestion.get("kind"))
        merged["block_kinds"] = kinds
    elif kind == "org_domain":
        domains = list(merged.get("org_domains") or [])
        if domain and domain not in domains:
            domains.append(domain)
        elif not domain and "<your domain here>" not in domains:
            domains.append("<your domain here>")
        merged["org_domains"] = domains
    else:
        raise ValueError(f"unknown suggestion type {kind!r}")
    return merged


def _render_yaml(
    suggestion: dict[str, Any], local_path: str | Path | None
) -> tuple[str, str, str]:
    """Before/after YAML plus the unified diff for one suggestion."""
    try:
        target = policy_module.local_policy_path() if local_path is None else Path(local_path)
        before = target.read_text(encoding="utf-8")
    except OSError:
        before = ""
    try:
        doc = policy_module.load_local_document(target)
    except ValueError:
        doc = {}
    after_doc = _apply_to_document(doc, suggestion)
    if suggestion.get("type") != "org_domain":
        # The exact change must validate before it is shown.
        policy_module.validate_local_document(after_doc)
    after = policy_module.dump_local_document(after_doc)
    diff = "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="policy.local.yaml (now)",
            tofile="policy.local.yaml (with suggestion)",
            lineterm="",
        )
    )
    return before, after, diff


# --- state file ----------------------------------------------------------


def _default_state() -> dict[str, Any]:
    return {"dismissed": [], "applied": [], "suggestions": []}


def load_state(explicit: str | Path | None = None) -> dict[str, Any]:
    """Read the suggestion state file ({} sections when missing)."""
    state = _default_state()
    path = Path(resolve_suggestions_path(explicit))
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return state
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError:
        return state
    if not isinstance(data, dict):
        return state
    if isinstance(data.get("dismissed"), list):
        state["dismissed"] = [str(item) for item in data["dismissed"]]
    if isinstance(data.get("applied"), list):
        kept: list[dict[str, Any]] = []
        for item in data["applied"]:
            if isinstance(item, dict) and item.get("id"):
                kept.append(
                    {
                        "id": str(item.get("id")),
                        "ts": str(item.get("ts") or ""),
                        "backup": str(item.get("backup") or ""),
                    }
                )
        state["applied"] = kept
    return state


def _write_state(
    state: dict[str, Any],
    suggestions: list[dict[str, Any]],
    explicit: str | Path | None = None,
) -> str:
    """Write open suggestions plus dismissed/applied ids to the file."""
    path = Path(resolve_suggestions_path(explicit))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "dismissed": list(state.get("dismissed") or []),
        "applied": list(state.get("applied") or []),
        "suggestions": suggestions,
    }
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return str(path)


# --- public API ----------------------------------------------------------


def collect_rules(
    log_path: str | None = None, local_path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Compute every suggestion the current log supports (no filtering)."""
    events = ecs_log.read_events(log_path)
    try:
        local = policy_module.load_local_policy(local_path)
    except ValueError:
        local = policy_module.LocalPolicy()
    found: list[dict[str, Any]] = []
    found.extend(_auto_redact_rule(events, local))
    found.extend(_cue_allow_rule(events, local))
    found.extend(_block_kind_rule(events, local))
    found.extend(_org_domain_rule(events, local))
    for suggestion in found:
        before, after, diff = _render_yaml(suggestion, local_path)
        suggestion["yaml_before"] = before
        suggestion["yaml_after"] = after
        suggestion["diff"] = diff
    return found


def open_suggestions(
    log_path: str | None = None,
    local_path: str | Path | None = None,
    state_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Open suggestions (fresh, minus dismissed/applied), also written out.

    The open list is written to ``runs/rule_suggestions.yaml`` so a
    developer can review it as a file.
    """
    state = load_state(state_path)
    hidden = set(state.get("dismissed") or []) | {
        str(item.get("id")) for item in state.get("applied") or []
    }
    fresh = [item for item in collect_rules(log_path, local_path) if item["id"] not in hidden]
    try:
        _write_state(state, fresh, state_path)
    except OSError:
        pass  # the mirror file is best-effort; the list is recomputed fresh
    return fresh


def _find_open(
    suggestion_id: str,
    log_path: str | None,
    local_path: str | Path | None,
    state_path: str | Path | None,
) -> dict[str, Any]:
    for suggestion in collect_rules(log_path, local_path):
        if suggestion["id"] == suggestion_id:
            return suggestion
    raise KeyError(f"unknown suggestion {suggestion_id!r}")


def _suggestion_kinds(suggestion: dict[str, Any]) -> list[str]:
    kind = suggestion.get("kind")
    return [str(kind)] if kind else []


def _log_apply_choice(
    log_path: str | None, suggestion: dict[str, Any], choice: str
) -> None:
    from . import rule_catalog as catalog_module

    kinds = _suggestion_kinds(suggestion)
    try:
        ecs_log.append_choice(
            log_path,
            decision_id=suggestion["id"],
            choice=choice,
            kinds=kinds,
            rule_ids=catalog_module.rule_ids_for_kinds(kinds),
            site=suggestion.get("site") or "app",
            source="text",
        )
    except ValueError:
        pass


def apply_suggestion(
    suggestion_id: str,
    *,
    log_path: str | None = None,
    local_path: str | Path | None = None,
    state_path: str | Path | None = None,
    domain: str = "",
) -> dict[str, Any]:
    """Apply one open suggestion: write the local file (undoable).

    Validates the merged file with the policy loader, keeps the
    previous file as ``policy.local.yaml.bak-<ts>``, logs a chained
    ``rule_change`` event plus the ``apply_suggestion`` choice, and
    records the backup for :func:`undo_suggestion`.
    """
    from . import hosted as hosted_module

    if hosted_module.is_hosted():
        raise PermissionError("suggestions are disabled in the public demo")
    state = load_state(state_path)
    if suggestion_id in set(state.get("dismissed") or []):
        raise KeyError(f"unknown suggestion {suggestion_id!r}")
    if suggestion_id in {str(item.get("id")) for item in state.get("applied") or []}:
        raise KeyError(f"suggestion {suggestion_id!r} is already applied")
    suggestion = _find_open(suggestion_id, log_path, local_path, state_path)
    clean_domain = ""
    if suggestion.get("type") == "org_domain":
        clean_domain = policy_module.validate_org_domain(domain)
        try:
            matches = ecs_log.fingerprint("domain:" + clean_domain) == str(
                suggestion.get("domain_fp") or ""
            )
        except Exception:
            matches = False
        if not matches:
            raise ValueError("that domain is not the repeated one")
    target = policy_module.local_policy_path() if local_path is None else Path(local_path)
    try:
        previous = target.read_text(encoding="utf-8")
        had_previous = True
    except OSError:
        previous = ""
        had_previous = False
    try:
        doc = policy_module.load_local_document(target)
    except ValueError:
        doc = {}
    merged = _apply_to_document(doc, suggestion, clean_domain)
    # The merged file must validate before anything is written.
    policy_module.validate_local_document(merged)
    backup = ""
    if had_previous:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        backup_path = Path(str(target) + f".bak-{stamp}")
        backup_path.write_text(previous, encoding="utf-8")
        backup = str(backup_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        policy_module.dump_local_document(merged), encoding="utf-8"
    )
    state.setdefault("applied", []).append(
        {
            "id": suggestion_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "backup": backup,
        }
    )
    _write_state(
        state,
        [item for item in collect_rules(log_path, local_path) if item["id"] != suggestion_id],
        state_path,
    )
    _target = str(
        suggestion.get("kind") or suggestion.get("cue") or suggestion.get("domain_fp") or ""
    )
    try:
        ecs_log.append_rule_change(
            log_path,
            suggestion_id=suggestion_id,
            type=str(suggestion.get("type") or ""),
            target=_target,
            action="applied",
            site=str(suggestion.get("site") or ""),
        )
    except ValueError:
        pass
    _log_apply_choice(log_path, suggestion, "apply_suggestion")
    return {"id": suggestion_id, "backup": backup, "applied": True}


def dismiss_suggestion(
    suggestion_id: str,
    *,
    log_path: str | None = None,
    local_path: str | Path | None = None,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Dismiss one open suggestion (hidden; logs the choice)."""
    from . import hosted as hosted_module

    if hosted_module.is_hosted():
        raise PermissionError("suggestions are disabled in the public demo")
    state = load_state(state_path)
    if suggestion_id in set(state.get("dismissed") or []):
        raise KeyError(f"unknown suggestion {suggestion_id!r}")
    suggestion = _find_open(suggestion_id, log_path, local_path, state_path)
    state.setdefault("dismissed", []).append(suggestion_id)
    _write_state(
        state,
        [item for item in collect_rules(log_path, local_path) if item["id"] != suggestion_id],
        state_path,
    )
    _log_apply_choice(log_path, suggestion, "dismiss_suggestion")
    return {"id": suggestion_id, "dismissed": True}


def undo_suggestion(
    suggestion_id: str,
    *,
    log_path: str | None = None,
    local_path: str | Path | None = None,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Undo one applied suggestion by restoring its backup."""
    from . import hosted as hosted_module

    if hosted_module.is_hosted():
        raise PermissionError("suggestions are disabled in the public demo")
    state = load_state(state_path)
    applied = [item for item in state.get("applied") or [] if item.get("id") == suggestion_id]
    if not applied:
        raise KeyError(f"suggestion {suggestion_id!r} is not applied")
    entry = applied[0]
    target = policy_module.local_policy_path() if local_path is None else Path(local_path)
    backup = str(entry.get("backup") or "")
    if backup:
        try:
            previous = Path(backup).read_text(encoding="utf-8")
        except OSError:
            raise KeyError(f"backup for {suggestion_id!r} is missing")
        policy_module.validate_local_document(_yaml_mapping(previous))
        target.write_text(previous, encoding="utf-8")
    else:
        try:
            target.unlink()
        except OSError:
            pass
    state["applied"] = [item for item in state.get("applied") or [] if item.get("id") != suggestion_id]
    if suggestion_id not in set(state.get("dismissed") or []):
        state.setdefault("dismissed", []).append(suggestion_id)
    _write_state(state, collect_rules(log_path, local_path), state_path)
    try:
        ecs_log.append_rule_change(
            log_path,
            suggestion_id=suggestion_id,
            type="undo",
            target=suggestion_id,
            action="undone",
        )
    except ValueError:
        pass
    return {"id": suggestion_id, "undone": True}


def _yaml_mapping(text: str) -> dict[str, Any]:
    """Parse YAML text into a mapping ({} when blank)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"backup is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("backup must hold a mapping")
    return dict(data)


def auto_redact_covers(
    policy: Any,
    kinds: list[str] | tuple[str, ...],
    site: str,
    rule_ids: list[str] | tuple[str, ...] = (),
) -> bool:
    """True when every kind is auto-covered for the site (never protected)."""
    labels = [str(item) for item in kinds or []]
    if not labels:
        return False
    if any(str(item) in PROTECTED_RULE_IDS for item in rule_ids or []):
        return False
    entries = list(getattr(policy, "auto_redact", None) or [])
    if not entries:
        return False
    wanted = ecs_log.clean_site(site)
    for label in labels:
        hit = False
        for entry in entries:
            kind = str(getattr(entry, "kind", ""))
            entry_site = str(getattr(entry, "site", "") or "")
            if kind != label:
                continue
            if entry_site in ("", wanted):
                hit = True
                break
        if not hit:
            return False
    return True
