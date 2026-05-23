"""Smoke test: run a minimal wcEcoli simulation end-to-end inside the image.

Mounts a host tmp dir into `/wcEcoli/out` so outputs written by ParCa and
runSim land on the host where pytest can inspect them.

Canonical successful outputs (per docs/notes/wcecoli-build-notes.md):
  - out/manual/kb/simData.cPickle                         (ParCa)
  - out/manual/wildtype_000000/000000/generation_000000/000000/simOut/
                                                          (runSim)
"""
import subprocess


SIM_TIMEOUT_SEC = 1800  # 30-minute ceiling; tighten once we know real runtime


def test_smoke_sim_produces_outputs(built_image, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{out_dir}:/wcEcoli/out",
            built_image,
            "python3", "-m", "worker.smoke",
        ],
        capture_output=True,
        text=True,
        timeout=SIM_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"smoke sim failed (exit {result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    parca_output = out_dir / "manual" / "kb" / "simData.cPickle"
    assert parca_output.exists(), (
        f"ParCa output missing: {parca_output} not found.\n"
        f"Files written under {out_dir}: {[str(p.relative_to(out_dir)) for p in out_dir.rglob('*')]}"
    )

    sim_outs = list(out_dir.glob("manual/wildtype_*/*/generation_*/*/simOut"))
    assert sim_outs, (
        f"runSim simOut directory missing under {out_dir}/manual/.\n"
        f"Files written: {[str(p.relative_to(out_dir)) for p in out_dir.rglob('*')]}"
    )
