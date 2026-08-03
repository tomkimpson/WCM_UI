"""Sole owner of the Firestore ``runs`` collection.

Both the worker and the API import this module rather than each knowing the
collection name and document shape. If a third consumer appears, promote it out
of ``worker/`` — but not before.

Document shape (collection ``runs``, document id = ``run_id``, a uuid4):

    state                  'queued' | 'running' | 'succeeded' | 'failed'
    created_at             server timestamp        (set on create_queued_run)
    execution_name         string                  (set on set_execution)
    started_at             server timestamp        (set on mark_running)
    attempt                int                     (set on try_mark_running)
    finished_at            server timestamp        (set on mark_*)

  Parameters — resolved is what ran, submitted is what the user typed:
    params_json            object                  (resolved: defaults ← form ← yaml)
    submitted_params_json  object, optional        (the user's raw form input)
    yaml_override_text     string, optional        (raw YAML; NEVER render as HTML)

  Provenance, all set at submit time:
    image_uri              string                  (digest-pinned when resolvable)
    image_digest           string, optional        ('sha256:…')
    image_pin_source       string, optional        job_spec_digest |
                                                   registry_manifest | unresolved
    wcecoli_git_sha        string, optional
    wcm_ui_git_sha         string, optional
    content_hash           string, optional        ('sha256:…' over digest+params)
    hash_version           int, optional
    deterministic          bool, optional          (False when parca_cpus > 1)
    schema_version         int, optional
    submitter              string, optional        'api' | 'cli'
    client_ip_hash         string, optional        salted HMAC, never a raw IP

  Artifacts:
    gcs_tarball_uri        string                  (set on mark_succeeded)
    gcs_parquet_uri        string                  (set on mark_succeeded)
    gcs_params_uri         string, optional        (set on create_queued_run)
    gcs_stderr_uri         string, optional        (on mark_failed)

  Failure and reconciliation:
    error_message          string                  (set on mark_failed)
    failure_source         string, optional        'params' | 'worker' |
                                                   'postprocess' | 'infrastructure'
    last_reconciled_at     server timestamp, optional

``create_queued_run`` is the only ``.set()`` in the codebase; everything else
uses ``.update()``, so a missing document raises ``NotFound`` — the right
failure mode when the submitter never inserted the row.

Note on ``state='queued'``: it means "document created, run_job() has not
returned yet", a sub-second window. It is NOT a queue. There is no broker —
run_job starts an execution immediately — and admission control rejects with
429 rather than holding work. Do not overload this state into a backlog.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Callable, Optional

from google.api_core.exceptions import NotFound
from google.cloud import firestore

_COLLECTION = "runs"

#: States from which no further transition is legal. The retry guard and the
#: reconciler both key off this: once a run is terminal, neither a Cloud Run
#: task retry nor a reconciliation sweep may reopen it.
TERMINAL_STATES = frozenset({"succeeded", "failed"})


@lru_cache(maxsize=4)
def _cached_client(project: Optional[str], database: str) -> firestore.Client:
    return firestore.Client(project=project, database=database)


def _client() -> firestore.Client:
    """The Firestore client.

    Cached on (project, database): the worker calls this once or twice per
    process, but the API calls it per request, and constructing a client means
    re-doing credential discovery and opening a fresh gRPC channel. Still the
    monkeypatch seam every test uses — patching `_client` bypasses the cache.
    """
    return _cached_client(
        os.environ.get("GCP_PROJECT"),
        os.environ.get("FIRESTORE_DATABASE", "(default)"),
    )


def _doc(run_id: str, client=None):
    """Document ref for a run.

    ``client`` lets a caller inject its own Firestore client — the CLI does, so
    that its own test seam still controls the writes while the collection name
    and document shape stay owned by this module.
    """
    return (client or _client()).collection(_COLLECTION).document(run_id)


def _atomic(client, fn: Callable[[Any], Any]) -> Any:
    """Execute ``fn(transaction)`` atomically.

    Real transactions go through ``firestore.transactional``, which drives a
    begin/commit/retry protocol. A test double sets ``applies_immediately`` to
    opt out: emulating that protocol in a dict-backed fake would test the
    emulation rather than the read-modify-write logic, and would break on every
    client-library upgrade. Genuine concurrency is proved against the emulator
    instead — see tests/test_quota_concurrency.py.
    """
    transaction = client.transaction()
    if getattr(transaction, "applies_immediately", False):
        return fn(transaction)
    return firestore.transactional(fn)(transaction)


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


# ---------------------------------------------------------------------------
# Reads. The worker never needed these; the API does for every status poll.
# ---------------------------------------------------------------------------

def get_run(run_id: str) -> Optional[dict[str, Any]]:
    """The run document, or None if it doesn't exist.

    Absence is a normal answer here — turning it into a 404 is the API's job,
    so this must not raise.
    """
    snapshot = _doc(run_id).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    data["run_id"] = run_id
    return data


def query_runs(
    *,
    state: Optional[str] = None,
    started_before: Any = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Runs matching a state, optionally bounded by ``started_at``.

    Used by the stranded-run sweep ("running and older than the wall-clock
    cap"). Deliberately not exposed by the API: `run_id` doubles as the share
    link, so an enumerable listing would leak every run.
    """
    query = _client().collection(_COLLECTION)
    if state is not None:
        query = query.where(filter=firestore.FieldFilter("state", "==", state))
    if started_before is not None:
        query = query.where(
            filter=firestore.FieldFilter("started_at", "<", started_before)
        )
    if limit is not None:
        query = query.limit(limit)

    out = []
    for snapshot in query.stream():
        data = snapshot.to_dict() or {}
        data["run_id"] = snapshot.id
        out.append(data)
    return out


# ---------------------------------------------------------------------------
# Creation. The only .set() in the codebase.
# ---------------------------------------------------------------------------

def create_queued_run(
    run_id: str,
    *,
    params_json: dict[str, Any],
    image_uri: str,
    submitted_params_json: Optional[dict[str, Any]] = None,
    yaml_override_text: Optional[str] = None,
    image_digest: Optional[str] = None,
    image_pin_source: Optional[str] = None,
    wcecoli_git_sha: Optional[str] = None,
    wcm_ui_git_sha: Optional[str] = None,
    content_hash: Optional[str] = None,
    hash_version: Optional[int] = None,
    deterministic: Optional[bool] = None,
    gcs_params_uri: Optional[str] = None,
    schema_version: Optional[int] = None,
    submitter: Optional[str] = None,
    client_ip_hash: Optional[str] = None,
    client=None,
) -> None:
    """Insert the run document with state='queued'.

    ``params_json`` is the RESOLVED parameter set — what the simulation will
    actually run with. ``submitted_params_json`` and ``yaml_override_text``
    keep the user's raw input so "fork these parameters" can reproduce the form
    they saw, not the merged result.

    ``client_ip_hash`` is a salted HMAC, never a raw address. Storing raw IPs
    of anonymous users in a permanent record would be a needless liability;
    the quota counters hold the only IP-derived data and expire on a TTL.
    """
    payload: dict[str, Any] = {
        "state": "queued",
        "created_at": firestore.SERVER_TIMESTAMP,
        "params_json": params_json,
        "image_uri": image_uri,
    }
    optional = {
        "submitted_params_json": submitted_params_json,
        "yaml_override_text": yaml_override_text,
        "image_digest": image_digest,
        "image_pin_source": image_pin_source,
        "wcecoli_git_sha": wcecoli_git_sha,
        "wcm_ui_git_sha": wcm_ui_git_sha,
        "content_hash": content_hash,
        "hash_version": hash_version,
        "deterministic": deterministic,
        "gcs_params_uri": gcs_params_uri,
        "schema_version": schema_version,
        "submitter": submitter,
        "client_ip_hash": client_ip_hash,
    }
    # `is not None` rather than truthiness: deterministic=False and
    # hash_version=0 are meaningful values that must survive.
    payload.update({k: v for k, v in optional.items() if v is not None})
    _doc(run_id, client).set(payload)


def set_execution(run_id: str, execution_name: str, client=None) -> None:
    """Record the Cloud Run execution the submitter launched.

    Separate from creation because the execution name only exists after
    run_job() returns, and the document has to exist first so the worker —
    which may start within milliseconds — finds something to update.
    """
    _doc(run_id, client).update({"execution_name": execution_name})


# ---------------------------------------------------------------------------
# Guarded transitions.
# ---------------------------------------------------------------------------

def try_mark_running(run_id: str, *, attempt: int = 1) -> bool:
    """Move a non-terminal run to 'running'. False if it was already terminal.

    Cloud Run's task retry can re-enter a run that already finished. Without
    this guard the retry reopens a succeeded run and re-runs the whole
    simulation — a second ~26 minutes and a second charge for an answer we
    already have. The caller treats False as "another attempt already
    completed this run" and exits without running anything.
    """
    client = _client()
    ref = client.collection(_COLLECTION).document(run_id)

    def txn(transaction) -> bool:
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            # The submitter creates the document. Its absence is a real bug,
            # not a state we should paper over with a .set().
            raise NotFound(f"no run document for {run_id}")
        if (snapshot.to_dict() or {}).get("state") in TERMINAL_STATES:
            return False
        transaction.update(ref, {
            "state": "running",
            "started_at": firestore.SERVER_TIMESTAMP,
            "attempt": attempt,
        })
        return True

    return _atomic(client, txn)


def mark_infra_failed(run_id: str, error_message: str) -> bool:
    """Mark a run failed because the infrastructure died under it.

    False, with no write, if the run already reached a terminal state. That
    guard is load-bearing: the reconciler races the worker's own terminal
    write, and if it clobbered a succeeded run the user would be told their
    completed run failed and its artifacts would become unreachable. Succeeded
    always wins.
    """
    client = _client()
    ref = client.collection(_COLLECTION).document(run_id)

    def txn(transaction) -> bool:
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            raise NotFound(f"no run document for {run_id}")
        if (snapshot.to_dict() or {}).get("state") in TERMINAL_STATES:
            return False
        transaction.update(ref, {
            "state": "failed",
            "finished_at": firestore.SERVER_TIMESTAMP,
            "error_message": error_message,
            "failure_source": "infrastructure",
        })
        return True

    return _atomic(client, txn)


def touch_reconciled(run_id: str) -> None:
    """Stamp the last reconciliation check, to rate-limit the next one."""
    _doc(run_id).update({"last_reconciled_at": firestore.SERVER_TIMESTAMP})
