IMAGE_TAG ?= wcm-ui/worker:dev

.PHONY: help build test-image clean

help:
	@echo "Targets:"
	@echo "  build       Build the worker Docker image"
	@echo "  test-image  Run image build/import tests"
	@echo "  clean       Remove local build artefacts"

build:
	docker build -t $(IMAGE_TAG) -f worker/Dockerfile .

test-image:
	pytest tests/test_image_builds.py -v

clean:
	rm -rf out/ __pycache__/ .pytest_cache/
