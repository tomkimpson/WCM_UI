"""Serve a run's artifacts.

Status codes carry real distinctions here, because "not there" has three
different causes and the UI should say different things about each:

  404  we have never heard of this run
  409  the run exists but hasn't produced this yet, or never will
  410  it existed and the bucket lifecycle rule deleted it
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse

from api import artifacts as art
from api import deps, errors, quota

router = APIRouter(prefix="/api")

#: Streamed in chunks so a large object never lands in the instance's memory —
#: the API runs with 512 MiB.
_CHUNK = 256 * 1024


def _run_or_404(run_id: str) -> dict:
    from worker import db
    doc = db.get_run(run_id)
    if doc is None:
        raise errors.RunNotFound(f"no run with id {run_id}")
    return doc


def _locate(doc: dict, artifact: str, settings):
    """Resolve an artifact to a live blob, or explain why it isn't available."""
    spec = art.ARTIFACTS[artifact]
    state = doc.get("state", "queued")

    if state not in spec.states:
        raise errors.RunNotComplete(
            f"this run is {state}; its {artifact} is not available",
        )

    location = art.object_location(doc, artifact, settings.runs_bucket)
    if location is None:
        raise errors.RunNotComplete(f"no location recorded for {artifact}")

    bucket, name = location
    blob = art.get_blob(deps.storage_client(), bucket, name)
    if blob is None:
        # For the tarball this is the expected end state — the lifecycle rule
        # deletes it on a schedule while the run document keeps pointing at it.
        raise errors.ArtifactGone(
            f"the {artifact} for this run is no longer stored",
        )
    return bucket, name, blob


@router.get("/runs/{run_id}/timeseries.parquet")
def get_timeseries(run_id: str, request: Request):
    """Proxy the Parquet.

    Proxied rather than redirected because the frontend fetches this with
    JavaScript to draw plots, and a cross-origin fetch of a signed URL would
    need CORS configured on the bucket — separate infrastructure that would
    silently break plotting the day it drifted. The file is small enough that
    proxying is the cheaper answer.

    Cached hard: a succeeded run's timeseries never changes, so a shared link
    costs one read ever.
    """
    settings = request.app.state.settings
    doc = _run_or_404(run_id)
    _bucket, _name, blob = _locate(doc, "timeseries", settings)

    def chunks():
        with blob.open("rb") as fh:
            while True:
                data = fh.read(_CHUNK)
                if not data:
                    break
                yield data

    return StreamingResponse(
        chunks(),
        media_type=art.ARTIFACTS["timeseries"].content_type,
        headers={
            "Content-Disposition":
                f'inline; filename="{run_id}-timeseries.parquet"',
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


@router.get("/runs/{run_id}/download/{artifact}")
def download(run_id: str, artifact: str, request: Request) -> Response:
    """Redirect to a short-lived signed URL.

    A 307 consumed by ``<a href download>`` is a top-level navigation, so CORS
    never enters into it, and a multi-gigabyte tarball never passes through the
    API.

    The TTL is deliberately short. A long-lived signed URL is a public download
    link that can be posted anywhere, turning the bucket into an unmetered CDN.
    """
    settings = request.app.state.settings
    ceilings = quota.Ceilings.from_env()
    now = datetime.now(timezone.utc)

    if artifact not in art.ARTIFACTS:
        raise errors.RunNotFound(f"unknown artifact {artifact!r}")

    doc = _run_or_404(run_id)
    bucket, name, blob = _locate(doc, artifact, settings)

    # Charge before minting. Egress is the largest per-byte cost in the system
    # and the one the compute ceilings do nothing about. Charged optimistically
    # and never refunded: the URL may well be used, and over-charging is the
    # safe direction.
    ip = quota.client_ip(request.headers.get("x-forwarded-for"),
                         trusted_hops=ceilings.trusted_proxy_hops)
    ip_hash = (quota.ip_hash(ip, day_key=quota.day_key_for(now),
                             ipv6_prefix_bits=ceilings.ipv6_prefix_bits)
               if ip else None)
    quota.charge_egress(size_bytes=int(blob.size or 0), ip_hash=ip_hash,
                        now=now, ceilings=ceilings)

    url = art.signed_url(
        deps.storage_client(),
        deps.signing_credentials(),
        bucket, name,
        ttl_sec=settings.signed_url_ttl_sec,
        filename=f"{run_id}-{art.ARTIFACTS[artifact].filename}",
    )
    # no-store on the redirect itself: the target expires, so a cached 307 would
    # send a later visitor to a dead URL.
    return RedirectResponse(url, status_code=307,
                            headers={"Cache-Control": "no-store"})
