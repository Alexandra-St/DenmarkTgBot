from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "folketinget_snapshot_workflow", ROOT / "tools" / "folketinget_snapshot_workflow.py"
)
FLOW = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(FLOW)


def artifact(name: str, *, expired: bool = False, size: int = 123) -> list[dict[str, object]]:
    return [{"id": 17, "name": name, "expired": expired, "size_in_bytes": size}]


def run(run_id: int, *, number: int, workflow_id: int = 20, conclusion: str = "success",
        title: str = FLOW.PRODUCTION_TITLE, event: str = "workflow_dispatch") -> dict[str, object]:
    return {"id": run_id, "run_number": number, "workflow_id": workflow_id,
            "status": "completed", "conclusion": conclusion, "display_title": title, "event": event}


class SnapshotWorkflowSelectionTests(unittest.TestCase):
    def test_missing_base_artifact_fails_closed(self) -> None:
        with self.assertRaises(FLOW.WorkflowError) as error:
            FLOW.validate_artifact([], "folketinget-snapshot-manifest-11")
        self.assertEqual(error.exception.category, "manifest_artifact_missing_or_ambiguous")

    def test_expired_bootstrap_artifact_fails_closed(self) -> None:
        with self.assertRaises(FLOW.WorkflowError) as error:
            FLOW.validate_artifact(artifact("folketinget-snapshot-manifest-11", expired=True),
                                   "folketinget-snapshot-manifest-11")
        self.assertEqual(error.exception.category, "manifest_artifact_expired")

    def test_failed_previous_run_is_never_selected(self) -> None:
        failed = run(12, number=12, conclusion="failure")
        previous_success = run(11, number=11)
        selected = FLOW.choose_base(
            bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
            poc_workflow_id=10, incremental_runs=[failed, previous_success], incremental_workflow_id=20,
            artifact_lookup=lambda chosen: artifact(f"folketinget-snapshot-incremental-{chosen}"),
        )
        self.assertEqual(selected, ("11", "folketinget-snapshot-incremental-11"))

    def test_latest_successful_production_artifact_is_required_even_if_expired(self) -> None:
        latest = run(12, number=12)
        with self.assertRaises(FLOW.WorkflowError) as error:
            FLOW.choose_base(
                bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
                poc_workflow_id=10, incremental_runs=[latest], incremental_workflow_id=20,
                artifact_lookup=lambda chosen: artifact(f"folketinget-snapshot-incremental-{chosen}", expired=True),
            )
        self.assertEqual(error.exception.category, "manifest_artifact_expired")

    def test_successful_no_commit_run_does_not_advance_base(self) -> None:
        latest_noop = run(13, number=13)
        prior_commit = run(12, number=12)
        selected = FLOW.choose_base(
            bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
            poc_workflow_id=10, incremental_runs=[latest_noop, prior_commit], incremental_workflow_id=20,
            artifact_lookup=lambda chosen: (
                artifact(f"{FLOW.NO_COMMIT_PREFIX}{chosen}") if chosen == "13"
                else artifact(f"folketinget-snapshot-incremental-{chosen}")
            ),
        )
        self.assertEqual(selected, ("12", "folketinget-snapshot-incremental-12"))

    def test_successful_run_without_promotion_proof_is_not_skipped(self) -> None:
        latest = run(13, number=13)
        prior_commit = run(12, number=12)
        with self.assertRaises(FLOW.WorkflowError) as error:
            FLOW.choose_base(
                bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
                poc_workflow_id=10, incremental_runs=[latest, prior_commit], incremental_workflow_id=20,
                artifact_lookup=lambda chosen: [] if chosen == "13" else artifact(
                    f"folketinget-snapshot-incremental-{chosen}"),
            )
        self.assertEqual(error.exception.category, "manifest_artifact_missing_or_ambiguous")

    def test_bootstrap_only_after_successful_exact_poc_run(self) -> None:
        successful = run(90, number=9, workflow_id=10, title="CREATE_MANIFEST_BASELINE")
        chosen = FLOW.choose_base(
            bootstrap_run_id="90", bootstrap_run=successful,
            bootstrap_artifacts=artifact("folketinget-snapshot-manifest-90"),
            poc_workflow_id=10, incremental_runs=[], incremental_workflow_id=20,
            artifact_lookup=lambda _: [],
        )
        self.assertEqual(chosen[0], "90")
        failed = {**successful, "conclusion": "cancelled"}
        with self.assertRaises(FLOW.WorkflowError):
            FLOW.choose_base(
                bootstrap_run_id="90", bootstrap_run=failed,
                bootstrap_artifacts=artifact("folketinget-snapshot-manifest-90"),
                poc_workflow_id=10, incremental_runs=[], incremental_workflow_id=20,
                artifact_lookup=lambda _: [],
            )

    def test_incremental_predecessor_takes_precedence_over_supplied_bootstrap(self) -> None:
        selected = FLOW.choose_base(
            bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
            poc_workflow_id=10, incremental_runs=[run(12, number=12)], incremental_workflow_id=20,
            artifact_lookup=lambda chosen: artifact(f"folketinget-snapshot-incremental-{chosen}"),
        )
        self.assertEqual(selected, ("12", "folketinget-snapshot-incremental-12"))

    def test_workflow_schedule_is_gated_and_artifact_promotion_is_post_commit_only(self) -> None:
        workflow = (ROOT / ".github/workflows/folketinget-snapshot-incremental.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("schedule:", workflow)
        self.assertIn("vars.FOLKETINGET_AUTOMATION_ENABLED == 'true'", workflow)
        self.assertIn("inputs.mode == 'production_ingest'", workflow)
        self.assertIn("steps.run-incremental.outputs.promote_manifest == 'true'", workflow)
        self.assertIn("actions/upload-artifact@v7", workflow)
        self.assertIn("retention-days: 30", workflow)
        self.assertIn("folketinget-snapshot-no-commit-${{ github.run_id }}", workflow)
        self.assertNotIn("notification", workflow.lower())
        self.assertNotIn("worker deploy", workflow.lower())


if __name__ == "__main__":
    unittest.main()
