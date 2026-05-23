# infra/

GCP infrastructure for the WCM_UI Stage 2 (Cloud Run Jobs + Firestore + GCS +
Artifact Registry) dev environment.

The provisioning is intentionally lightweight: a single idempotent bash
script (`scripts/up.sh`) using `gcloud`. No Terraform yet — at this scope
(one env, ~7 resources, solo developer) the IaC boilerplate outweighs the
benefit. Port to Terraform when a second environment lands.

What `up.sh` provisions:

  1. APIs: run, firestore, storage, iamcredentials, artifactregistry, sts
  2. GCS bucket `wcm-ui-runs-dev` (uniform access, 30-day lifecycle on `.tar.gz`)
  3. Firestore default database (native mode, us-central1)
  4. Artifact Registry repo `wcm-ui-worker` (docker, us-central1)
  5. Worker SA `wcm-ui-worker-dev` (storage.objectAdmin on bucket, datastore.user on project)
  6. CI push SA `wcm-ui-ci-push-dev` (artifactregistry.writer on AR repo)
  7. Workload Identity Federation pool + provider trusting GitHub OIDC tokens
     from `tomkimpson/WCM_UI` only — lets CI push to AR without long-lived keys
  8. Cloud Run Job `wcm-ui-worker-dev` (4 vCPU / 16 Gi / 4h timeout)

## One-time setup (operator)

```bash
# 1. Install the Google Cloud SDK.
brew install google-cloud-sdk

# 2. Authenticate. Two flows:
gcloud auth login                       # for interactive `gcloud` commands
gcloud auth application-default login   # for Python clients

# 3. Create a project.
gcloud projects create wcm-ui-dev
gcloud config set project wcm-ui-dev

# 4. Link a billing account (required for Cloud Run + Firestore + GCS).
gcloud billing accounts list
gcloud billing projects link wcm-ui-dev --billing-account=<XXXXXX-YYYYYY-ZZZZZZ>
```

## Provisioning

```bash
bash infra/scripts/up.sh
```

The script is idempotent — re-run after script edits or to bring up a fresh
project. Override defaults via env vars:

```bash
PROJECT=my-other-project REGION=europe-west1 bash infra/scripts/up.sh
```

After the first successful run, `up.sh` prints two values to plumb into
GitHub Actions:

```
GCP_WIF_PROVIDER  = projects/.../workloadIdentityPools/wcm-ui-github/providers/github
GCP_CI_SA_EMAIL   = wcm-ui-ci-push-dev@wcm-ui-dev.iam.gserviceaccount.com
```

Set them as **repository variables** (not secrets — they aren't sensitive):

```bash
gh variable set GCP_WIF_PROVIDER --body "<value>"
gh variable set GCP_CI_SA_EMAIL  --body "<value>"
```

## Image bootstrap (chicken-and-egg)

Cloud Run Jobs creation requires the image to exist. The first time you run
`up.sh` on a fresh project, push a placeholder so the job spec can be
created — CI will overwrite it with the real worker image on the next push
to `main`:

```bash
docker pull alpine:3.20
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
docker tag alpine:3.20 us-central1-docker.pkg.dev/wcm-ui-dev/wcm-ui-worker/worker:latest
docker push us-central1-docker.pkg.dev/wcm-ui-dev/wcm-ui-worker/worker:latest
```

## Verifying

```bash
gcloud storage buckets describe gs://wcm-ui-runs-dev
gcloud firestore databases describe --database='(default)'
gcloud artifacts repositories describe wcm-ui-worker --location=us-central1
gcloud run jobs describe wcm-ui-worker-dev --region=us-central1
```

## Tearing down

```bash
bash infra/scripts/down.sh
```

Empties the runs bucket, deletes the Cloud Run Job and service account.
Firestore is preserved by default (deleting it removes all run history);
pass `--purge-firestore` to wipe it too. WIF pool + provider and the AR
repo are left in place by default (cheap to keep, expensive to recreate).
