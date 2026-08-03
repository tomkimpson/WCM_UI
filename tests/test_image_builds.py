import subprocess

import pytest

pytestmark = pytest.mark.docker


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
