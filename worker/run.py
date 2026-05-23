"""Worker entrypoint: parse $PARAMS_JSON → run wcEcoli → (cloud mode) package + upload.

Two execution modes, controlled by the presence of ``$RUN_ID``:

  - **Local mode** (``RUN_ID`` unset, Stage 1b workflow): run the sim and
    exit. No DB writes, no GCS uploads. ``docker run -e PARAMS_JSON=…``
    keeps working unchanged.

  - **Cloud mode** (``RUN_ID`` and ``RUNS_BUCKET`` set, Stage 2 workflow):
    submitter has already inserted a ``runs/{run_id}`` doc with
    state='queued'. The worker:
        mark_running → run sim
        → on success: tar /wcEcoli/out/manual + extract Parquet
          + upload all three to GCS + mark_succeeded
        → on subprocess failure: upload stderr.log + mark_failed
        → on param validation failure: mark_failed (no stderr — nothing ran)

$PARAMS_JSON may be a path to a JSON file (recommended) or an inline JSON
blob (``docker run -e PARAMS_JSON='{...}' …``). If unset, defaults apply.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from worker.merge import merge_params
from worker.validate import ValidationError, validate_params

WCECOLI_DIR = "/wcEcoli"
DEFAULTS_PATH = Path(__file__).parent / "schema" / "defaults.json"


def _load_defaults() -> dict[str, Any]:
    return json.loads(DEFAULTS_PATH.read_text())


def _load_user_params() -> dict[str, Any]:
    raw = os.environ.get("PARAMS_JSON", "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        return json.loads(raw)
    return json.loads(Path(raw).read_text())


def resolve(user_params: dict[str, Any]) -> dict[str, Any]:
    resolved = merge_params(_load_defaults(), user_params)
    validate_params(resolved)
    return resolved


def build_commands(resolved: dict[str, Any]) -> list[list[str]]:
    sim = resolved["simulation"]
    parca = ["python3", "runscripts/manual/runParca.py", "--cpus", str(sim["parca_cpus"])]
    runsim = [
        "python3", "runscripts/manual/runSim.py",
        "--length-sec", str(sim["length_sec"]),
        "--seed", str(sim["seed"]),
        "--generations", str(sim["generations"]),
        "--init-sims", str(sim["init_sims"]),
    ]
    return [parca, runsim]


def _storage_client():
    """Lazy import — google.cloud.storage is only needed in cloud mode."""
    from google.cloud import storage
    return storage.Client()


def _run_commands(resolved: dict[str, Any], stderr_fh) -> int:
    """Execute the parca + runSim commands. Returns the first non-zero rc."""
    for cmd in build_commands(resolved):
        print(f"+ {' '.join(cmd)}", flush=True)
        result = subprocess.run(cmd, cwd=WCECOLI_DIR, stderr=stderr_fh)
        if result.returncode != 0:
            return result.returncode
    return 0


def _run_cloud(resolved: dict[str, Any], run_id: str, bucket: str) -> int:
    """Cloud-mode lifecycle. See module docstring."""
    from worker import db, postprocess

    db.mark_running(run_id)

    tmp = Path(tempfile.mkdtemp())
    stderr_path = tmp / "stderr.log"

    with open(stderr_path, "wb") as stderr_fh:
        rc = _run_commands(resolved, stderr_fh=stderr_fh)

    storage = _storage_client()

    if rc != 0:
        stderr_uri = postprocess.upload_stderr(storage, bucket, run_id, stderr_path)
        db.mark_failed(run_id, f"worker subprocess exited {rc}", stderr_uri)
        return rc

    out_root = Path(WCECOLI_DIR) / "out"
    tarball = tmp / "output.tar.gz"
    parquet = tmp / "timeseries.parquet"
    postprocess.make_tarball(out_root / "manual", tarball)
    postprocess.extract_timeseries(out_root, parquet)
    uris = postprocess.upload_run_artifacts(
        storage, bucket, run_id, tarball, parquet, resolved
    )
    db.mark_succeeded(run_id, uris["tarball"], uris["parquet"])
    return 0


def _mark_failed_quiet(run_id: str, message: str, stderr_uri: Optional[str]) -> None:
    """Best-effort failure write — never let it raise out of main()."""
    try:
        from worker import db
        db.mark_failed(run_id, message, stderr_uri)
    except Exception as exc:  # pragma: no cover — defensive only
        print(f"warn: could not write failure state for {run_id}: {exc}",
              file=sys.stderr, flush=True)


def main() -> int:
    run_id = os.environ.get("RUN_ID") or None

    try:
        resolved = resolve(_load_user_params())
    except (json.JSONDecodeError, ValidationError, OSError) as exc:
        message = f"param error: {exc}"
        print(message, file=sys.stderr, flush=True)
        if run_id:
            _mark_failed_quiet(run_id, message, stderr_uri=None)
        return 64  # EX_USAGE

    if run_id:
        bucket = os.environ.get("RUNS_BUCKET")
        if not bucket:
            message = "RUN_ID set but RUNS_BUCKET missing"
            print(message, file=sys.stderr, flush=True)
            _mark_failed_quiet(run_id, message, stderr_uri=None)
            return 64
        return _run_cloud(resolved, run_id, bucket)

    return _run_commands(resolved, stderr_fh=None)


if __name__ == "__main__":
    sys.exit(main())
