IMAGE_TAG ?= wcm-ui/worker:dev
PARAMS    ?= tests/fixtures/length_sec_30.json

.PHONY: help build test-image smoke e2e submit clean

help:
	@echo "Targets:"
	@echo "  build       Build the worker Docker image"
	@echo "  test-image  Run image build/import tests"
	@echo "  smoke       Build the image and run the end-to-end smoke simulation (~13 min)"
	@echo "  e2e         Build the image and run the parametric end-to-end test (~15 min)"
	@echo "  submit      Submit a Cloud Run Jobs execution from PARAMS=path/to/params.json"
	@echo "  clean       Remove local Python/pytest artefacts (does NOT touch Docker images)"

build:
	docker build -t $(IMAGE_TAG) -f worker/Dockerfile .

test-image:
	pytest tests/test_image_builds.py -v

smoke: build
	pytest tests/test_smoke_sim.py -v

e2e: build
	pytest tests/test_run_with_params.py -v

submit:
	python -m scripts.submit --params $(PARAMS)

clean:
	rm -rf out/ __pycache__/ .pytest_cache/
