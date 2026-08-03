"""Submit a run, preview a submission, and poll a run's status.

There is no ``GET /api/runs`` collection endpoint, deliberately. ``run_id`` is a
uuid4 that doubles as the share link, so it functions as an unguessable
capability — and an enumerable listing would hand out every run.

There are also no write endpoints for the worker. The worker writes its own
state directly to Firestore; a public unauthenticated heartbeat or PATCH keyed
on ``run_id`` would let anyone with a share link mark someone else's run failed
or inject fake progress.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request, Response, status

from api import deps, errors, quota, reconcile
from api.models import (
    FieldError,
    RunCreatedResponse,
    RunDetail,
    SubmitRequest,
    ValidationResponse,
)
from api.params import resolve_params
from api.runs import LaunchFailed, submit_run
from api import runs as runs_service

router = APIRouter(prefix="/api")

#: Server-controlled poll cadence, so the interval can be widened without a
#: frontend deploy. None once terminal — a settled run never changes again.
_POLL_MS = {"queued": 5_000, "running": 10_000}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _client_ip_hash(request: Request, ceilings: quota.Ceilings,
                    day_key: str) -> Optional[str]:
    ip = quota.client_ip(request.headers.get("x-forwarded-for"),
                         trusted_hops=ceilings.trusted_proxy_hops)
    if ip is None:
        return None
    return quota.ip_hash(ip, day_key=day_key,
                         ipv6_prefix_bits=ceilings.ipv6_prefix_bits)


@router.post("/runs/validate", response_model=ValidationResponse)
def validate(payload: SubmitRequest) -> ValidationResponse:
    """Preview a submission without spending anything.

    Always 200, with the verdict in ``valid`` — a syntax error while typing is
    not an HTTP failure. Touches neither Firestore nor Cloud Run, and consumes
    no quota, so the YAML editor can call it freely on every keystroke.

    Returns ``content_hash`` when valid so the UI can point out that these exact
    parameters have already been run.
    """
    result = resolve_params(payload.params, payload.yaml_override)
    if not result.ok:
        return ValidationResponse(
            valid=False,
            errors=[FieldError.from_data(e) for e in result.errors],
        )

    from api import provenance
    resolved = result.resolved or {}
    return ValidationResponse(
        valid=True,
        resolved_params=resolved,
        # No image lookup here: this endpoint must stay I/O-free, so the hash is
        # over params alone and will differ from the stored one. Enough to spot
        # a duplicate submission, not a provenance claim.
        content_hash=provenance.content_hash(resolved, None),
        deterministic=provenance.is_deterministic(resolved),
    )


@router.post("/runs", response_model=RunCreatedResponse,
             status_code=status.HTTP_202_ACCEPTED)
def create_run(payload: SubmitRequest, request: Request,
               response: Response) -> RunCreatedResponse:
    """Launch a simulation.

    202, not 201: nothing yet exists that the caller can use, only an accepted
    intention. ``Location`` still points at the poll URL.

    Order matters and is the reason this reads the way it does:

      1. resolve and validate — so a malformed request cannot consume a slot;
      2. check the compute budget — I/O-free, so a doomed run fails in
         milliseconds;
      3. check the kill switch;
      4. reserve quota;
      5. launch, releasing the reservation if the launch fails.
    """
    settings = request.app.state.settings
    ceilings = quota.Ceilings.from_env()
    now = _now()
    day_key = quota.day_key_for(now)

    # 1. Validate first. A rejected submission must leave no trace and consume
    #    no quota.
    resolved = resolve_params(payload.params, payload.yaml_override)
    if not resolved.ok:
        raise errors.InvalidParams(
            "the parameters could not be used",
            errors=[FieldError.from_data(e) for e in resolved.errors],
        )
    resolved_params = resolved.resolved or {}

    # 2. Cross-field limits the schema cannot express. Raises ParamsExceedBudget
    #    (400), handled by the quota error handler.
    quota.check_compute_budget(resolved_params, ceilings)

    # 3. The kill switch. Raises NotAcceptingRuns (503) if it cannot be read.
    accepting, message = quota.accepting_runs(now=now, ceilings=ceilings)
    if not accepting:
        raise quota.NotAcceptingRuns(
            message or "the service is not accepting new runs right now")

    ip_hash = _client_ip_hash(request, ceilings, day_key)
    run_id = str(uuid.uuid4())

    # 4. Reserve. Raises a QuotaError subclass carrying its own status and
    #    Retry-After.
    lease = quota.reserve(run_id, ip_hash=ip_hash, now=now, ceilings=ceilings)

    # 5. Launch. Any failure gives the slot back.
    try:
        result = submit_run(
            params=payload.params,
            yaml_override=payload.yaml_override,
            run_id=run_id,
            firestore_client=deps.firestore_client(),
            jobs_client=deps.jobs_client(),
            target=settings.job_target,
            submitter="api",
            wall_clock_cap_sec=ceilings.wall_clock_cap_sec,
            client_ip_hash=ip_hash,
            runs_bucket=settings.runs_bucket,
        )
    except LaunchFailed as exc:
        quota.release(lease, refund_daily=exc.refund_daily, now=_now())
        raise errors.UpstreamError(
            f"could not start the run: {exc}") from exc
    except Exception:
        # Anything unexpected: give the slot back rather than leaking it, then
        # let the catch-all handler log and return a 500.
        quota.release(lease, refund_daily=True, now=_now())
        raise

    if not result.ok:
        # resolve_params already succeeded above, so this is defensive only.
        quota.release(lease, refund_daily=True, now=_now())
        raise errors.InvalidParams(
            "the parameters could not be used",
            errors=[FieldError.from_data(e) for e in result.errors],
        )

    response.headers["Location"] = f"/api/runs/{run_id}"
    return RunCreatedResponse(
        run_id=run_id,
        state="queued",
        resolved_params=resolved_params,
        content_hash=result.content_hash,
        deterministic=result.deterministic,
        image_uri=result.image_uri,
        poll_after_ms=_POLL_MS["queued"],
    )


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, request: Request, response: Response) -> RunDetail:
    """Poll a run.

    Reconciles opportunistically: a run that looks stalled gets one Executions
    API call to find out whether it actually died. Observing a terminal state
    also frees the run's quota lease, which is the fast path that keeps capacity
    from waiting on the TTL sweep.
    """
    from worker import db

    settings = request.app.state.settings
    ceilings = quota.Ceilings.from_env()
    now = _now()

    doc = db.get_run(run_id)
    if doc is None:
        # 404 means exactly one thing: we have never heard of this run.
        raise errors.RunNotFound(f"no run with id {run_id}")

    if reconcile.needs_reconcile(
        doc, now,
        min_age_sec=settings.reconcile_min_age_sec,
        recheck_interval_sec=settings.reconcile_recheck_sec,
    ):
        new_state = reconcile.reconcile(
            doc, now=now,
            executions_client=deps.executions_client(),
            wall_clock_cap_sec=ceilings.wall_clock_cap_sec,
            completion_grace_sec=settings.reconcile_completion_grace_sec,
        )
        if new_state and new_state != doc.get("state"):
            doc = db.get_run(run_id) or doc

    if doc.get("state") in db.TERMINAL_STATES:
        # Fast path back to free capacity: don't make the next submitter wait
        # out the lease TTL for a run we can already see has finished.
        quota.finish(run_id, now=now)

    response.headers["Cache-Control"] = "no-store"
    return runs_service.build_run_detail(doc, settings)
