"""Pure, manually driven MOCK presentations of complete synthetic catalogs.

Guide snapshots cover every channel over the captured three-day fixture horizon.
They may be filtered locally by group/time; these are not network-window caches.
No rendering call advances time, retries a failure, or reads rebound GUI data.
"""

from dataclasses import dataclass
from typing import Generic, TypeVar

from .cache import SnapshotCache
from .models import Event, Job, Recording
from .normalization import DataResult, DataState
from .paging import Query
from .repository import MockRepository
from .ui_text import FALLBACK, text
from .ui_status import status_banner

T = TypeVar("T")
_TTL = 300


@dataclass(frozen=True)
class Presentation(Generic[T]):
    items: tuple[T, ...]
    state: DataState | None
    loading: bool
    stale: bool
    updated_at: int | None
    age_seconds: int | None
    banner: str
    message: str
    can_browse: bool


class OfflineDataSession:
    SCENARIOS = (
        ('healthy', FALLBACK['demo_recover']), ('loading', FALLBACK['demo_loading']),
        ('outage', FALLBACK['demo_outage']), ('malformed', FALLBACK['demo_malformed']),
        ('unsupported', FALLBACK['demo_unsupported']), ('partial', FALLBACK['demo_partial']),
        ('expired', FALLBACK['demo_expired']), ('empty', FALLBACK['demo_empty']),
        ('cold_outage', FALLBACK['demo_cold_outage']),
    )

    def __init__(self, repository: MockRepository, fixture_id: str = "normal", *, strings=FALLBACK):
        self.strings = strings
        self._events = tuple(repository.events)
        self._recordings = tuple(repository.recordings)
        self._jobs = tuple(repository.jobs)
        self._now = repository.now
        start, end = repository.horizon
        scope = (("namespace", "synthetic"), ("fixture_id", fixture_id),
                 ("horizon_start", str(start)), ("horizon_end", str(end)),
                 ("catalog", "complete-all-channels"))
        self._queries = {name: Query(name, scope)
                         for name in ("events", "recordings", "jobs")}
        self._scenario = "healthy"
        self._revision = 0
        self._applied: dict[str, int] = {}
        self._reset_caches()

    def _reset_caches(self) -> None:
        self._guide_cache: SnapshotCache[Event] = SnapshotCache()
        self._recordings_cache: SnapshotCache[Recording] = SnapshotCache()
        self._jobs_cache: SnapshotCache[Job] = SnapshotCache()
        self._applied.clear()

    @property
    def scenario(self) -> str:
        return self._scenario

    def scenario_entries(self):
        return tuple((code, text(self.strings, 'demo_recover' if code == 'healthy' else 'demo_' + code))
                     for code, _ in self.SCENARIOS)

    def choose(self, code: str) -> None:
        if code not in tuple(code for code, _ in self.SCENARIOS):
            raise ValueError("unknown mock scenario")
        self._now += 60 + (_TTL + 1 if code == "expired" else 0)
        self._revision += 1
        self._scenario = code
        if code == "cold_outage":
            self._reset_caches()

    def guide(self) -> Presentation[Event]:
        return self._present(self._guide_cache, self._queries["events"], self._events)

    def recordings(self) -> Presentation[Recording]:
        return self._present(self._recordings_cache, self._queries["recordings"],
                             self._recordings)

    def jobs(self) -> Presentation[Job]:
        return self._present(self._jobs_cache, self._queries["jobs"], self._jobs)

    def _present(self, cache: SnapshotCache[T], query: Query,
                 captured: tuple[T, ...]) -> Presentation[T]:
        scenario = self._scenario
        if self._applied.get(query.resource) != self._revision:
            result: DataResult[T] | None = None
            if scenario == "healthy":
                result = DataResult(captured, DataState.READY if captured else DataState.EMPTY)
            elif scenario == "empty":
                result = DataResult((), DataState.EMPTY)
            elif scenario == "partial":
                result = DataResult(captured[:1], DataState.PARTIAL)
            elif scenario == "malformed":
                result = DataResult((), DataState.MALFORMED, "mock schema mismatch")
            elif scenario == "unsupported":
                result = DataResult((), DataState.UNSUPPORTED)
            elif scenario in ("outage", "cold_outage"):
                result = DataResult((), DataState.OUTAGE)
            # Loading and expiry observe only: never create a snapshot or failure.
            if result is not None:
                cache.update(query, result, now=self._now, ttl=_TTL)
            self._applied[query.resource] = self._revision

        view = cache.get(query, now=self._now)
        items = view.items if view else ()
        updated_at = view.updated_at if view else None
        age = None if updated_at is None else self._now - updated_at
        loading = scenario == "loading"
        state = None if loading or view is None else view.state
        stale = view.stale if view else False
        banner = status_banner(state, loading, updated_at, age, stale, self.strings, demo=True)
        # Never interpolate CacheView.detail, query metadata, or fixture content.
        return Presentation(items, state, loading, stale, updated_at, age, banner,
                             text(self.strings, 'status_message', status=banner,
                                  message=text(self.strings, 'demo_' + scenario + '_message')),
                            updated_at is not None and bool(items))
