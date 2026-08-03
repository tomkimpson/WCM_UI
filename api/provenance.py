"""Reproducibility metadata for a run.

The design doc's promise is "same hash + same image = identical outputs". Two
things have to hold for that to be worth saying:

1. The recorded image must describe what actually ran. It cannot be a tag —
   tags are mutable, and CI overwrites `:latest` on every green main push. So
   the authority is the **digest in the Cloud Run Job spec**, read back at
   submit time. The API cannot force a digest itself:
   ``RunJobRequest.Overrides.ContainerOverride`` has no ``image`` field
   (verified against google-cloud-run 0.16.0), so whatever the Job spec holds
   is what Cloud Run pulls. CI pins it; we read it.

2. The hash must be byte-stable across processes and machines. Hence
   ``canonical_json``, where every serialisation argument is load-bearing.

One caveat we record rather than paper over: bit-identical output requires
``parca_cpus == 1``. Multiprocess ParCa can reorder float reductions, which
``OPENBLAS_NUM_THREADS=1`` does not address. That is what ``deterministic``
is for — a badge, not a guarantee we can't keep.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

#: Bump when the hash recipe changes, so old hashes stay interpretable rather
#: than silently meaning something different. It lives INSIDE the payload, so
#: a bump necessarily changes every digest.
HASH_VERSION = 1

_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ImagePin:
    """What we know about the image a run will use."""

    uri: str
    digest: Optional[str]
    source: str          # job_spec_digest | registry_manifest | unresolved

    @property
    def complete(self) -> bool:
        """False when the run is not fully reproducible from stored metadata."""
        return self.digest is not None


def canonical_json(resolved_params: dict[str, Any],
                   image_digest: Optional[str]) -> str:
    """The exact byte string the content hash is taken over.

    Exposed so a test can assert the string itself. A hardcoded digest
    constant would only prove the code agrees with itself; the readable form
    is what a human can actually check.

    Every argument matters:
      sort_keys        recurses, so nesting can't reintroduce order dependence
      separators       strips whitespace, so a reformat can't change the digest
      ensure_ascii     escapes non-ASCII, removing Unicode-normalisation
                       dependence between platforms
      allow_nan=False  NaN/Infinity are not valid JSON; emitting them would let
                       another reader disagree about the bytes, so raise instead
    """
    payload = {
        "hash_version": HASH_VERSION,
        "image_digest": image_digest,
        "params": resolved_params,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def content_hash(resolved_params: dict[str, Any],
                 image_digest: Optional[str]) -> str:
    """Stable identifier for "these parameters on that image".

    Deliberately excludes the git SHAs: the image digest strictly dominates
    them (same digest implies same source), so including a SHA that arrives
    via a build-arg env var could only introduce false differences if the var
    went stale. Excluding it can never introduce a false identity. The SHAs are
    stored as un-hashed provenance instead.
    """
    canonical = canonical_json(resolved_params, image_digest)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_deterministic(resolved_params: dict[str, Any]) -> bool:
    """Whether we can honestly claim bit-identical reruns. See module docstring."""
    return int(resolved_params.get("simulation", {}).get("parca_cpus", 1)) == 1


def resolve_image_pin(job: Any) -> ImagePin:
    """Read the image the Job spec will actually pull.

    Takes the already-fetched Job rather than a client, so the policy is a pure
    function and the I/O stays in api/cloudrun.py.

    Never raises. A provenance lookup must not be able to fail a submission —
    the run is still perfectly valid, we just know less about it, which is what
    ``source="unresolved"`` records.
    """
    try:
        image = job.template.template.containers[0].image
    except (AttributeError, IndexError, TypeError):
        return ImagePin(uri="", digest=None, source="unresolved")

    if not image:
        return ImagePin(uri="", digest=None, source="unresolved")

    if "@sha256:" in image:
        _, _, digest = image.partition("@")
        return ImagePin(uri=image, digest=digest, source="job_spec_digest")

    # A tag. Recording it as a digest would be a lie, and it races CI's next
    # push. Registry manifest lookup is the documented fallback; until CI pins
    # the Job spec the honest answer is that we don't know.
    return ImagePin(uri=image, digest=None, source="unresolved")


def _git_sha_from_repo(rev: str = "HEAD", path: Optional[str] = None) -> Optional[str]:
    """Best-effort git lookup, for local development only.

    In the deployed image the SHAs are baked in as env vars; this exists so a
    developer running uvicorn locally still gets useful provenance.
    """
    cmd = (["git", "ls-tree", rev, path] if path
           else ["git", "rev-parse", rev])
    try:
        out = subprocess.run(cmd, cwd=_REPO_ROOT, capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    text = out.stdout.strip()
    if path:
        # "160000 commit <sha>\tvendor/wcEcoli" — the submodule pointer, which
        # works without the submodule being checked out.
        parts = text.split()
        return parts[2] if len(parts) >= 3 else None
    return text


@lru_cache(maxsize=1)
def git_shas() -> tuple[Optional[str], Optional[str]]:
    """``(wcm_ui_git_sha, wcecoli_git_sha)``, or Nones if unavailable.

    Cached because in the deployed image these are immutable for the process
    lifetime, and the fallback shells out to git.
    """
    wcm_ui = os.environ.get("WCM_UI_GIT_SHA") or _git_sha_from_repo("HEAD")
    wcecoli = (os.environ.get("WCECOLI_GIT_SHA")
               or _git_sha_from_repo("HEAD", "vendor/wcEcoli"))
    return (wcm_ui or None, wcecoli or None)


def reproduce_command(*, image_uri: str, gcs_params_uri: str) -> str:
    """The one-liner the results page shows.

    Renders from stored fields only, so an old run's command keeps working
    after the image is bumped — which is the whole point of recording a digest.
    """
    return (
        f'docker run --rm -e PARAMS_JSON="$(gcloud storage cat {gcs_params_uri})" '
        f"{image_uri}"
    )
