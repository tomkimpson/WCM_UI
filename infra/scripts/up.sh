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

log "Done."
