IMAGE_TAG ?= wcm-ui/worker:dev
PARAMS    ?= tests/fixtures/length_sec_30.json
API_PORT  ?= 8000
API_SA    ?= wcm-ui-api-dev@wcm-ui-dev.iam.gserviceaccount.com

.PHONY: help deps test test-all build test-image smoke e2e api quota-emulator submit clean

help:
	@echo "Targets:"
	@echo "  deps        Install host-side test/dev dependencies"
	@echo "  test        Run the fast unit tests — no Docker, seconds"
	@echo "  test-all    Run the fast tests AND every Docker suite (~35 min)"
	@echo "  api         Run the API locally on API_PORT (default 8000), with reload"
	@echo "  quota-emulator  Prove the quota transaction against the Firestore emulator"
	@echo "  build       Build the worker Docker image"
	@echo "  test-image  Run image build/import tests"
	@echo "  smoke       Build the image and run the end-to-end smoke simulation (~13 min)"
	@echo "  e2e         Build the image and run the parametric end-to-end test (~15 min)"
	@echo "  submit      Submit a Cloud Run Jobs execution from PARAMS=path/to/params.json"
	@echo "  clean       Remove local Python/pytest artefacts (does NOT touch Docker images)"

deps:
	pip install -r requirements-dev.txt

# pyproject.toml's addopts default to -m 'not docker', so bare pytest is fast.
test:
	pytest -q

test-all:
	pytest -q
	pytest -m docker -v

build:
	docker build -t $(IMAGE_TAG) -f worker/Dockerfile .

# -m docker is mandatory on every target below: the addopts default of
# `not docker` would otherwise deselect these files and pytest would exit 5.
test-image:
	pytest -m docker tests/test_image_builds.py -v

smoke: build
	pytest -m docker tests/test_smoke_sim.py -v

e2e: build
	pytest -m docker tests/test_run_with_params.py -v

# Runs against REAL Firestore/GCS/Cloud Run in wcm-ui-dev — there is no emulator
# for those. V4 signed URLs need a credential that can SIGN, which plain
# `gcloud auth application-default login` cannot do. Once:
#   gcloud auth application-default login \
#     --impersonate-service-account=$(API_SA)
api:
	GCP_PROJECT=wcm-ui-dev \
	RUNS_BUCKET=wcm-ui-runs-dev \
	FIRESTORE_DATABASE='(default)' \
	WORKER_JOB_NAME=wcm-ui-worker-dev \
	WORKER_JOB_REGION=us-central1 \
	ALLOWED_ORIGINS=http://localhost:5173 \
	uvicorn api.main:app --reload --port $(API_PORT)

# The only test that proves the quota transaction actually serialises; mocking
# the Firestore client mocks away the thing under test. The emulator is free and
# starts in seconds, so this belongs in CI.
quota-emulator:
	@command -v gcloud >/dev/null || { echo "gcloud SDK required for the emulator"; exit 1; }
	gcloud emulators firestore start --host-port=localhost:8080 & \
	  EMU=$$!; sleep 5; \
	  FIRESTORE_EMULATOR_HOST=localhost:8080 \
	    pytest tests/test_quota_concurrency.py -v; \
	  RC=$$?; kill $$EMU 2>/dev/null; exit $$RC

submit:
	python -m scripts.submit --params $(PARAMS)

clean:
	rm -rf out/ __pycache__/ .pytest_cache/
