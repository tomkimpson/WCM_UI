"""Submit a wcEcoli simulation to AWS-Run-Jobs-shaped GCP infra.

Wait no — GCP Cloud Run Jobs. Run from the repo root:

    python -m scripts.submit --params path/to/params.json [--job NAME] \
        [--region us-central1] [--project wcm-ui-dev] \
        [--image-uri ghcr.io/tomkimpson/wcm-ui-worker:latest]

Flow:
    1. Read + validate params JSON against worker/schema/params.schema.json
    2. Generate run_id (uuid4)
    3. Create Firestore runs/{run_id} with state='queued'
    4. Call Cloud Run Jobs run_job() with env-var overrides
       (RUN_ID, PARAMS_JSON, IMAGE_URI)
    5. PATCH the doc with the returned execution name
    6. Print run_id + console URL

Auth: uses Application Default Credentials (gcloud auth application-default
login). No keys checked in.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Optional, Sequence

from worker.merge import merge_params
from worker.validate import ValidationError, validate_params

_DEFAULT_PROJECT = "wcm-ui-dev"
_DEFAULT_REGION = "us-central1"
_DEFAULT_JOB = "wcm-ui-worker-dev"
_DEFAULT_IMAGE = "ghcr.io/tomkimpson/wcm-ui-worker:latest"

_DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "worker" / "schema" / "defaults.json"


def _generate_run_id() -> str:
    return str(uuid.uuid4())


def _firestore_client():
    from google.cloud import firestore
    return firestore.Client()


def _jobs_client():
    from google.cloud import run_v2
    return run_v2.JobsClient()


def _load_and_resolve(params_path: Path) -> dict:
    """Read the user's params JSON, merge with defaults, validate."""
    user = json.loads(params_path.read_text()) if params_path.exists() else None
    if user is None:
        raise FileNotFoundError(params_path)
    defaults = json.loads(_DEFAULTS_PATH.read_text())
    resolved = merge_params(defaults, user)
    validate_params(resolved)
    return resolved


def _build_run_request(
    project: str, region: str, job: str, run_id: str,
    image_uri: str, resolved_params: dict,
):
    from google.cloud import run_v2

    env = [
        run_v2.EnvVar(name="RUN_ID", value=run_id),
        run_v2.EnvVar(name="PARAMS_JSON", value=json.dumps(resolved_params)),
        run_v2.EnvVar(name="IMAGE_URI", value=image_uri),
    ]
    container_override = run_v2.RunJobRequest.Overrides.ContainerOverride(env=env)
    overrides = run_v2.RunJobRequest.Overrides(
        container_overrides=[container_override],
    )
    return run_v2.RunJobRequest(
        name=f"projects/{project}/locations/{region}/jobs/{job}",
        overrides=overrides,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Submit a wcEcoli sim to Cloud Run Jobs")
    ap.add_argument("--params", required=True, type=Path,
                    help="Path to user params JSON")
    ap.add_argument("--project", default=_DEFAULT_PROJECT)
    ap.add_argument("--region", default=_DEFAULT_REGION)
    ap.add_argument("--job", default=_DEFAULT_JOB)
    ap.add_argument("--image-uri", default=_DEFAULT_IMAGE)
    args = ap.parse_args(argv)

    # Step 1: load + validate
    try:
        resolved = _load_and_resolve(args.params)
    except FileNotFoundError as exc:
        print(f"param error: file not found: {exc}", file=sys.stderr)
        return 64
    except (json.JSONDecodeError, ValidationError, OSError) as exc:
        print(f"param error: {exc}", file=sys.stderr)
        return 64

    # Step 2: generate run_id
    run_id = _generate_run_id()

    # Step 3: create queued Firestore doc
    from google.cloud import firestore
    firestore_client = _firestore_client()
    doc_ref = firestore_client.collection("runs").document(run_id)
    doc_ref.set({
        "state": "queued",
        "params_json": resolved,
        "image_uri": args.image_uri,
        "created_at": firestore.SERVER_TIMESTAMP,
    })

    # Step 4: submit Cloud Run Jobs execution
    jobs_client = _jobs_client()
    request = _build_run_request(
        args.project, args.region, args.job, run_id, args.image_uri, resolved,
    )
    operation = jobs_client.run_job(request=request)
    execution_name = operation.metadata.name

    # Step 5: update Firestore doc with execution name
    doc_ref.update({"execution_name": execution_name})

    # Step 6: print run_id and a clickable console URL
    console_url = (
        f"https://console.cloud.google.com/run/jobs/executions/details/"
        f"{args.region}/{execution_name.split('/')[-1]}/tasks?project={args.project}"
    )
    print(f"run_id={run_id}")
    print(f"execution={execution_name}")
    print(f"console={console_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
