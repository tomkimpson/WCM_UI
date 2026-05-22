# Stage 1: Worker Bootstrap — Dockerised wcEcoli Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Get the upstream wcEcoli model running reproducibly inside a Docker container on a developer laptop, with a passing smoke test and CI.

**Architecture:** Vendor wcEcoli as a git submodule pinned to a known SHA. Build a single Docker image from a project-owned Dockerfile (do not rely on the upstream one — they change). A smoke test runs the image, executes the smallest possible simulation, and asserts that expected output files appear in a mounted volume. CI on GitHub Actions builds the image and runs the smoke test on every push.

**Tech Stack:** Docker, Python 3.11 (host-side test runner), pytest, GNU Make, GitHub Actions. The wcEcoli container itself uses whatever Python version upstream requires (to be confirmed in Task 2).

**Scope and non-goals:** This stage does NOT include parameter injection, JSON Schema, AWS Batch, S3 uploads, the API, the frontend, or any networking. Those are Stages 1b–7 in the [design doc](./2026-05-22-wcm-frontend-design.md). The only contract this stage produces: "given a container image tag, you can `docker run` it and get wcEcoli output files on a host volume."

**Reference skills:**
- @superpowers:test-driven-development — every task follows red/green/commit
- @superpowers:verification-before-completion — run the actual commands, don't assume
- @superpowers:systematic-debugging — for wcEcoli build issues (likely)

---

## Task 1: Project scaffolding

**Files:**
- Create: `.gitignore`
- Create: `README.md`
- Create: `LICENSE` (MIT)
- Create: `worker/.gitkeep`
- Create: `tests/.gitkeep`
- Create: `Makefile`

**Step 1: Create `.gitignore`**

Write this content:

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
.venv/
venv/

# OS
.DS_Store
Thumbs.db

# Editors
.vscode/
.idea/
*.swp

# Project
out/
*.log
.env
.env.local

# Docker artefacts
docker-compose.override.yml
```

**Step 2: Create `README.md` stub**

```markdown
# WCM_UI

Web frontend for running the [wcEcoli](https://github.com/CovertLab/wcEcoli) whole-cell model on cloud compute.

See [docs/plans/2026-05-22-wcm-frontend-design.md](docs/plans/2026-05-22-wcm-frontend-design.md) for the design.

## Repository layout

- `worker/` — Dockerised wcEcoli runner.
- `tests/` — Host-side integration tests (pytest).
- `docs/` — Design docs and plans.

## Quick start

```bash
make smoke    # build the image and run a smoke simulation
```
```

**Step 3: Create MIT `LICENSE`**

Standard MIT licence text, year `2026`, author `<your name>` — leave a `TODO` for the author line if unknown.

**Step 4: Create empty `Makefile` placeholder**

```makefile
.PHONY: help
help:
	@echo "Targets: build, smoke, clean (added in later tasks)"
```

**Step 5: Verify and commit**

```bash
ls -la
git status
git add .gitignore README.md LICENSE worker/.gitkeep tests/.gitkeep Makefile
git commit -m "chore: project scaffolding"
```

Expected: clean working tree after commit.

---

## Task 2: Vendor wcEcoli as a submodule + discovery

**Files:**
- Create: `vendor/wcEcoli` (submodule)
- Create: `.gitmodules` (auto-generated)
- Create: `docs/notes/wcecoli-build-notes.md`

**Step 1: Add the submodule**

```bash
git submodule add https://github.com/CovertLab/wcEcoli.git vendor/wcEcoli
cd vendor/wcEcoli
git log -1 --format="%H %ai"
cd ../..
```

Note the SHA — this is the pinned version. Record it in the build notes.

**Step 2: Pin to a known-good SHA**

```bash
cd vendor/wcEcoli
git checkout <SHA>    # use the HEAD SHA from Step 1
cd ../..
git add vendor/wcEcoli
```

**Step 3: Discovery — read wcEcoli's docs**

Read these files inside `vendor/wcEcoli/`:
- `README.md`
- `docs/README.md` (if it exists)
- Any file matching `*docker*` or `Dockerfile*`
- `requirements.txt` / `pyproject.toml` / `setup.py`
- `runscripts/manual/runSim.py` or equivalent entry-point script

**Step 4: Write build notes**

Create `docs/notes/wcecoli-build-notes.md` documenting:

- Pinned SHA (with commit date)
- Required Python version
- System dependencies (apt packages, compilers, OpenBLAS, glpk, etc.)
- The canonical "run a simulation" command upstream uses
- Path to default output directory
- Any known gotchas from the README (e.g. Python 3.11 only, requires specific OpenBLAS version, …)
- Whether upstream provides a Dockerfile and why we are or aren't using it

**Step 5: Commit**

```bash
git add .gitmodules vendor/wcEcoli docs/notes/wcecoli-build-notes.md
git commit -m "chore: vendor wcEcoli at <SHA prefix>"
```

**Stop here.** Show the build notes to the user before continuing — Task 3's Dockerfile depends on them.

---

## Task 3: Write the Dockerfile (red phase)

**Files:**
- Create: `worker/Dockerfile`
- Create: `worker/.dockerignore`
- Create: `tests/test_image_builds.py`
- Modify: `Makefile`

**Step 1: Write the failing test first**

Create `tests/test_image_builds.py`:

```python
import subprocess
import pytest

IMAGE_TAG = "wcm-ui/worker:test"

@pytest.fixture(scope="session")
def built_image():
    """Build the worker image once per test session."""
    result = subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, "-f", "worker/Dockerfile", "."],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"docker build failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return IMAGE_TAG

def test_image_has_python(built_image):
    result = subprocess.run(
        ["docker", "run", "--rm", built_image, "python3", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.startswith("Python 3."), result.stdout

def test_image_has_wcecoli(built_image):
    """wcEcoli's main entry point should be importable inside the container."""
    result = subprocess.run(
        [
            "docker", "run", "--rm", built_image,
            "python3", "-c", "import wholecell; print('ok')",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"import failed:\n{result.stderr}"
    assert "ok" in result.stdout
```

**Step 2: Run the test to verify it fails**

```bash
pytest tests/test_image_builds.py -v
```

Expected: FAIL (Dockerfile doesn't exist yet).

**Step 3: Write `worker/.dockerignore`**

```
.git
.venv
out/
tests/
docs/
*.md
__pycache__/
```

Do NOT exclude `vendor/wcEcoli` — the build needs it.

**Step 4: Write `worker/Dockerfile`**

Use a multi-stage build. The exact base image and apt packages depend on Task 2's findings. Example skeleton (adjust per build notes):

```dockerfile
# syntax=docker/dockerfile:1.7

FROM python:3.11-slim-bookworm AS base

# System deps required by wcEcoli — verify list against build notes
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gfortran \
    libopenblas-dev \
    libglpk-dev \
    swig \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/wcecoli

# Copy only requirements first for layer caching
COPY vendor/wcEcoli/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full source
COPY vendor/wcEcoli/ ./

# Build C extensions if wcEcoli has any (per build notes)
RUN python3 setup.py build_ext --inplace || true

ENV PYTHONPATH=/opt/wcecoli
WORKDIR /work

CMD ["python3", "-c", "import wholecell; print('wcEcoli importable')"]
```

**Step 5: Run the test again**

```bash
pytest tests/test_image_builds.py -v
```

Expected: PASS.

If it fails, apply @superpowers:systematic-debugging. Likely failure modes:
- Missing apt package — read the build log, add it to the `RUN apt-get install` line.
- `pip install` fails on a wheel that has no Linux binary — may need to add `libsomething-dev` to apt deps.
- Module import path is wrong — wcEcoli's package is likely `wholecell` but verify with `ls vendor/wcEcoli/`.

Do NOT relax the test to make it pass. If wcEcoli genuinely can't import, the Dockerfile is broken — fix it.

**Step 6: Add `Makefile` targets**

Replace the placeholder `Makefile` with:

```makefile
IMAGE_TAG ?= wcm-ui/worker:dev

.PHONY: help build test-image clean

help:
	@echo "Targets:"
	@echo "  build       Build the worker Docker image"
	@echo "  test-image  Run image build/import tests"
	@echo "  clean       Remove local build artefacts"

build:
	docker build -t $(IMAGE_TAG) -f worker/Dockerfile .

test-image:
	pytest tests/test_image_builds.py -v

clean:
	rm -rf out/ __pycache__/ .pytest_cache/
```

**Step 7: Commit**

```bash
git add worker/Dockerfile worker/.dockerignore tests/test_image_builds.py Makefile
git commit -m "feat(worker): dockerfile that builds and imports wcEcoli"
```

---

## Task 4: Smoke test — run an actual minimal simulation

**Files:**
- Create: `worker/smoke.py`
- Create: `tests/test_smoke_sim.py`
- Modify: `Makefile`

**Step 1: Write the failing test**

Create `tests/test_smoke_sim.py`:

```python
import subprocess
from pathlib import Path
import pytest

IMAGE_TAG = "wcm-ui/worker:test"

def test_smoke_sim_produces_outputs(tmp_path):
    """Run the smallest possible wcEcoli sim and check expected files appear."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{out_dir}:/work/out",
            IMAGE_TAG,
            "python3", "/opt/wcecoli/smoke.py",
        ],
        capture_output=True,
        text=True,
        timeout=1800,  # 30 min ceiling
    )
    assert result.returncode == 0, (
        f"smoke sim failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    # Expected outputs — adjust per Task 2 build notes about default output paths
    produced = list(out_dir.rglob("*"))
    assert produced, "no output files written"

    # A canonical wcEcoli output — verify the exact name during Task 2 discovery
    assert any(p.name == "simOut" or "Daughter" in p.name or p.suffix == ".cPickle"
               for p in produced), \
        f"no recognised wcEcoli outputs in {[p.name for p in produced]}"
```

**Step 2: Run it to verify it fails**

```bash
pytest tests/test_smoke_sim.py -v
```

Expected: FAIL (`smoke.py` doesn't exist in the image yet).

**Step 3: Write `worker/smoke.py`**

This script invokes the canonical wcEcoli entry point with the minimum work. Use the entry point identified in Task 2's build notes. Skeleton:

```python
"""Smoke simulation: smallest possible wcEcoli run.

Used by tests and CI to verify the image is functional. Not part of the
production parameter-injection path — that's Stage 1b.
"""
import os
import subprocess
import sys
from pathlib import Path

OUT_DIR = Path("/work/out")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# The exact command depends on wcEcoli's runscripts — confirmed in Task 2.
# Typical incantation:
#   python3 runscripts/manual/runFitter.py --cpus 1
#   python3 runscripts/manual/runSim.py --total-time 60 --generations 1
#
# Adjust the command and flags to match what was documented in
# docs/notes/wcecoli-build-notes.md.

cmd = [
    "python3", "/opt/wcecoli/runscripts/manual/runSim.py",
    "--total-time", "60",
    "--generations", "1",
    "--out-dir", str(OUT_DIR),
]

print(f"+ {' '.join(cmd)}", flush=True)
result = subprocess.run(cmd, cwd="/opt/wcecoli")
sys.exit(result.returncode)
```

**Step 4: Update the Dockerfile to include `smoke.py`**

Add before the `CMD` line in `worker/Dockerfile`:

```dockerfile
COPY worker/smoke.py /opt/wcecoli/smoke.py
```

Replace the `CMD` with:

```dockerfile
CMD ["python3", "/opt/wcecoli/smoke.py"]
```

**Step 5: Rebuild and run the test**

```bash
make build
pytest tests/test_smoke_sim.py -v
```

Expected: PASS.

This is the highest-risk task in this plan. Failure modes are domain-specific. Likely issues:
- wcEcoli requires a "parca" (parameter calculator) step before any sim. The smoke script may need to invoke `runscripts/manual/runFitter.py` first and pass its output to `runSim.py`.
- `--total-time 60` may not be a valid flag. Inspect the script's `argparse` definition.
- Default output directory may be `out/manual/` not `/work/out/`. Adjust the volume mount or use `--out-dir`.
- The run may exceed the 30-minute timeout. If so, increase only after confirming the params are truly minimal.

Use @superpowers:systematic-debugging — read `result.stdout` and `result.stderr` carefully before changing anything.

**Step 6: Add `make smoke` target**

Append to `Makefile`:

```makefile
.PHONY: smoke
smoke: build
	pytest tests/test_smoke_sim.py -v
```

**Step 7: Commit**

```bash
git add worker/smoke.py worker/Dockerfile tests/test_smoke_sim.py Makefile
git commit -m "feat(worker): smoke simulation runs end-to-end in container"
```

---

## Task 5: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

**Step 1: Write the workflow**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  worker:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install test deps
        run: pip install pytest

      - name: Build worker image
        run: docker build -t wcm-ui/worker:test -f worker/Dockerfile .

      - name: Run image tests
        run: pytest tests/test_image_builds.py -v

      - name: Run smoke simulation
        run: pytest tests/test_smoke_sim.py -v
```

**Step 2: Verify locally that the equivalent commands all pass**

```bash
pytest tests/ -v
```

Expected: all tests PASS. Do not push CI that you haven't validated locally.

**Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: build worker image and run smoke test on push/PR"
```

**Step 4: Push and observe CI**

```bash
git remote -v   # if no remote, ask user before adding one
git push -u origin main
```

Watch the Actions tab. The smoke test is the long step — expect 15-45 minutes on `ubuntu-latest`. If it times out, the `timeout-minutes: 60` cap will need raising, but first confirm the sim parameters are truly minimal.

---

## Definition of Done for Stage 1

- [ ] `git clone --recurse-submodules <repo> && make smoke` works on a clean macOS / Linux machine with Docker installed.
- [ ] CI is green on `main`.
- [ ] `docs/notes/wcecoli-build-notes.md` documents the pinned SHA, build prerequisites, and the canonical run command.
- [ ] No untracked files, no commented-out debug code, no `TODO` placeholders left in committed code.

## What Stage 1b will add (do not implement here)

- A JSON Schema defining the curated parameter set.
- A `worker/run.py` that reads `$PARAMS_JSON`, validates against the schema, merges with defaults and the YAML override, and invokes wcEcoli with the resolved parameters.
- Tests that drive the worker with several parameter sets and assert outputs differ accordingly.

## Risks and unknowns

- **wcEcoli build complexity.** If `pip install -r requirements.txt` fails on a transitive C dependency, expect a half-day of dependency archaeology. Budget for this.
- **wcEcoli runtime.** Even a "minimal" sim may take 10-30 minutes. If the smoke test is too slow for CI, consider running it only on a nightly schedule and keeping a faster `test_image_builds.py` on every push.
- **Upstream churn.** Pinning to a SHA mitigates this. Do not use a moving branch like `master`.
- **Platform.** macOS Docker Desktop uses Apple's Virtualization framework; Linux CI uses native containers. Build outputs should be identical, but `.cPickle` files written on one architecture may not load on another. If issues arise, force `--platform linux/amd64` everywhere.
