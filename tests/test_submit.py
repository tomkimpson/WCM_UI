"""Verify scripts/submit.py wires a params file to a Cloud Run Jobs run.

Submit flow:
    1. Read + validate params JSON  (worker.validate)
    2. Generate run_id (uuid4)
    3. Create runs/{run_id} Firestore doc with state='queued'
    4. Call jobs_client.run_job with env-var overrides
    5. Update the doc with execution_name
    6. Print run_id to stdout

Failure modes asserted:
    - Invalid params → exit 64, no Firestore write, no Cloud Run call
    - Missing params file → exit 64
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from scripts import submit


@pytest.fixture
def valid_params_file(tmp_path):
    p = tmp_path / "params.json"
    p.write_text(json.dumps({"simulation": {"length_sec": 30}}))
    return p


@pytest.fixture
def mocked_clients(monkeypatch):
    """Patch the two GCP client factories with MagicMock instances.

    Returns dict with named mocks for assertion."""
    firestore_client = MagicMock(name="FirestoreClient")
    doc_ref = MagicMock(name="DocRef")
    firestore_client.collection.return_value.document.return_value = doc_ref

    jobs_client = MagicMock(name="JobsClient")
    operation = MagicMock(name="Operation")
    operation.metadata.name = (
        "projects/wcm-ui-dev/locations/us-central1/jobs/wcm-ui-worker-dev/executions/exec-xyz"
    )
    jobs_client.run_job.return_value = operation

    monkeypatch.setattr(submit, "_firestore_client", lambda: firestore_client)
    monkeypatch.setattr(submit, "_jobs_client", lambda: jobs_client)
    monkeypatch.setattr(submit, "_generate_run_id", lambda: "abc-123")

    return {
        "firestore": firestore_client,
        "doc_ref": doc_ref,
        "jobs": jobs_client,
        "operation": operation,
    }


def test_valid_submit_creates_queued_doc_and_runs_job(
    valid_params_file, mocked_clients, capsys
):
    rc = submit.main([
        "--params", str(valid_params_file),
        "--project", "wcm-ui-dev",
        "--job", "wcm-ui-worker-dev",
        "--region", "us-central1",
        "--image-uri", "us-central1-docker.pkg.dev/wcm-ui-dev/wcm-ui-worker/worker:latest",
    ])
    assert rc == 0

    # Firestore doc was created with state='queued' and the resolved params.
    mocked_clients["firestore"].collection.assert_any_call("runs")
    mocked_clients["doc_ref"].set.assert_called_once()
    queued_payload = mocked_clients["doc_ref"].set.call_args.args[0]
    assert queued_payload["state"] == "queued"
    # params_json holds the resolved (defaults + user) blob, not the raw input —
    # we want the row to record what the run actually used, for reproducibility.
    assert queued_payload["params_json"]["simulation"]["length_sec"] == 30
    assert queued_payload["params_json"]["simulation"]["seed"] == 0  # default
    assert queued_payload["image_uri"] == "us-central1-docker.pkg.dev/wcm-ui-dev/wcm-ui-worker/worker:latest"
    assert "created_at" in queued_payload

    # Cloud Run job was invoked with the right name and env overrides.
    mocked_clients["jobs"].run_job.assert_called_once()
    request = mocked_clients["jobs"].run_job.call_args.kwargs["request"]
    assert request.name == (
        "projects/wcm-ui-dev/locations/us-central1/jobs/wcm-ui-worker-dev"
    )
    env_overrides = {
        e.name: e.value
        for e in request.overrides.container_overrides[0].env
    }
    assert env_overrides["RUN_ID"] == "abc-123"
    assert env_overrides["IMAGE_URI"] == "us-central1-docker.pkg.dev/wcm-ui-dev/wcm-ui-worker/worker:latest"
    # PARAMS_JSON is the resolved (defaulted + validated) JSON, not the raw input.
    assert json.loads(env_overrides["PARAMS_JSON"])["simulation"]["length_sec"] == 30

    # Doc was updated with the execution name afterwards.
    mocked_clients["doc_ref"].update.assert_called_once()
    update_payload = mocked_clients["doc_ref"].update.call_args.args[0]
    assert "executions/exec-xyz" in update_payload["execution_name"]

    # Run ID printed to stdout.
    captured = capsys.readouterr()
    assert "abc-123" in captured.out


def test_invalid_params_exits_64_no_side_effects(tmp_path, mocked_clients):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"simulation": {"length_sec": -1}}))  # < minimum

    rc = submit.main(["--params", str(bad), "--project", "wcm-ui-dev"])
    assert rc == 64

    mocked_clients["doc_ref"].set.assert_not_called()
    mocked_clients["jobs"].run_job.assert_not_called()


def test_missing_params_file_exits_64(tmp_path, mocked_clients):
    missing = tmp_path / "does-not-exist.json"

    rc = submit.main(["--params", str(missing), "--project", "wcm-ui-dev"])
    assert rc == 64

    mocked_clients["doc_ref"].set.assert_not_called()
    mocked_clients["jobs"].run_job.assert_not_called()


def test_malformed_json_exits_64(tmp_path, mocked_clients):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")

    rc = submit.main(["--params", str(p), "--project", "wcm-ui-dev"])
    assert rc == 64

    mocked_clients["doc_ref"].set.assert_not_called()
    mocked_clients["jobs"].run_job.assert_not_called()
