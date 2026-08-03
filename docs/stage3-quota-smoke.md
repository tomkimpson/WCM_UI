# Stage 3 quota smoke

Manual acceptance procedure for the cost ceilings. Not automated in CI — it
launches real simulations and needs GCP credentials — so it lives here as the
canonical proof that the ceilings hold against the real platform.

**Cost of a full pass: about 5 runs at $0.16 each ≈ $0.80.** That number is
recorded because a procedure whose cost is unknown is a procedure nobody runs.

## What the automated tests already cover

Don't repeat these by hand:

- The reserve/release/finish logic, the three ceilings, stale-lease eviction and
  the kill switch — `tests/test_quota_lease.py`, against a dict-backed fake.
- That the transaction genuinely serialises under contention —
  `make quota-emulator` (`tests/test_quota_concurrency.py`). The emulator is
  free, so this is a CI job rather than a manual step.
- That `Overrides.timeout` is set on the request, and that `ContainerOverride`
  has no `image` field — `tests/test_api_cloudrun.py`.

## What only a live run can prove

Three assumptions the emulator and the fakes structurally cannot reach. Each is
load-bearing, and each fails in a different way.

### 1. `Overrides.timeout` really kills the task

**This is the most important step in the document.** The quota lease TTL is
`wall_clock_cap_sec + lease_grace_sec`, and evicting a lease at that age is only
*safe* — as opposed to a guess that could reclaim a slot from a live run — if the
platform has definitely killed the task by the cap. Verify it once:

```bash
WCM_WALL_CLOCK_CAP_SEC=300 python -m scripts.submit \
  --params tests/fixtures/length_sec_30.json
# → note the execution name

gcloud run jobs executions describe <execution-name> --region=us-central1 \
  --format='value(status.completionTime,status.failedCount,status.cancelledCount)'
```

Expected: the execution is killed at **~5 minutes**, not at the job spec's
`--task-timeout` (3600s). If it runs to 60 minutes instead, `Overrides.timeout`
is not being honoured and **the lease TTL must be raised to match the job spec's
timeout** until it is.

### 2. The service cannot wedge closed

A run killed by infrastructure never releases its lease — the worker cannot run
code through a SIGKILL. If nothing reclaimed it, the concurrency ceiling would
close permanently.

```bash
# Fill the ceiling, then destroy one execution out from under the worker.
WCM_MAX_CONCURRENT_RUNS=1 python -m scripts.submit --params tests/fixtures/length_sec_30.json
gcloud run jobs executions delete <execution-name> --region=us-central1 --quiet

# Immediately: refused, because the lease is still held.
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$API/api/runs" \
  -H 'content-type: application/json' -d '{"params":{}}'
```

Expected `429` now, and `202` after `wall_clock_cap_sec + lease_grace_sec`
(60 minutes with the defaults) without anyone intervening. Also confirm the
status endpoint stops claiming the run is alive:

```bash
curl -sS "$API/api/runs/<run_id>" | python3 -c "
import sys,json; d=json.load(sys.stdin)
print(d['state'], '|', d.get('failure_source'), '|', d.get('error_message'))"
```

Expected: `failed | infrastructure | …`. That comes from reconciliation on the
read path, which protects the *UI*; the lease TTL is what protected the ceiling.

### 3. Signed URLs actually work

Four independent things can break this and only the first is visible in code.
Each produces a different error, which is why the check asserts on the
credential rather than just the status:

```bash
URL=$(curl -sS "$API/api/runs/<run_id>/download/tarball" -o /dev/null -w '%{redirect_url}')
echo "$URL" | tr '&' '\n' | grep X-Goog-Credential
curl -sS -o /tmp/out.tar.gz -w '%{http_code} %{size_download}\n' "$URL"
```

| Symptom | Cause |
|---|---|
| `X-Goog-Credential` contains `default@` | `credentials.refresh()` not called before reading `service_account_email` |
| API returns 500, `iam.serviceAccounts.signBlob denied` | the API SA lacks `serviceAccountTokenCreator` **on itself** |
| URL signs fine, then 403 `AccessDenied` | the API SA lacks `storage.objectViewer` on the bucket — a signed URL authorises *as the signer* and GCS re-checks at request time |
| 403 `SignatureDoesNotMatch` | clock skew, or a malformed canonical request |

Then confirm the two things that make the URL a download link rather than a CDN:

```bash
sleep 901 && curl -sS -o /dev/null -w '%{http_code}\n' "$URL"     # expect 403 ExpiredToken
curl -sS -o /dev/null -w '%{http_code}\n' \
  "https://storage.googleapis.com/wcm-ui-runs-dev/<run_id>/output.tar.gz"  # expect 403
```

The second is public-access-prevention still holding. **If it returns 200, stop
and fix the bucket** — every run's output is world-readable and egress is
unmetered.

## Ceilings, cheaply

These submissions are *refused*, so they cost nothing.

```bash
# Per-IP cap (3/day). The first three cost money; only run when willing to pay.
for i in 1 2 3 4; do
  curl -sS -o /dev/null -w "attempt $i: %{http_code}\n" -X POST "$API/api/runs" \
    -H 'content-type: application/json' -d '{"params":{"simulation":{"length_sec":30}}}'
done
```

Expected `202 202 202 429`, and the 429 body's `error` should be
`daily_ip_limit_reached` — **distinct from** `at_concurrency_limit` and
`daily_limit_reached`, so the frontend can word all three differently.

Kill switch:

```bash
python3 -c "
from google.cloud import firestore
firestore.Client(project='wcm-ui-dev').collection('config').document('global').set(
    {'accepting_runs': False, 'message': 'at capacity for the month'})"
sleep 31   # the flag is cached for 30s per instance
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$API/api/runs" \
  -H 'content-type: application/json' -d '{"params":{}}'
curl -sS "$API/api/config" | python3 -c "import sys,json; print(json.load(sys.stdin)['accepting_runs'])"
```

Expected `503`, then `False`. **Remember to set it back to `true`.** Note the
30-second cache is a priced tradeoff, not an oversight: worst-case overshoot is
bounded by `max_concurrent_runs`, so about $0.56.

Zero-cost checks:

```bash
# Multi-generation runs are refused before anything launches.
curl -sS -X POST "$API/api/runs" -H 'content-type: application/json' \
  -d '{"params":{"simulation":{"generations":4}}}' | python3 -m json.tool
gcloud run jobs executions list --job=wcm-ui-worker-dev --region=us-central1 --limit=1
```

Expected a 4xx naming `simulation.generations`, and **no new execution**.

## What is still not covered

- **Per-IP quota evasion.** `X-Forwarded-For` is client-supplied; we read the
  rightmost entry, which Cloud Run appends, but an IPv6 /48 holder commands
  65,536 /64s. The per-IP cap is fairness; the **global daily cap** is the
  control that protects the bill. Turnstile is the only measure that would
  meaningfully raise the cost of rotation, and it is deferred.
- **Polling load.** `GET /api/runs/{id}` is public and unauthenticated. The
  ceiling on it is `--max-instances=3` on the Cloud Run service, not anything in
  this document. Worst case is around $65/month with no submissions at all,
  which is the largest single line in the adversarial cost model.
- **The tarball size**, which is the biggest unknown in that model. Measure it
  during any successful pass:
  `gcloud storage ls -l gs://wcm-ui-runs-dev/<run_id>/output.tar.gz`. If it is
  ~1 GB, excluding the regenerable parca `kb/` output from `make_tarball` is a
  one-line change worth more than any ceiling here.

## Verified runs

| Date | run_id | Step | Outcome | Notes |
|------|--------|------|---------|-------|
|      |        | 1. `Overrides.timeout` kills at the cap | | measured kill time vs the 300s cap |
|      |        | 2. lease evicted after cap + grace | | minutes until 202 returned |
|      |        | 3. signed URL followed | | `X-Goog-Credential` account, bytes |
|      |        | ceilings 429 | | which `error` slug for each |
|      |        | kill switch 503 | | flag restored to true? |
|      |        | tarball size | | bytes |
