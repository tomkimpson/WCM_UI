"""Validate user-supplied params against the curated schema.

Two entry points over one code path:

  - ``validate_params`` raises a single ``ValidationError`` on the first
    problem. This is the right shape for a CLI or the worker entrypoint, where
    the caller is a script and the first error is enough to abort.
  - ``field_errors`` returns *every* error, each anchored at an RFC 6901
    pointer. This is what a form needs: reporting one error at a time turns
    fixing a submission into a guessing game.

Both go through ``_iter_field_errors``, so a message improvement or a new error
kind lands in both at once.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Optional

import jsonschema

SCHEMA_PATH = Path(__file__).parent / "schema" / "params.schema.json"


class ValidationError(ValueError):
    """Raised when user params don't match the schema."""


@dataclass(frozen=True)
class FieldErrorData:
    """One validation problem, addressed at a single field.

    A plain dataclass rather than a pydantic model on purpose: worker/ is
    imported by the container, which has no pydantic. api/models.py converts
    these into the wire format.
    """

    path: str          # dotted, for humans: "simulation.length_sec"
    pointer: str       # RFC 6901, for form libraries: "/simulation/length_sec"
    kind: str          # unknown_field | invalid_type | invalid_value | …
    message: str
    keyword: Optional[str] = None   # the jsonschema validator that failed
    constraint: Any = None          # the schema value it failed against

    # Only set for errors that come from parsing text rather than validating a
    # structure — today that means a YAML syntax error in the override editor.
    # Declared here rather than in api/ because this dataclass is the single
    # error carrier both layers pass around, and two optional ints are cheaper
    # than a parallel type.
    line: Optional[int] = None
    column: Optional[int] = None


def _load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


@lru_cache(maxsize=8)
def _validator(schema_path: str) -> jsonschema.Draft202012Validator:
    """Cached validator for a schema file.

    Both the file read and the validator construction used to happen on every
    single call. That is invisible in a CLI and wasteful in a request handler,
    which is the caller this module is about to acquire. Keyed on the path
    string so the strict and loose schemas get separate entries.
    """
    return jsonschema.Draft202012Validator(json.loads(Path(schema_path).read_text()))


def _pointer(parts: tuple[str, ...]) -> str:
    # RFC 6901: '~' and '/' inside a token are escaped, and the root document
    # is the empty string rather than "/".
    escaped = (p.replace("~", "~0").replace("/", "~1") for p in parts)
    return "".join(f"/{p}" for p in escaped)


def _kind_for(err: jsonschema.ValidationError) -> str:
    if err.validator == "type":
        return "invalid_type"
    return "invalid_value"


def _expand(err: jsonschema.ValidationError) -> Iterator[FieldErrorData]:
    """Turn one jsonschema error into one error per addressable field.

    Only `additionalProperties` needs expanding. jsonschema reports it once,
    anchored at the *parent* object, with the offending key mentioned only in
    the prose message ("Additional properties are not allowed ('galaxy' was
    unexpected)"). Reading the key back out of that string would be brittle, so
    recover it structurally from the schema and the instance instead — which
    also means a form can highlight every typo at once rather than one per
    round trip.
    """
    base = tuple(str(p) for p in err.absolute_path)

    if err.validator == "additionalProperties" and isinstance(err.instance, dict):
        allowed = set(err.schema.get("properties", {}))
        # patternProperties would also legitimise a key. Our schemas don't use
        # it; if one ever does, unknown keys here would be over-reported.
        unexpected = sorted(k for k in err.instance if k not in allowed)
        if unexpected:
            for key in unexpected:
                parts = base + (str(key),)
                yield FieldErrorData(
                    path=".".join(parts),
                    pointer=_pointer(parts),
                    kind="unknown_field",
                    message=f"unknown field {key!r}",
                    keyword="additionalProperties",
                    constraint=sorted(allowed),
                )
            return
        # Fall through if we couldn't attribute it to a key — better a
        # parent-anchored error than none at all.

    yield FieldErrorData(
        path=".".join(base) or "<root>",
        pointer=_pointer(base),
        kind=_kind_for(err),
        message=err.message,
        keyword=str(err.validator) if err.validator is not None else None,
        constraint=err.validator_value,
    )


def _iter_field_errors(
    params: Any,
    *,
    schema: Optional[dict[str, Any]] = None,
    schema_path: Path = SCHEMA_PATH,
) -> list[FieldErrorData]:
    if schema is not None:
        validator = jsonschema.Draft202012Validator(schema)
    else:
        validator = _validator(str(schema_path))

    # Sort by the STRINGIFIED path. absolute_path is a deque mixing str keys
    # and int array indices, and sorting raw deques raises TypeError as soon as
    # arrays appear in a schema — which the loose override schema allows.
    # Side effect worth knowing: index 10 sorts before index 2. Stable and
    # harmless for display; don't rely on this order to be numeric.
    raw = sorted(validator.iter_errors(params),
                 key=lambda e: tuple(str(p) for p in e.absolute_path))

    out: list[FieldErrorData] = []
    for err in raw:
        out.extend(_expand(err))
    return out


def field_errors(
    params: Any,
    *,
    schema: Optional[dict[str, Any]] = None,
    schema_path: Path = SCHEMA_PATH,
) -> list[FieldErrorData]:
    """Every validation problem, for callers that render a form.

    Pass `schema` to validate against an inline schema (uncached), or
    `schema_path` to use a cached file-backed one.
    """
    return _iter_field_errors(params, schema=schema, schema_path=schema_path)


def validate_params(params: dict[str, Any]) -> None:
    """Raise on the first problem. Returns None when the params are clean."""
    errors = _iter_field_errors(params)
    if not errors:
        return
    first = errors[0]
    if first.kind == "unknown_field":
        raise ValidationError(f"unknown field at {first.path}: {first.message}")
    raise ValidationError(f"invalid value at {first.path}: {first.message}")
