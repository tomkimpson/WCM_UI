"""Dict-backed doubles for the GCP clients.

Why these exist rather than more MagicMocks: from Stage 3 onward the code does
read-modify-write inside transactions, and a MagicMock cannot tell you whether
a conditional write actually happened — `doc.update.assert_called()` passes
whether or not the guard worked. These fakes hold real state, so a test can
assert on the resulting document instead of on the call.

What they deliberately do NOT emulate: contention. `FakeTransaction` applies
writes immediately and serially, so it proves the read-modify-write *logic* and
nothing about concurrency. Firestore's actual locking is only observable against
the emulator — see tests/test_quota_concurrency.py.

`SERVER_TIMESTAMP` is resolved to an injectable `now` so timestamps are
assertable rather than opaque sentinels.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Optional

from google.cloud import firestore

# A fixed, obviously-synthetic instant. Tests that care about time pass their
# own; tests that don't get a stable value they can still assert on.
FAKE_NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


def _resolve_sentinels(payload: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Replace SERVER_TIMESTAMP sentinels with a concrete datetime."""
    out = {}
    for key, value in payload.items():
        if value is firestore.SERVER_TIMESTAMP:
            out[key] = now
        elif isinstance(value, dict):
            out[key] = _resolve_sentinels(value, now)
        else:
            out[key] = copy.deepcopy(value)
    return out


class FakeSnapshot:
    def __init__(self, doc_id: str, data: Optional[dict[str, Any]]):
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> Optional[dict[str, Any]]:
        return copy.deepcopy(self._data) if self._data is not None else None

    def get(self, field: str) -> Any:
        if self._data is None:
            raise KeyError(field)
        return copy.deepcopy(self._data[field])


class FakeDocumentRef:
    def __init__(self, store: "FakeFirestore", collection: str, doc_id: str):
        self._store = store
        self._collection = collection
        self.id = doc_id

    @property
    def _key(self) -> tuple[str, str]:
        return (self._collection, self.id)

    def get(self, transaction: Any = None) -> FakeSnapshot:
        # `transaction` is accepted and ignored: reads inside a fake
        # transaction see the same immediately-applied state.
        self._store.reads.append(self._key)
        return FakeSnapshot(self.id, self._store.data.get(self._key))

    def set(self, payload: dict[str, Any], merge: bool = False) -> None:
        resolved = _resolve_sentinels(payload, self._store.now)
        if merge and self._key in self._store.data:
            self._store.data[self._key].update(resolved)
        else:
            self._store.data[self._key] = resolved
        self._store.writes.append(("set", self._key, resolved))

    def update(self, payload: dict[str, Any]) -> None:
        if self._key not in self._store.data:
            # Mirrors the real client: .update() on a missing doc raises.
            # worker/db.py relies on this as the "submitter forgot to insert
            # the row" signal.
            from google.api_core.exceptions import NotFound
            raise NotFound(f"no document {self._collection}/{self.id}")
        resolved = _resolve_sentinels(payload, self._store.now)
        self._store.data[self._key].update(resolved)
        self._store.writes.append(("update", self._key, resolved))

    def delete(self) -> None:
        self._store.data.pop(self._key, None)
        self._store.writes.append(("delete", self._key, None))


class _FakeQuery:
    """Enough of the query surface for query_runs: where + order_by + limit."""

    def __init__(self, store: "FakeFirestore", collection: str):
        self._store = store
        self._collection = collection
        self._filters: list[tuple[str, str, Any]] = []
        self._order: Optional[tuple[str, str]] = None
        self._limit: Optional[int] = None

    def where(self, filter=None, **kwargs) -> "_FakeQuery":  # noqa: A002
        # The modern client takes a FieldFilter object; pull the parts back off.
        field = getattr(filter, "field_path", None)
        op = getattr(filter, "op_string", None)
        value = getattr(filter, "value", None)
        if field is None:
            raise TypeError("FakeFirestore.where expects a FieldFilter")
        self._filters.append((field, op, value))
        return self

    def order_by(self, field: str, direction: str = "ASCENDING") -> "_FakeQuery":
        self._order = (field, direction)
        return self

    def limit(self, n: int) -> "_FakeQuery":
        self._limit = n
        return self

    def stream(self) -> list[FakeSnapshot]:
        rows = [
            FakeSnapshot(doc_id, data)
            for (coll, doc_id), data in self._store.data.items()
            if coll == self._collection
        ]

        def matches(snap: FakeSnapshot) -> bool:
            d = snap.to_dict() or {}
            for field, op, value in self._filters:
                if field not in d:
                    return False
                actual = d[field]
                if op == "==" and not actual == value:
                    return False
                if op == "<" and not actual < value:
                    return False
                if op == "<=" and not actual <= value:
                    return False
                if op == ">" and not actual > value:
                    return False
                if op == ">=" and not actual >= value:
                    return False
                if op == "in" and actual not in value:
                    return False
            return True

        rows = [r for r in rows if matches(r)]
        if self._order:
            field, direction = self._order
            rows.sort(key=lambda s: (s.to_dict() or {}).get(field),
                      reverse=(direction == "DESCENDING"))
        if self._limit is not None:
            rows = rows[: self._limit]
        return rows


class FakeCollection:
    def __init__(self, store: "FakeFirestore", name: str):
        self._store = store
        self._name = name

    def document(self, doc_id: str) -> FakeDocumentRef:
        return FakeDocumentRef(self._store, self._name, doc_id)

    def where(self, filter=None, **kwargs) -> _FakeQuery:  # noqa: A002
        return _FakeQuery(self._store, self._name).where(filter, **kwargs)

    def order_by(self, field: str, direction: str = "ASCENDING") -> _FakeQuery:
        return _FakeQuery(self._store, self._name).order_by(field, direction)

    def stream(self) -> list[FakeSnapshot]:
        return _FakeQuery(self._store, self._name).stream()


class FakeTransaction:
    """Applies writes immediately. See the module docstring on contention.

    `applies_immediately` is read by worker.db._atomic to skip
    firestore.transactional. That decorator drives a begin/commit/retry
    protocol against library internals; emulating it here would test the
    emulation rather than our read-modify-write logic, and would break on
    every google-cloud-firestore upgrade.
    """

    applies_immediately = True

    def __init__(self, store: "FakeFirestore"):
        self._store = store
        self.committed = False

    def get(self, ref: FakeDocumentRef) -> FakeSnapshot:
        return ref.get()

    def set(self, ref: FakeDocumentRef, payload: dict[str, Any],
            merge: bool = False) -> None:
        ref.set(payload, merge=merge)
        self.committed = True

    def update(self, ref: FakeDocumentRef, payload: dict[str, Any]) -> None:
        ref.update(payload)
        self.committed = True

    def delete(self, ref: FakeDocumentRef) -> None:
        ref.delete()
        self.committed = True


class FakeFirestore:
    """A Firestore client backed by a plain dict keyed (collection, doc_id)."""

    def __init__(self, now: datetime = FAKE_NOW,
                 data: Optional[dict[tuple[str, str], dict[str, Any]]] = None):
        self.now = now
        self.data: dict[tuple[str, str], dict[str, Any]] = data or {}
        self.writes: list[tuple[str, tuple[str, str], Any]] = []
        self.reads: list[tuple[str, str]] = []

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self, name)

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    # -- test conveniences -------------------------------------------------
    def seed(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self.data[(collection, doc_id)] = copy.deepcopy(data)

    def doc(self, collection: str, doc_id: str) -> Optional[dict[str, Any]]:
        d = self.data.get((collection, doc_id))
        return copy.deepcopy(d) if d is not None else None

    def write_count(self, collection: Optional[str] = None) -> int:
        if collection is None:
            return len(self.writes)
        return sum(1 for _, (coll, _), _ in self.writes if coll == collection)
