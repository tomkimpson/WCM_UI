"""Verify worker/db.py issues the right Firestore updates for state transitions.

The worker never creates the runs/{run_id} doc — the submitter does that
with state='queued'. The worker only ever calls .update(), so .update()'s
NotFound behavior is the right contract for "submitter forgot to insert
the row" (we want loud failure, not silent .set).

Stage 3 makes this module the single owner of the `runs` collection: the API
imports it rather than duplicating the collection name and document shape.
The transition-guard tests below use tests/fakes.FakeFirestore rather than a
MagicMock, because a conditional write is exactly the thing a mock cannot
verify — `doc.update.assert_called()` passes whether the guard worked or not.
"""
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import NotFound

from tests.fakes import FAKE_NOW, FakeFirestore
from worker import db


def _mock_client_chain(monkeypatch):
    client = MagicMock()
    doc = MagicMock()
    client.collection.return_value.document.return_value = doc
    monkeypatch.setattr(db, "_client", lambda: client)
    return client, doc


@pytest.fixture
def store(monkeypatch):
    fake = FakeFirestore()
    monkeypatch.setattr(db, "_client", lambda: fake)
    return fake


def test_mark_running_updates_state_and_started_at(monkeypatch):
    client, doc = _mock_client_chain(monkeypatch)

    db.mark_running("abc-123")

    client.collection.assert_called_once_with("runs")
    client.collection.return_value.document.assert_called_once_with("abc-123")
    payload = doc.update.call_args.args[0]
    assert payload["state"] == "running"
    assert "started_at" in payload


def test_mark_succeeded_writes_state_finished_and_uris(monkeypatch):
    _, doc = _mock_client_chain(monkeypatch)

    db.mark_succeeded(
        "abc-123",
        "gs://wcm-ui-runs-dev/abc-123/output.tar.gz",
        "gs://wcm-ui-runs-dev/abc-123/timeseries.parquet",
    )

    payload = doc.update.call_args.args[0]
    assert payload["state"] == "succeeded"
    assert payload["gcs_tarball_uri"].endswith("output.tar.gz")
    assert payload["gcs_parquet_uri"].endswith("timeseries.parquet")
    assert "finished_at" in payload


def test_mark_failed_writes_state_error_and_stderr_uri(monkeypatch):
    _, doc = _mock_client_chain(monkeypatch)

    db.mark_failed("abc-123", "OOM at gen 2", "gs://wcm-ui-runs-dev/abc-123/stderr.log")

    payload = doc.update.call_args.args[0]
    assert payload["state"] == "failed"
    assert payload["error_message"] == "OOM at gen 2"
    assert payload["gcs_stderr_uri"].endswith("stderr.log")
    assert "finished_at" in payload


def test_mark_failed_omits_stderr_uri_when_unavailable(monkeypatch):
    """A validation-time failure (no subprocess invoked) has no stderr to upload."""
    _, doc = _mock_client_chain(monkeypatch)

    db.mark_failed("abc-123", "params failed schema validation", None)

    payload = doc.update.call_args.args[0]
    assert payload["state"] == "failed"
    assert payload["error_message"] == "params failed schema validation"
    assert "gcs_stderr_uri" not in payload


# --- Stage 3: reads -------------------------------------------------------

def test_get_run_returns_the_document_as_a_dict(store):
    store.seed("runs", "abc-123", {"state": "running", "params_json": {"simulation": {}}})

    got = db.get_run("abc-123")

    assert got["state"] == "running"
    assert got["params_json"] == {"simulation": {}}


def test_get_run_returns_none_for_an_unknown_id(store):
    """404 is the API's job; the store layer reports absence, not an error."""
    assert db.get_run("nope") is None


def test_create_queued_run_writes_every_provenance_field(store):
    db.create_queued_run(
        "abc-123",
        params_json={"simulation": {"length_sec": 30}},
        submitted_params_json={"simulation": {"length_sec": 30}},
        yaml_override_text=None,
        image_uri="reg/worker@sha256:aaa",
        image_digest="sha256:aaa",
        image_pin_source="job_spec_digest",
        wcecoli_git_sha="3fc8ec1f",
        wcm_ui_git_sha="deadbeef",
        content_hash="sha256:bbb",
        hash_version=1,
        deterministic=True,
        gcs_params_uri="gs://bucket/abc-123/params.json",
        schema_version=1,
        submitter="api",
        client_ip_hash="0123456789abcdef",
    )

    doc = store.doc("runs", "abc-123")
    assert doc["state"] == "queued"
    assert doc["created_at"] == FAKE_NOW
    assert doc["image_digest"] == "sha256:aaa"
    assert doc["content_hash"] == "sha256:bbb"
    assert doc["deterministic"] is True
    assert doc["submitter"] == "api"
    assert doc["client_ip_hash"] == "0123456789abcdef"


def test_create_queued_run_is_the_only_set_and_overwrites_nothing_else(store):
    db.create_queued_run("abc-123", params_json={}, image_uri="reg/w:latest")
    kinds = [kind for kind, _, _ in store.writes]
    assert kinds == ["set"], "creating a run must be exactly one .set()"


def test_set_execution_records_the_execution_name(store):
    store.seed("runs", "abc-123", {"state": "queued"})

    db.set_execution("abc-123", "projects/p/locations/r/jobs/j/executions/e")

    assert store.doc("runs", "abc-123")["execution_name"].endswith("/executions/e")


# --- Stage 3: transactional guards ---------------------------------------

def test_try_mark_running_transitions_a_queued_run(store):
    store.seed("runs", "abc-123", {"state": "queued"})

    assert db.try_mark_running("abc-123", attempt=1) is True

    doc = store.doc("runs", "abc-123")
    assert doc["state"] == "running"
    assert doc["started_at"] == FAKE_NOW
    assert doc["attempt"] == 1


def test_try_mark_running_refuses_a_terminal_run_and_writes_nothing(store):
    """Cloud Run's --max-retries can re-enter a finished run.

    Without this guard the retry re-marks a succeeded run as 'running' and
    then re-runs the whole simulation, doubling the bill for no benefit.
    """
    store.seed("runs", "abc-123", {"state": "succeeded",
                                   "gcs_parquet_uri": "gs://b/abc-123/t.parquet"})

    assert db.try_mark_running("abc-123", attempt=2) is False

    doc = store.doc("runs", "abc-123")
    assert doc["state"] == "succeeded", "a retry must not reopen a finished run"
    assert store.write_count("runs") == 0, "the guard must not write at all"


def test_try_mark_running_refuses_a_failed_run(store):
    store.seed("runs", "abc-123", {"state": "failed"})
    assert db.try_mark_running("abc-123", attempt=2) is False


def test_try_mark_running_on_a_missing_doc_raises(store):
    """The submitter creates the doc. A missing one is a real bug, not a state."""
    with pytest.raises(NotFound):
        db.try_mark_running("nope", attempt=1)


def test_mark_infra_failed_marks_a_running_run(store):
    store.seed("runs", "abc-123", {"state": "running"})

    assert db.mark_infra_failed("abc-123", "execution vanished") is True

    doc = store.doc("runs", "abc-123")
    assert doc["state"] == "failed"
    assert doc["failure_source"] == "infrastructure"
    assert doc["error_message"] == "execution vanished"
    assert doc["finished_at"] == FAKE_NOW


def test_mark_infra_failed_never_overwrites_a_succeeded_run(store):
    """The reconciler races the worker's own terminal write.

    If the worker marks succeeded while the API is mid-reconcile, succeeded
    must win — otherwise a healthy run is reported as failed and its
    artifacts become unreachable.
    """
    store.seed("runs", "abc-123", {"state": "succeeded",
                                   "gcs_tarball_uri": "gs://b/abc-123/o.tar.gz"})

    assert db.mark_infra_failed("abc-123", "looked stalled to me") is False

    doc = store.doc("runs", "abc-123")
    assert doc["state"] == "succeeded"
    assert "error_message" not in doc
    assert store.write_count("runs") == 0


def test_touch_reconciled_writes_only_the_timestamp(store):
    store.seed("runs", "abc-123", {"state": "running"})

    db.touch_reconciled("abc-123")

    _, _, payload = store.writes[-1]
    assert set(payload) == {"last_reconciled_at"}
    assert store.doc("runs", "abc-123")["state"] == "running"


# --- Stage 3: queries -----------------------------------------------------

def test_query_runs_filters_by_state(store):
    store.seed("runs", "r1", {"state": "running", "started_at": 1})
    store.seed("runs", "r2", {"state": "succeeded", "started_at": 2})
    store.seed("runs", "r3", {"state": "running", "started_at": 3})

    got = db.query_runs(state="running")

    assert {r["run_id"] for r in got} == {"r1", "r3"}


def test_query_runs_can_bound_by_started_at(store):
    """The stranded-run sweep needs "running and older than the cap"."""
    store.seed("runs", "old", {"state": "running", "started_at": 1})
    store.seed("runs", "new", {"state": "running", "started_at": 100})

    got = db.query_runs(state="running", started_before=50)

    assert [r["run_id"] for r in got] == ["old"]
