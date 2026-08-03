"""The run_v2 adapter: build a job execution request and launch it.

Two properties here are load-bearing rather than cosmetic, and each has a test
that exists to stop a regression rather than to describe behaviour:

  - Overrides.timeout is set. That is what makes the wall-clock cap
    platform-enforced, which in turn is what makes the quota lease TTL sound —
    a lease older than cap + grace is only *provably* dead if the platform
    really killed the task.
  - run_job does not wait. The API must never block on a simulation.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from api import cloudrun

TARGET = cloudrun.JobTarget(project="wcm-ui-dev", region="us-central1",
                            job="wcm-ui-worker-dev")


def test_job_name_is_the_full_resource_path():
    assert cloudrun.job_name(TARGET) == (
        "projects/wcm-ui-dev/locations/us-central1/jobs/wcm-ui-worker-dev"
    )


def test_request_carries_the_three_worker_env_overrides():
    request = cloudrun.build_run_request(
        TARGET, run_id="abc-123",
        resolved_params={"simulation": {"length_sec": 30}},
        image_uri="reg/worker@sha256:aaa",
        wall_clock_cap_sec=2700,
    )
    env = {e.name: e.value for e in request.overrides.container_overrides[0].env}
    assert env["RUN_ID"] == "abc-123"
    assert json.loads(env["PARAMS_JSON"]) == {"simulation": {"length_sec": 30}}
    assert env["IMAGE_URI"] == "reg/worker@sha256:aaa"


def test_request_targets_the_named_job():
    request = cloudrun.build_run_request(
        TARGET, run_id="abc-123", resolved_params={},
        image_uri="reg/w:latest", wall_clock_cap_sec=2700,
    )
    assert request.name == cloudrun.job_name(TARGET)


def test_request_sets_the_platform_enforced_timeout():
    """Overrides.timeout is why the wall-clock cap is real, not advisory.

    Verified against google-cloud-run 0.16.0: Overrides has fields
    (container_overrides, task_count, timeout), and proto-plus accepts and
    returns a timedelta for the Duration.
    """
    request = cloudrun.build_run_request(
        TARGET, run_id="abc-123", resolved_params={},
        image_uri="reg/w:latest", wall_clock_cap_sec=2700,
    )
    assert request.overrides.timeout.total_seconds() == 2700


def test_timeout_is_omitted_when_no_cap_is_given():
    """Without a cap the job spec's own --task-timeout applies."""
    request = cloudrun.build_run_request(
        TARGET, run_id="abc-123", resolved_params={},
        image_uri="reg/w:latest", wall_clock_cap_sec=None,
    )
    assert not request.overrides.timeout.total_seconds()


def test_image_cannot_be_overridden_per_execution():
    """Documents the constraint the whole provenance design works around.

    ContainerOverride has no `image` field, so the API cannot pin a digest at
    submit time — whatever the Job spec holds is what runs. If this ever starts
    failing, google-cloud-run has grown the field and resolve_image_pin can
    become an enforced pin instead of a read-back.
    """
    from google.cloud import run_v2
    fields = run_v2.RunJobRequest.Overrides.ContainerOverride.meta.fields
    assert "image" not in fields, (
        "ContainerOverride grew an `image` field — revisit api/provenance.py, "
        "which currently reads the digest back off the Job spec because it "
        "cannot set it"
    )


def test_run_job_returns_the_execution_name_without_waiting():
    """The API must not block on a ~26-minute simulation.

    run_job() returns a long-running operation; reading .metadata.name is
    immediate, whereas .result() would wait for the execution to finish.
    """
    client = MagicMock()
    operation = MagicMock()
    operation.metadata.name = (
        "projects/wcm-ui-dev/locations/us-central1/jobs/wcm-ui-worker-dev"
        "/executions/wcm-ui-worker-dev-abcde"
    )
    client.run_job.return_value = operation

    name = cloudrun.run_job(client, MagicMock())

    assert name.endswith("/executions/wcm-ui-worker-dev-abcde")
    operation.result.assert_not_called()


def test_console_url_points_at_the_execution():
    url = cloudrun.console_url(
        TARGET,
        "projects/p/locations/us-central1/jobs/j/executions/exec-xyz",
    )
    assert "exec-xyz" in url
    assert "wcm-ui-dev" in url


def test_get_job_asks_for_the_configured_job():
    client = MagicMock()
    cloudrun.get_job(client, TARGET)
    assert client.get_job.call_args.kwargs["name"] == cloudrun.job_name(TARGET)


def test_get_job_returns_none_when_the_job_is_missing():
    """A provenance read must not be able to fail a submission."""
    from google.api_core.exceptions import NotFound
    client = MagicMock()
    client.get_job.side_effect = NotFound("nope")
    assert cloudrun.get_job(client, TARGET) is None


def test_get_job_returns_none_on_permission_denied():
    """The likeliest production failure: the API SA lacks run.jobs.get.

    Degrade to unresolved provenance rather than refusing to launch runs.
    """
    from google.api_core.exceptions import PermissionDenied
    client = MagicMock()
    client.get_job.side_effect = PermissionDenied("no run.jobs.get")
    assert cloudrun.get_job(client, TARGET) is None


def test_get_execution_returns_none_when_missing():
    from google.api_core.exceptions import NotFound
    client = MagicMock()
    client.get_execution.side_effect = NotFound("gone")
    assert cloudrun.get_execution(client, "projects/p/.../executions/e") is None


@pytest.mark.parametrize("cap", [0, -1])
def test_a_nonpositive_cap_is_rejected(cap):
    """A zero or negative timeout would mean 'kill immediately' or be ignored."""
    with pytest.raises(ValueError):
        cloudrun.build_run_request(
            TARGET, run_id="x", resolved_params={},
            image_uri="reg/w:latest", wall_clock_cap_sec=cap,
        )
