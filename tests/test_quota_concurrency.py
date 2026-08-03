"""Prove the quota transaction against a real Firestore.

Everything else in the quota suite runs against tests/fakes.FakeTransaction,
which applies writes immediately and serially. That proves the read-modify-write
*logic* and says nothing whatsoever about contention — **mocking the client
mocks away the thing under test.** Two simultaneous submits are exactly the case
the ceilings exist for, so they need a real transaction.

Skipped unless FIRESTORE_EMULATOR_HOST is set:

    gcloud emulators firestore start --host-port=localhost:8080
    FIRESTORE_EMULATOR_HOST=localhost:8080 pytest tests/test_quota_concurrency.py -v

or `make quota-emulator`, which does both.

Honest limits of the emulator: it reliably catches "more winners than the
ceiling allows" and read-after-write ordering errors, which is the bug class
that matters here. It does not reproduce production lock timeouts or latency,
and it cannot show that Overrides.timeout really kills a task — which is the
assumption the lease TTL rests on. That one needs a live run; see
docs/stage3-quota-smoke.md.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="needs the Firestore emulator: make quota-emulator",
)

NOW = datetime.now(timezone.utc)
IP_A = "aaaaaaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def clean_emulator(monkeypatch):
    """Point the clients at the emulator and clear the quota documents."""
    monkeypatch.setenv("GCP_PROJECT", "wcm-ui-emulator-test")
    monkeypatch.setenv("IP_HASH_SALT", "emulator-test-salt")

    from worker import db
    db._cached_client.cache_clear()
    client = db._client()
    for coll, doc_id in (("quota", "inflight"),
                         ("quota_days", NOW.strftime("%Y-%m-%d"))):
        client.collection(coll).document(doc_id).delete()
    yield client
    db._cached_client.cache_clear()


def _try_reserve(run_id, ceilings):
    from api import quota
    try:
        quota.reserve(run_id, ip_hash=IP_A, now=NOW, ceilings=ceilings)
        return True
    except quota.QuotaError as exc:
        return exc


def test_only_max_concurrent_reservations_win_under_real_contention(clean_emulator):
    """20 threads race one transaction. Exactly max_concurrent may win.

    This is the only test in the suite that proves the transaction actually
    serialises. If the counter were a count() query instead of a locked
    document, a phantom insert between the count and the commit would let extra
    winners through and this is where it would show.
    """
    from api import quota
    ceilings = quota.Ceilings(max_concurrent_runs=2, max_runs_per_day=99,
                              max_runs_per_ip_per_day=99)

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda i: _try_reserve(f"run-{i}", ceilings),
                                range(20)))

    winners = [r for r in results if r is True]
    refused = [r for r in results if isinstance(r, quota.AtConcurrencyLimit)]
    assert len(winners) == 2, f"expected exactly 2 winners, got {len(winners)}"
    assert len(refused) == 18

    leases = (clean_emulator.collection("quota").document("inflight")
              .get().to_dict()["leases"])
    assert len(leases) == 2, "the stored state must agree with the verdicts"


def test_the_per_ip_cap_holds_under_contention(clean_emulator):
    """Same race, but on the daily map rather than the lease map."""
    from api import quota
    ceilings = quota.Ceilings(max_concurrent_runs=99, max_runs_per_day=99,
                              max_runs_per_ip_per_day=3)

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda i: _try_reserve(f"ip-{i}", ceilings),
                                range(20)))

    assert sum(1 for r in results if r is True) == 3


def test_the_global_daily_cap_holds_under_contention(clean_emulator):
    """The ceiling that actually bounds the monthly bill."""
    from api import quota
    ceilings = quota.Ceilings(max_concurrent_runs=99, max_runs_per_day=5,
                              max_runs_per_ip_per_day=99)

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda i: _try_reserve(f"day-{i}", ceilings),
                                range(20)))

    assert sum(1 for r in results if r is True) == 5


def test_concurrent_finishes_leave_no_negative_counters(clean_emulator):
    """finish() is called from the read path, so it races itself constantly."""
    from api import quota
    ceilings = quota.Ceilings(max_concurrent_runs=99, max_runs_per_day=99,
                              max_runs_per_ip_per_day=99)
    for i in range(5):
        quota.reserve(f"fin-{i}", ip_hash=IP_A, now=NOW, ceilings=ceilings)

    with ThreadPoolExecutor(max_workers=10) as pool:
        pool.map(lambda i: quota.finish(f"fin-{i % 5}", now=NOW), range(20))

    leases = (clean_emulator.collection("quota").document("inflight")
              .get().to_dict()["leases"])
    assert leases == {}

    day = (clean_emulator.collection("quota_days")
           .document(NOW.strftime("%Y-%m-%d")).get().to_dict())
    assert day["runs_started"] == 5, "finish must not refund the daily charge"
