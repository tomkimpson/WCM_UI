"""The reconciliation verdict table, branch by branch.

``verdict_from_execution`` is pure, so every row below is reachable from a
``SimpleNamespace`` stub with no mocks and no I/O. That is the reason it was
written as a pure function: the policy is the part that is easy to get subtly
wrong, and it deserves tests that read like the table they implement.

What this protects is the **UI**, not the quota ceiling. A run killed by
infrastructure has its slot reclaimed by the lease TTL regardless; this is what
stops the page claiming a run is still going an hour after it died.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api.reconcile import (
    SUBMIT_GRACE_SEC,
    needs_reconcile,
    verdict_from_execution,
)

T0 = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
CAP = 2700
GRACE = 60


def _doc(state="running", *, age_sec=600, execution_name="projects/p/x",
         last_reconciled_at=None):
    doc = {
        "run_id": "r1",
        "state": state,
        "created_at": T0 - timedelta(seconds=age_sec),
        "started_at": T0 - timedelta(seconds=age_sec),
    }
    if execution_name:
        doc["execution_name"] = execution_name
    if last_reconciled_at:
        doc["last_reconciled_at"] = last_reconciled_at
    return doc


def _execution(*, completion_time=None, succeeded=0, failed=0, cancelled=0,
               retried=0):
    return SimpleNamespace(
        completion_time=completion_time,
        succeeded_count=succeeded,
        failed_count=failed,
        cancelled_count=cancelled,
        retried_count=retried,
    )


def _verdict(execution, doc, **kw):
    return verdict_from_execution(
        execution, doc, T0,
        wall_clock_cap_sec=CAP, completion_grace_sec=GRACE, **kw)


# --- the cheap gates ------------------------------------------------------

@pytest.mark.parametrize("state", ["succeeded", "failed"])
def test_a_terminal_run_is_never_reconciled(state):
    """Settled is settled. Re-asking would cost a call per poll forever."""
    assert not needs_reconcile(_doc(state), T0, min_age_sec=90,
                               recheck_interval_sec=60)


def test_a_young_run_is_not_reconciled():
    """A run that just started has not had time to report anything."""
    assert not needs_reconcile(_doc(age_sec=10), T0, min_age_sec=90,
                               recheck_interval_sec=60)


def test_a_stale_run_is_reconciled():
    assert needs_reconcile(_doc(age_sec=600), T0, min_age_sec=90,
                           recheck_interval_sec=60)


def test_a_recently_checked_run_is_not_re_checked():
    """Rate-limited per document, or every poll would pay for an API call."""
    doc = _doc(age_sec=600, last_reconciled_at=T0 - timedelta(seconds=5))
    assert not needs_reconcile(doc, T0, min_age_sec=90, recheck_interval_sec=60)


def test_a_run_checked_long_ago_is_re_checked():
    doc = _doc(age_sec=600, last_reconciled_at=T0 - timedelta(seconds=600))
    assert needs_reconcile(doc, T0, min_age_sec=90, recheck_interval_sec=60)


# --- the verdict table ----------------------------------------------------

def test_no_execution_name_within_the_grace_period_waits():
    """run_job() may simply not have returned yet."""
    doc = _doc(age_sec=30, execution_name=None)
    assert _verdict(None, doc).action == "wait"


def test_no_execution_name_past_the_grace_period_fails():
    doc = _doc(age_sec=SUBMIT_GRACE_SEC + 10, execution_name=None)
    v = _verdict(None, doc)
    assert v.should_fail
    assert "never reached Cloud Run" in v.message


def test_a_missing_execution_fails():
    """Deleted, or never created. Either way nothing is running."""
    v = _verdict(None, _doc(), execution_missing=True)
    assert v.should_fail
    assert "no longer exists" in v.message


def test_an_incomplete_execution_within_the_cap_waits():
    """Cloud Run says it's still going, and it is inside its time budget."""
    assert _verdict(_execution(), _doc(age_sec=600)).action == "wait"


def test_an_incomplete_execution_far_past_the_cap_fails():
    """Only reachable if Overrides.timeout itself failed to fire."""
    doc = _doc(age_sec=CAP + SUBMIT_GRACE_SEC + 10)
    v = _verdict(_execution(), doc)
    assert v.should_fail
    assert "wall-clock cap" in v.message


def test_a_cancelled_execution_fails():
    v = _verdict(_execution(completion_time=T0, cancelled=1), _doc())
    assert v.should_fail
    assert "cancelled" in v.message


def test_a_failed_task_fails_and_names_the_likely_causes():
    """The worker died before writing, so the message has to speculate — but
    usefully, and it should say it is speculating."""
    v = _verdict(_execution(completion_time=T0, failed=1, retried=1), _doc())
    assert v.should_fail
    assert "failed=1" in v.message
    assert "memory" in v.message


def test_a_just_completed_success_waits_for_the_worker_s_write():
    """The container exited 0; its Firestore write may still be in flight.

    Declaring failure here would contradict a run that is about to report
    success, and mark_succeeded is not transactional.
    """
    just_now = T0 - timedelta(seconds=GRACE - 10)
    v = _verdict(_execution(completion_time=just_now, succeeded=1), _doc())
    assert v.action == "wait"


def test_a_long_completed_success_that_never_reported_fails():
    """Exited 0 but wrote nothing, and the grace period has passed."""
    long_ago = T0 - timedelta(seconds=GRACE + 60)
    v = _verdict(_execution(completion_time=long_ago, succeeded=1), _doc())
    assert v.should_fail
    assert "never recorded" in v.message


def test_an_execution_with_no_counts_waits():
    """Unknown rather than dead — don't kill a run on missing telemetry."""
    v = _verdict(_execution(completion_time=T0), _doc())
    assert v.action == "wait"


def test_the_verdict_function_performs_no_io(monkeypatch):
    """Purity is the property that makes the table above trustworthy."""
    import api.reconcile as mod
    monkeypatch.setattr(mod, "verdict_from_execution", verdict_from_execution)
    # No client is constructed and none is needed: the call takes only data.
    assert _verdict(_execution(), _doc()).action == "wait"
