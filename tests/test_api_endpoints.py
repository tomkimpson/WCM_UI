"""HTTP behaviour: the contract Stage 4 consumes.

These drive the real app through TestClient with the GCP clients faked, so they
cover the wiring — status codes, headers, the error envelope, and the ordering
rule that a rejected submission consumes no quota.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import InvalidArgument

from api import deps, quota
from api.main import create_app
from api.settings import Settings
from tests.fakes import FakeFirestore
from worker import db

# Based on the wall clock, not a fixed instant. The routers call
# datetime.now(timezone.utc) directly, so a hardcoded "now" would drift: a
# seeded run would look stale enough for the reconciler to declare it dead, and
# the per-day quota document would be filed under a different UTC day. Tests
# that WANT staleness subtract from NOW explicitly.
NOW = datetime.now(timezone.utc)
DAY = NOW.strftime("%Y-%m-%d")
IMAGE = "reg/worker@sha256:deadbeef"

SETTINGS = Settings(
    gcp_project="wcm-ui-test",
    runs_bucket="wcm-ui-runs-test",
    allowed_origins=("https://wcm-ui.pages.dev",),
)


@pytest.fixture
def store(monkeypatch):
    fake = FakeFirestore(now=NOW)
    monkeypatch.setattr(db, "_client", lambda: fake)
    monkeypatch.setattr(quota, "_client", lambda: fake)
    quota.reset_flag_cache()
    return fake


@pytest.fixture
def jobs():
    client = MagicMock()
    op = MagicMock()
    op.metadata.name = "projects/p/locations/us-central1/jobs/j/executions/exec-1"
    client.run_job.return_value = op
    client.get_job.return_value = SimpleNamespace(
        template=SimpleNamespace(template=SimpleNamespace(
            containers=[SimpleNamespace(image=IMAGE)])))
    return client


@pytest.fixture
def client(store, jobs, monkeypatch):
    monkeypatch.setattr(deps, "jobs_client", lambda: jobs)
    monkeypatch.setattr(deps, "executions_client", lambda: MagicMock())
    monkeypatch.setattr(deps, "firestore_client", lambda: store)
    monkeypatch.delenv("IP_HASH_SALT", raising=False)
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    quota.ip_hash_salt.cache_clear()
    return TestClient(create_app(SETTINGS), raise_server_exceptions=False)


# --- meta -----------------------------------------------------------------

def test_healthz_is_ok_and_touches_no_backing_service(client, store):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert store.reads == [], "the startup probe must not depend on Firestore"


def test_schema_endpoint_serves_the_form_definition(client):
    r = client.get("/api/schema")
    assert r.status_code == 200
    body = r.json()
    knobs = set(body["strict_schema"]["properties"]["simulation"]["properties"])
    assert knobs == {"length_sec", "seed", "generations", "init_sims", "parca_cpus"}
    assert body["supported_namespaces"] == ["simulation"]
    assert body["defaults"]["simulation"]["length_sec"] == 60


def test_schema_field_order_covers_every_knob(client):
    """A knob absent from field_order would be invisible in the form."""
    body = client.get("/api/schema").json()
    knobs = set(body["strict_schema"]["properties"]["simulation"]["properties"])
    assert {f.split(".", 1)[1] for f in body["field_order"]} == knobs


def test_schema_is_cacheable_and_honours_if_none_match(client):
    first = client.get("/api/schema")
    etag = first.headers["etag"]
    assert "max-age" in first.headers["cache-control"]
    again = client.get("/api/schema", headers={"If-None-Match": etag})
    assert again.status_code == 304


def test_config_reports_the_ceilings_so_the_ui_need_not_hardcode_them(client):
    body = client.get("/api/config").json()
    assert body["max_runs_per_day"] == 8
    assert body["max_concurrent_runs"] == 2
    assert body["wall_clock_cap_sec"] == 2700
    assert body["accepting_runs"] is True


def test_config_is_never_cached(client):
    """A cached "yes" would keep submit live after the switch was thrown."""
    r = client.get("/api/config")
    assert r.headers["cache-control"] == "no-store"


# --- CORS -----------------------------------------------------------------

def test_the_allowed_origin_is_echoed(client):
    r = client.get("/healthz", headers={"Origin": "https://wcm-ui.pages.dev"})
    assert r.headers["access-control-allow-origin"] == "https://wcm-ui.pages.dev"


def test_a_disallowed_origin_gets_no_cors_header(client):
    """A wildcard config passes the previous test and is wrong."""
    r = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_preflight_permits_post(client):
    r = client.options("/api/runs", headers={
        "Origin": "https://wcm-ui.pages.dev",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert r.status_code in (200, 204)
    assert "POST" in r.headers["access-control-allow-methods"]


# --- validate -------------------------------------------------------------

def test_validate_returns_200_even_when_invalid(client):
    """A syntax error mid-typing is not an HTTP failure."""
    r = client.post("/api/runs/validate",
                    json={"params": {"simulation": {"length_sec": -1}}})
    assert r.status_code == 200
    assert r.json()["valid"] is False
    assert r.json()["errors"][0]["path"] == "simulation.length_sec"


def test_validate_touches_nothing(client, store, jobs):
    """It runs on every keystroke, so it must cost nothing and reserve nothing."""
    client.post("/api/runs/validate", json={"params": {}})
    assert store.writes == []
    jobs.run_job.assert_not_called()


def test_validate_previews_the_merged_result(client):
    body = client.post("/api/runs/validate", json={
        "params": {"simulation": {"length_sec": 120}},
        "yaml_override": "simulation:\n  seed: 7\n",
    }).json()
    assert body["valid"] is True
    assert body["resolved_params"]["simulation"] == {
        "length_sec": 120, "seed": 7, "generations": 1,
        "init_sims": 1, "parca_cpus": 1,
    }
    assert body["content_hash"].startswith("sha256:")


def test_validate_reports_a_yaml_syntax_error_with_a_position(client):
    body = client.post("/api/runs/validate",
                       json={"yaml_override": "a: [unclosed\n"}).json()
    assert body["valid"] is False
    err = body["errors"][0]
    assert err["kind"] == "yaml_syntax"
    assert err["line"] is not None


def test_an_unknown_top_level_body_key_is_rejected(client):
    """extra="forbid" — a mistyped "parameters" should be told, not dropped."""
    r = client.post("/api/runs/validate", json={"parameters": {}})
    assert r.status_code == 422


# --- submit ---------------------------------------------------------------

def test_submit_accepts_and_returns_a_run_id(client, store):
    r = client.post("/api/runs", json={"params": {"simulation": {"length_sec": 30}}})
    assert r.status_code == 202
    body = r.json()
    assert body["state"] == "queued"
    assert r.headers["location"] == f"/api/runs/{body['run_id']}"
    assert body["poll_after_ms"] == 5000
    assert store.doc("runs", body["run_id"])["state"] == "queued"


def test_submit_records_the_digest_and_content_hash(client, store):
    body = client.post("/api/runs", json={"params": {}}).json()
    doc = store.doc("runs", body["run_id"])
    assert doc["image_digest"] == "sha256:deadbeef"
    assert doc["content_hash"] == body["content_hash"]


def test_submit_stores_an_ip_hash_never_a_raw_address(client, store):
    body = client.post("/api/runs", json={"params": {}},
                       headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.9"}).json()
    doc = store.doc("runs", body["run_id"])
    assert "203.0.113.9" not in str(doc)
    assert len(doc["client_ip_hash"]) == 16


def test_the_response_body_never_leaks_an_ip(client):
    body = client.post("/api/runs", json={"params": {}},
                       headers={"X-Forwarded-For": "203.0.113.9"}).json()
    assert not any("ip" in k.lower() for k in body)


def test_invalid_params_return_all_errors_and_consume_no_quota(client, store):
    r = client.post("/api/runs", json={
        "params": {"simulation": {"length_sec": 0, "seed": -1}}})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid_params"
    assert {e["path"] for e in body["errors"]} == {
        "simulation.length_sec", "simulation.seed"}
    assert store.doc("quota", "inflight") is None, "no slot may be reserved"


def test_a_multi_generation_run_is_refused_as_a_client_error(client):
    """400, not 429 — waiting will not make these parameters acceptable."""
    r = client.post("/api/runs", json={"params": {"simulation": {"generations": 4}}})
    assert r.status_code == 422  # schema clamps it first
    body = r.json()
    assert body["errors"][0]["path"] == "simulation.generations"


def test_the_concurrency_ceiling_returns_429_with_retry_after(client, store):
    for _ in range(quota.Ceilings().max_concurrent_runs):
        assert client.post("/api/runs", json={"params": {}}).status_code == 202

    r = client.post("/api/runs", json={"params": {}})
    assert r.status_code == 429
    assert r.json()["error"] == "at_concurrency_limit"
    assert int(r.headers["retry-after"]) > 0


def test_the_kill_switch_returns_503(client, store):
    store.seed("config", "global",
               {"accepting_runs": False, "message": "at capacity for the month"})
    quota.reset_flag_cache()

    r = client.post("/api/runs", json={"params": {}})
    assert r.status_code == 503
    assert r.json()["error"] == "not_accepting_runs"
    assert "capacity" in r.json()["message"]


def test_a_failed_launch_gives_the_quota_slot_back(client, store, jobs):
    """Otherwise a broken Cloud Run leaks the whole day's capacity."""
    jobs.run_job.side_effect = InvalidArgument("bad override")

    r = client.post("/api/runs", json={"params": {}})
    assert r.status_code == 502
    assert r.json()["error"] == "upstream_error"
    assert store.doc("quota", "inflight")["leases"] == {}
    day = store.doc("quota_days", DAY)
    assert day["runs_started"] == 0, "a launch that never started must not charge"


def test_a_failed_launch_marks_the_run_failed_not_queued(client, store, jobs):
    jobs.run_job.side_effect = InvalidArgument("bad override")
    client.post("/api/runs", json={"params": {}})
    runs = [v for (c, _), v in store.data.items() if c == "runs"]
    assert runs and runs[0]["state"] == "failed"
    assert runs[0]["failure_source"] == "infrastructure"


# --- status ---------------------------------------------------------------

def test_an_unknown_run_is_404(client):
    r = client.get("/api/runs/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert r.json()["error"] == "run_not_found"


def test_a_queued_run_reports_a_poll_interval(client, store):
    store.seed("runs", "r1", {"state": "queued", "created_at": NOW,
                              "params_json": {"simulation": {}}})
    body = client.get("/api/runs/r1").json()
    assert body["state"] == "queued"
    assert body["poll_after_ms"] == 5000


def test_a_terminal_run_reports_no_poll_interval(client, store):
    """A settled run never changes, so the frontend should stop asking."""
    store.seed("runs", "r1", {"state": "succeeded", "created_at": NOW,
                              "started_at": NOW, "finished_at": NOW,
                              "params_json": {}})
    body = client.get("/api/runs/r1").json()
    assert body["poll_after_ms"] is None


def test_a_succeeded_run_advertises_its_artifacts(client, store):
    store.seed("runs", "r1", {
        "state": "succeeded", "created_at": NOW, "started_at": NOW,
        "finished_at": NOW, "params_json": {},
        "gcs_tarball_uri": "gs://b/r1/output.tar.gz",
        "gcs_parquet_uri": "gs://b/r1/timeseries.parquet",
    })
    arts = client.get("/api/runs/r1").json()["artifacts"]
    assert arts["timeseries"]["available"] is True
    assert arts["timeseries"]["url"] == "/api/runs/r1/timeseries.parquet"
    assert arts["tarball"]["available"] is True


def test_artifact_availability_costs_no_gcs_call(client, store, monkeypatch):
    """Polling happens every few seconds; a GCS round trip per poll would
    dominate the cost of the whole service."""
    monkeypatch.setattr(deps, "storage_client",
                        lambda: pytest.fail("status poll touched GCS"))
    store.seed("runs", "r1", {"state": "succeeded", "created_at": NOW,
                              "finished_at": NOW, "params_json": {}})
    assert client.get("/api/runs/r1").status_code == 200


def test_an_expired_tarball_is_reported_as_expired(client, store):
    """The lifecycle rule deletes output.tar.gz while the document still
    points at it, so 'gone' and 'not ready' must be distinguishable."""
    long_ago = NOW - timedelta(days=60)
    store.seed("runs", "r1", {"state": "succeeded", "created_at": long_ago,
                              "finished_at": long_ago, "params_json": {}})
    arts = client.get("/api/runs/r1").json()["artifacts"]
    assert arts["tarball"]["available"] is False
    assert arts["tarball"]["reason"] == "expired"


def test_a_failed_run_surfaces_the_reason_and_its_source(client, store):
    store.seed("runs", "r1", {
        "state": "failed", "created_at": NOW, "finished_at": NOW,
        "params_json": {}, "error_message": "worker subprocess exited 2",
        "failure_source": "worker",
        "gcs_stderr_uri": "gs://b/r1/stderr.log",
    })
    body = client.get("/api/runs/r1").json()
    assert body["error_message"] == "worker subprocess exited 2"
    assert body["failure_source"] == "worker"
    assert body["artifacts"]["stderr"]["available"] is True


def test_a_validation_time_failure_has_no_stderr(client, store):
    """Nothing ran, so there was nothing to capture — verified live in Stage 2."""
    store.seed("runs", "r1", {"state": "failed", "created_at": NOW,
                              "finished_at": NOW, "params_json": {},
                              "error_message": "param error",
                              "failure_source": "params"})
    arts = client.get("/api/runs/r1").json()["artifacts"]
    assert arts["stderr"]["available"] is False
    assert arts["stderr"]["reason"] == "never_produced"


def test_the_status_response_never_carries_an_ip_hash(client, store):
    store.seed("runs", "r1", {"state": "queued", "created_at": NOW,
                              "params_json": {},
                              "client_ip_hash": "0123456789abcdef"})
    body = client.get("/api/runs/r1").json()
    assert "0123456789abcdef" not in str(body)


def test_observing_a_terminal_run_frees_its_quota_slot(client, store):
    """The fast path back to capacity: don't make the next submitter wait out
    the lease TTL for a run we can already see has finished."""
    submitted = client.post("/api/runs", json={"params": {}}).json()
    run_id = submitted["run_id"]
    assert store.doc("quota", "inflight")["leases"]

    store.data[("runs", run_id)]["state"] = "succeeded"
    store.data[("runs", run_id)]["finished_at"] = NOW
    client.get(f"/api/runs/{run_id}")

    assert store.doc("quota", "inflight")["leases"] == {}


def test_the_reproduce_command_uses_the_digest(client, store):
    store.seed("runs", "r1", {
        "state": "succeeded", "created_at": NOW, "finished_at": NOW,
        "params_json": {}, "image_uri": IMAGE,
        "gcs_params_uri": "gs://b/r1/params.json",
    })
    prov = client.get("/api/runs/r1").json()["provenance"]
    assert "@sha256:deadbeef" in prov["reproduce_command"]


# --- error envelope -------------------------------------------------------

def test_every_error_uses_one_envelope(client):
    r = client.get("/api/runs/nope")
    assert set(r.json()) >= {"error", "message"}


def test_an_unhandled_exception_leaks_no_traceback(client, monkeypatch):
    """A public endpoint must not echo internals — a Firestore traceback names
    the project, the collection, and the client library version."""
    def boom(_run_id):
        raise RuntimeError("firestore said something revealing")
    monkeypatch.setattr(db, "get_run", boom)

    r = client.get("/api/runs/r1")
    assert r.status_code == 500
    body = r.json()
    assert "revealing" not in str(body)
    assert body["request_id"]


def test_no_collection_listing_endpoint_exists(client):
    """run_id is the capability; enumeration would hand out every run."""
    r = client.get("/api/runs")
    assert r.status_code in (404, 405)
