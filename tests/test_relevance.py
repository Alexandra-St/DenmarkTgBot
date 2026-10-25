from __future__ import annotations

import unittest
import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from collectors.relevance import DIRECT, IRRELEVANT, POTENTIAL, RULESET_VERSION, classify, normalize_text
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

    def test_unrelated_case_cannot_be_promoted_by_a_direct_child(self):
        for title in ("Dansk militær støtte til Ukraine", "Ændring af vejloven", "Forslag om adopterede fra udlandet"):
            with self.subTest(title=title):
                result = classify(publication(title, documents=[{"title": "Ukrainesærloven"}]))
                self.assertEqual(result.relevance, IRRELEVANT)
                self.assertIn("negative.child_evidence_outside_case_scope", result.matched_rules)
                self.assertNotIn("documents.title", result.evidence_fields)
                self.assertFalse(any(rule.startswith("direct.") for rule in result.matched_rules))

    def test_primary_direct_evidence_still_beats_conflicting_document_context(self):
        result = classify(publication("Ukrainesærloven", documents=[{"title": "Dansk militær støtte til Ukraine"}]))
        self.assertEqual(result.relevance, DIRECT)
        self.assertNotIn("conflicting_context", [item.get("code") for item in result.diagnostics if isinstance(item, dict)])

    def test_general_immigration_rules_need_target_context(self):
        result = classify(publication("Ændring af udlændingeloven om opholdstilladelser for udlændinge"))
        self.assertEqual(result.relevance, POTENTIAL)
        self.assertIn("insufficient_scope_evidence", result.diagnostics)
        self.assertNotIn("potential.residence_immigration_law", result.matched_rules)

    def test_residence_and_family_with_explicit_primary_target_context(self):
        for topic in ("opholdstilladelse", "udlændingeloven", "familiesammenføring"):
            with self.subTest(topic=topic):
                result = classify({"source": "hoeringsportalen", "title": "Regler om " + topic,
                                   "content_text": "Ordningen omfatter også ukrainere."})
                self.assertEqual(result.relevance, POTENTIAL)
        self.assertEqual(classify(publication("Opholdstilladelse ved midlertidig beskyttelse")).relevance, POTENTIAL)
        self.assertEqual(classify(publication("Opholdstilladelse efter Afghanistansærloven")).relevance, IRRELEVANT)

    def test_population_inflections_are_context_not_standalone_relevance(self):
        for person in ("udlænding", "udlændinge", "udlændingen", "udlændinges", "udlændingene", "udlændingenes"):
            with self.subTest(person=person):
                self.assertEqual(classify(publication(person)).relevance, IRRELEVANT)
                self.assertEqual(classify(publication("Integrationsprogram for " + person)).relevance, POTENTIAL)
        self.assertEqual(classify(publication("Integrationsindsats nævnt i udlændingeloven")).relevance, POTENTIAL)

    def test_ukrainian_refugee_inflections_are_direct(self):
        for phrase in ("ukrainske flygtninge", "ukrainske flygtninges", "den ukrainske flygtning",
                       "ukrainske flygtningenes", "flygtninge fra Ukraine"):
            with self.subTest(phrase=phrase):
                result = classify(publication("Ophold for " + phrase))
                self.assertEqual(result.relevance, DIRECT)
                self.assertIn("direct.ukrainian_refugees", result.matched_rules)

    def test_committee_container_caps_child_relevance_at_potential(self):
        for details in ({"title": "UUI alm. del"}, {"title": "Komitémateriale", "type_id": 4}):
            with self.subTest(details=details):
                result = classify({"source": "folketinget", **details,
                                   "stages": [{"title": "Fordrevne fra Ukraine"}],
                                   "documents": [{"title": "Ændring af Ukrainesærloven"}]})
                self.assertEqual(result.relevance, POTENTIAL)
                self.assertIn("documents.title", result.evidence_fields)

    def test_mixed_case_target_agenda_is_potential(self):
        result = classify(publication("Rådsmøde om retlige og indre anliggender", type_id=6,
                                     stages=[{"title": "Den fremtidige juridiske status for fordrevne fra Ukraine"}]))
        self.assertEqual(result.relevance, POTENTIAL)
        self.assertIn("potential.mixed_case_target_context", result.matched_rules)
        self.assertIn("stages.title", result.evidence_fields)

    def test_unrelated_case_ignores_both_potential_and_direct_stage_or_document(self):
        for kind in ("stages", "documents"):
            for child_title in ("Ukrainske flygtninge", "Opholdstilladelse for ukrainere"):
                with self.subTest(kind=kind, child_title=child_title):
                    result = classify(publication("Forslag om adopterede fra udlandet", **{kind: [{"title": child_title}]}))
                    self.assertEqual(result.relevance, IRRELEVANT)

    def test_container_without_relevant_or_uncertain_child_is_irrelevant(self):
        self.assertEqual(classify(publication("TRU alm. del", type_id=4,
                         documents=[{"title": "Asfaltering af motorveje"}])).relevance, IRRELEVANT)
        result = classify(publication("UUI alm. del", type_id=4,
                         documents=[{"title": "Opholdstilladelse"}]))
        self.assertEqual(result.relevance, POTENTIAL)
        self.assertIn("insufficient_scope_evidence", result.diagnostics)

    def test_broad_keywords_are_uncertainty_not_confirmed_target_rules(self):
        for word in ("udlændingeloven", "opholdstilladelse", "familiesammenføring", "Midlertidig beskyttelse"):
            with self.subTest(word=word):
                result = classify(publication(word))
                self.assertEqual(result.relevance, POTENTIAL)
                self.assertEqual(result.matched_rules, ("potential.insufficient_scope_evidence",))
                self.assertIn("insufficient_scope_evidence", result.diagnostics)

    def test_syria_and_greenland_are_explicit_scope_exclusions(self):
        for title in ("Midlertidig beskyttelse for syriske flygtninge i Danmark",
                      "Lov for Grønland om opholds- og arbejdstilladelse"):
            with self.subTest(title=title):
                result = classify(publication(title, documents=[{"title": "Ukrainske flygtninge"}]))
                self.assertEqual(result.relevance, IRRELEVANT)
        self.assertEqual(classify(publication("Midlertidig beskyttelse af fordrevne fra Ukraine")).relevance, DIRECT)

    def test_old_source_update_does_not_prove_old_individual_case(self):
        result = classify(publication("Spørgsmål om sagsbehandling af opholdstilladelse", type_id=10,
                         source_updated_at_raw="2007-01-01"))
        self.assertEqual(result.relevance, POTENTIAL)
        result = classify(publication("Spørgsmål om sagsbehandling af opholdstilladelse", type_id=10,
                         stages=[{"date_raw": "2007-10-15T00:00:00"}]))
        self.assertEqual(result.relevance, POTENTIAL)
        result = classify(publication("Udvises efter sagsbehandling af opholdstilladelse", type_id=10,
                         stages=[{"date_raw": "2007-10-15T00:00:00"}]))
        self.assertEqual(result.relevance, IRRELEVANT)
        # Age of a general law/question alone is never sufficient to exclude scope.
        result = classify(publication("Regler om opholdstilladelse", type_id=10,
                         stages=[{"date_raw": "2007-10-15T00:00:00"}]))
        self.assertEqual(result.relevance, POTENTIAL)

    def test_child_target_does_not_make_another_program_direct(self):
        result = classify(publication("Opholdstilladelse efter Afghanistansærloven",
                         documents=[{"title": "På lige fod med ukrainske flygtninge"}]))
        self.assertEqual(result.relevance, POTENTIAL)
        self.assertFalse(any(rule.startswith("direct.") for rule in result.matched_rules))

    def test_political_resolution_is_not_a_concrete_direct_legal_change(self):
        result = classify(publication("Ukrainske fordrevne skal beskyttes; våben og sanktioner", type_id=9))
        self.assertEqual(result.relevance, POTENTIAL)
        self.assertIn("potential.mixed_case_target_context", result.matched_rules)

    def test_country_example_or_institution_name_does_not_exclude_general_programme(self):
        for title in ("Forslag om videreførelse af igu og mulighed for opholdstilladelse til personer der i Afghanistan har bistået myndigheder",
                      "Ændring af udlændingeloven om myndighedernes registre og Forsvarets adgang til oplysninger",
                      "Arbejdets vilkår og arbejdstilladelse for statsborgere fra nye EU lande"):
            with self.subTest(title=title):
                result = classify(publication(title, type_id=3))
                self.assertEqual(result.relevance, POTENTIAL)
                self.assertIn("insufficient_scope_evidence", result.diagnostics)

    def test_country_example_keeps_scope_uncertain(self):
        result = classify(publication("Regeringen vil give opholdstilladelse til f.eks. unge kvinder fra Syrien under uddannelse", type_id=10))
        self.assertEqual(result.relevance, POTENTIAL)
        self.assertIn("insufficient_scope_evidence", result.diagnostics)

    def test_container_scope_cannot_discard_target_child_or_missing_graph(self):
        result = classify(publication("Komité for Grønland", type_id=4,
                         documents=[{"title": "Ukrainske flygtninge i Danmark"}]))
        self.assertEqual(result.relevance, POTENTIAL)
        self.assertEqual(classify(publication("UUI alm. del", type_id=4, complete=False)).relevance, POTENTIAL)

    def test_comparative_child_cannot_make_generic_case_direct(self):
        result = classify(publication("Forslag om regler", documents=[{"title": "Undtagelse for andre grupper på lige fod med ukrainske flygtninge"}]))
        self.assertEqual(result.relevance, POTENTIAL)
        self.assertFalse(any(rule.startswith("direct.") for rule in result.matched_rules))

    def test_case_type_alone_does_not_downgrade_explicit_target_scope(self):
        self.assertEqual(classify(publication("Forlængelse af Ukrainesærloven", type_id=9)).relevance, DIRECT)

    def test_territory_only_scope_applies_to_both_sources_and_beats_ukraine_evidence(self):
        for source in ("folketinget", "hoeringsportalen"):
            for territory in ("Grønland", "Færøerne", "Norge"):
                with self.subTest(source=source, territory=territory):
                    result = classify({"source": source,
                        "title": f"Bekendtgørelse for {territory} om ophold for fordrevne fra Ukraine",
                        "evidence_fragments": [{"kind": "DRAFT_NEW_PROVISIONS", "text": "Ukrainesærloven"}]})
                    self.assertEqual(result.relevance, IRRELEVANT)
                    self.assertIn("negative.territory_specific_regime", result.matched_rules)

    def test_mixed_danish_and_territorial_scope_remains_applicable(self):
        for territory in ("Grønland", "Færøerne"):
            for title in (f"Regler for Danmark og {territory} om opholdstilladelse for ukrainere",
                          f"Regler for {territory} og Danmark om opholdstilladelse for ukrainere"):
                result = classify({"source": "hoeringsportalen", "title": title})
                self.assertEqual(result.relevance, POTENTIAL)
                self.assertNotIn("negative.territory_specific_regime", result.matched_rules)
        self.assertEqual(classify(publication("Opholdstilladelse i Danmark for personer fra Grønland")).relevance, POTENTIAL)
        self.assertEqual(classify({"source": "hoeringsportalen", "title": "Regler for Grønland om opholdstilladelser",
                                   "content_text": "Ordningen gælder også i Danmark for ukrainere."}).relevance, POTENTIAL)
        self.assertEqual(classify({"source": "hoeringsportalen", "title": "Regler for Grønland om opholdstilladelser",
                                   "content_text": "Ordningen gælder ikke i Danmark."}).relevance, IRRELEVANT)

    def test_child_ukraine_business_financing_military_and_event_do_not_establish_applicability(self):
        for title in ("Build with Ukraine og etablering af ukrainske virksomheder",
                      "Finansiering og investeringer via Ukrainefonden",
                      "Militær støtte til Ukraine og våbenleverancer",
                      "Invitation: humanitær adgang og sikker evakuering i Ukraine",
                      "Rådets betaling af niende rate under Ukrainefaciliteten"):
            with self.subTest(title=title):
                result = classify(publication("ERU alm. del", type_id=4, complete=True,
                                               documents=[{"title": title}]))
                self.assertEqual(result.relevance, IRRELEVANT)
                self.assertNotIn("potential.child_target_context", result.matched_rules)

    def test_humanitarian_event_cannot_inherit_protected_group_phrase(self):
        title = "Invitation til velgørenhedskoncert for ukrainske flygtninge"
        self.assertEqual(classify(publication(title)).relevance, IRRELEVANT)
        self.assertEqual(classify(publication("SOU alm. del", type_id=4,
                         documents=[{"title": title}])).relevance, IRRELEVANT)
        self.assertEqual(classify(publication("UUI alm. del", type_id=4,
                         documents=[{"title": "Invitation til høring om opholdstilladelse for ukrainske flygtninge"}])).relevance, POTENTIAL)

    def test_territorial_permits_are_not_mistaken_for_mixed_danish_law(self):
        result = classify(publication("Ændring af udlændingeloven om behandlingen af opholdstilladelser på Færøerne og i Grønland"))
        self.assertEqual(result.relevance, IRRELEVANT)
        self.assertEqual(classify(publication("Regler om opholdstilladelser i Danmark og på Færøerne")).relevance, POTENTIAL)

    def test_child_applicable_residence_rights_and_services_are_preserved(self):
        for title in ("Opholdstilladelse til ukrainere", "Rettigheder og tandpleje for ukrainere",
                      "Ændring af Ukrainesærloven", "Midlertidig beskyttelse for ukrainere"):
            with self.subTest(title=title):
                result = classify(publication("UUI alm. del", type_id=4, documents=[{"title": title}]))
                self.assertEqual(result.relevance, POTENTIAL)

    def test_foreign_residence_in_applicant_background_is_not_exclusive_jurisdiction(self):
        result = classify(publication("Spørgsmål om familiesammenføring med en hustru som har opholdstilladelse i Norge"))
        self.assertEqual(result.relevance, POTENTIAL)
        self.assertNotIn("negative.territory_specific_regime", result.matched_rules)

    def test_mixed_welfare_question_keeps_conservative_potential(self):
        result = classify(publication("FIU alm. del", type_id=4, documents=[{
            "title": "Afskære udenlandske statsborgere fra danske velfærdsydelser og yde lån til Ukraine"}]))
        self.assertEqual(result.relevance, POTENTIAL)
        statistics = classify(publication("Forslag til finanslov", type_id=3, documents=[{
            "title": "Udgifter til skattefinansierede velfærdsydelser for udenlandske statsborgere"},
            {"title": "Militær støtte til Ukraine"}]))
        self.assertEqual(statistics.relevance, IRRELEVANT)

    def test_bare_special_law_requires_manual_scope_review_without_automatic_upgrade(self):
        result = classify(publication("SUU alm. del", type_id=4, documents=[{
            "title": "Finansiering af voksentandplejen inden for rammerne af særloven og en tandkonto"}]))
        self.assertEqual(result.relevance, IRRELEVANT)
        self.assertIn("ambiguous_special_law_scope", result.diagnostics)
        self.assertEqual(classify(publication("Opholdstilladelse efter særloven")).relevance, POTENTIAL)

    def test_source_scope_regressions_are_not_identifier_exceptions(self):
        path = Path(__file__).parent / "fixtures" / "relevance_scope_1617.json"
        fixtures = json.loads(path.read_text())
        output = io.StringIO()
        with redirect_stdout(output):
            for item in fixtures:
                classify(item["record"])
        self.assertEqual(output.getvalue(), "")
        for item in fixtures:
            for external_id in (item["record"].get("external_id"), "999999999"):
                with self.subTest(name=item["name"], external_id=external_id):
                    result = classify({**item["record"], "external_id": external_id})
                    self.assertEqual(result.relevance, item["expected"])

    def test_production_audit_regression_evidence_and_identifier_independence(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures" / "relevance_production_audit_1611.json").read_text())
        self.assertEqual(len(fixtures), 12)
        for fixture in fixtures:
            with self.subTest(fixture=fixture["name"]):
                for external_id in (fixture["record"]["external_id"], "999999999"):
                    result = classify({**fixture["record"], "external_id": external_id})
                    self.assertEqual(result.relevance, fixture["expected"])
                    self.assertTrue(result.evidence_fields or result.relevance == IRRELEVANT)
        output = io.StringIO()
        with redirect_stdout(output):
            for fixture in fixtures:
                classify(fixture["record"])
        self.assertEqual(output.getvalue(), "")

    def test_work_permits_for_foreigners_are_potential(self):
        result = classify(publication("Arbejdstilladelser for udenlandske borgere"))
        self.assertEqual(result.relevance, POTENTIAL)
        self.assertIn("potential.insufficient_scope_evidence", result.matched_rules)

    def test_integration_benefits_for_displaced_people_are_potential(self):
        self.assertEqual(classify(publication("Integrationsydelser til fordrevne personer")).relevance, POTENTIAL)

    def test_integration_program_for_foreigners_is_potential(self):
        self.assertEqual(classify(publication("Integrationsprogram for udlændinge")).relevance, POTENTIAL)

    def test_housing_for_refugees_is_potential(self):
        self.assertEqual(classify(publication("Kommunal boligplacering af flygtninge")).relevance, POTENTIAL)

    def test_family_reunification_needs_target_context(self):
        self.assertEqual(classify(publication("Regler om familiesammenføring")).relevance, POTENTIAL)
        self.assertIn("insufficient_scope_evidence", classify(publication("familiesammenføring")).diagnostics)

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
        self.assertFalse(any(rule.startswith(("direct.", "potential.")) for rule in result.matched_rules))

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

    def test_incomplete_source_data_blocks_notification_without_changing_relevance(self):
        for title, expected in (("Ukrainesærloven", DIRECT), ("Ukraine og opholdstilladelser", POTENTIAL), ("Andre regler", IRRELEVANT)):
            with self.subTest(title=title):
                result = classify(publication(title, complete=False, historical=False, notify_eligible=True))
                self.assertEqual(result.relevance, expected)
                self.assertFalse(result.notify_eligible)
                self.assertTrue(result.notification_blocked)
                diagnostic = next(item for item in result.diagnostics if isinstance(item, dict) and item.get("code") == "incomplete_source_data")
                self.assertIn("available text only", diagnostic["message"])

    def test_complete_or_unspecified_source_completeness_does_not_block_notification(self):
        for extra in ({"complete": True}, {}):
            with self.subTest(extra=extra):
                result = classify(publication("Ukrainesærloven", notify_eligible=True, **extra))
                self.assertTrue(result.notify_eligible)
                self.assertFalse(result.notification_blocked)
                self.assertNotIn("incomplete_source_data", [item.get("code") for item in result.diagnostics if isinstance(item, dict)])

    def test_results_are_deterministic_and_include_required_fields(self):
        item = publication("Ukrainesærloven")
        first, second = classify(item).to_dict(), classify(item).to_dict()
        self.assertEqual(first, second)
        self.assertEqual(first["external_id"], "123")
        self.assertEqual(first["ruleset_version"], RULESET_VERSION)
        self.assertTrue(first["reason"])
        self.assertTrue(first["matched_rules"])

    def test_batch_evaluation_supports_both_collector_envelopes(self):
        hearings = evaluate({"publications": [publication("Ukrainesærloven"), publication("Arbejde")]})
        cases = evaluate({"cases": [publication("Ukrainesærloven"), publication("Ukraine og opholdstilladelser")]})
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
