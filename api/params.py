"""Turn user input into the parameter set a run will actually use.

    defaults ← curated form ← yaml_override        (later wins)

One rule governs this module: **nothing here raises on user input.** Every way
a human can get it wrong comes back as a ``FieldErrorData`` the form can render
beside the offending field. Exceptions are reserved for our own bugs.

Both ``POST /api/runs`` and ``POST /api/runs/validate`` call ``resolve_params``
identically, so the preview a user sees in the editor is produced by the same
code path that will submit the run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from worker.validate import FieldErrorData, field_errors

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "worker" / "schema"
DEFAULTS_PATH = SCHEMA_DIR / "defaults.json"
STRICT_SCHEMA_PATH = SCHEMA_DIR / "params.schema.json"
LOOSE_SCHEMA_PATH = SCHEMA_DIR / "params.loose.schema.json"

#: Namespaces the worker actually applies. `worker/run.py:build_commands`
#: reads only `resolved["simulation"]`, so anything else is accepted by the
#: loose schema and then silently discarded — a run that burns full compute
#: while ignoring the user's override, and whose content hash covers
#: parameters that had no effect. Reject rather than mislead. When Stage 1c
#: teaches build_commands about `wcecoli`, add it here.
SUPPORTED_NAMESPACES = frozenset({"simulation"})

#: Roughly the strict schema's nesting plus headroom for a `wcecoli` block.
#: JSON Schema cannot express recursion depth, so this is enforced in Python:
#: without it, a deeply nested override makes the recursive $ref in the loose
#: schema do exponential work.
MAX_DEPTH = 6

#: Matches the 64 KiB cap the request model declares, so a direct caller of
#: resolve_params gets the same limit as an HTTP caller.
MAX_YAML_BYTES = 65_536


@dataclass(frozen=True)
class ResolveResult:
    """Either a resolved parameter set or the reasons there isn't one.

    ``resolved`` is None whenever ``errors`` is non-empty — never a
    half-merged dict, so a caller that checks ``ok`` cannot accidentally
    submit something partially validated.
    """

    ok: bool
    resolved: Optional[dict[str, Any]]
    errors: tuple[FieldErrorData, ...]

    @classmethod
    def failure(cls, *errors: FieldErrorData) -> "ResolveResult":
        return cls(ok=False, resolved=None, errors=tuple(errors))

    @classmethod
    def success(cls, resolved: dict[str, Any]) -> "ResolveResult":
        return cls(ok=True, resolved=resolved, errors=())


def _error(kind: str, message: str, *, path: str = "<root>",
           pointer: str = "", line: Optional[int] = None,
           column: Optional[int] = None) -> FieldErrorData:
    return FieldErrorData(
        path=path, pointer=pointer, kind=kind, message=message,
        line=line, column=column,
    )


@lru_cache(maxsize=1)
def _defaults() -> dict[str, Any]:
    return json.loads(DEFAULTS_PATH.read_text())


def _depth(obj: Any, _level: int = 1) -> int:
    """Maximum nesting depth of dicts and lists."""
    if isinstance(obj, dict):
        return max((_depth(v, _level + 1) for v in obj.values()), default=_level)
    if isinstance(obj, list):
        return max((_depth(v, _level + 1) for v in obj), default=_level)
    return _level


def _parse_yaml(text: str) -> tuple[Optional[dict[str, Any]],
                                    Optional[FieldErrorData]]:
    """Parse the override into a mapping, or explain why it isn't one."""
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        return None, _error(
            "too_large",
            f"the override is larger than {MAX_YAML_BYTES} bytes",
        )
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or str(exc)
        return None, _error(
            "yaml_syntax",
            f"could not parse the override: {problem}",
            # yaml reports 0-based; editors count from 1.
            line=(mark.line + 1) if mark is not None else None,
            column=(mark.column + 1) if mark is not None else None,
        )

    if loaded is None:
        # Empty, whitespace, or comments only. A blank editor is not an error.
        return {}, None
    if not isinstance(loaded, dict):
        return None, _error(
            "not_an_object",
            "the override must be a mapping of parameter names to values, "
            f"not a {type(loaded).__name__}",
        )
    return loaded, None


def resolve_params(
    params: Any,
    yaml_override: Optional[str] = None,
) -> ResolveResult:
    """Merge and validate. Never raises on user input.

    Order matters: parse the override before merging (so a syntax error is
    reported on its own rather than buried under schema noise), then check
    structural limits, then the namespace guard, then the schema. Each stage
    short-circuits, because a later stage's errors would be misleading noise if
    an earlier one already failed.
    """
    if not isinstance(params, dict):
        return ResolveResult.failure(_error(
            "not_an_object",
            f"parameters must be an object, not a {type(params).__name__}",
        ))

    override: dict[str, Any] = {}
    if yaml_override is not None:
        override, err = _parse_yaml(yaml_override)
        if err is not None:
            return ResolveResult.failure(err)

    # Two-stage merge gives exactly `defaults ← form ← yaml`. merge_params
    # deep-copies, so the caller's dicts are never mutated.
    from worker.merge import merge_params
    merged = merge_params(merge_params(_defaults(), params), override)

    if _depth(merged) > MAX_DEPTH:
        return ResolveResult.failure(_error(
            "too_deep",
            f"parameters are nested more than {MAX_DEPTH} levels deep",
        ))

    unsupported = sorted(set(merged) - SUPPORTED_NAMESPACES)
    if unsupported:
        applied = ", ".join(sorted(SUPPORTED_NAMESPACES))
        return ResolveResult.failure(*[
            _error(
                "unsupported_namespace",
                f"{name!r} is a recognised shape but this release's worker "
                f"does not apply it — only {applied} takes effect. Remove it, "
                f"or the run would silently ignore your override.",
                path=name,
                pointer=f"/{name}",
            )
            for name in unsupported
        ])

    errors = field_errors(merged, schema_path=LOOSE_SCHEMA_PATH)
    if errors:
        return ResolveResult.failure(*errors)

    return ResolveResult.success(merged)
