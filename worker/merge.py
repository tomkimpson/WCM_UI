"""Recursive dict merge for parameter resolution.

Later (user) wins for scalars. Dicts merge recursively. Lists are
replaced wholesale (not extended); we don't ship list-valued knobs in
Stage 1b, but document the rule for future stages.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def merge_params(defaults: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any]:
    if not user:
        return deepcopy(defaults)
    result = deepcopy(defaults)
    _merge_into(result, user)
    return result


def _merge_into(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for key, value in src.items():
        if (
            key in dst
            and isinstance(dst[key], dict)
            and isinstance(value, dict)
        ):
            _merge_into(dst[key], value)
        else:
            dst[key] = deepcopy(value)
