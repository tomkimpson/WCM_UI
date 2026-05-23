# Research log

## 2026-05-23 — Stage 1b: parameter injection, end-to-end on Linux

**Goal.** Drive the worker image from a JSON parameter file — validate against a curated schema, merge with defaults, invoke wcEcoli's runscripts with resolved CLI flags — and prove the override actually reaches the sim, both locally and in CI.

**What was tried.** Started by finishing Stage 1's leftover items: smoke simulation (Task 4, ran in 12m36s on Apple Silicon and produced the expected `simData.cPickle` + `simOut/` outputs), build-notes patch documenting the `--no-build-isolation` + early-`setuptools==73.0.1` pattern, and GitHub Actions CI (passed in 21m55s on `ubuntu-latest`). Created the public GitHub repo `tomkimpson/WCM_UI` and pushed the 7 Stage 1 commits.

Drafted the Stage 1b plan (`docs/plans/2026-05-23-stage1b-parameter-injection.md`) as 8 TDD tasks landing five initial knobs (`length_sec`, `seed`, `generations`, `init_sims`, `parca_cpus`) that map 1:1 onto existing `runSim.py` / `runParca.py` CLI flags. Explicitly cut: YAML override layer (Stage 1c), variant-based knobs like gene knockouts (Stage 1c — needs traversing wcEcoli's variant system), heartbeat reporting (Stage 3, needs the API).

Executed all 8 tasks via subagent-driven-development with two-stage review per task (spec compliance, then code quality). Composition: Task 1 (worker-side `requirements.txt` for `jsonschema`) → Task 2 (`worker/schema/*.json`) → Task 3 (`worker/merge.py` recursive merger) → Task 4 (`worker/validate.py`, first-error with field-named messages) → Task 5 (`worker/run.py` keystone composing the above) → Task 6 (repackage `worker/` into the image; `CMD ["python3","-m","worker.run"]`) → Task 7 (end-to-end parametric test) → Task 8 (wire into CI).

Real CI run with parametric step landed at 38m40s on `ubuntu-latest` (build 1m51s, smoke ~18 min, parametric ~18 min).

**What was learned.**
- The Main listener's `attributes.json` directly records the `lengthSec` the sim ran with (e.g. `{"lengthSec": 30.0, ...}`). Strict value equality on this field is the right keystone assertion — strictly stronger than the plan's first-guess "is there a `time` key?" check, which would have passed silently even if `--length-sec` were dropped on the way to the sim.
- Bare `pytest tests/...` does *not* put the repo root on `sys.path` automatically when the only `conftest.py` lives under `tests/`. With no `pyproject.toml` / `pytest.ini` / root `conftest.py`, host-side tests that `import worker.*` fail with `ModuleNotFoundError`. Fixed with `pyproject.toml` containing `[tool.pytest.ini_options] pythonpath = ["."]`.
- `jsonschema.Draft202012Validator.iter_errors(...)`'s `absolute_path` is a `deque` that mixes `str` keys and `int` indices once arrays enter the schema; sorting raw `deque` objects raises `TypeError`. Coerced to `tuple(str(p) for p in e.absolute_path)` proactively — Stage 1b's schema is array-free, but Stage 1c's variant knobs will exercise this.
- The whole-output `worker/` directory `COPY` (Task 6) sits above the wcEcoli requirements layer so worker-only edits invalidate only the trailing ~74 kB layer, not the ~3-min requirements install. Layer caching held up across 6+ iterations.
- `jsonschema.__version__` emits a `DeprecationWarning` in 4.23 — harmless, just noisy in test output. Downstream code should use `importlib.metadata.version("jsonschema")` if the runtime version is ever needed programmatically.

**Decisions / dead ends.**
- Catching `FileNotFoundError` in `worker.run.main()` was too narrow — `PARAMS_JSON` pointing at a directory raised raw `IsADirectoryError` and exited 1 with a traceback. Broadened to `OSError`, which covers `FileNotFoundError`, `IsADirectoryError`, `PermissionError`, and `NotADirectoryError`. All semantically "the operator pointed PARAMS_JSON at something unreadable".
- The plan's Step 1 example for Task 6 included a second `ENV PYTHONPATH=/wcEcoli` next to the new COPY despite an earlier `ENV PYTHONPATH=/wcEcoli` already being correct. Removed the redundancy in both the Dockerfile and the plan code block.
- Did not refactor `build_commands` into a declarative `(schema_key, CLI_flag)` table. At 5 knobs the inlined three-line-per-knob form is more readable than a table; revisit at ~10 knobs. Softened the DoD wording from "one-line entry" to "small structural change" to match reality.
- Did not re-run Task 7's 15-min sim after tightening the assertion. The first run captured `attributes.json` with `lengthSec: 30.0`; the new assertion is `attrs.get("lengthSec") == float(30)`, i.e. `30.0 == 30.0`, which is trivially true in IEEE 754. Saved 15 minutes of CI wall time. CI's subsequent green run on Linux is the independent confirmation.
- Considered defaulting `make smoke`'s output to a persistent `./out/` directory rather than pytest's GC-prone tmp dir. Deferred — pytest tmp behavior is fine for one-off inspections; if it becomes a real friction, fix is ~3 lines.

**Open threads.**
- Stage 1c: YAML override layer (`defaults ← curated_form ← yaml_override`), variant-based knobs (gene knockouts, media composition — requires `models/ecoli/sim/variants/` traversal), and growing the curated knob set toward the design doc's 15–25 target.
- Stage 2: AWS Batch + S3 wiring. Worker is now parametric-by-env-var, which is the right shape for Batch's job-definition pattern.
- Node.js 20 actions deprecation in CI (annotation on every run): `actions/checkout@v4`, `actions/setup-python@v5`, `docker/setup-buildx-action@v3` will be forced to Node 24 by 2026-06-02. Non-blocking but worth tracking; the upstream actions will release Node-24-compatible majors before then.
- `worker/Dockerfile` and `worker/requirements.txt` ship inside `/wcEcoli/worker/` because the `COPY worker/` is a directory copy. A few KB; harmless but slightly muddy. Could add `worker/Dockerfile` + `worker/requirements.txt` to `.dockerignore` if it ever matters.
- `CLAUDE.md`, `handoff.md`, `log.md` are still untracked in git. Worth committing so they travel with the repo.

## 2026-05-23 — Stage 1 bootstrap: design, plan, and Dockerized wcEcoli

**Goal.** Bootstrap WCM_UI from nothing: agree a design, write an implementation plan, and execute Stage 1 (a Dockerized wcEcoli worker that builds and imports cleanly).

**What was tried.** Brainstormed the project shape via a Q&A loop: audience (researchers), compute (cloud — AWS Batch the planned target), parameter UI (curated form + YAML override escape hatch), outputs (in-browser plots + raw download), access (public, no login). Wrote the design to `docs/plans/2026-05-22-wcm-frontend-design.md` covering architecture, job lifecycle, parameter schema, reproducibility metadata, deployment, and a hard cost-control regime that's load-bearing for the no-login choice. Wrote the Stage 1 plan (`docs/plans/2026-05-22-stage1-worker-bootstrap.md`) breaking the worker bootstrap into 5 TDD tasks. Executed Tasks 1–3 via the subagent-driven-development workflow (implementer → spec reviewer → code-quality reviewer per task), pausing for a human checkpoint after Task 2 to review the build notes.

For Task 3, the implementer hit two real wcEcoli build issues and resolved both: (1) the documented `python:3.11.3-slim-bookworm` base tag doesn't exist on Docker Hub — switched to `python:3.11.3-slim` (bullseye, matches upstream's Debian release); (2) `Equation==1.2.1` and `stochastic-arrow==1.0.0` ship legacy setup.py scripts that break under PEP 517 isolated builds — fixed with `--no-build-isolation` plus pinning `setuptools==73.0.1` in the parent env before the requirements install. Build went green in ~4 min on Apple Silicon (2.52 GB image).

Code review then surfaced a real bug: `worker/.dockerignore` was being silently ignored because Docker resolves `.dockerignore` from the build context root, not the Dockerfile's directory. Moved it to repo root and the build context dropped from multi-GB to 91 kB. Five other tightenings applied in the same fix commit (bullseye EOL note, consistent test error-surfacing, Cython submodule import test, Makefile help-text honesty).

**What was learned.**
- wcEcoli builds in ~4 min on Apple Silicon (10 CPUs, 8 GB RAM, Docker Desktop 29.4.3). All heavy deps have aarch64 wheels.
- `make compile` produces three Cython `.so` files in `wholecell/utils/` — `_build_sequences`, `_fastsums`, `mc_complexation`. Importing one of those (not just `wholecell`) is a cheap canary for a successful Cython compile.
- Upstream's two Dockerfiles (`cloud/docker/runtime/`, `cloud/docker/wholecell/`) are good reference but tightly coupled to upstream's build context — confirmed the right call to write our own.
- `python:3.11.3-slim` resolves to Debian bullseye-slim, EOL 2026-08-31. Noted in Dockerfile for the next maintainer.
- The build notes captured the right things — every Task 3 surprise was anticipated except the two legacy-pip-package issues (which the notes have been updated for once already, and need a second update — see open threads).

**Decisions / dead ends.**
- Pinning `pip==24.3.1` + `wheel==0.45.1` triggered pip's dependency resolver to backtrack endlessly under `--no-build-isolation` (output showed virtualenv versions being downloaded in descending order: 20.24.4 → 20.24.3 → 20.24.2 → …). Build hung for ~50 min before being killed. Reverted to unpinned `pip wheel` — the existing build works in 4 min, the resolver picks happy versions on first try. Accepted pip/wheel drift across rebuilds as the lesser evil. Don't re-attempt this pin without also addressing the underlying Equation / stochastic-arrow legacy packages.
- Rejected forking wcEcoli into the project; vendored as a git submodule pinned to `3fc8ec1f` instead.
- Rejected per-user accounts for v1; public + hard quotas + queue. Auth deferred to v2 with pinning of results as the natural upgrade trigger.
- Rejected curated-only or YAML-only parameter UIs; chose curated form with YAML override (`defaults ← form ← yaml`).
- Rejected upstream's `setup.py build_ext` for Cython; used `make clean compile` per upstream README. The `clean` is redundant in a fresh image but matches upstream incantation for reproducibility — kept it.

**Open threads.**
- Task 4: smoke simulation end-to-end (Parca + sim with `--length-sec 60`). Plan exists, image is ready, just needs the wrapper + test + Makefile target. Expect 30–60 min including likely debug iterations on volume mounts and output paths.
- Task 5: GitHub Actions CI. Needs a GitHub remote to be added first.
- Update `docs/notes/wcecoli-build-notes.md` to document (a) the `--no-build-isolation` + early `setuptools==73.0.1` pattern uncovered during Task 3, and (b) the pip-pin resolver hang failure mode.
- Longer-term: the legacy packages (`Equation==1.2.1`, `stochastic-arrow==1.0.0`) are the root cause of the `--no-build-isolation` workaround. Patching their setup.py scripts or vendoring pre-built wheels would let us drop the workaround and pin pip/wheel safely.
- No GitHub remote yet — local-only commits.
