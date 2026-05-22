import subprocess
import pytest

IMAGE_TAG = "wcm-ui/worker:test"

@pytest.fixture(scope="session")
def built_image():
    """Build the worker image once per test session."""
    result = subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, "-f", "worker/Dockerfile", "."],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"docker build failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return IMAGE_TAG

def test_image_has_python(built_image):
    result = subprocess.run(
        ["docker", "run", "--rm", built_image, "python3", "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"python3 --version failed:\n{result.stderr}"
    assert result.stdout.startswith("Python 3."), result.stdout

def test_image_has_wcecoli(built_image):
    """wcEcoli is importable AND its Cython extensions compiled.

    Importing a Cython-built submodule (not just `wholecell`) ensures
    `make compile` actually produced the .so artefacts. Task 4's smoke
    sim depends on these; catching their absence here is cheaper than
    discovering it mid-simulation.
    """
    result = subprocess.run(
        [
            "docker", "run", "--rm", built_image,
            "python3", "-c", "import wholecell.utils.mc_complexation; print('ok')",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"import failed:\n{result.stderr}"
    assert "ok" in result.stdout
