from __future__ import annotations

import json
import unittest
from pathlib import Path

from collectors.folketinget_relationship_summary import compact_folketinget_record, relationship_summary


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "folketinget_relationship_summary.json").read_text(encoding="utf-8")
)


class FolketingetRelationshipSummaryTests(unittest.TestCase):
    def test_shared_fixture_hashes_and_order_are_deterministic(self) -> None:
        for kind, rows in FIXTURE["relationships"].items():
            self.assertEqual(relationship_summary(rows, kind), FIXTURE["expected"][kind])
            self.assertEqual(relationship_summary(list(reversed(rows)), kind), FIXTURE["expected"][kind])

    def test_source_timestamps_are_excluded_and_semantic_changes_are_detected(self) -> None:
        rows = FIXTURE["relationships"]["documents"]
        timestamp_only = [{**row, "source_updated_at_raw": "2099-01-01T00:00:00"} for row in rows]
        changed = [{**row, "title": "Changed"} if index == 0 else row for index, row in enumerate(rows)]
        self.assertEqual(relationship_summary(timestamp_only, "documents"), FIXTURE["expected"]["documents"])
        self.assertNotEqual(relationship_summary(changed, "documents"), FIXTURE["expected"]["documents"])
        self.assertEqual(relationship_summary([], "documents")["count"], 0)

    def test_compaction_replaces_full_arrays_after_preparation(self) -> None:
        record = {"normalized_snapshot": {"title": "Private", **FIXTURE["relationships"]}}
        compact = compact_folketinget_record(record)
        snapshot = compact["normalized_snapshot"]
        self.assertEqual(snapshot["relationship_summary"], FIXTURE["expected"])
        self.assertTrue(all(kind not in snapshot for kind in ("documents", "stages", "votes")))
        self.assertIn("documents", record["normalized_snapshot"])

    def test_compaction_is_idempotent_for_valid_compact_records(self) -> None:
        record = {"normalized_snapshot": {"title": "Private", "relationship_summary": FIXTURE["expected"]}}
        self.assertEqual(compact_folketinget_record(record), record)

    def test_compaction_rejects_malformed_or_mixed_summaries(self) -> None:
        malformed = {"normalized_snapshot": {"relationship_summary": {}}}
        mixed = {"normalized_snapshot": {
            "relationship_summary": FIXTURE["expected"], "documents": [], "stages": [], "votes": []}}
        with self.assertRaisesRegex(ValueError, "relationship_summary_invalid"):
            compact_folketinget_record(malformed)
        with self.assertRaisesRegex(ValueError, "relationship_summary_invalid"):
            compact_folketinget_record(mixed)


if __name__ == "__main__":
    unittest.main()
