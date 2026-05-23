import pytest

from worker.validate import ValidationError, validate_params


def test_valid_params_pass():
    validate_params({"simulation": {"length_sec": 120}})


def test_unknown_top_level_key_rejected():
    with pytest.raises(ValidationError, match="unknown"):
        validate_params({"galaxy": {}})


def test_out_of_range_rejected():
    with pytest.raises(ValidationError, match="length_sec"):
        validate_params({"simulation": {"length_sec": 999999}})


def test_wrong_type_rejected():
    with pytest.raises(ValidationError, match="length_sec"):
        validate_params({"simulation": {"length_sec": "sixty"}})


def test_error_includes_jsonpath():
    """The error message should name the offending field so users can fix it."""
    with pytest.raises(ValidationError) as exc_info:
        validate_params({"simulation": {"seed": -1}})
    assert "seed" in str(exc_info.value)


def test_validation_error_is_value_error():
    """Callers may catch ValueError to unify with json.JSONDecodeError."""
    assert issubclass(ValidationError, ValueError)
