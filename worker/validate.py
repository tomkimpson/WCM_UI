"""Validate user-supplied params against the Stage 1b schema.

Raises a single ValidationError on the first problem, with a message that
names the offending field. We don't try to collect all errors — Stage 1b
users are scripts and CI, not humans typing into a form. The frontend
(Stage 4) will aggregate errors itself.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

SCHEMA_PATH = Path(__file__).parent / "schema" / "params.schema.json"


class ValidationError(ValueError):
    """Raised when user params don't match the schema."""


def _load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def validate_params(params: dict[str, Any]) -> None:
    schema = _load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(params), key=lambda e: e.absolute_path)
    if not errors:
        return
    first = errors[0]
    path = ".".join(str(p) for p in first.absolute_path) or "<root>"
    if first.validator == "additionalProperties":
        raise ValidationError(f"unknown field at {path}: {first.message}")
    raise ValidationError(f"invalid value at {path}: {first.message}")
