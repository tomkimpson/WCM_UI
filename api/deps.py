"""Client factories and FastAPI dependency providers.

Each factory is the seam tests override, either with
``app.dependency_overrides`` or by monkeypatching the module attribute. Clients
are cached because constructing one re-does credential discovery and opens a
fresh gRPC channel — invisible in a batch job, per-request waste in a service.
"""
from __future__ import annotations

from functools import lru_cache

from api.settings import Settings, get_settings


def firestore_client():
    """Shared with the worker, so the two cannot point at different databases."""
    from worker import db
    return db._client()


@lru_cache(maxsize=1)
def jobs_client():
    from google.cloud import run_v2
    return run_v2.JobsClient()


@lru_cache(maxsize=1)
def executions_client():
    from google.cloud import run_v2
    return run_v2.ExecutionsClient()


@lru_cache(maxsize=1)
def storage_client():
    from google.cloud import storage
    return storage.Client()


def settings_dep() -> Settings:
    return get_settings()
