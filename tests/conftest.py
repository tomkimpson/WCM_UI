"""Shared pytest fixtures for the host-side worker tests.

The `built_image` fixture builds the worker Docker image once per pytest
session at the canonical `wcm-ui/worker:test` tag, so both
`test_image_builds.py` and `test_smoke_sim.py` can run off a single build.
"""
import subprocess

import pytest

IMAGE_TAG = "wcm-ui/worker:test"


@pytest.fixture(scope="session")
def built_image():
    result = subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, "-f", "worker/Dockerfile", "."],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            f"docker build failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return IMAGE_TAG
