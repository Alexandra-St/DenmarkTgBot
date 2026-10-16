from __future__ import annotations

import unittest
import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from collectors.relevance import DIRECT, IRRELEVANT, POTENTIAL, classify, normalize_text
from collectors.relevance_batch import evaluate, main as batch_main
from collectors.hoeringsportalen import Consultation
from collectors.folketinget_models import CaseStatus, CaseStage, LegislativeCase


def publication(title=None, **fields):
    return {"source": "folketinget", "external_id": "123", "title": title, **fields}


class RelevanceTests(unittest.TestCase):
    def test_direct_law_spelling_variants_and_inflections(self):
        for title in ("Ukrainesærloven", "Ukraine-særloven", "Ukraine særlovens", "Ukraine-særlovgivningen"):
            with self.subTest(title=title):
                result = classify(publication(title))
                self.assertEqual(result.relevance, DIRECT)
                self.assertIn("direct.ukraine_special_law", result.matched_rules)

    def test_direct_displaced_group_inflections(self):
        for title in ("Fordrevne fra Ukraine", "Personer der er fordrevet fra Ukraine", "Ukrainske fordrevne", "Fordrevne ukrainere", "Fordrevne personer fra Ukraine"):
            with self.subTest(title=title):
                self.assertEqual(classify(publication(title)).relevance, DIRECT)

    def test_temporary_protection_needs_ukraine_for_direct_and_population_for_potential(self):
        self.assertEqual(classify(publication("Midlertidig beskyttelse for ukrainere")).relevance, DIRECT)
        self.assertEqual(classify(publication("Midlertidig beskyttelse for fordrevne")).relevance, POTENTIAL)
        unlinked = "Ukraine nævnes i udenrigspolitikken. " + ("generel debat " * 20) + "Midlertidig beskyttelse for andre grupper"
        self.assertNotEqual(classify(publication(unlinked)).relevance, DIRECT)

    def test_direct_evidence_beats_negative_context(self):
        result = classify(publication("Dansk militær støtte til Ukraine efter Ukrainesærloven"))
        self.assertEqual(result.relevance, DIRECT)
        self.assertIn("negative.ukraine_foreign_policy_or_aid", result.matched_rules)

    def test_document_title_can_clarify_generic_title(self):
        result = classify(publication("Forslag om ændrede opholdsregler", documents=[{"title": "Ændring af Ukrainesærloven"}]))
        self.assertEqual(result.relevance, DIRECT)
        self.assertIn("documents.title", result.evidence_fields)

    def test_stage_title_can_be_direct_evidence(self):
        result = classify(publication("Forslag om ændrede regler", stages=[{"title": "Fordrevne fra Ukraine"}]))
        self.assertEqual(result.relevance, DIRECT)
        self.assertIn("stages.title", result.evidence_fields)

    def test_document_direct_is_suppressed_by_unrelated_primary_context(self):
        for title in ("Dansk militær støtte til Ukraine", "Ændring af vejloven"):
            with self.subTest(title=title):
                result = classify(publication(title, documents=[{"title": "Ukrainesærloven"}]))
                self.assertEqual(result.relevance, IRRELEVANT)
                self.assertIn("document_direct_suppressed_by_unrelated_primary_context", result.diagnostics)
                self.assertIn("direct.ukraine_special_law", result.matched_rules)

    def test_general_immigration_rules_are_potential(self):
        result = classify(publication("Ændring af udlændingeloven om opholdstilladelser for udlændinge"))
        self.assertEqual(result.relevance, POTENTIAL)
        self.assertIn("potential.residence_immigration_law", result.matched_rules)

    def test_work_permits_for_foreigners_are_potential(self):
        result = classify(publication("Arbejdstilladelser for udenlandske borgere"))
        self.assertEqual(result.relevance, POTENTIAL)
        self.assertIn("potential.work_foreign_population", result.matched_rules)

    def test_integration_benefits_for_displaced_people_are_potential(self):
        self.assertEqual(classify(publication("Integrationsydelser til fordrevne personer")).relevance, POTENTIAL)

    def test_integration_program_for_foreigners_is_potential(self):
        self.assertEqual(classify(publication("Integrationsprogram for udlændinge")).relevance, POTENTIAL)

    def test_housing_for_refugees_is_potential(self):
        self.assertEqual(classify(publication("Kommunal boligplacering af flygtninge")).relevance, POTENTIAL)

    def test_family_reunification_is_potential(self):
        self.assertEqual(classify(publication("Regler om familiesammenføring")).relevance, POTENTIAL)

    def test_education_access_for_displaced_people_is_potential(self):
        self.assertEqual(classify(publication("Adgang til uddannelse for fordrevne personer")).relevance, POTENTIAL)

    def test_foreign_policy_military_aid_and_trade_about_ukraine_are_irrelevant(self):
        for title in ("Dansk udenrigspolitik over for Ukraine", "Dansk militær bistand og våben til Ukraine", "Landbrugseksport og handel med Ukraine"):
            with self.subTest(title=title):
                self.assertEqual(classify(publication(title)).relevance, IRRELEVANT)

    def test_ukraine_or_flygtninge_alone_are_not_direct(self):
        self.assertNotEqual(classify(publication("Ukraine")).relevance, DIRECT)
        self.assertNotEqual(classify(publication("Flygtninge")).relevance, DIRECT)

    def test_unrelated_labor_housing_and_education_policy_are_irrelevant(self):
        titles = (
            "Reform af arbejdsmarkedets generelle lønforhold",
            "Boligbyggeri og nybyggeri i Aarhus",
            "Reform af dansk uddannelsespolitik",
            "Nye generelle regler for arbejde",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(classify(publication(title)).relevance, IRRELEVANT)

    def test_source_feed_status_vote_and_date_do_not_establish_relevance(self):
        result = classify(publication("Ændring af vejloven", feed_codes=["immigration_ministry"], status={"name": "Aktuel"}, votes=[{"approved": True}], updated_at="2026-01-01"))
        self.assertEqual(result.relevance, IRRELEVANT)
        self.assertEqual(result.matched_rules, ())

    def test_empty_or_unreadable_text_is_insufficient(self):
        result = classify({"source": "folketinget", "external_id": "7", "title": "... —", "notify_eligible": True})
        self.assertEqual(result.relevance, IRRELEVANT)
        self.assertIn("insufficient_information", result.diagnostics)
        self.assertTrue(result.notification_blocked)
        self.assertFalse(result.notify_eligible)

    def test_horingsportalen_title_and_content_are_supported(self):
        self.assertEqual(classify({"source": "hoeringsportalen", "external_id": "h1", "title": "Ukrainesærloven"}).relevance, DIRECT)
        self.assertEqual(classify({"source": "hoeringsportalen", "external_id": "h2", "title": "Udkast til lov", "content_text": "Fordrevne fra Ukraine omfattes af ændringen"}).relevance, DIRECT)

    def test_folketinget_document_and_stage_titles_are_supported(self):
        case = publication("Forslag om ændrede regler", stages=[{"title": "Ukrainske fordrevne"}], documents=[{"title": "Ændring af Ukrainesærloven"}])
        result = classify(case)
        self.assertEqual(result.relevance, DIRECT)
        self.assertIn("stages.title", result.evidence_fields)
        self.assertIn("documents.title", result.evidence_fields)

    def test_existing_normalized_model_objects_are_accepted(self):
        hearing = Consultation("h-1", "hoeringsportalen", "Fordrevne fra Ukraine", None, None, None, None, ())
        self.assertEqual(classify(hearing).relevance, DIRECT)
        case = LegislativeCase(
            external_id="42", source="folketinget", number="L 1", title="Forslag om regler",
            type_id=None, category_id=None, status=CaseStatus(None, None), source_updated_at_raw=None,
            updated_at_utc=None, source_url="https://oda.ft.dk/api/Sag(42)", legal_url=None,
            stages=(CaseStage(1, "Ukrainesærloven", None, None, None, None, None),),
            documents=(), votes=(), complete=True,
        )
        self.assertEqual(classify(case).relevance, DIRECT)

    def test_missing_optional_fields_and_danish_unicode_are_safe(self):
        raw_title = "ÆNDRING AF UKRAINE-SÆRLOVEN — fordrevne fra Ukraine"
        result = classify({"source": "hoeringsportalen", "external_id": "h3", "title": raw_title})
        self.assertEqual(result.relevance, DIRECT)
        self.assertEqual(result.publication["title"], raw_title)
        self.assertIn("særloven", normalize_text("SÆRLOVEN"))

    def test_historical_and_incremental_notification_flags_are_preserved_safely(self):
        old = classify(publication("Ukrainesærloven", historical=True, notify_eligible=True)).to_dict()
        self.assertTrue(old["historical"])
        self.assertFalse(old["notify_eligible"])
        incremental = classify(publication("Ukrainesærloven", historical=False, notify_eligible=None))
        self.assertIsNone(incremental.notify_eligible)

    def test_results_are_deterministic_and_include_required_fields(self):
        item = publication("Ukrainesærloven")
        first, second = classify(item).to_dict(), classify(item).to_dict()
        self.assertEqual(first, second)
        self.assertEqual(first["external_id"], "123")
        self.assertEqual(first["ruleset_version"], "1.0")
        self.assertTrue(first["reason"])
        self.assertTrue(first["matched_rules"])

    def test_batch_evaluation_supports_both_collector_envelopes(self):
        hearings = evaluate({"publications": [publication("Ukrainesærloven"), publication("Arbejde")]})
        cases = evaluate({"cases": [publication("Ukrainesærloven"), publication("Udlændingeloven")]})
        self.assertEqual(hearings["counts"], {"DIRECT": 1, "POTENTIAL": 0, "IRRELEVANT": 1})
        self.assertEqual(cases["counts"], {"DIRECT": 1, "POTENTIAL": 1, "IRRELEVANT": 0})

    def test_batch_cli_reads_local_file_and_does_not_modify_it(self):
        original = {"publications": [publication("Ukrainesærloven")]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            before = path.read_bytes()
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(batch_main([str(path)]), 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(json.loads(output.getvalue())["counts"]["DIRECT"], 1)


if __name__ == "__main__":
    unittest.main()
