"""Typed application errors and the handlers that render them.

Every failure leaves through ``ErrorResponse``, so the frontend has one shape to
parse rather than a mix of FastAPI's ``{"detail": …}`` and ad-hoc dicts.

The catch-all logs the traceback server-side and returns a bare 500. A public
unauthenticated endpoint must not echo internals: a Firestore traceback names
the project, the collection, and the client library version.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from api.models import ErrorResponse

log = logging.getLogger("wcm_ui.api")


class ApiError(Exception):
    """An error with an intended HTTP status and machine-readable slug."""

    http_status = 500
    code = "internal_error"

    def __init__(self, message: str, *, retry_after_sec: Optional[int] = None,
                 errors=None):
        super().__init__(message)
        self.message = message
        self.retry_after_sec = retry_after_sec
        self.errors = errors


class RunNotFound(ApiError):
    """404 must mean exactly one thing: we have never heard of this run id."""

    http_status = 404
    code = "run_not_found"


class InvalidParams(ApiError):
    http_status = 422
    code = "invalid_params"


class RunNotComplete(ApiError):
    """409, not 404: the run exists, its artifacts do not exist yet."""

    http_status = 409
    code = "run_not_complete"


class ArtifactGone(ApiError):
    """410: the object existed and the lifecycle rule deleted it.

    Distinct from 409 so the UI can say "expired" rather than "not ready" —
    the bucket deletes output.tar.gz on a schedule while the run document
    keeps pointing at it.
    """

    http_status = 410
    code = "artifact_gone"


class UpstreamError(ApiError):
    """502: a GCP call we depend on refused. Not the caller's fault."""

    http_status = 502
    code = "upstream_error"


def _response(err: ApiError, request_id: Optional[str]) -> JSONResponse:
    body = ErrorResponse(
        error=err.code,
        message=err.message,
        errors=err.errors,
        retry_after_sec=err.retry_after_sec,
        request_id=request_id,
    )
    headers = {}
    if err.retry_after_sec is not None:
        headers["Retry-After"] = str(err.retry_after_sec)
    return JSONResponse(status_code=err.http_status,
                        content=body.model_dump(mode="json", exclude_none=True),
                        headers=headers)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return _response(exc, getattr(request.state, "request_id", None))


async def quota_error_handler(request: Request, exc) -> JSONResponse:
    """Render api.quota's errors, which know their own status and slug."""
    wrapped = ApiError(str(exc), retry_after_sec=getattr(exc, "retry_after_sec", None))
    wrapped.http_status = exc.http_status
    wrapped.code = exc.code
    return _response(wrapped, getattr(request.state, "request_id", None))


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    # Traceback to the logs, nothing but an id to the caller.
    log.exception("unhandled error on %s %s (request_id=%s)",
                  request.method, request.url.path, request_id)
    err = ApiError("an internal error occurred")
    return _response(err, request_id)
