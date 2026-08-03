"""Runtime configuration, read from the environment.

A frozen dataclass with a ``from_env`` classmethod rather than
pydantic-settings: it is about thirty lines, and this repo has been
deliberately spare about dependencies.

Field names deliberately reuse the env vars the worker already reads —
``GCP_PROJECT``, ``RUNS_BUCKET``, ``FIRESTORE_DATABASE`` — so ``up.sh`` sets one
block for both containers instead of two that can disagree.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from api.cloudrun import JobTarget

#: Bumped by hand; surfaced on /healthz so a deployed revision is identifiable.
API_VERSION = "0.1.0"


def _split_origins(raw: str) -> tuple[str, ...]:
    return tuple(o.strip() for o in raw.split(",") if o.strip())


@dataclass(frozen=True)
class Settings:
    gcp_project: Optional[str] = None
    runs_bucket: Optional[str] = None
    firestore_database: str = "(default)"
    worker_job_name: str = "wcm-ui-worker-dev"
    worker_job_region: str = "us-central1"
    #: Explicit list, never "*". The frontend is on a different origin, so CORS
    #: is load-bearing, and a wildcard would make this public endpoint callable
    #: from any page a user happens to visit.
    allowed_origins: tuple[str, ...] = ("http://localhost:5173",)
    signed_url_ttl_sec: int = 900
    #: Advisory only — the bucket lifecycle rule is the real deleter. Used to
    #: tell the UI a tarball has probably expired without paying a GCS call on
    #: every status poll.
    tarball_retention_days: int = 7
    #: How stale a non-terminal run must look before we ask Cloud Run about it.
    reconcile_min_age_sec: int = 90
    reconcile_recheck_sec: int = 60
    #: Grace for the worker's terminal write to land before we call a completed
    #: execution a failure.
    reconcile_completion_grace_sec: int = 60
    api_version: str = API_VERSION

    @property
    def job_target(self) -> JobTarget:
        return JobTarget(
            project=self.gcp_project or "",
            region=self.worker_job_region,
            job=self.worker_job_name,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        def _int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer, got {raw!r}") from exc

        return cls(
            gcp_project=os.environ.get("GCP_PROJECT") or None,
            runs_bucket=os.environ.get("RUNS_BUCKET") or None,
            firestore_database=os.environ.get("FIRESTORE_DATABASE", "(default)"),
            worker_job_name=os.environ.get("WORKER_JOB_NAME", "wcm-ui-worker-dev"),
            worker_job_region=os.environ.get("WORKER_JOB_REGION", "us-central1"),
            allowed_origins=_split_origins(
                os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173")),
            signed_url_ttl_sec=_int("SIGNED_URL_TTL_SECONDS", 900),
            tarball_retention_days=_int("TARBALL_RETENTION_DAYS", 7),
            reconcile_min_age_sec=_int("RECONCILE_MIN_AGE_SEC", 90),
            reconcile_recheck_sec=_int("RECONCILE_RECHECK_SEC", 60),
            reconcile_completion_grace_sec=_int(
                "RECONCILE_COMPLETION_GRACE_SEC", 60),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
