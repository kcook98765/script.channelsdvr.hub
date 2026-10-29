"""One bounded metadata worker and a latest-intent plain-data mailbox.

No GUI objects, automatic refresh, retries or timers belong here. Cancellation
cannot preempt admitted I/O; the next named operation checks the generation.
"""

from dataclasses import dataclass, field
import ipaddress
import re
from threading import Condition, Thread
from typing import Callable, Sized

from .metadata_http import ChannelsMetadataClient, MetadataError, StatusSample, status_sample, _CODES, _origin
from .metadata_repository import DevelopmentSnapshot, MetadataClient, load_metadata
from .guide_index import IndexInput, IndexProduct, build_index, matches_input
from .guide_fixture_data import FixtureMode, FixtureSnapshot, generate_fixture
from .models import Channel, Event
from .search import SearchRetirement
from .normalization import DataState
from .newbuild_compatibility import BASE_MINIMUM_DVR_BUILD, parse_build


@dataclass(frozen=True)
class DevelopmentConfig:
    origin: str = field(repr=False)
    namespace: str = "channels-dvr-development"
    expected_version: str = "2026.08.07.0346"

    def __post_init__(self) -> None:
        origin = _origin(self.origin)
        if (type(self.namespace) is not str
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.namespace)
                or type(self.expected_version) is not str
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.expected_version)):
            raise MetadataError("invalid_argument") from None
        object.__setattr__(self, "origin", origin)
        if parse_build(self.expected_version) is not None:
            # Preserve the persisted legacy setting, canonicalize ONLY this DTO.
            object.__setattr__(self, 'expected_version', BASE_MINIMUM_DVR_BUILD)

    @classmethod
    def from_host(cls, host: str) -> "DevelopmentConfig":
        """Pin a unicast numeric-dialog IP literal to HTTP port 8089, without DNS."""
        try:
            if type(host) is not str or not host or len(host) > 45 or "%" in host:
                raise ValueError
            address = ipaddress.ip_address(host)
            target = ((address.ipv4_mapped or address)
                      if isinstance(address, ipaddress.IPv6Address) else address)
            if (target.is_unspecified or target.is_multicast
                    or (isinstance(target, ipaddress.IPv4Address)
                        and int(target) == 0xFFFFFFFF)):
                raise ValueError
        except ValueError:
            raise MetadataError("invalid_origin") from None
        authority = f"[{address.compressed}]" if address.version == 6 else str(address)
        return cls(f"http://{authority}:8089")


@dataclass(frozen=True)
class LoadResult:
    generation: int
    requested_start: int
    guide_windows: int
    snapshot: DevelopmentSnapshot | None
    error_code: str | None
    request_count: int = 0


@dataclass(frozen=True)
class _Intent:
    generation: int
    config: DevelopmentConfig
    time_s: int
    guide_windows: int
    receipt: "MetadataSubmission"
    # A superseded not-yet-started pure source stays in this ONE pending slot
    # until the worker takes it. Never release the old large tuple on submit.
    discarded_source: IndexInput | None = field(default=None, repr=False)


@dataclass(frozen=True, eq=False, slots=True)
class MetadataSubmission:
    work_generation: int
    metadata_generation: int
    owner: object | None
    ticket: object | None
    index_revision: int


@dataclass(frozen=True, eq=False, slots=True)
class StatusObservation:
    config: DevelopmentConfig = field(repr=False)
    receipt: MetadataSubmission = field(repr=False)
    requested_start: int
    guide_windows: int
    sample: StatusSample = field(repr=False)


@dataclass(frozen=True, eq=False, slots=True)
class FixtureSubmission:
    work_generation: int
    mode: FixtureMode
    now: int
    revision: int
    owner: object
    ticket: object
    index_revision: int


@dataclass(frozen=True)
class IndexedLoadResult(LoadResult):
    """Opt-in metadata result. Legacy direct_metadata keeps its exact six fields."""
    work_generation: int = 0
    receipt: MetadataSubmission | None = None
    index_source: IndexInput | None = None
    index_product: IndexProduct | None = None
    index_error: str | None = None
    status_observation: "StatusObservation | None" = field(default=None, kw_only=True, repr=False)


@dataclass(frozen=True, eq=False, slots=True)
class IndexResult:
    work_generation: int
    receipt: IndexInput | FixtureSubmission
    index_source: IndexInput | None = None
    index_product: IndexProduct | None = None
    index_error: str | None = None
    fixture: FixtureSnapshot | None = None


@dataclass(frozen=True, eq=False, slots=True)
class SubmissionRejected:
    code: str = "index_busy"


WorkResult = LoadResult | IndexResult


@dataclass(frozen=True, eq=False, slots=True)
class RetirementBundle:
    """Only plain DTO references, not holders/windows/repos or arbitrary objects.

    result MUST be included when retiring a borrowed publication/display lease.
    Extra cache tuples allow O(1) transfer of a bounded (max8) cache eviction set.
    Owner must clear ALL aliases/local refs before commit_retirement(token).
    """
    result: WorkResult | None = None
    product: IndexProduct | None = None
    source: IndexInput | None = None
    snapshot: DevelopmentSnapshot | FixtureSnapshot | None = None
    events: tuple[Event, ...] | None = None
    channels: tuple[Channel, ...] | None = None
    cached_events: tuple[tuple[Event, ...], ...] = ()
    search: SearchRetirement | None = None
    resources: tuple[tuple, ...] = ()  # at most channels/recordings/jobs, plain DTO refs


@dataclass(eq=False, slots=True)
class _Retirement:
    token: object
    bundle: RetirementBundle
    committed: bool = False


class _PreparationCancelled(Exception):
    """Presentation-only cancellation must NOT discard current metadata."""


class _Cancelled(Exception):
    """Deliberately not MetadataError: stop independent repository reads too."""


class _GuardedClient:
    def __init__(self, client: MetadataClient, check: Callable[[], None]) -> None:
        self._client = client
        self._check = check

    @property
    def namespace(self) -> str:
        self._check()
        return self._client.namespace

    def verify_status(self) -> object:
        self._check()
        return self._client.verify_status()

    def public_channels(self) -> object:
        self._check()
        return self._client.public_channels()

    def guide_window(self, time_s: int, duration: int = 1800) -> object:
        self._check()
        return self._client.guide_window(time_s, duration=duration)

    def files_page(self, limit: int = 100, offset: int = 0) -> object:
        self._check()
        return self._client.files_page(limit=limit, offset=offset)

    def scheduled_jobs(self) -> object:
        self._check()
        return self._client.scheduled_jobs()


def _default_client(config: DevelopmentConfig) -> MetadataClient:
    return ChannelsMetadataClient(config.origin, namespace=config.namespace,
                                  expected_version=config.expected_version,
                                  request_budget=24, timeout=5, max_bytes=4 * 1024 * 1024)


def _request_count(client: MetadataClient | None) -> int:
    try:
        receipts = getattr(client, "receipts", None)
        return len(receipts) if isinstance(receipts, Sized) else 0
    except Exception:
        return 0


class MetadataLoader:
    """One worker, one pending descriptor, one published lease, one display lease.

    prepare/fixture reject while ANY work or publication is unresolved (metadata
    priority includes published-unconsumed data). request may supersede pure work.
    poll borrows opt-in results once; it does not remove their worker lease.
    accept moves that lease to the single active display. Reject/replace/close:
    offer_retirement -> clear ALL owner aliases -> commit_retirement, in that
    order. An occupied retirement slot defers adoption, never blocks the owner.
    close signals only; a borrowed/display lease keeps the daemon alive until its
    explicit handoff commits. Runtime must participate; this is not automatic GC.
    """

    def __init__(self, client_factory: Callable[[DevelopmentConfig], MetadataClient]
                 = _default_client) -> None:
        self._client_factory = client_factory
        self._condition = Condition()
        self._generation = 0
        self._work_generation = 0
        self._index_revision = 0
        self._pending_index_revision = 0
        self._active_index_revision = 0
        self._index_ticket: object | None = None
        self._metadata_receipt: MetadataSubmission | None = None
        self._pending: _Intent | IndexInput | FixtureSubmission | None = None
        self._active: _Intent | IndexInput | FixtureSubmission | None = None
        self._published: WorkResult | None = None
        self._polled = False
        self._display: WorkResult | None = None
        self._retirement: _Retirement | None = None
        self._final_retirement: _Retirement | None = None
        self._inflight = False
        self._closed = False
        self._stale_results = 0
        self._thread: Thread | None = None

    def request(self, config: DevelopmentConfig, time_s: int,
                guide_windows: int = 4, *, index_owner: object | None = None,
                index_ticket: object | None = None) -> int:
        # Validate/copy before touching tokens, pending work or the result slot.
        if (type(config) is not DevelopmentConfig or type(time_s) is not int
                or time_s < 0 or type(guide_windows) is not int
                or not 1 <= guide_windows <= 4
                or not self._tokens(index_owner, index_ticket, optional=True)):
            raise MetadataError("invalid_argument") from None
        captured = DevelopmentConfig(config.origin, config.namespace, config.expected_version)
        with self._condition:
            if self._closed:
                raise ValueError("loader_closed") from None
            self._generation += 1
            self._advance(index_ticket)
            self._metadata_receipt = MetadataSubmission(
                self._work_generation, self._generation, index_owner, index_ticket,
                self._index_revision)
            discarded = (self._pending if type(self._pending) is IndexInput else
                         self._pending.discarded_source if type(self._pending) is _Intent else None)
            self._pending = _Intent(self._generation, captured, time_s, guide_windows,
                                    self._metadata_receipt, discarded)
            self._wake()
            return self._generation

    @staticmethod
    def _tokens(owner: object, ticket: object, *, optional: bool = False) -> bool:
        return ((optional and owner is None and ticket is None)
                or (type(owner) is object and type(ticket) is object))

    def _advance(self, ticket: object | None) -> None:
        self._work_generation += 1
        self._index_revision += 1
        self._index_ticket = ticket

    def _wake(self) -> None:
        if self._thread is None:
            self._thread = Thread(target=self._run, name="channels-metadata", daemon=True)
            self._thread.start()
        self._condition.notify_all()

    def submitted(self, metadata_generation: int) -> MetadataSubmission | None:
        """Record this exact scalar receipt immediately after opt-in request()."""
        with self._condition:
            receipt = self._metadata_receipt
            return receipt if receipt and receipt.metadata_generation == metadata_generation else None

    def current_status_receipt(self, receipt: MetadataSubmission) -> bool:
        """Scalar-only latest request gate, including cancellation after delivery."""
        with self._condition:
            return (not self._closed and receipt is self._metadata_receipt
                    and receipt.metadata_generation == self._generation
                    and receipt.work_generation == self._work_generation)

    def _pure_rejection(self) -> SubmissionRejected | None:
        if self._closed:
            return SubmissionRejected("loader_closed")
        if self._inflight or self._pending is not None or self._published is not None:
            return SubmissionRejected()
        return None

    def prepare(self, events: tuple[Event, ...], channels: tuple[Channel, ...],
                namespace: str, version: str, horizon: tuple[int, int], fetched_at: int,
                owner: object, ticket: object, metadata_generation: int | None = None
                ) -> IndexInput | SubmissionRejected:
        """Return the EXACT source issued under the condition, or a closed busy code.

        Only outer shapes/scalars are checked here; all O(E/C) validation is on
        worker. Inputs are plain frozen DTO tuples, never SDK/owner containers.
        No session.begin, metadata generation advance, cache timestamp or client.
        """
        if (type(events) is not tuple or type(channels) is not tuple
                or type(namespace) is not str or type(version) is not str
                or type(horizon) is not tuple or len(horizon) != 2
                or any(type(t) is not int for t in horizon) or horizon[0] >= horizon[1]
                or type(fetched_at) is not int or fetched_at < 0
                or not self._tokens(owner, ticket)
                or (metadata_generation is not None and (
                    type(metadata_generation) is not int or metadata_generation < 0))):
            raise ValueError("invalid_index_input") from None
        with self._condition:
            rejected = self._pure_rejection()
            if rejected is not None:
                return rejected
            self._advance(ticket)
            source = IndexInput(events, channels, namespace, version, horizon, fetched_at,
                                self._work_generation, ticket, owner, metadata_generation)
            self._pending = source
            self._pending_index_revision = self._index_revision
            self._wake()
            return source

    def fixture(self, mode: FixtureMode, now: int, revision: int, owner: object,
                ticket: object) -> FixtureSubmission | SubmissionRejected:
        if (type(mode) is not FixtureMode or type(now) is not int or now < 0
                or type(revision) is not int or revision < 0 or not self._tokens(owner, ticket)):
            raise ValueError("invalid_fixture_input") from None
        with self._condition:
            rejected = self._pure_rejection()
            if rejected is not None:
                return rejected
            self._advance(ticket)
            receipt = FixtureSubmission(self._work_generation, mode, now, revision,
                                        owner, ticket, self._index_revision)
            self._pending = receipt
            self._wake()
            return receipt

    def poll(self) -> WorkResult | None:
        with self._condition:
            if self._polled or not self._current(self._published):
                return None
            self._polled = True
            self._condition.notify_all()
            return self._published

    def owns_result(self, result: WorkResult, receipt: object) -> bool:
        """O(1) exact publication/submission/input receipt; not source admission."""
        with self._condition:
            return ((result is self._published or result is self._display)
                    and self._current(result) and isinstance(result, (IndexedLoadResult, IndexResult))
                    and result.receipt is receipt
                    and (result.index_product is None or (
                        result.index_source is not None
                        and matches_input(result.index_product, result.index_source))))

    def accept(self, result: WorkResult) -> bool:
        """Retain one accepted display lease. Retire old display before replacing."""
        with self._condition:
            if (result is not self._published or not self._polled or self._display is not None
                    or self._retirement is not None
                    or not self._current(result)
                    or not isinstance(result, (IndexedLoadResult, IndexResult))):
                return False
            self._display = result
            self._published = None  # result/display retain it; no last-ref release
            self._polled = False
            self._condition.notify_all()
            return True

    def offer_retirement(self, bundle: RetirementBundle) -> object | None:
        """Phase 1: worker takes a lease but MUST NOT dispose before commit.

        No callback/arbitrary object payload. Heavy recursive DTO validation is
        worker-only; callers must use plain-data tuples as in prepare().
        """
        if (type(bundle) is not RetirementBundle
                or (bundle.result is not None and type(bundle.result) not in
                    (LoadResult, IndexedLoadResult, IndexResult))
                or (bundle.product is not None and type(bundle.product) is not IndexProduct)
                or (bundle.source is not None and type(bundle.source) is not IndexInput)
                or (bundle.snapshot is not None and type(bundle.snapshot) not in
                    (DevelopmentSnapshot, FixtureSnapshot))
                or (bundle.events is not None and type(bundle.events) is not tuple)
                or (bundle.channels is not None and type(bundle.channels) is not tuple)
                or (bundle.source is not None and not self._plain_source(bundle.source))
                or (bundle.product is not None and not self._retirable_product(bundle.product))
                or type(bundle.cached_events) is not tuple or len(bundle.cached_events) > 8
                or any(type(events) is not tuple for events in bundle.cached_events)
                or type(bundle.resources) is not tuple or len(bundle.resources) > 3
                or any(type(items) is not tuple for items in bundle.resources)
                or (bundle.search is not None and (type(bundle.search) is not SearchRetirement
                    or (bundle.search.snapshot is not None and type(bundle.search.snapshot) is not tuple)
                    or type(bundle.search.records) is not tuple or len(bundle.search.records) > 100000))):
            raise ValueError("invalid_retirement_bundle") from None
        with self._condition:
            if (isinstance(bundle.result, (IndexedLoadResult, IndexResult))
                    and bundle.result is not self._published and bundle.result is not self._display):
                raise ValueError("invalid_retirement_bundle") from None
            if self._retirement is not None:
                return None
            if self._closed and self._published is None and self._display is None:
                return None
            token = object()
            self._retirement = _Retirement(token, bundle)
            self._wake()
            return token

    def replace_display(self, result: WorkResult, bundle: RetirementBundle) -> object | None:
        """Atomic lease swap; the old display stays held until owner commit.

        Reserve the normal retirement slot before acknowledging the new result.
        No accept-vs-retirement gap, and no second ordinary disposal queue.
        """
        with self._condition:
            if (self._closed or result is not self._published or not self._polled
                    or not self._current(result) or self._retirement is not None
                    or bundle.result is not self._display):
                return None
            token = self.offer_retirement(bundle)
            if token is None:
                return None
            self._display = result
            self._published = None
            self._polled = False
            self._condition.notify_all()
            return token

    def offer_final_close(self, bundle: RetirementBundle) -> object:
        """One terminal attachment, independent of a busy ordinary retirement.

        Caller clears every window/cache/controller/result alias, then commits
        this token. Worker owns BOTH publication and display until terminal drain.
        This is a fixed final handoff, not a task/retirement queue. No GUI polling
        after close is required. Do not call twice with different attachments.
        """
        with self._condition:
            if self._final_retirement is not None:
                raise ValueError("final_close_already_offered")
            # Reuse envelope validation while holding existing leases. A busy
            # normal slot is temporarily detached, never released or overwritten.
            normal = self._retirement
            self._retirement = None
            try:
                token = self.offer_retirement(bundle)
                if token is None:
                    raise ValueError("loader_closed")
                self._final_retirement = self._retirement
            finally:
                self._retirement = normal
            self._closed = True
            self._invalidate()
            return token

    def commit_final_close(self, token: object) -> bool:
        with self._condition:
            if self._final_retirement is None or self._final_retirement.token is not token:
                return False
            self._final_retirement.committed = True
            self._condition.notify_all()
            return True

    @classmethod
    def _retirable_product(cls, product: IndexProduct) -> bool:
        # Missing/tampered source is rejection, never an unchecked attribute
        # access. _plain_source retains the exact IndexInput class gate.
        source: object = getattr(product, "source", None)
        return (isinstance(source, IndexInput) and cls._plain_source(source)
                and matches_input(product, source))

    @classmethod
    def _plain_source(cls, source: IndexInput) -> bool:
        """Constant-size envelope check, never recurse/iterate event payloads."""
        return (type(source) is IndexInput and cls._tokens(source.owner, source.ticket)
                and type(source.events) is tuple and type(source.channels) is tuple
                and type(source.namespace) is str and type(source.version) is str
                and type(source.horizon) is tuple and len(source.horizon) == 2
                and all(type(t) is int for t in source.horizon)
                and type(source.fetched_at) is int and type(source.work_generation) is int
                and (source.metadata_generation is None or type(source.metadata_generation) is int))

    def commit_retirement(self, token: object) -> bool:
        """Phase 2, scalar-only. Owner has removed EVERY old alias before this."""
        with self._condition:
            if self._retirement is None or self._retirement.token is not token:
                return False
            self._retirement.committed = True
            self._condition.notify_all()
            return True

    @property
    def retirement_busy(self) -> bool:
        with self._condition:
            return self._retirement is not None

    def cancel_index(self, ticket: object) -> None:
        """Invalidate only CPU preparation; current network snapshot still publishes."""
        if type(ticket) is not object:
            raise ValueError("invalid_index_ticket") from None
        with self._condition:
            if ticket is self._index_ticket:
                self._index_revision += 1
                self._condition.notify_all()

    def cancel(self) -> None:
        with self._condition:
            self._invalidate()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._invalidate()

    def _invalidate(self) -> None:
        self._generation += 1
        self._advance(None)
        # Invalid descriptors/results remain owned until WORKER disposal. Do not
        # use LatestRequest.begin (drops a possibly sole large reference here).
        self._condition.notify_all()

    @property
    def busy(self) -> bool:
        with self._condition:
            return self._inflight

    @property
    def pending(self) -> bool:
        with self._condition:
            return self._pending is not None and self._work(self._pending) == self._work_generation

    @property
    def work_quiet(self) -> bool:
        """Pure re-entry admission includes an untaken/borrowed publication."""
        with self._condition:
            return (not self._closed and not self._inflight and self._pending is None
                    and self._published is None)

    @property
    def worker_ident(self) -> int | None:
        with self._condition:
            return self._thread.ident if self._thread is not None else None

    @property
    def stale_results(self) -> int:
        with self._condition:
            return self._stale_results

    @staticmethod
    def _work(intent: _Intent | IndexInput | FixtureSubmission) -> int:
        if isinstance(intent, _Intent) and type(intent) is _Intent:
            return intent.receipt.work_generation
        if (isinstance(intent, (IndexInput, FixtureSubmission))
                and type(intent) in (IndexInput, FixtureSubmission)):
            return intent.work_generation
        return -1  # closed invalid descriptor: worker discards outside Condition

    def _current(self, result: WorkResult | None) -> bool:
        if result is None or self._closed:
            return False
        if isinstance(result, (IndexedLoadResult, IndexResult)):
            return result.work_generation == self._work_generation
        return result.generation == self._generation

    def _drain_retirement(self) -> None:
        # This method is worker-only, including its local reference destruction.
        retired = published = display = None
        with self._condition:
            if (self._retirement is None and self._final_retirement is not None
                    and self._final_retirement.committed):
                self._retirement = self._final_retirement
                self._final_retirement = None
                published, display = self._published, self._display
                self._published = self._display = None
                self._polled = False
            if self._retirement is not None and self._retirement.committed:
                retired = self._retirement
                target = retired.bundle.result
                if target is not None and self._published is target:
                    published = self._published
                    self._published = None
                    self._polled = False
                if target is not None and self._display is target:
                    display = self._display
                    self._display = None
                # Keep the ONE slot occupied through destruction, including
                # finalizers/GIL release. No second offer during this drain.
                self._retirement = _Retirement(retired.token, RetirementBundle())
                del target
        if retired is not None:
            token = retired.token
            del retired, published, display
            with self._condition:
                assert self._retirement is not None and self._retirement.token is token
                self._retirement = None
                self._condition.notify_all()

    def _check(self, work_generation: int) -> None:
        self._drain_retirement()
        with self._condition:
            if self._closed or work_generation != self._work_generation:
                raise _Cancelled() from None

    def _check_prepare(self, work_generation: int, revision: int) -> None:
        self._check(work_generation)
        with self._condition:
            if revision != self._index_revision:
                raise _PreparationCancelled() from None

    def _prepare(self, source: IndexInput, revision: int
                 ) -> tuple[IndexProduct | None, str | None]:
        try:
            product = build_index(source, check=lambda: self._check_prepare(
                source.work_generation, revision))
            self._check_prepare(source.work_generation, revision)
            return product, None
        except _PreparationCancelled:
            return None, "index_cancelled"
        except _Cancelled:
            raise
        except Exception:
            return None, "index_error"

    def _load(self, intent: _Intent) -> LoadResult | None:
        client: MetadataClient | None = None
        snapshot: DevelopmentSnapshot | None = None
        code: str | None = None
        try:
            self._check(intent.receipt.work_generation)
            client = self._client_factory(intent.config)
            guarded = _GuardedClient(client, lambda: self._check(intent.receipt.work_generation))
            snapshot = load_metadata(guarded, time_s=intent.time_s,
                                     guide_windows=intent.guide_windows)
            self._check(intent.receipt.work_generation)
        except _Cancelled:
            return None
        except MetadataError as error:
            code = (error.code if type(error.code) is str and error.code in _CODES
                    else "worker_error")
        except Exception:
            code = "worker_error"
        args = (intent.generation, intent.time_s, intent.guide_windows,
                snapshot, code, _request_count(client))
        receipt = intent.receipt
        sample = status_sample(client)
        observation = (StatusObservation(intent.config, receipt, intent.time_s,
                       intent.guide_windows, sample) if sample is not None
                       and code in (None, 'identity_mismatch') else None)
        if receipt.owner is None:
            return LoadResult(*args)
        source = product = None
        index_error = None
        if snapshot is not None:
            if (snapshot.channels.state in (DataState.READY, DataState.EMPTY)
                    and snapshot.events.state in (DataState.READY, DataState.EMPTY)):
                source = IndexInput(snapshot.events.items, snapshot.channels.items,
                                    snapshot.namespace, snapshot.version, snapshot.horizon,
                                    snapshot.fetched_at, receipt.work_generation,
                                    receipt.ticket, receipt.owner, intent.generation)
                try:
                    product, index_error = self._prepare(source, receipt.index_revision)
                except _Cancelled:
                    return None
            else:
                index_error = "index_ineligible"
        return IndexedLoadResult(*args, receipt.work_generation, receipt, source,
                                 product, index_error, status_observation=observation)

    def _pure(self, intent: IndexInput | FixtureSubmission) -> IndexResult | None:
        source = fixture = None
        try:
            if isinstance(intent, IndexInput) and type(intent) is IndexInput:
                source = intent
                # Pure requests only start when idle; their generation reserves
                # the presentation revision, captured under the condition.
                with self._condition:
                    revision = self._active_index_revision
            elif isinstance(intent, FixtureSubmission) and type(intent) is FixtureSubmission:
                revision = intent.index_revision
                fixture = generate_fixture(intent.mode, intent.now, intent.revision,
                    check=lambda: self._check_prepare(intent.work_generation, revision))
                source = IndexInput(fixture.events, fixture.channels, fixture.namespace,
                                    fixture.version, fixture.horizon, fixture.fetched_at,
                                     intent.work_generation, intent.ticket, intent.owner)
            else:
                raise ValueError("invalid_pure_intent")
            product, error = self._prepare(source, revision)
            return IndexResult(intent.work_generation, intent, source, product, error, fixture)
        except _PreparationCancelled:
            return IndexResult(intent.work_generation, intent, source, None, "index_cancelled", fixture)
        except _Cancelled:
            return None
        except Exception:
            return IndexResult(intent.work_generation, intent, source, None, "index_error", fixture)

    def _run(self) -> None:
        result: WorkResult | None
        while True:
            self._drain_retirement()
            discarded = None
            with self._condition:
                # Unborrowed stale results (and legacy consumed results) may be
                # disposed by worker. Borrowed opt-in results require handoff.
                if self._published is not None and (
                        not self._current(self._published)
                        or (self._polled and type(self._published) is LoadResult)):
                    if not self._polled or type(self._published) is LoadResult:
                        discarded = self._published
                        self._published = None
                        self._polled = False
                intent = self._pending
                if intent is not None:
                    self._pending = None
                    valid = not self._closed and self._work(intent) == self._work_generation
                    self._active = intent if valid else None
                    self._active_index_revision = self._pending_index_revision
                    self._inflight = valid
                else:
                    valid = False
                done = (self._closed and self._published is None and self._display is None
                        and self._retirement is None and self._final_retirement is None and intent is None)
            del discarded
            if done:
                return
            if intent is None:
                with self._condition:
                    # Recheck the SAME predicate under the SAME condition: no
                    # lost wakeup between idle transition and waiting.
                    self._condition.wait_for(self._wake_needed)
                continue
            if valid:
                if isinstance(intent, _Intent) and type(intent) is _Intent:
                    result = self._load(intent)
                elif (isinstance(intent, (IndexInput, FixtureSubmission))
                        and type(intent) in (IndexInput, FixtureSubmission)):
                    result = self._pure(intent)
                else:
                    result = None
            else:
                result = None
            # Drop input/discarded_source on WORKER, outside condition.
            with self._condition:
                self._active = None
            del intent
            while result is not None:
                self._drain_retirement()
                discarded = None
                with self._condition:
                    if self._published is not None and (
                            (not self._polled and not self._current(self._published))
                            or (self._polled and type(self._published) is LoadResult)):
                        discarded = self._published
                        self._published = None
                        self._polled = False
                del discarded
                with self._condition:
                    if not self._current(result):
                        break
                    if self._published is None:
                        self._published = result
                        self._polled = False
                        break
                    # At most one inflight result waiting behind a borrowed
                    # publication. Never overwrite it or silently lose loading.
                    self._condition.wait_for(lambda: self._published is None
                        or not self._current(result) or (self._retirement is not None
                                                       and self._retirement.committed))
            with self._condition:
                if result is None or self._published is not result:
                    self._stale_results += int(valid)
                self._inflight = False
                self._condition.notify_all()
            # Never keep a hidden worker-local large ref across idle/close.
            del result

    def _wake_needed(self) -> bool:
        return (self._pending is not None
                or (self._retirement is None and self._final_retirement is not None
                    and self._final_retirement.committed)
                or (self._retirement is not None and self._retirement.committed)
                or (self._published is not None and (
                    (not self._polled and not self._current(self._published))
                    or (self._polled and type(self._published) is LoadResult)))
                or (self._closed and self._published is None and self._display is None
                    and self._retirement is None and self._final_retirement is None))
