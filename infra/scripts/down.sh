#!/usr/bin/env bash
# infra/scripts/down.sh — tear down the Stage 2 dev environment.
#
# Pass --purge-firestore to also wipe the Firestore database (default is to
# preserve it — deleting Firestore also deletes all run history).

set -euo pipefail

PROJECT="${PROJECT:-wcm-ui-dev}"
REGION="${REGION:-us-central1}"
BUCKET="${BUCKET:-wcm-ui-runs-dev}"
SA_NAME="${SA_NAME:-wcm-ui-worker-dev}"
JOB_NAME="${JOB_NAME:-wcm-ui-worker-dev}"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

PURGE_FIRESTORE=0
for arg in "$@"; do
  case "$arg" in
    --purge-firestore) PURGE_FIRESTORE=1 ;;
  esac
done

log() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }

if gcloud run jobs describe "${JOB_NAME}" --region="${REGION}" --project="${PROJECT}" >/dev/null 2>&1; then
  log "Deleting Cloud Run Job ${JOB_NAME}"
  gcloud run jobs delete "${JOB_NAME}" --region="${REGION}" --project="${PROJECT}" --quiet
fi

if gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT}" >/dev/null 2>&1; then
  log "Deleting service account ${SA_EMAIL}"
  gcloud iam service-accounts delete "${SA_EMAIL}" --project="${PROJECT}" --quiet
fi

if gcloud storage buckets describe "gs://${BUCKET}" --project="${PROJECT}" >/dev/null 2>&1; then
  log "Emptying and deleting bucket gs://${BUCKET}"
  gcloud storage rm --recursive "gs://${BUCKET}/**" --project="${PROJECT}" 2>/dev/null || true
  gcloud storage buckets delete "gs://${BUCKET}" --project="${PROJECT}" --quiet
fi

if [[ "${PURGE_FIRESTORE}" == "1" ]]; then
  log "Purging Firestore default database"
  gcloud firestore databases delete --database='(default)' --project="${PROJECT}" --quiet
else
  log "Skipping Firestore (pass --purge-firestore to delete it and lose run history)"
fi

log "Done."
