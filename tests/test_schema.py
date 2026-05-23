"""Schema is self-consistent and the defaults satisfy it."""
import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = Path(__file__).parent.parent / "worker" / "schema" / "params.schema.json"
DEFAULTS_PATH = Path(__file__).parent.parent / "worker" / "schema" / "defaults.json"


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture(scope="module")
def defaults():
    return json.loads(DEFAULTS_PATH.read_text())


def test_schema_is_valid_jsonschema(schema):
    """The schema document itself must be a valid JSON Schema."""
    jsonschema.Draft202012Validator.check_schema(schema)


def test_defaults_satisfy_schema(schema, defaults):
    """defaults.json must validate cleanly against params.schema.json."""
    jsonschema.validate(instance=defaults, schema=schema)


def test_schema_declares_simulation_knobs(schema):
    """Sanity: the five Stage 1b knobs are present."""
    sim_props = schema["properties"]["simulation"]["properties"]
    expected = {"length_sec", "seed", "generations", "init_sims", "parca_cpus"}
    assert expected.issubset(sim_props.keys()), (
        f"missing: {expected - sim_props.keys()}"
    )
