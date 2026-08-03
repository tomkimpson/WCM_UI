"""Firestore client for run state transitions.

Document shape (collection ``runs``, document id = ``run_id``):

    state            'queued' | 'running' | 'succeeded' | 'failed'
    params_json      object                                  (set by submitter)
    image_uri        string                                  (set by submitter)
    execution_name   string                                  (set by submitter)
    started_at       server timestamp                        (set on mark_running)
    finished_at      server timestamp                        (set on mark_*)
    gcs_tarball_uri  string                                  (set on mark_succeeded)
    gcs_parquet_uri  string                                  (set on mark_succeeded)
    gcs_stderr_uri   string                                  (optional, on mark_failed)
    error_message    string                                  (set on mark_failed)
    failure_source   string                                  (optional, on mark_failed)
                     'params' | 'worker' | 'postprocess' | 'infrastructure' —
                     lets the UI distinguish "your parameters were wrong" from
                     "the infrastructure died"

The submitter creates the doc with ``state='queued'``. The worker only ever
calls ``.update()``, so a missing doc raises ``NotFound`` — the right
failure mode if the submitter forgot to insert the row.
"""
from __future__ import annotations

import os
from typing import Optional

from google.cloud import firestore

_COLLECTION = "runs"


def _client() -> firestore.Client:
    return firestore.Client(
        project=os.environ.get("GCP_PROJECT"),
        database=os.environ.get("FIRESTORE_DATABASE", "(default)"),
    )


def _doc(run_id: str):
    return _client().collection(_COLLECTION).document(run_id)


def mark_running(run_id: str) -> None:
    _doc(run_id).update({
        "state": "running",
        "started_at": firestore.SERVER_TIMESTAMP,
    })


def mark_succeeded(run_id: str, gcs_tarball_uri: str, gcs_parquet_uri: str) -> None:
    _doc(run_id).update({
        "state": "succeeded",
        "finished_at": firestore.SERVER_TIMESTAMP,
        "gcs_tarball_uri": gcs_tarball_uri,
        "gcs_parquet_uri": gcs_parquet_uri,
    })


def mark_failed(
    run_id: str,
    error_message: str,
    gcs_stderr_uri: Optional[str],
    *,
    failure_source: Optional[str] = None,
) -> None:
    payload = {
        "state": "failed",
        "finished_at": firestore.SERVER_TIMESTAMP,
        "error_message": error_message,
    }
    if gcs_stderr_uri:
        payload["gcs_stderr_uri"] = gcs_stderr_uri
    if failure_source:
        payload["failure_source"] = failure_source
    _doc(run_id).update(payload)
