"""Post-sim packaging: tarball the wcEcoli output tree, extract listener
columns to a long-format Parquet, upload everything to GCS.

Long-format Parquet schema:
    timestep:int64, listener:string, column:string, value:float64

Adding a listener column = one line in ``LISTENERS_TO_EXTRACT``. The
schema absorbs new listeners without churn.

Multi-generation runs (init_sims or generations > 1) produce more than
one ``simOut/`` directory; the per-generation schema isn't designed yet,
so ``extract_timeseries`` fails loudly when it sees that case.
"""
from __future__ import annotations

import glob
import json
import tarfile
from pathlib import Path
from typing import Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

LISTENERS_TO_EXTRACT: dict[str, list[str]] = {
    "Mass": ["cellMass", "dryMass", "proteinMass", "rnaMass"],
}


def _table_reader_cls():
    """Lazy import — wholecell.io.tablereader is only present inside the
    worker container (PYTHONPATH=/wcEcoli). Host-side tests mock this."""
    from wholecell.io.tablereader import TableReader  # type: ignore
    return TableReader


def make_tarball(src_dir: Path, dest: Path) -> Path:
    """Gzipped tar of ``src_dir``'s contents into ``dest``."""
    src_dir = Path(src_dir)
    dest = Path(dest)
    with tarfile.open(dest, mode="w:gz") as tf:
        tf.add(src_dir, arcname=src_dir.name)
    return dest


def _find_simout_dirs(out_root: Path) -> list[Path]:
    pattern = str(out_root / "manual" / "wildtype_*" / "*"
                  / "generation_*" / "*" / "simOut")
    return sorted(Path(p) for p in glob.glob(pattern))


def extract_timeseries(
    out_root: Path,
    dest_parquet: Path,
    listeners: dict[str, list[str]] = LISTENERS_TO_EXTRACT,
) -> Path:
    simouts = _find_simout_dirs(Path(out_root))
    if len(simouts) != 1:
        raise RuntimeError(
            f"Expected exactly one simOut directory under {out_root}, found "
            f"{len(simouts)}. Multi-generation runs need a per-generation "
            "schema (deferred)."
        )
    simout = simouts[0]
    TR = _table_reader_cls()

    timesteps: list[int] = []
    listener_col: list[str] = []
    column_col: list[str] = []
    values: list[float] = []

    for listener_name, columns in listeners.items():
        ldir = simout / listener_name
        if not ldir.is_dir():
            continue
        reader = TR(str(ldir))
        for col in columns:
            data = np.asarray(reader.readColumn(col)).ravel().astype(np.float64)
            for t, v in enumerate(data):
                timesteps.append(t)
                listener_col.append(listener_name)
                column_col.append(col)
                values.append(float(v))

    table = pa.table({
        "timestep": pa.array(timesteps, type=pa.int64()),
        "listener": pa.array(listener_col, type=pa.string()),
        "column": pa.array(column_col, type=pa.string()),
        "value": pa.array(values, type=pa.float64()),
    })
    pq.write_table(table, str(dest_parquet))
    return Path(dest_parquet)


def upload_run_artifacts(
    storage_client,
    bucket_name: str,
    run_id: str,
    tarball_path: Path,
    parquet_path: Path,
    params_dict: dict,
) -> dict[str, str]:
    """Upload three artifacts to ``gs://{bucket}/{run_id}/``. Returns a
    dict of GCS URIs keyed by 'tarball' / 'parquet' / 'params'."""
    bucket = storage_client.bucket(bucket_name)
    keys = {
        "tarball": f"{run_id}/output.tar.gz",
        "parquet": f"{run_id}/timeseries.parquet",
        "params":  f"{run_id}/params.json",
    }
    bucket.blob(keys["tarball"]).upload_from_filename(str(tarball_path))
    bucket.blob(keys["parquet"]).upload_from_filename(str(parquet_path))
    bucket.blob(keys["params"]).upload_from_string(
        json.dumps(params_dict, indent=2),
        content_type="application/json",
    )
    return {k: f"gs://{bucket_name}/{v}" for k, v in keys.items()}


def upload_stderr(
    storage_client,
    bucket_name: str,
    run_id: str,
    stderr_path: Path,
) -> str:
    key = f"{run_id}/stderr.log"
    storage_client.bucket(bucket_name).blob(key).upload_from_filename(str(stderr_path))
    return f"gs://{bucket_name}/{key}"
