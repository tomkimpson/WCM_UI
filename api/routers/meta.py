"""Health, schema, and config.

``/api/schema`` and ``/api/config`` are split for one reason: cache semantics.
The schema changes when we deploy and is safe to cache for minutes; the kill
switch must never be cached at the edge, or turning the service off would take
effect only after the caches drained. Merging them would force the stricter
policy on both.
"""
from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache

from fastapi import APIRouter, Request, Response

from api import provenance, quota
from api.models import ConfigResponse, HealthResponse, SchemaResponse
from api.params import (
    DEFAULTS_PATH,
    LOOSE_SCHEMA_PATH,
    STRICT_SCHEMA_PATH,
    SUPPORTED_NAMESPACES,
)
from api.runs import SCHEMA_VERSION

router = APIRouter()


@lru_cache(maxsize=1)
def _schema_payload() -> SchemaResponse:
    strict = json.loads(STRICT_SCHEMA_PATH.read_text())
    loose = json.loads(LOOSE_SCHEMA_PATH.read_text())
    defaults = json.loads(DEFAULTS_PATH.read_text())

    # JSON object key order is not a contract, so the form's field order is
    # declared rather than inferred. Derived from the schema so a new knob
    # cannot be invisible in the UI.
    field_order = [
        f"simulation.{name}"
        for name in strict["properties"]["simulation"]["properties"]
    ]
    return SchemaResponse(
        schema_version=SCHEMA_VERSION,
        strict_schema=strict,
        loose_schema=loose,
        defaults=defaults,
        field_order=field_order,
        supported_namespaces=sorted(SUPPORTED_NAMESPACES),
    )


@lru_cache(maxsize=1)
def _schema_etag() -> str:
    payload = _schema_payload().model_dump_json()
    return '"' + hashlib.sha256(payload.encode()).hexdigest()[:16] + '"'


@router.get("/healthz", response_model=HealthResponse)
def healthz(request: Request) -> HealthResponse:
    """Liveness, and enough identity to tell which revision answered.

    Deliberately touches no backing service: it is the Cloud Run startup
    probe's target, and a probe that fails when Firestore hiccups would
    roll back a perfectly good revision.
    """
    wcm_ui_sha, _ = provenance.git_shas()
    return HealthResponse(
        status="ok",
        api_version=request.app.state.settings.api_version,
        # K_REVISION is injected by Cloud Run; its absence means local.
        revision=os.environ.get("K_REVISION", "local"),
        wcm_ui_git_sha=wcm_ui_sha,
    )


@router.get("/api/schema", response_model=SchemaResponse)
def get_schema(request: Request, response: Response):
    """The form definition.

    Served from the API rather than bundled into the frontend so the form and
    the validator cannot drift: a schema change ships to both at once.
    """
    etag = _schema_etag()
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "public, max-age=300"
    return _schema_payload()


@router.get("/api/config", response_model=ConfigResponse)
def get_config(request: Request, response: Response) -> ConfigResponse:
    """Limits and the kill switch, so the UI can disable submit up front.

    ``no-store`` is deliberate: a cached "yes we're accepting runs" would keep
    the submit button live after the switch was thrown.
    """
    from datetime import datetime, timezone

    settings = request.app.state.settings
    ceilings = quota.Ceilings.from_env()
    try:
        accepting, message = quota.accepting_runs(
            now=datetime.now(timezone.utc), ceilings=ceilings)
    except quota.NotAcceptingRuns as exc:
        # Report it as a value rather than a 503: the frontend wants to render
        # a banner, and failing this endpoint would leave it with nothing to say.
        accepting, message = False, str(exc)

    response.headers["Cache-Control"] = "no-store"
    return ConfigResponse(
        accepting_runs=accepting,
        message=message,
        max_concurrent_runs=ceilings.max_concurrent_runs,
        max_runs_per_day=ceilings.max_runs_per_day,
        max_runs_per_ip_per_day=ceilings.max_runs_per_ip_per_day,
        wall_clock_cap_sec=ceilings.wall_clock_cap_sec,
        tarball_retention_days=settings.tarball_retention_days,
        signed_url_ttl_sec=settings.signed_url_ttl_sec,
        api_version=settings.api_version,
    )
