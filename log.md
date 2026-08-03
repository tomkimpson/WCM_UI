# Research log

## 2026-08-02 — Stage 3: the API and quota layer, built before its infrastructure

**Goal.** Build Stage 3 of the build order — a FastAPI service with
submit/status/results endpoints, hard cost ceilings, and a Cloud Run deployment.

**What was tried.** Started by planning rather than coding, because the design
doc (`docs/plans/2026-05-22-wcm-frontend-design.md`) is three months old and
predates the AWS→GCP pivot. Ran three parallel design passes — API surface,
quota enforcement, infra/CI — then reconciled them into
`docs/plans/2026-08-02-stage3-api-and-quotas.md` as two phases: 3a for the
substrate ending in a live URL, 3b for the application.

Phase 3a's ordering rationale was to surface the production-only IAM failures
early. That collapsed on discovering **this machine has neither Docker nor the
gcloud SDK** — Stage 1 and 2 were built elsewhere. Since no 3a step could be
verified here and all of 3b is pure Python against mocked GCP clients, the
phases were run in reverse. Tasks 1, 2 and 8–20 landed; Tasks 3–7 (image, IAM,
Cloud Run service, CI graph, deploy smoke) are deferred to a machine with the
tooling. Recorded in the plan doc so it isn't mistaken for drift.

Two claims in the plan were verified against the installed
`google-cloud-run==0.16.0` before any code depended on them, because both were
sourced from documentation rather than the library.

**What was learned.**

- **`Overrides.ContainerOverride` has no `image` field** — its fields are exactly
  `name`, `args`, `env`, `clear_args`. So the API *cannot* pin an execution to a
  digest; whatever the Job spec holds is what Cloud Run pulls. Resolving a tag to
  a digest at submit time would record a guess that races CI's next
  `docker push :latest`. The flow is inverted instead: CI pins the Job spec, and
  `api/provenance.resolve_image_pin` reads the digest back off the spec, where it
  is authoritative by construction. A test now asserts the field's absence so
  this becomes an enforced pin if the library ever grows it.
- **`Overrides.timeout` does exist**, is a protobuf `Duration`, and proto-plus
  accepts a `timedelta`. This matters more than it looks: the quota lease TTL
  evicts a lease at `cap + grace`, and that eviction is only *safe* — rather than
  a guess that could reclaim a slot from a live run — because the platform
  really kills the task at the cap.
- **Concurrency is not a cost control.** Re-deriving the cost model for Cloud Run
  Jobs (4 vCPU + 16 GiB = $0.000104/s = $0.3744/job-hour; the measured 26-minute
  run = $0.162) shows the doc's ceiling of 4 concurrent runs permits ~221
  runs/day ≈ **$1,093/month**. Concurrency bounds the rate, not the total. A
  global daily cap was added and is the only ceiling that bounds monthly spend.
  Per-IP caps don't help — an IPv6 subscriber owns at least a /64.
- **The doc's "spot only, ~70% savings" doesn't transfer.** Cloud Run Jobs bill
  per-second at on-demand rates, so the cost basis underwriting the no-login
  decision was wrong. Worst case with the new ceilings is ~$150/month against an
  expected ~$5.
- **Egress was entirely unpriced.** GCS egress is $0.12/GB and the tarball size
  has never been measured; at ~1 GB, one download costs most of what the run cost
  to produce, and downloads are repeatable. No compute ceiling touches it.
  Metering was added at signed-URL mint time.
- **`fastapi==0.141.1` allows `starlette>=0.46.0` unbounded**, and starlette has
  since gone 1.x. Release dates settled it: fastapi 0.141.1 shipped 2026-07-29,
  six weeks *after* starlette 1.3.1, so 1.x is intended — but it is pinned
  explicitly so a future 2.0 can't arrive on its own. Downstream consequence:
  starlette 1.x's TestClient wants `httpx2`, so `requirements-dev.txt` uses that
  instead of `httpx`.
- **The true fast-test baseline was 39 passing, 1 failing**, not 40 green.
  `tests/test_postprocess.py` called `.to_pandas()` and pandas was in no
  manifest — it had only ever passed because Tom's environment happened to have
  it. The whole point of running the baseline before Task 1 was to find this.
- **Reconciliation works, demonstrated accidentally.** Two endpoint tests failed
  because they seeded documents at a hardcoded instant ~18 hours stale, and the
  reconciler correctly declared those runs dead. The routers read the wall clock,
  so the fixtures were rebased on it.

**Decisions / dead ends.**

- **Rejected `count()` queries for the quota counters** in favour of a single
  locked document. Firestore's contention docs never promise range locks, so a
  phantom insert between a `count()` and its commit is not provably excluded, and
  aggregation queries read from index entries — the wrong primitive for a
  spend decision. Side benefit: the quota path needs no composite index.
- **Rejected an integer concurrency counter** for a map of leases. Release
  becomes `del leases[run_id]`, so it is idempotent; a *missed* release is
  visible rather than silently corrupting a counter; and eviction happens lazily
  inside `reserve()`, so reconciliation needs no cron, query or index.
- **Deleted `MAX_QUEUED_RUNS` from the design.** `run_job` starts an execution
  immediately — there is no broker — so a queue would mean Cloud Tasks, a drain
  endpoint and stranded-claim recovery. It would also *increase* cost risk by
  turning "reject and the user leaves" into "we owe them 26 minutes later". A
  429 with `Retry-After` is both cheaper and a more honest UX than a progress
  page that isn't progressing.
- **Rejected a wildcard "any wcEcoli key" loose schema.** `build_commands` reads
  only `resolved["simulation"]`, so accepting `wcecoli.foo` would produce a run
  that silently discards the override, burns the compute, and records a content
  hash covering parameters that had no effect. A `SUPPORTED_NAMESPACES` guard
  rejects it with a message naming what does take effect.
- **Excluded the git SHAs from the content hash.** The image digest strictly
  dominates them, so including a SHA that arrives via a build-arg env var could
  only introduce false *differences* if the var went stale; excluding it can
  never introduce a false *identity*.
- **Clamped `generations` and `init_sims` to 1** rather than widening the Parquet
  now. The schema advertised 8 while `extract_timeseries` has only ever handled
  one `simOut` directory, so those values burned a full ~26-minute run to
  manufacture a guaranteed failure. Widening the Parquet (adding a generation
  column) is Stage 1c, but note it becomes a breaking change once Stage 4 ships
  plotting against the four-column schema.
- **Dropped pandas rather than adding it.** `worker/postprocess.py` writes
  parquet with pyarrow alone, so the test now asserts through `to_pydict()` —
  removing a host-only dependency instead of adding one to the CI unit job.
- **Rejected a background reconciler** for lazy reconciliation on the read path.
  The polling frontend is the only observer, so a scheduler plus a reconciler
  service would add a deployable and an IAM binding to serve nobody extra.

**Open threads.**

- Tasks 3–7 need a machine with Docker and gcloud. Do Tasks 4 and 5 (IAM, Cloud
  Run service) before trusting any of the API's GCP calls — `roles/run.invoker`
  does *not* include `run.jobs.runWithOverrides`, which is the likeliest
  "tests green, deployed API broken" failure and is invisible locally because dev
  runs as the operator.
- The **tarball size** is the largest unknown in the cost model. Measure it on
  the next successful run; if ~1 GB, excluding the regenerable parca `kb/` output
  from `make_tarball` is a one-line change worth more than any ceiling.
- **Where the 26 minutes goes** (provision / image pull / parca / sim /
  postprocess) is unmeasured. Per-run cost is dominated by a fixed floor that
  `length_sec` barely moves, so shrinking the 2.52 GB image may beat every
  ceiling for reducing *expected* cost.
- **Debian bullseye reaches EOL 2026-08-31**, four weeks out, and
  `worker/Dockerfile` pins it with a live `apt-get` layer. It will present as an
  unrelated CI failure. The bullseye pin exists for numerical reproducibility, so
  bumping to bookworm means re-running both sims and comparing outputs.
- `/api/runs/validate` computes its content hash over params alone (no image
  lookup, to stay I/O-free), so it differs from the stored hash. Fine for
  spotting a duplicate submission; worth revisiting if the UI implies otherwise.
- A stray `google_cloud_storage-3.13.0-py3-none-any.whl` sits untracked at the
  repo root from an earlier session. Not deleted without say-so.

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
