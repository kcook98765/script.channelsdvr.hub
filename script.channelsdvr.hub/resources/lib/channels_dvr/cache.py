"""In-memory, query-scoped snapshots with explicit stale-on-failure views.

Only complete successful scans replace snapshots, including verified empty ones.
Failed/partial scans never merge, erase, or extend the lifetime of old data.
No persistence, clocks, callbacks, loading, or thread ownership are hidden here.
"""

from dataclasses import dataclass
from typing import Generic, TypeVar

from .normalization import DataResult, DataState
from .paging import Query, ScanResult

T = TypeVar("T")


@dataclass(frozen=True)
class CacheView(Generic[T]):
    items: tuple[T, ...]
    state: DataState
    stale: bool
    updated_at: int | None
    detail: str = ""


@dataclass(frozen=True)
class _Snapshot(Generic[T]):
    items: tuple[T, ...]
    updated_at: int
    expires_at: int


class SnapshotCache(Generic[T]):
    def __init__(self):
        self._snapshots: dict[Query, _Snapshot[T]] = {}
        self._failures: dict[Query, tuple[DataState, str]] = {}

    def get(self, query: Query, *, now: int) -> CacheView[T] | None:
        snapshot = self._snapshots.get(query)
        failure = self._failures.get(query)
        if snapshot is None and failure is None:
            return None
        items = snapshot.items if snapshot else ()
        state = DataState.READY if items else DataState.EMPTY
        if failure:
            state, detail = failure
        else:
            detail = ""
        return CacheView(items, state,
                         bool(failure) or snapshot is None or now >= snapshot.expires_at,
                         snapshot.updated_at if snapshot else None, detail)

    def update(self, query: Query, scan: DataResult[T] | ScanResult[T], *,
               now: int, ttl: int) -> CacheView[T]:
        if type(now) is not int or type(ttl) is not int or ttl < 0:
            raise ValueError("integer time and nonnegative integer TTL required")
        success = scan.complete and scan.state in (DataState.READY, DataState.EMPTY)
        valid_shape = isinstance(scan.items, tuple) and (
            (scan.state == DataState.READY and bool(scan.items))
            or (scan.state == DataState.EMPTY and not scan.items))
        if success and valid_shape:
            self._snapshots[query] = _Snapshot(scan.items, now, now + ttl)
            self._failures.pop(query, None)
        else:
            state = scan.state
            if state in (DataState.READY, DataState.EMPTY):
                state = DataState.PARTIAL if not scan.complete else DataState.MALFORMED
            self._failures[query] = (state, scan.detail or "scan did not complete")
        view = self.get(query, now=now)
        assert view is not None  # Both branches above establish cache evidence.
        return view
