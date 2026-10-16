from __future__ import annotations

import unittest

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


STAMP = "2026-10-14T08:46:08.6566667"


class NormalizationTests(unittest.TestCase):
    def test_valid_sag_preserves_ids_type_category_and_danish_text(self) -> None:
        result = normalize_sag({
            "id": 106028, "nummer": "L 34", "titel": "Ændring af udlændingeloven",
            "typeid": 3, "kategoriid": 13, "statusid": 25,
            "opdateringsdato": STAMP, "retsinformationsurl": "https://example.test/lov",
        })
        self.assertTrue(result.complete)
        self.assertIsInstance(result.record, Sag)
        self.assertEqual(result.record.id, 106028)  # type: ignore[union-attr]
        self.assertEqual(result.record.typeid, 3)  # type: ignore[union-attr]
        self.assertEqual(result.record.kategoriid, 13)  # type: ignore[union-attr]
        self.assertEqual(result.record.titel, "Ændring af udlændingeloven")  # type: ignore[union-attr]

    def test_missing_optional_fields_are_null_not_invented(self) -> None:
        result = normalize_sag({"id": 1, "opdateringsdato": STAMP})
        self.assertTrue(result.complete)
        self.assertIsNone(result.record.nummer)  # type: ignore[union-attr]
        self.assertIsNone(result.record.titel)  # type: ignore[union-attr]
        self.assertIsNone(result.record.statusid)  # type: ignore[union-attr]
        self.assertIsNone(result.record.retsinformationsurl)  # type: ignore[union-attr]

    def test_invalid_required_id_is_rejected_with_diagnostic(self) -> None:
        result = normalize_sag({"id": "1", "opdateringsdato": STAMP})
        self.assertIsNone(result.record)
        self.assertFalse(result.complete)
        self.assertEqual(result.diagnostics[0].code, "invalid_identifier")

    def test_invalid_foreign_key_does_not_become_a_null_foreign_key(self) -> None:
        result = normalize_sag({"id": 1, "statusid": "bad", "opdateringsdato": STAMP})
        self.assertIsNone(result.record)
        self.assertEqual(result.diagnostics[0].code, "invalid_identifier")

    def test_nullable_foreign_keys_are_supported(self) -> None:
        result = normalize_afstemning({"id": 7, "sagstrinid": None, "opdateringsdato": STAMP})
        self.assertTrue(result.complete)
        self.assertIsInstance(result.record, Afstemning)
        self.assertIsNone(result.record.sagstrinid)  # type: ignore[union-attr]

    def test_offset_free_seven_digit_timestamp_is_preserved_and_not_called_utc(self) -> None:
        result = normalize_sag({"id": 1, "opdateringsdato": STAMP})
        self.assertEqual(result.record.opdateringsdato, STAMP)  # type: ignore[union-attr]
        self.assertIsNone(result.record.updated_at_utc)  # type: ignore[union-attr]
        self.assertTrue(result.complete)

    def test_offset_aware_timestamp_is_normalized_but_raw_value_remains(self) -> None:
        raw = "2026-10-14T08:46:08.6566667+02:00"
        result = normalize_sag({"id": 1, "opdateringsdato": raw})
        self.assertEqual(result.record.opdateringsdato, raw)  # type: ignore[union-attr]
        self.assertEqual(result.record.updated_at_utc, "2026-10-14T06:46:08.656666Z")  # type: ignore[union-attr]

    def test_missing_and_invalid_timestamps_are_reported_without_fake_utc(self) -> None:
        missing = normalize_sag({"id": 1})
        invalid = normalize_sag({"id": 2, "opdateringsdato": "not-a-date"})
        self.assertFalse(missing.complete)
        self.assertEqual(missing.diagnostics[0].code, "missing_timestamp")
        self.assertFalse(invalid.complete)
        self.assertEqual(invalid.record.opdateringsdato, "not-a-date")  # type: ignore[union-attr]
        self.assertIsNone(invalid.record.updated_at_utc)  # type: ignore[union-attr]

    def test_sagstrin_status_id_is_preserved_without_case_status_lookup(self) -> None:
        result = normalize_sagstrin({"id": 9, "sagid": 1, "statusid": 2, "opdateringsdato": STAMP})
        self.assertIsInstance(result.record, Sagstrin)
        self.assertEqual(result.record.statusid, 2)  # type: ignore[union-attr]

    def test_sagsstatus_without_label_is_incomplete(self) -> None:
        result = normalize_sagsstatus({"id": 25, "opdateringsdato": STAMP})
        self.assertFalse(result.complete)
        self.assertIsNone(result.record.status)  # type: ignore[union-attr]
        self.assertIn("missing_status_text", [item.code for item in result.diagnostics])

    def test_all_six_source_entity_models_keep_their_ids_and_raw_dates(self) -> None:
        cases = (
            (normalize_sagdokument({"id": 2, "sagid": 1, "dokumentid": 3, "frigivelsesdato": "2026-01-01T00:00:00", "opdateringsdato": STAMP}), SagDokument),
            (normalize_dokument({"id": 3, "dato": "2026-01-01T00:00:00", "modtagelsesdato": "2026-01-02T00:00:00", "opdateringsdato": STAMP}), Dokument),
            (normalize_sagsstatus({"id": 4, "status": "Vedtaget", "opdateringsdato": STAMP}), Sagsstatus),
        )
        for result, model_type in cases:
            with self.subTest(model=model_type.__name__):
                self.assertTrue(result.complete)
                self.assertIsInstance(result.record, model_type)
                self.assertEqual(result.record.opdateringsdato, STAMP)  # type: ignore[union-attr]

    def test_invalid_date_value_is_reported_and_source_text_is_not_normalized(self) -> None:
        result = normalize_dokument({"id": 3, "dato": "01/02/2026", "opdateringsdato": STAMP})
        self.assertEqual(result.record.dato, "01/02/2026")  # type: ignore[union-attr]
        self.assertFalse(result.complete)
        self.assertIn("invalid_source_date", [item.code for item in result.diagnostics])


if __name__ == "__main__":
    unittest.main()
