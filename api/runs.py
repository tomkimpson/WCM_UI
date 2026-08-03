"""Launch a run. The one submit path, shared by the CLI and the HTTP endpoint.

``scripts/submit.py`` used to *be* the submit logic, which meant the API would
have had to reimplement it. Both now call ``submit_run``, so they cannot drift —
and a drift here is the worst kind of reproducibility bug, showing up months
later as a run nobody can reproduce.

Quota is deliberately absent. The router reserves a slot before calling this and
releases it if this raises, which keeps the ordering rule — validate before
reserving, so a malformed request cannot consume a slot — in one visible place
rather than buried in here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime as datetime_type, timezone
from typing import Any, Optional

from google.api_core.exceptions import (
    GoogleAPICallError,
    InvalidArgument,
    NotFound,
    PermissionDenied,
    ResourceExhausted,
)

from api import cloudrun, provenance
from api.params import resolve_params
from worker import db
from worker.validate import FieldErrorData

#: Bump when the shape of the parameter schema changes, so a stored run records
#: which contract it was created under.
SCHEMA_VERSION = 1


def _utcnow() -> datetime_type:
    return datetime_type.now(timezone.utc)

#: Errors that mean Cloud Run definitively did not start anything. Only these
#: justify refunding the day's quota — see LaunchFailed.
_PROVABLY_NOT_STARTED = (
    InvalidArgument,      # the request itself was rejected
    PermissionDenied,     # we were not allowed to run it
    NotFound,             # the job doesn't exist
    ResourceExhausted,    # a GCP-side quota refused it
)


class LaunchFailed(Exception):
    """Cloud Run would not start the execution.

    ``refund_daily`` tells the caller whether to give the day's quota back. It
    is True only for errors that prove nothing started. A DeadlineExceeded gets
    False: the client gave up, but the server may well have launched the job, and
    the failure modes are asymmetric — over-charging annoys one user, while
    under-charging is an unbounded bill.
    """

    def __init__(self, message: str, *, refund_daily: bool):
        super().__init__(message)
        self.refund_daily = refund_daily


@dataclass(frozen=True)
class SubmitResult:
    ok: bool
    errors: tuple[FieldErrorData, ...] = ()
    run_id: Optional[str] = None
    execution_name: Optional[str] = None
    resolved_params: Optional[dict[str, Any]] = None
    content_hash: Optional[str] = None
    deterministic: Optional[bool] = None
    image_uri: Optional[str] = None
    console_url: Optional[str] = None


def submit_run(
    *,
    params: Any,
    yaml_override: Optional[str],
    run_id: str,
    firestore_client,
    jobs_client,
    target: cloudrun.JobTarget,
    submitter: str,
    wall_clock_cap_sec: Optional[int],
    client_ip_hash: Optional[str] = None,
    runs_bucket: Optional[str] = None,
    fallback_image_uri: Optional[str] = None,
) -> SubmitResult:
    """Validate, record, and launch.

    Returns a SubmitResult with ``ok=False`` and populated ``errors`` for
    anything the user can fix; raises ``LaunchFailed`` only when Cloud Run
    refuses, which is not the user's fault and needs different handling.
    """
    resolved = resolve_params(params, yaml_override)
    if not resolved.ok:
        # Nothing has been written and nothing launched — a rejected submission
        # must leave no trace.
        return SubmitResult(ok=False, errors=resolved.errors)

    resolved_params = resolved.resolved
    assert resolved_params is not None  # guaranteed by ResolveResult

    # Provenance is best-effort and must never block a run: an unreadable job
    # spec (the likeliest production misconfiguration is a missing
    # run.jobs.get grant) degrades to source="unresolved".
    pin = provenance.resolve_image_pin(cloudrun.get_job(jobs_client, target))
    image_uri = pin.uri or fallback_image_uri or ""
    wcm_ui_sha, wcecoli_sha = provenance.git_shas()
    content_hash = provenance.content_hash(resolved_params, pin.digest)

    gcs_params_uri = (f"gs://{runs_bucket}/{run_id}/params.json"
                      if runs_bucket else None)

    db.create_queued_run(
        run_id,
        params_json=resolved_params,
        image_uri=image_uri,
        submitted_params_json=params,
        yaml_override_text=yaml_override,
        image_digest=pin.digest,
        image_pin_source=pin.source,
        wcecoli_git_sha=wcecoli_sha,
        wcm_ui_git_sha=wcm_ui_sha,
        content_hash=content_hash,
        hash_version=provenance.HASH_VERSION,
        deterministic=provenance.is_deterministic(resolved_params),
        gcs_params_uri=gcs_params_uri,
        schema_version=SCHEMA_VERSION,
        submitter=submitter,
        client_ip_hash=client_ip_hash,
        client=firestore_client,
    )

    request = cloudrun.build_run_request(
        target,
        run_id=run_id,
        resolved_params=resolved_params,
        image_uri=image_uri,
        wall_clock_cap_sec=wall_clock_cap_sec,
    )

    try:
        execution_name = cloudrun.run_job(jobs_client, request)
    except Exception as exc:
        refund = isinstance(exc, _PROVABLY_NOT_STARTED)
        if refund:
            # Nothing is running and nothing will, so record the terminal state
            # rather than leaving the document in 'queued' forever.
            db.mark_failed(
                run_id, f"could not start the run: {exc}", None,
                failure_source="infrastructure",
            )
        # Otherwise leave it queued: the execution may exist, and
        # reconciliation resolves it against the Executions API. Marking it
        # failed here could contradict a run that is actually working.
        raise LaunchFailed(str(exc), refund_daily=refund) from exc

    db.set_execution(run_id, execution_name, client=firestore_client)

    return SubmitResult(
        ok=True,
        run_id=run_id,
        execution_name=execution_name,
        resolved_params=resolved_params,
        content_hash=content_hash,
        deterministic=provenance.is_deterministic(resolved_params),
        image_uri=image_uri,
        console_url=cloudrun.console_url(target, execution_name),
    )


# ---------------------------------------------------------------------------
# Response assembly
# ---------------------------------------------------------------------------

#: Poll cadence by state, mirrored in the router. None once terminal.
_POLL_MS = {"queued": 5_000, "running": 10_000}


def _artifact(available: bool, url: Optional[str] = None,
              reason: Optional[str] = None, expires_at=None):
    from api.models import ArtifactInfo
    return ArtifactInfo(available=available, url=url, reason=reason,
                        expires_at=expires_at)


def build_run_detail(doc: dict[str, Any], settings) -> "Any":
    """Assemble the poll response from a run document.

    Deliberately performs **no GCS calls**. Availability is derived from the
    run's state and the URIs already recorded, and tarball expiry is computed
    from ``finished_at`` plus the retention window. Polling happens every few
    seconds for the life of a run, so a round trip to GCS per poll would be the
    dominant cost of the whole service — and would buy nothing the download
    endpoint doesn't already check authoritatively.
    """
    from datetime import timedelta

    from api.models import RunArtifacts, RunDetail, RunProvenance

    run_id = doc["run_id"]
    state = doc.get("state", "queued")
    succeeded = state == "succeeded"
    failed = state == "failed"
    terminal = succeeded or failed

    started_at = doc.get("started_at")
    finished_at = doc.get("finished_at")
    duration = None
    if isinstance(started_at, datetime_type) and isinstance(finished_at, datetime_type):
        duration = (finished_at - started_at).total_seconds()

    # The tarball is the only artifact the bucket lifecycle rule deletes, so it
    # is the only one that can be "expired" rather than simply absent.
    tarball_expires = None
    tarball_expired = False
    if isinstance(finished_at, datetime_type):
        tarball_expires = finished_at + timedelta(
            days=settings.tarball_retention_days)
        tarball_expired = tarball_expires < _utcnow()

    base = f"/api/runs/{run_id}"
    if succeeded:
        timeseries = _artifact(True, f"{base}/timeseries.parquet")
        tarball = (
            _artifact(False, reason="expired", expires_at=tarball_expires)
            if tarball_expired
            else _artifact(True, f"{base}/download/tarball",
                           expires_at=tarball_expires)
        )
    else:
        reason = "never_produced" if failed else "not_ready"
        timeseries = _artifact(False, reason=reason)
        tarball = _artifact(False, reason=reason)

    # params.json is uploaded by the worker at the end of a successful run.
    params_art = (_artifact(True, f"{base}/download/params") if succeeded
                  else _artifact(False, reason="not_ready" if not failed
                                 else "never_produced"))
    # stderr exists only on failure — and not even then for a validation-time
    # failure, where nothing ran and there was nothing to capture.
    stderr_art = (_artifact(True, f"{base}/download/stderr")
                  if doc.get("gcs_stderr_uri")
                  else _artifact(False, reason="never_produced"))

    image_uri = doc.get("image_uri")
    gcs_params_uri = doc.get("gcs_params_uri")
    provenance_complete = bool(
        doc.get("image_digest") and doc.get("wcecoli_git_sha")
        and doc.get("wcm_ui_git_sha")
    )
    reproduce = None
    if image_uri and gcs_params_uri:
        reproduce = provenance.reproduce_command(
            image_uri=image_uri, gcs_params_uri=gcs_params_uri)

    return RunDetail(
        run_id=run_id,
        state=state,
        created_at=doc.get("created_at"),
        started_at=started_at,
        finished_at=finished_at,
        duration_sec=duration,
        params=doc.get("params_json") or {},
        submitted_params=doc.get("submitted_params_json"),
        yaml_override=doc.get("yaml_override_text"),
        error_message=doc.get("error_message"),
        failure_source=doc.get("failure_source"),
        attempt=doc.get("attempt"),
        provenance=RunProvenance(
            image_uri=image_uri,
            image_digest=doc.get("image_digest"),
            image_pin_source=doc.get("image_pin_source"),
            wcecoli_git_sha=doc.get("wcecoli_git_sha"),
            wcm_ui_git_sha=doc.get("wcm_ui_git_sha"),
            content_hash=doc.get("content_hash"),
            hash_version=doc.get("hash_version"),
            deterministic=doc.get("deterministic"),
            complete=provenance_complete,
            reproduce_command=reproduce,
        ),
        artifacts=RunArtifacts(
            timeseries=timeseries, tarball=tarball,
            params=params_art, stderr=stderr_art,
        ),
        poll_after_ms=None if terminal else _POLL_MS.get(state, 10_000),
        schema_version=doc.get("schema_version"),
    )
