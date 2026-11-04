"""One rule catalogue, generated from what the code really does (T62).

Each entry describes a protection that EXISTS in the code today: what it
catches, what the gate does about it, and why the data is sensitive.
Actions and ``can_send_anyway`` are derived from the live policy
(``policy.action_for`` / ``policy.keepable``), never hard-coded twice.
"""

from __future__ import annotations

from typing import Any

FOOTER = (
    "These notes explain why the data is sensitive. They are not legal "
    "advice and Shadowfenced does not make you compliant with any law."
)

NOTE_GDPR = "GDPR / UK GDPR treat this as personal data"
NOTE_CCPA = "CCPA/CPRA (California) treat it as personal information"
NOTE_PCI = "PCI DSS: card numbers must be protected wherever they are stored or sent"
NOTE_GLBA = "GLBA (US) protects customers' non-public financial information"
NOTE_HIPAA = (
    "HIPAA (US) protects health information that can be tied to a person; "
    "GDPR treats health data as a special category"
)
NOTE_REID = (
    "Name, date of birth and postcode together can identify most people, "
    "even with no single 'secret' field"
)
NOTE_KEYS = (
    "Security practice: a leaked key gives anyone the account's access; "
    "it cannot be un-sent"
)
NOTE_PERSON_SEARCH = (
    "Looking up a private person can expose them to stalking or harassment; "
    "GDPR covers processing someone else's personal data"
)


def _actions(policy: Any) -> dict[str, str]:
    """Gate-level actions derived from the live policy (no duplicated literals)."""
    block = str(policy.action_for("secret")).upper()
    redact = str(policy.action_for("pii")).upper()
    business = str(policy.action_for("business")).upper()
    review = str(policy.action_for("beacon")).upper()
    hold = "HOLD" if review == "REVIEW" else review
    return {
        "block": block, "redact": redact, "business": business,
        "hold": hold, "warn": "WARN",
    }


def _keep(policy: Any, cls: str) -> bool:
    return bool(policy.keepable(cls))


def build_catalog(policy: Any) -> list[dict[str, Any]]:
    """Build every catalogue entry from the live policy."""
    acts = _actions(policy)
    keep_secret = _keep(policy, "secret")
    keep_pii = _keep(policy, "pii")
    keep_business = _keep(policy, "business")
    keep_beacon = _keep(policy, "beacon")
    return [
        {
            "id": "canary-tokens",
            "title": "Planted canary tokens",
            "catches": "Planted canary trap tokens that prove a leak when they appear",
            "action": acts["block"],
            "can_send_anyway": keep_secret,
            "why": (
                "Canaries are fake secrets planted to catch leaks. "
                "If one appears in outgoing text, something copied data it should not have."
            ),
            "related": [NOTE_KEYS],
            "detectors": ["tier1.canary_scan"],
        },
        {
            "id": "api-keys",
            "title": "API keys and passwords",
            "catches": (
                "API keys and tokens for OpenAI, Anthropic, AWS and others, "
                "including base64-encoded ones, plus stored protected keys"
            ),
            "action": acts["block"],
            "can_send_anyway": keep_secret,
            "why": (
                "A key sent to an AI provider can be logged or reused by anyone who sees it. "
                "Blocking keeps the account's access on this computer only."
            ),
            "related": [NOTE_KEYS],
            "detectors": [
                "tier1.key_scan", "tier1.encoded_key_scan", "keyguard",
                "tier1.env_scan",
            ],
        },
        {
            "id": "personal-data",
            "title": "Personal details",
            "catches": "Names, emails, phone numbers, addresses and dates of birth",
            "action": acts["redact"],
            "can_send_anyway": keep_pii,
            "why": (
                "These details point at a real person and add up fast in a chat log. "
                "Shadowfenced replaces them with placeholders before anything is sent."
            ),
            "related": [NOTE_GDPR, NOTE_CCPA],
            "detectors": [
                "tier1.pii_scan", "tier1.name_scan", "tier1.csv_scan",
                "tier1.json_scan", "tier1.env_scan",
                "tier1.dob_scan", "tier1.address_scan", "tier1.holder_scan",
                "tier1.short_phone_scan",
            ],
        },
        {
            "id": "card-numbers",
            "title": "Payment card numbers",
            "catches": "13 to 19 digit card numbers that pass the Luhn check",
            "action": acts["redact"],
            "can_send_anyway": keep_pii,
            "why": (
                "A card number plus a name is enough to attempt payments or fraud. "
                "Shadowfenced redacts the number so it never reaches the provider."
            ),
            "related": [NOTE_PCI],
            "detectors": ["tier1.pii_scan"],
        },
        {
            "id": "bank-details",
            "title": "Bank account details",
            "catches": (
                "Bank account numbers, routing numbers, IBANs, SWIFT codes and sort codes"
            ),
            "action": acts["redact"],
            "can_send_anyway": keep_pii,
            "why": (
                "Account and routing details let money be moved or accounts be linked. "
                "Shadowfenced redacts them before anything leaves this computer."
            ),
            "related": [NOTE_GLBA],
            "detectors": [
                "tier1.account_scan", "tier1.routing_scan",
                "tier1.iban_scan", "tier1.swift_scan",
            ],
        },
        {
            "id": "sensitive-documents",
            "title": "Sensitive documents",
            "catches": (
                "Bank statements, medical records, payslips and contracts, "
                "which usually hold account, health, pay or confidential details"
            ),
            "action": acts["warn"],
            "can_send_anyway": True,
            "why": (
                "Whole documents carry far more context than one number alone. "
                "Shadowfenced warns what kind it looks like so you can check before sending."
            ),
            "related": [NOTE_HIPAA],
            "detectors": ["doc.classify"],
        },
        {
            "id": "reidentification",
            "title": "Combined identifying details",
            "catches": (
                "Small details that together can identify someone, "
                "such as name plus date of birth or postcode"
            ),
            "action": acts["warn"],
            "can_send_anyway": True,
            "why": (
                "No single detail looks secret, yet together they single out one person. "
                "Shadowfenced warns so you can generalise before sending."
            ),
            "related": [NOTE_REID],
            "detectors": ["reid"],
        },
        {
            "id": "full-identity-bundle",
            "title": "Full identity bundle",
            "catches": (
                "A name plus date of birth plus a home address or postcode "
                "plus a phone number or email address together"
            ),
            "action": "BLOCK",
            "can_send_anyway": False,
            "why": (
                "A full bundle names one real person and says where to find them. "
                "GDPR and UK GDPR treat each piece as personal data, "
                "US state privacy laws protect the same details, and together "
                "they enable identity theft, stalking and fraud. "
                "Shadowfenced blocks the original so only a generalised version can go."
            ),
            "related": [NOTE_GDPR, NOTE_CCPA, NOTE_REID],
            "detectors": ["reid"],
        },
        {
            "id": "person-search",
            "title": "Person search",
            "catches": "Messages that name a person and give details that identify them",
            "action": acts["warn"],
            "can_send_anyway": True,
            "why": (
                "This message names a person and asks about someone who may be private. "
                "Sending it shares their details with the AI provider, so you decide."
            ),
            "related": [NOTE_PERSON_SEARCH],
            "detectors": ["person_search"],
        },
        {
            "id": "business-details",
            "title": "Internal business details and custom rules",
            "catches": (
                "Internal codenames, client names, unreleased figures "
                "and your organisation's custom rules"
            ),
            "action": acts["business"],
            "can_send_anyway": keep_business,
            "why": (
                "Codenames and client names leak plans and relationships. "
                "Shadowfenced redacts them and applies your own rules the same way."
            ),
            "related": [],
            "detectors": ["tier1.business_scan", "tier1.custom_scan"],
        },
        {
            "id": "contract-clauses",
            "title": "Pasted contract clauses",
            "catches": (
                "Numbered or headed contract clauses and confidential contract "
                "text held for review"
            ),
            "action": acts["business"],
            "can_send_anyway": keep_business,
            "why": (
                "Pasted clauses carry binding terms and party names. "
                "Shadowfenced redacts clauses and holds confidential text for a human."
            ),
            "related": [],
            "detectors": ["tier1.contract_scan", "contract"],
        },
        {
            "id": "suspicious-text",
            "title": "Possible pointers to secrets",
            "catches": (
                "Sentences that point at where a secret lives and high-entropy "
                "tokens that might be secrets"
            ),
            "action": acts["hold"],
            "can_send_anyway": keep_beacon,
            "why": (
                "A pointer says where to look even when the secret itself is hidden. "
                "Shadowfenced holds these for review instead of guessing."
            ),
            "related": [],
            "detectors": ["tier1.beacon_scan", "tier1.entropy_scan"],
        },
        {
            "id": "image-content",
            "title": "Sensitive image content",
            "catches": "Faces, plates and text visible in attached images, redacted before sending",
            "action": acts["redact"],
            "can_send_anyway": True,
            "why": (
                "Photos and screenshots can show people, plates and documents at a glance. "
                "Shadowfenced blacks out sensitive regions and only keeps the redacted image."
            ),
            "related": [],
            "detectors": ["image"],
        },
        {
            "id": "audio-transcript",
            "title": "Spoken personal details in audio",
            "catches": "Transcripts of attached audio, checked like any other text",
            "action": acts["redact"],
            "can_send_anyway": True,
            "why": (
                "Dictation and recordings carry the same personal details as typed text. "
                "Shadowfenced transcribes on this computer and checks the words first."
            ),
            "related": [],
            "detectors": ["audio"],
        },
    ]


def get_catalog(policy: Any | None = None) -> list[dict[str, Any]]:
    """Catalogue entries for an explicit policy, else the default file."""
    if policy is None:
        from .policy import default_policy_path, load_policy

        policy = load_policy(default_policy_path())
    return build_catalog(policy)


# Detectors that must each have a catalogue entry. Tier 1 step names come
# from ``tier1.scan`` trace steps; the short names cover layered checks.
TIER1_STEPS = (
    "tier1.canary_scan", "tier1.key_scan", "tier1.encoded_key_scan",
    "tier1.pii_scan", "tier1.name_scan", "tier1.csv_scan",
    "tier1.json_scan", "tier1.env_scan", "tier1.dob_scan",
    "tier1.address_scan", "tier1.account_scan", "tier1.routing_scan",
    "tier1.iban_scan", "tier1.swift_scan", "tier1.holder_scan",
    "tier1.short_phone_scan", "tier1.business_scan", "tier1.custom_scan",
    "tier1.entropy_scan", "tier1.beacon_scan", "tier1.contract_scan",
)
LAYERED_DETECTORS = (
    "reid", "person_search", "intent.person_search", "contract",
    "doc.classify", "image", "audio", "keyguard",
)


def known_detectors() -> set[str]:
    """Every detector id a catalogue entry may name."""
    return set(TIER1_STEPS) | set(LAYERED_DETECTORS)


_SUBCLASS_TO_RULE = {
    "canary": "canary-tokens",
    "provider_key": "api-keys",
    "password_assignment": "api-keys",
    "protected_key": "api-keys",
    "aws_secret": "api-keys",
    "entropy": "suspicious-text",
    "beacon_cue": "suspicious-text",
    "card": "card-numbers",
    "account": "bank-details",
    "routing": "bank-details",
    "iban": "bank-details",
    "swift": "bank-details",
    "sort_code": "bank-details",
    "codename": "business-details",
    "client": "business-details",
    "figure": "business-details",
    "custom_pattern": "business-details",
    "contract_clause": "contract-clauses",
}

_CATALOGUE_ORDER = (
    "canary-tokens", "api-keys", "personal-data", "card-numbers",
    "bank-details", "sensitive-documents", "reidentification", "full-identity-bundle",
    "person-search", "business-details", "contract-clauses",
    "suspicious-text", "image-content", "audio-transcript",
)


def rule_ids_for_decision(decision: Any) -> list[str]:
    """Map a finished gate decision to catalogue ids (server-side only).

    Derived from span subclasses plus layered evidence (re-id,
    person-search, document type, confidential contract). The UI never
    guesses: it renders exactly these ids.
    """
    found: set[str] = set()
    for span in list(getattr(decision, "spans", None) or []):
        sub = str(getattr(span, "subclass", "") or "")
        cls = str(getattr(span, "cls", "") or "")
        rule = _SUBCLASS_TO_RULE.get(sub)
        if rule is None:
            if cls == "secret":
                rule = "api-keys"
            elif cls == "beacon":
                rule = "suspicious-text"
            elif cls == "business":
                rule = "business-details"
            elif cls == "pii":
                rule = "personal-data"
        if rule is not None:
            found.add(rule)
    evidence = getattr(decision, "evidence", None) or {}
    if isinstance(evidence, dict):
        reid = evidence.get("reid")
        if isinstance(reid, dict) and reid.get("outcome") in ("WARN", "HOLD", "BLOCK"):
            found.add("reidentification")
            if isinstance(reid, dict) and reid.get("outcome") == "BLOCK":
                found.add("full-identity-bundle")
        if isinstance(evidence.get("person_search"), dict):
            found.add("person-search")
        if evidence.get("doc_type") or evidence.get("doc_warning"):
            found.add("sensitive-documents")
        if evidence.get("confidential_contract"):
            found.add("contract-clauses")
    if getattr(decision, "doc_type", None) or getattr(decision, "doc_warning", None):
        found.add("sensitive-documents")
    return [rule for rule in _CATALOGUE_ORDER if rule in found]


def normalize_detector(name: str) -> str:
    """Canonical detector id for a trace step or catalogue detector name."""
    name = str(name or "").strip()
    if name == "intent.person_search":
        return "person_search"
    if name in ("reid.extract", "reid.combine", "reid.residual"):
        return "reid"
    if name == "contract.hold":
        return "contract"
    return name


def _normalise_detector(name: str) -> str:
    return normalize_detector(name)


def detector_coverage(entries: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Map each known detector to the entry ids that cover it."""
    covered: dict[str, set[str]] = {name: set() for name in known_detectors()}
    covered["person_search"] |= covered.pop("intent.person_search", set())
    for entry in entries:
        for raw in entry.get("detectors", []):
            key = _normalise_detector(raw)
            if key in covered:
                covered[key].add(str(entry.get("id", "")))
    return covered


def catalogue_ids() -> list[str]:
    """Catalogue entry ids in display order."""
    return list(_CATALOGUE_ORDER)


def rule_ids_for_kinds(kinds: list[str] | tuple[str, ...]) -> list[str]:
    """Map ``cls:subclass`` kind labels to catalogue ids.

    Same mapping as :func:`rule_ids_for_decision` (subclass table,
    then the class fallback), for callers that only kept kinds.
    """
    found: set[str] = set()
    for item in kinds or []:
        text = str(item or "").strip().lower()
        if ":" not in text:
            continue
        cls, _, sub = text.partition(":")
        rule = _SUBCLASS_TO_RULE.get(sub)
        if rule is None:
            if cls == "secret":
                rule = "api-keys"
            elif cls == "beacon":
                rule = "suspicious-text"
            elif cls == "business":
                rule = "business-details"
            elif cls == "pii":
                rule = "personal-data"
        if rule is not None:
            found.add(rule)
    return [rule for rule in _CATALOGUE_ORDER if rule in found]
