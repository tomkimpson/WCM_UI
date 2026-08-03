"""Reproducibility metadata: image digest, git SHAs, and the content hash.

The design doc promises "same hash + same image = identical outputs". That
promise is only worth making if the hash is genuinely deterministic and the
digest genuinely describes what ran, so most of this file is about those two
properties rather than about happy-path plumbing.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from api import provenance


def _job_with_image(image: str):
    """Mimic the shape of run_v2.Job for the one field we read."""
    return SimpleNamespace(
        template=SimpleNamespace(
            template=SimpleNamespace(containers=[SimpleNamespace(image=image)])
        )
    )


# --- the content hash -----------------------------------------------------

def test_canonical_payload_is_exactly_this_string():
    """Pin the canonical form itself, not just its digest.

    Asserting the string keeps the next test honest: a magic hash constant
    proves only that the code agrees with itself, whereas this is readable and
    a human can confirm it by eye.
    """
    got = provenance.canonical_json(
        {"simulation": {"seed": 0, "length_sec": 30}}, "sha256:abc",
    )
    assert got == (
        '{"hash_version":1,'
        '"image_digest":"sha256:abc",'
        '"params":{"simulation":{"length_sec":30,"seed":0}}}'
    )


def test_content_hash_is_sha256_of_the_canonical_payload():
    params = {"simulation": {"seed": 0, "length_sec": 30}}
    canonical = provenance.canonical_json(params, "sha256:abc")
    expected = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert provenance.content_hash(params, "sha256:abc") == expected


def test_key_insertion_order_does_not_change_the_hash():
    a = {"simulation": {"length_sec": 30, "seed": 1}}
    b = {"simulation": {"seed": 1, "length_sec": 30}}
    assert (provenance.content_hash(a, "sha256:x")
            == provenance.content_hash(b, "sha256:x"))


def test_nested_key_order_does_not_change_the_hash():
    """sort_keys must recurse, or nesting reintroduces order dependence."""
    a = {"wcecoli": {"outer": {"z": 1, "a": 2}}, "simulation": {}}
    b = {"simulation": {}, "wcecoli": {"outer": {"a": 2, "z": 1}}}
    assert (provenance.content_hash(a, "sha256:x")
            == provenance.content_hash(b, "sha256:x"))


def test_a_different_digest_changes_the_hash():
    params = {"simulation": {"seed": 1}}
    assert (provenance.content_hash(params, "sha256:aaa")
            != provenance.content_hash(params, "sha256:bbb"))


def test_a_different_param_value_changes_the_hash():
    assert (provenance.content_hash({"simulation": {"seed": 1}}, "sha256:x")
            != provenance.content_hash({"simulation": {"seed": 2}}, "sha256:x"))


def test_an_unresolved_digest_still_hashes():
    """Provenance must never block a run, so None is a legal digest."""
    assert provenance.content_hash({"simulation": {}}, None).startswith("sha256:")


def test_nan_is_rejected_rather_than_silently_hashed():
    """json.dumps emits bare NaN by default, which isn't valid JSON.

    Another implementation reading it would disagree about the bytes, so the
    hash would stop meaning what it claims. Fail loudly instead.
    """
    with pytest.raises(ValueError):
        provenance.content_hash({"simulation": {"x": float("nan")}}, "sha256:x")


def test_the_hash_carries_no_whitespace_sensitivity():
    """separators must be compact, so a reformat can't change the digest."""
    canonical = provenance.canonical_json({"a": 1, "b": 2}, "sha256:x")
    assert " " not in canonical


# --- determinism flag -----------------------------------------------------

def test_single_cpu_parca_is_marked_deterministic():
    assert provenance.is_deterministic({"simulation": {"parca_cpus": 1}}) is True


def test_multi_cpu_parca_is_not_marked_deterministic():
    """Multiprocess ParCa can reorder float reductions.

    OPENBLAS_NUM_THREADS=1 in the Dockerfile handles BLAS, but not this. Don't
    claim bit-identity we can't deliver.
    """
    assert provenance.is_deterministic({"simulation": {"parca_cpus": 4}}) is False


# --- image pin resolution -------------------------------------------------

def test_a_digest_pinned_job_spec_is_authoritative():
    """What the Job spec holds is literally what Cloud Run will pull."""
    pin = provenance.resolve_image_pin(
        _job_with_image("reg/worker@sha256:deadbeef"))
    assert pin.digest == "sha256:deadbeef"
    assert pin.source == "job_spec_digest"
    assert pin.uri == "reg/worker@sha256:deadbeef"


def test_a_tag_only_job_spec_yields_an_unresolved_pin():
    """A tag is mutable, so recording it as a digest would be a lie.

    ContainerOverride has no `image` field (verified against
    google-cloud-run 0.16.0), so the API cannot force a digest at submit
    time. CI pins the Job spec; until it has, the honest answer is 'unresolved'.
    """
    pin = provenance.resolve_image_pin(_job_with_image("reg/worker:latest"))
    assert pin.digest is None
    assert pin.source == "unresolved"
    assert pin.uri == "reg/worker:latest"


def test_a_missing_job_never_raises():
    """Provenance is best-effort by design; a lookup failure must not 500."""
    pin = provenance.resolve_image_pin(None)
    assert pin.digest is None
    assert pin.source == "unresolved"


def test_a_malformed_job_object_never_raises():
    pin = provenance.resolve_image_pin(SimpleNamespace(template=None))
    assert pin.source == "unresolved"


def test_pin_is_incomplete_without_a_digest():
    assert provenance.resolve_image_pin(_job_with_image("r/w:latest")).complete is False
    assert provenance.resolve_image_pin(_job_with_image("r/w@sha256:a")).complete is True


# --- git SHAs -------------------------------------------------------------

def test_git_shas_come_from_the_build_args(monkeypatch):
    monkeypatch.setenv("WCM_UI_GIT_SHA", "cafebabe")
    monkeypatch.setenv("WCECOLI_GIT_SHA", "3fc8ec1f")
    provenance.git_shas.cache_clear()
    assert provenance.git_shas() == ("cafebabe", "3fc8ec1f")


def test_missing_git_shas_are_none_not_an_error(monkeypatch):
    """The API image bakes these in; a local uvicorn run has neither."""
    monkeypatch.delenv("WCM_UI_GIT_SHA", raising=False)
    monkeypatch.delenv("WCECOLI_GIT_SHA", raising=False)
    monkeypatch.setattr(provenance, "_git_sha_from_repo", lambda *a: None)
    provenance.git_shas.cache_clear()
    assert provenance.git_shas() == (None, None)


def test_git_shas_are_excluded_from_the_content_hash(monkeypatch):
    """The digest strictly dominates them.

    Including a SHA that arrives via a build-arg env var can only introduce
    false DIFFERENCES if the var goes stale; excluding it can never introduce
    a false IDENTITY. So they are provenance, not hash input.
    """
    params = {"simulation": {"seed": 1}}
    before = provenance.content_hash(params, "sha256:x")
    monkeypatch.setenv("WCM_UI_GIT_SHA", "something-else-entirely")
    provenance.git_shas.cache_clear()
    assert provenance.content_hash(params, "sha256:x") == before


# --- the reproduce command ------------------------------------------------

def test_reproduce_command_uses_the_digest_not_the_tag():
    cmd = provenance.reproduce_command(
        image_uri="reg/worker@sha256:abc",
        gcs_params_uri="gs://bucket/run-1/params.json",
    )
    assert "@sha256:abc" in cmd
    assert ":latest" not in cmd
    assert "gs://bucket/run-1/params.json" in cmd
