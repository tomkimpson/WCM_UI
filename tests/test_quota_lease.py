"""Admission control: reserve a slot, then release or finish it.

The concurrency ceiling is a **map of leases**, not an integer. That choice is
what makes the rest work:

  - releasing is `del leases[run_id]`, so it is idempotent and a double release
    is harmless;
  - a *missed* release is visible — you can see which run is stuck and for how
    long — instead of silently corrupting a counter;
  - reconciliation needs no cron, no query and no index, because every reserve()
    first evicts leases past their expiry.

That last point is only sound because Overrides.timeout makes the platform kill
the task at the wall-clock cap, so a lease older than cap + grace is *provably*
dead rather than probably dead. The eviction tests below are the stranded-run
proof, and they run instantly because `now` is injected everywhere.

These tests do NOT prove concurrency — FakeTransaction applies writes serially.
See tests/test_quota_concurrency.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api import quota
from tests.fakes import FakeFirestore
from worker import db

T0 = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
IP_A = "aaaaaaaaaaaaaaaa"
IP_B = "bbbbbbbbbbbbbbbb"


@pytest.fixture
def ceilings():
    return quota.Ceilings()


@pytest.fixture
def store(monkeypatch):
    fake = FakeFirestore(now=T0)
    monkeypatch.setattr(db, "_client", lambda: fake)
    monkeypatch.setattr(quota, "_client", lambda: fake)
    return fake


def _reserve(run_id, ip_hash=IP_A, now=T0, ceilings=None):
    return quota.reserve(run_id, ip_hash=ip_hash, now=now,
                         ceilings=ceilings or quota.Ceilings())


# --- the happy path -------------------------------------------------------

def test_reserve_records_a_lease_and_increments_both_counters(store, ceilings):
    lease = _reserve("run-1")

    assert lease.run_id == "run-1"
    inflight = store.doc("quota", "inflight")
    assert set(inflight["leases"]) == {"run-1"}

    day = store.doc("quota_days", "2026-08-02")
    assert day["runs_started"] == 1
    assert day["by_ip"][IP_A] == 1


def test_the_lease_expires_at_the_cap_plus_the_grace(store, ceilings):
    lease = _reserve("run-1")
    assert lease.expires_at == T0 + timedelta(seconds=ceilings.lease_ttl_sec)


def test_the_daily_document_carries_a_ttl_field(store):
    """Firestore's TTL policy deletes on expire_at, so IP-derived data is
    removed by the platform rather than by a cron we might forget."""
    _reserve("run-1")
    assert store.doc("quota_days", "2026-08-02")["expire_at"] > T0


def test_the_day_key_is_utc(store):
    """A local-timezone boundary would move the cap unpredictably."""
    _reserve("run-1", now=datetime(2026, 8, 2, 23, 59, tzinfo=timezone.utc))
    assert store.doc("quota_days", "2026-08-02") is not None


# --- the three ceilings ---------------------------------------------------

def test_concurrency_limit_refuses_a_further_run(store, ceilings):
    for i in range(ceilings.max_concurrent_runs):
        _reserve(f"run-{i}")

    with pytest.raises(quota.AtConcurrencyLimit) as exc:
        _reserve("run-overflow")

    assert exc.value.http_status == 429
    assert exc.value.retry_after_sec is not None


def test_concurrency_retry_after_points_at_the_next_expiry(store, ceilings):
    """Telling the client to retry needs a time that will actually help."""
    for i in range(ceilings.max_concurrent_runs):
        _reserve(f"run-{i}")
    with pytest.raises(quota.AtConcurrencyLimit) as exc:
        _reserve("run-overflow")
    assert 0 < exc.value.retry_after_sec <= ceilings.lease_ttl_sec


def test_global_daily_cap_refuses_even_with_free_concurrency(store):
    """The ceiling that actually bounds the monthly bill."""
    c = quota.Ceilings(max_runs_per_day=3, max_concurrent_runs=99,
                       max_runs_per_ip_per_day=99)
    for i in range(3):
        quota.reserve(f"run-{i}", ip_hash=IP_A, now=T0, ceilings=c)
        quota.finish(f"run-{i}", now=T0)

    with pytest.raises(quota.DailyGlobalLimitReached) as exc:
        quota.reserve("run-4", ip_hash=IP_A, now=T0, ceilings=c)
    assert exc.value.http_status == 429


def test_global_daily_retry_after_is_the_time_until_utc_midnight(store):
    c = quota.Ceilings(max_runs_per_day=1, max_concurrent_runs=99)
    quota.reserve("run-0", ip_hash=IP_A, now=T0, ceilings=c)
    quota.finish("run-0", now=T0)
    with pytest.raises(quota.DailyGlobalLimitReached) as exc:
        quota.reserve("run-1", ip_hash=IP_A, now=T0, ceilings=c)
    assert exc.value.retry_after_sec == 12 * 3600  # T0 is noon UTC


def test_per_ip_cap_refuses_the_fourth_run_from_one_address(store):
    c = quota.Ceilings(max_runs_per_ip_per_day=3, max_concurrent_runs=99,
                       max_runs_per_day=99)
    for i in range(3):
        quota.reserve(f"run-{i}", ip_hash=IP_A, now=T0, ceilings=c)
        quota.finish(f"run-{i}", now=T0)

    with pytest.raises(quota.DailyIpLimitReached):
        quota.reserve("run-4", ip_hash=IP_A, now=T0, ceilings=c)


def test_a_different_address_is_unaffected_by_another_s_cap(store):
    c = quota.Ceilings(max_runs_per_ip_per_day=3, max_concurrent_runs=99,
                       max_runs_per_day=99)
    for i in range(3):
        quota.reserve(f"a-{i}", ip_hash=IP_A, now=T0, ceilings=c)
        quota.finish(f"a-{i}", now=T0)

    lease = quota.reserve("b-0", ip_hash=IP_B, now=T0, ceilings=c)
    assert lease.run_id == "b-0"


def test_the_error_codes_differ_so_the_ui_can_word_them_differently(store):
    """"we're busy, retry in 15 min" and "you've had your 3 today" are
    different messages, and both are 429."""
    assert quota.AtConcurrencyLimit.code != quota.DailyIpLimitReached.code
    assert quota.DailyGlobalLimitReached.code != quota.DailyIpLimitReached.code


def test_a_missing_ip_hash_still_counts_against_the_global_cap(store):
    """An unparseable X-Forwarded-For must not be a way past the ceilings."""
    c = quota.Ceilings(max_runs_per_day=1, max_concurrent_runs=99)
    quota.reserve("run-0", ip_hash=None, now=T0, ceilings=c)
    quota.finish("run-0", now=T0)
    with pytest.raises(quota.DailyGlobalLimitReached):
        quota.reserve("run-1", ip_hash=None, now=T0, ceilings=c)


# --- stale-lease eviction (the stranded-run proof) ------------------------

def test_a_lease_past_its_ttl_is_evicted_on_the_next_reserve(store, ceilings):
    """The service must not be able to wedge closed.

    A run killed by Cloud Run infrastructure never releases its lease — the
    worker cannot run code through a SIGKILL. Nothing else reclaims it, so
    reserve() has to.
    """
    for i in range(ceilings.max_concurrent_runs):
        _reserve(f"stuck-{i}")

    later = T0 + timedelta(seconds=ceilings.lease_ttl_sec + 1)
    lease = _reserve("fresh", now=later)

    assert lease.run_id == "fresh"
    assert set(store.doc("quota", "inflight")["leases"]) == {"fresh"}


def test_a_lease_one_second_before_its_ttl_is_not_evicted(store, ceilings):
    """The boundary in the other direction: don't evict a live run."""
    for i in range(ceilings.max_concurrent_runs):
        _reserve(f"live-{i}")

    almost = T0 + timedelta(seconds=ceilings.lease_ttl_sec - 1)
    with pytest.raises(quota.AtConcurrencyLimit):
        _reserve("too-soon", now=almost)


def test_eviction_does_not_refund_the_daily_count(store, ceilings):
    """The compute was spent, so the day's budget was spent with it."""
    _reserve("stuck")
    later = T0 + timedelta(seconds=ceilings.lease_ttl_sec + 1)
    _reserve("fresh", now=later)
    assert store.doc("quota_days", "2026-08-02")["runs_started"] == 2


# --- release and finish ---------------------------------------------------

def test_release_with_refund_undoes_the_reservation_entirely(store):
    """A launch that provably never started must not consume quota."""
    lease = _reserve("run-1")
    quota.release(lease, refund_daily=True, now=T0)

    assert store.doc("quota", "inflight")["leases"] == {}
    day = store.doc("quota_days", "2026-08-02")
    assert day["runs_started"] == 0
    assert day["by_ip"].get(IP_A, 0) == 0


def test_release_without_refund_frees_only_the_slot(store):
    """Used when we cannot prove the job didn't start. When in doubt, charge:
    over-charging annoys one user, under-charging is an unbounded bill."""
    lease = _reserve("run-1")
    quota.release(lease, refund_daily=False, now=T0)

    assert store.doc("quota", "inflight")["leases"] == {}
    assert store.doc("quota_days", "2026-08-02")["runs_started"] == 1


def test_finish_frees_the_slot_and_keeps_the_daily_charge(store):
    lease = _reserve("run-1")
    quota.finish(lease.run_id, now=T0)

    assert store.doc("quota", "inflight")["leases"] == {}
    assert store.doc("quota_days", "2026-08-02")["runs_started"] == 1


def test_finish_is_idempotent(store):
    """Called from the status read path, which may fire many times."""
    _reserve("run-1")
    quota.finish("run-1", now=T0)
    quota.finish("run-1", now=T0)
    quota.finish("run-1", now=T0)
    assert store.doc("quota", "inflight")["leases"] == {}


def test_finish_on_an_unknown_run_is_harmless(store):
    quota.finish("never-existed", now=T0)


def test_counters_never_go_negative(store):
    """A double refund must not manufacture free capacity."""
    lease = _reserve("run-1")
    quota.release(lease, refund_daily=True, now=T0)
    quota.release(lease, refund_daily=True, now=T0)
    day = store.doc("quota_days", "2026-08-02")
    assert day["runs_started"] >= 0
    assert day["by_ip"].get(IP_A, 0) >= 0


def test_a_freed_slot_can_be_reused_immediately(store, ceilings):
    for i in range(ceilings.max_concurrent_runs):
        _reserve(f"run-{i}")
    quota.finish("run-0", now=T0)
    assert _reserve("run-new").run_id == "run-new"


# --- the kill switch ------------------------------------------------------

def test_accepting_runs_defaults_to_true_when_the_flag_doc_is_missing(store, ceilings):
    """A missing document is a provisioning gap, not a signal.

    Failing closed on it would make a freshly provisioned project
    inexplicably refuse every run.
    """
    quota.reset_flag_cache()
    accepting, message = quota.accepting_runs(now=T0, ceilings=ceilings)
    assert accepting is True
    assert message == ""


def test_accepting_runs_reads_the_flag(store, ceilings):
    store.seed("config", "global", {"accepting_runs": False,
                                    "message": "at capacity for the month"})
    quota.reset_flag_cache()
    accepting, message = quota.accepting_runs(now=T0, ceilings=ceilings)
    assert accepting is False
    assert message == "at capacity for the month"


def test_the_flag_is_cached_for_the_ttl(store, ceilings):
    """One Firestore read per instance per 30 s, not one per request."""
    store.seed("config", "global", {"accepting_runs": True, "message": ""})
    quota.reset_flag_cache()
    quota.accepting_runs(now=T0, ceilings=ceilings)
    reads_after_first = len(store.reads)
    quota.accepting_runs(now=T0 + timedelta(seconds=5), ceilings=ceilings)
    assert len(store.reads) == reads_after_first, "second call should hit the cache"


def test_the_cache_expires(store, ceilings):
    store.seed("config", "global", {"accepting_runs": True, "message": ""})
    quota.reset_flag_cache()
    quota.accepting_runs(now=T0, ceilings=ceilings)
    reads_after_first = len(store.reads)
    later = T0 + timedelta(seconds=ceilings.flag_cache_ttl_sec + 1)
    quota.accepting_runs(now=later, ceilings=ceilings)
    assert len(store.reads) > reads_after_first


def test_a_read_failure_with_no_cached_value_fails_closed(store, ceilings, monkeypatch):
    """If Firestore is unreachable we cannot check the ceilings either.

    reserve() would fail regardless, so refusing up front is both correct and
    more honest than a 500.
    """
    def boom():
        raise RuntimeError("firestore unreachable")
    monkeypatch.setattr(quota, "_client", boom)
    quota.reset_flag_cache()
    with pytest.raises(quota.NotAcceptingRuns) as exc:
        quota.accepting_runs(now=T0, ceilings=ceilings)
    assert exc.value.http_status == 503


def test_a_read_failure_serves_a_stale_cached_value(store, ceilings, monkeypatch):
    """Availability beats freshness for a value that changes once a month."""
    store.seed("config", "global", {"accepting_runs": True, "message": ""})
    quota.reset_flag_cache()
    quota.accepting_runs(now=T0, ceilings=ceilings)

    def boom():
        raise RuntimeError("firestore unreachable")
    monkeypatch.setattr(quota, "_client", boom)
    later = T0 + timedelta(seconds=ceilings.flag_cache_ttl_sec + 1)
    accepting, _ = quota.accepting_runs(now=later, ceilings=ceilings)
    assert accepting is True


# --- consistency with the rest of the codebase ---------------------------

def test_the_runs_collection_name_agrees_with_worker_db():
    """Two modules now know the document shape; catch the drift cheaply."""
    assert quota._COLLECTION_RUNS == db._COLLECTION
