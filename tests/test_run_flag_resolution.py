"""Verify run.py produces the right CLI invocations for given params.

Doesn't actually run wcEcoli — patches subprocess.run and inspects the
recorded calls. The end-to-end smoke (Task 7) verifies the real binding.
"""
from unittest.mock import MagicMock

from worker.run import resolve, build_commands, main


def test_resolve_applies_defaults_when_input_empty():
    resolved = resolve({})
    assert resolved["simulation"]["length_sec"] == 60
    assert resolved["simulation"]["seed"] == 0


def test_resolve_user_overrides_defaults():
    resolved = resolve({"simulation": {"length_sec": 120, "seed": 7}})
    assert resolved["simulation"]["length_sec"] == 120
    assert resolved["simulation"]["seed"] == 7
    # Untouched fields keep defaults
    assert resolved["simulation"]["generations"] == 1


def test_build_commands_emits_parca_then_runsim():
    resolved = {"simulation": {"length_sec": 120, "seed": 3, "generations": 2,
                                "init_sims": 1, "parca_cpus": 2}}
    cmds = build_commands(resolved)
    assert len(cmds) == 2
    assert cmds[0] == ["python3", "runscripts/manual/runParca.py", "--cpus", "2"]
    assert cmds[1] == [
        "python3", "runscripts/manual/runSim.py",
        "--length-sec", "120",
        "--seed", "3",
        "--generations", "2",
        "--init-sims", "1",
    ]


def test_main_invokes_subprocess_for_each_command(monkeypatch, tmp_path):
    params_file = tmp_path / "params.json"
    params_file.write_text('{"simulation": {"length_sec": 30}}')
    monkeypatch.setenv("PARAMS_JSON", str(params_file))

    calls = []

    def fake_run(cmd, cwd=None):
        calls.append((cmd, cwd))
        return MagicMock(returncode=0)

    monkeypatch.setattr("worker.run.subprocess.run", fake_run)
    rc = main()
    assert rc == 0
    assert len(calls) == 2
    assert calls[0][0][:2] == ["python3", "runscripts/manual/runParca.py"]
    assert "--length-sec" in calls[1][0]
    assert "30" in calls[1][0]


def test_main_returns_nonzero_on_parca_failure(monkeypatch, tmp_path):
    params_file = tmp_path / "params.json"
    params_file.write_text("{}")
    monkeypatch.setenv("PARAMS_JSON", str(params_file))

    def fake_run(cmd, cwd=None):
        return MagicMock(returncode=2)

    monkeypatch.setattr("worker.run.subprocess.run", fake_run)
    assert main() == 2


def test_main_accepts_inline_json(monkeypatch):
    """If $PARAMS_JSON is JSON (not a path), parse it directly."""
    monkeypatch.setenv("PARAMS_JSON", '{"simulation": {"length_sec": 45}}')

    calls = []

    def fake_run(cmd, cwd=None):
        calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr("worker.run.subprocess.run", fake_run)
    assert main() == 0
    runsim_cmd = calls[1]
    assert runsim_cmd[runsim_cmd.index("--length-sec") + 1] == "45"


def test_main_handles_params_json_pointing_at_directory(monkeypatch, tmp_path):
    """PARAMS_JSON pointing at a directory (not a file) should exit cleanly."""
    monkeypatch.setenv("PARAMS_JSON", str(tmp_path))

    # subprocess should never be called — main() should fail at param load.
    def fake_run(cmd, cwd=None):
        raise AssertionError("subprocess.run must not be invoked on param error")

    monkeypatch.setattr("worker.run.subprocess.run", fake_run)
    assert main() == 64
