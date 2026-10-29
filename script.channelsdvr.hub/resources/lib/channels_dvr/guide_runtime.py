"""Owner-only index coordinator. Workers receive frozen DTOs and opaque tokens.

The authoritative repository may advance while the display repository stays
pinned. Neither a product seal nor metadata apply=True is action permission.
No schedule construction, Event iteration, wait, join or I/O belongs here.
"""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from threading import get_ident

from .guide import GuideController
from .guide_fixture_data import FixtureMode
from .guide_index import IndexInput, IndexProduct, matches_input
from .library import LibraryBrowser
from .live_loader import (DevelopmentConfig, FixtureSubmission, IndexedLoadResult,
                          IndexResult, LoadResult, MetadataSubmission,
                          RetirementBundle, SubmissionRejected)
from .live_loader import StatusObservation
from .metadata_http import valid_status_sample
from .live_session import DevelopmentRepository, LiveDataSession
from .models import Event
from .normalization import DataState
from .repository import MockRepository
from .search import SearchRetirement
from .session import OfflineDataSession, Presentation
from .ui_text import text

if TYPE_CHECKING:
    from .kodi_ui import ChannelsWindow


@dataclass(eq=False, slots=True)
class TicketContext:
    window: object
    repository: MockRepository
    controller: GuideController
    session: LiveDataSession | OfflineDataSession
    config: DevelopmentConfig | None
    view: str
    group: str
    revision: int
    receipt: MetadataSubmission | FixtureSubmission | IndexInput
    source: IndexInput | None = None
    applied: bool = False
    saved_connect: object = None
    saved_developer: object = None


def display_repository(repository: MockRepository) -> DevelopmentRepository:
    """O(1) DTO reference binding; small group/catalog projection is owner work."""
    result = DevelopmentRepository(repository.now, repository.horizon)
    result.channels, result.events = repository.channels, repository.events
    result.groups = repository.groups
    return result


class GuideRuntime:
    def __init__(self, window: "ChannelsWindow") -> None:
        self.window = window
        self.incarnation = object()
        self.synthetic_owner = object()
        self.context: TicketContext | None = None
        self.result: IndexedLoadResult | IndexResult | None = None
        self.display: IndexedLoadResult | IndexResult | None = None
        self.error: str | None = None
        self.revision = 0
        self._needs_prepare = True
        self._fixture: FixtureMode | None = None
        self._cancelled_fixture_ticket: object | None = None
        self._request: int | None = None
        self._mode: tuple[str, DevelopmentConfig | None] | None = None
        self._attempted: tuple[int, int, int, int | None, tuple[int, int]] | None = None
        self.latest_status: tuple[StatusObservation, TicketContext] | None = None

    def _session(self) -> LiveDataSession | OfflineDataSession:
        return self.window._live_session or self.window.data_session

    def _context(self, receipt: MetadataSubmission | FixtureSubmission | IndexInput) -> TicketContext:
        w = self.window
        context = TicketContext(self.incarnation, w.repository, w.guide, self._session(),
                                w._development_config, w.view, w.guide.group, self.revision, receipt)
        try:
            context.saved_connect = w.get_setting('development_metadata_enabled')
            context.saved_developer = w.get_setting('ShowDeveloperTools')
        except Exception:
            pass
        return context

    def _same_context(self, context: TicketContext) -> bool:
        w = self.window
        return (not w._closed and context.window is self.incarnation
                and self._mode is None and (w._live_session is not None or w.demo_allowed())
                and context.repository is w.repository and context.controller is w.guide
                and context.session is self._session() and context.config is w._development_config
                and context.view == w.view and context.group == w.guide.group
                and context.revision == self.revision)

    @property
    def ready(self) -> bool:
        w = self.window
        product = w.guide.current_product
        if (w._closed or (w._live_session is None and not w.demo_allowed())
                or self.error is not None or self._fixture is not None or self._mode is not None
                or self._request is not None or not w.guide.prepared_ready
                or product is None or self.result is not None
                or product.events is not w.repository.events
                or product.channels is not w.repository.channels
                or product.source.horizon != w.repository.horizon):
            return False
        if w._live_session is not None:
            return (not w._live_session.loading
                    and w._live_session.accepts_index_origin(product))
        base = w.data_session.guide()
        return (product.source.owner is self.synthetic_owner
                and base.items is product.events and base.updated_at == product.source.fetched_at)

    def presentation(self, base: Presentation[Event]) -> Presentation[Event]:
        if self.ready:
            return replace(base, can_browse=False) if base.loading else base
        strings = self.window.strings
        status = text(strings, 'guide_unavailable' if self.error else 'preparing_guide')
        if self.window.guide.current_product is not None:
            status = text(strings, 'guide_retained_status', status=status)
        return replace(base, can_browse=False,
            banner=text(strings, 'status_with_warning', status=base.banner, warning=status),
            message=text(strings, 'index_notice', message=base.message, status=status))

    def selected(self, event: Event | None = None) -> Event | None:
        w = self.window
        if (w.view not in ("Guide", "Search") or w._development_blocked is not None or not self.ready):
            return None
        base = w._live_session.guide() if w._live_session is not None else w.data_session.guide()
        if not base.can_browse or base.loading:
            return None
        if w._live_session is not None:
            # Exact current logical configuration, including post-modal changes.
            if (w.get_setting("development_metadata_enabled") is not True
                    or w._saved_config() != w._development_config):
                return None
        selected = event if event is not None else w.guide.selected_event()
        product = w.guide.current_product
        if selected is None or product is None or w.guide.channel(selected.channel) is None:
            return None
        schedule = product.schedules.get(selected.channel)
        if schedule is None:
            return None
        index = schedule.positions.get((selected.id, selected.start))
        return selected if index is not None and schedule.events[index] is selected else None

    def invalidate_view(self) -> None:
        self.latest_status = None
        # Installed all-T indices remain valid for O(C) local collection changes.
        # A pending old-view preparation is cancelled without cancelling HTTP.
        self.revision += 1
        if self.context is not None:
            self.window._metadata_loader.cancel_index(self.context.receipt.ticket)
        self._needs_prepare = True

    def request(self, anchor: int) -> None:
        self.latest_status = None  # at intent admission, including deferred submissions
        self._request = max(0, anchor)
        self.error = None
        self._needs_prepare = False
        self._start_request()

    def reenter(self, anchor: int) -> bool:
        w = self.window
        loader = w._metadata_loader
        if (w._live_session is None or not loader.work_quiet
                or self.result is not None or self.context is not None
                or loader.retirement_busy or self._request is not None):
            return False
        # prepare() additionally rejects any published-unconsumed load. Public
        # drain ordering is maintained by process_pending before this route.
        if not w._live_session.reenter(anchor):
            return False
        w._intended_anchor = anchor
        self._needs_prepare = True
        self.error = None
        self._attempted = None
        self.prepare()
        return True

    def _hold_caches(self) -> object | None:
        w = self.window
        return w._metadata_loader.offer_retirement(RetirementBundle(
            events=w.repository.events,
            cached_events=(w._live_session.cached_event_refs() if w._live_session
                           else (w.data_session._events,)), search=self._search_hold(),
            resources=w._live_session.cached_resource_refs() if w._live_session else ()))

    def _search_hold(self) -> SearchRetirement:
        w = self.window
        return SearchRetirement(w._search_snapshot,
            w._search_index.retirement_records if w._search_index is not None else ())

    def _start_request(self) -> None:
        w = self.window
        if self._request is None or self.result is not None or self._mode is not None:
            return
        session, config = w._live_session, w._development_config
        if session is None or config is None or w._development_blocked is not None:
            self._request = None
            return
        token = self._hold_caches()
        if token is None:
            return
        anchor = self._request
        start = anchor - anchor % 1800
        ticket = object()
        generation = w._metadata_loader.request(config, start, index_owner=session.incarnation,
                                                 index_ticket=ticket)
        receipt = w._metadata_loader.submitted(generation)
        session.begin(generation, start, index_ticket=ticket)
        w._intended_anchor = anchor
        self.context = self._context(receipt) if receipt is not None else None
        self._request = None
        w._clear_search_cache()
        w._metadata_loader.commit_retirement(token)

    def fixture(self, mode: FixtureMode) -> None:
        self.latest_status = None
        if (self.window._live_session is not None or self.window._closed
                or not self.window.demo_allowed()):
            return
        self.revision += 1
        self.error = None
        self._fixture = mode
        self._needs_prepare = False
        if self.result is None:
            # No borrowed publication exists. The worker still owns invalidated
            # in-flight/pending/unborrowed sources; don't leave an obsolete scalar
            # receipt preventing the latest fixed fixture from being submitted.
            self.context = None
        self.window._metadata_loader.cancel()
        self._start_fixture()

    def cancel_fixture(self) -> bool:
        """Back cancels a pending fixed fixture, preserving the accepted display."""
        if self._fixture is None:
            return False
        self._fixture = None
        self._needs_prepare = False
        self.revision += 1
        if self.context is not None:
            self._cancelled_fixture_ticket = self.context.receipt.ticket
        if self.result is None:
            self.context = None
        self.error = None
        self.window._metadata_loader.cancel()
        return True

    def _start_fixture(self) -> None:
        if self._fixture is None or self.result is not None or not self.window.demo_allowed():
            return
        w = self.window
        receipt = w._metadata_loader.fixture(self._fixture, 1_800_000_000,
                                             self.revision, self.synthetic_owner, object())
        if isinstance(receipt, SubmissionRejected):
            return
        self.context = self._context(receipt)
        # Keep _fixture until accepted: metadata ready is not fixture/index ready.

    def prepare(self) -> None:
        w = self.window
        if (self._mode is not None or w._development_blocked is not None
                or (w._live_session is None and not w.demo_allowed())):
            self._needs_prepare = False
            return
        base = w._live_session.guide() if w._live_session else w.data_session.guide()
        key = (id(self._session()), id(base.items), id(w.repository.channels),
               base.updated_at, w.repository.horizon)
        if key != self._attempted and not self.ready:
            self._needs_prepare = True
        if (not self._needs_prepare or self.context is not None or self.result is not None
                or self._fixture is not None or self._request is not None or self.error):
            return
        session = w._live_session
        if session is not None:
            base = session.guide()
            origin = session.guide_origin
            if session.loading or origin is None:
                return
            source = w._metadata_loader.prepare(origin.events, origin.channels, origin.namespace,
                origin.version, origin.horizon, origin.fetched_at, origin.owner, origin.ticket,
                origin.metadata_generation)
        else:
            base = w.data_session.guide()
            if base.updated_at is None or base.loading:
                return
            w.repository.events = base.items
            source = w._metadata_loader.prepare(base.items, w.repository.channels, "synthetic",
                "fixture", w.repository.horizon, base.updated_at, self.synthetic_owner, object())
        if isinstance(source, SubmissionRejected):
            return
        self.context = self._context(source)
        self.context.source = source
        self._attempted = key
        self._needs_prepare = False

    def poll(self) -> bool:
        """Drain/apply metadata BEFORE arbitration for pure cache preparation."""
        w = self.window
        changed = False
        if self.result is None:
            result = w._metadata_loader.poll()
            if isinstance(result, (IndexedLoadResult, IndexResult)):
                self.result = result
            elif isinstance(result, LoadResult):
                self.latest_status = None
                # Runtime always submits tokened loads; never bless an untokened
                # result supplied by an old caller/test double.
                # Untokened/fixture results cannot establish an actual build.
            result = None
        if self.result is not None:
            changed = self._consume() or changed
        # A native view can change through several menu/helper paths. Detect it
        # here as well as explicit collection callbacks; never cancel current HTTP.
        if self.context is not None and not self._same_context(self.context):
            w._metadata_loader.cancel_index(self.context.receipt.ticket)
        if self._mode is not None:
            changed = self._change_mode() or changed
        elif self._request is not None:
            self._start_request()
        elif self._fixture is not None:
            if self.context is None:
                self._start_fixture()
        else:
            self.prepare()
        return changed

    def _consume(self) -> bool:
        w = self.window
        result, context = self.result, self.context
        if result is None:
            return False
        if isinstance(result, IndexedLoadResult) and context is not None and not context.applied:
            token = self._hold_caches()
            if token is None:
                return False
            if (w._live_session is context.session
                    and context.config is w._development_config
                    and context.window is self.incarnation
                    and isinstance(context.receipt, MetadataSubmission)
                    and context.receipt.owner is w._live_session.incarnation
                    and w._saved_config() == context.config
                    and w.get_setting('development_metadata_enabled') is True
                    and w._metadata_loader.owns_result(result, context.receipt)):
                accepted = w._live_session.apply(result)
                self.latest_status = None
                observation = result.status_observation
                if (accepted and type(observation) is StatusObservation
                        and isinstance(context.receipt, MetadataSubmission)
                        and self._same_context(context)
                        and w._metadata_loader.owns_result(result, context.receipt)
                        and observation.receipt is context.receipt
                        and valid_status_sample(observation.sample)
                        and observation.config == context.config
                        and observation.requested_start == result.requested_start
                        and observation.guide_windows == result.guide_windows
                        and context.receipt.metadata_generation == result.generation
                        and context.receipt.work_generation == result.work_generation
                        and context.receipt.owner is w._live_session.incarnation
                        and context.saved_connect is True
                        and w.get_setting('development_metadata_enabled') is context.saved_connect
                        and w.get_setting('ShowDeveloperTools') is context.saved_developer
                        and w._saved_config() == context.config):
                    self.latest_status = (observation, context)
                w.library.update(w.repository.recordings)
            context.applied = True
            w._clear_search_cache()
            w._metadata_loader.commit_retirement(token)
            return True  # retirement must drain before display swap
        product, source = result.index_product, result.index_source
        if context is not None and source is not None and product is not None:
            valid = (self._same_context(context)
                     and w._metadata_loader.owns_result(result, context.receipt)
                     and product.source is source and matches_input(product, source)
                     and (context.source is None or context.source is source))
            if valid and w._live_session is not None:
                valid = w._live_session.accepts_index_origin(product)
            elif valid and isinstance(context.receipt, FixtureSubmission):
                fixture = result.fixture if isinstance(result, IndexResult) else None
                valid = (fixture is not None and source.owner is self.synthetic_owner
                         and source.ticket is context.receipt.ticket
                         and fixture.channels is source.channels and fixture.events is source.events
                         and fixture.horizon == source.horizon and fixture.fetched_at == source.fetched_at
                         and fixture.mode is context.receipt.mode and fixture.revision == context.receipt.revision
                         and fixture.now == context.receipt.now)
            elif valid:
                base = w.data_session.guide()
                valid = (base.items is source.events and base.updated_at == source.fetched_at
                         and source.channels is w.repository.channels and source.owner is self.synthetic_owner
                         and source.horizon == w.repository.horizon and not base.loading)
            if valid and not (isinstance(result, IndexResult) and result.fixture is not None):
                valid = (source.events is w.repository.events and source.channels is w.repository.channels
                         and source.horizon == w.repository.horizon)
            if valid:
                return self._adopt(result, product, source)
        # Missing inputs use the same borrowed-result retirement path below.
        token = w._metadata_loader.offer_retirement(RetirementBundle(result=result))
        if token is None:
            return False
        # Do not resubmit an error forever. View-only cancellation permits ONE
        # latest preparation after unchanged metadata has been applied normally.
        self.error = result.index_error or "index_unavailable"
        retry = self.error == "index_cancelled" or (context is not None and not self._same_context(context))
        cancelled_fixture = (isinstance(result, IndexResult)
            and isinstance(result.receipt, FixtureSubmission)
            and result.receipt.ticket is self._cancelled_fixture_ticket)
        if cancelled_fixture:
            retry = False
            self.error = None
        self.result = self.context = None
        self._needs_prepare = retry
        if not retry:
            self._fixture = None
        if retry:
            self.error = None
        del result, context, source, product
        w._metadata_loader.commit_retirement(token)
        return True

    def _adopt(self, result: IndexedLoadResult | IndexResult, product: IndexProduct,
               source: IndexInput) -> bool:
        w = self.window
        fixture = result.fixture if isinstance(result, IndexResult) else None
        if fixture is not None:
            repository = DevelopmentRepository(fixture.now, fixture.horizon)
            repository.channels, repository.events = fixture.channels, fixture.events
            repository.groups = fixture.groups
        else:
            repository = display_repository(w.repository)
        controller = GuideController(display_repository(repository), deferred=True,
            projection=w._live_session.channel_projection if w._live_session else None, strings=w.strings)
        controller.group = w.guide.group if w.guide.group in repository.groups else "All"
        controller.selection, controller.viewport, controller.top = (
            w.guide.selection, w.guide.viewport, w.guide.top)
        if w._intended_anchor is not None:
            controller.selection = replace(controller.selection, anchor=w._intended_anchor)
        if not controller.adopt_prepared(product, source):
            self.error = "index_unavailable"
            return False
        if fixture is None and w._live_session is None and not source.events:
            # Preserve the synthetic scenario bookmark independently of the
            # accepted empty index; selected() still rejects an empty schedule.
            controller.selection = w.guide.selection
        token = w._metadata_loader.replace_display(result, RetirementBundle(
            result=self.display, product=w.guide.current_product, events=w.repository.events,
            cached_events=(w._live_session.cached_event_refs() if w._live_session
                           else (w.data_session._events,)), search=self._search_hold(),
            resources=w._live_session.cached_resource_refs() if w._live_session else ()))
        if token is None:
            return False
        # Relinquish OLD identity-bound owner holders under the uncommitted
        # worker hold. The next render is too late: drain may follow commit
        # immediately. SDK controls stay owner-only; these caches are bounded.
        w._render_memo.reset()
        w._clear_search_cache()
        if fixture is not None:
            w.repository = repository
            w.data_session = OfflineDataSession(repository, fixture.mode.value, strings=w.strings)
            w._large_fixture = fixture.mode in (FixtureMode.LARGE500, FixtureMode.DENSE500_48H)
            w.stress_fixture = False
            w.library = LibraryBrowser((), strings=w.strings)
            w._reset_search()
        w.guide = controller
        if self.context is not None:
            # _consume's local context must not keep the old controller/maps
            # alive beyond commit and later destroy them on the owner thread.
            self.context.controller = controller
            self.context.repository = w.repository
            self.context.session = self._session()
        self.display = result
        self.result = self.context = None
        self._fixture = None
        self.error = None
        self._needs_prepare = False
        w._intended_anchor = None
        w._metadata_loader.commit_retirement(token)
        return True

    def mode(self, kind: str, config: DevelopmentConfig | None = None) -> None:
        self.latest_status = None
        if kind not in ('live', 'no_connection', 'normal', 'stress'):
            raise ValueError('Unknown fixed guide mode')
        if kind in ('normal', 'stress') and not (self.window.transient_demo_mode is True
                and self.window.developer_enabled() and not self.window._closed):
            return
        if kind in ('live', 'no_connection'):
            self.window.transient_demo_mode = False
        self._mode = (kind, config)
        # Paint admission changes even when the swap finishes synchronously or
        # awaits retirement. Existing controller/repository/display pins stay
        # until _change_mode obtains its worker hold; painting does not detach.
        self.window._paint_pending = True
        self.window._metadata_loader.cancel()
        self.revision += 1
        self._change_mode()

    def _change_mode(self) -> bool:
        if self._mode is None or self.result is not None:
            return False
        w = self.window
        token = w._metadata_loader.offer_retirement(RetirementBundle(
            result=self.display, product=w.guide.current_product, events=w.repository.events,
            cached_events=(w._live_session.cached_event_refs() if w._live_session
                           else (w.data_session._events,)), search=self._search_hold(),
            resources=w._live_session.cached_resource_refs() if w._live_session else ()))
        if token is None:
            return False
        w._render_memo.reset()
        kind, config = self._mode
        w._recording_ui.close()
        if w._live_session is not None:
            w._live_session.detach_event_refs()
        if kind == "live":
            w._live_session = LiveDataSession(config.namespace if config else "channels-dvr-development",
                config.expected_version if config else "2026.08.07.0346", strings=w.strings)
            w.repository = w._live_session.repository
            w.data_session = OfflineDataSession(DevelopmentRepository(0, (0, 7200)), strings=w.strings)
        elif kind == 'no_connection':
            w._live_session = None
            w.repository = DevelopmentRepository(0, (0, 7200))
            w.data_session = OfflineDataSession(w.repository, strings=w.strings)
        else:
            w._live_session = None
            # Only the bounded legacy normal/stress seed is generated on owner.
            # Preserve existing recordings/jobs/airings; ALL index CPU is worker.
            if kind == "stress":
                from .guide_fixtures import stress_repository
                w.repository = stress_repository()
            else:
                w.repository = MockRepository()
            w.data_session = OfflineDataSession(w.repository, kind, strings=w.strings)
        w._development_config = config
        w.guide = GuideController(display_repository(w.repository), deferred=True, strings=w.strings)
        self.display = None
        w.library = LibraryBrowser(w.repository.recordings, strings=w.strings)
        w.library_return_mode = "Shows"
        w.positions.clear()
        w._reset_search()
        w.stress_fixture = kind == "stress"
        w._large_fixture = False
        w._intended_anchor = None
        if kind in ('normal', 'stress'):
            w.view = "Guide"
        self.synthetic_owner = object()
        self.context = None
        self._fixture = None
        self._mode = None
        self.error = None
        self._attempted = None
        self._needs_prepare = kind in ('normal', 'stress') and w.demo_allowed()
        w._metadata_loader.commit_retirement(token)
        return True

    def close(self) -> None:
        self.latest_status = None
        w = self.window
        # Final attachment works even while an ordinary retirement is draining.
        # No owner poll is needed after GUI close; commit precedes super.close().
        token = w._metadata_loader.offer_final_close(RetirementBundle(
            result=self.result or self.display, product=w.guide.current_product,
            events=w.repository.events,
            cached_events=(w._live_session.cached_event_refs() if w._live_session
                            else (w.data_session._events,)), search=self._search_hold(),
            resources=w._live_session.cached_resource_refs() if w._live_session else ()))
        w._render_memo.reset()
        if w._live_session is not None:
            w._live_session.detach_event_refs()
        w._reset_search()
        w.guide.detach_product()
        w.guide.repository.events = ()
        w.repository.events = ()
        w.data_session = OfflineDataSession(DevelopmentRepository(0, (0, 7200)), strings=w.strings)
        self.result = self.display = self.context = None
        w._metadata_loader.commit_final_close(token)

    def diagnostic(self) -> str:
        product = self.window.guide.current_product
        return text(self.window.strings, 'diagnostic_index', owner=get_ident(),
            worker=self.window._metadata_loader.worker_ident, ready=self.ready, deferred=self.window.guide.deferred,
            channels=len(product.channels) if product else 0, events=len(product.events) if product else 0,
            pending=self.context is not None, error=self.error or 'none')
