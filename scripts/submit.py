"""Submit a wcEcoli simulation to Cloud Run Jobs from the command line.

    python -m scripts.submit --params path/to/params.json [--job NAME] \
        [--region us-central1] [--project wcm-ui-dev] [--image-uri URI]

This is now a thin wrapper. Everything except reading the file and printing the
result lives in ``api.runs.submit_run``, which the HTTP endpoint also calls — so
the CLI and the service produce identical Firestore documents and identical
Cloud Run requests by construction rather than by care.

The four private helpers below are kept as they were because tests/test_submit.py
monkeypatches them; ``main`` calls each seam and passes the result down, so
patching still controls the shared path.

Auth: Application Default Credentials (``gcloud auth application-default
login``). No keys checked in.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Optional, Sequence

from api import cloudrun, runs
from api.quota import Ceilings

_DEFAULT_PROJECT = "wcm-ui-dev"
_DEFAULT_REGION = "us-central1"
_DEFAULT_JOB = "wcm-ui-worker-dev"
_DEFAULT_IMAGE = "us-central1-docker.pkg.dev/wcm-ui-dev/wcm-ui-worker/worker:latest"
_DEFAULT_BUCKET = "wcm-ui-runs-dev"


def _generate_run_id() -> str:
    return str(uuid.uuid4())


def _firestore_client():
    from google.cloud import firestore
    from worker import db
    # Via worker.db rather than a bare firestore.Client(): the bare form
    # ignores GCP_PROJECT and FIRESTORE_DATABASE, so the submitter and the
    # worker could write to different databases.
    assert firestore  # imported for the side effect of failing early if absent
    return db._client()


def _jobs_client():
    from google.cloud import run_v2
    return run_v2.JobsClient()


def _load_params_file(params_path: Path) -> dict:
    """Read the user's params JSON. Merging and validation happen downstream."""
    if not params_path.exists():
        raise FileNotFoundError(params_path)
    return json.loads(params_path.read_text())


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Submit a wcEcoli sim to Cloud Run Jobs")
    ap.add_argument("--params", required=True, type=Path,
                    help="Path to user params JSON")
    ap.add_argument("--project", default=_DEFAULT_PROJECT)
    ap.add_argument("--region", default=_DEFAULT_REGION)
    ap.add_argument("--job", default=_DEFAULT_JOB)
    ap.add_argument("--image-uri", default=_DEFAULT_IMAGE)
    ap.add_argument("--bucket", default=_DEFAULT_BUCKET)
    args = ap.parse_args(argv)

    try:
        user_params = _load_params_file(args.params)
    except FileNotFoundError as exc:
        print(f"param error: file not found: {exc}", file=sys.stderr)
        return 64
    except (json.JSONDecodeError, OSError) as exc:
        print(f"param error: {exc}", file=sys.stderr)
        return 64

    target = cloudrun.JobTarget(project=args.project, region=args.region,
                                job=args.job)
    try:
        result = runs.submit_run(
            params=user_params,
            yaml_override=None,
            run_id=_generate_run_id(),
            firestore_client=_firestore_client(),
            jobs_client=_jobs_client(),
            target=target,
            submitter="cli",
            # The operator gets the same ceiling the API enforces, so a CLI
            # submission cannot quietly outlive what the service would allow.
            wall_clock_cap_sec=Ceilings.from_env().wall_clock_cap_sec,
            runs_bucket=args.bucket,
            fallback_image_uri=args.image_uri,
        )
    except runs.LaunchFailed as exc:
        print(f"launch error: {exc}", file=sys.stderr)
        return 69  # EX_UNAVAILABLE — the service refused, not the operator

    if not result.ok:
        for err in result.errors:
            print(f"param error: {err.path}: {err.message}", file=sys.stderr)
        return 64

    print(f"run_id={result.run_id}")
    print(f"execution={result.execution_name}")
    print(f"console={result.console_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
