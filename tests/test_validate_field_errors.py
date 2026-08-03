"""All-errors validation for form-backing callers.

`validate_params` raises on the first problem only, which is right for a CLI.
A form needs every error at once, each anchored at a pointer the UI can
highlight — so `field_errors` returns a list instead of raising.

The interesting case is `additionalProperties`: jsonschema reports ONE error
anchored at the parent object, with the offending key buried in prose
("Additional properties are not allowed ('galaxy' was unexpected)"). Parsing
that string would be brittle, so `field_errors` expands it structurally into
one error per unexpected key. Those tests are the reason this module exists.
"""
from __future__ import annotations

from worker.validate import field_errors


def test_valid_params_yield_no_errors():
    assert field_errors({"simulation": {"length_sec": 120}}) == []


def test_two_bad_values_yield_two_errors_sorted_by_path():
    errs = field_errors({"simulation": {"length_sec": 999999, "seed": -1}})
    assert [e.path for e in errs] == ["simulation.length_sec", "simulation.seed"]
    assert all(e.kind == "invalid_value" for e in errs)


def test_error_carries_an_rfc6901_pointer():
    (err,) = field_errors({"simulation": {"length_sec": 999999}})
    assert err.pointer == "/simulation/length_sec"


def test_error_carries_the_failing_keyword_and_constraint():
    """The UI needs the constraint to say "must be at most 86400"."""
    (err,) = field_errors({"simulation": {"length_sec": 999999}})
    assert err.keyword == "maximum"
    assert err.constraint == 86400


def test_wrong_type_is_reported_as_invalid_type():
    (err,) = field_errors({"simulation": {"length_sec": "sixty"}})
    assert err.kind == "invalid_type"
    assert err.keyword == "type"


def test_two_unknown_keys_in_one_object_yield_one_error_each():
    """This is what validate_params structurally cannot do.

    jsonschema emits a single additionalProperties error for the whole object,
    so without expansion a form could only ever highlight one typo at a time.
    """
    errs = field_errors({"simulation": {"galaxy": 1, "nebula": 2}})
    assert [e.path for e in errs] == ["simulation.galaxy", "simulation.nebula"]
    assert all(e.kind == "unknown_field" for e in errs)
    assert [e.pointer for e in errs] == ["/simulation/galaxy", "/simulation/nebula"]


def test_unknown_key_error_names_the_key_not_the_parent():
    (err,) = field_errors({"galaxy": {}})
    assert err.path == "galaxy"
    assert "galaxy" in err.message


def test_unknown_and_invalid_errors_are_both_reported():
    """A form submit with one typo and one bad value must surface both."""
    errs = field_errors({"simulation": {"galaxy": 1, "seed": -1}})
    assert {e.path for e in errs} == {"simulation.galaxy", "simulation.seed"}


def test_root_level_non_object_is_reported_at_root():
    (err,) = field_errors([1, 2, 3])
    assert err.path == "<root>"
    assert err.kind == "invalid_type"


def test_validator_is_cached_across_calls():
    """The schema was being re-read and re-compiled on every single call."""
    from worker.validate import SCHEMA_PATH, _validator
    assert _validator(str(SCHEMA_PATH)) is _validator(str(SCHEMA_PATH))


def test_paths_with_array_indices_do_not_raise_on_sort():
    """absolute_path is a deque mixing str keys and int indices.

    Sorting the raw deques raises TypeError once arrays enter the schema. The
    loose schema (Stage 3) allows arrays, so pin the coercion here rather than
    discovering it later.
    """
    schema = {
        "type": "object",
        "properties": {
            "xs": {"type": "array", "items": {"type": "integer"}},
        },
    }
    errs = field_errors({"xs": ["a", "b"]}, schema=schema)
    assert [e.path for e in errs] == ["xs.0", "xs.1"]
    assert [e.pointer for e in errs] == ["/xs/0", "/xs/1"]


def test_validate_params_still_raises_on_the_first_error_only():
    """Sharing a code path must not turn validate_params into a list-returner.

    Its message does improve: it now names the offending key
    ("simulation.galaxy") where before it could only name the parent object
    ("simulation"), because both entry points share the structural expansion.
    """
    import pytest

    from worker.validate import ValidationError, validate_params

    with pytest.raises(ValidationError, match=r"unknown field at simulation\.galaxy"):
        validate_params({"simulation": {"galaxy": 1, "nebula": 2}})
