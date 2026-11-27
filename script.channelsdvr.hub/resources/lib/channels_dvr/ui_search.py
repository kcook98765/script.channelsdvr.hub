"""Owner-only Search adapter. It observes local publications, never requests one."""

from itertools import islice

from .browser import Row, recording_key, recording_row, scheduled_row
from .dvr_ui import PassRows
from .guide import time_label
from .guide_projection import ChannelProjection
from .models import Job, Recording
from .normalization import DataState
from .pass_browser import display_text
from .search import MAX_SEARCH_CHANNELS, ProgrammeSearchIndex, airing_key, normalize_query
from .search_projection import (MAX_LISTING_RECORDS, GuideScope, GuideSnapshot, Listing,
                                ListingCoverage, PassEntry, PassId, ProgrammeId, RecordingId,
                                ScheduledId, SearchCategory, SearchHit, project_search)
from .ui_menu import command, group_label, select
from .ui_modal import dialog_for, modal_input
from .ui_routes import SearchReturn, enter, route_fence


CATEGORIES = dict(zip(SearchCategory, ('recordings_all', 'search_category_guide', 'recordings',
                                      'search_category_scheduled', 'dvr_passes')))
TAB_COMMANDS = {'search.tab.' + category.value: category for category in CATEGORIES}
TABS = dict(zip(range(220, 225), TAB_COMMANDS))
LABELS = {code: CATEGORIES[category] for code, category in TAB_COMMANDS.items()} | {
    'search.programmes': 'search_programmes', 'search.context': 'search', 'search.query': 'search',
    'search.clear': 'search_clear', 'search.reveal': 'show_in_guide', 'search.coverage': 'search_coverage',
}


def _health(data, *, partial=False, capped=False, loading=False, unavailable=False):
    if data is None:
        return ListingCoverage(loading=loading, unavailable=unavailable)
    return ListingCoverage(loading=loading or data.loading, stale=data.stale and data.updated_at is not None,
                           partial=partial or data.state == DataState.PARTIAL,
                           unavailable=unavailable or data.state in (DataState.OUTAGE, DataState.MALFORMED,
                                                                     DataState.UNSUPPORTED), capped=capped)


def _signature(data):
    # No large tuple is pinned in an activation or projection cache key.
    return None if data is None else (id(data.items), data.updated_at, data.loading, data.stale, data.state)


class SearchUI:
    def __init__(self, window):
        self.w = window
        self.category = SearchCategory.ALL
        self._source = None
        self._serial = 0
        self.invalidate()

    def invalidate(self):
        self._key = None
        self._page = None
        self._hits: tuple[SearchHit, ...] = ()
        self._rows: tuple[Row, ...] = ()
        self._empty = {}
        self.publication = object()

    def reset(self):
        self.category = SearchCategory.ALL
        self._source = None
        self.invalidate()

    def set_category(self, category):
        if self.category is not category:
            self.category = category
            self.w.search_selection = None
            self.w.positions['Search'] = 0
            self.invalidate()

    def active_tab(self):
        return next(ident for ident, code in TABS.items() if TAB_COMMANDS[code] is self.category)

    def _admission(self):
        w = self.w
        runtime, live = w._guide_runtime, w._live_session
        if (w._closed or w._development_blocked is not None or runtime._mode is not None
                or getattr(live, '_version_blocked', False)):
            return None
        if live is not None:
            return live if w._recording_ui._current_config() is not None else None
        return w.data_session if w.demo_allowed() else None

    def _inputs(self):
        w = self.w
        admitted = self._admission()
        live = w._live_session
        binding = ((live.incarnation, getattr(live, 'actual_version', None)) if live is not None else
                   (id(w.data_session), w._guide_runtime.synthetic_owner))
        if binding != self._source:
            self._source = binding
            self._serial += 1
            w.search_selection = None
            w.positions['Search'] = 0
            self.invalidate()
        if admitted is None:
            return admitted, (None,) * 4
        return admitted, (admitted.guide(), admitted.recordings(), admitted.jobs(), w._dvr.search_data())

    def _guide(self, data):
        w = self.w
        runtime, live = w._guide_runtime, w._live_session
        product = w.guide.current_product
        error = runtime.error is not None
        coherent = (data.updated_at is not None and data.items is w.repository.events
                    and w.guide.repository.horizon == w.repository.horizon
                    and ((product is not None and product.events is data.items
                          and product.channels is w.repository.channels
                          and w.guide.prepared_ready and (live is None or live.accepts_index_origin(product)))
                         or (live is None and not w.guide.deferred)))
        warnings = getattr(live, 'snapshot_warning_codes', ())
        gap = w.guide.projection.footer if coherent else ''
        coverage = _health(data, loading=not coherent and not error and data.updated_at is not None
                           and (data.state in (None, DataState.READY, DataState.EMPTY)),
                           unavailable=error, partial=bool(gap) or any(code in warnings for code in
                               ('guide_station_missing', 'guide_station_unqualified')),
                           capped=coherent and len(w.guide.channels) > MAX_SEARCH_CHANNELS)
        if not coherent:
            return Listing(coverage=coverage)
        events, channels = data.items, w.repository.channels
        if w._search_index is None or w._search_snapshot is not events or w._search_channels is not channels:
            w._search_index = ProgrammeSearchIndex(events, channels)
            w._search_snapshot, w._search_channels = events, channels
        scope = GuideScope(w.repository.horizon, tuple(c.key for c in islice(w.guide.channels, MAX_SEARCH_CHANNELS)))
        return Listing(GuideSnapshot(w._search_index, scope), coverage)

    def page(self):
        w = self.w
        admitted, data = self._inputs()
        guide, recordings, jobs, passes = data
        live = w._live_session
        product = w.guide.current_product
        warnings = getattr(live, 'snapshot_warning_codes', ())
        key = (normalize_query(w.search_query), self.category, admitted is not None, self._serial,
               getattr(live, '_generation', None), getattr(live, '_catalog_epoch', None),
               getattr(w.data_session, '_revision', None), w._guide_runtime.revision,
               w._guide_runtime.error, w._guide_runtime._mode is not None,
               w.guide.group, w.repository.horizon, id(w.guide.channels),
               product.source.ticket if product is not None else None, id(w.library.search_records),
               tuple(warnings), tuple(_signature(item) for item in data))
        if self._page is not None and key == self._key:
            return self._page
        pending = w._guide_runtime._mode
        loading = pending is not None and pending[0] == 'live' and pending[1] is not None
        missing = ListingCoverage(loading=loading, unavailable=not loading)
        listing_guide: Listing[GuideSnapshot] = Listing(coverage=missing)
        listing_recordings: Listing[tuple[Recording, ...]] = Listing(coverage=missing)
        listing_jobs: Listing[tuple[Job, ...]] = Listing(coverage=missing)
        listing_passes: Listing[tuple[PassEntry, ...]] = Listing(coverage=missing)
        if admitted is not None:
            listing_guide = self._guide(guide)
            # Demo snapshots can change without visiting the Recordings route.
            # The live browser has already admitted/validated the same leaf batch.
            records = self._records(recordings)
            listing_recordings = Listing(records if recordings.updated_at is not None else None,
                _health(recordings, partial=(live is not None and w.library.coverage_limited)
                        or 'offset_scan_not_snapshot_consistent' in warnings,
                        capped='files_page_limit' in warnings,
                        loading=records is None and recordings.updated_at is not None))
            listing_jobs = Listing(jobs.items if jobs.updated_at is not None else None, _health(jobs))
            entries = (tuple(PassEntry(row.key, row.name) for row in islice(passes.items, MAX_LISTING_RECORDS))
                       if passes.updated_at is not None else None)
            listing_passes = Listing(entries, _health(passes, capped=len(passes.items) > MAX_LISTING_RECORDS,
                                                       partial=bool(entries and any(not item.name for item in entries))))
        page = project_search(w.search_query, source=str(self._serial), category=self.category,
                              guide=listing_guide, recordings=listing_recordings,
                              scheduled=listing_jobs, passes=listing_passes)
        snapshots: dict[SearchCategory, GuideSnapshot | tuple[Recording | Job | PassEntry, ...] | None] = {
            SearchCategory.GUIDE: listing_guide.snapshot,
            SearchCategory.RECORDINGS: listing_recordings.snapshot,
            SearchCategory.SCHEDULED: listing_jobs.snapshot,
            SearchCategory.PASSES: listing_passes.snapshot,
        }
        empty = {}
        rows, hits = [], []
        for group in page.groups:
            snapshot = snapshots[group.category]
            empty[group.category] = (snapshot == () or
                (isinstance(snapshot, GuideSnapshot) and (not snapshot.scope.channels or not w._search_snapshot)))
            for hit in group.hits:
                hits.append(hit)
                title = display_text(hit.title, 100, w.strings) or w.t('unknown')
                if isinstance(hit.id, ProgrammeId):
                    channel = w.guide.channel(hit.id.channel)
                    if channel is not None:
                        title += ' | ' + (display_text(f'{channel.number} {channel.name}', 50, w.strings) or '')
                if hit.when is not None:
                    try:
                        title += ' | ' + time_label(hit.when)
                    except (ValueError, OverflowError, OSError):
                        pass
                rows.append(Row(repr(hit.id), f"{w.t(CATEGORIES[group.category])}: {title}", ''))
        pos = min(w.positions.get('Search', 0), max(0, len(hits)-1))
        pos = next((i for i, hit in enumerate(hits) if hit.id == w.search_selection), pos)
        w.positions['Search'] = pos
        w.search_selection = hits[pos].id if hits else None
        self._page, self._key, self._rows, self._hits = page, key, tuple(rows), tuple(hits)
        self._empty = empty
        self.publication = object()
        return page

    def rows(self):
        self.page()
        return self._rows

    def _records(self, data):
        if self.w._live_session is None or not data.items:
            return data.items
        return self.w.library.search_records_for(data.items)

    def select(self, position):
        self.page()
        if self._hits:
            self.w.search_selection = self._hits[position].id

    def hit_key(self, position):
        return self._hits[position].id

    def select_hit(self, identity):
        self.page()
        for index, hit in enumerate(self._hits):
            if hit.id == identity:
                self.w.positions['Search'] = index
                self.w.search_selection = hit.id
                return True
        return False

    def _scope_label(self, page):
        w = self.w
        if page.guide_scope is None:
            return ''
        start, end = page.guide_scope.horizon
        group = group_label(w.strings, w.guide.group) + f' ({len(page.guide_scope.channels)})'
        return w.t('guide_range', group=group, start=time_label(start), end=time_label(end))

    def _coverage(self, page, *, compact=False):
        w = self.w
        for group in page.groups:
            c = group.coverage
            states = [key for flag, key in ((not group.has_snapshot and not (compact and c.unavailable), 'cache_none'),
                      (c.loading, 'live_loading'), (c.stale, 'dashboard_stale'),
                      (c.partial, 'search_partial'), (c.unavailable, 'unavailable'),
                      (c.capped, 'search_capped'), (group.truncated, 'truncated')) if flag]
            if group.has_snapshot:
                states.insert(0, 'live_empty' if self._empty[group.category] else
                              'search_no_matches' if page.query and not group.hits and group.coverage_complete
                              and not group.truncated else 'cache_fresh')
            label = {SearchCategory.GUIDE: 'guide', SearchCategory.SCHEDULED: 'scheduled'}.get(
                group.category, CATEGORIES[group.category]) if compact else CATEGORIES[group.category]
            yield f"{w.t(label)}: {', '.join(w.t(key).strip() for key in states)}"

    def paint_status(self):
        page = self.page()
        self.w._label(114, '  •  '.join(self._coverage(page, compact=True)))

    def render(self):
        w, page = self.w, self.page()
        selected = w._render_rows(self._rows)
        query = display_text(page.query, 80, w.strings)
        category = w.t(CATEGORIES[self.category])
        w._label(112, w.t('search_heading', query=query, group=category) if query else category)
        w._label(116, self._scope_label(page))
        if self._rows:
            status = 'search_rows_status'
        elif not page.query:
            status = 'search_query_prompt'
        elif all(g.coverage_complete and not g.truncated for g in page.groups):
            status = 'live_empty' if all(self._empty.values()) else 'search_no_matches'
        else:
            status = 'search_coverage'
        w._label(113, w.t(status, position=selected + 1, count=len(self._rows)))
        w._label(115, w.t('search_local') + (' | ' + w.t('demo_footer') if w.demo_allowed() else ''))
        self.paint_status()

    def _target(self):
        w, page = self.w, self.page()
        for group in page.groups:
            health = group.coverage
            if not group.has_snapshot or health.loading or health.stale or health.unavailable:
                continue
            for hit in group.hits:
                if hit.id == w.search_selection:
                    return hit.id
        return None

    def _entity(self, identity):
        """Re-read exact accepted identities, never bind by row index, title or label."""
        w = self.w
        if identity is None or identity != self._target():
            return None
        admitted = self._admission()
        if admitted is None:
            return None
        if isinstance(identity, ProgrammeId):
            product = w.guide.current_product
            airing_lookup = identity.channel, identity.event_id, identity.start
            if product is not None:
                schedule = product.schedules.get(identity.channel)
                index = schedule.positions.get(airing_lookup[1:]) if schedule is not None else None
                event = schedule.events[index] if schedule is not None and index is not None else None
            else:
                records = w._search_index.retirement_records if w._search_index is not None else ()
                event = next((event for event, _ in records if airing_key(event) == airing_lookup), None)
            return w.selected_indexed_event(event) if event is not None else None
        if isinstance(identity, RecordingId):
            records = self._records(admitted.recordings()) or ()
            recording_lookup = identity.namespace, identity.provenance, identity.recording_id
            return next((r for r in islice(records, MAX_LISTING_RECORDS) if recording_key(r) == recording_lookup), None)
        if isinstance(identity, ScheduledId):
            return next((j for j in islice(admitted.jobs().items, MAX_LISTING_RECORDS) if j.id == identity.job_id), None)
        return next((r for r in islice(w._dvr.search_data().items, MAX_LISTING_RECORDS) if r.key == identity.pass_id), None)

    def event(self):
        identity = self._target()
        return self._entity(identity) if isinstance(identity, ProgrammeId) else None

    def actionable(self):
        identity = self._target()
        return identity is not None and (not isinstance(identity, ProgrammeId) or self.w._guide_runtime.ready)

    def open_label(self):
        return 'search_open_recording' if isinstance(self.w.search_selection, RecordingId) else 'details'

    def reveal_label(self):
        identity = self._target()
        if isinstance(identity, ProgrammeId) and self.w._guide_runtime.ready:
            return 'show_in_guide'
        if isinstance(identity, (ScheduledId, PassId)):
            return 'search_show_dvr'
        return None

    def reveal(self, gui, origin, *, expected=None):
        w = self.w
        identity = self._target()
        entity = self._entity(identity) if expected is None or expected == identity else None
        if entity is None:
            dialog_for(w, gui).ok(w.t('search_result'), w.t('search_select_result'))
            return
        bookmark = SearchReturn(identity, self.category, w.search_query, w.positions.get('Search', 0), origin)
        with modal_input(w):
            if isinstance(identity, ProgrammeId):
                guide = w.guide.bookmark()
                if w.guide.focus_event(entity):
                    enter(w, 'Guide', guide_return=guide, search_return=bookmark)
            elif isinstance(identity, (ScheduledId, PassId)):
                dvr = w._dvr.bookmark()
                if isinstance(identity, PassId):
                    items = w._dvr.search_data().items
                    if not w._dvr.hit(items, identity.pass_id):
                        return
                    w._dvr.tab = 'dvr.passes'
                else:
                    admitted = self._admission()
                    if admitted is None:
                        return
                    items = admitted.jobs().items
                    index = next((i for i, job in enumerate(items) if job is entity), None)
                    if index is None:
                        return
                    w._dvr.tab = 'dvr.scheduled'
                    w._dvr_selected, w.positions['DVR'] = identity.job_id, index
                enter(w, 'DVR', dvr_return=dvr, search_return=bookmark)

    def details(self, gui, allow_reveal, origin):
        w = self.w
        identity = self._target()
        entity = self._entity(identity)
        dialog = dialog_for(w, gui)
        if entity is None:
            dialog.ok(w.t('search_result'), w.t('search_select_result'))
            return
        if allow_reveal and self.reveal_label() is not None:
            intent = w._chrome.command_activation('selection.open', 'details')
            choice = select(dialog, w.strings, 'search_result',
                            (('details', 'details'), ('reveal', self.reveal_label())))
            if choice is None or not w._owns_input() or not w._chrome.command_current(intent):
                return
            if self._entity(identity) is not entity:
                return
            if choice == 'reveal':
                self.reveal(gui, origin, expected=identity)
                return
        if isinstance(identity, ProgrammeId):
            w._show_event_details(entity)
            return
        if isinstance(identity, RecordingId):
            row = recording_row(entity, w.strings)
        elif isinstance(identity, PassId):
            row = PassRows((entity,), w.strings)[0]
        else:
            projection = ChannelProjection(w.strings).update(w.repository.channels)
            row = scheduled_row(entity, w.repository.events, projection, w.strings, demo=w.demo_allowed())
        dialog.textviewer(row.label, row.detail)

    def execute(self, gui, code, origin):
        w = self.w
        if code in ('search.programmes', 'search.context'):
            category = (SearchCategory.GUIDE if w.view == 'Guide' else
                        SearchCategory.RECORDINGS if w.view == 'Recordings' else
                        SearchCategory.PASSES if w._dvr.passes else SearchCategory.SCHEDULED)
            enter(w, 'Search', search_context=(category, origin))
            return
        if code == 'search.reveal':
            self.reveal(gui, origin)
            return
        if code == 'search.coverage':
            page = self.page()
            dialog_for(w, gui).textviewer(w.t('search_coverage'), '\n'.join(
                (w.t('search_local'), self._scope_label(page), *self._coverage(page))))
            return
        if code in TAB_COMMANDS:
            category = TAB_COMMANDS[code]
            if category is not self.category:
                with modal_input(w):
                    self.set_category(category)
                    route_fence(w, guide=False)
                    w.render()
                    w._chrome.initialize_focus(origin)
            return
        value = '' if code == 'search.clear' else self._query(gui)
        if value is not None:
            try:
                normalize_query(value)
            except ValueError:
                dialog_for(w, gui).ok(w.t('search'), w.t('search_limit'))
                return
            with modal_input(w):
                w._set_search_query(value)
                route_fence(w, guide=False)
                w.render()
                w._chrome.initialize_focus(origin)

    def _query(self, gui):
        w = self.w
        intent = w._chrome.command_activation('search.query', 'search')
        dialog = dialog_for(w, gui)
        presets = ('special', 'atlas', 'sample', 'no-match-xyz') if w.demo_allowed() else ()
        entries = [('enter', w.t('search_enter')), ('clear', w.t('search_clear'))]
        entries.extend((f'preset.{i}', w.t('demo_preset', query=preset)) for i, preset in enumerate(presets))
        chosen = command(tuple(entries), dialog.select(w.t('search'), [label for _, label in entries]))
        if chosen is None or not w._owns_input() or not w._chrome.command_current(intent):
            return None
        if chosen == 'enter':
            value = dialog.input(w.t('search_input'), defaultt=w.search_query, type=gui.INPUT_ALPHANUM)
            return value if value and w._owns_input() and w._chrome.command_current(intent) else None
        return '' if chosen == 'clear' else presets[int(chosen.removeprefix('preset.'))] if w.demo_allowed() else None
