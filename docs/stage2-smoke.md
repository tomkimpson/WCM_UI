# Stage 2 end-to-end smoke

Manual acceptance procedure for Stage 2 (Cloud Run Jobs + Firestore + GCS).
Not automated in CI — costs money, needs GCP creds — so it lives here as
the canonical proof that the cloud loop works.

## Pre-reqs

- `bash infra/scripts/up.sh` has run successfully against `wcm-ui-dev`
- The worker image at `us-central1-docker.pkg.dev/wcm-ui-dev/wcm-ui-worker/worker:latest`
  was built by CI from a green `main` push (not the alpine placeholder)
- `gcloud auth application-default login` is set on the laptop
- `pip install -r scripts/requirements.txt` has run locally

## Happy-path smoke (~13 min)

```bash
python -m scripts.submit --params tests/fixtures/length_sec_30.json
# → prints run_id, execution name, console URL
```

Wait for the Cloud Run execution to finish:

```bash
gcloud run jobs executions describe <execution-name> --region=us-central1
# look at status.completionTime and status.succeededCount
```

Expected final state: `completionTime` set, `succeededCount=1`,
`failedCount=0`.

Verify Firestore + GCS:

```bash
# State transition
python3 -c "
from google.cloud import firestore
d = firestore.Client(project='wcm-ui-dev').collection('runs').document('<run_id>').get().to_dict()
print(d['state'], d.get('gcs_tarball_uri'), d.get('gcs_parquet_uri'))
"
# Expect: 'succeeded' + both URIs populated

# Three artifacts in GCS
gcloud storage ls "gs://wcm-ui-runs-dev/<run_id>/"
# Expect: output.tar.gz, timeseries.parquet, params.json

# Parquet is readable and contains the Mass listener columns
gcloud storage cp "gs://wcm-ui-runs-dev/<run_id>/timeseries.parquet" /tmp/ts.parquet
python3 -c "
import pyarrow.parquet as pq
df = pq.read_table('/tmp/ts.parquet').to_pandas()
print(df.head())
print('listeners:', df['listener'].unique())
print('columns:', df['column'].unique())
print('n_rows:', len(df))
"
# Expect: Mass listener with cellMass/dryMass/proteinMass/rnaMass over ~30 timesteps
```

## Failure-path smoke

Submit a deliberately-broken sim and verify `state=failed` + stderr in GCS.

The cleanest way is to trip a wcEcoli runtime error (a schema-valid param
that produces a sim crash). Without easy levers for that yet, the
fallback is to bypass `submit.py`'s pre-validation and submit a malformed
override directly via `gcloud run jobs execute --update-env-vars`:

```bash
RUN_ID=test-fail-$(uuidgen | tr A-Z a-z)
# Pre-create the Firestore doc so mark_failed has something to update
python3 -c "
from google.cloud import firestore
firestore.Client(project='wcm-ui-dev').collection('runs').document('${RUN_ID}').set({
    'state': 'queued',
    'params_json': {'simulation': {'length_sec': -1}},  # invalid
    'image_uri': 'us-central1-docker.pkg.dev/wcm-ui-dev/wcm-ui-worker/worker:latest',
    'created_at': firestore.SERVER_TIMESTAMP,
})
"
gcloud run jobs execute wcm-ui-worker-dev --region=us-central1 \
  --update-env-vars="RUN_ID=${RUN_ID},PARAMS_JSON={\"simulation\":{\"length_sec\":-1}}"
```

Wait for completion, then check:

```bash
python3 -c "
from google.cloud import firestore
d = firestore.Client(project='wcm-ui-dev').collection('runs').document('${RUN_ID}').get().to_dict()
print(d['state'], d.get('error_message'), d.get('gcs_stderr_uri'))
"
# Expect: 'failed' + non-empty error_message + a gs:// stderr URI

gcloud storage cat "gs://wcm-ui-runs-dev/${RUN_ID}/stderr.log"
# Expect: actual stderr content (param error or sim traceback)
```

## Coverage notes

The two cloud-side failure paths in `worker/run.py` are:

  - **Validation-time failure** — params fail schema validation, no
    subprocess runs, `mark_failed(run_id, message, stderr_uri=None)`.
    Verified live below.
  - **Subprocess-time failure** — wcEcoli exits non-zero, worker
    uploads the captured stderr to `gs://.../{run_id}/stderr.log` and
    calls `mark_failed(run_id, "worker subprocess exited N",
    stderr_uri)`. Triggering this reliably from outside (the schema is
    strict enough that valid params usually run cleanly) is hard
    enough that it's covered by `tests/test_run_lifecycle.py::
    test_cloud_subprocess_failure_uploads_stderr_and_marks_failed`
    rather than the live smoke. If it ever fires for real, we'll see
    the stderr blob materialize in GCS.

## Verified runs

| Date | run_id | Outcome | Notes |
|------|--------|---------|-------|
| 2026-05-23 | `de9f4440-ab51-4a63-a6bc-be4906ef3632` | succeeded | length_sec=30. Took ~26 min wall (provision + image pull + parca + sim + postprocess). Parquet has 124 rows = 31 timesteps × 4 Mass columns (cellMass, dryMass, proteinMass, rnaMass). |
| 2026-05-23 | `813a5342-05ed-4ef4-98f9-2452f6448f59` | failed (validation) | length_sec=-1 (below schema minimum). state=failed, error_message captured, no gcs_stderr_uri (validation-time failure → nothing to upload). |
