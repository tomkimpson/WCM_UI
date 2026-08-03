"""Ceilings, and the cross-field checks JSON Schema cannot express.

The schema validates each knob independently, so
`generations x init_sims x length_sec` at their individual maxima passes
cleanly while describing an enormous run. These checks run before any counter
is touched and do no I/O, so a doomed request costs milliseconds rather than
a quota slot and a 26-minute simulation.
"""
from __future__ import annotations

import pytest

from api import quota


@pytest.fixture
def ceilings():
    return quota.Ceilings()


# --- defaults -------------------------------------------------------------

def test_defaults_match_the_derived_cost_model(ceilings):
    """These numbers are load-bearing; changing one changes the worst-case bill.

    The global daily cap is the only ceiling that bounds monthly spend: at 2
    concurrent runs and ~26 minutes each, concurrency alone permits ~221
    runs/day.
    """
    assert ceilings.max_runs_per_day == 8
    assert ceilings.max_concurrent_runs == 2
    assert ceilings.max_runs_per_ip_per_day == 3
    assert ceilings.wall_clock_cap_sec == 2700
    assert ceilings.lease_grace_sec == 900


def test_every_ceiling_is_env_configurable(monkeypatch):
    monkeypatch.setenv("WCM_MAX_RUNS_PER_DAY", "20")
    monkeypatch.setenv("WCM_MAX_CONCURRENT_RUNS", "4")
    monkeypatch.setenv("WCM_MAX_RUNS_PER_IP_PER_DAY", "5")
    monkeypatch.setenv("WCM_WALL_CLOCK_CAP_SEC", "3600")
    c = quota.Ceilings.from_env()
    assert (c.max_runs_per_day, c.max_concurrent_runs) == (20, 4)
    assert (c.max_runs_per_ip_per_day, c.wall_clock_cap_sec) == (5, 3600)


def test_a_non_numeric_env_override_is_rejected_loudly(monkeypatch):
    """A typo'd ceiling must not silently fall back to the default."""
    monkeypatch.setenv("WCM_MAX_RUNS_PER_DAY", "lots")
    with pytest.raises(ValueError, match="WCM_MAX_RUNS_PER_DAY"):
        quota.Ceilings.from_env()


def test_the_lease_ttl_is_the_cap_plus_the_grace(ceilings):
    """A lease older than this is provably dead, because Overrides.timeout
    makes the platform kill the task at the cap."""
    assert ceilings.lease_ttl_sec == 2700 + 900


# --- the wall-clock clamp -------------------------------------------------

def test_an_oversized_request_is_clamped_down(ceilings):
    assert quota.clamp_wall_clock(99999, ceilings) == 2700


def test_a_modest_request_is_left_alone(ceilings):
    assert quota.clamp_wall_clock(600, ceilings) == 600


def test_no_request_gets_the_cap(ceilings):
    assert quota.clamp_wall_clock(None, ceilings) == 2700


def test_the_clamp_only_ever_reduces(ceilings):
    """A user-settable cap is an input to be bounded, never trusted upward."""
    for requested in (1, 60, 2699, 2700, 2701, 10**9):
        assert quota.clamp_wall_clock(requested, ceilings) <= 2700


# --- cross-field budget checks --------------------------------------------

def test_default_params_are_within_budget(ceilings):
    quota.check_compute_budget({"simulation": {
        "length_sec": 60, "seed": 0, "generations": 1,
        "init_sims": 1, "parca_cpus": 1,
    }}, ceilings)


def test_multi_generation_is_rejected_with_a_reason(ceilings):
    """postprocess cannot represent more than one simOut directory.

    Admitting one burns a full simulation to manufacture a guaranteed failure,
    so reject at admission rather than 26 minutes later.
    """
    with pytest.raises(quota.ParamsExceedBudget, match="multi-generation"):
        quota.check_compute_budget(
            {"simulation": {"generations": 2}}, ceilings)


def test_multiple_init_sims_is_rejected(ceilings):
    with pytest.raises(quota.ParamsExceedBudget, match="multi-generation"):
        quota.check_compute_budget({"simulation": {"init_sims": 2}}, ceilings)


def test_oversubscribed_parca_cpus_is_rejected(ceilings):
    """The schema allows 16; the job is provisioned with 4 vCPU.

    Above the vCPU count parca gets slower, not faster, so this is pure waste.
    """
    with pytest.raises(quota.ParamsExceedBudget, match="parca_cpus"):
        quota.check_compute_budget({"simulation": {"parca_cpus": 8}}, ceilings)


def test_parca_cpus_at_the_vcpu_count_is_allowed(ceilings):
    quota.check_compute_budget({"simulation": {"parca_cpus": 4}}, ceilings)


def test_an_absurd_length_sec_is_rejected_early(ceilings):
    """An honest filter, not the real enforcement.

    The wall-clock cap is what actually stops a long run. This only rejects
    requests certain to hit it, so they fail in milliseconds instead of after
    45 minutes of billed compute.
    """
    with pytest.raises(quota.ParamsExceedBudget, match="length_sec"):
        quota.check_compute_budget({"simulation": {"length_sec": 86400}}, ceilings)


def test_budget_errors_are_client_errors_not_rate_limits(ceilings):
    """400, not 429: waiting will not make these parameters acceptable."""
    with pytest.raises(quota.ParamsExceedBudget) as exc:
        quota.check_compute_budget({"simulation": {"generations": 4}}, ceilings)
    assert exc.value.http_status == 400
    assert exc.value.code


def test_budget_check_does_no_io(ceilings, monkeypatch):
    """It runs before any reservation, so it must not touch Firestore."""
    monkeypatch.setattr(quota, "_client",
                        lambda: pytest.fail("budget check touched Firestore"))
    quota.check_compute_budget({"simulation": {"length_sec": 30}}, ceilings)
