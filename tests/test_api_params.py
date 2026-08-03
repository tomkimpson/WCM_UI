"""Resolve user input into the parameter set a run will actually use.

Merge order is `defaults ← form ← yaml_override` (later wins), which is the
contract the design doc specifies and the reason the two-stage merge exists.

The rule that shapes this module: resolve_params NEVER raises on user input.
Every way a human can get it wrong — malformed YAML, a scalar where an object
belongs, a knob out of range, a namespace the worker ignores — comes back as a
FieldErrorData the form can render next to the offending input.
"""
from __future__ import annotations

from api.params import MAX_YAML_BYTES, SUPPORTED_NAMESPACES, resolve_params


# --- merge order ----------------------------------------------------------

def test_no_input_resolves_to_the_defaults():
    result = resolve_params({})
    assert result.ok
    assert result.resolved["simulation"]["length_sec"] == 60
    assert result.errors == ()


def test_form_overrides_the_defaults():
    result = resolve_params({"simulation": {"length_sec": 120}})
    assert result.ok
    assert result.resolved["simulation"]["length_sec"] == 120
    # Untouched knobs still come from defaults.
    assert result.resolved["simulation"]["seed"] == 0


def test_yaml_override_beats_the_form():
    """The whole point of the override layer: it is the last word."""
    result = resolve_params(
        {"simulation": {"length_sec": 120}},
        yaml_override="simulation:\n  length_sec: 300\n",
    )
    assert result.ok
    assert result.resolved["simulation"]["length_sec"] == 300


def test_yaml_override_merges_rather_than_replaces():
    result = resolve_params(
        {"simulation": {"length_sec": 120, "seed": 7}},
        yaml_override="simulation:\n  length_sec: 300\n",
    )
    assert result.ok
    assert result.resolved["simulation"]["seed"] == 7, "seed came from the form"
    assert result.resolved["simulation"]["length_sec"] == 300


def test_resolving_does_not_mutate_the_caller_s_input():
    submitted = {"simulation": {"length_sec": 120}}
    resolve_params(submitted, yaml_override="simulation:\n  length_sec: 300\n")
    assert submitted == {"simulation": {"length_sec": 120}}


# --- YAML failure modes ---------------------------------------------------

def test_malformed_yaml_reports_a_syntax_error_with_a_position():
    result = resolve_params({}, yaml_override="simulation:\n  - [unclosed\n")
    assert not result.ok
    assert result.resolved is None
    (err,) = result.errors
    assert err.kind == "yaml_syntax"
    assert err.line is not None, "the editor needs somewhere to put the marker"


def test_yaml_scalar_is_rejected_as_not_an_object():
    result = resolve_params({}, yaml_override="just a string")
    assert not result.ok
    (err,) = result.errors
    assert err.kind == "not_an_object"


def test_yaml_list_is_rejected_as_not_an_object():
    result = resolve_params({}, yaml_override="- a\n- b\n")
    assert not result.ok
    assert result.errors[0].kind == "not_an_object"


def test_empty_yaml_is_treated_as_no_override():
    """A blank editor must not be an error."""
    for blank in ("", "   \n", "# just a comment\n"):
        result = resolve_params({"simulation": {"seed": 3}}, yaml_override=blank)
        assert result.ok, f"blank override {blank!r} should be accepted"
        assert result.resolved["simulation"]["seed"] == 3


def test_oversized_yaml_is_rejected_without_parsing():
    result = resolve_params({}, yaml_override="a: 1\n" * MAX_YAML_BYTES)
    assert not result.ok
    assert result.errors[0].kind == "too_large"


def test_deeply_nested_yaml_is_rejected():
    """Depth is inexpressible in JSON Schema, so it's enforced in Python."""
    deep = {}
    node = deep
    for _ in range(40):
        node["wcecoli"] = {}
        node = node["wcecoli"]
    result = resolve_params(deep)
    assert not result.ok
    assert any(e.kind == "too_deep" for e in result.errors)


def test_non_dict_form_params_are_rejected():
    result = resolve_params([1, 2, 3])
    assert not result.ok
    assert result.errors[0].kind == "not_an_object"


# --- the namespace guard --------------------------------------------------

def test_wcecoli_namespace_is_rejected_while_the_worker_ignores_it():
    """The loose schema type-checks `wcecoli`, but build_commands reads only
    `simulation`. Accepting it would produce a run that silently discards the
    user's override, burns the compute, and records a content hash covering
    parameters that had no effect."""
    result = resolve_params({}, yaml_override="wcecoli:\n  foo: 1\n")
    assert not result.ok
    (err,) = result.errors
    assert err.kind == "unsupported_namespace"
    assert "wcecoli" in err.message
    assert "simulation" in err.message, "say which namespaces DO take effect"


def test_only_simulation_is_supported_today():
    assert SUPPORTED_NAMESPACES == frozenset({"simulation"})


# --- schema validation ----------------------------------------------------

def test_out_of_range_value_is_reported_not_raised():
    result = resolve_params({"simulation": {"length_sec": 999999}})
    assert not result.ok
    assert result.resolved is None
    (err,) = result.errors
    assert err.path == "simulation.length_sec"
    assert err.keyword == "maximum"


def test_two_bad_values_are_both_reported():
    result = resolve_params({"simulation": {"length_sec": 0, "seed": -1}})
    assert not result.ok
    assert {e.path for e in result.errors} == {
        "simulation.length_sec", "simulation.seed",
    }


def test_unknown_knob_is_reported_at_its_own_pointer():
    result = resolve_params({"simulation": {"warp_factor": 9}})
    assert not result.ok
    (err,) = result.errors
    assert err.path == "simulation.warp_factor"
    assert err.kind == "unknown_field"


def test_clamped_generations_are_enforced_through_the_loose_schema():
    """The loose schema must not become a way around the strict limits."""
    result = resolve_params({}, yaml_override="simulation:\n  generations: 4\n")
    assert not result.ok
    assert result.errors[0].path == "simulation.generations"


def test_resolved_is_none_whenever_there_are_errors():
    """Callers branch on `ok`; `resolved` must never be half-built."""
    for bad in (
        {"simulation": {"length_sec": -5}},
        {"simulation": {"nope": 1}},
    ):
        result = resolve_params(bad)
        assert not result.ok and result.resolved is None
