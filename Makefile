IMAGE_TAG ?= wcm-ui/worker:dev
PARAMS    ?= tests/fixtures/length_sec_30.json

.PHONY: help deps test test-all build test-image smoke e2e submit clean

help:
	@echo "Targets:"
	@echo "  deps        Install host-side test/dev dependencies"
	@echo "  test        Run the fast unit tests — no Docker, seconds"
	@echo "  test-all    Run the fast tests AND every Docker suite (~35 min)"
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

submit:
	python -m scripts.submit --params $(PARAMS)

clean:
	rm -rf out/ __pycache__/ .pytest_cache/
