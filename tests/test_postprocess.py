"""Verify worker/postprocess.py packages and uploads sim artifacts.

The wcEcoli listener column format is non-trivial (custom chunked binary
read by wholecell.io.tablereader.TableReader). The implementation
lazy-imports TableReader so these host-side tests can mock it without
needing /wcEcoli on PYTHONPATH.

Real-format verification lives in Task 9's manual smoke against GCP.
"""
from __future__ import annotations

import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pyarrow.parquet as pq

from worker import postprocess


def _make_fake_simout(out_root: Path, listener_cols: dict[str, list[str]]) -> Path:
    """Build a fake out/manual/wildtype_*/.../simOut tree. Column files are
    empty placeholders — TableReader is mocked so contents don't matter."""
    simout = (out_root / "manual" / "wildtype_000000" / "000000"
              / "generation_000000" / "000000" / "simOut")
    for listener, cols in listener_cols.items():
        ldir = simout / listener
        ldir.mkdir(parents=True, exist_ok=True)
        (ldir / "attributes.json").write_text("{}")
        for col in cols:
            (ldir / col).write_bytes(b"")
    return simout


def _fake_table_reader_factory(data_by_column: dict[str, np.ndarray]):
    """Build a fake TableReader class. Constructed with a listener dir path;
    its readColumn(name) returns the canned numpy array for `name`."""
    def cls(path):
        reader = MagicMock(name=f"TableReader({path})")
        reader.readColumn = lambda name: data_by_column[name]
        return reader
    return cls


def test_make_tarball_includes_simout_tree(tmp_path):
    src = tmp_path / "manual"
    nested = src / "wildtype_000000" / "000000" / "generation_000000" / "000000" / "simOut" / "Mass"
    nested.mkdir(parents=True)
    (nested / "cellMass").write_bytes(b"\x00\x01\x02")
    dest = tmp_path / "out.tar.gz"

    postprocess.make_tarball(src, dest)

    with tarfile.open(dest, "r:gz") as tf:
        members = [m.name for m in tf.getmembers()]
    assert any("simOut/Mass/cellMass" in m for m in members)


def test_extract_timeseries_writes_long_format_parquet(tmp_path, monkeypatch):
    out_root = tmp_path / "out"
    listener_cols = {"Mass": ["cellMass", "dryMass"]}
    _make_fake_simout(out_root, listener_cols)

    data = {
        "cellMass": np.array([10.0, 11.0, 12.0]),
        "dryMass": np.array([3.0, 3.3, 3.6]),
    }
    monkeypatch.setattr(
        postprocess, "_table_reader_cls",
        lambda: _fake_table_reader_factory(data),
    )

    dest = tmp_path / "ts.parquet"
    postprocess.extract_timeseries(out_root, dest, listeners=listener_cols)

    df = pq.read_table(dest).to_pandas()
    assert set(df.columns) == {"timestep", "listener", "column", "value"}

    cellmass = df[(df["listener"] == "Mass") & (df["column"] == "cellMass")].sort_values("timestep")
    assert list(cellmass["timestep"]) == [0, 1, 2]
    assert list(cellmass["value"]) == [10.0, 11.0, 12.0]

    drymass = df[(df["listener"] == "Mass") & (df["column"] == "dryMass")].sort_values("timestep")
    assert list(drymass["value"]) == [3.0, 3.3, 3.6]


def test_extract_timeseries_raises_on_multiple_simout_dirs(tmp_path, monkeypatch):
    """Multi-generation runs would produce >1 simOut. Stage 2 doesn't
    handle a per-gen schema; fail loudly instead of silently mangling rows."""
    out_root = tmp_path / "out"
    _make_fake_simout(out_root, {"Mass": ["cellMass"]})
    # Add a second simOut under a different generation.
    second = (out_root / "manual" / "wildtype_000000" / "000000"
              / "generation_000001" / "000000" / "simOut" / "Mass")
    second.mkdir(parents=True)

    monkeypatch.setattr(
        postprocess, "_table_reader_cls",
        lambda: _fake_table_reader_factory({"cellMass": np.array([1.0])}),
    )

    import pytest
    with pytest.raises(RuntimeError, match="exactly one simOut"):
        postprocess.extract_timeseries(out_root, tmp_path / "ts.parquet")


def test_upload_run_artifacts_writes_three_keys_and_returns_uris(tmp_path):
    tar = tmp_path / "out.tar.gz"; tar.write_bytes(b"tarball-bytes")
    parquet = tmp_path / "ts.parquet"; parquet.write_bytes(b"parquet-bytes")

    client = MagicMock()
    bucket = MagicMock()
    client.bucket.return_value = bucket

    uris = postprocess.upload_run_artifacts(
        client, "wcm-ui-runs-dev", "abc-123",
        tar, parquet,
        {"simulation": {"length_sec": 30}},
    )

    client.bucket.assert_called_with("wcm-ui-runs-dev")
    keys = [call.args[0] for call in bucket.blob.call_args_list]
    assert "abc-123/output.tar.gz" in keys
    assert "abc-123/timeseries.parquet" in keys
    assert "abc-123/params.json" in keys

    assert uris == {
        "tarball": "gs://wcm-ui-runs-dev/abc-123/output.tar.gz",
        "parquet": "gs://wcm-ui-runs-dev/abc-123/timeseries.parquet",
        "params": "gs://wcm-ui-runs-dev/abc-123/params.json",
    }

    # params blob was uploaded with the dict's JSON encoding
    params_blob = None
    for call in bucket.blob.call_args_list:
        if call.args[0] == "abc-123/params.json":
            params_blob = bucket.blob.return_value
    assert params_blob.upload_from_string.called
    sent_json = params_blob.upload_from_string.call_args.args[0]
    assert json.loads(sent_json) == {"simulation": {"length_sec": 30}}


def test_upload_stderr_writes_run_id_stderr_log_and_returns_uri(tmp_path):
    stderr = tmp_path / "stderr.log"
    stderr.write_text("traceback...\n")

    client = MagicMock()
    bucket = MagicMock()
    client.bucket.return_value = bucket

    uri = postprocess.upload_stderr(client, "wcm-ui-runs-dev", "abc-123", stderr)

    assert uri == "gs://wcm-ui-runs-dev/abc-123/stderr.log"
    keys = [call.args[0] for call in bucket.blob.call_args_list]
    assert "abc-123/stderr.log" in keys
