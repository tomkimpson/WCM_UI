# wcEcoli Build Notes

These notes capture what Task 3 (Dockerfile) and Task 4 (smoke sim) need to know
about the vendored wcEcoli source. Update this file whenever the pinned SHA
changes.

## Pinned version

- SHA: `3fc8ec1f0ca5451f1e68351a3e288ab377931043`
- Commit date: `2026-04-30 08:04:42 -0700`
- Branch at time of vendoring: `master` (upstream default)
- Source: https://github.com/CovertLab/wcEcoli
- Commit subject: "Transcription factor binding listener update (#1472)"

## Required Python version

- `3.11.3` (exact, as used by upstream's Docker base image and pyenv instructions).
- Source of truth in upstream:
  - `vendor/wcEcoli/cloud/docker/runtime/Dockerfile` line 14: `ARG from=python:3.11.3`
  - `vendor/wcEcoli/requirements.txt` lines 21-26 (pyenv instructions: `pyenv install 3.11.3`).
- There is no `python_requires` in `setup.py` (it's a minimal Cython build script,
  not a packaging manifest). `pyproject.toml` does not exist.

## System dependencies

OS-level packages, sourced from upstream's `cloud/docker/runtime/Dockerfile` and
`docs/create-pyenv.md`. The Docker route is intentionally narrower than the
pyenv route — many of the pyenv-only packages exist only to let `pyenv` build
Python itself, which we don't need when starting from `python:3.11.3` slim/bookworm.

Minimum apt packages required when building from `python:3.11.3` (matches
upstream `cloud/docker/runtime/Dockerfile`):

- `swig` — required to build `swiglpk==5.0.8` Python bindings against GLPK at pip install time.
- `gfortran` — Fortran compiler; needed for scientific stack builds (and required if `COMPILE_BLAS=1`).
- `llvm` — required by `numba==0.58.1` / `llvmlite==0.41.1` runtime.
- `cmake` — required to build `osqp` and `qdldl` from source (cvxpy dependencies).
- `nano` — convenience editor; can be dropped from our Dockerfile.

Implicit (already present in `python:3.11.3` Debian-based image): `gcc`, `g++`,
`make`, `git`, `curl`. We must verify these are present on whichever base image
we choose; the upstream Dockerfile does not reinstall them.

OpenBLAS: upstream's default in 2026 is to let the `numpy`/`scipy` wheels supply
their bundled OpenBLAS — they do NOT install `libopenblas-dev` from apt
(`docs/create-pyenv.md` lines 76-86 explicitly warns against it because the
Debian/Ubuntu repo version lags behind). They optionally compile OpenBLAS
v0.3.27 from source when `--build-arg COMPILE_BLAS=1` is passed (default 0).
For our Dockerfile: do NOT pass `COMPILE_BLAS=1`; rely on wheel-bundled OpenBLAS
and set `OPENBLAS_NUM_THREADS=1` (see "Gotchas" below).

GLPK: upstream installs nothing for GLPK on the Docker path beyond `swig`. The
`swiglpk` pip package vendors its own GLPK build. On the pyenv path they note
`brew install glpk` (macOS) or `apt install glpk-utils libglpk-dev` (Ubuntu).
For our Dockerfile, follow the upstream Docker recipe and rely on swig only;
if `pip install swiglpk` fails we can add `libglpk-dev` later.

## Python dependencies file

- Path: `vendor/wcEcoli/requirements.txt`
- Pinning style: exact pins (`==`) for almost every direct and transitive
  package. A few installer-tier packages use `>=` (`pip>=23.1`,
  `virtualenv>=20.21.0`, `wheel>=0.40.0`).
- `numpy==1.26.3` must be installed **before** `pip install -r requirements.txt`
  in a separate step. Upstream does this in their Dockerfile (line 80) because
  some downstream packages (notably `scipy`, `numba`) need numpy already present
  at build time. Replicate this two-phase install in our Dockerfile.
- Notable pinned packages that depend on OS support:
  - `numpy==1.26.3`, `scipy==1.11.4` — need a working C/Fortran toolchain even
    though we install wheels; failing wheels would fall back to source builds.
  - `Cython==0.29.35` — required to compile wcEcoli's own `.pyx` files via
    `make compile`.
  - `numba==0.58.1` / `llvmlite==0.41.1` — need `llvm` apt package.
  - `cvxpy==1.3.2`, `osqp==0.6.2.post9`, `qdldl==0.1.7` — need `cmake` apt
    package if wheels are unavailable for the platform.
  - `swiglpk==5.0.8` — needs `swig` apt package.
  - `aesara==2.9.3` — locked to `setuptools==73.0.1`; upstream's comment notes
    `setuptools>=74.0.0` breaks aesara.
  - `pymongo[ocsp]==4.3.3` — pulls in `cryptography`, `pyOpenSSL`; harmless
    in our container but noted because it pulls compiled extensions.
- Note the trailing constraint `urllib3<2` (line 206) — keep this in mind if
  we add anything that requests a newer urllib3.

## Canonical run commands

The minimum sequence to run a simulation, taken from upstream
`README.md` (top-level "Quick start" + "Using the manual runscripts" sections)
and `cloud/docker/wholecell/Dockerfile`:

```bash
# Inside the container, working dir = /wcEcoli (or wherever the source lives)
export PYTHONPATH="$PWD"

# Build the Cython extensions (once, at image build time)
make clean compile

# Step 1 — parameter calculator (ParCa). Writes to out/manual/kb/simData.cPickle.
python runscripts/manual/runParca.py

# Step 2 — simulation. Reads kb/simData.cPickle, writes out/manual/.../simOut/.
python runscripts/manual/runSim.py
```

For each command:

- **ParCa must run before sim.** Source: `runscripts/manual/runSim.py` lines
  81-82 explicitly call `fp.verify_file_exists(sim_data_file, 'Run runParca?')`
  on `kb/simData.cPickle` and abort if missing. README.md "Using the manual
  runscripts" also states "you're responsible for properly sequencing all the
  steps: parameter calculation, cell simulation generations, and analyses."
- **Default output location:** under `<repo_root>/out/<sim_outdir>/` where
  `sim_outdir` defaults to `manual`. Source: `runscripts/manual/runParca.py`
  lines 27-30 (`default='manual'`) and `wholecell/utils/filepath.py` line 21
  (`OUT_DIR = os.path.join(ROOT_PATH, 'out')`). With the upstream Dockerfile's
  `WORKDIR /wcEcoli`, output ends up at `/wcEcoli/out/manual/`.
- **Smallest possible run flags (for Task 4 smoke sim):**
  - `runParca.py`: defaults are already minimal (single CPU). Optionally add
    `-c 1` for explicit single-CPU; `--cpus` defaults to 1.
  - `runSim.py`: defaults to `--generations 1`, `--init-sims 1`, and the wild-type
    variant. To get the shortest possible sim time, add the upstream sim option
    `--length-sec N` where N is short (default cell-cycle is hours of sim time;
    upstream supports cutting it short). Confirm exact flag name via
    `python runscripts/manual/runSim.py -h` inside the built container during
    Task 4 — the option is defined in `scriptBase.define_sim_options`.

## Default output directory

- Path inside container (upstream Dockerfile sets `WORKDIR /wcEcoli`):
  `/wcEcoli/out/manual/`
  - `kb/` — sim data (ParCa output)
  - `wildtype_000000/000000/generation_000000/000000/simOut/` — per-cell sim
    output for variant=wildtype, seed=0, generation=0, daughter=0
  - `metadata/` — `metadata.json` with git hash, branch, run description
- File types written: `.cPickle` (sim data), `.json` (metadata), `.tsv` /
  numeric arrays (sim listeners), plus `.png` / `.pdf` (only if analysis
  scripts are run — not required for a smoke sim).
- One canonical file we can assert exists to know ParCa succeeded:
  `/wcEcoli/out/manual/kb/simData.cPickle`
  (constant `SERIALIZED_SIM_DATA_FILENAME` in `wholecell/utils/constants.py`).
- One canonical directory to assert exists to know runSim succeeded:
  `/wcEcoli/out/manual/wildtype_000000/000000/generation_000000/000000/simOut/`
  (path constructed in `runscripts/manual/runSim.py` lines 145-156).

## Gotchas and known issues

Sourced from upstream README, `docs/README.md`, `docs/create-pyenv.md`, and
the runtime Dockerfile comments:

- **`OPENBLAS_NUM_THREADS=1` is mandatory.** Upstream sets this as an `ENV` in
  the runtime Dockerfile (line 68) and re-emphasizes it in `requirements.txt`
  lines 12-17. Without it OpenBLAS produces slightly different numerical
  results and runs significantly slower when called from multiple processes.
  **Our Dockerfile must export this.**
- **Memory:** upstream `docs/README.md` lines 28-30 warns: "Open Docker's
  Advanced Preferences and increase the memory allocation to 4GB. (The default
  allocation is 2GB which would make the model's Python code run out of memory,
  print 'Killed', and stop with exit code 137.)" Worker hosts running our
  container must provide at least 4 GB RAM per concurrent sim.
- **Do NOT use Alpine as the base image.** `cloud/docker/runtime/Dockerfile`
  line 13 explicitly warns: "DO NOT USE an alpine base since the simulation
  math comes out different!". Stick with `python:3.11.3` (Debian-based).
- **macOS / Apple Silicon Docker Desktop:** upstream documents an OpenBLAS AVX2
  bug specific to Docker-on-Mac (`docs/README.md` lines 50-55,
  `cloud/locally-build-runtime.sh` lines 11-15). They work around it by passing
  `NO_AVX2=1` when `COMPILE_BLAS=1`. We are NOT compiling OpenBLAS (we use the
  numpy/scipy wheel-bundled copy), so this bug shouldn't bite us — but flag for
  Task 3 testing if results look wrong on a Mac CI runner.
- **Aesara cache directory:** the upstream wcm-code Dockerfile creates
  `/.aesara` with `umask 000` so non-root users (and users without a home dir,
  e.g. `docker run --user $(id -u):$(id -g)`) can write into it. Replicate
  this in our Dockerfile (`mkdir -p /.aesara && chmod 777 /.aesara`) or set
  `AESARA_FLAGS=base_compiledir=/tmp/aesara` instead.
- **`setuptools` version cap:** `requirements.txt` line 51 pins
  `setuptools==73.0.1` because `>=74.0.0` breaks Aesara (distutils removal).
  Make sure our pip-upgrade step does not blow past this.
- **PYTHONPATH must be set to the repo root.** `runscripts/manual/runParca.py`
  docstring says "Set PYTHONPATH when running this." The upstream wcm-code
  Dockerfile does `ENV PYTHONPATH=/wcEcoli` (line 57). Replicate.
- **`make clean compile` is mandatory before any run.** It builds three Cython
  extensions (`wholecell/utils/_build_sequences.pyx`, `mc_complexation.pyx`,
  `_fastsums.pyx`). README "Quick start" steps 1-3.

## Dockerfile decision

Does upstream ship a Dockerfile? **Yes — two of them**:

1. `cloud/docker/runtime/Dockerfile` builds `wcm-runtime` (base image #1 —
   Python 3.11.3 + apt packages + pip-installed requirements).
2. `cloud/docker/wholecell/Dockerfile` builds `wcm-code` on top of `wcm-runtime`
   (image #2 — `COPY . /wcEcoli`, `make clean compile`).

The Stage 1 plan calls for us to write our own. After reading what upstream
provides, the recommendation remains: **write our own single-stage Dockerfile**,
but treat the upstream Dockerfiles as the authoritative recipe. Specifically:

- Their two-image split (runtime / code) is useful for cloud-build caching but
  unnecessary for our Stage 1 worker. One stage is simpler.
- We pin a specific upstream SHA via submodule, so we don't inherit upstream
  changes accidentally — but we should re-run `make compile` whenever the
  pinned SHA changes (Task 3 should COPY the submodule and `make compile`).
- The decision to write our own avoids two coupling problems with upstream:
  (a) they expect the repo to BE the build context (`COPY . /wcEcoli`), but
  for us the build context will be the WCM_UI repo with wcEcoli at
  `vendor/wcEcoli/`; (b) we may want to layer worker-side helpers (REST
  client, supervisor, etc.) on top, which is awkward to do on a `FROM wcm-code`
  image we don't own.
- Practically: copy the steps from `cloud/docker/runtime/Dockerfile` lines
  14-83 and `cloud/docker/wholecell/Dockerfile` lines 53-67 into our own
  `worker/Dockerfile`, adapting paths so the build context is the WCM_UI repo
  root and the wcEcoli source comes from `vendor/wcEcoli/`.

**Confirmed: write our own Dockerfile, port the upstream recipe.**
