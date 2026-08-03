"""Artifact access: proxying, signed URLs, and the three flavours of absent.

The signing tests assert on the KWARGS passed to generate_signed_url rather
than on a real signature. That verifies our code, not Google's crypto, and needs
no credentials — so it runs in the fast suite. The parts a unit test genuinely
cannot reach (whether signBlob is permitted, whether the signer identity can
read the bucket) are covered by the manual smoke procedure, because each has a
different production-only failure mode.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api import artifacts as art
from api import deps, quota
from api.main import create_app
from api.settings import Settings
from tests.fakes import FakeFirestore
from worker import db

NOW = datetime.now(timezone.utc)
DAY = NOW.strftime("%Y-%m-%d")
SETTINGS = Settings(gcp_project="wcm-ui-test", runs_bucket="wcm-ui-runs-test",
                    signed_url_ttl_sec=900)

SIGNED = "https://storage.googleapis.com/signed?X-Goog-Signature=abc"


@pytest.fixture
def store(monkeypatch):
    fake = FakeFirestore(now=NOW)
    monkeypatch.setattr(db, "_client", lambda: fake)
    monkeypatch.setattr(quota, "_client", lambda: fake)
    quota.reset_flag_cache()
    return fake


@pytest.fixture
def blob():
    b = MagicMock()
    b.size = 1024
    b.generate_signed_url.return_value = SIGNED
    handle = MagicMock()
    handle.read.side_effect = [b"parquet-bytes", b""]
    b.open.return_value.__enter__.return_value = handle
    return b


@pytest.fixture
def storage(blob):
    client = MagicMock()
    client.bucket.return_value.blob.return_value = blob
    return client


@pytest.fixture
def client(store, storage, monkeypatch):
    monkeypatch.setattr(deps, "storage_client", lambda: storage)
    monkeypatch.setattr(deps, "signing_credentials", lambda: MagicMock())
    monkeypatch.setattr(deps, "executions_client", lambda: MagicMock())
    monkeypatch.delenv("IP_HASH_SALT", raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    quota.ip_hash_salt.cache_clear()
    return TestClient(create_app(SETTINGS), raise_server_exceptions=False)


def _seed(store, state="succeeded", **extra):
    doc = {"state": state, "created_at": NOW, "params_json": {}}
    if state in ("succeeded", "failed"):
        doc["finished_at"] = NOW
    if state == "succeeded":
        doc["gcs_tarball_uri"] = "gs://wcm-ui-runs-test/r1/output.tar.gz"
        doc["gcs_parquet_uri"] = "gs://wcm-ui-runs-test/r1/timeseries.parquet"
        doc["gcs_params_uri"] = "gs://wcm-ui-runs-test/r1/params.json"
    doc.update(extra)
    store.seed("runs", "r1", doc)


# --- gs:// URI parsing ----------------------------------------------------

def test_parse_gs_uri_splits_bucket_and_object():
    assert art.parse_gs_uri("gs://b/r1/output.tar.gz") == ("b", "r1/output.tar.gz")


@pytest.mark.parametrize("bad", ["https://b/x", "gs://b", "gs://", "b/x"])
def test_parse_gs_uri_rejects_anything_else(bad):
    with pytest.raises(ValueError):
        art.parse_gs_uri(bad)


def test_a_malformed_recorded_uri_falls_back_to_convention():
    """A bad stored URI should not make an artifact permanently unreachable."""
    doc = {"run_id": "r1", "gcs_tarball_uri": "not-a-uri"}
    assert art.object_location(doc, "tarball", "bucket") == (
        "bucket", "r1/output.tar.gz")


# --- the parquet proxy ----------------------------------------------------

def test_the_parquet_is_proxied_as_bytes(client, store):
    """Proxied, not redirected: a cross-origin fetch of a signed URL would need
    CORS on the bucket, which is separate infrastructure that would silently
    break plotting the day it drifted."""
    _seed(store)
    r = client.get("/api/runs/r1/timeseries.parquet")
    assert r.status_code == 200
    assert r.content == b"parquet-bytes"
    assert r.headers["content-type"].startswith("application/vnd.apache.parquet")


def test_the_parquet_is_cached_immutably(client, store):
    """A succeeded run's timeseries never changes, so a shared link should cost
    one read ever."""
    _seed(store)
    r = client.get("/api/runs/r1/timeseries.parquet")
    assert "immutable" in r.headers["cache-control"]


def test_the_parquet_of_a_running_run_is_409(client, store):
    """409, not 404: the run exists, the artifact does not exist yet."""
    _seed(store, state="running")
    r = client.get("/api/runs/r1/timeseries.parquet")
    assert r.status_code == 409
    assert r.json()["error"] == "run_not_complete"


def test_the_parquet_of_an_unknown_run_is_404(client):
    assert client.get("/api/runs/nope/timeseries.parquet").status_code == 404


def test_a_deleted_parquet_is_410_not_404(client, store, storage):
    """410 distinguishes "was here, now gone" from "never heard of it"."""
    _seed(store)
    storage.bucket.return_value.blob.return_value = None
    from google.api_core.exceptions import NotFound
    blob = MagicMock()
    blob.reload.side_effect = NotFound("gone")
    storage.bucket.return_value.blob.return_value = blob

    r = client.get("/api/runs/r1/timeseries.parquet")
    assert r.status_code == 410
    assert r.json()["error"] == "artifact_gone"


# --- signed-URL downloads -------------------------------------------------

def test_download_redirects_to_a_signed_url(client, store):
    _seed(store)
    r = client.get("/api/runs/r1/download/tarball", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == SIGNED


def test_the_signed_url_is_a_v4_get_with_the_configured_ttl(client, store, blob):
    _seed(store)
    client.get("/api/runs/r1/download/tarball", follow_redirects=False)
    kwargs = blob.generate_signed_url.call_args.kwargs
    assert kwargs["version"] == "v4"
    assert kwargs["method"] == "GET"
    assert kwargs["expiration"] == timedelta(seconds=900)


def test_the_signed_url_forces_a_download_with_a_useful_filename(client, store, blob):
    _seed(store)
    client.get("/api/runs/r1/download/tarball", follow_redirects=False)
    disposition = blob.generate_signed_url.call_args.kwargs["response_disposition"]
    assert "attachment" in disposition
    assert "r1-output.tar.gz" in disposition


def test_signing_passes_the_iam_signblob_arguments(client, store, blob):
    """ADC on Cloud Run has no private key, so signing must be delegated.

    Both service_account_email and access_token have to be present or
    google-cloud-storage takes the credentials.signer path, which
    compute_engine.Credentials does not implement.
    """
    _seed(store)
    client.get("/api/runs/r1/download/tarball", follow_redirects=False)
    kwargs = blob.generate_signed_url.call_args.kwargs
    assert "service_account_email" in kwargs
    assert "access_token" in kwargs


def test_credentials_are_refreshed_before_signing(client, store, monkeypatch):
    """compute_engine.Credentials starts with service_account_email == "default"
    and only resolves the real address during refresh. Signing without it means
    signBlob 404s for an account called default@…"""
    creds = MagicMock()
    monkeypatch.setattr(deps, "signing_credentials", lambda: creds)
    _seed(store)
    client.get("/api/runs/r1/download/tarball", follow_redirects=False)
    creds.refresh.assert_called_once()


def test_the_redirect_itself_is_not_cached(client, store):
    """The target expires, so a cached 307 would send a later visitor to a
    dead URL."""
    _seed(store)
    r = client.get("/api/runs/r1/download/tarball", follow_redirects=False)
    assert r.headers["cache-control"] == "no-store"


def test_stderr_is_downloadable_for_a_failed_run(client, store):
    _seed(store, state="failed",
          gcs_stderr_uri="gs://wcm-ui-runs-test/r1/stderr.log")
    r = client.get("/api/runs/r1/download/stderr", follow_redirects=False)
    assert r.status_code == 307


def test_stderr_of_a_succeeded_run_is_409(client, store):
    """A successful run produces no stderr artifact, which is not an error."""
    _seed(store)
    r = client.get("/api/runs/r1/download/stderr", follow_redirects=False)
    assert r.status_code == 409


def test_an_unknown_artifact_name_is_rejected(client, store):
    _seed(store)
    assert client.get("/api/runs/r1/download/bogus").status_code == 404


# --- egress metering ------------------------------------------------------

def test_minting_a_url_charges_the_object_size(client, store):
    """Egress is $0.12/GB and the compute ceilings do nothing about it."""
    _seed(store)
    client.get("/api/runs/r1/download/tarball", follow_redirects=False)
    assert store.doc("quota_days", DAY)["egress_bytes"] == 1024


def test_the_daily_egress_cap_refuses_further_downloads(client, store):
    _seed(store)
    ceilings = quota.Ceilings()
    store.seed("quota_days", DAY, {
        "egress_bytes": ceilings.max_egress_gib_per_day * (1024 ** 3),
        "egress_by_ip": {},
    })
    r = client.get("/api/runs/r1/download/tarball", follow_redirects=False)
    assert r.status_code == 429
    assert r.json()["error"] == "egress_limit_reached"


def test_egress_is_charged_before_the_url_is_minted(client, store, blob):
    """Charge first so a refusal cannot hand out a usable URL."""
    _seed(store)
    ceilings = quota.Ceilings()
    store.seed("quota_days", DAY, {
        "egress_bytes": ceilings.max_egress_gib_per_day * (1024 ** 3),
        "egress_by_ip": {},
    })
    client.get("/api/runs/r1/download/tarball", follow_redirects=False)
    blob.generate_signed_url.assert_not_called()


def test_the_egress_counter_carries_the_ttl_field(client, store):
    _seed(store)
    client.get("/api/runs/r1/download/tarball", follow_redirects=False)
    assert store.doc("quota_days", DAY)["expire_at"] > NOW
