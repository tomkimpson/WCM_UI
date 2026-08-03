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


def test_defaults_file_matches_schema_defaults(schema, defaults):
    """defaults.json must mirror the inline `default` declared in the schema.

    Catches drift between the two sources of truth (I-1) and, transitively,
    any knob missing from defaults.json (I-2) — a missing key raises KeyError
    naming the offender.
    """
    sim_props = schema["properties"]["simulation"]["properties"]
    sim_defaults = defaults["simulation"]
    for knob, prop in sim_props.items():
        assert knob in sim_defaults, (
            f"knob {knob!r} declared in schema but missing from defaults.json"
        )
        assert sim_defaults[knob] == prop["default"], (
            f"default for {knob!r} drifted: "
            f"schema={prop['default']!r} vs defaults.json={sim_defaults[knob]!r}"
        )


def test_multi_generation_knobs_are_clamped_to_one(schema):
    """`generations` and `init_sims` must not advertise values that always fail.

    postprocess.extract_timeseries raises unless exactly one simOut directory
    exists, so any run with either knob above 1 validates, burns a full
    simulation, and then dies in postprocessing. Advertising a maximum of 8 is
    a lie the schema shouldn't tell. Raise these only together with a
    per-generation Parquet schema (deferred to Stage 1c).
    """
    sim_props = schema["properties"]["simulation"]["properties"]
    for knob in ("generations", "init_sims"):
        assert sim_props[knob]["maximum"] == 1, (
            f"{knob} advertises maximum={sim_props[knob]['maximum']}, but "
            "extract_timeseries only handles a single simOut directory"
        )


def test_simulation_knobs_have_required_metadata(schema):
    """Every simulation knob must declare type, default, and description.

    Structural invariant — never needs editing when knobs are added or removed.
    """
    sim_props = schema["properties"]["simulation"]["properties"]
    required_keys = ("type", "default", "description")
    for knob, prop in sim_props.items():
        for key in required_keys:
            assert key in prop, (
                f"knob {knob!r} is missing required metadata key {key!r}"
            )
