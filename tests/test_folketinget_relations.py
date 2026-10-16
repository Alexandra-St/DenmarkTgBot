from __future__ import annotations

import re
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock

from collectors.folketinget import Cursor, ODA_ROOT, ODataPageError, ScanMetadata, ScanResult
from collectors.folketinget_models import (
    Afstemning,
    Dokument,
    Sag,
    SagDokument,
    Sagstrin,
    Sagsstatus,
    normalize_afstemning,
    normalize_dokument,
    normalize_sag,
    normalize_sagdokument,
    normalize_sagstrin,
    normalize_sagsstatus,
)
from collectors.folketinget_relations import assemble_cases, load_case


STAMP = "2026-10-14T08:46:08.6566667"


def sag(case_id: int, **updates: object) -> Sag:
    row: dict[str, object] = {"id": case_id, "nummer": f"L {case_id}", "titel": "Forslag om ø-støtte", "statusid": None, "typeid": 3, "kategoriid": 13, "opdateringsdato": STAMP}
    row.update(updates)
    result = normalize_sag(row)
    assert result.record is not None
    return result.record


def stage(stage_id: int, case_id: int, status_id: int | None = None) -> Sagstrin:
    result = normalize_sagstrin({"id": stage_id, "sagid": case_id, "titel": f"Behandling {stage_id}", "dato": "2026-01-01T00:00:00", "statusid": status_id, "opdateringsdato": STAMP})
    assert result.record is not None
    return result.record


def link(link_id: int, case_id: int, doc_id: int) -> SagDokument:
    result = normalize_sagdokument({"id": link_id, "sagid": case_id, "dokumentid": doc_id, "bilagsnummer": str(link_id), "frigivelsesdato": "2026-01-02T00:00:00", "opdateringsdato": STAMP})
    assert result.record is not None
    return result.record


def document(doc_id: int, title: str | None = None) -> Dokument:
    result = normalize_dokument({"id": doc_id, "titel": title or f"Bilag {doc_id}", "dato": "2026-01-01T00:00:00", "modtagelsesdato": "2026-01-02T00:00:00", "opdateringsdato": STAMP})
    assert result.record is not None
    return result.record


def vote(vote_id: int, stage_id: int | None) -> Afstemning:
    result = normalize_afstemning({"id": vote_id, "sagstrinid": stage_id, "nummer": vote_id, "vedtaget": True, "konklusion": "Vedtaget", "kommentar": "Afstemning", "opdateringsdato": STAMP})
    assert result.record is not None
    return result.record


def status(status_id: int, name: str) -> Sagsstatus:
    result = normalize_sagsstatus({"id": status_id, "status": name, "opdateringsdato": STAMP})
    assert result.record is not None
    return result.record


def successful_scan(rows: list[dict[str, object]], page_count: int = 1) -> ScanResult:
    return ScanResult(tuple(rows), ScanMetadata("Sagstrin", page_count, len(rows), None, None, True, page_count + 1))


class AssemblyTests(unittest.TestCase):
    def test_case_has_multiple_stages_and_stable_order(self) -> None:
        result = assemble_cases([sag(1)], stages=[stage(8, 1), stage(3, 1)])
        self.assertEqual([item.id for item in result.cases[0].stages], [3, 8])
        self.assertTrue(result.cases[0].complete)

    def test_multiple_document_links_preserve_link_and_document_ids(self) -> None:
        result = assemble_cases(
            [sag(1)], document_links=[link(9, 1, 7), link(4, 1, 7), link(6, 1, 8)],
            documents=[document(7), document(8)],
        )
        children = result.cases[0].documents
        self.assertEqual([(item.id, item.link_id) for item in children], [(7, 4), (8, 6), (7, 9)])

    def test_shared_document_is_reused_for_multiple_cases(self) -> None:
        result = assemble_cases([sag(1), sag(2)], document_links=[link(11, 1, 7), link(12, 2, 7)], documents=[document(7)])
        self.assertEqual([case.documents[0].id for case in result.cases], [7, 7])
        self.assertEqual([case.documents[0].link_id for case in result.cases], [11, 12])

    def test_multiple_votes_attach_by_stage_id(self) -> None:
        result = assemble_cases([sag(1)], stages=[stage(10, 1)], votes=[vote(3, 10), vote(2, 10)])
        self.assertEqual([item.id for item in result.cases[0].votes], [2, 3])
        self.assertEqual([item.stage_id for item in result.cases[0].votes], [10, 10])

    def test_null_stage_vote_is_unresolved_and_not_guessed(self) -> None:
        result = assemble_cases([sag(1)], votes=[vote(3, None)])
        self.assertEqual(result.cases[0].votes, ())
        self.assertIn("unresolved_vote", [item.code for item in result.diagnostics])

    def test_missing_document_target_is_preserved_as_partial_link(self) -> None:
        result = assemble_cases([sag(1)], document_links=[link(5, 1, 70)])
        child = result.cases[0].documents[0]
        self.assertEqual((child.id, child.link_id), (70, 5))
        self.assertIsNone(child.title)
        self.assertFalse(result.cases[0].complete)
        self.assertIn("missing_document", [item.code for item in result.diagnostics])

    def test_missing_status_keeps_id_and_marks_case_incomplete(self) -> None:
        result = assemble_cases([sag(1, statusid=404)])
        self.assertEqual(result.cases[0].status.id, 404)
        self.assertIsNone(result.cases[0].status.name)
        self.assertFalse(result.cases[0].complete)

    def test_stage_status_is_preserved_but_not_resolved_as_case_status(self) -> None:
        result = assemble_cases([sag(1, statusid=25)], stages=[stage(10, 1, status_id=900)], statuses=[status(25, "Vedtaget")])
        self.assertEqual(result.cases[0].status.name, "Vedtaget")
        self.assertEqual(result.cases[0].stages[0].status_id, 900)
        self.assertFalse(hasattr(result.cases[0].stages[0], "status_name"))

    def test_identical_duplicates_deduplicate_and_conflicting_duplicates_report(self) -> None:
        source = sag(1)
        result = assemble_cases([source, source, replace(source, titel="Ændret titel")])
        self.assertEqual(len(result.cases), 1)
        self.assertIn("duplicate_conflict", [item.code for item in result.diagnostics])
        self.assertFalse(result.cases[0].complete)

    def test_output_order_and_source_urls_are_deterministic(self) -> None:
        result = assemble_cases([sag(9), sag(2)], stages=[stage(8, 2), stage(1, 2)])
        self.assertEqual([case.external_id for case in result.cases], ["2", "9"])
        self.assertEqual(result.cases[0].source_url, f"{ODA_ROOT}Sag(2)")
        self.assertEqual([item.id for item in result.cases[0].stages], [1, 8])

    def test_orphan_stage_and_link_remain_structured_diagnostics(self) -> None:
        result = assemble_cases([sag(1)], stages=[stage(2, 99)], document_links=[link(3, 99, 7)])
        self.assertEqual({item.code for item in result.diagnostics}, {"orphan_stage", "orphan_document_link"})


class HydrationTests(unittest.TestCase):
    def make_client(self, *, failing_collection: str | None = None, doc_count: int = 1) -> Mock:
        client = Mock()
        base = {"id": 1, "nummer": "L 1", "titel": "Forslag", "statusid": 25, "typeid": 3, "kategoriid": 13, "opdateringsdato": STAMP}
        client.fetch_page.side_effect = lambda collection, params: (
            [base] if collection == "Sag" else ([{"id": 25, "status": "Vedtaget", "opdateringsdato": STAMP}] if collection == "Sagsstatus" else [])
        )
        stages = [{"id": 10, "sagid": 1, "titel": "1. behandling", "statusid": 77, "opdateringsdato": STAMP}]
        links = [{"id": index + 100, "sagid": 1, "dokumentid": index + 200, "opdateringsdato": STAMP} for index in range(doc_count)]
        docs = [{"id": item["dokumentid"], "titel": f"Dokument {item['dokumentid']}", "opdateringsdato": STAMP} for item in links]
        votes = [{"id": 31, "sagstrinid": 10, "nummer": 1, "vedtaget": True, "opdateringsdato": STAMP}]

        def scan(collection: str, *, filter: str) -> ScanResult:
            if collection == failing_collection:
                partial_rows = stages if collection == "Sagstrin" else []
                partial = successful_scan(partial_rows, page_count=1 if partial_rows else 0)
                raise ODataPageError(2, replace(partial, metadata=replace(partial.metadata, complete=False)), "HTTP 503")
            if collection == "Sagstrin":
                selected = stages
            elif collection == "SagDokument":
                selected = links
            elif collection == "Dokument":
                ids = {int(value) for value in re.findall(r"id eq (\d+)", filter)}
                selected = [item for item in docs if item["id"] in ids]
            elif collection == "Afstemning":
                selected = votes
            else:
                selected = []
            return successful_scan(selected, page_count=2 if collection == "Sagstrin" else 1)

        client.scan.side_effect = scan
        return client

    def test_targeted_fetch_loads_related_records_and_serializes_case(self) -> None:
        client = self.make_client()
        result = load_case(client, 1)
        self.assertTrue(result.complete)
        self.assertEqual(result.case.source, "folketinget")  # type: ignore[union-attr]
        self.assertEqual(result.case.status.name, "Vedtaget")  # type: ignore[union-attr]
        self.assertEqual(result.case.stages[0].status_id, 77)  # type: ignore[union-attr]
        self.assertEqual(result.case.documents[0].link_id, 100)  # type: ignore[union-attr]
        self.assertEqual(result.case.votes[0].stage_id, 10)  # type: ignore[union-attr]
        payload = result.case.to_dict()  # type: ignore[union-attr]
        self.assertEqual(payload["source_url"], f"{ODA_ROOT}Sag(1)")
        self.assertEqual(payload["type_id"], 3)
        self.assertIn('"source": "folketinget"', json.dumps(payload, ensure_ascii=False))

    def test_related_scan_result_can_contain_multiple_pages(self) -> None:
        client = self.make_client()
        previous_scan = client.scan.side_effect

        def with_two_stages(collection: str, *, filter: str) -> ScanResult:
            if collection == "Sagstrin":
                return successful_scan([
                    {"id": 10, "sagid": 1, "opdateringsdato": STAMP},
                    {"id": 11, "sagid": 1, "opdateringsdato": "2026-10-15T00:00:00"},
                ], page_count=2)
            return previous_scan(collection, filter=filter)

        client.scan.side_effect = with_two_stages
        result = load_case(client, 1)
        self.assertEqual([item.id for item in result.case.stages], [10, 11])  # type: ignore[union-attr]
        self.assertIn("sagid eq 1", [call.kwargs["filter"] for call in client.scan.call_args_list])

    def test_failed_related_scan_keeps_partial_rows_and_marks_case_incomplete(self) -> None:
        client = self.make_client(failing_collection="Sagstrin")
        result = load_case(client, 1)
        self.assertIsNotNone(result.case)
        self.assertFalse(result.complete)
        self.assertFalse(result.case.complete)  # type: ignore[union-attr]
        self.assertIn("related_page_failed", [item.code for item in result.diagnostics])

    def test_failed_document_scan_does_not_claim_link_target_is_missing(self) -> None:
        client = self.make_client(failing_collection="Dokument")
        result = load_case(client, 1)
        self.assertFalse(result.complete)
        self.assertIn("related_page_failed", [item.code for item in result.diagnostics])
        self.assertNotIn("missing_document", [item.code for item in result.diagnostics])

    def test_targeted_queries_are_bounded_and_use_id_filters(self) -> None:
        client = self.make_client(doc_count=51)
        result = load_case(client, 1)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.case.documents), 51)  # type: ignore[union-attr]
        self.assertEqual(client.fetch_page.call_args_list[0].args[0], "Sag")
        for call in client.fetch_page.call_args_list:
            self.assertGreaterEqual(call.args[1]["$top"], 1)
            self.assertIn("$filter", call.args[1])
        document_filters = [call.kwargs["filter"] for call in client.scan.call_args_list if call.args[0] == "Dokument"]
        self.assertEqual(len(document_filters), 2)
        self.assertTrue(all(len(re.findall(r"id eq \d+", item)) <= 50 for item in document_filters))
        self.assertTrue(all(call.kwargs.get("filter") for call in client.scan.call_args_list))

    def test_failed_status_request_does_not_mark_partial_case_complete(self) -> None:
        client = self.make_client()
        original = client.fetch_page.side_effect

        def fail_status(collection: str, params: dict[str, object]) -> list[dict[str, object]]:
            if collection == "Sagsstatus":
                from collectors.folketinget import ODataHTTPError
                raise ODataHTTPError("HTTP 503")
            return original(collection, params)

        client.fetch_page.side_effect = fail_status
        result = load_case(client, 1)
        self.assertIsNotNone(result.case)
        self.assertFalse(result.complete)
        self.assertEqual(result.case.status.id, 25)  # type: ignore[union-attr]
        self.assertIsNone(result.case.status.name)  # type: ignore[union-attr]
        self.assertIn("status_fetch_failed", [item.code for item in result.diagnostics])

    def test_missing_requested_case_is_not_returned_as_an_empty_success(self) -> None:
        client = self.make_client()
        client.fetch_page.side_effect = lambda collection, params: []
        result = load_case(client, 456)
        self.assertIsNone(result.case)
        self.assertFalse(result.complete)
        self.assertIn("case_not_found", [item.code for item in result.diagnostics])


if __name__ == "__main__":
    unittest.main()
