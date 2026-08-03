"""Access to a run's GCS objects.

The bucket has uniform access and public-access-prevention, so the browser
cannot fetch anything directly. That makes the API the only route — which is
what lets egress be metered at all — and it means the two artifact types need
different treatment:

  - **The Parquet is proxied** as bytes through the API. A cross-origin
    ``fetch()`` of a GCS signed URL requires CORS configured on the *bucket*,
    which is a separate piece of infrastructure nobody will remember to set up
    or keep in sync. The file is small (124 rows for a 30-second run), so
    proxying costs little and removes that failure mode entirely.
  - **Everything else is a 307 to a signed URL**, consumed by
    ``<a href download>``. That is a top-level navigation, not subject to CORS,
    and it keeps a potentially large tarball out of the API's memory and off its
    egress path.

On signing: Application Default Credentials on Cloud Run are
``compute_engine.Credentials`` — a bearer token with no private key, so they
cannot sign. ``generate_signed_url`` has to be handed both
``service_account_email`` and ``access_token``, which routes it through
``iamcredentials…:signBlob``. See ``signed_url`` for the two non-obvious
requirements that follow from that.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional


@dataclass(frozen=True)
class ArtifactSpec:
    """How to find an artifact and when it is expected to exist."""

    filename: str
    uri_field: Optional[str]     # the run document field, when one is recorded
    content_type: str
    #: Which run states can have produced it.
    states: tuple[str, ...]


#: The full set the API will serve. Names are the URL path segment.
ARTIFACTS: dict[str, ArtifactSpec] = {
    "tarball": ArtifactSpec(
        "output.tar.gz", "gcs_tarball_uri", "application/gzip", ("succeeded",)),
    "timeseries": ArtifactSpec(
        "timeseries.parquet", "gcs_parquet_uri",
        "application/vnd.apache.parquet", ("succeeded",)),
    "params": ArtifactSpec(
        "params.json", "gcs_params_uri", "application/json", ("succeeded",)),
    "stderr": ArtifactSpec(
        "stderr.log", "gcs_stderr_uri", "text/plain", ("failed",)),
}


def parse_gs_uri(uri: str) -> tuple[str, str]:
    """``gs://bucket/a/b`` → ``("bucket", "a/b")``."""
    if not uri.startswith("gs://"):
        raise ValueError(f"not a gs:// URI: {uri!r}")
    rest = uri[len("gs://"):]
    bucket, _, name = rest.partition("/")
    if not bucket or not name:
        raise ValueError(f"incomplete gs:// URI: {uri!r}")
    return bucket, name


def object_location(doc: dict, artifact: str, default_bucket: Optional[str]
                    ) -> Optional[tuple[str, str]]:
    """Where an artifact lives, preferring the recorded URI over convention.

    The recorded URI is authoritative because it survives a bucket rename; the
    convention is the fallback for artifacts written before we started recording
    them (``gcs_params_uri`` is new in Stage 3).
    """
    spec = ARTIFACTS[artifact]
    uri = doc.get(spec.uri_field) if spec.uri_field else None
    if uri:
        try:
            return parse_gs_uri(uri)
        except ValueError:
            pass  # fall through to convention rather than failing the request
    if not default_bucket:
        return None
    return default_bucket, f"{doc['run_id']}/{spec.filename}"


def get_blob(storage_client, bucket: str, name: str):
    """The blob, with its metadata loaded, or None if it isn't there.

    ``reload()`` is what populates ``size``; a bare ``blob()`` is a local handle
    that has spoken to nobody. This is the authoritative existence check the
    status endpoint deliberately skips.
    """
    from google.api_core.exceptions import NotFound
    blob = storage_client.bucket(bucket).blob(name)
    try:
        blob.reload()
    except NotFound:
        return None
    return blob


def signed_url(storage_client, credentials, bucket: str, name: str, *,
               ttl_sec: int, filename: str) -> str:
    """A V4 signed GET URL, valid for ``ttl_sec``.

    Two requirements that are easy to get wrong and only fail in production:

    1. ``credentials.refresh()`` must be called first. On Cloud Run,
       ``compute_engine.Credentials`` initialises ``service_account_email`` to
       the literal string ``"default"`` and only resolves the real address
       during a refresh — sign without it and signBlob returns 404 for an
       account called ``default@…``.
    2. The API's service account needs ``storage.objects.get`` on the bucket.
       A signed URL authorises *as the signer* and GCS re-checks that identity's
       IAM when the URL is followed, so signing can succeed and the download
       still 403. Signing proving nothing about read access is the trap.

    Passing ``service_account_email`` and ``access_token`` routes signing
    through the IAM credentials API. When both come back None — impersonated or
    real service-account credentials locally — the library falls back to
    ``credentials.signer``, which those types do implement. One code path, two
    environments, no branching.
    """
    from google.auth.transport import requests as ga_requests

    credentials.refresh(ga_requests.Request())
    blob = storage_client.bucket(bucket).blob(name)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=ttl_sec),
        method="GET",
        response_disposition=f'attachment; filename="{filename}"',
        service_account_email=getattr(credentials, "service_account_email", None),
        access_token=getattr(credentials, "token", None),
    )
