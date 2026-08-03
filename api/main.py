"""The FastAPI application.

``create_app`` is a factory rather than a module-level ``app`` so tests can
inject settings; ``app`` at the bottom is what uvicorn imports.

One rule for every route in this package: handlers are **sync ``def``, never
``async def``**. Every GCP client here (firestore, storage, run_v2) is blocking
gRPC. A blocking call inside an ``async def`` handler stalls the event loop and
collapses effective concurrency to one, while a sync handler is run in
Starlette's threadpool for free. The Cloud Run service is configured with
``--concurrency=40`` to match that threadpool's default size.
"""
from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from api import errors, quota
from api.routers import artifacts as artifacts_router
from api.routers import meta as meta_router
from api.routers import runs as runs_router
from api.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="WCM_UI API",
        version=settings.api_version,
        description="Run the wcEcoli whole-cell model on cloud compute.",
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        # An explicit list, never ["*"]. The frontend is on a different origin,
        # so CORS is doing real work here.
        allow_origins=list(settings.allowed_origins),
        # No cookies are used, so credentialed CORS would be pure added risk.
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type"],
        expose_headers=["Retry-After", "Location", "ETag"],
    )

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    app.add_exception_handler(errors.ApiError, errors.api_error_handler)
    app.add_exception_handler(quota.QuotaError, errors.quota_error_handler)
    app.add_exception_handler(Exception, errors.unhandled_error_handler)

    app.include_router(meta_router.router)
    app.include_router(runs_router.router)
    app.include_router(artifacts_router.router)

    return app


app = create_app()
