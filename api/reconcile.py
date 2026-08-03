"""Detect runs the worker never finished reporting on.

A run can be stranded in ``running`` forever: an OOM kill, a SIGKILL at the task
timeout, or a Cloud Run infrastructure failure all stop the worker before it can
write a terminal state, and no other process is watching.

Reconciliation is **lazy, on the read path**, not a background sweeper. The only
observer that cares is the frontend polling ``GET /api/runs/{id}``, so that is
where the information is needed; a scheduler plus a reconciler service would add
a deployable and an IAM binding to serve nobody extra. It costs one
``get_execution`` call, only on documents that already look stale, rate-limited
per document by ``last_reconciled_at``.

Note what this is *for*. The quota lease TTL protects the concurrency ceiling
and is what stops the service wedging closed; this protects the **UI**, so a run
that died an hour ago stops claiming to be running. Conflating the two is what
produces a service that looks fine and accepts nothing.

The policy is a pure function so every branch is testable against a stub, with
the single impure wrapper doing the I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from worker.db import TERMINAL_STATES


@dataclass(frozen=True)
class ReconcileVerdict:
    """What to do about a run that looks stalled."""

    action: str                     # "wait" | "mark_failed"
    message: Optional[str] = None

    @property
    def should_fail(self) -> bool:
        return self.action == "mark_failed"


WAIT = ReconcileVerdict("wait")

#: How long after submission we still believe a document with no execution_name
#: is simply mid-flight rather than a launch that vanished.
SUBMIT_GRACE_SEC = 120


def _age_sec(doc: dict[str, Any], now: datetime) -> float:
    stamp = doc.get("started_at") or doc.get("created_at")
    if not isinstance(stamp, datetime):
        return 0.0
    return (now - stamp).total_seconds()


def needs_reconcile(doc: dict[str, Any], now: datetime, *,
                    min_age_sec: int, recheck_interval_sec: int) -> bool:
    """Whether this document is worth an Executions API call.

    Three cheap gates before we spend anything: terminal runs are settled, a
    young run is simply still starting, and a recently-checked run is not worth
    re-asking about on every poll.
    """
    if doc.get("state") in TERMINAL_STATES:
        return False
    if _age_sec(doc, now) < min_age_sec:
        return False
    last = doc.get("last_reconciled_at")
    if isinstance(last, datetime):
        if (now - last).total_seconds() < recheck_interval_sec:
            return False
    return True


def verdict_from_execution(
    execution: Any,
    doc: dict[str, Any],
    now: datetime,
    *,
    wall_clock_cap_sec: int,
    completion_grace_sec: int,
    execution_missing: bool = False,
) -> ReconcileVerdict:
    """Decide whether a stalled-looking run is actually dead.

    Pure: `execution` is only read for counts and a completion time, so every
    branch below is reachable from a stub.
    """
    if not doc.get("execution_name"):
        if _age_sec(doc, now) > SUBMIT_GRACE_SEC:
            return ReconcileVerdict(
                "mark_failed",
                "the submission never reached Cloud Run",
            )
        return WAIT

    if execution_missing or execution is None:
        # Either deleted, or never created. Both mean nothing is running.
        return ReconcileVerdict(
            "mark_failed",
            "the Cloud Run execution no longer exists",
        )

    completion_time = getattr(execution, "completion_time", None)
    if not completion_time:
        # Still going, as far as Cloud Run is concerned. Only overrule that if
        # it has outlived the platform-enforced cap by a clear margin, which
        # would mean the timeout itself failed to fire.
        if _age_sec(doc, now) > wall_clock_cap_sec + SUBMIT_GRACE_SEC:
            return ReconcileVerdict(
                "mark_failed",
                "the run exceeded its wall-clock cap without reporting",
            )
        return WAIT

    cancelled = int(getattr(execution, "cancelled_count", 0) or 0)
    failed = int(getattr(execution, "failed_count", 0) or 0)
    succeeded = int(getattr(execution, "succeeded_count", 0) or 0)

    if cancelled:
        return ReconcileVerdict("mark_failed", "the execution was cancelled")

    if failed:
        retried = int(getattr(execution, "retried_count", 0) or 0)
        return ReconcileVerdict(
            "mark_failed",
            f"the Cloud Run task failed without reporting "
            f"(failed={failed}, retried={retried}) — most likely out of "
            f"memory, the task timeout, or a preemption",
        )

    if succeeded:
        # The container exited 0 but never wrote a terminal state. Give its
        # Firestore write time to land before contradicting it.
        if isinstance(completion_time, datetime):
            since = (now - completion_time).total_seconds()
            if since < completion_grace_sec:
                return WAIT
        return ReconcileVerdict(
            "mark_failed",
            "the worker exited successfully but never recorded its results",
        )

    return WAIT


def reconcile(
    doc: dict[str, Any],
    *,
    now: datetime,
    executions_client,
    wall_clock_cap_sec: int,
    completion_grace_sec: int,
) -> Optional[str]:
    """Ask Cloud Run about a stalled run and settle it. Returns the new state.

    The only impure function here. Writes through ``db.mark_infra_failed``,
    which is transactional and refuses to overwrite a terminal state — so a
    worker's ``mark_succeeded`` racing this cannot be clobbered.
    """
    from api import cloudrun
    from worker import db

    run_id = doc["run_id"]
    execution_name = doc.get("execution_name")

    execution = None
    missing = False
    if execution_name:
        execution = cloudrun.get_execution(executions_client, execution_name)
        missing = execution is None

    verdict = verdict_from_execution(
        execution, doc, now,
        wall_clock_cap_sec=wall_clock_cap_sec,
        completion_grace_sec=completion_grace_sec,
        execution_missing=missing,
    )

    if verdict.should_fail:
        if db.mark_infra_failed(run_id, verdict.message or "run did not report"):
            return "failed"
        # Lost the race: the worker wrote a terminal state first, which is the
        # outcome we want. Re-read rather than guess.
        fresh = db.get_run(run_id)
        return (fresh or {}).get("state")

    db.touch_reconciled(run_id)
    return doc.get("state")
