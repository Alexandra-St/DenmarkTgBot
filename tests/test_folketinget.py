from __future__ import annotations

import io
import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

from collectors.folketinget import (
    COLLECTIONS,
    ODA_ROOT,
    ODataClient,
    ODataCursorError,
    ODataHTTPError,
    ODataJSONError,
    ODataNetworkError,
    ODataPageError,
    ODataPaginationError,
    ODataStructureError,
    build_url,
)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, url: str | None = None) -> None:
        self.body = body
        self.status = status
        self.url = url or ODA_ROOT + "Sag"

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.url

    def read(self) -> bytes:
        return self.body


def response(rows: list[dict[str, object]]) -> FakeResponse:
    return FakeResponse(json.dumps({"odata.metadata": ODA_ROOT + "$metadata#Sag", "value": rows}).encode())


def row(entity_id: int, timestamp: str, title: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"id": entity_id, "opdateringsdato": timestamp}
    if title is not None:
        result["titel"] = title
    return result


T1 = "2026-10-14T08:46:08.6566667"
T2 = "2026-10-14T08:46:08.6566668"


class URLAndTransportTests(unittest.TestCase):
    def test_valid_odata_json_is_returned_as_raw_entities(self) -> None:
        entity = row(7, T1, "Forslag om ændringer")
        client = ODataClient(opener=Mock(return_value=response([entity])))
        self.assertEqual(client.fetch_page("Sag", {"$top": 1}), [entity])

    def test_empty_value_array_is_a_valid_empty_page(self) -> None:
        client = ODataClient(opener=Mock(return_value=response([])))
        self.assertEqual(client.fetch_page("Sag", {"$top": 1}), [])

    def test_query_parameters_are_encoded_and_bounded(self) -> None:
        url = build_url("Sag", {"$filter": "titel eq 'A & B'", "$select": "id,titel", "$top": 3})
        self.assertEqual(urlparse(url).scheme, "https")
        self.assertEqual(urlparse(url).netloc, "oda.ft.dk")
        self.assertEqual(parse_qs(urlparse(url).query)["$filter"], ["titel eq 'A & B'"])
        self.assertNotIn("A & B", url)
        self.assertIn("%24filter", url)

    def test_only_allowlisted_collections_and_query_options_are_supported(self) -> None:
        self.assertEqual(COLLECTIONS, {"Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus"})
        for collection in COLLECTIONS:
            self.assertTrue(build_url(collection, {"$top": 1}).startswith(ODA_ROOT + collection))
        with self.assertRaisesRegex(ValueError, "unsupported"):
            build_url("https://example.org/data", {"$top": 1})
        with self.assertRaisesRegex(ValueError, "unsupported"):
            build_url("Sag", {"$top": 1, "$expand": "anything"})
        with self.assertRaisesRegex(ValueError, "required"):
            build_url("Sag", {"$select": "id"})
        with self.assertRaises(ValueError):
            build_url("Sag", {"$top": 0})

    def test_transport_sets_accept_user_agent_and_timeout(self) -> None:
        opener = Mock(return_value=response([row(1, T1)]))
        client = ODataClient(timeout=4.5, opener=opener)
        client.fetch_page("Sag", {"$top": 1})
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("User-agent"), "DenmarkTgBot-FolketingetCollector/1.0")
        self.assertEqual(opener.call_args.kwargs["timeout"], 4.5)

    def test_non_https_or_non_official_response_redirect_is_rejected(self) -> None:
        opener = Mock(return_value=FakeResponse(b"{}", url="http://oda.ft.dk/api/Sag"))
        with self.assertRaisesRegex(ODataHTTPError, "redirect"):
            ODataClient(opener=opener).fetch_page("Sag", {"$top": 1})

    def test_retries_429_and_5xx_with_bounded_backoff(self) -> None:
        opener = Mock(side_effect=[
            HTTPError("url", 429, "busy", {}, io.BytesIO(b"secret body")),
            FakeResponse(b"", status=503),
            response([row(1, T1)]),
        ])
        sleep = Mock()
        client = ODataClient(opener=opener, sleep=sleep)
        self.assertEqual(client.fetch_page("Sag", {"$top": 1}), [row(1, T1)])
        self.assertEqual(opener.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.5])

    def test_permanent_http_error_is_not_retried_or_exposed(self) -> None:
        opener = Mock(side_effect=HTTPError("url", 404, "not found", {}, io.BytesIO(b"private data")))
        with self.assertRaises(ODataHTTPError) as error:
            ODataClient(opener=opener).fetch_page("Sag", {"$top": 1})
        self.assertIn("404", str(error.exception))
        self.assertNotIn("private data", str(error.exception))
        opener.assert_called_once()

    def test_timeout_and_network_errors_are_retried_a_bounded_number_of_times(self) -> None:
        opener = Mock(side_effect=[TimeoutError("slow"), URLError("offline"), TimeoutError("slow")])
        sleep = Mock()
        with self.assertRaises(ODataNetworkError):
            ODataClient(retries=2, opener=opener, sleep=sleep).fetch_page("Sag", {"$top": 1})
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_invalid_json_missing_value_wrong_row_and_empty_body_fail(self) -> None:
        with self.assertRaises(ODataJSONError):
            ODataClient(opener=Mock(return_value=FakeResponse(b"not-json"))).fetch_page("Sag", {"$top": 1})
        with self.assertRaises(ODataStructureError):
            ODataClient(opener=Mock(return_value=FakeResponse(b'{"d":[]}'))).fetch_page("Sag", {"$top": 1})
        with self.assertRaises(ODataStructureError):
            ODataClient(opener=Mock(return_value=FakeResponse(b'{"value":[1]}'))).fetch_page("Sag", {"$top": 1})
        with self.assertRaises(ODataStructureError):
            ODataClient(opener=Mock(return_value=FakeResponse(b""))).fetch_page("Sag", {"$top": 1})


class PaginationTests(unittest.TestCase):
    def test_keyset_pages_keep_timestamp_ties_and_seven_digit_precision(self) -> None:
        high = row(4, T1)
        first = [row(1, T1, "å"), row(2, T1, "æ")]
        second = [row(3, T1, "ø"), high]
        opener = Mock(side_effect=[response([high]), response(first), response(second)])
        result = ODataClient(page_size=2, opener=opener).scan("Sag")
        self.assertEqual([entity["id"] for entity in result.records], [1, 2, 3, 4])
        self.assertEqual(result.records[0]["opdateringsdato"], T1)
        self.assertEqual(result.metadata.upper_bound.timestamp, T1)  # type: ignore[union-attr]
        self.assertEqual(result.metadata.last_successful_cursor.entity_id, 4)  # type: ignore[union-attr]
        self.assertEqual(result.metadata.page_count, 2)
        self.assertTrue(result.metadata.complete)
        keyset = parse_qs(urlparse(opener.call_args_list[2].args[0].full_url).query)["$filter"][0]
        self.assertIn("opdateringsdato gt datetime'" + T1 + "'", keyset)
        self.assertIn("opdateringsdato eq datetime'" + T1 + "' and id gt 2", keyset)

    def test_timestamp_precision_equivalent_forms_compare_without_rewriting(self) -> None:
        a = "2026-10-14T08:46:08.9"
        b = "2026-10-14T08:46:08.9000000"
        opener = Mock(side_effect=[response([row(2, b)]), response([row(1, a), row(2, b)])])
        result = ODataClient(page_size=3, opener=opener).scan("Sag")
        self.assertEqual([item["opdateringsdato"] for item in result.records], [a, b])

    def test_same_entity_seen_again_with_newer_timestamp_emits_only_latest_record(self) -> None:
        opener = Mock(side_effect=[
            response([row(9, T2)]),
            response([row(9, T1, "earlier")]),
            response([row(9, T2, "latest")]),
        ])
        result = ODataClient(page_size=1, opener=opener).scan("Sag")
        self.assertTrue(result.metadata.complete)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.metadata.record_count, 2)
        self.assertEqual(result.records[0]["titel"], "latest")

    def test_upper_bound_uses_timestamp_and_id_and_excludes_later_tie(self) -> None:
        high = row(8, T1)
        opener = Mock(side_effect=[response([high]), response([row(7, T1), high])])
        result = ODataClient(page_size=10, opener=opener).scan("Sag")
        self.assertEqual([item["id"] for item in result.records], [7, 8])
        filter_value = parse_qs(urlparse(opener.call_args_list[1].args[0].full_url).query)["$filter"][0]
        self.assertIn("id le 8", filter_value)
        self.assertTrue(result.metadata.complete)

    def test_empty_collection_is_a_completed_empty_scan(self) -> None:
        opener = Mock(return_value=response([]))
        result = ODataClient(opener=opener).scan("Dokument")
        self.assertEqual(result.records, ())
        self.assertEqual(result.metadata.page_count, 0)
        self.assertTrue(result.metadata.complete)

    def test_short_page_does_not_end_scan_before_bounded_range_is_exhausted(self) -> None:
        opener = Mock(side_effect=[response([row(3, T2)]), response([row(1, T1)]), response([])])
        result = ODataClient(page_size=3, opener=opener).scan("Sag")
        self.assertEqual([item["id"] for item in result.records], [1])
        self.assertEqual(result.metadata.page_count, 1)
        self.assertEqual(opener.call_count, 3)
        self.assertTrue(result.metadata.complete)

    def test_exact_page_size_boundary_requests_next_page_when_upper_not_reached(self) -> None:
        opener = Mock(side_effect=[
            response([row(4, T1)]),
            response([row(1, T1), row(2, T1)]),
            response([]),
        ])
        result = ODataClient(page_size=2, opener=opener).scan("Sag")
        self.assertEqual([item["id"] for item in result.records], [1, 2])
        self.assertEqual(result.metadata.page_count, 1)
        self.assertEqual(opener.call_count, 3)

    def test_scan_respects_optional_filter_and_raw_selected_fields(self) -> None:
        entity = row(3, T1, "Forslag")
        opener = Mock(side_effect=[response([row(3, T1)]), response([entity])])
        result = ODataClient(opener=opener).scan("Sag", filter="typeid eq 2", select="id,opdateringsdato,titel")
        self.assertEqual(result.records, (entity,))
        query = parse_qs(urlparse(opener.call_args_list[1].args[0].full_url).query)
        self.assertIn("typeid eq 2", query["$filter"][0])
        self.assertEqual(query["$select"], ["id,opdateringsdato,titel"])
        with self.assertRaisesRegex(ValueError, "include id"):
            ODataClient(opener=Mock()).scan("Sag", select="titel")

    def test_nonadvancing_cursor_and_out_of_order_page_fail(self) -> None:
        opener = Mock(side_effect=[response([row(3, T1)]), response([row(1, T1), row(1, T1)])])
        with self.assertRaises(ODataPaginationError):
            ODataClient(opener=opener).scan("Sag")
        opener = Mock(side_effect=[response([row(3, T1)]), response([row(2, T1), row(1, T1)])])
        with self.assertRaises(ODataPaginationError):
            ODataClient(opener=opener).scan("Sag")

    def test_malformed_timestamp_or_id_fails_as_cursor_error(self) -> None:
        for bad in ("not-a-date", "2026-10-14T08:46:08.12345678", "2026-02-31T00:00:00"):
            opener = Mock(side_effect=[response([row(1, bad)])])
            with self.subTest(timestamp=bad), self.assertRaises(ODataCursorError):
                ODataClient(opener=opener).scan("Sag")
        opener = Mock(side_effect=[response([{"id": "one", "opdateringsdato": T1}])])
        with self.assertRaises(ODataCursorError):
            ODataClient(opener=opener).scan("Sag")

    def test_page_failure_exposes_incomplete_partial_result_not_success(self) -> None:
        opener = Mock(side_effect=[response([row(2, T2)]), response([row(1, T1)]), TimeoutError("offline")])
        with self.assertRaises(ODataPageError) as raised:
            ODataClient(page_size=1, opener=opener, retries=0).scan("Sag")
        partial = raised.exception.partial_result
        self.assertFalse(partial.metadata.complete)
        self.assertEqual(partial.metadata.page_count, 1)
        self.assertEqual(partial.metadata.last_successful_cursor.entity_id, 1)  # type: ignore[union-attr]
        self.assertEqual([item["id"] for item in partial.records], [1])

    def test_page_response_above_upper_bound_fails(self) -> None:
        opener = Mock(side_effect=[response([row(1, T1)]), response([row(2, T2)])])
        with self.assertRaises(ODataPaginationError):
            ODataClient(opener=opener).scan("Sag")

    def test_output_order_is_deterministic_by_source_timestamp_then_id(self) -> None:
        high = row(9, T2)
        opener = Mock(side_effect=[response([high]), response([row(3, T1), row(9, T2)])])
        result = ODataClient(opener=opener).scan("Sag")
        self.assertEqual([item["id"] for item in result.records], [3, 9])


if __name__ == "__main__":
    unittest.main()
