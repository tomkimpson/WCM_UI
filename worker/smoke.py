"""Smoke simulation: smallest possible end-to-end wcEcoli run.

Used by tests and CI to verify the worker image is functional. Not part of
the production parameter-injection path (that's Stage 1b).

Runs the canonical two-step sequence (see docs/notes/wcecoli-build-notes.md
"Canonical run commands"):
  1. ParCa  — runscripts/manual/runParca.py  (defaults are already minimal)
  2. runSim — runscripts/manual/runSim.py --length-sec 60
              (60 simulated seconds; default is 3 hours)

Outputs land at /wcEcoli/out/manual/ inside the container. The caller is
expected to bind-mount a host directory onto /wcEcoli/out to capture them.
"""
import subprocess
import sys

WCECOLI_DIR = "/wcEcoli"


def run(cmd: list[str]) -> int:
    print(f"+ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=WCECOLI_DIR).returncode


def main() -> int:
    rc = run(["python3", "runscripts/manual/runParca.py"])
    if rc != 0:
        print(f"runParca.py exited {rc}", file=sys.stderr, flush=True)
        return rc

    rc = run(["python3", "runscripts/manual/runSim.py", "--length-sec", "60"])
    if rc != 0:
        print(f"runSim.py exited {rc}", file=sys.stderr, flush=True)
        return rc

    return 0


if __name__ == "__main__":
    sys.exit(main())
