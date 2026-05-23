"""Verify worker/db.py issues the right Firestore updates for state transitions.

The worker never creates the runs/{run_id} doc — the submitter does that
with state='queued'. The worker only ever calls .update(), so .update()'s
NotFound behavior is the right contract for "submitter forgot to insert
the row" (we want loud failure, not silent .set).
"""
from unittest.mock import MagicMock

from worker import db


def _mock_client_chain(monkeypatch):
    client = MagicMock()
    doc = MagicMock()
    client.collection.return_value.document.return_value = doc
    monkeypatch.setattr(db, "_client", lambda: client)
    return client, doc


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
