"""Thin adapter over ``google.cloud.run_v2``.

All the run_v2 vocabulary lives here so the rest of the API talks in terms of
run ids and parameter dicts. Extracted from ``scripts/submit.py`` so the CLI and
the service share one launch path rather than drifting apart.

Two constraints from the proto, verified against google-cloud-run 0.16.0:

  - ``Overrides.timeout`` exists, so the per-run wall-clock cap is enforced by
    the platform rather than being advisory. Everything the quota lease TTL
    assumes rests on this.
  - ``Overrides.ContainerOverride`` has fields ``name``, ``args``, ``env``,
    ``clear_args`` — and **no** ``image``. The API therefore cannot pin an
    execution to a digest; whatever the Job spec holds is what Cloud Run pulls.
    CI pins the spec, and api/provenance.py reads it back.

Read helpers return ``None`` rather than raising on NotFound or
PermissionDenied. Both are best-effort: provenance and reconciliation must never
be able to fail a submission or a status poll.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from google.api_core.exceptions import GoogleAPICallError, NotFound, PermissionDenied


@dataclass(frozen=True)
class JobTarget:
    """Which Cloud Run Job to execute, and where."""

    project: str
    region: str
    job: str


def job_name(target: JobTarget) -> str:
    return (f"projects/{target.project}/locations/{target.region}"
            f"/jobs/{target.job}")


def build_run_request(
    target: JobTarget,
    *,
    run_id: str,
    resolved_params: dict[str, Any],
    image_uri: str,
    wall_clock_cap_sec: Optional[int],
):
    """A RunJobRequest carrying this run's parameters and its time limit.

    ``IMAGE_URI`` is passed for provenance only — the worker records it but the
    running image is whatever the Job spec pins (see the module docstring).
    """
    from google.cloud import run_v2

    if wall_clock_cap_sec is not None and wall_clock_cap_sec <= 0:
        # A zero Duration is indistinguishable from "unset" on the wire, and a
        # negative one is meaningless. Fail here rather than silently launching
        # a run with the job spec's much longer default.
        raise ValueError(
            f"wall_clock_cap_sec must be positive, got {wall_clock_cap_sec}"
        )

    env = [
        run_v2.EnvVar(name="RUN_ID", value=run_id),
        run_v2.EnvVar(name="PARAMS_JSON", value=json.dumps(resolved_params)),
        run_v2.EnvVar(name="IMAGE_URI", value=image_uri),
    ]
    overrides_kwargs: dict[str, Any] = {
        "container_overrides": [
            run_v2.RunJobRequest.Overrides.ContainerOverride(env=env)
        ],
    }
    if wall_clock_cap_sec is not None:
        overrides_kwargs["timeout"] = timedelta(seconds=wall_clock_cap_sec)

    return run_v2.RunJobRequest(
        name=job_name(target),
        overrides=run_v2.RunJobRequest.Overrides(**overrides_kwargs),
    )


def run_job(jobs_client, request) -> str:
    """Launch the execution and return its resource name.

    Reads ``operation.metadata.name`` and returns immediately. Calling
    ``.result()`` here would block until the simulation finished — tens of
    minutes — which is exactly what "the API never blocks on a sim" forbids.
    """
    operation = jobs_client.run_job(request=request)
    return operation.metadata.name


def get_job(jobs_client, target: JobTarget):
    """The Job resource, or None if it can't be read.

    None on PermissionDenied as well as NotFound: a missing ``run.jobs.get``
    grant should degrade provenance to "unresolved", not stop people running
    simulations.
    """
    try:
        return jobs_client.get_job(name=job_name(target))
    except (NotFound, PermissionDenied):
        return None
    except GoogleAPICallError:
        return None


def get_execution(executions_client, execution_name: str):
    """The Execution resource, or None if it can't be read.

    Used by reconciliation to distinguish "still running" from "died without
    telling us". A None here means "don't conclude anything", which the verdict
    logic treats separately from a definite NotFound.
    """
    try:
        return executions_client.get_execution(name=execution_name)
    except NotFound:
        return None
    except (PermissionDenied, GoogleAPICallError):
        return None


def console_url(target: JobTarget, execution_name: str) -> str:
    """A clickable link to the execution, for CLI output and logs."""
    execution_id = execution_name.rsplit("/", 1)[-1]
    return (
        "https://console.cloud.google.com/run/jobs/executions/details/"
        f"{target.region}/{execution_id}/tasks?project={target.project}"
    )
