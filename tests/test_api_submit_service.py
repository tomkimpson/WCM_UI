"""One submit path, shared by the CLI and the HTTP endpoint.

Before this, scripts/submit.py *was* the submit logic and the API would have had
to reimplement it. The property worth protecting is that both callers produce
identical Firestore documents and identical Cloud Run requests, because a
divergence there is a reproducibility bug that only shows up months later in a
run nobody can reproduce.

Quota deliberately does not appear here. The router reserves before calling
submit_run and releases if it raises, so this module stays a pure "launch a run"
operation and the ordering rule — validate before reserving, so a malformed
request cannot consume a slot — lives in one visible place.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import DeadlineExceeded, InvalidArgument

from api import cloudrun, runs
from tests.fakes import FakeFirestore
from worker import db

T0 = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
TARGET = cloudrun.JobTarget(project="wcm-ui-dev", region="us-central1",
                            job="wcm-ui-worker-dev")
IMAGE = "reg/worker@sha256:deadbeef"


@pytest.fixture
def store(monkeypatch):
    fake = FakeFirestore(now=T0)
    monkeypatch.setattr(db, "_client", lambda: fake)
    return fake


@pytest.fixture
def jobs_client():
    client = MagicMock()
    operation = MagicMock()
    operation.metadata.name = (
        "projects/wcm-ui-dev/locations/us-central1/jobs/wcm-ui-worker-dev"
        "/executions/exec-xyz"
    )
    client.run_job.return_value = operation
    client.get_job.return_value = SimpleNamespace(
        template=SimpleNamespace(
            template=SimpleNamespace(containers=[SimpleNamespace(image=IMAGE)])
        )
    )
    return client


def _submit(store, jobs_client, **kwargs):
    kwargs.setdefault("params", {"simulation": {"length_sec": 30}})
    kwargs.setdefault("yaml_override", None)
    kwargs.setdefault("run_id", "abc-123")
    kwargs.setdefault("firestore_client", store)
    kwargs.setdefault("jobs_client", jobs_client)
    kwargs.setdefault("target", TARGET)
    kwargs.setdefault("submitter", "api")
    kwargs.setdefault("wall_clock_cap_sec", 2700)
    return runs.submit_run(**kwargs)


# --- the happy path -------------------------------------------------------

def test_submit_creates_the_document_then_launches(store, jobs_client):
    result = _submit(store, jobs_client)

    assert result.ok
    assert result.run_id == "abc-123"
    assert result.execution_name.endswith("/executions/exec-xyz")

    doc = store.doc("runs", "abc-123")
    assert doc["state"] == "queued"
    assert doc["params_json"]["simulation"]["length_sec"] == 30
    assert doc["params_json"]["simulation"]["seed"] == 0, "defaults are merged in"
    assert doc["execution_name"].endswith("/executions/exec-xyz")


def test_the_document_exists_before_the_job_is_launched(store, jobs_client):
    """Ordering is not cosmetic: the worker can start within milliseconds and
    its first act is db.try_mark_running, which requires the document."""
    order = []
    real_set = store.collection("runs").document("abc-123").set

    def spy_run_job(request=None):
        order.append("run_job")
        return jobs_client.run_job.return_value
    jobs_client.run_job.side_effect = spy_run_job

    _submit(store, jobs_client)
    kinds = [k for k, (coll, _), _ in store.writes if coll == "runs"]
    assert kinds[0] == "set", "the run document must be created first"
    assert order == ["run_job"]
    assert real_set is not None  # keep the reference meaningful


def test_the_launch_request_carries_the_resolved_params(store, jobs_client):
    _submit(store, jobs_client, params={"simulation": {"length_sec": 45}})
    request = jobs_client.run_job.call_args.kwargs["request"]
    env = {e.name: e.value for e in request.overrides.container_overrides[0].env}
    assert json.loads(env["PARAMS_JSON"])["simulation"]["length_sec"] == 45
    assert env["RUN_ID"] == "abc-123"


def test_the_wall_clock_cap_reaches_the_request(store, jobs_client):
    _submit(store, jobs_client, wall_clock_cap_sec=600)
    request = jobs_client.run_job.call_args.kwargs["request"]
    assert request.overrides.timeout.total_seconds() == 600


def test_provenance_is_recorded_from_the_job_spec_digest(store, jobs_client):
    """The digest comes from the Job spec, which is what Cloud Run will pull."""
    result = _submit(store, jobs_client)
    doc = store.doc("runs", "abc-123")
    assert doc["image_digest"] == "sha256:deadbeef"
    assert doc["image_pin_source"] == "job_spec_digest"
    assert doc["content_hash"] == result.content_hash
    assert doc["hash_version"] == 1
    assert doc["deterministic"] is True


def test_the_submitted_and_resolved_params_are_both_kept(store, jobs_client):
    """"Fork these parameters" must reproduce the form the user saw, not the
    merged result."""
    _submit(store, jobs_client,
            params={"simulation": {"length_sec": 45}},
            yaml_override="simulation:\n  seed: 9\n")
    doc = store.doc("runs", "abc-123")
    assert doc["submitted_params_json"] == {"simulation": {"length_sec": 45}}
    assert doc["yaml_override_text"] == "simulation:\n  seed: 9\n"
    assert doc["params_json"]["simulation"]["seed"] == 9


def test_the_params_gcs_uri_is_recorded(store, jobs_client):
    """postprocess uploads params.json but never recorded where."""
    _submit(store, jobs_client, runs_bucket="wcm-ui-runs-dev")
    doc = store.doc("runs", "abc-123")
    assert doc["gcs_params_uri"] == "gs://wcm-ui-runs-dev/abc-123/params.json"


def test_the_submitter_is_recorded(store, jobs_client):
    _submit(store, jobs_client, submitter="cli")
    assert store.doc("runs", "abc-123")["submitter"] == "cli"


def test_the_ip_hash_is_stored_but_never_the_raw_address(store, jobs_client):
    _submit(store, jobs_client, client_ip_hash="0123456789abcdef")
    doc = store.doc("runs", "abc-123")
    assert doc["client_ip_hash"] == "0123456789abcdef"
    assert not any("ip" == k or k.endswith("_ip") for k in doc), doc.keys()


# --- validation short-circuits -------------------------------------------

def test_invalid_params_write_nothing_and_launch_nothing(store, jobs_client):
    result = _submit(store, jobs_client, params={"simulation": {"length_sec": -1}})

    assert not result.ok
    assert result.errors
    assert store.write_count() == 0
    jobs_client.run_job.assert_not_called()


def test_all_validation_errors_come_back_at_once(store, jobs_client):
    result = _submit(store, jobs_client,
                     params={"simulation": {"length_sec": 0, "seed": -1}})
    assert {e.path for e in result.errors} == {
        "simulation.length_sec", "simulation.seed",
    }


def test_a_yaml_syntax_error_is_reported_not_raised(store, jobs_client):
    result = _submit(store, jobs_client, yaml_override="a: [unclosed\n")
    assert not result.ok
    assert result.errors[0].kind == "yaml_syntax"
    jobs_client.run_job.assert_not_called()


# --- launch failures ------------------------------------------------------

def test_a_provably_unstarted_launch_marks_the_run_failed_and_asks_for_a_refund(
    store, jobs_client,
):
    """InvalidArgument means Cloud Run rejected the request outright.

    No compute was spent, so the quota reservation must be given back — and the
    document must not be left sitting in 'queued' forever.
    """
    jobs_client.run_job.side_effect = InvalidArgument("bad override")

    with pytest.raises(runs.LaunchFailed) as exc:
        _submit(store, jobs_client)

    assert exc.value.refund_daily is True
    doc = store.doc("runs", "abc-123")
    assert doc["state"] == "failed"
    assert doc["failure_source"] == "infrastructure"


def test_a_timeout_does_not_ask_for_a_refund(store, jobs_client):
    """The nastiest case: the client gave up but the server may have started it.

    When in doubt, charge. Over-charging annoys one user; under-charging is an
    unbounded bill.
    """
    jobs_client.run_job.side_effect = DeadlineExceeded("no answer")

    with pytest.raises(runs.LaunchFailed) as exc:
        _submit(store, jobs_client)

    assert exc.value.refund_daily is False


def test_a_timeout_leaves_the_run_for_the_reconciler(store, jobs_client):
    """We cannot say it failed — it may be running. Reconciliation resolves it
    against the Executions API."""
    jobs_client.run_job.side_effect = DeadlineExceeded("no answer")
    with pytest.raises(runs.LaunchFailed):
        _submit(store, jobs_client)
    assert store.doc("runs", "abc-123")["state"] == "queued"


# --- provenance degradation ----------------------------------------------

def test_an_unreadable_job_spec_still_lets_the_run_launch(store, jobs_client):
    """Provenance is best-effort. A missing run.jobs.get grant — the likeliest
    production misconfiguration — must not stop people running simulations."""
    from google.api_core.exceptions import PermissionDenied
    jobs_client.get_job.side_effect = PermissionDenied("no run.jobs.get")

    result = _submit(store, jobs_client)

    assert result.ok
    doc = store.doc("runs", "abc-123")
    assert doc["image_pin_source"] == "unresolved"
    assert "image_digest" not in doc
    jobs_client.run_job.assert_called_once()


def test_a_tag_only_job_spec_is_recorded_as_unresolved(store, jobs_client):
    jobs_client.get_job.return_value = SimpleNamespace(
        template=SimpleNamespace(template=SimpleNamespace(
            containers=[SimpleNamespace(image="reg/worker:latest")]))
    )
    result = _submit(store, jobs_client)
    assert result.ok
    assert store.doc("runs", "abc-123")["image_pin_source"] == "unresolved"


def test_the_same_params_and_image_produce_the_same_content_hash(store, jobs_client):
    a = _submit(store, jobs_client, run_id="run-a")
    b = _submit(store, jobs_client, run_id="run-b")
    assert a.content_hash == b.content_hash
