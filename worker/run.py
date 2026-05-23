"""Stage 1b entrypoint: parse $PARAMS_JSON → run wcEcoli with resolved flags.

$PARAMS_JSON may be:
  - a path to a JSON file (recommended; supports comments-as-keys, multi-line)
  - an inline JSON blob (CI convenience: `docker run -e PARAMS_JSON='{...}' …`)

If unset or empty, defaults are used.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

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
    # Inline JSON starts with `{`; otherwise treat as a file path.
    if raw.startswith("{"):
        return json.loads(raw)
    path = Path(raw)
    return json.loads(path.read_text())


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


def main() -> int:
    try:
        user = _load_user_params()
        resolved = resolve(user)
    except (json.JSONDecodeError, ValidationError, OSError) as exc:
        print(f"param error: {exc}", file=sys.stderr, flush=True)
        return 64  # EX_USAGE

    for cmd in build_commands(resolved):
        print(f"+ {' '.join(cmd)}", flush=True)
        result = subprocess.run(cmd, cwd=WCECOLI_DIR)
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
