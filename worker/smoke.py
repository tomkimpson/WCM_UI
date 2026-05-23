"""Smoke entrypoint: invokes worker.run with no user params (defaults).

Kept as a thin wrapper so tests/test_smoke_sim.py keeps working — it
exercises the same code path the production run takes, just with an
empty $PARAMS_JSON.
"""
import os
import sys

from worker.run import main

if __name__ == "__main__":
    os.environ.setdefault("PARAMS_JSON", "")
    sys.exit(main())
