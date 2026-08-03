# Handoff — 2026-08-02 17:30

## Goal
Build Stage 3: a FastAPI service with submit/status/results endpoints, hard cost
ceilings, and a Cloud Run deployment.

## Status
**Phase 3b (the application) is complete and green. Phase 3a (infrastructure and
deploy) is not started.** 296 fast tests pass in ~1.3s, plus 4 emulator tests
that skip. The API boots under uvicorn and serves `/healthz`, `/api/schema` and
`/api/runs/validate` correctly over real HTTP.

Nothing has been deployed, and **nothing that touches GCP has been executed
even once** — this machine has neither Docker nor the gcloud SDK, so the phases
were run in reverse. See `docs/plans/2026-08-02-stage3-api-and-quotas.md`, whose
"Execution deviation" section records why.

## What changed this session
See `log.md`.

## Blockers / problems
- **No Docker runtime and no gcloud SDK on this machine.** Blocks Tasks 3–7
  entirely: the API image, `up.sh`/`down.sh`, the CI deploy job, `make submit`,
  and `make quota-emulator`. Stage 1 and 2 were built on a different machine.
  Note `gcloud auth login` and `gcloud auth application-default login` are
  interactive, so Tom must run those himself even once the SDK is installed.

## Next steps
1. On a machine with Docker and gcloud, pick up **Tasks 3–7** from
   `docs/plans/2026-08-02-stage3-api-and-quotas.md`. Task 3 has shrunk: it was
   going to ship a stub app so infra had something to deploy against, but
   `api/main.py` is now real, so Task 3 is just `api/Dockerfile`, its ignore
   file, `tests/test_api_image.py`, and the Makefile build targets.
2. **Do Tasks 4 and 5 before trusting any of the API's GCP calls.** Then run the
   impersonation check in the plan's Verification section *first* — it is the
   cheapest way to catch the `run.invoker` trap below.
3. Run `make quota-emulator` once gcloud exists. It is the only test that proves
   the quota transaction actually serialises.
4. Work through `docs/stage3-quota-smoke.md` and fill in its Verified runs table.
   Step 1 (that `Overrides.timeout` really kills the task at the cap) is the most
   important, because the quota lease TTL's safety depends on it.

## Open questions
- **Ceiling values.** Defaults are 8 runs/day globally, 2 concurrent, 3 per IP
  per day, 45-minute wall clock — all env-configurable, all derived from a cost
  model in the plan doc (expected ~$5/month, adversarial worst case ~$150). They
  are deliberately tighter than the design doc's illustrative numbers. Worth a
  look before going public.
- **Multi-generation runs.** `generations` and `init_sims` are clamped to 1
  because `extract_timeseries` handles only one `simOut` directory. Widening the
  Parquet with a generation column is Stage 1c — but it becomes a *breaking*
  change once Stage 4 ships plotting against the current four-column schema, so
  decide before then.
- **`up.sh` is not yet updated for the quota documents.** It needs the two
  composite indexes, the `quota_days.expire_at` TTL policy, a conditional
  `config/global` seed, and a billing budget. All specified in the plan, none
  written — Task 5.

## Non-obvious context
- **Run the tests in the conda env `wcm-ui` (Python 3.11)**, not the miniconda
  base (3.13). `export PATH=/Users/tomkimpson/miniconda3/envs/wcm-ui/bin:$PATH`
  then `make test`. numpy is pinned to 1.26.3 to match wcEcoli and has no cp313
  wheels.
- **`ContainerOverride` has no `image` field** (verified against
  google-cloud-run 0.16.0), so the API *cannot* pin an execution to a digest.
  CI must pin the Job spec with `gcloud run jobs update --image=…@sha256:…`, and
  the API reads it back. Until CI does that, every run records
  `image_pin_source="unresolved"` and the reproducibility claim is not yet true.
  `tests/test_api_cloudrun.py` asserts the field's absence, so it will tell you
  if this ever becomes possible.
- **`roles/run.invoker` does not include `run.jobs.runWithOverrides`.** The API
  passes overrides, so a naive invoker grant fails at submit with a 403 — and
  only in production, because local dev runs as the operator. The plan specifies
  a four-permission custom role instead of `roles/run.developer`, which would
  also let a compromised public API repoint the Job at another image.
- **Every Docker test invocation needs `-m docker`.** `pyproject.toml` defaults
  `addopts` to `-m 'not docker'`, so naming a Docker file without it deselects
  everything and exits 5.
- **`up.sh` currently un-pins any digest CI sets**, because its Job block
  unconditionally re-applies `--image`. Task 5 fixes it; don't run `up.sh` after
  a digest-pinning deploy until then.
- **Endpoint tests are based on the wall clock, not a fixed instant**, because
  the routers call `datetime.now(timezone.utc)` directly. Seeding a stale
  timestamp makes the reconciler correctly declare the run dead — which is how
  that behaviour got confirmed.
- A stray `google_cloud_storage-3.13.0-py3-none-any.whl` sits at the repo root
  from an earlier session. `*.whl` is now gitignored so it can't be committed by
  accident, but **the file is still on disk** — it wasn't deleted, since this
  session didn't create it. Remove it whenever.
- **16 commits sit unpushed on `main`.** This repo has no PR gate (single
  developer, direct-to-main is the recorded working model), so pushing is a
  deliberate call. Worth noting that until Task 6 lands, CI will only re-run the
  three Docker suites on these commits — none of the 296 new fast tests are
  wired into the current workflow.
