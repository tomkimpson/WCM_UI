IMAGE_TAG ?= wcm-ui/worker:dev

.PHONY: help build test-image smoke clean

help:
	@echo "Targets:"
	@echo "  build       Build the worker Docker image"
	@echo "  test-image  Run image build/import tests"
	@echo "  smoke       Build the image and run the end-to-end smoke simulation (~13 min)"
	@echo "  clean       Remove local Python/pytest artefacts (does NOT touch Docker images)"

build:
	docker build -t $(IMAGE_TAG) -f worker/Dockerfile .

test-image:
	pytest tests/test_image_builds.py -v

smoke: build
	pytest tests/test_smoke_sim.py -v

clean:
	rm -rf out/ __pycache__/ .pytest_cache/
