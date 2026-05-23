"""Verify worker/run.py orchestrates the cloud-mode lifecycle correctly.

Cloud mode (RUN_ID set, RUNS_BUCKET set):
    queued (by submitter) → mark_running → run sim
    → on success: tarball + Parquet + upload + mark_succeeded
    → on subprocess failure: upload stderr + mark_failed
    → on param validation failure: mark_failed (no stderr to upload)

Local mode (RUN_ID unset, Stage 1b behavior): no DB or postprocess
calls at all — preserves the local `docker run -e PARAMS_JSON=…` loop.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from worker import db, postprocess, run


@pytest.fixture
def fake_wcecoli_dir(tmp_path, monkeypatch):
    wc = tmp_path / "wcEcoli"
    (wc / "out" / "manual").mkdir(parents=True)
    monkeypatch.setattr(run, "WCECOLI_DIR", str(wc))
    return wc


def _mock_cloud_libs(monkeypatch):
    """Replace db, postprocess, and storage-client functions with MagicMocks."""
    mocks = {
        "mark_running":   MagicMock(),
        "mark_succeeded": MagicMock(),
        "mark_failed":    MagicMock(),
        "make_tarball":   MagicMock(),
        "extract_timeseries": MagicMock(),
        "upload_run_artifacts": MagicMock(return_value={
            "tarball": "gs://wcm-ui-runs-dev/abc-123/output.tar.gz",
            "parquet": "gs://wcm-ui-runs-dev/abc-123/timeseries.parquet",
            "params":  "gs://wcm-ui-runs-dev/abc-123/params.json",
        }),
        "upload_stderr":  MagicMock(return_value="gs://wcm-ui-runs-dev/abc-123/stderr.log"),
    }
    monkeypatch.setattr(db, "mark_running", mocks["mark_running"])
    monkeypatch.setattr(db, "mark_succeeded", mocks["mark_succeeded"])
    monkeypatch.setattr(db, "mark_failed", mocks["mark_failed"])
    monkeypatch.setattr(postprocess, "make_tarball", mocks["make_tarball"])
    monkeypatch.setattr(postprocess, "extract_timeseries", mocks["extract_timeseries"])
    monkeypatch.setattr(postprocess, "upload_run_artifacts", mocks["upload_run_artifacts"])
    monkeypatch.setattr(postprocess, "upload_stderr", mocks["upload_stderr"])
    monkeypatch.setattr(run, "_storage_client", lambda: MagicMock())
    return mocks


def test_no_run_id_preserves_stage1b_behavior(monkeypatch, fake_wcecoli_dir):
    """Without RUN_ID, db and postprocess are never touched."""
    monkeypatch.delenv("RUN_ID", raising=False)
    monkeypatch.setenv("PARAMS_JSON", "{}")

    mocks = _mock_cloud_libs(monkeypatch)
    # Side-effect: blow up if anything cloud-y gets called.
    for name in ("mark_running", "mark_succeeded", "mark_failed",
                 "upload_run_artifacts", "upload_stderr"):
        mocks[name].side_effect = AssertionError(f"{name} called in local mode")

    monkeypatch.setattr("worker.run.subprocess.run",
                        MagicMock(return_value=MagicMock(returncode=0)))

    assert run.main() == 0


def test_cloud_success_path_runs_full_lifecycle(monkeypatch, fake_wcecoli_dir):
    monkeypatch.setenv("RUN_ID", "abc-123")
    monkeypatch.setenv("RUNS_BUCKET", "wcm-ui-runs-dev")
    monkeypatch.setenv("PARAMS_JSON", '{"simulation": {"length_sec": 30}}')

    mocks = _mock_cloud_libs(monkeypatch)
    monkeypatch.setattr("worker.run.subprocess.run",
                        MagicMock(return_value=MagicMock(returncode=0)))

    assert run.main() == 0

    mocks["mark_running"].assert_called_once_with("abc-123")
    mocks["make_tarball"].assert_called_once()
    mocks["extract_timeseries"].assert_called_once()
    mocks["upload_run_artifacts"].assert_called_once()
    mocks["mark_succeeded"].assert_called_once_with(
        "abc-123",
        "gs://wcm-ui-runs-dev/abc-123/output.tar.gz",
        "gs://wcm-ui-runs-dev/abc-123/timeseries.parquet",
    )
    mocks["mark_failed"].assert_not_called()
    mocks["upload_stderr"].assert_not_called()


def test_cloud_subprocess_failure_uploads_stderr_and_marks_failed(monkeypatch, fake_wcecoli_dir):
    monkeypatch.setenv("RUN_ID", "abc-123")
    monkeypatch.setenv("RUNS_BUCKET", "wcm-ui-runs-dev")
    monkeypatch.setenv("PARAMS_JSON", '{"simulation": {"length_sec": 30}}')

    mocks = _mock_cloud_libs(monkeypatch)
    monkeypatch.setattr("worker.run.subprocess.run",
                        MagicMock(return_value=MagicMock(returncode=2)))

    rc = run.main()

    assert rc == 2
    mocks["mark_running"].assert_called_once_with("abc-123")
    mocks["upload_stderr"].assert_called_once()
    mocks["mark_failed"].assert_called_once()
    run_id_arg, _err_msg, stderr_uri_arg = mocks["mark_failed"].call_args.args
    assert run_id_arg == "abc-123"
    assert stderr_uri_arg == "gs://wcm-ui-runs-dev/abc-123/stderr.log"
    mocks["mark_succeeded"].assert_not_called()
    mocks["upload_run_artifacts"].assert_not_called()


def test_param_validation_failure_marks_failed_without_stderr(monkeypatch, fake_wcecoli_dir):
    """Validation fails before subprocess runs, so there's no stderr to upload."""
    monkeypatch.setenv("RUN_ID", "abc-123")
    monkeypatch.setenv("RUNS_BUCKET", "wcm-ui-runs-dev")
    monkeypatch.setenv("PARAMS_JSON", '{"simulation": {"length_sec": -1}}')

    mocks = _mock_cloud_libs(monkeypatch)
    # subprocess.run must never be called — assert by raising if it is.
    monkeypatch.setattr("worker.run.subprocess.run",
                        MagicMock(side_effect=AssertionError("subprocess invoked on bad params")))

    rc = run.main()

    assert rc == 64
    mocks["mark_running"].assert_not_called()
    mocks["mark_failed"].assert_called_once()
    run_id_arg, err_msg, stderr_uri_arg = mocks["mark_failed"].call_args.args
    assert run_id_arg == "abc-123"
    assert "param error" in err_msg
    assert stderr_uri_arg is None
    mocks["upload_stderr"].assert_not_called()
