"""Re-identification risk (T30): quasi-identifiers that identify in combination.

A single detail (a first name, a birth year, a town) is often harmless, but
together details identify or locate a person: date of birth + postcode +
gender alone single out most people (Sweeney 2000). This module finds
quasi-identifiers with deterministic regexes (Tier 1 code, no model), scores
the set found in one message and across the messages of one conversation,
offers a one-click generalisation, and runs a residual-context check that
asks local Muse Glimmer -- never a cloud model -- whether the redacted text
still gives the game away.

Weights are explicit and documented in :data:`WEIGHTS`. Each distinct kind
counts once per conversation. ``WARN_THRESHOLD`` (3) marks "tell the user,
who may send anyway"; ``HOLD_THRESHOLD`` (6) marks "hold". A high-risk
combination present in the findings escalates a WARN-level score to HOLD.
Public figures acting in public roles and fictional-story details are
exempt (return OK) so they are never flagged.

False-warning guards (T30b, documented in ``docs/REID.md`` section 6):

- a single quasi-identifier kind alone never warns, however identifying
  it looks on its own;
- a birth year alone (``born 1991``) or an age alone (``34 years old``)
  never counts as a date of birth: only full calendar dates are DOB;
  year-only birth references score as age and bare years as dated
  events;
- details attached to an organisation (company suffixes Ltd/Inc/LLC/
  Clinic/Pty, ``our office at``, ``headquarters``, ``contact us``, and
  business email local parts such as info@/sales@/support@) are not
  treated as a private person's details;
- aggregate or plural phrasing (``people``, ``staff aged``, ``customers
  born``, percentages) suppresses nearby age/date-of-birth findings;
- a public-role indicator (``the actor``, ``the senator``, ``the
  singer``, ``CEO of``) removes the birthday's weight: a published
  birthday in a public role does not count as a private date of birth;
- the same span is never counted twice (a street name that also matches
  a person's name scores as street only; a birth date that also matches
  a dated event scores as date of birth only).

Second-pass guards (T30c, also documented in ``docs/REID.md``
section 6): bare pronouns (he/she/him/her/his) never score as gender,
a verb-led pair (``Tell Jake``) is never a name, public-figure markers
(``pop star``, ``(public figure)``, ``band's singer``,
``all over the press``) drop birthdays message- and
conversation-wide, business cues (shop/store/brewery/cafe/restaurant/
clinic/office/studio, ``our shop``, opening hours, ``for bookings``,
``trade enquiries``) mark nearby street/phone/email/postcode as the
business's, job-advert cues (``we are hiring``, ``apply to``,
``vacancy``, ``experience level``, jobs@/careers@/hr@/tours@/
bookings@/hello@) mark job title and town as the role's, and group
statistics (``42 patients``, age bands such as ``aged 30-39``,
``60% female``, averages, area codes) never score as one person's
detail. A singleton role plus a town (``only night-shift dispatcher
at ...``) still warns: it names a team of one.

Raw values never leave this module except inside caller-held return objects:
trace helpers emit counts, kind labels and short sha256 fingerprints only.
See ``docs/REID.md`` for the quasi-identifier list, combination rationale
and the generalisation table this module implements.

Historical and public-figure guards (T48, documented in ``docs/REID.md``
section 6c):

- a birth year more than 110 years before today carries no
  living-person weight, so the matching date of birth never counts;
  death dates ("died 1852") are never a living birthday either;
- an encyclopedic question ("summarise the life of", "biography of",
  "who was", "what did ... invent") about a name local Muse Glimmer
  confirms is a well-known public figure stays quiet, unless a current
  street address, phone, email or ID number is also present;
- Glimmer is asked over a local call with a short timeout; any failure
  falls back to the date rule alone.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

# --- Weights ----------------------------------------------------------------
# One point per unit of joining power. Contact anchors (phone, email),
# full dates of birth, street addresses and plates identify on their own;
# demographics (city, age, gender) only join in combination, so a lone one
# scores below WARN_THRESHOLD.
WEIGHTS: dict[str, int] = {
    "name": 2,
    "dob": 3,
    "age": 1,
    "street": 3,
    "postcode": 2,
    "city": 1,
    "employer": 2,
    "jobtitle": 2,
    "school": 2,
    "gender": 1,
    "medical": 2,
    "plate": 3,
    "vehicle": 2,  # distinctive vehicle description (extension of plate)
    "phone": 2,  # contact anchor (REID.md section 1)
    "email": 2,  # contact anchor (REID.md section 1)
    "dated_event": 2,
}

WARN_THRESHOLD = 3
HOLD_THRESHOLD = 6

# Minimal kind sets that escalate a WARN-level score to HOLD. The first four
# are required; the rest follow the REID.md rule of thumb (any pair with a
# name, plate, phone or email is high risk; vehicle + workplace resolves an
# owner the way plate + city does).
HIGH_RISK_COMBOS: tuple[frozenset[str], ...] = (
    frozenset({"name", "dob"}),
    frozenset({"name", "street"}),
    frozenset({"dob", "postcode", "gender"}),
    frozenset({"employer", "jobtitle", "city"}),
    frozenset({"name", "phone"}),
    frozenset({"name", "email"}),
    frozenset({"plate", "city"}),
    frozenset({"medical", "city"}),
    frozenset({"employer", "vehicle"}),
)

# Human labels for banners. Plain words only, never values.
KIND_LABELS: dict[str, str] = {
    "name": "name",
    "dob": "date of birth",
    "age": "age",
    "street": "street address",
    "postcode": "postcode",
    "city": "city",
    "employer": "employer",
    "jobtitle": "job title",
    "school": "school",
    "gender": "gender",
    "medical": "medical details",
    "plate": "vehicle plate",
    "vehicle": "vehicle",
    "phone": "phone number",
    "email": "email address",
    "dated_event": "dates",
}

OUTCOMES = ("OK", "WARN", "HOLD")

# --- Reference data ----------------------------------------------------------
CITIES = frozenset({
    "Port Meridian", "Kingsbury", "Dunmere", "Gullhaven", "Fairhaven",
    "Port Aubrun", "Harrowgate", "Greyhook Point", "Greyhook", "Brinepool",
    "Millrace", "Loomis Corner", "Loomis", "Candle Row", "Bramble",
    "Thornmere", "Hollow Isles",
})

# Words that mark a capitalised pair as a place/org, never a person name.
_PLACE_WORDS = frozenset({
    "Row", "Cottages", "Lane", "Street", "Road", "Court", "School",
    "College", "University", "Institute", "Clinic", "Office", "Harbour",
    "Harbor", "Bakery", "Kiosk", "Point", "Corner", "Isles",
})

_TITLE_WORDS = frozenset({
    "mayor", "actor", "actress", "councillor", "councilor", "captain",
    "detective", "doctor", "professor", "chef", "nurse", "officer",
    "father", "sister", "mr", "mrs", "ms", "miss", "dr",
})

_STARTERS = frozenset({
    "the", "a", "an", "in", "on", "at", "my", "our", "his", "her",
    "its", "their", "this", "that", "whoever",
})

# Message verbs that pair with a lone first name ("Tell Jake I said
# hi"): the pair is an address to that person, never a surname (T30c).
_NON_NAME_LEADERS = frozenset({
    "tell", "ask", "call", "email", "message", "text", "phone",
    "thank", "invite", "remind",
})
_ORG_SUFFIXES = (
    "clinic|office|bakery|kiosk|harbour|harbor|hospital|bank|hotel|"
    "restaurant|cafe|laboratories|labs|logistics|shop|store|depot"
)

_JOB_TITLES = (
    "pharmacist|watchmaker|operator|keeper|dispatcher|surgeon|nurse|"
    "teacher|decorator|physio|physiotherapist|clerk|captain|pilot|"
    "barista|analyst|doctor|baker|driver|mechanic|librarian|accountant|"
    "engineer|manager|cashier|attendant|technician|therapist|dentist"
)

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|"
    "October|November|December"
)
_WEEKDAYS = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"

# --- Patterns ----------------------------------------------------------------
_NAME_PAIR_RE = re.compile(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b")
_TITLE_NAME_RE = re.compile(r"\b(?:Ms|Mr|Mrs|Miss|Dr)\.\s+([A-Z][a-z]+)\b")
_REDACTED_NAME_RE = re.compile(r"\[(?:PII_NAME|NAME)(?:_\d+)?\]", re.IGNORECASE)

_DOB_FULL_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|"
    r"\d{1,2}\s+(?:" + _MONTHS + r")\s+\d{4})\b"
)
_BORN_YEAR_RE = re.compile(r"(?i)\bborn\s+(?:in\s+|on\s+)?(\d{4})\b")
_DAY_MONTH_RE = re.compile(r"\b\d{1,2}\s+(?:" + _MONTHS + r")\b")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_WEEKDAY_RE = re.compile(r"\b(?:" + _WEEKDAYS + r")'s?\b")
_LAST_SEASON_RE = re.compile(
    r"(?i)\blast\s+(?:week|month|year|spring|summer|autumn|winter|night)\b"
)

_AGE_RE = re.compile(
    r"(?i)\b\d{1,3}\s+years?\s+old\b|\baged?\s+\d{1,3}\b|"
    r"\bturned\s+\d{1,3}\b|\b(?:is|was)\s+\d{1,3}\b"
)
_COMMA_AGE_RE = re.compile(r",\s*(\d{1,3})\s*,")
_DECADE_RE = re.compile(r"(?<!\d)(\d{2})s\b")

_STREET_SUFFIX = (
    r"St|Street|Ave|Avenue|Rd|Road|Way|Lane|Ln|Dr|Drive|Blvd|Ct|Court|"
    r"Terrace|Ter|Place|Pl|Circle|Cir|Row|Cottages|Pike"
)
_STREET_RE = re.compile(
    r"\b\d{1,5}\s+[A-Z][A-Za-z0-9']*(?:\s+[A-Za-z0-9']+){0,3}\s+"
    r"(?i:" + _STREET_SUFFIX + r")\b"
)
_BARE_STREET_RE = re.compile(
    r"\b(?:on|at|of|in)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,1}\s+"
    r"(?:" + _STREET_SUFFIX + r"))\b"
)
_HOUSE_ON_RE = re.compile(
    r"\b(?:house|home|flat|apartment)\s+(?:on|at|in)\s+[A-Z][a-z]+\b",
    re.IGNORECASE,
)
_NUMBERED_PLACE_RE = re.compile(
    r"\bnumber\s+\d{1,4}\s*,\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b",
    re.IGNORECASE,
)

_POSTCODE_RE = re.compile(
    r"\b\d{5}(?:-\d{4})?\b|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b"
)

_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s().-]?)?(?:\(\d{3}\)|\d{3})[\s().-]?"
    r"\d{3}[\s().-]?\d{4}(?!\d)|\b\d{3}[-. ]\d{4}\b"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

_EMPLOYER_RE = re.compile(
    r"\b([A-Z][\w']*(?:\s+[A-Z][\w']*){0,2})\s+(?i:" + _ORG_SUFFIXES + r")\b"
)
_THE_PLACE_RE = re.compile(
    r"\bthe\s+(harbour|harbor|clinic|office|bakery|kiosk|hospital)\b",
    re.IGNORECASE,
)
_DEPT_RE = re.compile(
    r"(?i)\bin\s+(accounts|payroll|human resources|sales|marketing)\b"
)
_AT_ORG_RE = re.compile(
    r"\b(?:at|for|with|from)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b"
)
_SAINT_RE = re.compile(r"\bSt\.\s+[A-Z][a-z]+'s\b")

_SCHOOL_RE = re.compile(
    r"\b([A-Z][\w']*(?:\s+[A-Z][\w']*){0,2})\s+"
    r"(School|College|University|Institute)\b"
)
_SCHOOL_SUFFIX = frozenset({"School", "College", "University", "Institute"})

_JOB_RE = re.compile(r"\b(?:" + _JOB_TITLES + r")\b", re.IGNORECASE)
_SINGLETON_RE = re.compile(
    r"(?i)\b(?:only|lone|sole)\s+((?:[a-z]+-)?[a-z]+(?:\s+[a-z]+){0,3})\b"
)
_ROLE_WORD_RE = re.compile(
    r"(?i)\b(?:" + _JOB_TITLES + r")\b|\b\w+(?:er|or|ist|ian|eer)\b"
)

_GENDER_RE = re.compile(
    r"(?i)\b(women|woman|men|man|female|male|girls|girl|boys|boy|sons|son|"
    r"daughters|daughter|husbands|husband|wives|wife|couples|couple|guys|"
    r"guy|gals|gal|lady|ladies|gentleman|gentlemen|mr\.|mrs\.|ms\.|miss|"
    r"she/her|he/him)\b|\b(she|he|her|his|him)\b"
)

_MEDICAL_RE = re.compile(
    r"(?i)\b(allerg(?:y|ies|ic)|asthma|diabet|gout|dialysis|kidney|"
    r"migraine|pregnan|twins|expecting|surgery|surgical|huntington|"
    r"cancer|hiv|depress|anxiety|peanut|bee-sting|dementia|alzheimer|"
    r"parkinson|stroke|tumou?r|disease|syndrome|disorder|diagnos|"
    r"treatment|therapy)\b"
)

_PLATE_RE = re.compile(
    r"\b\d[A-Z]{2,3}[- ]?\d{2,4}\b|\b[A-Z]{2}\d{2}\s?[A-Z]{3}\b|"
    r"\bplate(?:\s+ending)?\s+\w+",
    re.IGNORECASE,
)
_VEHICLE_RE = re.compile(
    r"(?i)\bdrives?\s+(?:the\s+|a\s+|an\s+)?"
    r"([a-z]+\s+(?:mini|car|van|truck|bike|motorbike|suv|sedan))\b"
)

# Preposition + capitalised word(s): city heuristic. "for" is excluded so
# "ask for Liora Venn" never reads a name as a place.
_CITY_PREP_RE = re.compile(
    r"\b(?:in|at|near|of|from|to)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b"
)
_CITY_STOP = frozenset(
    "January February March April May June July August September October "
    "November December Monday Tuesday Wednesday Thursday Friday Saturday "
    "Sunday Year Apt Suite St Mr Mrs Ms Dr Mayor Captain Detective Chef "
    "Actor Councillor The My Our His Her Its Their This That Year".split()
)

# Public figure in a public role, and fictional-story framing: both exempt.
_PUBLIC_ROLE_RE = re.compile(
    r"(?i)\b(mayor|actor|actress|councillor|councilor|chef)\b.{0,60}\b"
    r"(opened|announced|stars|starred|judged|chaired|launched|unveiled|"
    r"performed|hosted)\b"
)
_STORY_RE = re.compile(
    r"(?i)\b(novel|dragon|princess|ghost ship|fairy|mystery|moon-apple|"
    r"chapter)\b"
)


# --- T48 historical / public-figure guards ----------------------------------------
# A birth year more than 110 years ago cannot locate anyone alive, so the
# matching date of birth carries no living-person weight; death dates are
# never a living birthday. Encyclopedic questions about a Glimmer-confirmed
# well-known figure stay quiet when no current contact detail (street,
# phone, email, postcode) is present.
_HISTORICAL_YEARS = 110
CONTACT_KINDS = frozenset({"street", "phone", "email", "postcode"})
_DEATH_CUE_RE = re.compile(r"(?i)\bdied\b")
_DEATH_YEAR_RE = re.compile(
    r"(?i)\bdied\b(?:\s+(?:on|in))?\s+[^\n.]{0,60}?(\d{4})")
_ENCYCLOPEDIC_RE = re.compile(
    r"(?i)\bsummari[sz]e the life of\b"
    r"|\bbiography of\b"
    r"|\bwho was\b"
    r"|\bwhat did\b[\s\S]{0,80}\binvent\b")
_VALUE_YEAR_RE = re.compile(r"\b(1[0-9]{3}|2[0-9]{3})\b")


def _value_year(value: str) -> int | None:
    """First 1000-2999 year in a finding value, or None."""
    match = _VALUE_YEAR_RE.search(value or "")
    return int(match.group(1)) if match else None


def _is_historical_year(
    year: int | None, today: datetime.date | None = None,
) -> bool:
    """True when a year is more than 110 years before today."""
    if year is None:
        return False
    ref = today or datetime.date.today()
    return ref.year - year > _HISTORICAL_YEARS


def has_encyclopedic_framing(
    text: str, conversation: list[str] | None = None,
) -> bool:
    """True when the message asks an encyclopedia-style question."""
    if text:
        combined = "\n".join([*(conversation or []), text])
    else:
        combined = "\n".join(conversation or [])
    return _ENCYCLOPEDIC_RE.search(combined) is not None


def groups_have_contact(groups: list[list[dict[str, Any]]]) -> bool:
    """True when any group holds a current contact detail."""
    return any(item.get("kind") in CONTACT_KINDS
               for group in groups for item in group)


def _groups_have_living_birth(groups: list[list[dict[str, Any]]]) -> bool:
    """True when a date of birth or born-year age remains in play."""
    for group in groups:
        for item in group:
            kind = item.get("kind")
            if kind == "dob":
                return True
            if (kind == "age"
                    and "born" in str(item.get("value", "")).lower()):
                return True
    return False


def _without_historical_dob(
    groups: list[list[dict[str, Any]]], text: str,
) -> tuple[list[list[dict[str, Any]]], bool]:
    """Drop birth/death dates that carry no living-person weight.

    Returns ``(groups, historical)``: ``historical`` is True when the
    text holds a birth year more than 110 years ago or an old death
    date. Dropped: dob findings with a historical year, dob findings
    next to a "died" cue (a death date is never a living birthday),
    "born YYYY" ages with a historical year, and age/dated-event
    fragments on a dropped span.
    """
    historical = False
    death_year = _DEATH_YEAR_RE.search(text)
    if death_year and _is_historical_year(int(death_year.group(1))):
        historical = True

    def _drop_dob(item: dict[str, Any]) -> bool:
        start, end = item["span"]
        if _nearby(text, start, end, _DEATH_CUE_RE):
            return True
        return _is_historical_year(
            _value_year(str(item.get("value", ""))))

    dropped: list[tuple[int, int]] = []
    for group in groups:
        for item in group:
            if item.get("kind") != "dob" or not _drop_dob(item):
                continue
            dropped.append((item["span"][0], item["span"][1]))
            if _is_historical_year(
                    _value_year(str(item.get("value", "")))):
                historical = True
    if any(item.get("kind") == "age"
           and "born" in str(item.get("value", "")).lower()
           and _is_historical_year(_value_year(str(item.get("value", ""))))
           for group in groups for item in group):
        historical = True

    def _kept(item: dict[str, Any]) -> bool:
        kind = item.get("kind")
        if kind == "dob":
            return not _drop_dob(item)
        if (kind == "age"
                and "born" in str(item.get("value", "")).lower()
                and _is_historical_year(
                    _value_year(str(item.get("value", ""))))):
            return False
        if kind in ("age", "dated_event"):
            start, end = item["span"]
            if any(start < other_end and end > other_start
                   for other_start, other_end in dropped):
                return False
        return True

    return ([[item for item in group if _kept(item)]
             for group in groups], historical)


# --- T30b false-warning guards ------------------------------------------------
# Company suffixes marking a name/address as an organisation's, not a
# private person's (case-sensitive: these suffixes are capitalised).
_ORG_SUFFIX_RE = re.compile(r"\b(Ltd|Inc|LLC|Clinic|Pty|Corp|GmbH|PLC)\b\.?")
# Phrases attaching nearby contact details to an organisation.
_ORG_PHRASE_RE = re.compile(
    r"(?i)\b(our office at|head office|headquarters|contact us|"
    r"corporate office)\b"
)
# Business inbox local parts: role addresses, never a person's email.
_BUSINESS_EMAIL_LOCALS = frozenset({
    "info", "sales", "support", "contact", "admin", "hello", "office",
    "help", "service", "enquiries", "inquiries", "bookings", "careers",
    "jobs", "hr", "tours",
})
# Aggregate/plural phrasing: group statements, never one person's detail.
_AGGREGATE_RE = re.compile(
    r"(?i)\b(people|staff|customers|employees|residents|clients|patients|"
    r"respondents|participants|members)\b"
)
_PERCENT_RE = re.compile(r"%|\bpercent\b", re.IGNORECASE)
# Public-role indicators: a published birthday in a public role carries
# no private weight (stronger public-figure cases are exempt outright).
_PUBLIC_ROLE_INDICATOR_RE = re.compile(
    r"(?i)\bthe\s+(actor|actress|senator|singer|pop\s?star)\b|"
    r"\b(actor|actress|senator|singer|pop\s?stars?)\b|"
    r"\bbands?(?:'s|\u2019s)?\s+singer\b|\(public figure\)|"
    r"\ball over the press\b|\bCEO\s+of\b"
)
# How far (code points) an organisation/aggregate cue may sit from a
# finding and still attach to it.
# --- T30c second-pass guards -------------------------------------------------
# Bare pronouns on their own (he/she/him/her/his) never score as gender:
# only explicit statements ("female", "gender: M", "a 34-year-old man")
# do. Extraction still records pronouns so callers can see them; scoring
# ignores them via `_is_pronoun_only`.
_PRONOUN_GENDER = frozenset({"he", "she", "him", "her", "his"})
# Explicit gender declarations such as "gender: M" / "gender: female".
_GENDER_DECL_RE = re.compile(r"(?i)\bgender\s*:\s*(male|female|m|f)\b")
# Business cues: a nearby street/phone/email/postcode belongs to the
# business, not a person. Two shapes -- article plus trade noun
# ("our shop", "the cafe") and trade noun plus locative ("brewery at
# 5 Mill Lane") -- plus opening-hours and booking/enquiry cues.
_BUSINESS_CUE_RE = re.compile(
    r"(?i)\b(our|the|this|that|a|an)\s+(shops?|stores?|brewer(?:y|ies)|"
    r"caf[ée]s?|restaurants?|clinics?|offices?|studios?)\b|"
    r"\b(shops?|stores?|brewer(?:y|ies)|caf[ée]s?|restaurants?|clinics?|"
    r"offices?|studios?)\s+(at|on|in|near)\b|"
    r"\bopening hours\b|\bopen\s+(?:mon|tue|wed|thu|fri|sat|sun|daily|"
    r"late|weekends?|nights?)\b|\bopen\s+\d|\bfor bookings\b|"
    r"\btrade enquir?ies\b"
)
# Job-advert cues: the job title and town describe an open role, not a
# person. Advert phrases plus role-based mailbox local parts.
_JOB_AD_RE = re.compile(
    r"(?i)\bwe need a\b|\bwe\s+(?:are|'re)\s+hiring\b|\bapply to\b|"
    r"\bvacanc(?:y|ies)\b|\bexperience level\b"
)
_ROLE_MAILBOX_LOCALS = frozenset({
    "jobs", "careers", "hr", "tours", "bookings", "hello",
})
# Group statistics: averages and area codes describe a group, never one
# person.
_AVERAGE_RE = re.compile(r"(?i)\baverage\b")
_AREA_CODE_RE = re.compile(r"(?i)\barea codes?\b")
# Age bands ("aged 30-39", "ages 30 to 39", en/em dashes included).
_AGE_BAND_RE = re.compile(
    r"(?i)\baged?\s+\d{1,3}\s+(?:to)\s+\d{1,3}\b|"
    r"\baged?\s+\d{1,3}\s*[\u2013\u2014-]\s*\d{1,3}\b|"
    r"\bages?\s+\d{1,3}\s*(?:to|[\u2013\u2014-])\s*\d{1,3}\b"
)


def _is_pronoun_only(item: dict[str, Any]) -> bool:
    """True when a gender finding is just a bare pronoun (T30c)."""
    return (item.get("kind") == "gender"
            and str(item.get("value", "")).lower() in _PRONOUN_GENDER)


def _is_business_attached(text: str, start: int, end: int) -> bool:
    """True when an address/phone/email belongs to a business (T30c)."""
    return _nearby(text, start, end, _BUSINESS_CUE_RE)


_GUARD_WINDOW = 60


def _nearby(text: str, start: int, end: int, pattern: re.Pattern[str]) -> bool:
    """True when ``pattern`` matches within ``_GUARD_WINDOW`` of a span."""
    window = text[max(0, start - _GUARD_WINDOW):end + _GUARD_WINDOW]
    return pattern.search(window) is not None


def _is_org_attached(text: str, start: int, end: int) -> bool:
    """True when contact details belong to an organisation, not a person."""
    return _nearby(text, start, end, _ORG_SUFFIX_RE) or _nearby(
        text, start, end, _ORG_PHRASE_RE)


def _is_aggregate_context(text: str, start: int, end: int) -> bool:
    """True when an age/birth detail describes a group, not one person."""
    return _nearby(text, start, end, _AGGREGATE_RE) or _nearby(
        text, start, end, _PERCENT_RE)


def _hit(kind: str, start: int, end: int, value: str) -> dict[str, Any]:
    """One quasi-identifier finding."""
    return {"kind": kind, "span": (start, end), "value": value}


def extract_quasi_identifiers(text: str) -> list[dict[str, Any]]:
    """Find quasi-identifiers in one message.

    Returns ``[{kind, span, value}]`` with code-point ``(start, end)``
    spans. Kinds: name, dob, age, street, postcode, city, employer,
    jobtitle, school, gender, medical, plate, vehicle, phone, email,
    dated_event. Overlapping mentions are all returned; scoring dedupes.
    """
    findings: list[dict[str, Any]] = []
    if not text:
        return findings

    for first, second in _NAME_PAIR_RE.findall(text):
        if (first.lower() in _TITLE_WORDS or first.lower() in _STARTERS
                or first.lower() in _NON_NAME_LEADERS):
            continue
        if f"{first} {second}" in CITIES:
            continue
        if second in _PLACE_WORDS:
            continue
        for match in re.finditer(
            r"\b" + re.escape(first) + r"\s+" + re.escape(second) + r"\b",
            text,
        ):
            findings.append(_hit("name", match.start(), match.end(),
                                 match.group(0)))
            break
    for match in _TITLE_NAME_RE.finditer(text):
        findings.append(_hit("name", match.start(), match.end(),
                             match.group(0)))
    for match in _REDACTED_NAME_RE.finditer(text):
        findings.append(_hit("name", match.start(), match.end(),
                             match.group(0)))

    for match in _DOB_FULL_RE.finditer(text):
        findings.append(_hit("dob", match.start(), match.end(),
                             match.group(0)))
    for match in _BORN_YEAR_RE.finditer(text):
        findings.append(_hit("dob", match.start(), match.end(),
                             match.group(0)))
    for match in _AGE_RE.finditer(text):
        findings.append(_hit("age", match.start(), match.end(),
                             match.group(0)))
    for match in _COMMA_AGE_RE.finditer(text):
        findings.append(_hit("age", match.start(), match.end(),
                             match.group(0)))
    for match in _DECADE_RE.finditer(text):
        if re.search(r"\bYear\s+\d", text[max(0, match.start() - 8):]):
            continue
        findings.append(_hit("age", match.start(), match.end(),
                             match.group(0)))

    for match in _STREET_RE.finditer(text):
        findings.append(_hit("street", match.start(), match.end(),
                             match.group(0)))
    for match in _BARE_STREET_RE.finditer(text):
        findings.append(_hit("street", match.start(1), match.end(1),
                             match.group(1)))
    for match in _HOUSE_ON_RE.finditer(text):
        findings.append(_hit("street", match.start(), match.end(),
                             match.group(0)))
    for match in _NUMBERED_PLACE_RE.finditer(text):
        findings.append(_hit("street", match.start(), match.end(),
                             match.group(0)))

    for match in _POSTCODE_RE.finditer(text):
        value = match.group(0)
        if len(value) == 4 and value.isdigit():
            continue
        findings.append(_hit("postcode", match.start(), match.end(), value))

    for city in CITIES:
        start = text.find(city)
        if start != -1:
            findings.append(_hit("city", start, start + len(city), city))
    for match in _CITY_PREP_RE.finditer(text):
        candidate = match.group(1)
        if candidate in _CITY_STOP or candidate in CITIES:
            continue
        if candidate.split()[0] in _CITY_STOP:
            continue
        findings.append(_hit("city", match.start(1), match.end(1), candidate))

    for match in _EMPLOYER_RE.finditer(text):
        org = match.group(1)
        if not org or not org[0].isupper():
            continue
        if org.split()[-1] in _PLACE_WORDS and match.group(0).split(
               )[-1].lower() in ("row", "cottages", "pike"):
            continue
        findings.append(_hit("employer", match.start(), match.end(),
                             match.group(0)))
    for match in _THE_PLACE_RE.finditer(text):
        findings.append(_hit("employer", match.start(), match.end(),
                             match.group(0)))
    for match in _DEPT_RE.finditer(text):
        findings.append(_hit("employer", match.start(), match.end(),
                             match.group(0)))
    for match in _SAINT_RE.finditer(text):
        findings.append(_hit("employer", match.start(), match.end(),
                             match.group(0)))
    for match in _AT_ORG_RE.finditer(text):
        candidate = match.group(1)
        if candidate in CITIES:
            continue
        if candidate.split()[-1] in _PLACE_WORDS:
            continue
        if candidate.split()[0].lower() in _TITLE_WORDS:
            continue
        findings.append(_hit("employer", match.start(1), match.end(1),
                             candidate))

    for match in _SCHOOL_RE.finditer(text):
        findings.append(_hit("school", match.start(), match.end(),
                             match.group(0)))

    for match in _JOB_RE.finditer(text):
        findings.append(_hit("jobtitle", match.start(), match.end(),
                             match.group(0)))
    for match in _SINGLETON_RE.finditer(text):
        phrase = match.group(1)
        role = _ROLE_WORD_RE.search(phrase)
        if role:
            # Keep only the role word itself: the trailing context
            # ("at Fairhaven", "in Gullhaven") belongs to other kinds.
            base = match.start(1)
            _singleton_item = _hit("jobtitle", base + role.start(),
                                 base + role.end(), role.group(0))
            _singleton_item["singleton"] = True
            findings.append(_singleton_item)
            # Singleton roles ("only/lone/sole X") stay flaggable (T30c).

    for match in _GENDER_DECL_RE.finditer(text):
        findings.append(_hit("gender", match.start(), match.end(),
                             match.group(0)))
    for match in _GENDER_RE.finditer(text):
        findings.append(_hit("gender", match.start(), match.end(),
                             match.group(0)))

    for match in _MEDICAL_RE.finditer(text):
        findings.append(_hit("medical", match.start(), match.end(),
                             match.group(0)))

    for match in _PLATE_RE.finditer(text):
        findings.append(_hit("plate", match.start(), match.end(),
                             match.group(0)))
    for match in _VEHICLE_RE.finditer(text):
        findings.append(_hit("vehicle", match.start(1), match.end(1),
                             match.group(1)))

    for match in _PHONE_RE.finditer(text):
        findings.append(_hit("phone", match.start(), match.end(),
                             match.group(0)))
    for match in _EMAIL_RE.finditer(text):
        findings.append(_hit("email", match.start(), match.end(),
                             match.group(0)))

    for match in _DAY_MONTH_RE.finditer(text):
        findings.append(_hit("dated_event", match.start(), match.end(),
                             match.group(0)))
    for match in _WEEKDAY_RE.finditer(text):
        findings.append(_hit("dated_event", match.start(), match.end(),
                             match.group(0)))
    for match in _LAST_SEASON_RE.finditer(text):
        findings.append(_hit("dated_event", match.start(), match.end(),
                             match.group(0)))
    for match in _YEAR_RE.finditer(text):
        findings.append(_hit("dated_event", match.start(), match.end(),
                             match.group(0)))

    _band_spans = [match.span() for match in _AGE_BAND_RE.finditer(text)]
    _has_role_box = any(
        match.group(0).split("@", 1)[0].lower() in _ROLE_MAILBOX_LOCALS
        for match in _EMAIL_RE.finditer(text))
    guarded: list[dict[str, Any]] = []
    for item in findings:
        kind = item["kind"]
        start, end = item["span"]
        if kind == "dob" and item["value"][:4].lower() == "born":
            # Year-only birth reference ("born 1991"): age, never a
            # date of birth (T30b). Full calendar dates keep kind dob.
            item = dict(item, kind="age")
            kind = "age"
        if kind == "email":
            local = item["value"].split("@", 1)[0].lower()
            if local in _BUSINESS_EMAIL_LOCALS:
                continue  # role inbox, not a person's address (T30b)
        if kind in ("phone", "email", "street", "postcode", "employer"):
            if _is_org_attached(text, start, end):
                continue  # the organisation's details, not a person's
        if kind in ("street", "phone", "email", "postcode"):
            if _is_business_attached(text, start, end):
                continue  # the business's details, not a person's (T30c)
        if kind in ("age", "dob", "dated_event", "gender"):
            if _is_aggregate_context(text, start, end):
                continue  # a group statement, not one person's detail
        if kind == "age" and any(
                start < band_end and end > band_start
                for band_start, band_end in _band_spans):
            continue  # an age band ("aged 30-39") describes a group (T30c)
        if kind in ("age", "gender", "dob", "dated_event"):
            if _nearby(text, start, end, _AVERAGE_RE):
                continue  # an average over many people, not one person (T30c)
        if kind == "postcode" and _nearby(text, start, end, _AREA_CODE_RE):
            continue  # an area code for a whole area, not one person (T30c)
        if kind in ("jobtitle", "city"):
            if _has_role_box or _nearby(text, start, end, _JOB_AD_RE):
                continue  # a job advert describes a role, not a person (T30c)
        if kind == "name":
            tight = text[max(0, start - 2):end + 10]
            if _ORG_SUFFIX_RE.search(tight):
                continue  # a company name (Acme Ltd), not a person
        guarded.append(item)
    findings = guarded
    findings.sort(key=lambda item: (item["span"][0], item["span"][1]))
    return findings


# --- Scoring ---------------------------------------------------------------
# Name ties lose to every other kind on the same span: "Copper Kettle" is a
# workplace first, "Westmere Secondary" part of a school name first.
_KIND_PRIORITY = {
    "employer": 0, "school": 1, "street": 2, "city": 3, "jobtitle": 4,
    "name": 5,
}


def _dedupe(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the highest-weight finding per overlapping cluster.

    Sorts by weight (descending), span length (descending), kind priority,
    then offset; accepts a finding only when it overlaps nothing accepted.
    """
    ordered = sorted(
        findings,
        key=lambda item: (
            -WEIGHTS.get(item["kind"], 0),
            -(item["span"][1] - item["span"][0]),
            _KIND_PRIORITY.get(item["kind"], 9),
            item["span"][0],
        ),
    )
    accepted: list[dict[str, Any]] = []
    for item in ordered:
        start, end = item["span"]
        if all(end <= other["span"][0] or start >= other["span"][1]
               for other in accepted):
            accepted.append(item)
    accepted.sort(key=lambda item: item["span"][0])
    return accepted


def _kinds_in_order(findings: list[dict[str, Any]]) -> list[str]:
    """Distinct kinds of deduped findings, first-appearance order."""
    kinds: list[str] = []
    for item in _dedupe(findings):
        if item["kind"] not in kinds:
            kinds.append(item["kind"])
    return kinds


def score_groups(
    groups: list[list[dict[str, Any]]],
) -> tuple[int, list[str], str]:
    """Score message groups (one group per message): ``(score, combo, out)``.

    Spans restart at zero in every message, so dedupe runs per group and
    the union of kinds scores. Each distinct kind counts once per
    conversation.
    """
    kinds: list[str] = []
    _scored = [[item for item in group if not _is_pronoun_only(item)]
               for group in groups]
    for group in _scored:
        for kind in _kinds_in_order(group):
            if kind not in kinds:
                kinds.append(kind)
    score = sum(WEIGHTS.get(kind, 0) for kind in kinds)
    present = set(kinds)
    if len(kinds) <= 1:
        # One quasi-identifier alone never warns (T30b), however
        # identifying it looks on its own.
        return score, kinds, "OK"
    _singleton_role = any(
        item.get("kind") == "jobtitle" and item.get("singleton")
        for group in _scored for item in group)
    if present == {"jobtitle", "city"} and not _singleton_role:
        # A job title plus a town, with no name or employer, does not
        # identify anyone on its own (T30b); a singleton role ("only ...")
        # plus a town still warns: it names a team of one (T30c).
        return score, kinds, "OK"
    combo_hit = any(combo <= present for combo in HIGH_RISK_COMBOS)
    if score >= HOLD_THRESHOLD or (combo_hit and score >= WARN_THRESHOLD):
        outcome = "HOLD"
    elif score >= WARN_THRESHOLD:
        outcome = "WARN"
    else:
        outcome = "OK"
    return score, kinds, outcome


def score_message(
    findings: list[dict[str, Any]],
) -> tuple[int, list[str], str]:
    """Score one message's findings: ``(score, combination, outcome)``.

    Each distinct kind counts once. ``outcome`` is HOLD at or above
    ``HOLD_THRESHOLD``, WARN at or above ``WARN_THRESHOLD``, else OK. A
    high-risk combination present in the findings escalates a WARN-level
    score to HOLD.
    """
    return score_groups([findings])


def banner_for(combination: list[str], outcome: str) -> str:
    """Plain-words banner listing the combining kinds, never values."""
    labels = [KIND_LABELS.get(kind, kind) for kind in combination]
    if not labels:
        return ""
    if len(labels) == 1:
        listing = labels[0]
    elif len(labels) == 2:
        listing = f"{labels[0]} and {labels[1]}"
    else:
        listing = ", ".join(labels[:-1]) + f" and {labels[-1]}"
    if outcome == "HOLD":
        return (f"Together, your {listing} can identify and locate you.")
    return (f"Together, your {listing} could help identify and locate you.")


@dataclass
class ReIDResult:
    """Assessment of one message in conversation context."""

    outcome: str  # OK | WARN | HOLD
    score: int = 0
    combination: list[str] = field(default_factory=list)
    banner: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    generalised_text: str = ""
    generalise_mapping: dict[str, str] = field(default_factory=dict)
    exempted: bool = False
    exempt_reason: str = ""


def _exemption(text: str) -> str:
    """Return the exemption reason, or '' when the text is in scope."""
    if _STORY_RE.search(text):
        return "fictional-story framing"
    if _PUBLIC_ROLE_RE.search(text):
        return "public figure in a public role"
    return ""


def assess(
    text: str,
    conversation: list[str] | None = None,
    current_findings: list[dict[str, Any]] | None = None,
    generalise_offer: bool = True,
    public_figure_judge_fn: Callable[[str], Any] | None = None,
) -> ReIDResult:
    """Assess one message together with earlier conversation messages.

    Findings accumulate across ``conversation`` plus ``text`` (each kind
    counts once); generalisation covers the current message only.
    """
    prior = list(conversation or [])
    current = (current_findings if current_findings is not None
               else extract_quasi_identifiers(text))
    groups = [current]
    for earlier in prior:
        groups.append(extract_quasi_identifiers(earlier))
    exempt_reason = _exemption("\n".join([*prior, text]))
    if exempt_reason:
        return ReIDResult(outcome="OK", findings=list(current),
                          exempted=True, exempt_reason=exempt_reason)
    if _PUBLIC_ROLE_INDICATOR_RE.search("\n".join([*prior, text])):
        # A published birthday in a public role carries no private
        # weight (T30b): drop the birthday and any age/dated fragment
        # on the same span (day-month or year also matched as dated
        # events) before scoring.
        spans = [tuple(item["span"]) for group in groups
                 for item in group if item["kind"] == "dob"]

        def _kept(item: dict[str, Any]) -> bool:
            if item["kind"] == "dob":
                return False
            if item["kind"] in ("age", "dated_event"):
                start, end = item["span"]
                if any(start < other_end and end > other_start
                       for other_start, other_end in spans):
                    return False
            if item["kind"] == "age" and "born" in item["value"].lower():
                return False
            return True

        groups = [[item for item in group if _kept(item)]
                  for group in groups]
    combined_text = "\n".join([*prior, text])
    groups, historical = _without_historical_dob(groups, combined_text)
    if (historical and not groups_have_contact(groups)
            and not _groups_have_living_birth(groups)):
        # A birth/death date more than a century old cannot locate
        # anyone alive (T48 rule 1): with no current contact detail
        # and no living birthday left, nothing here identifies a
        # living person.
        score, combination, _ = score_groups(groups)
        return ReIDResult(
            outcome="OK", score=score, combination=combination,
            banner="", findings=list(current),
        )
    if (has_encyclopedic_framing(text, prior)
            and any(item.get("kind") == "name"
                    for group in groups for item in group)
            and not groups_have_contact(groups)):
        # An encyclopedic question about a Glimmer-confirmed
        # well-known figure stays quiet (T48 rule 2); any Glimmer
        # failure falls back to the code-only rules above and below.
        try:
            raw = (public_figure_judge_fn(combined_text)
                   if public_figure_judge_fn is not None
                   else local_glimmer_public_figure(combined_text))
        except Exception:
            raw = None
        if _parse_public_figure(raw) is True:
            lowered = [[item for item in group
                        if item.get("kind") != "name"]
                       for group in groups]
            score, combination, _ = score_groups(lowered)
            return ReIDResult(
                outcome="OK", score=score, combination=combination,
                banner="", findings=list(current),
                exempted=True,
                exempt_reason=(
                    "well-known public figure in encyclopedic framing"),
            )
    score, combination, outcome = score_groups(groups)
    banner = banner_for(combination, outcome) if outcome != "OK" else ""
    generalised_text = ""
    mapping: dict[str, str] = {}
    if outcome != "OK" and generalise_offer:
        generalised_text, mapping = generalise(text, current)
    return ReIDResult(
        outcome=outcome, score=score, combination=combination,
        banner=banner, findings=list(current),
        generalised_text=generalised_text, generalise_mapping=mapping,
    )


class ConversationAccumulator:
    """Union findings across the messages of one request/UI conversation."""

    def __init__(self) -> None:
        self._messages: list[str] = []
        self._groups: list[list[dict[str, Any]]] = []

    def add_message(self, text: str) -> list[dict[str, Any]]:
        """Add one message; return its own findings."""
        current = extract_quasi_identifiers(text)
        self._messages.append(text)
        self._groups.append(current)
        return current

    @property
    def messages(self) -> list[str]:
        """Messages added so far (oldest first)."""
        return list(self._messages)

    def reset(self) -> None:
        """Forget the conversation (new request or UI thread)."""
        self._messages.clear()
        self._groups.clear()

    def score(self) -> tuple[int, list[str], str]:
        """Score the accumulated conversation."""
        return score_groups(self._groups)


# --- Generalisation (REID.md section 4) ---------------------------------------
_JOB_FAMILY = (
    ("surgeon", "medical staff"), ("pharmacist", "pharmacy staff"),
    ("nurse", "nursing staff"), ("teacher", "teaching staff"),
    ("watchmaker", "a craft worker"), ("operator", "a machine operator"),
    ("keeper", "facilities staff"), ("dispatcher", "logistics staff"),
    ("decorator", "design staff"), ("physio", "a health worker"),
    ("clerk", "office staff"), ("captain", "transport staff"),
    ("pilot", "transport staff"), ("barista", "food-service staff"),
    ("analyst", "an analyst"), ("doctor", "medical staff"),
    ("baker", "food-service staff"), ("driver", "a driver"),
)

_EMPLOYER_SECTOR = (
    ("clinic", "a health clinic"), ("hospital", "a hospital"),
    ("school", "a school"), ("bakery", "a bakery"),
    ("harbour", "a harbour business"), ("harbor", "a harbour business"),
    ("office", "an office"), ("bank", "a bank"), ("hotel", "a hotel"),
    ("labs", "a company"), ("logistics", "a logistics firm"),
)

_MEDICAL_CATEGORY = (
    ("allerg", "an allergic condition"), ("asthma", "a breathing condition"),
    ("diabet", "a chronic condition"), ("gout", "a metabolic condition"),
    ("dialysis", "ongoing treatment"), ("kidney", "ongoing treatment"),
    ("migrain", "a neurological condition"), ("huntington",
     "a neurological condition"), ("pregnan", "a health matter"),
    ("twins", "a health matter"), ("expecting", "a health matter"),
    ("surgery", "a medical procedure"), ("surgical", "a medical procedure"),
    ("cancer", "a serious illness"), ("hiv", "a chronic condition"),
    ("depress", "a health matter"), ("anxiety", "a health matter"),
)

_GENDER_NEUTRAL = {
    "woman": "person", "women": "person", "man": "person", "men": "person",
    "lady": "person", "ladies": "person", "gentleman": "person",
    "gentlemen": "person", "guy": "person", "guys": "people", "gal": "person",
    "gals": "people", "girl": "person", "boy": "person", "female": "person",
    "male": "person", "son": "child", "daughter": "child",
    "husband": "spouse", "wife": "spouse", "couple": "household",
}


def _generalised_value(kind: str, value: str, city: str | None) -> str:
    """Coarsen one raw value per the REID.md generalisation table."""
    low = value.lower()
    if kind == "name":
        parts = re.findall(r"[A-Za-z]+", value)
        if len(parts) >= 2:
            return ".".join(part[0].upper() for part in parts[:2]) + "."
        return "the person"
    if kind == "dob":
        year = re.search(r"\d{4}", value)
        return f"born in {year.group(0)}" if year else "birth year"
    if kind == "age":
        number = re.search(r"\d+", value)
        if number is None:
            return value
        if "born" in low:
            birth_year = re.search(r"\d{4}", value)
            if birth_year:
                return f"born in {birth_year.group(0)}"
        age = int(number.group(0))
        if age < 13:
            return "child"
        return f"{age // 10 * 10}s"
    if kind == "street":
        return city if city else "the area"
    if kind == "postcode":
        return value[:3] + "*" * max(0, len(value) - 3)
    if kind == "city":
        return "the area"
    if kind == "employer":
        for keyword, sector in _EMPLOYER_SECTOR:
            if keyword in low:
                return sector
        return "a local employer"
    if kind == "jobtitle":
        for keyword, family in _JOB_FAMILY:
            if keyword in low:
                return family
        return "staff"
    if kind == "school":
        return "a school"
    if kind == "gender":
        key = low.strip().rstrip(".")
        if key in _GENDER_NEUTRAL:
            return _GENDER_NEUTRAL[key]
        pronoun = {"she": "they", "he": "they", "her": "them",
                   "him": "them", "his": "their", "hers": "their"}.get(key)
        return pronoun if pronoun else "person"
    if kind == "medical":
        for keyword, category in _MEDICAL_CATEGORY:
            if keyword in low:
                return category
        return "a health condition"
    if kind == "plate":
        return "a private vehicle"
    if kind == "vehicle":
        return "a vehicle"
    if kind in ("phone", "email"):
        return "[contact removed]"
    if kind == "dated_event":
        year = re.search(r"\d{4}", value)
        if year:
            return year.group(0)
        if _WEEKDAY_RE.search(value):
            return "that week"
        return "recently"
    return "[redacted]"


def _absorb_affixes(
    text: str, start: int, end: int, kind: str, replacement: str
) -> tuple[int, int]:
    """Fold neighbouring words into an edit so the output reads naturally.

    Swallows a preceding "born" when the replacement already says
    "born in ...", a preceding article when the replacement brings its
    own ("the" + "a health clinic"), and a following ", City" when a
    street generalises to that same city ("of X Lane, Dunmere").
    """
    low = replacement.lower()
    if kind == "dob" or (kind == "age" and low.startswith("born in ")):
        if (text[max(0, start - 5):start].lower() == "born "
                and not text[start:end].lower().startswith("born")):
            start -= 5
    if low.split(" ", 1)[0] in ("a", "an", "the"):
        article = re.search(r"(?i)\b(a|an|the)\s+$", text[:start])
        if article:
            start = article.start()
    if kind == "street":
        following = re.match(r",\s*" + re.escape(replacement.strip()) + r"\b",
                             text[end:])
        if following and re.fullmatch(
                r"[A-Z][A-Za-z'’.\-]*(?: [A-Z][A-Za-z'’.\-]*)*",
                replacement.strip()):
            end += following.end()
    return start, end


def generalise(
    text: str, findings: list[dict[str, Any]]
) -> tuple[str, dict[str, str]]:
    """Rewrite risky details to coarser values.

    Returns ``(new_text, mapping)`` where mapping is
    ``{label: raw_value}`` with labels like ``[REID_DOB_1]``. The mapping
    holds raw values and must stay local: never log it, never put it in a
    trace or API evidence. The generalised text holds coarsened values only.
    """
    findings = _dedupe(findings)
    cities = [item["value"] for item in findings if item["kind"] == "city"]
    city = cities[0] if cities else None
    counters: dict[str, int] = {}
    mapping: dict[str, str] = {}
    edits: list[tuple[int, int, str]] = []
    for item in findings:
        start, end = item["span"]
        if start < 0 or end > len(text) or start >= end:
            continue
        if edits and start < edits[-1][1]:
            continue
        kind = item["kind"]
        counters[kind] = counters.get(kind, 0) + 1
        if kind == "name" and _REDACTED_NAME_RE.fullmatch(text[start:end]):
            label = f"[REID_{kind.upper()}_{counters[kind]}]"
            mapping[label] = text[start:end]
            edits.append((start, end, text[start:end]))
            continue
        label = f"[REID_{kind.upper()}_{counters[kind]}]"
        mapping[label] = text[start:end]
        replacement = _generalised_value(kind, text[start:end],
                                                     city)
        start, end = _absorb_affixes(text, start, end, kind, replacement)
        edits.append((start, end, replacement))
    out = text
    for start, end, replacement in reversed(edits):
        out = out[:start] + replacement + out[end:]
    out = re.sub(r"(?i)\bthe\s+the\b", "the", out)
    out = re.sub(r"(?i)\ba\s+a\b", "a", out)
    out = re.sub(r"(?i)\ban\s+an\b", "an", out)
    return out, mapping


# --- T48 public-figure Glimmer judgement ------------------------------------------
PUBLIC_FIGURE_SYSTEM = (
    "Decide whether the user text asks about a well-known public or "
    "historical figure (an encyclopedia-style question about a famous "
    "person). Reply with JSON only: {\"public_figure\": bool}."
)


def local_glimmer_public_figure(
    text: str,
    timeout_s: float = 2.0,
    transport: Any = None,
) -> bool | None:
    """Ask local Muse Glimmer whether the text is about a public figure.

    Local Ollama only (``/api/chat`` on the configured Ollama URL with the
    configured judge model). Never touches a cloud model or the Meta
    upstream client. The timeout is deliberately short; Tier 2 ``off``
    and any transport error, timeout or unparsable answer return None
    so the caller falls back to the code-only rules.
    """
    try:
        from .judge import tier2_mode
        if tier2_mode() == "off":
            return None
    except Exception:
        pass
    try:
        import httpx

        from .config import get_settings

        settings = get_settings()
        url = settings.ollama_url.rstrip("/") + "/api/chat"
        with httpx.Client(transport=transport, timeout=timeout_s) as client:
            response = client.post(
                url,
                json={
                    "model": settings.judge_model,
                    "messages": [
                        {"role": "system", "content": PUBLIC_FIGURE_SYSTEM},
                        {"role": "user", "content": text[:2000]},
                    ],
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0, "seed": 7,
                                "num_ctx": 2048, "num_predict": 20},
                },
            )
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        message = payload.get("message", {})
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            return None
        cleaned = content.strip()
        import json as _json

        try:
            parsed = _json.loads(cleaned)
        except ValueError:
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start == -1 or end <= start:
                return None
            try:
                parsed = _json.loads(cleaned[start:end + 1])
            except ValueError:
                return None
        if isinstance(parsed, dict):
            verdict = parsed.get("public_figure",
                                 parsed.get("subject_public"))
            if isinstance(verdict, bool):
                return verdict
        return None
    except Exception:
        return None


def _parse_public_figure(raw: Any) -> bool | None:
    """Normalise an injected public-figure verdict; None when unusable."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, dict):
        for key in ("public_figure", "subject_public", "is_public"):
            if isinstance(raw.get(key), bool):
                return raw[key]
    return None


# --- Residual-context check (inference reversal) --------------------------------
@dataclass
class ResidualResult:
    """Outcome of the residual-context check on redacted text."""

    hold: bool
    verified_clues: list[str] = field(default_factory=list)
    can_identify: bool = False
    can_locate: bool = False
    reason: str = ""


def _parse_judge_answer(raw: Any) -> dict[str, Any] | None:
    """Normalise a judge answer to a dict; None when unusable."""
    if raw is None:
        return None
    if hasattr(raw, "model_dump"):
        try:
            raw = raw.model_dump()
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    try:
        return {
            "can_identify": bool(raw.get("can_identify", False)),
            "can_locate": bool(raw.get("can_locate", False)),
            "clues": list(raw.get("clues", []) or []),
        }
    except (TypeError, ValueError):
        return None


def local_glimmer_judge(
    redacted_text: str,
    timeout_s: float = 10.0,
) -> dict[str, Any] | None:
    """Ask local Muse Glimmer about redacted text; None on any failure.

    Local Ollama only (``/api/chat`` on the configured Ollama URL with the
    configured judge model). Never touches a cloud model or the Meta
    upstream client. Any transport error, timeout or unparsable answer
    returns None so the caller fails closed.
    """
    try:
        import httpx

        from .config import get_settings

        settings = get_settings()
        url = settings.ollama_url.rstrip("/") + "/api/chat"
        prompt = (
            "You are an OSINT analyst. Decide whether the redacted text "
            "below still identifies or locates a specific private person. "
            "Reply with JSON only: "
            '{"can_identify": bool, "can_locate": bool, '
            '"clues": ["<exact substring of the text>", ...]}. '
            "Each clue must be copied word-for-word from the text.\n\n"
            + redacted_text[:2000]
        )
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(
                url,
                json={
                    "model": settings.judge_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "think": False,
                },
            )
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        message = payload.get("message", {})
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            return None
        text = content.strip()
        try:
            return json.loads(text)
        except ValueError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                return None
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                return None
    except Exception:
        return None


def residual_check(
    redacted_text: str,
    judge_fn: Callable[[str], Any] | None = None,
) -> ResidualResult:
    """Check whether redacted text still identifies or locates someone.

    ``judge_fn`` takes the redacted text and returns
    ``{can_identify, can_locate, clues}``; the default asks local Muse
    Glimmer. Only clues that are exact substrings of the redacted text
    count. Two or more verified clues with ``can_identify`` or
    ``can_locate`` hold; fewer than two quasi-identifiers in the text is
    OK without calling the judge; a judge error, timeout or unusable
    answer holds (fail closed).
    """
    remaining = [item for item in extract_quasi_identifiers(redacted_text)
                 if not _is_pronoun_only(item)]
    if len(remaining) < 2:
        return ResidualResult(
            hold=False,
            reason="fewer than 2 quasi-identifiers remain",
        )
    judge = judge_fn if judge_fn is not None else local_glimmer_judge
    try:
        raw = judge(redacted_text)
    except Exception:
        raw = None
    parsed = _parse_judge_answer(raw)
    if parsed is None:
        return ResidualResult(
            hold=True,
            reason="judge unavailable or unusable -> HOLD (fail closed)",
        )
    verified = [clue for clue in parsed["clues"]
                if isinstance(clue, str) and clue and clue in redacted_text]
    if (parsed["can_identify"] or parsed["can_locate"]) and len(verified) >= 2:
        return ResidualResult(
            hold=True, verified_clues=verified,
            can_identify=parsed["can_identify"],
            can_locate=parsed["can_locate"],
            reason="2+ verified residual clues still identify or locate",
        )
    return ResidualResult(
        hold=False, verified_clues=verified,
        can_identify=parsed["can_identify"],
        can_locate=parsed["can_locate"],
        reason="no identifying residual combination verified",
    )


# --- Trace helpers (labels and fingerprints, never raw values) ------------------
def fingerprint(value: str) -> str:
    """Short sha256 fingerprint of one raw value for join detection."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def trace_extract(
    trace: list[dict[str, Any]] | None,
    findings: list[dict[str, Any]],
    step: str = "reid.extract",
) -> None:
    """Append a ``reid.extract`` step: counts, kinds and fingerprints."""
    if trace is None:
        return
    kinds = sorted({item["kind"] for item in findings})
    marks = sorted({f"{item['kind']}~{fingerprint(item['value'])}"
                    for item in findings})
    detail = f"{len(findings)} finding(s): " + ", ".join(kinds) if kinds \
        else "0 findings"
    if marks:
        detail += " [" + ", ".join(marks) + "]"
    trace.append({
        "step": step, "tool": "reid", "detail": detail,
        "result": "hit" if findings else "clean", "ms": 0.0,
    })


def trace_combine(
    trace: list[dict[str, Any]] | None,
    score: int,
    combination: list[str],
    outcome: str,
    step: str = "reid.combine",
) -> None:
    """Append a ``reid.combine`` step: score, kinds and outcome."""
    if trace is None:
        return
    labels = [KIND_LABELS.get(kind, kind) for kind in combination]
    trace.append({
        "step": step, "tool": "reid",
        "detail": f"score={score} ({'+'.join(labels) or 'none'})",
        "result": outcome, "ms": 0.0,
    })


def trace_residual(
    trace: list[dict[str, Any]] | None,
    result: ResidualResult,
    step: str = "reid.residual",
) -> None:
    """Append a ``reid.residual`` step: verified-clue fingerprints only."""
    if trace is None:
        return
    marks = sorted({fingerprint(clue) for clue in result.verified_clues})
    detail = (f"{len(result.verified_clues)} verified clue(s)")
    if marks:
        detail += " [" + ", ".join(marks) + "]"
    trace.append({
        "step": step, "tool": "reid", "detail": detail,
        "result": "HOLD" if result.hold else "OK", "ms": 0.0,
    })
