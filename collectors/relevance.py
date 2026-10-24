"""Offline, deterministic relevance screening for normalized publications."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Mapping

from collectors.hearing_evidence import DRAFT_NEW_PROVISIONS

RULESET_VERSION = "1.2"
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
    ("direct.ukrainian_refugees", _compile(r"\bukrainsk(?:e)?\s+flygtning(?:e|en|es|ens|ene|enes)?\b")),
    ("direct.ukrainian_refugees", _compile(r"\bflygtning(?:e|en|ene)?\s+fra\s+ukraine\b")),
)
_UKRAINIAN = _compile(r"\bukrain\w*\b")
_TEMP_PROTECTION = _compile(r"\bmidlertidig\s+beskyttelse\b")

_POPULATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Inflections of people, excluding compounds such as udlændingeloven.
    _compile(r"\budlænding(?:e|en|es|ens|ene|enes|s)?\b"),
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
    "potential.temporary_protection_general": "Temporary protection wording is linked to Ukraine-specific context.",
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
    # Attachment provenance is never flattened into content_text. Current-law and
    # other attachments cannot promote a proposed change.
    if record.get("source") == "hoeringsportalen":
        fragments = record.get("evidence_fragments")
        for fragment in fragments if isinstance(fragments, (list, tuple)) else ():
            if isinstance(fragment, Mapping) and fragment.get("kind") == DRAFT_NEW_PROVISIONS:
                add("evidence.DRAFT_NEW_PROVISIONS", fragment.get("text"))
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


def _target_context(text: str) -> bool:
    # Bare særloven is ambiguous (e.g. Afghanistansærloven). Do not infer Ukraine.
    return bool(_UKRAINIAN.search(text))


def _child_evidence_scope(record: Mapping[str, object], primary_text: str) -> str:
    """Require a topical bridge before a child can classify an entire Sag."""
    if record.get("source") != "folketinget":
        return "topical"
    title = normalize_text(record.get("title")) if isinstance(record.get("title"), str) else ""
    if record.get("type_id") == 4 or re.fullmatch(r"[a-zæøå]+ alm del(?: .*)?", title):
        return "mixed"
    unrelated_subjects = (
        _compile(r"\badopter\w*\b"),
        *_NEGATIVE_RULES[0][1],
        _compile(r"\b(?:vejlov\w*|trafik\w*|jernbane\w*|togdrift\w*|boligbyggeri\w*|uddannelsespolitik\w*)\b"),
    )
    if _has(unrelated_subjects, primary_text):
        return "none"
    if record.get("type_id") == 6 or re.search(r"\brådsmøde\w*\b|\bfinanslov\w*\b", title):
        return "mixed"
    generic_case = re.fullmatch(r"(?:forslag|udkast)(?: til lov)?(?: om(?: ændrede| nye)? regler)?", title)
    if (generic_case or _target_context(primary_text)
            or any(_has(patterns, primary_text) for patterns in _TOPICS.values())
            or re.search(r"\bopholdsreg(?:el|ler)\w*\b", primary_text)):
        return "topical"
    return "none"


def _potential_matches(text: str, *, target_context: bool = False) -> list[str]:
    population = _has(_POPULATION_PATTERNS, text)
    target_context = target_context or _target_context(text)
    matched: list[str] = []
    for topic, patterns in _TOPICS.items():
        if not _has(patterns, text):
            continue
        if topic == "residence" and target_context:
            matched.append("potential.residence_immigration_law")
        elif topic == "work" and population and target_context:
            matched.append("potential.work_foreign_population")
        elif topic == "social_support" and target_context and (population or _has((_compile(r"\bintegrationsydelse\w*\b"),), text)):
            matched.append("potential.social_support_targeted")
        elif topic == "integration" and population and target_context:
            matched.append("potential.integration_targeted")
        elif topic == "housing" and population and target_context:
            matched.append("potential.housing_displaced_population")
        elif topic == "family" and target_context:
            matched.append("potential.family_reunification")
        elif topic == "education" and population and target_context:
            matched.append("potential.education_displaced_population")
    if _TEMP_PROTECTION.search(text) and target_context:
        matched.append("potential.temporary_protection_general")
    return matched


def _scope_exclusion(record: Mapping[str, object], primary: str) -> str | None:
    """Positive evidence of another jurisdiction/regime, never absence of a keyword."""
    if record.get("source") != "folketinget":
        return None
    title = normalize_text(record.get("title")) if isinstance(record.get("title"), str) else ""
    strong = _matched_direct(primary)
    if not strong:
        if re.search(r"\bafghanistansærlov\w*\b", title) and not _UKRAINIAN.search(primary):
            child_titles = " ".join(str(item.get("title") or "") for key in ("documents", "stages")
                                    for item in record.get(key, ()) or () if isinstance(item, Mapping))
            if not _UKRAINIAN.search(normalize_text(child_titles)):
                return "negative.other_protection_regime"
        if (re.search(r"\b(?:for grønland|i grønland|grønlandske)\b", title)
                and not re.search(r"\bdanmark\b", title) and not re.search(r"\b1\s+.*\b2\b", title)):
            return "negative.greenland_specific_regime"
        syrian_subject = re.search(r"\bsyriske (?:flygtninge|asylansøgere|statsborgere)\b|\b(?:syrere|syrer|syrien)\b", title)
        # A country mentioned as one example in a general programme is not its scope.
        if (syrian_subject and not _UKRAINIAN.search(primary)
                and not re.search(r"\bf eks\b|\beksempelvis\b", title)
                and (record.get("type_id") in (9, 10) or title.startswith("om ")
                     or title.startswith("midlertidig beskyttelse for syriske"))):
            return "negative.other_protection_regime"
        if (re.search(r"\bpermanent(?:e)? ophold\w*\b", title)
                and re.search(r"\bfængselsstraf\w*|\bkriminalitet\b|\bdomfæld\w*", title)
                and not _UKRAINIAN.search(primary)):
            return "negative.permanent_residence_conviction"
        dates = [item.get("date_raw") for item in record.get("stages", ()) or () if isinstance(item, Mapping)]
        years = [int(value[:4]) for value in dates if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value)]
        individual_scheme = (re.search(r"\b(?:praktikant\w*|praktikophold\w*)\b", title)
                             or (re.search(r"\bsagsbehandling\b", title)
                                 and re.search(r"\b(?:udvises|udvist|sagen|familien)\b", title)))
        if record.get("type_id") == 10 and individual_scheme and years and min(years) < 2022:
            return "negative.pre_temporary_protection_individual_question"
    if not strong or all(rule != "direct.ukraine_special_law" for rule in strong):
        unrelated = re.search(r"\badopter\w*\b|\bmilitær\w* (?:bidrag|støtte|bistand)\b|\b(?:vejlov\w*|jernbane\w*|togdrift\w*)\b", title)
        if unrelated and not strong:
            return "negative.unrelated_case_subject"
    return None


def _broad_scope(text: str) -> bool:
    population = _has(_POPULATION_PATTERNS, text)
    migration_context = population or bool(re.search(r"\b(?:udlænding\w*|flygtning\w*|ophold\w*|ukrain\w*)\b", text))
    return (_has(_TOPICS["residence"] + _TOPICS["family"] + _TOPICS["work"], text)
            or bool(_TEMP_PROTECTION.search(text))
            or (migration_context and any(_has(patterns, text) for patterns in _TOPICS.values()))
            or bool(re.search(r"\bintegrationsydelse\w*\b", text)))


def classify(publication: object) -> RelevanceResult:
    """Screen with provenance, explicit scope exclusions and conservative uncertainty."""
    record = _as_mapping(publication)
    texts = _publication_texts(record)
    primary = [item for item in texts if item[0] in {"title", "content_text"}]
    drafts = [item for item in texts if item[0] == "evidence.DRAFT_NEW_PROVISIONS"]
    children = [item for item in texts if item[0] in {"documents.title", "stages.title"}]
    primary_context = " ".join(text for _, _, text in primary)
    # One child's words cannot establish scope for unrelated siblings.
    primary_direct = [(field, rule) for field, _, text in primary for rule in _matched_direct(text)]
    # A comparison is not an assertion that the proposed change applies to this group.
    comparative = re.compile(r"\bpå lige fod med\b|\bsammenlignet med\b|\bi modsætning til\b|\btil forskel fra\b")
    child_direct = [(field, rule) for field, _, text in children
                    if not comparative.search(text) for rule in _matched_direct(text)]
    child_target = [(field, "potential.child_target_context") for field, _, text in children if _target_context(text)]
    draft_direct = [(field, rule) for field, _, text in drafts for rule in _matched_direct(text)]
    negative_rules: list[str] = []
    if _UKRAINIAN.search(primary_context) and _has(_NEGATIVE_RULES[0][1], primary_context):
        negative_rules.append(_NEGATIVE_RULES[0][0])
    for rule_id, patterns in _NEGATIVE_RULES[1:]:
        if _has(patterns, primary_context):
            negative_rules.append(rule_id)
    child_scope = _child_evidence_scope(record, primary_context)
    container = (record.get("type_id") == 4 or bool(re.fullmatch(r"[a-zæøå]+ alm del(?: .*)?", normalize_text(str(record.get("title") or "")))))
    exclusion = None if container else _scope_exclusion(record, primary_context)
    if child_scope == "none" and not exclusion:
        child_scope = "uncertain"
    # An explicitly unrelated substantive title cannot inherit a comparison in a child.
    if child_scope == "none" and (child_direct or child_target):
        negative_rules.append("negative.child_evidence_outside_case_scope")
    direct_matches = primary_direct[:]
    if _target_context(primary_context):
        direct_matches.extend(draft_direct)
    title = normalize_text(record.get("title")) if isinstance(record.get("title"), str) else ""
    generic = re.fullmatch(r"(?:forslag|udkast)(?: til lov)?(?: om(?: ændrede| nye)? regler)?|forslag om (?:ændrede|nye) opholdsregler", title)
    if child_scope == "topical" and (generic or _target_context(primary_context)):
        direct_matches.extend(child_direct)
    mixed = (child_scope == "mixed" or (record.get("source") == "folketinget"
             and record.get("type_id") == 9 and _has(_NEGATIVE_RULES[0][1], primary_context)))
    diagnostics: list[object] = []
    rules: list[str] = []
    evidence_fields: list[str] = []
    allowed = primary + drafts + (children if child_scope != "none" else [])
    potential = [(field, rule) for field, _, text in allowed
                 for rule in _potential_matches(text, target_context=_target_context(primary_context))]
    relevant_child = (child_direct + child_target) if child_scope != "none" else []
    relevant_child += draft_direct
    uncertain = [(field, text) for field, _, text in allowed if _broad_scope(text)]
    if container and record.get("complete") is False and not uncertain:
        uncertain = [("case.graph", "")]
    if exclusion:
        relevance = IRRELEVANT
        reason = "Available evidence establishes a different subject, jurisdiction or legal regime."
        rules.append(exclusion)
    elif direct_matches and not mixed:
        relevance = DIRECT
        reason = "Explicit Ukraine-specific protection evidence within the publication scope."
        rules.extend(rule for _, rule in direct_matches)
        evidence_fields.extend(field for field, _ in direct_matches)
    elif direct_matches or relevant_child:
        relevance = POTENTIAL
        reason = "Target-specific evidence belongs to a mixed or not fully resolved case scope."
        rules.append("potential.mixed_case_target_context" if mixed else "potential.child_target_context")
        evidence_fields.extend(field for field, _ in direct_matches + relevant_child)
    elif potential:
        relevance = POTENTIAL
        rules.extend(rule for _, rule in potential)
        evidence_fields.extend(field for field, _ in potential)
        reason = _POTENTIAL_REASONS[potential[0][1]]
    elif uncertain:
        relevance = POTENTIAL
        reason = "Insufficient scope evidence to confirm or exclude applicability to the target group."
        rules.append("potential.insufficient_scope_evidence")
        diagnostics.append("insufficient_scope_evidence")
        evidence_fields.extend(field for field, _ in uncertain)
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
