"""Pure owner-side development metadata state; callers own dispatch and I/O.

One session belongs to one logical server identity. Replace it on origin changes.
Only complete snapshots enter repositories; failures retain the original cache
timestamp. Guide caches describe exact requested windows, never universal coverage.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
import re
import time
from typing import Callable, TYPE_CHECKING, TypeVar

from .cache import CacheView, SnapshotCache
from .guide_projection import ChannelProjection
from .guide_index import IndexInput, IndexProduct, matches_input
from .metadata_http import _CODES
from .metadata_models import MetadataBatch
from .metadata_repository import _SAFE_WARNINGS
from .models import Channel, Event, Job, Recording
from .normalization import DataResult, DataState
from .paging import Query
from .repository import MockRepository
from .session import Presentation
from .ui_text import FALLBACK, text
from .ui_status import listing_warning_details, status_banner
from .newbuild_compatibility import version_admitted
from .artwork import ArtInventory
from .dashboard_models import ResourceSummary

if TYPE_CHECKING:
    from .live_loader import LoadResult

T = TypeVar("T")
_SUCCESS = (DataState.READY, DataState.EMPTY)
_ERROR_CODES = _CODES | {"worker_error"}
_EXPLANATIONS = {state: 'live_' + state.value + '_message' for state in DataState}


def currentUTCseconds() -> int:
    return int(time.time())


def _integer(value: int, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


class DevelopmentRepository(MockRepository):
    """Repository-compatible data holder; never constructs mock fixtures."""

    def __init__(self, now: int, horizon: tuple[int, int]) -> None:
        if (not _integer(now) or not isinstance(horizon, tuple) or len(horizon) != 2
                or not all(_integer(t) for t in horizon)
                or not horizon[0] <= now < horizon[1]):
            raise ValueError("invalid_repository_scope")
        self.now = now
        self.horizon = horizon
        self.channels = ()
        self.events = ()
        self.recordings = ()
        self.jobs = ()
        self.groups = ("All", "Visible", "Favorites", "Guide station unqualified",
                       "Guide station missing")


@dataclass
class _Window:
    query: Query
    cache: SnapshotCache[Event] = field(default_factory=SnapshotCache)
    # Accepted warning metadata travels with its exact window, including EMPTY.
    warnings: tuple[str, ...] = ()
    origin: "GuideOrigin | None" = None
    summary: ResourceSummary | None = None


@dataclass(frozen=True, eq=False, slots=True)
class GuideOrigin:
    """Coherent positive cache update; neither freshness nor action permission.

    Stored with each bounded event window. Receipt identity and catalog_epoch must
    remain session-owned: independently replacing channels never requalifies an
    older event tuple, even when tuples/timestamps happen to be identical.
    """

    owner: object
    ticket: object
    catalog_epoch: object
    metadata_generation: int
    namespace: str
    version: str
    horizon: tuple[int, int]
    fetched_at: int
    events: tuple[Event, ...]
    channels: tuple[Channel, ...]


class LiveDataSession:
    def __init__(self, namespace: str, expected_version: str, *,
                 clock: Callable[[], int] = currentUTCseconds, ttl: int = 300,
                 window_limit: int = 4, strings=FALLBACK) -> None:
        if (type(namespace) is not str
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", namespace)
                or type(expected_version) is not str
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", expected_version)
                or not callable(clock) or not _integer(ttl)
                or not _integer(window_limit, 1) or window_limit > 8):
            raise ValueError("invalid_session_argument")
        now = clock()
        if not _integer(now):
            raise ValueError("invalid_clock")
        self._namespace = namespace
        self._minimum = expected_version
        self._version: str | None = None
        self._version_blocked = False
        self._clock = clock
        self._ttl = ttl
        self._window_limit = window_limit
        self.strings = strings
        self._repository = DevelopmentRepository(now, (now, now + 7200))
        self._scope = (("namespace", namespace), ("version", 'unverified'))
        self._channels_query = Query("channels", self._scope)
        self._recordings_query = Query("recordings", self._scope)
        self._jobs_query = Query("jobs", self._scope)
        self._channels: SnapshotCache[Channel] = SnapshotCache()
        self._recordings: SnapshotCache[Recording] = SnapshotCache()
        self._jobs: SnapshotCache[Job] = SnapshotCache()
        self._windows: OrderedDict[tuple[int, int], _Window] = OrderedDict()
        self._generation: int | None = None
        self._horizon: tuple[int, int] | None = None
        self._guide_windows = 4
        self._loading = False
        self._closed = False
        self._last_error: str | None = None
        self._last_request_count = 0
        self._snapshot_warning_codes: tuple[str, ...] = ()
        self._channel_projection = ChannelProjection(strings)
        self._incarnation = object()
        self._catalog_epoch = object()
        self._index_ticket = object()
        self.artwork = ArtInventory()
        self.summaries: dict[str, ResourceSummary] = {}

    @property
    def incarnation(self) -> object:
        return self._incarnation

    @property
    def actual_version(self) -> str | None:
        return self._version

    @property
    def guide_origin(self) -> GuideOrigin | None:
        """O(1) current coherent origin, including explicit accepted EMPTY.

        Loading, stale and failure presentation remain live caller inputs. This
        does not refresh cache lifetime or grant browse/record/pass authority.
        """
        if self._closed or self._version_blocked or self._horizon is None:
            return None
        window = self._windows[self._horizon]
        origin = window.origin
        if origin is None or origin.catalog_epoch is not self._catalog_epoch:
            return None
        now = self._clock()
        channels = self._channels.get(self._channels_query, now=now)
        events = window.cache.get(window.query, now=now)
        if (channels is None or events is None or channels.items is not origin.channels
                or events.items is not origin.events or events.updated_at != origin.fetched_at
                or self._repository.channels is not origin.channels
                or self._repository.events is not origin.events):
            return None
        return origin

    def accepts_index_origin(self, product: object) -> bool:
        """Positive-origin binding only; coordinator/context/action gates still required."""
        source = getattr(product, "source", None)
        if (type(product) is not IndexProduct or type(source) is not IndexInput
                or not matches_input(product, source)):
            return False
        origin = self.guide_origin
        return (origin is not None and source.owner is origin.owner
                and source.ticket is origin.ticket
                and source.metadata_generation == origin.metadata_generation
                and source.events is origin.events and source.channels is origin.channels
                and source.namespace == origin.namespace and source.version == origin.version
                and source.horizon == origin.horizon and source.fetched_at == origin.fetched_at)

    @property
    def repository(self) -> DevelopmentRepository:
        return self._repository

    @property
    def channel_projection(self) -> ChannelProjection:
        return self._channel_projection

    @property
    def loading(self) -> bool:
        return self._loading

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def last_request_count(self) -> int:
        return self._last_request_count

    @property
    def snapshot_warning_codes(self) -> tuple[str, ...]:
        return self._snapshot_warning_codes

    def begin(self, generation: int, time_s: int, guide_windows: int = 4, *,
              index_ticket: object | None = None) -> None:
        # Validate everything before changing active scope, diagnostics or LRU.
        if (not _integer(generation) or not _integer(time_s)
                or not _integer(guide_windows, 1) or guide_windows > 4
                or (self._generation is not None and generation <= self._generation)
                or (index_ticket is not None and type(index_ticket) is not object)):
            raise ValueError("invalid_request")
        if self._closed:
            raise ValueError("session_closed")
        horizon = (time_s, time_s + guide_windows * 1800)
        window = self._windows.get(horizon)
        if window is None:
            query = Query("events", self._scope + (("start", str(horizon[0])),
                                                  ("end", str(horizon[1]))))
            window = _Window(query)
            self._windows[horizon] = window
        self._windows.move_to_end(horizon)
        while len(self._windows) > self._window_limit:
            self._windows.popitem(last=False)
        self._generation = generation
        self._index_ticket = object() if index_ticket is None else index_ticket
        self._horizon = horizon
        if window.summary is not None:
            self.summaries['events'] = window.summary
        else:
            self.summaries.pop('events', None)
        self._guide_windows = guide_windows
        self._loading = True
        self.artwork = ArtInventory()
        self._repository.horizon = horizon
        self._repository.now = time_s
        # An unrequested old window must not remain visible through repository APIs.
        self._repository.events = self._accepted_items(window.cache, window.query, time_s)

    def apply(self, result: "LoadResult") -> bool:
        if (self._closed or not self._loading or self._horizon is None
                or type(result.generation) is not int or result.generation != self._generation
                or type(result.requested_start) is not int
                or result.requested_start != self._horizon[0]
                or type(result.guide_windows) is not int
                or result.guide_windows != self._guide_windows):
            return False
        snapshot = result.snapshot
        if snapshot is not None and (
                snapshot.namespace != self._namespace
                or not version_admitted(self._minimum, snapshot.version)
                or not isinstance(snapshot.horizon, tuple) or len(snapshot.horizon) != 2
                or not all(_integer(t) for t in snapshot.horizon)
                or snapshot.horizon != self._horizon or not _integer(snapshot.fetched_at)):
            return False
        if snapshot is not None:
            # Current read diagnostics are independent of positive cache admission.
            codes = tuple(code for batch in (snapshot.channels, snapshot.events,
                                              snapshot.recordings, snapshot.jobs)
                          for code in batch.warnings)
            self._snapshot_warning_codes = self._safe_warnings(codes)
        if snapshot is not None and snapshot.version != self._version:
            coherent = self._positive(snapshot.channels) and self._positive(snapshot.events)
            if coherent and (snapshot.channels.items or not snapshot.events.items):
                self._bind_actual(snapshot.version)
            else:
                # New-version partial data must not be mixed into an old query.
                # Keep old age/items as stale, revoke every old positive origin.
                self._version_blocked = True
                self._catalog_epoch = object()
                state = (snapshot.channels.state if not self._positive(snapshot.channels)
                         else snapshot.events.state)
                if state in _SUCCESS:
                    state = DataState.MALFORMED
                now = self._clock()
                failure: DataResult = DataResult((), state, 'metadata_result_rejected')
                self._channels.update(self._channels_query, failure, now=now, ttl=self._ttl)
                self._recordings.update(self._recordings_query, failure, now=now, ttl=self._ttl)
                self._jobs.update(self._jobs_query, failure, now=now, ttl=self._ttl)
                window = self._windows[self._horizon]
                window.cache.update(window.query, failure, now=now, ttl=self._ttl)
                self._loading = False
                self._last_error = 'worker_error'
                self._last_request_count = result.request_count if _integer(result.request_count) else 0
                self._sync_repository(now)
                return True
        window = self._windows[self._horizon]
        if snapshot is None:
            now = self._clock()
            failure = DataResult((), DataState.OUTAGE, "metadata_outage")
            self._channels.update(self._channels_query, failure, now=now, ttl=self._ttl)
            self._recordings.update(self._recordings_query, failure, now=now, ttl=self._ttl)
            self._jobs.update(self._jobs_query, failure, now=now, ttl=self._ttl)
            window.cache.update(window.query, failure, now=now, ttl=self._ttl)
            self._last_error = (result.error_code if type(result.error_code) is str
                                and result.error_code in _ERROR_CODES else "worker_error")
        else:
            now = snapshot.fetched_at
            channel_view = self._update(self._channels, self._channels_query, snapshot.channels, now)
            channel_accepted = (channel_view.state in _SUCCESS
                                and channel_view.items is snapshot.channels.items
                                and channel_view.updated_at == now)
            if channel_accepted:
                self._catalog_epoch = object()
            self._update(self._recordings, self._recordings_query, snapshot.recordings, now)
            self._update(self._jobs, self._jobs_query, snapshot.jobs, now)
            view = self._update(window.cache, window.query, snapshot.events, now)
            if view.state in _SUCCESS:
                window.warnings = self._safe_warnings(snapshot.events.warnings)
                # Event acceptance without a newly accepted qualifying catalog
                # cannot reuse or retag the prior positive origin.
                window.origin = None
                if (channel_accepted and view.items is snapshot.events.items
                        and view.updated_at == now
                        and (bool(channel_view.items) or not view.items)):
                    window.origin = GuideOrigin(
                        self._incarnation, self._index_ticket, self._catalog_epoch,
                        result.generation, self._namespace, snapshot.version,
                        self._horizon, now, view.items, channel_view.items)
                    self._version_blocked = False
            self._last_error = None
        self._last_request_count = (result.request_count
                                    if _integer(result.request_count) else 0)
        self._loading = False
        self._sync_repository(now)
        # Bind optional sidecar ONLY to this accepted result/session, never to a
        # cached stale fallback or new-version partial response. No art completion
        # calls apply(), rebuilds tuples/indexes, or renews metadata TTL.
        self.artwork = (snapshot.artwork if snapshot is not None and not self._version_blocked
            and all(self._positive(batch) for batch in (snapshot.channels, snapshot.events,
                snapshot.recordings, snapshot.jobs)) else ArtInventory())
        return True

    @staticmethod
    def _positive(batch: MetadataBatch) -> bool:
        return (type(batch.items) is tuple and batch.state in _SUCCESS
                and bool(batch.items) == (batch.state == DataState.READY))

    def _bind_actual(self, version: str) -> None:
        """O(1) cache epoch replacement, ONLY under runtime's worker ref hold.

        No old cached DTO is retagged to the new actual build. Resource failures
        on the new scope have no old-version positive fallback.
        """
        self._version = version
        self.summaries.clear()
        self._scope = (("namespace", self._namespace), ("version", version))
        self._channels_query = Query('channels', self._scope)
        self._recordings_query = Query('recordings', self._scope)
        self._jobs_query = Query('jobs', self._scope)
        self._channels = SnapshotCache()
        self._recordings = SnapshotCache()
        self._jobs = SnapshotCache()
        self._windows = OrderedDict()
        horizon = self._horizon
        assert horizon is not None
        self._windows[horizon] = _Window(Query('events', self._scope + (
            ('start', str(horizon[0])), ('end', str(horizon[1])))))
        self._catalog_epoch = object()
        self._version_blocked = False

    @staticmethod
    def _safe_warnings(codes: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(code for code in codes
                                   if type(code) is str and code in _SAFE_WARNINGS))

    def _update(self, cache: SnapshotCache[T], query: Query, batch: MetadataBatch[T],
                now: int) -> CacheView[T]:
        # Detail is deliberately fixed, even for constructed/malformed batches.
        result = DataResult(batch.items, batch.state, "metadata_result_rejected")
        view = cache.update(query, result, now=now, ttl=self._ttl)
        if (self._positive(batch) and view.items is batch.items and view.updated_at == now
                and not self._version_blocked and self._horizon is not None):
            summary = ResourceSummary(self._namespace, self._version or '',
                self._horizon, now, len(batch.items), batch.counts)
            self.summaries[query.resource] = summary
            if query.resource == 'events':
                self._windows[self._horizon].summary = summary
        return view

    @staticmethod
    def _accepted_items(cache: SnapshotCache[T], query: Query, now: int) -> tuple[T, ...]:
        view = cache.get(query, now=now)
        return view.items if view is not None and view.updated_at is not None else ()

    def _sync_repository(self, now: int) -> None:
        self._repository.channels = self._accepted_items(self._channels, self._channels_query, now)
        self._repository.recordings = self._accepted_items(
            self._recordings, self._recordings_query, now)
        self._repository.jobs = self._accepted_items(self._jobs, self._jobs_query, now)
        if self._horizon is not None:
            window = self._windows[self._horizon]
            self._repository.events = self._accepted_items(window.cache, window.query, now)

    def guide(self) -> Presentation[Event]:
        now = self._clock()
        self._sync_repository(now)
        window = self._windows.get(self._horizon) if self._horizon is not None else None
        view = window.cache.get(window.query, now=now) if window else None
        channels = self._channels.get(self._channels_query, now=now)
        presentation = self._present(view, now)
        state = presentation.state
        if not self._loading and channels and channels.state not in _SUCCESS:
            state = channels.state
        stale = presentation.stale or bool(channels and channels.stale)
        can_browse = (presentation.can_browse and channels is not None
                      and channels.updated_at is not None and bool(channels.items))
        gaps = self._channel_projection.update(self._repository.channels).footer
        warning = bool(gaps) or bool(window and any(code in window.warnings for code in
                                    ('guide_station_unqualified', 'guide_station_missing')))
        banner = self._banner(state, presentation.updated_at, presentation.age_seconds, stale,
                              warning=warning)
        return Presentation(presentation.items, state, self._loading, stale,
                            presentation.updated_at, presentation.age_seconds, banner,
                            self._message(state), can_browse)

    def recordings(self) -> Presentation[Recording]:
        now = self._clock()
        self._sync_repository(now)
        return self._present(self._recordings.get(self._recordings_query, now=now), now)

    def jobs(self) -> Presentation[Job]:
        now = self._clock()
        self._sync_repository(now)
        return self._present(self._jobs.get(self._jobs_query, now=now), now)

    def _present(self, view: CacheView[T] | None, now: int) -> Presentation[T]:
        items = view.items if view else ()
        updated_at = view.updated_at if view else None
        age = max(0, now - updated_at) if updated_at is not None else None
        state = None if self._loading or view is None else view.state
        stale = view.stale if view else False
        return Presentation(items, state, self._loading, stale, updated_at, age,
                            self._banner(state, updated_at, age, stale), self._message(state),
                            updated_at is not None and bool(items))

    def _banner(self, state: DataState | None, updated_at: int | None,
                age: int | None, stale: bool, *, warning: bool = False) -> str:
        result = status_banner(state, self._loading, updated_at, age, stale, self.strings, compact=True)
        if warning or self._snapshot_warning_codes:
            result = text(self.strings, 'status_with_warning', status=result,
                          warning=text(self.strings, 'listing_warning_hint'))
        return result

    def warning_details(self) -> str:
        """Presentation only: cached public fields and closed current read warnings."""
        window = self._windows.get(self._horizon) if self._horizon is not None else None
        return listing_warning_details(self._snapshot_warning_codes,
            self._channel_projection.update(self._repository.channels).footer,
            window.warnings if window else (), self.strings)

    def _message(self, state: DataState | None) -> str:
        if self._loading:
            return text(self.strings, 'live_loading_message')
        return text(self.strings, _EXPLANATIONS[state] if state else 'live_no_data_message')

    def close(self) -> None:
        self._closed = True
        self._loading = False
        self.artwork = ArtInventory()

    def cached_event_refs(self) -> tuple[tuple[Event, ...], ...]:
        """Bounded O(window_limit) retirement hold, no Event iteration/copy.

        Runtime offers these refs BEFORE begin/apply can evict/replace a cache.
        Commit only after mutation and all caller-local aliases are gone.
        """
        now = self._clock()
        return tuple(self._accepted_items(window.cache, window.query, now)
                      for window in self._windows.values())

    def cached_resource_refs(self) -> tuple[tuple, ...]:
        """Three bounded tuple references; transfer before actual-version rebind."""
        now = self._clock()
        return (self._accepted_items(self._channels, self._channels_query, now),
                self._accepted_items(self._recordings, self._recordings_query, now),
                self._accepted_items(self._jobs, self._jobs_query, now))

    def reenter(self, anchor: int) -> bool:
        """Select an already coherent cached horizon; never renew its lifetime.

        Caller must first drain metadata and ensure its single worker is quiet.
        Catalog epoch mismatch is deliberately NOT repaired by tuple identity.
        """
        if self._closed or self._version_blocked or self._loading or not _integer(anchor):
            return False
        for horizon, window in self._windows.items():
            if (horizon[0] <= anchor < horizon[1] and window.origin is not None
                    and window.origin.catalog_epoch is self._catalog_epoch):
                self._horizon = horizon
                if window.summary is not None:
                    self.summaries['events'] = window.summary
                else:
                    self.summaries.pop('events', None)
                self._repository.horizon = horizon
                self._repository.now = horizon[0]
                self._sync_repository(self._clock())
                return self.guide_origin is not None
        return False

    def detach_event_refs(self) -> None:
        """Only under a worker hold from cached_event_refs; owner transfers refs."""
        self.close()
        self._windows.clear()
        self._repository.events = ()
        self._horizon = None
