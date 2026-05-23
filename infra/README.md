# infra/

GCP infrastructure for the WCM_UI Stage 2 (Cloud Run Jobs + Firestore + GCS)
dev environment.

The provisioning is intentionally lightweight: a single idempotent bash
script (`scripts/up.sh`) using `gcloud`. No Terraform yet — at this scope
(one env, ~5 resources, solo developer) the IaC boilerplate outweighs the
benefit. Port to Terraform when a second environment lands.

## One-time setup (operator)

```bash
# 1. Install the Google Cloud SDK.
brew install google-cloud-sdk

# 2. Authenticate. Two flows:
gcloud auth login                       # for interactive `gcloud` commands
gcloud auth application-default login   # for Python clients (boto3-style)

# 3. Create a project (any name works — the script accepts PROJECT=).
gcloud projects create wcm-ui-dev
gcloud config set project wcm-ui-dev

# 4. Link a billing account (required for Cloud Run + Firestore + GCS).
#    List your billing accounts:
gcloud billing accounts list
#    Link one:
gcloud billing projects link wcm-ui-dev \
  --billing-account=<XXXXXX-YYYYYY-ZZZZZZ>
```

## Provisioning

```bash
bash infra/scripts/up.sh
```

The script is idempotent — re-run it after any change to the script
itself, after a worker image tag bump, or when bringing up a fresh
project.

Override defaults via env vars if needed:

```bash
PROJECT=my-other-project REGION=europe-west1 bash infra/scripts/up.sh
```

## Verifying

```bash
gcloud storage buckets describe gs://wcm-ui-runs-dev
gcloud storage buckets describe gs://wcm-ui-runs-dev --format="value(lifecycle)"
# Once Tasks 2 and 3 land:
gcloud firestore databases describe --database='(default)'
gcloud run jobs describe wcm-ui-worker-dev --region=us-central1
```

## Tearing down

```bash
bash infra/scripts/down.sh
```

Empties the runs bucket, deletes the Cloud Run Job and service account.
Firestore is preserved by default (deleting it removes all run history);
pass `--purge-firestore` to wipe it too.
