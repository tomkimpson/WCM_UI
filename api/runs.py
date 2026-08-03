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

from dataclasses import dataclass, field
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
