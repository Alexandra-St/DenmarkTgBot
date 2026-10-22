"""Fail-closed GitHub artifact selection for Folketinget snapshot runs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any


API = "https://api.github.com"
POC_WORKFLOW = "folketinget-backup-poc.yml"
INCREMENTAL_WORKFLOW = "folketinget-snapshot-incremental.yml"
PRODUCTION_TITLE = "Folketinget snapshot incremental / production_ingest"
NO_COMMIT_PREFIX = "folketinget-snapshot-no-commit-"
USER_AGENT = "DenmarkTgBot-SnapshotWorkflow/1.0"
RECOVERABLE_PREWRITE_RUN_ID = "39683128314"
RECOVERABLE_PREWRITE_HEAD_SHA = "830ce7ccdd3ae532b6519d13d58be948eb7f2625"


class WorkflowError(Exception):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def validate_production_ref(event_name: str, mode: str, git_ref: str) -> None:
    """Allow production snapshot ingestion only from the main branch ref."""
    production = event_name == "schedule" or (
        event_name == "workflow_dispatch" and mode == "production_ingest"
    )
    if production and git_ref != "refs/heads/main":
        raise WorkflowError("production_branch_invalid")


def validate_artifact(artifacts: object, expected_name: str) -> dict[str, Any]:
    if not isinstance(artifacts, list):
        raise WorkflowError("manifest_artifact_missing_or_ambiguous")
    matches = [item for item in artifacts if isinstance(item, dict) and item.get("name") == expected_name]
    if len(matches) != 1:
        raise WorkflowError("manifest_artifact_missing_or_ambiguous")
    artifact = matches[0]
    if artifact.get("expired") is True:
        raise WorkflowError("manifest_artifact_expired")
    if artifact.get("expired") is not False:
        raise WorkflowError("manifest_artifact_missing_or_ambiguous")
    size = artifact.get("size_in_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise WorkflowError("manifest_artifact_missing_or_ambiguous")
    artifact_id = artifact.get("id")
    if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id <= 0:
        raise WorkflowError("manifest_artifact_missing_or_ambiguous")
    return artifact


def validate_run(run: object, *, requested_id: str, workflow_id: int,
                 allowed_events: set[str], expected_title: str | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9]+", requested_id or "") or not isinstance(run, dict):
        raise WorkflowError("manifest_run_not_successful")
    if (str(run.get("id")) != requested_id or run.get("status") != "completed"
            or run.get("conclusion") != "success" or run.get("workflow_id") != workflow_id
            or run.get("event") not in allowed_events
            or (expected_title is not None and run.get("display_title") != expected_title)):
        raise WorkflowError("manifest_run_not_successful")
    return run


def production_runs(
    runs: object, workflow_id: int, *, current_run_id: str | None = None
) -> list[dict[str, Any]]:
    if not isinstance(runs, list):
        raise WorkflowError("manifest_base_not_found")
    if current_run_id is not None and not re.fullmatch(r"[0-9]+", current_run_id):
        raise WorkflowError("current_workflow_run_id_invalid")
    live_runs = [run for run in runs if isinstance(run, dict)
                 and run.get("workflow_id") == workflow_id
                 and run.get("event") in {"schedule", "workflow_dispatch"}
                 and ((run.get("display_title") == PRODUCTION_TITLE and run.get("head_branch") == "main")
                      or str(run.get("id", "")) == RECOVERABLE_PREWRITE_RUN_ID)
                 and str(run.get("id", "")) != current_run_id]
    for run in live_runs:
        if (not re.fullmatch(r"[0-9]+", str(run.get("id", "")))
                or isinstance(run.get("run_number"), bool) or not isinstance(run.get("run_number"), int)
                or run["run_number"] < 1):
            raise WorkflowError("manifest_run_not_successful")
    return sorted(live_runs, key=lambda item: item["run_number"], reverse=True)


def is_proven_prewrite_incident(run: object) -> bool:
    """Recognize the single reviewed run that failed locally before its first Worker request."""
    return isinstance(run, dict) and (
        str(run.get("id")) == RECOVERABLE_PREWRITE_RUN_ID
        and run.get("head_sha") == RECOVERABLE_PREWRITE_HEAD_SHA
        and run.get("head_branch") == "main"
        and run.get("display_title") == PRODUCTION_TITLE
        and run.get("status") == "completed"
        and run.get("conclusion") == "failure"
    )


def choose_base(
    *,
    bootstrap_run_id: str,
    bootstrap_run: object | None,
    bootstrap_artifacts: object | None,
    poc_workflow_id: int,
    incremental_runs: object,
    incremental_workflow_id: int,
    artifact_lookup: Any,
    current_run_id: str | None = None,
) -> tuple[str, str]:
    """Select the newest durable production outcome without crossing ambiguity."""
    if not isinstance(incremental_runs, list):
        raise WorkflowError("manifest_base_not_found")

    live_runs = production_runs(
        incremental_runs, incremental_workflow_id, current_run_id=current_run_id
    )

    for latest in live_runs:
        run_id = str(latest.get("id", ""))
        if not re.fullmatch(r"[0-9]+", run_id):
            raise WorkflowError("manifest_run_not_successful")
        artifacts = artifact_lookup(run_id)
        name = f"folketinget-snapshot-incremental-{run_id}"
        noop_name = f"{NO_COMMIT_PREFIX}{run_id}"
        if isinstance(artifacts, list):
            names = [item.get("name") for item in artifacts if isinstance(item, dict)]
            has_manifest = name in names
            has_noop = noop_name in names
        else:
            raise WorkflowError("manifest_artifact_missing_or_ambiguous")
        if has_manifest and has_noop:
            raise WorkflowError("manifest_artifact_missing_or_ambiguous")
        if has_manifest:
            validate_artifact(artifacts, name)
            return run_id, name
        if has_noop:
            validate_artifact(artifacts, noop_name)
            continue
        if is_proven_prewrite_incident(latest):
            continue
        # The Worker may have committed before a later workflow failure. Without
        # either durable proof, an older base is unsafe regardless of conclusion.
        raise WorkflowError("ambiguous_previous_production_run")

    if not re.fullmatch(r"[0-9]+", bootstrap_run_id or ""):
        raise WorkflowError("manifest_run_id_invalid")
    validate_run(bootstrap_run, requested_id=bootstrap_run_id,
                 workflow_id=poc_workflow_id, allowed_events={"workflow_dispatch"})
    name = f"folketinget-snapshot-manifest-{bootstrap_run_id}"
    validate_artifact(bootstrap_artifacts, name)
    return bootstrap_run_id, name


class GitHubApi:
    def __init__(self, repository: str, token: str, opener: Any = urllib.request.urlopen) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or "") or not token:
            raise WorkflowError("github_api_configuration_invalid")
        self.repository = repository
        self.token = token
        self.opener = opener

    def get(self, path: str) -> dict[str, Any]:
        if not path.startswith(f"/repos/{self.repository}/actions/"):
            raise WorkflowError("github_api_request_failed")
        request = urllib.request.Request(
            API + path,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "User-Agent": USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"},
        )
        try:
            with self.opener(request, timeout=15) as response:
                raw = response.read(1_000_001)
        except Exception:
            raise WorkflowError("github_api_request_failed") from None
        if len(raw) > 1_000_000:
            raise WorkflowError("github_api_response_invalid")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WorkflowError("github_api_response_invalid") from None
        if not isinstance(value, dict):
            raise WorkflowError("github_api_response_invalid")
        return value

    def workflow_id(self, filename: str) -> int:
        value = self.get(f"/repos/{self.repository}/actions/workflows/{filename}").get("id")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise WorkflowError("github_api_response_invalid")
        return value

    def run(self, run_id: str) -> dict[str, Any]:
        return self.get(f"/repos/{self.repository}/actions/runs/{run_id}")

    def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        value = self.get(f"/repos/{self.repository}/actions/runs/{run_id}/artifacts?per_page=100").get("artifacts")
        if not isinstance(value, list):
            raise WorkflowError("github_api_response_invalid")
        return value

    def recent_runs(self, workflow_id: int) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        total_count: int | None = None
        for page in range(1, 11):
            response = self.get(
                f"/repos/{self.repository}/actions/workflows/{workflow_id}/runs?per_page=100&page={page}"
            )
            value = response.get("workflow_runs")
            count = response.get("total_count")
            if (not isinstance(value, list) or isinstance(count, bool) or not isinstance(count, int)
                    or count < 0):
                raise WorkflowError("github_api_response_invalid")
            total_count = count
            collected.extend(value)
            if len(collected) >= total_count or len(value) < 100:
                break
        if total_count is None or len(collected) < total_count:
            raise WorkflowError("workflow_run_history_limit")
        return collected


def select_base(
    api: GitHubApi, bootstrap_run_id: str, *, current_run_id: str | None = None
) -> tuple[str, str]:
    incremental_id = api.workflow_id(INCREMENTAL_WORKFLOW)
    runs = api.recent_runs(incremental_id)
    live_runs = production_runs(runs, incremental_id, current_run_id=current_run_id)
    if live_runs:
        return choose_base(
            bootstrap_run_id=bootstrap_run_id, bootstrap_run=None, bootstrap_artifacts=None,
            poc_workflow_id=-1, incremental_runs=live_runs, incremental_workflow_id=incremental_id,
            artifact_lookup=api.artifacts, current_run_id=current_run_id,
        )

    if not re.fullmatch(r"[0-9]+", bootstrap_run_id or ""):
        raise WorkflowError("manifest_run_id_invalid")
    poc_id = api.workflow_id(POC_WORKFLOW)
    run = api.run(bootstrap_run_id)
    artifacts = api.artifacts(bootstrap_run_id)
    return choose_base(
        bootstrap_run_id=bootstrap_run_id, bootstrap_run=run, bootstrap_artifacts=artifacts,
        poc_workflow_id=poc_id, incremental_runs=[], incremental_workflow_id=incremental_id,
        artifact_lookup=api.artifacts, current_run_id=current_run_id,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["select-base", "validate-production-ref"])
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-production-ref":
            validate_production_ref(
                os.environ.get("GITHUB_EVENT_NAME", ""),
                os.environ.get("INPUT_MODE", ""),
                os.environ.get("GITHUB_REF", ""),
            )
            print("production_branch_validation=PASS")
            return 0
        api = GitHubApi(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", ""))
        run_id, artifact_name = select_base(
            api,
            os.environ.get("BOOTSTRAP_MANIFEST_RUN_ID", ""),
            current_run_id=os.environ.get("GITHUB_RUN_ID") or None,
        )
        output = os.environ.get("GITHUB_OUTPUT")
        if not output:
            raise WorkflowError("github_output_missing")
        with open(output, "a", encoding="utf-8") as target:
            target.write(f"run_id={run_id}\nartifact_name={artifact_name}\n")
        print("snapshot_base_selection=PASS")
    except WorkflowError as error:
        operation = ("production_branch_validation" if args.command == "validate-production-ref"
                     else "snapshot_base_selection")
        print(f"{operation}=FAIL category={error.category}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
