# Stage 3: FastAPI Service, Quotas, and Deployment

**Date:** 2026-08-02
**Status:** Approved, ready to execute. Phase 3a first, checkpoint, then 3b.

> **For Claude:** implement task-by-task, TDD where a test is possible. Infra
> tasks follow the Stage 1 precedent (Create/Modify → verify with a real command
> → Commit) rather than forcing a failing test.


## Context

WCM_UI is a public, no-login web frontend for running the wcEcoli whole-cell
*E. coli* model on GCP. Stage 1 shipped a Dockerised, parameter-driven worker;
Stage 2 shipped the cloud loop (Cloud Run Jobs + Firestore + GCS + Artifact
Registry), verified by two live runs on 2026-05-23. Nothing has been touched
since 2026-05-24.

Stage 3 in the design doc's build order is *"FastAPI with submit/status/results
endpoints, Postgres, quotas."* Today the only way to launch a run is
`python -m scripts.submit` on a laptop holding GCP credentials. Stage 3 replaces
that with an HTTP service, so Stage 4's frontend has a contract to consume and a
live URL to point at. It also has to make the endpoint safe to expose: the design
doc is explicit that the no-login decision is *"acceptable only because hard
quotas, per-IP rate limits, wall-clock caps, and a budget kill-switch cap the
worst-case cost."*

Naming note: the doc still says Postgres, S3, AWS Batch, and Fargate throughout.
The project pivoted to GCP during Stage 2 and only git, `infra/README.md`, and
`docs/stage2-smoke.md` record it. Task 7 fixes that. Read the design doc's
infrastructure claims as intent, not as description.

## Decisions locked before planning

1. **Firestore stays** as the run store. The doc's "Postgres" is stale; Firestore
   is provisioned, IAM-wired, and tested. Quota counters go in Firestore
   documents, not SQL.
2. **The worker keeps writing run state directly to Firestore.** No
   `/heartbeat` POST, no `PATCH /api/runs/{id}`. The API is read-mostly plus
   submit. A public unauthenticated write path keyed on `run_id` — which doubles
   as the share link — would let anyone mark someone else's run failed.
3. **Deploy to Cloud Run in this stage.**
4. In scope: GCS signed-URL downloads, the YAML override layer, the
   stranded-`running` fix, and running the fast unit tests in CI.
5. **Clamp `generations` and `init_sims` maxima to 1**; widening the Parquet to
   support multi-generation runs is Stage 1c work.
6. **Two phases.** 3a builds the substrate and ends with a live URL; 3b builds
   the application. Checkpoint between them.

## Execution deviation, 2026-08-02: phases run out of order

Tasks 1 and 2 landed as written. Tasks 3–7 are **deferred**, and Phase 3b runs
first, because the machine this session ran on has **neither Docker nor the
gcloud SDK** installed — Stage 1 and 2 were built elsewhere. That makes every
Phase 3a verification step impossible here: no image build, no `up.sh` run, no
deploy, and no `gcloud emulators firestore` for Task 20.

Phase 3b is unaffected — it is pure Python against mocked GCP clients, so every
task can go genuinely green locally.

The cost is real and worth stating: 3a's ordering existed to surface the
production-only IAM failures early (`run.jobs.runWithOverrides`, signed-URL
signing). Those now land later. When picking 3a up on a machine with the
tooling, do Tasks 4 and 5 before trusting any of the API's GCP calls, and run
the impersonation check in the Verification section first — it is the cheapest
way to catch the `run.invoker` trap.

Task 3 also shrinks: it exists to ship a *stub* app so infra could deploy
against something. After Phase 3b there is a real `api/main.py`, so Task 3
reduces to the Dockerfile, its ignore file, `tests/test_api_image.py`, and the
Makefile targets.

## Corrections to the design doc

These are load-bearing and each changes what gets built.

**You cannot pin a Cloud Run Job execution to an image digest at submit time.**
`run_v2.RunJobRequest.Overrides.ContainerOverride` has exactly `name`, `args`,
`env`, `clear_args` — no `image` (verified against the `run_v2` proto source).
Whatever the Job spec holds is what runs. So "resolve tag→digest in the API"
would record a guess that races CI's next `docker push :latest`. **Invert it:**
CI pins the Job spec to a digest (`gcloud run jobs update --image=…@sha256:…`)
and the API reads it back via `JobsClient.get_job()` →
`job.template.template.containers[0].image`. That needs `run.jobs.get`, not
`artifactregistry.reader`, and it closes the race rather than documenting it.

**`roles/run.invoker` does not include `run.jobs.runWithOverrides`.** Overriding
job config requires `roles/run.developer`, which also grants `jobs.update` and
`jobs.delete` — letting a compromised public API repoint the Job at a different
image. Use a four-permission custom role instead. This is the single most likely
"tests green, deployed API broken" failure in the stage, because local dev runs
as the operator and no unit test can catch it.

**Concurrency is not a cost control, and the doc has no global daily cap.** At
the doc's "max concurrent 4" and the measured ~26-minute run, a day fits ~221
runs ≈ $35/day ≈ **$1,093/month**. Per-IP caps don't help — IPs are free (an
IPv6 /64 per subscriber, cloud VMs, Tor). Add `MAX_RUNS_PER_DAY`, which is the
only ceiling that actually bounds monthly spend.

**"Spot only, ~70% savings" does not transfer to Cloud Run Jobs**, which bill
per-second at on-demand rates. Re-derived: 4 vCPU + 16 GiB = $0.000104/s =
**$0.3744/job-hour**; the measured 26-minute baseline is **$0.162/run**. Free
tier (memory binds) covers roughly the first 18 runs/month.

**The bucket's 30-day lifecycle and GCS egress are unpriced in the doc.** Egress
is $0.12/GB and the tarball size has never been measured — if it is ~1 GB, *one
download costs 74% of what the run cost to produce*, and downloads are
repeatable and unmetered. Meter egress, shorten the tarball lifecycle to 7 days,
and measure the tarball.

**A wildcard "looser schema that allows any wcEcoli config key" is harmful.**
`worker/run.py:build_commands` reads *only* `resolved["simulation"]`. Accepting
`wcecoli.foo` produces a run that silently ignores the override, burns the
compute, and poisons the reproducibility hash with parameters that had no
effect. The loose schema type-checks a free-form `wcecoli` namespace, but a
`SUPPORTED_NAMESPACES = {"simulation"}` guard rejects namespaces the worker
doesn't implement, with a message saying so. One constant changes in Stage 1c.

**The content hash must exclude the git SHAs.** The image digest strictly
dominates them. Including a SHA sourced from a build-arg env var can only
introduce false *differences* if the var goes stale; excluding it can never
introduce a false *identity*. Store both SHAs as un-hashed provenance.

**"Same hash + same image = identical outputs" is false when `parca_cpus > 1`** —
multiprocess ParCa can reorder float reductions. Store a derived
`deterministic: bool = (parca_cpus == 1)` and badge it rather than claiming
bit-identity we can't deliver.

**Cross-origin `fetch()` of a GCS signed URL needs CORS on the bucket**, which
nobody will remember to configure. Split by access mode: **proxy the Parquet
through the API** (it is ~124 rows), and **307-redirect the tarball** to a signed
URL consumed by `<a href download>`, which is a top-level navigation and not
subject to CORS.

## Recommended ceilings

All env-var configurable; these are the defaults. Worst case if fully saturated
is **~$150/month**, against an expected **~$5/month**. That ratio is the actual
justification for no-login and belongs in the design doc as prose.

| Ceiling | Value | Why not the doc's number |
|---|---|---|
| `MAX_RUNS_PER_DAY` (global, UTC) | **8** | **New.** The only ceiling that bounds monthly spend. 8 × $0.281 = $2.25/day. |
| `MAX_CONCURRENT_RUNS` | **2** | Doc says 4. Not a cost control; chosen for latency and because a lease TTL has to bound it. |
| `MAX_RUNS_PER_IP_PER_DAY` | **3** | Keep. It is a *fairness* control, trivially evaded, but free. |
| `WALL_CLOCK_CAP_SEC` | **2700** (45 min) | Doc says 2 h, invented before any measurement. 45 min is 1.7× the one real data point and caps per-run cost at $0.281 vs $0.749. Enforced via `Overrides.timeout`. |
| Job `--task-timeout` | **3600** (1 h) | Down from 14400. Platform backstop if a code path forgets `Overrides.timeout`. |
| `MAX_QUEUED_RUNS` | **deleted** | See below. |
| `MAX_EGRESS_GIB_PER_DAY` | **5** global, **2** per-IP | New. The doc prices no egress at all. |
| API `--max-instances` | **3** | The only hard, free ceiling on request-driven spend. Not 1: a wedged instance would take the service down. |
| Tarball lifecycle | **7 days** | Down from 30, pending a tarball-size measurement. |

**No queue — reject with 429 + `Retry-After`.** `run_job` starts an execution
immediately; there is no broker. Building one means Cloud Tasks, a drain
endpoint, a claim protocol, and stranded-claim recovery. A queue also *increases*
cost risk: it turns "reject and the user leaves" into "we owe them 26 minutes of
compute later". At 2 concurrent and 26-minute runs the expected wait at capacity
is 13–26 minutes, so a 429 saying "2 runs in flight, retry in ~15 min" is a
better experience than a progress page that isn't progressing. `state='queued'`
keeps its current meaning — *doc created, `run_job` not yet returned* — and must
not be overloaded. Graduation path if wanted later: Cloud Tasks, whose
`max_concurrent_dispatches` is a platform-enforced ceiling.

**Quota counters are documents, not `count()` queries.** A single-document
transaction takes an exclusive lock on that document, which is exactly the
serialization point needed. Firestore's contention docs never promise range
locks, so phantom inserts between a `count()` and its commit are not provably
excluded. Consequence: **the quota path needs zero composite indexes.**

`quota/inflight` holds `leases: map<run_id, {reserved_at, expires_at}>` rather
than an integer. Release is `del leases[run_id]` — idempotent, and a *missed*
release is visible rather than silently corrupting a counter. Every `reserve()`
first evicts leases past `expires_at`, so reconciliation needs no cron, no query,
and no index. This is only sound because `Overrides.timeout` makes the wall-clock
cap platform-enforced: a lease older than cap + grace is *provably* dead.

## Phase 3a — substrate (ends with a live public URL)

### Task 1: Split fast from slow tests with pytest markers
`requirements-dev.txt` (new), `pyproject.toml`, `Makefile`, the three Docker test
modules.

The 8 fast unit-test files have never run in CI — no step names them, and they
would fail anyway on `pip install pytest` alone. Add
`markers = ["docker: …"]`, `addopts = ["-m", "not docker", "--strict-markers", "-ra"]`
(list form — pytest shlex-splits string addopts), and `pytestmark =
pytest.mark.docker` at the top of `test_image_builds.py`, `test_smoke_sim.py`,
`test_run_with_params.py`. Bare `pytest` becomes a seconds-long operation.

**Trap:** with that default, existing invocations like `pytest
tests/test_smoke_sim.py -v` deselect everything and exit 5. Every Docker
invocation must carry `-m docker` explicitly, in CI *and* the Makefile.

`requirements-dev.txt` uses `-r` on the three container requirements files so
pins can't drift, and adds `pytest`, `httpx` (TestClient needs it), and
`numpy==1.26.3` (matching wcEcoli's pin — do not "modernise" to 2.x, which
needs Python ≥3.12).

**Verify pins actually resolve** rather than trusting them:
`pip install --dry-run --report /tmp/report.json -r requirements-dev.txt` on
Python 3.11, then read the resolved closure. Check two specifically: `fastapi`
declares `starlette>=0.46.0` with **no upper bound** and starlette has since
shipped 1.x, so pip may install a major version fastapi was never tested
against; and `grpcio` skew between `google-cloud-firestore` and
`google-cloud-run` can silently trigger a source build that turns a 40-second
image build into 8 minutes. Record the resolved versions in the file header.

Makefile gains `deps`, `test` (fast), `test-all`.

### Task 2: Stop postprocess failures stranding runs, and clamp the schema
`worker/run.py`, `worker/db.py`, `worker/schema/params.schema.json`,
`tests/test_run_lifecycle.py`, `tests/test_schema.py`.

Three layers. **(1)** Clamp `generations` and `init_sims` maxima to 1, with
descriptions naming the deferred per-generation Parquet schema — those values
have never produced a usable run. **(2)** Wrap everything after a successful sim
in `_run_cloud` so a `make_tarball` / `extract_timeseries` / upload exception
uploads the sim's stderr and calls `mark_failed(..., failure_source="postprocess")`,
returning 70 (`EX_SOFTWARE`), distinguishable from the sim's own exit code.
**(3)** An `except BaseException` net in `main()` catching the residue — transient
Firestore errors, GCS 500s, `MemoryError` during tarball creation. `mark_failed`
gains an optional `failure_source` kwarg defaulting to `None`, so
`tests/test_db.py` is untouched.

Also recommend `--max-retries=0` on the Job. An automatic retry of a
non-idempotent 26-minute simulation is a cost bomb with near-zero user value;
the user can resubmit, and after Task 18 they will actually be told the run died.

### Task 3: Slim API image with a stub app
`api/{__init__,main}.py`, `api/requirements.txt`, `api/Dockerfile`,
`api/Dockerfile.dockerignore`, `tests/test_api_image.py`, `Makefile`.

A deliberate stub serving only `/healthz` and `/api/schema`, so the whole
infra/CI/deploy path can go green and hand Stage 4 a live URL before the
application exists. Tasks 8–20 fill it in without touching Tasks 4–7's files.

`python:3.11.15-slim-bookworm` — **not** the worker's `python:3.11.3-slim`
(bullseye, EOL 2026-08-31). Not `FROM` the worker image: that is ~2.5 GB and
Cloud Run pulls on every cold start. `COPY worker/` whole (74 kB) rather than
cherry-picking modules, which breaks the package layout; `postprocess.py` and
`run.py` come along but their top-level `numpy`/`pyarrow` imports never execute
because nothing imports them. Non-root UID 10001. No `HEALTHCHECK` (Cloud Run
ignores it). `CMD ["sh","-c","exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers"]`
— shell form because Cloud Run injects `$PORT` and exec-form won't expand it.

`tests/test_api_image.py` (marked `docker`) asserts: `api.main` imports;
`worker.{merge,validate,db}` import; the schema files are present; **`import
numpy` fails** (the regression guard that keeps the image slim); `wholecell`
absent; UID is 10001; size < 500 MB; and `PORT=9123` is honoured with `/healthz`
answering.

**All route handlers are sync `def`, never `async def`.** Every GCP client is
blocking gRPC; `async def` + blocking client stalls the event loop and collapses
effective concurrency to 1.

**`.dockerignore` does not exclude `vendor/`**, so a local `docker build -f
api/Dockerfile .` ships the whole wcEcoli checkout. Mitigate twice: CI checks
out this job *without* submodules, and add `api/Dockerfile.dockerignore`. Verify
the latter works with `--progress=plain 2>&1 | grep "transferring context"` and
delete it if the byte count doesn't move.

### Task 4: API service account, custom job-runner role, API image repo
`infra/scripts/up.sh`.

A **second AR repo `wcm-ui-api`**, not a second image name in `wcm-ui-worker`:
cleanup policies and IAM are both per-repo, and the worker manifest is ~2.5 GB
wanting `keepCount=3` while the API's is ~200 MB and can keep far more. AR bills
by bytes, so the split is free. Add a cleanup policy to the *worker* repo, which
is the one that grows — apply with `--dry-run` first, since the docs don't show
the JSON body shape and both a bare array and a `{"rules": […]}` wrapper exist
in the wild.

Custom role `wcmUiJobRunner` with exactly four permissions: `run.jobs.get` (read
the pinned digest back), `run.jobs.runWithOverrides`, `run.executions.get`
(detect a task that died before writing Firestore), `run.operations.get`. Note
`gcloud iam roles create` is not idempotent and a soft-deleted role blocks
re-creation for 7 days — hence the `describe`/`update`/`create` branches, and
hence `down.sh` must leave it in place.

Four grants on the API SA, each justified in a comment:
- `wcmUiJobRunner` **bound at the job resource**, not the project.
- `roles/datastore.user` on the project (the API writes `runs/{id}` and quota counters).
- `roles/storage.objectViewer` on the bucket — **not optional**: a V4 signed URL
  authorises *as the signer*, and GCS re-checks that identity's IAM at request
  time, so without it the URL signs fine and then 403s in the browser.
- `roles/iam.serviceAccountTokenCreator` **on itself**. ADC on Cloud Run is
  `compute_engine.Credentials` — a bearer token with no private key, so it cannot
  sign. `generate_signed_url(version="v4", service_account_email=…, access_token=…)`
  delegates to `iamcredentials…:signBlob`.

No `artifactregistry.reader` on the API SA — with CI pinning the digest, the API
never talks to Artifact Registry. No `logging.logWriter` — Cloud Run captures
stdout via the platform agent; log JSON with a `severity` key.

**Signing gotcha to encode in the code:** `compute_engine.Credentials`
initialises `_service_account_email` to the literal string `"default"` and only
resolves the real address in `refresh()`. **Call `creds.refresh()` before reading
`service_account_email`** or you sign as `default@…` and get a 404 from signBlob.
Use `getattr(creds, "service_account_email", None)` so local impersonated
credentials fall through to the `credentials.signer` path unchanged — one
function, two environments, no branching.

**Local dev:** `gcloud auth application-default login
--impersonate-service-account=wcm-ui-api-dev@…` (plus granting yourself
`serviceAccountTokenCreator` on it). Impersonated credentials *do* implement
`sign_bytes`, so signing works locally and exercises the real IAM surface.
Document this in `make api`'s comment.

### Task 5: Cloud Run service, main-only deploy identity, quota infrastructure
`infra/scripts/up.sh`, `infra/scripts/down.sh`, `infra/README.md`.

**Cloud Run service** — `--cpu=1 --memory=512Mi --min-instances=0
--max-instances=3 --concurrency=40 --timeout=60 --cpu-boost --ingress=all
--allow-unauthenticated`, plus a `/healthz` startup probe. `min-instances=0`
deliberately contradicts the doc's "Fargate min 1 task": Cloud Run's
scale-to-zero is free, and the polling workload is precisely the one that keeps
the instance warm, so it doesn't pay cold starts. `concurrency=40` matches
Starlette's default AnyIO threadpool size — going higher just queues requests
inside the instance where Cloud Run can't see the pressure.

**Chicken-and-egg is worse than the Job's.** The `alpine:3.20` trick does not
transfer: a *service* must accept TCP on `$PORT` or `gcloud run deploy` fails.
Bootstrap from `us-docker.pkg.dev/cloudrun/container/hello` instead, making the
startup probe conditional on `API_IMAGE != bootstrap`.

**A separate deploy SA `wcm-ui-ci-deploy-dev`, bound to
`attribute.ref/refs/heads/main`.** The least-privilege argument is about the
*principal*, not the roles: the existing push SA is bound to
`attribute.repository/tomkimpson/WCM_UI`, matching **every ref including PR
branches**. The only thing stopping a PR from pushing `:latest` — which the Job
runs — is an `if:` in `ci.yml`, a file the PR may edit. Splitting the SAs moves
the guard into IAM where a PR can't reach it. **Tighten the push SA's binding
the same way while in there.** `run.developer` does *not* include
`iam.serviceAccounts.actAs`, so add two narrow `serviceAccountUser` grants on
the API and worker SAs rather than a project-wide one.

**Fix two existing idempotency defects.** The Job block unconditionally
re-applies `--image="${IMAGE_URI}"` (i.e. `:latest`), so re-running `up.sh`
**un-pins the digest CI just set** — read the current image first and fall back
to it. And the WIF provider block only ever *creates*, silently skipping
attribute-mapping changes — make it `providers update-oidc` when it exists.
Verify the `--format='value(...)'` paths against real output before relying on
them: `gcloud run` has both v1 (Knative-shaped) and v2 surfaces, and a wrong
path returns empty, takes the fallback, and un-pins silently. Add a
`[ -n … ] || log WARNING` guard.

**Quota infrastructure:** two composite indexes (for the reconciler and Stage 4,
*not* for the quota path) behind an `ensure_index` guard, since `indexes
composite create` fails `ALREADY_EXISTS` and `up.sh` runs `set -euo pipefail`; a
Firestore TTL policy on `quota_days.expire_at` (honest retention claim is "≤ 8
days", since deletion lags expiry by up to 24 h); and a **conditional** seed of
`config/global` with `accepting_runs=true` that never clobbers an existing doc —
so re-running `up.sh` cannot re-open a kill switch a human deliberately closed.

**Also configure a GCP billing budget now** with alerts at 50/80/100%. Ten
minutes of `gcloud billing budgets create`, and it is the only backstop that
catches what the in-API ceilings cannot see: an egress loop, a runaway instance,
or a bug in the quota code. Stage 6 builds the automated *response*; Stage 3
gets the email. Shipping a public endpoint without it is the wrong order.

`down.sh`: delete the service *before* the API SA (an SA deleted under a live
revision leaves a confusing broken state). Leave the custom role and the AR repo
in place. Note loudly in the header that `down.sh` now takes the public API
offline, which the Stage 2 version never did.

### Task 6: Restructure CI into gated jobs with a digest-pinning deploy
`.github/workflows/ci.yml`.

Four jobs, underscore-named (`needs.worker-image.…` parses as *subtraction* in
GitHub's expression engine):

| Job | needs | Timeout | ~Time |
|---|---|---|---|
| `unit` | — | 10 | 2 min |
| `api_image` | `unit` | 20 | 5 min |
| `worker_image` | `unit` | 60 | 40 min |
| `deploy` (main only) | both images | 15 | 3 min |

`unit` checks out without submodules, installs `requirements-dev.txt`, runs
`pytest -v`, and carries a **floor check** (`>= 23` collected) so a stray
`pytestmark` can't silently shrink the fast suite back to nothing and go green
on zero tests.

Both image jobs emit their pushed digest as a job output. `deploy` then runs
`gcloud run jobs update --image=…@<digest>` — **this is what makes the
reproducibility claim true** — plus `gcloud run deploy` with *only* `--image`
(`up.sh` owns the config flags), then curls `/healthz` and `/api/schema`.

Critical path ≈ 45 min, up from 38m40s. The extra ~6 minutes buys: a PR with a
broken unit test fails in 2 minutes instead of 39, and 35 minutes of runner time
isn't burned on it.

Verify the digest extraction before relying on it — `gcloud artifacts docker
images describe --format='value(image_summary.digest)'` may need
`artifactregistry.reader`; the no-extra-IAM alternative is `docker buildx
imagetools inspect --format '{{.Manifest.Digest}}'`. Either way keep the
`test -n "${DIGEST}"` guard.

### Task 7: Prove the deployed API and record the smoke procedure
`docs/stage3-smoke.md` (new), `docs/stage2-smoke.md`, `handoff.md`, `log.md`.

Modelled on `docs/stage2-smoke.md` — manual, not in CI, with a "Verified runs"
table. Cover: liveness and serving revision; that the image is an `@sha256:`
digest and **not** the `cloudrun/container/hello` bootstrap; genuinely public
(200 unauthenticated); CORS preflight from the frontend origin **and** that a
disallowed origin gets no `access-control-allow-origin` (a wildcard config
passes the first check and is wrong); the schema endpoint's shape; and validation
rejection launching no execution.

Also refresh `log.md` and `handoff.md`, which both still describe Stage 2 as
future *AWS* work — only git, `infra/README.md`, and `docs/stage2-smoke.md`
record the GCP pivot. Add an "AWS → GCP" note to the design doc so the next
session doesn't re-read a doc describing a cloud the project isn't on.

**→ Checkpoint. A live public URL serving `/healthz` and `/api/schema`, with
every production-only IAM failure mode already surfaced.**

## Phase 3b — the application

### Task 8: Collect all schema errors and cache the validator
`worker/validate.py`, `tests/test_validate_field_errors.py`.

`validate_params` keeps its exact single-error behaviour and message format, so
`tests/test_validate.py` stays byte-identical. Add `field_errors(params, *,
schema_path) -> list[FieldErrorData]` for form-backing callers, plus
`@lru_cache _validator(schema_path)` — today the schema JSON is re-read,
re-parsed, and re-compiled on *every* call.

The interesting part is `additionalProperties`: jsonschema reports one error
anchored at the *parent*, with the offending key buried in prose. Don't parse the
string — expand it structurally from `err.schema["properties"]` and
`err.instance`, yielding one error per typo'd key at a real JSON pointer. The
docstring's "the frontend (Stage 4) will aggregate errors itself" gets rewritten:
aggregating in TypeScript would mean reimplementing the sort key, the
`additionalProperties` expansion, and pointer construction on the far side of the
wire. Keep the `tuple(str(p) for p in e.absolute_path)` coercion — `absolute_path`
is a deque mixing str keys and int indices.

### Task 9: Firestore reads and transactional state guards
`worker/db.py`, `tests/fakes.py` (new), `tests/test_db.py`.

`worker/db.py` becomes the single owner of the `runs` collection — the API
imports it rather than duplicating the collection name and doc shape (today
`scripts/submit.py` hardcodes the literal `"runs"`). The existing three functions
are untouched. Add `get_run`, `create_queued_run` (the only `.set()` in the
codebase), `try_mark_running(run_id, *, attempt)` returning `False` without
writing if already terminal (the missing `--max-retries` idempotency guard),
`mark_infra_failed` refusing to overwrite a terminal state, `touch_reconciled`,
and `query_runs`. Add `@lru_cache` behind `_client()` without breaking
`monkeypatch.setattr(db, "_client", …)`.

`tests/fakes.py` is the investment that makes Tasks 10–20 cheap: a dict-backed
`FakeFirestore`, a transaction context that executes immediately,
`SERVER_TIMESTAMP` resolved to an injectable `now`, and Jobs/Executions/Storage
stubs.

### Task 10: Loose schema and the YAML override resolver
`worker/schema/params.loose.schema.json` (new), `api/params.py`,
`tests/test_api_params.py`, `tests/test_schema.py`.

`resolve_params(params, yaml_override) -> ResolveResult` — **never raises on user
input.** Order: `yaml.safe_load` (mapping `YAMLError` to one error carrying
`problem_mark.line/column`) → two-stage `merge_params(merge_params(defaults,
params), override)`, which is exactly `defaults ← form ← yaml` → depth and size
checks → the `SUPPORTED_NAMESPACES` guard → `field_errors` against the loose
schema. Both `POST /api/runs` and `POST /api/runs/validate` call it identically.

The loose schema duplicates the strict `simulation` subschema **verbatim** rather
than `$ref`-ing across files (jsonschema 4.23 needs a `Registry` and base URI for
that, which buys nothing); a test asserts deep equality, the same guard pattern
already used for `defaults.json`. Recursion depth is inexpressible in JSON
Schema, so enforce it in Python.

### Task 11: Provenance — digest read-back and a deterministic content hash
`api/provenance.py`, `api/cloudrun.py`, `tests/test_api_provenance.py`.

`api/cloudrun.py` takes `build_run_request` verbatim from `scripts/submit.py`
(preserving the `run_job(request=…)` call shape that `tests/test_submit.py`
asserts on) plus `get_job`, `run_job`, `get_execution`.

`content_hash` covers `{hash_version, image_digest, params}` —
`json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=True,
allow_nan=False)`. Every argument is load-bearing: `sort_keys` sorts
*recursively*, which is the determinism guarantee; `separators` makes it
formatter-proof; `ensure_ascii` removes Unicode-normalisation dependence;
`allow_nan=False` turns `NaN` into a loud `ValueError` instead of non-standard
JSON another implementation would hash differently.

`resolve_image_pin` reads `get_job()` → `containers[0].image`; if it holds
`@sha256:` that *is* the authoritative digest, because it is literally what Cloud
Run will pull. Registry manifest `HEAD` is the fallback. **Provenance must never
block a run** — on failure record `source="unresolved"` and proceed. Add an
explicit 3-second timeout and a 60-second TTL cache (not `lru_cache`, so deploys
propagate).

Tests lock determinism: identical params with keys inserted in different orders —
including nested — hash identically; and a **hardcoded expected digest** for a
canonical fixture, so anyone changing the recipe is named by a failing test.

### Task 12: The Stage 4 response contract
`api/models.py`, `api/settings.py`, `tests/test_api_models.py`.

Worth its own commit because it is what Stage 4 codegens against; freezing it
early lets Stage 4 start before the endpoints land. `SubmitRequest` is
`{params, yaml_override}` with `extra="forbid"` and a 64 KiB cap (the doc's
`optional_yaml_override` loses the `optional_`, which is type information).
`FieldError` carries `path`, RFC-6901 `pointer`, `kind`, `message`, `keyword`,
`constraint`, and `line`/`column` for YAML. `RunDetail` carries state,
timestamps, resolved *and* submitted params plus the raw YAML (for a faithful
"fork"), `failure_source`, a `RunProvenance` block, a `RunArtifacts` block whose
per-artifact `reason ∈ {not_ready, expired, never_produced}` lets the frontend
render buttons without probing, and **`poll_after_ms`** so the cadence is
server-controlled and can be widened without a frontend deploy.

Two invariants each get a test: `RunDetail` has no field matching `*ip*`, and
`field_order` covers exactly the strict schema's knobs so a new knob can't be
invisible in the form. Name the field `strict_schema`, not `schema` — the latter
shadows `BaseModel.schema` and warns in pydantic v2.

New Firestore fields: `submitted_params_json`, `yaml_override_text`,
`image_digest`, `image_pin_source`, `wcecoli_git_sha`, `wcm_ui_git_sha`,
`content_hash`, `hash_version`, `deterministic`, `gcs_params_uri` (closing the
"uploaded but never recorded" gap), `schema_version`, `submitter`,
`client_ip_hash`, `attempt`, `failure_source`, `last_reconciled_at`.
`params_json` keeps its current meaning — `tests/test_submit.py` asserts on it.

### Task 13: Client-IP hashing and compute-budget ceilings
`api/quota.py`, `tests/test_quota_ip.py`, `tests/test_quota_budget.py`.

**Parse `X-Forwarded-For` right to left**, index `-(1 + TRUSTED_PROXY_HOPS)`.
Cloud Run *appends* the peer IP it observed to whatever the client sent, so
`xff[0]` is fully attacker-controlled and `request.client.host` is not the client
IP either. An unauthenticated per-IP quota keyed on `xff[0]` is decoration, not a
quota. Both mistakes get a pinning test.

**Truncate IPv6 to /64 before hashing.** A subscriber gets at least a /64, so
per-address counting is evaded by incrementing the last hextet. Hash with
**HMAC-SHA256** keyed on a Secret Manager salt, message `f"{day_key}|{prefix}"`
— HMAC not bare SHA-256 because IPv4 is 2^32 and an unsalted hash is brute-forced
in seconds, which is not pseudonymisation. Including `day_key` gives cross-day
unlinkability for free. Refuse to boot without the salt when `GCP_PROJECT` is set.

`check_compute_budget` runs *before* any counter is touched (zero I/O), rejecting
what JSON Schema cannot express: `generations`/`init_sims` > 1, `parca_cpus` >
`JOB_VCPUS` (the schema allows 16 against a 4-vCPU job — oversubscription makes
parca *slower*), and `length_sec > 7200` as an obvious-waste filter. Be honest
about that last one: the wall-clock cap is the real enforcement; this only makes
certain-to-fail requests fail in 5 ms instead of 45 minutes.

`build_overrides` sets `Overrides.timeout` to the clamped cap. **Pin the proto
shape with a test** — this is the one contract verified only from documentation,
and proto-plus may want a `Duration` rather than a `timedelta`.

### Task 14: Transactional lease-based reserve, release, and finish
`api/quota.py`, `api/db.py`, `tests/test_quota_lease.py`.

One transaction over two documents: evict stale leases → check the three
ceilings → write the lease and increment `runs_started` and `by_ip[hash]`. Then,
and only then, create the run doc and launch.

Reserve/commit/release semantics, with the failure directions chosen
deliberately:
- `run_job` succeeds → lease stays, daily count stands.
- `run_job` raises a *provably-not-started* error (`InvalidArgument`,
  `PermissionDenied`, `NotFound`, `ResourceExhausted`) → drop the lease **and
  refund** both counters; no compute was spent.
- `run_job` raises `DeadlineExceeded` → **do not refund.** The server may have
  started the job. When in doubt, charge: over-charging annoys one user,
  under-charging is an unbounded bill.
- Worker reaches a terminal state → drop the lease only; the compute was spent.
- Nothing happens for 60 min → the next `reserve()` evicts it.

Note the coupling worth writing down: `by_ip` is a map on the daily doc (keeping
admission to two documents in one transaction), and its size is bounded *only*
because the global daily cap exists. Remove that cap and the map grows toward the
1 MiB limit.

`accepting_runs` reads `config/global` behind a 30-second per-instance TTL cache.
Price the staleness rather than asserting it: 30 s of overshoot bounded by
`MAX_CONCURRENT=2` is **$0.56**. Failure semantics are deliberately asymmetric —
a *missing* doc means accepting (it's a provisioning gap, and failing closed
would make a fresh deploy inexplicably broken); a *read error with no cached
value* means 503 (if Firestore is unreachable the reserve would fail anyway).

Add `test_collection_names_agree`: `api.db._COLLECTION == worker.db._COLLECTION`.
One line, catches the realistic drift now two modules know the shape.

### Task 15: One submit code path for the CLI and the API
`api/runs.py`, `scripts/submit.py`, `tests/test_api_submit_service.py`,
`tests/test_submit.py`.

The keystone. `main()` calls its existing monkeypatch seams and passes the
results *into* the shared `submit_run(...)`, so patching the seams still controls
the shared path and the CLI keeps working. `_load_and_resolve`'s path-based
signature — the specific obstacle to reuse — splits: file reading stays in the
CLI, merge+validate moves to `api/params.py`.

Also fixed in passing: `submit._firestore_client()` uses bare
`firestore.Client()` while `db._client()` honours `GCP_PROJECT` and
`FIRESTORE_DATABASE`. Today, submitter and worker can write to *different*
databases. Both go through `api/deps.py`.

`tests/test_submit.py` needs exactly two added lines in its `mocked_clients`
fixture — a `get_job` return value and a `_resolve_image_pin` patch. **This is
the only existing test that must change, and only additively.**

### Task 16: App factory, CORS, error envelope, and meta endpoints
`api/main.py`, `api/deps.py`, `api/errors.py`, `api/routers/meta.py`,
`tests/conftest.py`, `tests/test_api_meta.py`.

`create_app(settings=None)` as a factory so tests inject settings.
`allow_credentials=False` — there are no cookies, so credentialed CORS is pure
risk. `GET /api/schema` with an `ETag` (serving it from the API rather than
bundling a copy in the SPA is what guarantees form and validator can't drift);
`GET /api/config` with `no-store`, separate purely for cache semantics, since the
kill switch must never be cached and the schema is cacheable for minutes. The
catch-all handler logs the traceback server-side and returns a bare 500 — a test
asserts no traceback reaches the body.

### Task 17: POST /api/runs and POST /api/runs/validate
`api/routers/runs.py`, `tests/test_api_runs_post.py`.

`202`, not `201` — nothing exists yet that the caller can use; `Location` still
points at the poll URL. **Resolve and validate params *before* touching quota**,
so a malformed request can't consume a reservation. On any exception after
reservation, release and re-raise. `POST /api/runs/validate` returns **200 even
when invalid** (`valid: false` plus errors) and provably touches neither
Firestore nor Cloud Run — it feeds the YAML editor live and returns the
`content_hash` so the UI can say "you already ran this".

**SSE is cut.** A long run means a long open connection that Cloud Run bills for
its full duration while holding a concurrency slot, and each connection needs its
own Firestore listener. `poll_after_ms` does the job for one read per interval.

### Task 18: GET /api/runs/{id} with lazy reconciliation
`api/reconcile.py`, `api/runs.py`, `tests/test_api_reconcile.py`,
`tests/test_api_runs_get.py`.

Runs can be stranded in `running` forever by OOM, SIGKILL, or Cloud Run infra
failure — `worker/run.py` has no `finally` that survives those. **Lazy on read,
not a background reconciler:** the only observer that cares is the polling
frontend, so the read path is where the information is needed, and a scheduler
plus reconciler service would add a deployable and an IAM binding to serve zero
extra users.

Structure it as a **pure verdict function** so all the policy is unit-testable
against `SimpleNamespace` stubs, with the single impure wrapper doing
`get_execution` → verdict → `mark_infra_failed` → `touch_reconciled`. Consult the
Cloud Run Executions API before writing `failed` so a merely-slow run isn't
killed on paper. Every reconcile sets `failure_source="infrastructure"`, which is
what lets the frontend say "the infrastructure failed" rather than "your
parameters were wrong". A 60-second post-completion grace covers a worker write
still in flight.

Note the separation: the lease TTL and the read-path `finish()` protect the
*ceiling*; this task protects the *UI*. Conflating them is what produces a
service wedged closed.

`404` means exactly one thing — unknown `run_id`. Compute artifact availability
and advisory tarball expiry (`finished_at + retention`) with **no GCS call**, so
polling stays cheap; a test asserts the fake storage client was never touched.

### Task 19: Parquet streaming, signed-URL downloads, and egress metering
`api/artifacts.py`, `api/routers/artifacts.py`, `tests/test_api_artifacts.py`.

Parquet is proxied as bytes (chunked, so a large file never lands in memory);
tarball/params/stderr are `307` to a 15-minute V4 signed URL — short enough that
the URL can't be passed around as a free CDN. `blob.exists()` before signing, so
the lifecycle-expired tarball returns **`410 artifact_expired`, not 404**. `409
run_not_complete` for a run still running; `409` with `reason="never_produced"`
for stderr on a successful run.

Charge the object's byte count against `egress_bytes` and `egress_by_ip` at mint
time, optimistically and without refund — the URL might be used, and
over-charging is the safe direction. This is the largest per-byte risk in the
cost model and the doc prices none of it.

Unit tests monkeypatch `Blob.generate_signed_url` and assert the kwargs. That
verifies our code, not Google's crypto, and needs zero credentials — so it runs
in the fast job. The real thing is covered by the smoke doc.

### Task 20: Emulator concurrency proof and the quota smoke procedure
`tests/test_quota_concurrency.py`, `docs/stage3-quota-smoke.md`, `Makefile`,
`.github/workflows/ci.yml`.

**Mocking the Firestore client mocks away the thing under test.** 20 threads race
on one transaction; exactly 2 may win. The emulator is free and starts in
seconds, so unlike the sim tests **this belongs in CI** — it is the regression
guard for the one bug class unit tests structurally cannot catch. Record honestly
in the docstring that the emulator's contention model is not bit-exact: it
reliably catches "two winners" and read-after-write ordering, and proves nothing
about production lock timeouts.

`docs/stage3-quota-smoke.md` covers what the emulator can't, ~$0.80 of real runs:
a 429 at `MAX_CONCURRENT=1`; that `Overrides.timeout=300` **kills the execution
at ~5 min rather than the job spec's 60** (the direct proof the lease TTL is
sound); deleting an execution mid-flight and confirming the lease is evicted
after cap + grace so **the service cannot wedge closed**; the two 429 codes
differing so the frontend can word them differently; the kill switch producing
503 within the cache TTL; signed-URL expiry and the matching `egress_bytes`
increment; and a `403` on direct `storage.googleapis.com` access proving
public-access-prevention still holds.

Recording that $0.80 figure is what makes the procedure something a person
actually runs.

## Verification

**Fast, every commit:** `make test` → the full unit suite in seconds, no Docker.

**Phase 3a done when:** `bash infra/scripts/up.sh` run twice is clean and does
not un-pin the digest; `gcloud run services describe` shows the flags from Task
5 and an `@sha256:` image that is not the bootstrap; an unauthenticated `curl
$API/healthz` returns 200; CORS echoes the allowed origin and omits the header
for a disallowed one; CI's job graph shows `unit → {api_image, worker_image} →
deploy` with a red unit test skipping both image jobs in under 3 minutes.

**Prove the IAM before trusting it** — this is what catches the
`run.invoker`-vs-`runWithOverrides` trap before production:
```
gcloud run jobs describe wcm-ui-worker-dev --region=us-central1 \
  --impersonate-service-account=wcm-ui-api-dev@wcm-ui-dev.iam.gserviceaccount.com \
  --format='value(template.template.containers[0].image)'
```

**Phase 3b done when:** `POST /api/runs` with `{params, yaml_override}` returns
202 and a `run_id`; `GET /api/runs/{id}` walks `queued → running → succeeded`;
the Parquet streams and parses (124 rows for a 30-second sim); `download/tarball`
307s to a signed URL that a plain `curl` can follow; invalid params return **all**
errors in one 422, each with a JSON pointer; a manually-cancelled execution
reaches `failed` with `failure_source="infrastructure"`; a postprocess exception
marks the run failed with stderr in GCS; a retried task does not re-run the sim;
`make submit` and `tests/test_submit.py` still pass; two submissions of the same
params against the same image produce the same `content_hash`; and no raw client
IP appears anywhere in the `runs` collection.

## Measure these two before trusting the cost model

**The tarball size** — `gcloud storage ls -l gs://wcm-ui-runs-dev/<run_id>/output.tar.gz`.
It drives both the storage and egress lines and has never been measured. If it is
~1 GB, excluding the regenerable parca `kb/` output from `make_tarball` is a
one-line change that is probably the highest-leverage cost reduction available —
and it isn't a quota at all.

**Where the 26 minutes goes** — provision, image pull, parca, sim, postprocess.
Per-run cost is dominated by a fixed floor that `length_sec` barely moves. If the
2.52 GB image pull is several minutes, shrinking the image beats every ceiling in
this plan for reducing *expected* cost. Ceilings bound the adversarial case; the
floor is what you actually pay.

## Risks

- **Signed URLs fail in production for four independent reasons** — missing
  self-`tokenCreator`; missing `objectViewer` (signing succeeds, following 403s);
  `refresh()` not called so you sign as `default@…`; `iamcredentials` unreachable.
  Each yields a different error and only the first is guessable from the code.
  *Detect:* the smoke asserts on `X-Goog-Credential`'s contents specifically, to
  distinguish the third. *Fallback:* proxy the tarball through the API too —
  genuinely a fallback, not a design, since it burns egress on a large file.
- **Debian bullseye goes EOL 2026-08-31**, four weeks out, and
  `worker/Dockerfile` pins it with a live `apt-get` layer. It will look like an
  unrelated CI failure on an unrelated PR. *Detect:* `worker_image` failing at
  `apt-get update` with 404s. *Fallback:* short-term, point apt at
  `archive.debian.org`; properly, bump to bookworm and re-run both sims to
  compare outputs — the bullseye pin exists for numerical reproducibility, so
  this is not a cosmetic bump. Worth scheduling deliberately, not discovering.
- **Per-IP quotas remain bypassable** and the fix isn't in this layer. `--max-instances=3`
  and the global daily cap are the IP-independent controls that actually hold.
  The structural fix is Cloudflare in front using `CF-Connecting-IP`, or
  Turnstile — both blocked on the custom domain that arrives with Stage 4.
  Until then be clear in the docs: **the per-IP cap is fairness; the global daily
  cap is safety.**
- **The kill switch doesn't kill enough.** Flipping `accepting_runs=false` stops
  the ~$67/month compute line but not the ~$65 API line or the ~$18 egress line —
  under half the worst case. Stage 6's responder must also set `--ingress=internal`
  and stop minting signed URLs. Flipping a boolean the runaway process doesn't
  consult is not a kill switch.
- **`run_id` is both capability and share link**, and there is no access control
  on run outputs at all. uuid4 makes it unguessable, but anything a user types
  into the parameter box is effectively public to anyone with the URL. That
  belongs next to the submit button, not buried in the design doc's cut list.
- **The reconciliation two-writer race.** `mark_infra_failed` is transactional
  and refuses terminal states, but `mark_succeeded` uses a plain `.update()`, so
  a pathological interleaving could leave `state="failed"` with populated
  `gcs_*_uri`. *Detect:* a one-line Firestore query for exactly that, in the
  smoke doc. *Fallback:* make `mark_succeeded` transactional and treat succeeded
  as strictly dominant.
- **Cold starts on a scale-to-zero endpoint may exceed the ~3 s estimate** —
  `import google.cloud.firestore` pulls grpcio. If it is 8 s, Stage 4's first
  paint looks broken. *Detect:* `curl -w '%{time_total}'` on `/healthz` after 20
  minutes idle. *Fallback:* `--min-instances=1`, at the cost of an always-billed
  idle instance.
- **The 40-ish existing unit tests have never run in CI**, so their green status
  rests on local runs alone. *Detect:* run `pip install -r requirements-dev.txt
  && pytest` **before Task 1** and record the true baseline. Every pass count in
  Phase 3b shifts by the same delta if it isn't what's expected — adjust once and
  carry it.

## Deferred to Stage 6

The budget-alarm → Pub/Sub → Cloud Function that *flips* the kill switch (Stage 3
owns the check and the email alert); a kill switch that actually kills; Cloudflare
or an ALB in front; scheduled reconciliation (lazy sweeping already protects the
ceiling, so this is UI polish); a seconds-denominated daily budget, which prices
what we're actually billed for and which the lease machinery is already the
skeleton for; a real queue via Cloud Tasks if the 429 proves unpopular; and
Turnstile, the only measure that meaningfully raises the cost of IP rotation.
