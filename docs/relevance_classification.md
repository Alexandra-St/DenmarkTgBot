# Rule-based publication relevance screening

This is an offline, deterministic screening layer over normalized collector records. It does not change either collector, persist results, create events, or send notifications. It uses Python's standard library. Real Danish examples and the scope of the live audit are documented in `docs/relevance_real_world_audit.md`; regression test examples are synthetic.

## Categories

| Value | Meaning |
| --- | --- |
| `DIRECT` | Explicit reference to Ukraine's special-law protection, people displaced from Ukraine, or Ukraine-linked temporary protection. |
| `POTENTIAL` | A residence, work, support, housing, integration, family, or education topic has meaningful foreign, refugee, or displaced-person context. |
| `IRRELEVANT` | No configured rule matched. For empty or unreadable text, inspect `diagnostics` to distinguish insufficient information from an evaluated non-match. |

The values match the existing D1 enum. They indicate possible relevance, not legal outcomes or confirmed changes to anyone's rights. This is not legal advice.

## Inputs and output

`classify()` accepts a mapping or either current normalized model. It reads only:

- Høringsportalen: `source`, `external_id`, `title`, `content_text`, `historical`, `notify_eligible`.
- Folketinget: `source`, `external_id`, `title`, `stages[].title`, `documents[].title`, `historical`, `notify_eligible`.

It does not use `feed_codes`, status, votes, dates, case type/category, author, or URLs as relevance evidence. Original fields and text are returned unchanged under `publication`. Matching applies Unicode NFC, case folding, whitespace collapse, and punctuation-to-word-boundary normalization, preserving Danish `æ`, `ø`, and `å`.

Every result has `source`, `external_id`, `relevance`, a short `reason`, stable `matched_rules`, `ruleset_version`, `evidence_fields`, `diagnostics`, and the unchanged `publication`. Ruleset version is `1.1`. An empty record uses enum-compatible `IRRELEVANT` plus `insufficient_information` and `notification_blocked: true`; it must not be treated as a reliably assessed irrelevant item. An input with `complete: false` adds a structured `incomplete_source_data` diagnostic and sets `notification_blocked: true` and `notify_eligible: false`, while retaining the classification based on available text. Historical records retain `historical: true` and cannot become notification-eligible. Incremental eligibility is passed through unchanged unless the source is explicitly incomplete or text is insufficient. The classifier never creates notifications.

Example:

```json
{
  "source": "folketinget",
  "external_id": "12345",
  "relevance": "DIRECT",
  "reason": "Explicit reference to Ukrainesærloven or Ukraine-specific protection.",
  "matched_rules": ["direct.ukraine_special_law"],
  "ruleset_version": "1.1",
  "evidence_fields": ["title"],
  "diagnostics": [],
  "historical": true,
  "notify_eligible": false,
  "notification_blocked": false,
  "publication": {"title": "Ukrainesærloven"}
}
```

## Rule precedence

1. Specific direct expressions are evaluated in the main title, Høringsportalen `content_text`, and Folketinget stage titles. Examples include inflected/hyphenated forms of `Ukrainesærloven`, `fordrevne fra Ukraine`, and Ukraine-linked `midlertidig beskyttelse`. `Ukraine` or `flygtninge` alone is insufficient.
2. A related Folketinget document title can provide direct evidence when the primary title is generic. If a document-only direct match conflicts with a primary title that clearly identifies an unrelated domain, the result is conservatively `POTENTIAL` for review. `conflicting_context` records the positive rules, negative rules, and evidence fields; `matched_rules` retains both the direct document evidence and unrelated-context marker. A direct match in the primary title still takes precedence.
3. Direct evidence takes precedence over generic negative context. An explicit special-law title is not downgraded because the same record also mentions military or foreign-policy terms.
4. Potential rules combine subject matter with affected population context, except immigration-specific residence terms and family reunification, which are inherently scoped to residence. Broad terms such as `arbejde`, `bolig`, and `uddannelse` do not qualify alone. General labor-market, housing-construction, and education-policy markers are recorded as negative evidence but do not override a positive population/topic combination.
5. No readable title, content, stage title, or document title produces the insufficient-information diagnostic.

Stable identifiers include `direct.ukraine_special_law`, `direct.displaced_from_ukraine`, `direct.ukraine_temporary_protection`, `potential.work_foreign_population`, `potential.housing_displaced_population`, `negative.ukraine_foreign_policy_or_aid`, `negative.unrelated_primary_context`, and `negative.document_direct_suppressed`. `matched_rules` preserves match order and removes duplicates; `evidence_fields` identifies where they matched. Diagnostics are strings for legacy `insufficient_information` and structured objects for `conflicting_context` and `incomplete_source_data`.

## Offline batch evaluation

The utility reads existing JSON without network access and never modifies the input file. It accepts the Høringsportalen `publications` envelope, Folketinget `cases` envelope, one publication object, or a JSON list. It emits per-record classifications, rule IDs, and totals by category.

```sh
python3 -m collectors.relevance_batch local_collector_output.json
```

JSON goes to stdout; input and format errors go to stderr.

## Extending the rules

Add a narrowly scoped Danish phrase and stable identifier in `collectors/relevance.py`. Extend the reason map for a new potential rule, then add both positive and nearby negative synthetic examples in `tests/test_relevance.py`. Do not classify by source or feed membership. Keep direct rules specific enough that a Ukraine mention, `flygtninge`, or broad everyday subject does not match alone.

## Limitations

- The phrase rules are not a complete Danish legal taxonomy. Synonyms, abbreviations, unusual word order, OCR errors, and context requiring legal interpretation may be missed.
- Residence expressions may conservatively produce `POTENTIAL` when a provision does not apply to this bot's audience. Consequential items need human review.
- Stage and document titles may be incomplete or loosely related. Known unrelated primary domains suppress document-only direct matches, but a small heuristic cannot identify every mismatch.
- Negative markers are evidence, not a universal blacklist; explicit direct signals take precedence.
- The classifier identifies topical relevance only. It does not establish that an amendment was adopted, entered into force, or affects an individual.

This is a rule-based relevance screening system, not legal advice. A result signals possible relevance, not confirmed legal impact.
