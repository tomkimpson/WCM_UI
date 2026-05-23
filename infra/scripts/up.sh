#!/usr/bin/env bash
# infra/scripts/up.sh — idempotent provisioner for the WCM_UI Stage 2 dev env.
#
# What this script provisions (re-run safely after image bumps or config
# changes):
#
#   - Enabled APIs: run.googleapis.com, firestore.googleapis.com,
#     storage.googleapis.com, iamcredentials.googleapis.com
#   - GCS bucket gs://wcm-ui-runs-dev with 30-day lifecycle on output.tar.gz
#
# Subsequent tasks layer Firestore (Task 2), the worker service account +
# IAM + Cloud Run Job (Task 3) into the same script.
#
# Pre-reqs: see infra/README.md.

set -euo pipefail

PROJECT="${PROJECT:-wcm-ui-dev}"
REGION="${REGION:-us-central1}"
BUCKET="${BUCKET:-wcm-ui-runs-dev}"
SA_NAME="${SA_NAME:-wcm-ui-worker-dev}"
JOB_NAME="${JOB_NAME:-wcm-ui-worker-dev}"
AR_REPO="${AR_REPO:-wcm-ui-worker}"
AR_IMAGE_NAME="${AR_IMAGE_NAME:-worker}"
CI_SA_NAME="${CI_SA_NAME:-wcm-ui-ci-push-dev}"
WIF_POOL="${WIF_POOL:-wcm-ui-github}"
WIF_PROVIDER="${WIF_PROVIDER:-github}"
GH_REPO="${GH_REPO:-tomkimpson/WCM_UI}"

SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
CI_SA_EMAIL="${CI_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
IMAGE_URI="${IMAGE_URI:-${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/${AR_IMAGE_NAME}:latest}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# -----------------------------------------------------------------------------
# 1. APIs
# -----------------------------------------------------------------------------
log "Enabling required APIs on project ${PROJECT}"
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  iamcredentials.googleapis.com \
  artifactregistry.googleapis.com \
  sts.googleapis.com \
  --project="${PROJECT}"

# -----------------------------------------------------------------------------
# 2. GCS runs bucket + lifecycle
# -----------------------------------------------------------------------------
log "Ensuring GCS bucket gs://${BUCKET}"
if ! gcloud storage buckets describe "gs://${BUCKET}" --project="${PROJECT}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" \
    --project="${PROJECT}" \
    --location="${REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention
else
  log "  bucket already exists, leaving in place"
fi

# Lifecycle: delete output.tar.gz after 30 days. Parquet, params.json, and
# stderr.log are tiny and kept indefinitely for debugging.
LIFECYCLE_FILE="$(mktemp)"
trap 'rm -f "${LIFECYCLE_FILE}"' EXIT
cat > "${LIFECYCLE_FILE}" <<'EOF'
{
  "lifecycle": {
    "rule": [
      {
        "action": {"type": "Delete"},
        "condition": {"age": 30, "matchesSuffix": ["output.tar.gz"]}
      }
    ]
  }
}
EOF
log "Applying lifecycle rule (30d expiry on output.tar.gz)"
gcloud storage buckets update "gs://${BUCKET}" \
  --project="${PROJECT}" \
  --lifecycle-file="${LIFECYCLE_FILE}"

# -----------------------------------------------------------------------------
# 3. Firestore (Native mode) default database
# -----------------------------------------------------------------------------
# Schemaless. The runs/{run_id} document shape lives in worker/db.py's
# module docstring — that's the source of truth.
log "Ensuring Firestore default database (native mode, ${REGION})"
if ! gcloud firestore databases describe --database='(default)' --project="${PROJECT}" >/dev/null 2>&1; then
  gcloud firestore databases create \
    --location="${REGION}" \
    --type=firestore-native \
    --project="${PROJECT}"
else
  log "  database already exists, leaving in place"
fi

# -----------------------------------------------------------------------------
# 4. Artifact Registry repo for the worker image
# -----------------------------------------------------------------------------
# Cloud Run Jobs can't pull from ghcr.io — only gcr.io, *.docker.pkg.dev,
# or docker.io. We host the worker image in Artifact Registry here, in the
# same project + region as Cloud Run, so Cloud Run's service agent has
# automatic pull access (no extra IAM).
log "Ensuring Artifact Registry repo ${AR_REPO}"
if ! gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" --project="${PROJECT}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${AR_REPO}" \
    --location="${REGION}" \
    --repository-format=docker \
    --description="WCM_UI worker images (Stage 2 dev)" \
    --project="${PROJECT}"
else
  log "  repo already exists, leaving in place"
fi

# -----------------------------------------------------------------------------
# 5. Worker + CI service accounts
# -----------------------------------------------------------------------------
log "Ensuring worker service account ${SA_EMAIL}"
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="WCM_UI Stage 2 worker (dev)" \
    --project="${PROJECT}"
else
  log "  worker SA already exists, leaving in place"
fi

log "Ensuring CI push service account ${CI_SA_EMAIL}"
if ! gcloud iam service-accounts describe "${CI_SA_EMAIL}" --project="${PROJECT}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${CI_SA_NAME}" \
    --display-name="WCM_UI CI image push (dev)" \
    --project="${PROJECT}"
else
  log "  CI push SA already exists, leaving in place"
fi

# -----------------------------------------------------------------------------
# 6. Workload Identity Federation for GitHub Actions
# -----------------------------------------------------------------------------
# Allows GitHub Actions on the ${GH_REPO} repo to impersonate the CI push
# SA without checking long-lived JSON keys into GitHub Secrets. Uses OIDC
# tokens minted by GitHub for each workflow run.
log "Ensuring Workload Identity Pool ${WIF_POOL}"
if ! gcloud iam workload-identity-pools describe "${WIF_POOL}" --location=global --project="${PROJECT}" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "${WIF_POOL}" \
    --location=global \
    --display-name="GitHub Actions WIF pool" \
    --project="${PROJECT}"
else
  log "  pool already exists, leaving in place"
fi

WIF_POOL_NAME="$(gcloud iam workload-identity-pools describe "${WIF_POOL}" --location=global --project="${PROJECT}" --format='value(name)')"

log "Ensuring WIF provider ${WIF_PROVIDER} (constrained to repo ${GH_REPO})"
if ! gcloud iam workload-identity-pools providers describe "${WIF_PROVIDER}" \
     --workload-identity-pool="${WIF_POOL}" --location=global --project="${PROJECT}" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-oidc "${WIF_PROVIDER}" \
    --workload-identity-pool="${WIF_POOL}" \
    --location=global \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository == '${GH_REPO}'" \
    --project="${PROJECT}"
else
  log "  provider already exists, leaving in place"
fi

# Bind the GitHub repo's WIF subject to the CI push SA.
log "Granting WIF impersonation on ${CI_SA_EMAIL} to repo ${GH_REPO}"
gcloud iam service-accounts add-iam-policy-binding "${CI_SA_EMAIL}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/${WIF_POOL_NAME}/attribute.repository/${GH_REPO}" \
  --project="${PROJECT}" \
  --condition=None >/dev/null

# -----------------------------------------------------------------------------
# 7. IAM bindings (worker SA + CI SA)
# -----------------------------------------------------------------------------
# add-iam-policy-binding is idempotent — no-ops on duplicates.
log "Granting roles/storage.objectAdmin on gs://${BUCKET} to worker SA"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectAdmin" \
  --project="${PROJECT}" \
  --condition=None >/dev/null

log "Granting roles/datastore.user on ${PROJECT} to worker SA"
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/datastore.user" \
  --condition=None >/dev/null

log "Granting roles/artifactregistry.writer on repo ${AR_REPO} to CI SA"
gcloud artifacts repositories add-iam-policy-binding "${AR_REPO}" \
  --location="${REGION}" \
  --member="serviceAccount:${CI_SA_EMAIL}" \
  --role="roles/artifactregistry.writer" \
  --project="${PROJECT}" \
  --condition=None >/dev/null

# -----------------------------------------------------------------------------
# 8. Cloud Run Job
# -----------------------------------------------------------------------------
# Image is referenced by :latest — Cloud Run only pulls it at execution
# time, so the job spec can be created before the first CI push. After
# the first push, the existing :latest tag is hit automatically; nothing
# in the job spec needs to change. Job env vars at execution time
# (RUN_ID, PARAMS_JSON, IMAGE_URI) come from `scripts/submit.py`'s
# container override.
JOB_ENV="GCP_PROJECT=${PROJECT},RUNS_BUCKET=${BUCKET},FIRESTORE_DATABASE=(default)"

if gcloud run jobs describe "${JOB_NAME}" --region="${REGION}" --project="${PROJECT}" >/dev/null 2>&1; then
  log "Updating Cloud Run Job ${JOB_NAME}"
  gcloud run jobs update "${JOB_NAME}" \
    --image="${IMAGE_URI}" \
    --region="${REGION}" \
    --project="${PROJECT}" \
    --service-account="${SA_EMAIL}" \
    --cpu=4 \
    --memory=16Gi \
    --task-timeout=14400s \
    --max-retries=1 \
    --set-env-vars="${JOB_ENV}"
else
  log "Creating Cloud Run Job ${JOB_NAME}"
  gcloud run jobs create "${JOB_NAME}" \
    --image="${IMAGE_URI}" \
    --region="${REGION}" \
    --project="${PROJECT}" \
    --service-account="${SA_EMAIL}" \
    --cpu=4 \
    --memory=16Gi \
    --task-timeout=14400s \
    --max-retries=1 \
    --set-env-vars="${JOB_ENV}"
fi

log "Done."
echo
log "GitHub Actions secrets (set via 'gh secret set' or repo settings):"
echo "  GCP_WIF_PROVIDER  = ${WIF_POOL_NAME}/providers/${WIF_PROVIDER}"
echo "  GCP_CI_SA_EMAIL   = ${CI_SA_EMAIL}"
