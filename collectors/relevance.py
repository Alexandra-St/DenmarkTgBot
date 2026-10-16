"""Offline, deterministic relevance screening for normalized publications."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Mapping

RULESET_VERSION = "1.1"
DIRECT = "DIRECT"
POTENTIAL = "POTENTIAL"
IRRELEVANT = "IRRELEVANT"


@dataclass(frozen=True)
class RelevanceResult:
    source: str | None
    external_id: str | None
    relevance: str
    reason: str
    matched_rules: tuple[str, ...]
    ruleset_version: str
    evidence_fields: tuple[str, ...]
    diagnostics: tuple[object, ...]
    publication: dict[str, object]
    historical: object | None = None
    notify_eligible: object | None = None
    notification_blocked: bool = False

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["matched_rules"] = list(self.matched_rules)
        result["evidence_fields"] = list(self.evidence_fields)
        result["diagnostics"] = list(self.diagnostics)
        return result


def _as_mapping(publication: object) -> dict[str, object]:
    if isinstance(publication, Mapping):
        return dict(publication)
    to_dict = getattr(publication, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if isinstance(value, Mapping):
            return dict(value)
    try:
        value = asdict(publication)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def normalize_text(value: str) -> str:
    """Casefold and preserve Danish letters while turning punctuation into boundaries."""
    source = unicodedata.normalize("NFC", value).casefold()
    chars = [character if character.isalnum() else " " for character in source]
    return " ".join("".join(chars).split())


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


_DIRECT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("direct.ukraine_special_law", _compile(r"\bukraine\s*særlov(?:en|ens|e|ene|enes)?\b")),
    ("direct.ukraine_special_law", _compile(r"\bukraine\s*særlovgivning(?:en|ens)?\b")),
    ("direct.ukraine_special_law", _compile(r"\bsærloven\s+for\s+(?:de\s+)?fordrevne\s+fra\s+ukraine\b")),
    ("direct.displaced_from_ukraine", _compile(r"\bfordrevne\s+fra\s+ukraine\b")),
    ("direct.displaced_from_ukraine", _compile(r"\bfordrevne\s+personer\s+fra\s+ukraine\b")),
    ("direct.displaced_from_ukraine", _compile(r"\bperson(?:er|en)?\s+(?:der\s+er\s+)?fordrevet\s+fra\s+ukraine\b")),
    ("direct.displaced_from_ukraine", _compile(r"\bukrainske\s+fordrevne\b")),
    ("direct.displaced_from_ukraine", _compile(r"\bfordrevne\s+ukrainere?\b")),
)
_UKRAINIAN = _compile(r"\bukrain\w*\b")
_TEMP_PROTECTION = _compile(r"\bmidlertidig\s+beskyttelse\b")

_POPULATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    _compile(r"\budlænding\w*\b"),
    _compile(r"\b(?:flygtning|flygtninge|flygtningens|flygtningenes)\b"),
    _compile(r"\bfordrevne?\b"),
    _compile(r"\basylansøger\w*\b"),
    _compile(r"\btredjelandsborger\w*\b"),
    _compile(r"\bpersoner\s+med\s+opholdstilladelse\b"),
    _compile(r"\budenlandske\s+borgere\b"),
    _compile(r"\bikke\s*danske\s+statsborgere\b"),
    _compile(r"\bpersoner\s+med\s+international\s+beskyttelse\b"),
)

_TOPICS: dict[str, tuple[re.Pattern[str], ...]] = {
    "residence": (
        _compile(r"\bopholdstillad\w*\b"), _compile(r"\bopholdsgrundlag\w*\b"),
        _compile(r"\bforlæng\w*\s+af\s+ophold\w*\b"), _compile(r"\budlændingelov\w*\b"),
        _compile(r"\bopholdsret\w*\b"),
    ),
    "work": (
        _compile(r"\barbejdstillad\w*\b"), _compile(r"\bbeskæftigelse\s+af\s+udlænding\w*\b"),
        _compile(r"\budenlandsk\w*\s+arbejdskraft\b"),
    ),
    "social_support": (
        _compile(r"\boffentlige\s+ydelser\b"), _compile(r"\bkommunale\s+ydelser\b"),
        _compile(r"\bintegrationsydelse\w*\b"), _compile(r"\bselvforsørgelses\w*\b"),
        _compile(r"\bsociale\s+ydelser\b"),
    ),
    "integration": (
        _compile(r"\bintegrationslov\w*\b"), _compile(r"\bintegrationsprogram\w*\b"),
        _compile(r"\bintegrationsindsats\w*\b"),
    ),
    "housing": (
        _compile(r"\bboligplacering\w*\b"), _compile(r"\bindkvartering\w*\b"),
        _compile(r"\bbolig(?:forhold|rettighed|anvisning)\w*\b"), _compile(r"\bboligplacering\w*\b"),
    ),
    "family": (_compile(r"\bfamiliesammenføring\w*\b"), _compile(r"\bfamilieophold\w*\b")),
    "education": (
        _compile(r"\badgang\s+til\s+uddannelse\b"), _compile(r"\buddannelsesadgang\w*\b"),
        _compile(r"\bskolegang\w*\b"), _compile(r"\buddannelsesret\w*\b"),
    ),
}

_NEGATIVE_RULES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    ("negative.ukraine_foreign_policy_or_aid", (
        _compile(r"\bmilitær\w*\b"), _compile(r"\bforsvar\w*\b"), _compile(r"\bvåben\w*\b"),
        _compile(r"\budenrigspolitik\w*\b"), _compile(r"\bdiplomat\w*\b"),
        _compile(r"\beksport\w*\b"), _compile(r"\bhandel\w*\b"), _compile(r"\blandbrug\w*\b"),
    )),
    ("negative.general_labor_market", (_compile(r"\barbejdsmarked\w*\b"), _compile(r"\blønforhold\w*\b"))),
    ("negative.general_housing_construction", (_compile(r"\bboligbyggeri\w*\b"), _compile(r"\bnybyggeri\w*\b"))),
    ("negative.general_education_policy", (_compile(r"\buddannelsespolitik\w*\b"), _compile(r"\buniversitetsreform\w*\b"))),
)

_POTENTIAL_REASONS = {
    "potential.residence_immigration_law": "Immigration or residence rules may be relevant to the target population.",
    "potential.work_foreign_population": "Work authorization rules concern foreign workers or residents.",
    "potential.social_support_targeted": "A social support measure is linked to a potentially affected population.",
    "potential.integration_targeted": "An integration measure is linked to a potentially affected population.",
    "potential.housing_displaced_population": "Housing or accommodation provisions concern displaced people or foreigners.",
    "potential.family_reunification": "Family reunification or family residence rules may be relevant.",
    "potential.education_displaced_population": "Education access provisions concern displaced people or foreigners.",
    "potential.temporary_protection_general": "Temporary protection rules may concern displaced people even without a Ukraine-specific reference.",
}


def _publication_texts(record: Mapping[str, object]) -> list[tuple[str, str, str]]:
    texts: list[tuple[str, str, str]] = []

    def add(field: str, value: object) -> None:
        if isinstance(value, str) and normalize_text(value):
            texts.append((field, value, normalize_text(value)))

    add("title", record.get("title"))
    add("content_text", record.get("content_text"))
    for field, collection in (("stages", record.get("stages")), ("documents", record.get("documents"))):
        if isinstance(collection, (list, tuple)):
            for item in collection:
                mapped = _as_mapping(item)
                add(f"{field}.title", mapped.get("title"))
    return texts


def _has(patterns: tuple[re.Pattern[str], ...] | list[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _linked_ukraine_protection(text: str) -> bool:
    ukraine_spans = [match.span() for match in _UKRAINIAN.finditer(text)]
    protection_spans = [match.span() for match in _TEMP_PROTECTION.finditer(text)]
    return any(
        len(text[min(left[1], right[1]):max(left[0], right[0])].split()) <= 4
        for left in ukraine_spans
        for right in protection_spans
    )


def _matched_direct(text: str) -> list[str]:
    matches = [rule_id for rule_id, pattern in _DIRECT_RULES if pattern.search(text)]
    if _linked_ukraine_protection(text):
        matches.append("direct.ukraine_temporary_protection")
    return matches


def _potential_matches(text: str) -> list[str]:
    population = _has(_POPULATION_PATTERNS, text)
    matched: list[str] = []
    for topic, patterns in _TOPICS.items():
        if not _has(patterns, text):
            continue
        if topic == "residence":
            # These legal domains are specifically immigration/residence
            # measures even when a short title omits the affected population.
            matched.append("potential.residence_immigration_law")
        elif topic == "work" and population:
            matched.append("potential.work_foreign_population")
        elif topic == "social_support" and (population or _has((_compile(r"\bintegrationsydelse\w*\b"),), text)):
            matched.append("potential.social_support_targeted")
        elif topic == "integration" and population:
            matched.append("potential.integration_targeted")
        elif topic == "housing" and population:
            matched.append("potential.housing_displaced_population")
        elif topic == "family":
            matched.append("potential.family_reunification")
        elif topic == "education" and population:
            matched.append("potential.education_displaced_population")
    if _TEMP_PROTECTION.search(text) and population:
        matched.append("potential.temporary_protection_general")
    return matched


def classify(publication: object) -> RelevanceResult:
    """Classify a Høringsportalen consultation or normalized Folketinget case."""
    record = _as_mapping(publication)
    texts = _publication_texts(record)
    primary = [item for item in texts if item[0] in {"title", "content_text", "stages.title"}]
    documents = [item for item in texts if item[0] == "documents.title"]
    primary_direct = [(field, rule) for field, _, text in primary for rule in _matched_direct(text)]
    document_direct = [(field, rule) for field, _, text in documents for rule in _matched_direct(text)]

    negative_rules: list[str] = []
    title_context = normalize_text(record.get("title")) if isinstance(record.get("title"), str) else ""
    primary_context = " ".join(text for _, _, text in primary)
    if _UKRAINIAN.search(primary_context) and _has(_NEGATIVE_RULES[0][1], primary_context):
        negative_rules.append(_NEGATIVE_RULES[0][0])
    for rule_id, patterns in _NEGATIVE_RULES[1:]:
        if _has(patterns, primary_context):
            negative_rules.append(rule_id)

    has_population = _has(_POPULATION_PATTERNS, title_context)
    clear_unrelated_title = _has((
        _compile(r"\bvejlov\w*\b"), _compile(r"\btrafik\w*\b"), _compile(r"\bjernbane\w*\b"),
        _compile(r"\btogdrift\w*\b"), _compile(r"\bboligbyggeri\w*\b"), _compile(r"\buddannelsespolitik\w*\b"),
    ), title_context)
    unrelated_primary = bool(
        (_UKRAINIAN.search(title_context) and _has(_NEGATIVE_RULES[0][1], title_context))
        or (clear_unrelated_title and not has_population)
    )
    direct_matches = primary_direct[:]
    suppressed_document_direct = bool(document_direct and not direct_matches and unrelated_primary)
    if document_direct and not unrelated_primary:
        direct_matches.extend(document_direct)

    diagnostics: list[object] = []
    rules: list[str] = []
    evidence_fields: list[str] = []
    if direct_matches:
        relevance = DIRECT
        first_rule = direct_matches[0][1]
        reason = (
            "Explicit reference to Ukrainesærloven or Ukraine-specific protection."
            if first_rule == "direct.ukraine_special_law"
            else "Explicit reference to people displaced from Ukraine or their protection."
        )
        rules.extend(rule for _, rule in direct_matches)
        evidence_fields.extend(field for field, _ in direct_matches)
    else:
        allowed_texts = texts
        if unrelated_primary:
            allowed_texts = [item for item in texts if item[0] not in {"documents.title"}]
        potential = [(field, rule) for field, _, text in allowed_texts for rule in _potential_matches(text)]
        if suppressed_document_direct:
            relevance = POTENTIAL
            reason = "A related document has direct evidence, but the case title points to an unrelated subject; review the conflict."
            rules.extend(rule for _, rule in document_direct)
            rules.extend(("potential.conflicting_context_review", "negative.unrelated_primary_context", "negative.document_direct_suppressed"))
            evidence_fields.extend(field for field, _ in document_direct)
            diagnostics.append({
                "code": "conflicting_context",
                "message": "Direct document evidence conflicts with an unrelated primary title; classified POTENTIAL for review.",
                "positive_rules": list(dict.fromkeys(rule for _, rule in document_direct)),
                "negative_rules": list(dict.fromkeys([*negative_rules, "negative.unrelated_primary_context", "negative.document_direct_suppressed"])),
                "evidence_fields": ["title", "documents.title"],
            })
        elif potential:
            relevance = POTENTIAL
            rules.extend(rule for _, rule in potential)
            evidence_fields.extend(field for field, _ in potential)
            reason = _POTENTIAL_REASONS[potential[0][1]]
        elif not texts:
            relevance = IRRELEVANT
            reason = "Insufficient publication text for assessment."
            diagnostics.append("insufficient_information")
        else:
            relevance = IRRELEVANT
            reason = "No configured relevance rule matched the available publication text."

    rules.extend(negative_rules)
    matched_rules = tuple(dict.fromkeys(rules))
    ordered_evidence = tuple(sorted(set(evidence_fields), key=lambda field: (field not in {"title", "content_text", "stages.title", "documents.title"}, field)))

    source_value = record.get("source")
    external_value = record.get("external_id")
    source = source_value if isinstance(source_value, str) else None
    external_id = str(external_value) if external_value is not None and not isinstance(external_value, (dict, list, bool)) else None
    historical = record.get("historical")
    notify_eligible = record.get("notify_eligible")
    incomplete_source = record.get("complete") is False
    if incomplete_source:
        diagnostics.append({
            "code": "incomplete_source_data",
            "message": "The source record is explicitly marked incomplete; classification uses available text only.",
        })
    if historical is True or incomplete_source or ("insufficient_information" in diagnostics and notify_eligible is True):
        notify_eligible = False
    return RelevanceResult(
        source=source,
        external_id=external_id,
        relevance=relevance,
        reason=reason,
        matched_rules=matched_rules,
        ruleset_version=RULESET_VERSION,
        evidence_fields=ordered_evidence,
        diagnostics=tuple(diagnostics),
        publication=record,
        historical=historical,
        notify_eligible=notify_eligible,
        notification_blocked="insufficient_information" in diagnostics or incomplete_source,
    )


__all__ = ["DIRECT", "IRRELEVANT", "POTENTIAL", "RULESET_VERSION", "RelevanceResult", "classify", "normalize_text"]
