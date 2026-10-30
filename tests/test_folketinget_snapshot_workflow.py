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
        title: str = FLOW.PRODUCTION_TITLE, event: str = "workflow_dispatch",
        status: str = "completed", head_branch: str = "main", head_sha: str = "a" * 40) -> dict[str, object]:
    return {"id": run_id, "run_number": number, "workflow_id": workflow_id,
            "status": status, "conclusion": conclusion, "display_title": title,
            "event": event, "head_branch": head_branch, "head_sha": head_sha}


class FakeApi:
    def __init__(self, runs: list[dict[str, object]], bootstrap: dict[str, object],
                 artifacts: dict[str, list[dict[str, object]]]) -> None:
        self.runs = runs
        self.bootstrap = bootstrap
        self.artifact_map = artifacts
        self.bootstrap_run_calls = 0

    def workflow_id(self, workflow: str) -> int:
        return 10 if workflow == FLOW.POC_WORKFLOW else 20

    def recent_runs(self, workflow_id: int) -> list[dict[str, object]]:
        return self.runs

    def run(self, run_id: str) -> dict[str, object]:
        self.bootstrap_run_calls += 1
        return self.bootstrap

    def artifacts(self, run_id: str) -> list[dict[str, object]]:
        return self.artifact_map.get(run_id, [])


class SnapshotWorkflowSelectionTests(unittest.TestCase):
    def test_explicit_recovery_base_validates_production_run_and_artifact(self) -> None:
        recovery_run_id = "40304681498"
        recovery_run = run(int(recovery_run_id), number=13)
        selected = FLOW.select_explicit_recovery_base(
            FakeApi([], recovery_run,
                   {recovery_run_id: artifact(f"folketinget-snapshot-incremental-{recovery_run_id}")}),
            recovery_run_id,
        )
        self.assertEqual(selected, (recovery_run_id, f"folketinget-snapshot-incremental-{recovery_run_id}"))

    def test_explicit_recovery_base_rejects_wrong_or_failed_run(self) -> None:
        for candidate in (
            run(12, number=12, conclusion="failure"),
            run(12, number=12, event="push"),
        ):
            with self.subTest(candidate=candidate), self.assertRaises(FLOW.WorkflowError):
                FLOW.select_explicit_recovery_base(FakeApi([], candidate, {"12": artifact(
                    "folketinget-snapshot-incremental-12")}), "12")

    def test_explicit_recovery_base_rejects_wrong_branch_title_or_workflow(self) -> None:
        candidates = (
            run(12, number=12, head_branch="feature"),
            run(12, number=12, title="other workflow"),
            run(12, number=12, workflow_id=21),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(FLOW.WorkflowError):
                FLOW.select_explicit_recovery_base(FakeApi([], candidate, {"12": artifact(
                    "folketinget-snapshot-incremental-12")}), "12")

    def test_explicit_recovery_base_rejects_missing_expired_or_ambiguous_artifact(self) -> None:
        name = "folketinget-snapshot-incremental-12"
        cases = ([], artifact(name, expired=True), artifact(name) + artifact(name))
        for artifacts in cases:
            with self.subTest(artifacts=artifacts), self.assertRaises(FLOW.WorkflowError):
                FLOW.select_explicit_recovery_base(FakeApi([], run(12, number=12),
                    {"12": artifacts}), "12")

    def test_recovery_input_contract_rejects_partial_or_scheduled_overrides(self) -> None:
        self.assertTrue(FLOW.validate_recovery_inputs("workflow_dispatch", "true", "12"))
        self.assertFalse(FLOW.validate_recovery_inputs("workflow_dispatch", "false", ""))
        self.assertFalse(FLOW.validate_recovery_inputs("schedule", "false", ""))
        for event, confirmation, run_id, category in (
            ("workflow_dispatch", "false", "12", "recovery_confirmation_invalid"),
            ("workflow_dispatch", "true", "", "recovery_confirmation_invalid"),
            ("schedule", "true", "12", "recovery_override_not_allowed"),
            ("schedule", "false", "12", "recovery_override_not_allowed"),
        ):
            with self.subTest(event=event, confirmation=confirmation, run_id=run_id):
                with self.assertRaises(FLOW.WorkflowError) as error:
                    FLOW.validate_recovery_inputs(event, confirmation, run_id)
                self.assertEqual(error.exception.category, category)

    def test_select_base_defaults_to_existing_selector_unless_recovery_requested(self) -> None:
        workflow = (ROOT / ".github/workflows/folketinget-snapshot-incremental.yml").read_text()
        self.assertIn("select_explicit_recovery_base", (ROOT / "tools/folketinget_snapshot_workflow.py").read_text())
        self.assertIn("CONFIRM_RECOVERY_BASE", workflow)
        self.assertIn("RECOVERY_BASE_RUN_ID", workflow)

    def test_select_base_uses_bootstrap_when_no_production_runs_exist(self) -> None:
        api = FakeApi(
            [], run(90, number=9, workflow_id=10, title="CREATE_MANIFEST_BASELINE"),
            {"90": artifact("folketinget-snapshot-manifest-90")},
        )
        self.assertEqual(FLOW.select_base(api, "90"), ("90", "folketinget-snapshot-manifest-90"))

    def test_select_base_uses_bootstrap_after_only_safely_skipped_runs(self) -> None:
        incident = run(
            int(FLOW.RECOVERABLE_PREWRITE_RUN_ID), number=14, conclusion="failure",
            head_sha=FLOW.RECOVERABLE_PREWRITE_HEAD_SHA,
        )
        api = FakeApi(
            [run(15, number=15, event="schedule", conclusion="skipped"), incident],
            run(90, number=9, workflow_id=10, title="CREATE_MANIFEST_BASELINE"),
            {"90": artifact("folketinget-snapshot-manifest-90")},
        )
        self.assertEqual(FLOW.select_base(api, "90"), ("90", "folketinget-snapshot-manifest-90"))
        self.assertEqual(api.bootstrap_run_calls, 1)

    def test_select_base_prefers_valid_production_manifest_over_bootstrap(self) -> None:
        api = FakeApi(
            [run(15, number=15)],
            run(90, number=9, workflow_id=10, title="CREATE_MANIFEST_BASELINE"),
            {"15": artifact("folketinget-snapshot-incremental-15"),
             "90": artifact("folketinget-snapshot-manifest-90")},
        )
        self.assertEqual(FLOW.select_base(api, "90"), ("15", "folketinget-snapshot-incremental-15"))
        self.assertEqual(api.bootstrap_run_calls, 0)

    def test_select_base_ambiguous_run_blocks_without_loading_bootstrap(self) -> None:
        api = FakeApi(
            [run(15, number=15, conclusion="failure")],
            run(90, number=9, workflow_id=10, title="CREATE_MANIFEST_BASELINE"),
            {"90": artifact("folketinget-snapshot-manifest-90")},
        )
        with self.assertRaises(FLOW.WorkflowError) as error:
            FLOW.select_base(api, "90")
        self.assertEqual(error.exception.category, "ambiguous_previous_production_run")
        self.assertEqual(api.bootstrap_run_calls, 0)

    def test_select_base_rejects_missing_or_invalid_bootstrap_after_safe_skips(self) -> None:
        skipped = run(15, number=15, event="schedule", conclusion="skipped")
        invalid_bootstrap = run(90, number=9, workflow_id=10, title="CREATE_MANIFEST_BASELINE",
                                conclusion="cancelled")
        api = FakeApi([skipped], invalid_bootstrap, {"90": artifact("folketinget-snapshot-manifest-90")})
        with self.assertRaises(FLOW.WorkflowError) as error:
            FLOW.select_base(api, "90")
        self.assertEqual(error.exception.category, "manifest_run_not_successful")

        missing_artifact = FakeApi(
            [skipped], run(90, number=9, workflow_id=10, title="CREATE_MANIFEST_BASELINE"), {"90": []}
        )
        with self.assertRaises(FLOW.WorkflowError) as error:
            FLOW.select_base(missing_artifact, "90")
        self.assertEqual(error.exception.category, "manifest_artifact_missing_or_ambiguous")

    def test_production_branch_guard_allows_dry_run_but_requires_main_for_writes(self) -> None:
        FLOW.validate_production_ref("workflow_dispatch", "dry_run", "refs/heads/feature")
        FLOW.validate_production_ref("workflow_dispatch", "production_ingest", "refs/heads/main")
        FLOW.validate_production_ref("schedule", "", "refs/heads/main")
        for event, mode in (("workflow_dispatch", "production_ingest"), ("schedule", "")):
            with self.subTest(event=event, mode=mode), self.assertRaises(FLOW.WorkflowError) as error:
                FLOW.validate_production_ref(event, mode, "refs/heads/feature")
            self.assertEqual(error.exception.category, "production_branch_invalid")

    def test_missing_base_artifact_fails_closed(self) -> None:
        with self.assertRaises(FLOW.WorkflowError) as error:
            FLOW.validate_artifact([], "folketinget-snapshot-manifest-11")
        self.assertEqual(error.exception.category, "manifest_artifact_missing_or_ambiguous")

    def test_expired_bootstrap_artifact_fails_closed(self) -> None:
        with self.assertRaises(FLOW.WorkflowError) as error:
            FLOW.validate_artifact(artifact("folketinget-snapshot-manifest-11", expired=True),
                                   "folketinget-snapshot-manifest-11")
        self.assertEqual(error.exception.category, "manifest_artifact_expired")

    def test_failed_committed_run_manifest_is_selected(self) -> None:
        failed = run(12, number=12, conclusion="failure")
        previous_success = run(11, number=11)
        selected = FLOW.choose_base(
            bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
            poc_workflow_id=10, incremental_runs=[failed, previous_success], incremental_workflow_id=20,
            artifact_lookup=lambda chosen: artifact(f"folketinget-snapshot-incremental-{chosen}"),
        )
        self.assertEqual(selected, ("12", "folketinget-snapshot-incremental-12"))

    def test_failed_or_cancelled_run_without_durable_proof_blocks_fallback(self) -> None:
        for conclusion in ("failure", "cancelled"):
            with self.subTest(conclusion=conclusion), self.assertRaises(FLOW.WorkflowError) as error:
                FLOW.choose_base(
                    bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
                    poc_workflow_id=10,
                    incremental_runs=[run(13, number=13, conclusion=conclusion), run(12, number=12)],
                    incremental_workflow_id=20,
                    artifact_lookup=lambda chosen: [] if chosen == "13" else artifact(
                        f"folketinget-snapshot-incremental-{chosen}"),
                )
            self.assertEqual(error.exception.category, "ambiguous_previous_production_run")

    def test_gated_scheduled_skip_without_artifact_skips_to_previous_base(self) -> None:
        skipped_schedule = run(14, number=14, event="schedule", conclusion="skipped")
        selected = FLOW.choose_base(
            bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
            poc_workflow_id=10,
            incremental_runs=[skipped_schedule, run(13, number=13)],
            incremental_workflow_id=20,
            artifact_lookup=lambda chosen: [] if chosen == "14" else artifact(
                f"folketinget-snapshot-incremental-{chosen}"),
        )
        self.assertEqual(selected, ("13", "folketinget-snapshot-incremental-13"))

    def test_skipped_dispatch_and_failed_or_cancelled_schedule_remain_ambiguous(self) -> None:
        cases = (
            run(14, number=14, event="workflow_dispatch", conclusion="skipped"),
            run(14, number=14, event="schedule", conclusion="failure"),
            run(14, number=14, event="schedule", conclusion="cancelled"),
            run(14, number=14, event="schedule", conclusion="neutral"),
        )
        for candidate in cases:
            with self.subTest(event=candidate["event"], conclusion=candidate["conclusion"]):
                with self.assertRaises(FLOW.WorkflowError) as error:
                    FLOW.choose_base(
                        bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
                        poc_workflow_id=10,
                        incremental_runs=[candidate, run(13, number=13)],
                        incremental_workflow_id=20,
                        artifact_lookup=lambda chosen: [] if chosen == "14" else artifact(
                            f"folketinget-snapshot-incremental-{chosen}"),
                    )
                self.assertEqual(error.exception.category, "ambiguous_previous_production_run")

    def test_manifest_artifact_remains_authoritative_for_skipped_schedule_run(self) -> None:
        skipped_with_manifest = run(14, number=14, event="schedule", conclusion="skipped")
        selected = FLOW.choose_base(
            bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
            poc_workflow_id=10, incremental_runs=[skipped_with_manifest],
            incremental_workflow_id=20,
            artifact_lookup=lambda chosen: artifact(f"folketinget-snapshot-incremental-{chosen}"),
        )
        self.assertEqual(selected, ("14", "folketinget-snapshot-incremental-14"))

    def test_failed_no_commit_marker_safely_skips_to_committed_base(self) -> None:
        selected = FLOW.choose_base(
            bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
            poc_workflow_id=10,
            incremental_runs=[run(13, number=13, conclusion="failure"), run(12, number=12)],
            incremental_workflow_id=20,
            artifact_lookup=lambda chosen: (
                artifact(f"{FLOW.NO_COMMIT_PREFIX}{chosen}") if chosen == "13"
                else artifact(f"folketinget-snapshot-incremental-{chosen}")
            ),
        )
        self.assertEqual(selected, ("12", "folketinget-snapshot-incremental-12"))

    def test_exact_reviewed_prewrite_incident_safely_skips_to_committed_base(self) -> None:
        incident = run(
            int(FLOW.RECOVERABLE_PREWRITE_RUN_ID), number=14, conclusion="failure",
            head_sha=FLOW.RECOVERABLE_PREWRITE_HEAD_SHA,
        )
        selected = FLOW.choose_base(
            bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
            poc_workflow_id=10, incremental_runs=[incident, run(12, number=12)],
            incremental_workflow_id=20,
            artifact_lookup=lambda chosen: [] if chosen == FLOW.RECOVERABLE_PREWRITE_RUN_ID else artifact(
                f"folketinget-snapshot-incremental-{chosen}"),
        )
        self.assertEqual(selected, ("12", "folketinget-snapshot-incremental-12"))

    def test_prewrite_incident_recovery_requires_every_exact_property(self) -> None:
        exact = run(
            int(FLOW.RECOVERABLE_PREWRITE_RUN_ID), number=14, conclusion="failure",
            head_sha=FLOW.RECOVERABLE_PREWRITE_HEAD_SHA,
        )
        variants = {
            "id": {**exact, "id": int(FLOW.RECOVERABLE_PREWRITE_RUN_ID) + 1},
            "sha": {**exact, "head_sha": "b" * 40},
            "branch": {**exact, "head_branch": "feature"},
            "title": {**exact, "display_title": "different"},
            "conclusion": {**exact, "conclusion": "cancelled"},
        }
        for field, candidate in variants.items():
            with self.subTest(field=field), self.assertRaises(FLOW.WorkflowError) as error:
                FLOW.choose_base(
                    bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
                    poc_workflow_id=10, incremental_runs=[candidate, run(12, number=12)],
                    incremental_workflow_id=20,
                    artifact_lookup=lambda chosen: artifact(f"folketinget-snapshot-incremental-{chosen}")
                    if chosen == "12" else [],
                )
            self.assertEqual(error.exception.category, "ambiguous_previous_production_run")

    def test_current_in_progress_run_is_ignored(self) -> None:
        selected = FLOW.choose_base(
            bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
            poc_workflow_id=10,
            incremental_runs=[
                run(14, number=14, conclusion="", status="in_progress"),
                run(13, number=13),
            ],
            incremental_workflow_id=20,
            artifact_lookup=lambda chosen: artifact(f"folketinget-snapshot-incremental-{chosen}"),
            current_run_id="14",
        )
        self.assertEqual(selected, ("13", "folketinget-snapshot-incremental-13"))

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
        self.assertEqual(error.exception.category, "ambiguous_previous_production_run")

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

    def test_only_main_branch_production_artifacts_are_trusted(self) -> None:
        selected = FLOW.choose_base(
            bootstrap_run_id="90", bootstrap_run=None, bootstrap_artifacts=None,
            poc_workflow_id=10,
            incremental_runs=[run(13, number=13, head_branch="feature"), run(12, number=12)],
            incremental_workflow_id=20,
            artifact_lookup=lambda chosen: artifact(f"folketinget-snapshot-incremental-{chosen}"),
        )
        self.assertEqual(selected, ("12", "folketinget-snapshot-incremental-12"))

    def test_workflow_schedule_is_gated_and_artifact_promotion_is_post_commit_only(self) -> None:
        workflow = (ROOT / ".github/workflows/folketinget-snapshot-incremental.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("schedule:", workflow)
        self.assertIn("vars.FOLKETINGET_AUTOMATION_ENABLED == 'true'", workflow)
        self.assertLess(
            workflow.index("if: github.event_name != 'schedule' || vars.FOLKETINGET_AUTOMATION_ENABLED == 'true'"),
            workflow.index("    steps:"),
        )
        self.assertIn("inputs.mode == 'production_ingest'", workflow)
        self.assertIn('default: "39433055408"', workflow)
        self.assertIn("folketinget_snapshot_workflow.py validate-production-ref", workflow)
        helper = (ROOT / "tools/folketinget_snapshot_workflow.py").read_text()
        self.assertIn('os.environ.get("GITHUB_REF", "")', helper)
        self.assertIn('current_run_id=os.environ.get("GITHUB_RUN_ID") or None', helper)
        self.assertIn("steps.run-incremental.outputs.promote_manifest == 'true'", workflow)
        self.assertIn("actions/upload-artifact@v7", workflow)
        self.assertIn("retention-days: 30", workflow)
        self.assertIn("folketinget-snapshot-no-commit-${{ github.run_id }}", workflow)
        notification_step = workflow.split("- name: Queue and deliver notifications after snapshot advancement", 1)[1].split("- name: Promote target manifest", 1)[0]
        self.assertIn("github.event_name == 'schedule'", notification_step)
        self.assertIn("steps.run-incremental.outputs.promote_manifest == 'true'", notification_step)
        self.assertIn("collectors.notification_client", notification_step)
        self.assertIn("NOTIFICATION_QUEUE_AUTOMATION_ENABLED", notification_step)
        self.assertIn("NOTIFICATION_DELIVERY_AUTOMATION_ENABLED", notification_step)
        self.assertNotIn("worker deploy", workflow.lower())


if __name__ == "__main__":
    unittest.main()
