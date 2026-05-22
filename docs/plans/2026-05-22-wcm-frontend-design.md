# WCM Frontend — Design

**Date:** 2026-05-22
**Status:** Validated design, ready for implementation planning

## Goal

A public web frontend that lets researchers configure and run the
[wcEcoli](https://github.com/CovertLab/wcEcoli) whole-cell model on
cloud compute, view results in-browser, and download raw outputs.

## Audience and access

- **Primary audience:** researchers and collaborators familiar with
  whole-cell modelling.
- **Access:** public, no login (v1). Acceptable only because hard
  quotas, per-IP rate limits, wall-clock caps, and a budget kill-switch
  cap the worst-case cost. Auth deferred to v2.

## Architecture

Four independently deployable pieces:

1. **Frontend (SPA)** — React or Svelte. Auto-generated form for
   curated parameters (from JSON Schema), Monaco YAML editor for
   overrides, run history, plot viewer (Plotly or Vega-Lite). Hosted on
   Cloudflare Pages or S3+CloudFront.
2. **API service** — FastAPI (Python). Validates parameters, enforces
   quotas, enqueues jobs, serves run metadata and plot data. Stateless,
   horizontally scalable, behind a load balancer.
3. **Job orchestrator + workers** — wcEcoli packaged as a Docker image
   (pinned to a git SHA). Jobs run on AWS Batch (Fargate spot) or
   equivalent. Each job pulls params, runs the sim, uploads outputs.
4. **Storage** — Postgres for run metadata; S3 for raw outputs
   (full wcEcoli output tarball + compact Parquet of curated time
   series).

The frontend never talks to compute. The API never blocks on a sim.

## Job lifecycle

1. **Submit** — `POST /api/runs` with `{params, optional_yaml_override}`.
   Schema-validated, quota-checked, written as `queued`, returns `run_id`.
2. **Enqueue** — API submits a Batch job with the image SHA, the
   resolved parameter JSON (via env var or small S3 object), and the
   `run_id`.
3. **Execute** — Worker container writes params into wcEcoli's config
   format, runs the sim. A wrapper POSTs heartbeats to
   `/api/runs/{id}/heartbeat` so the frontend can show progress
   ("step 3/8: parca complete, simulating generation 1...").
4. **Post-process** — On success, the wrapper extracts curated time
   series to Parquet, tarballs the full output directory, uploads both
   to S3, and PATCHes the run row to `succeeded` with the S3 keys.
5. **Display** — Frontend polls `/api/runs/{id}` (or subscribes via
   SSE). Renders plots from the Parquet client-side; download button
   uses a presigned S3 URL.

Failures (timeout, OOM, crash) PATCH to `failed` with stderr captured
to S3 and surfaced to the user.

## Parameters

**Curated layer.** A JSON Schema (`params.schema.json`) defines 15–25
high-value knobs grouped into:

- *Environment* — media composition, glucose, oxygen.
- *Genetics* — gene knockouts (multi-select), expression-level multipliers.
- *Simulation* — generations, seed, wall-clock cap.
- *Initial conditions* — cell mass, starting-state preset.

Each field has units, range, default, and a tooltip. The frontend
auto-generates the form (e.g. via `react-jsonschema-form`), so adding
a knob is a one-file change.

**Override layer.** A Monaco YAML editor below the form. On submit the
API merges: `defaults ← curated_form ← yaml_override` (later wins).
The merged result is validated against a looser schema that allows any
wcEcoli config key but still type-checks.

## Reproducibility

Every run row stores:

- wcEcoli git SHA (baked into the image tag)
- Docker image digest
- Fully-merged resolved parameter set (JSON)
- Random seed
- Content hash of the above

The results page shows a "Reproduce this run" panel with:

- A one-line CLI command (`docker run ghcr.io/you/wcecoli:<sha> --params <s3-url>`).
- A "Fork these parameters" button that pre-fills a new submission form.

Same hash + same image = identical outputs.

When the wcEcoli image is bumped, old runs stay viewable with a version
badge; the reproduce command still works because images are immutable.

## Deployment

- **Cloud:** AWS to start (Batch + Fargate spot is cheapest for bursty
  heavy workloads).
- **IaC:** Terraform or Pulumi, three environments (`dev`, `staging`,
  `prod`) sharing modules, from day one.
- **CI/CD:** GitHub Actions. PR builds the wcEcoli image, runs a tiny
  smoke sim (one generation, ~5 min) against staging, promotes on
  merge. Frontend deploys to Cloudflare Pages on push to main.

## Cost controls (the load-bearing safety net)

- **Spot only** for compute (~70% savings; fail-and-retry on
  preemption).
- **Hard ceilings enforced in the API**, not just billing alerts:
  - Max concurrent running jobs (e.g. 4)
  - Max queued jobs (e.g. 20)
  - Per-IP daily run count (e.g. 3)
  - Max wall-clock per job (e.g. 2 hours)
- **AWS Budgets alarm** at ~80% of monthly budget triggers a Lambda
  that flips a `accepting_runs=false` feature flag. Frontend shows a
  "at capacity for the month" message.
- **Storage lifecycle:** tarballs auto-expire after 30 days unless
  pinned (pinning will require accounts in v2 — natural funnel).
- **Scale to zero:** API on Fargate min 1 task; Batch min 0 vCPUs.

## Cut list (explicitly NOT in v1)

- No accounts, no auth, no per-user history. `run_id` URL = share link.
- No notebook output, no in-browser analysis, no custom plots.
- No multi-run comparison or parameter sweeps.
- No real-time sim-state streaming (heartbeats are enough).
- No admin dashboard (use CloudWatch + Postgres console).
- No model selection — wcEcoli only, one pinned version at a time.

## Build order

1. Dockerize wcEcoli + one-command local run with params from JSON.
2. Batch job + S3 upload, triggered manually.
3. FastAPI with submit/status/results endpoints, Postgres, quotas.
4. Frontend form + run page + plots.
5. Reproducibility panel + tarball download.
6. Cost guardrails + budget alarm.
7. Polish, docs, soft launch to collaborators.

## Known risks / open questions

- **wcEcoli runtime.** A full multi-generation run can be hours. Need
  to characterise real wall-clock and memory profile on Fargate spot
  before committing to it — may need EC2 spot instead.
- **Parameter surface.** The 15–25 curated knobs aren't picked yet;
  needs a pass against wcEcoli's config to choose biologically
  meaningful ones with sensible ranges.
- **wcEcoli licence.** Confirm it permits hosted-service redistribution.
- **Public abuse vector.** Even with quotas, a coordinated attack can
  fill the queue. May need Cloudflare Turnstile / hCaptcha before
  launch.
