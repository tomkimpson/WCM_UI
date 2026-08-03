"""End-to-end: a user-supplied length_sec actually reaches the sim.

This is one full sim run (~15 min). Kept as a single test, not a
parametrised matrix, so CI doesn't multiply that runtime.

The check: the Main listener writes an `attributes.json` whose
`lengthSec` field records the simulated wall time used for the run.
If our override travelled the full pipeline — PARAMS_JSON env var
→ worker.run → runSim --length-sec → wcEcoli's sim loop — the value
in that file is the value we sent in.
"""
import json
import subprocess

import pytest

pytestmark = pytest.mark.docker


SIM_TIMEOUT_SEC = 1800
LENGTH_SEC_OVERRIDE = 30  # default is 60; pick anything different


def test_length_sec_param_reaches_sim(built_image, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    params = {"simulation": {"length_sec": LENGTH_SEC_OVERRIDE}}

    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "-e", f"PARAMS_JSON={json.dumps(params)}",
            "-v", f"{out_dir}:/wcEcoli/out",
            built_image,
        ],
        capture_output=True,
        text=True,
        timeout=SIM_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"run failed (exit {result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    main_dirs = list(out_dir.glob("manual/wildtype_*/*/generation_*/*/simOut/Main"))
    assert main_dirs, f"Main listener missing. Files: {list(out_dir.rglob('*'))[:30]}"

    attrs = json.loads((main_dirs[0] / "attributes.json").read_text())
    assert attrs.get("lengthSec") == float(LENGTH_SEC_OVERRIDE), (
        f"expected lengthSec={LENGTH_SEC_OVERRIDE}, got attrs={attrs}"
    )
