"""Owner-side DVR tabs and pass snapshots. Only explicit Series actions enter control review."""
from collections.abc import Mapping, Sequence
from typing import overload

from .browser import Row
from .normalization import DataState
from .pass_browser import display_text
from .series_http import segment
from .series_manager_models import GeneralSeriesPlan, ManagerError, ManagerView, RuleRow, TTL, rule_rows
from .series_manager_presentation import text_pages
from .series_manager_worker import SeriesManagerWorker
from .session import Presentation
from .ui_routes import presentation_scope, route_fence
from .ui_status import status_banner
from .ui_text import text

# Fixed, read-only subjects for the existing transient developer demo.
_DEMO_RULES = rule_rows([
    dict(ID='demo-pass-1', Paused=False, NumJobs=2, Priority=0, UpdatedAt=1,
         **GeneralSeriesPlan('SH00000001', 'Sample evening series').body()),
    dict(ID='demo-pass-2', Name='Sample custom pass', Paused=True,
         EQ={'SeriesID': 'SH00000002', 'ProgramID': 'EP000000020001'}),
])

TABS = {230: ('dvr.scheduled', 'scheduled'), 231: ('dvr.passes', 'dvr_passes')}
DVR_MORE = ('dvr.advanced', 'dvr.status', 'connection.refresh')
DVR_LABELS = dict((*TABS.values(), ('dvr.advanced', 'series_raw'),
                  ('dvr.manage', 'dvr_manage'), ('dvr.status', 'dvr_series_status'),
                  ('dvr.refresh', 'refresh')))


class PassRows(Sequence[Row]):
    """Decode text only for painted rows (or the explicitly selected rule)."""
    def __init__(self, items: tuple[RuleRow, ...], strings: Mapping[str, str]):
        self.items, self.strings = items, strings

    def __len__(self) -> int:
        return len(self.items)

    @overload
    def __getitem__(self, index: int) -> Row: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[Row, ...]: ...

    def __getitem__(self, index: int | slice) -> Row | tuple[Row, ...]:
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        rule = self.items[index]
        body = rule.payload()
        title = display_text(body.get('Name'), 120, self.strings) or text(self.strings, 'unknown')
        paused, jobs = body.get('Paused'), body.get('NumJobs')
        status = text(self.strings, 'dvr_pass_state',
                      paused=text(self.strings, ('yes' if paused else 'no') if type(paused) is bool else 'unknown'),
                      jobs=jobs if type(jobs) is int and jobs >= 0 else text(self.strings, 'unknown'))
        kind = text(self.strings, 'dvr_series_review' if rule.supported else 'dvr_pass_readonly')
        return Row(rule.key, f'{title} | {status}', f'{title}\n{status}\n{kind}')


class DvrUI:
    def __init__(self, window, worker_factory=SeriesManagerWorker):
        self.w = window
        self._factory = worker_factory
        self.worker: SeriesManagerWorker | None = None
        self._retiring: SeriesManagerWorker | None = None
        self.tab = 'dvr.scheduled'
        self._frame = None
        self._result: ManagerView | None = None
        self._observed: ManagerView | None = None  # admitted browse result, never a control result
        self._observed_source: tuple | None = None
        self._publication = object()
        self._stale = False
        self._selected: str | None = None
        self.position = 0
        self._selection_epoch = 0
        self.notice: str | None = None

    @property
    def passes(self):
        return self.w.view == 'DVR' and self.tab == 'dvr.passes'

    @property
    def child(self):
        return self.w.view == 'DVR' and self.w._route.parent == 'Search'

    def switch(self, tab, origin):
        w = self.w
        w._dispatcher.assert_owner()
        if w.view != 'DVR' or self.child or tab not in ('dvr.scheduled', 'dvr.passes') or tab == self.tab:
            return
        with w._dispatcher.modal_input():
            self.tab = tab
            self.refresh()
            route_fence(w)
            w.render()
            w._chrome.initialize_focus(origin)

    def config(self):
        return self._read_config() if self.passes and not self.child else None

    def _read_config(self):
        w = self.w
        return w._recording_ui._current_config() if w._guide_runtime._mode is None else None

    def _source(self, config):
        session = self.w._live_session
        if config is not None and session is not None and not getattr(session, '_version_blocked', False):
            return session.incarnation, config, session._generation, getattr(session, 'actual_version', None)
        return None

    def _scope(self):
        config = self.config()
        source = self._source(config)
        return (presentation_scope(self.w), *source) if source is not None else None

    def _stop(self):
        if self.worker is not None:
            self.worker.close()
            self._retiring = self.worker
            self.worker = None

    def refresh(self):
        self._stop()
        self._frame = self._result = None
        self._publication = object()
        self._stale = False

    def request_refresh(self):
        if self.worker is None:
            self._observed = self._observed_source = None
            self.refresh()

    def reset(self):
        self._observed = self._observed_source = None
        self.refresh()
        self._selected, self.position, self.notice = None, 0, None
        self._selection_epoch += 1

    def poll(self):
        w = self.w
        w._dispatcher.assert_owner()
        if not self.passes and self.notice == 'dvr_series_verified':
            self.notice = None  # Do not carry a prior success through Guide controls.
        scope = self._scope()
        changed = scope != self._frame
        if changed:
            self.refresh()
            self._frame = scope
        if self._retiring is not None:
            if not self._retiring.finished:
                return changed
            self._retiring = None
        if scope is None:
            return changed
        if self.worker is None:
            if self._result is not None:
                stale = self._result.error is None and self._aged(self._result)
                repaint = stale != self._stale
                self._stale = stale
                return changed or repaint
            try:
                self.worker = self._factory(scope[2], None)
                if not self.worker.submit('browse'):
                    raise RuntimeError('pass_read_not_started')
            except Exception:
                self._stop()
                self._result = ManagerView('browse', error='network')
                self._observed, self._observed_source = self._result, scope[1:]
            return True
        result = self.worker.poll()
        if result is None:
            return False
        self._stop()
        if scope != self._scope():
            self._frame = None
            return True
        self._result = (result if type(result) is ManagerView and result.operation == 'browse'
                        else ManagerView('browse', error='schema'))
        self._publication = object()
        self._observed, self._observed_source = self._result, scope[1:]
        if self._result.error is None:
            self._restore(self._result.rows)
        return True

    def _aged(self, result):
        return not 0 <= self.w.utc_now() - result.observed_at <= TTL

    def data(self) -> Presentation[RuleRow]:
        if self.child:
            return self.search_data()
        return self._present(self._scope(), local=False)

    def search_data(self) -> Presentation[RuleRow]:
        """Observe only a previously admitted DVR browse, never start one."""
        scope = self._source(self._read_config())
        return self._present(scope, local=True)

    def _present(self, scope, *, local) -> Presentation[RuleRow]:
        w = self.w
        if w.demo_allowed() and w._guide_runtime._mode is None:
            demo = w.data_session.jobs()
            items = _DEMO_RULES if demo.can_browse else ()
            return Presentation(items, demo.state, demo.loading, demo.stale, demo.updated_at,
                                demo.age_seconds, demo.banner, demo.message, bool(items))
        if scope is None:
            return Presentation((), DataState.OUTAGE, False, False, None, None,
                                w.t('not_connected'), w.t('connection_prompt'), False)
        result = (self._observed if self._observed_source == scope else None) if local else (
            self._result if self._frame == scope else None)
        loading = result is None and (not local or self.passes and not self.child)
        stamp, age, stale, items = None, None, False, ()
        if result is None:
            state, message = None, 'live_loading' if loading else 'no_listings'
        elif result.error is not None:
            state = (DataState.UNSUPPORTED if result.error in ('source', 'unsupported') else
                     DataState.MALFORMED if result.error == 'schema' else DataState.OUTAGE)
            message = {'unsupported': 'live_unsupported', 'malformed': 'live_malformed', 'outage': 'live_outage'}[state.value]
        else:
            stamp, items = result.observed_at, result.rows
            age = w.utc_now() - stamp
            stale = not 0 <= age <= TTL
            state = DataState.READY if items else DataState.EMPTY
            message = 'dvr_pass_stale' if stale else 'live_ready' if items else 'dvr_empty_passes'
        banner = status_banner(state, loading, stamp, age if age is None or age >= 0 else None,
                               stale, w.strings, compact=True)
        return Presentation(items, state, loading, stale, stamp, age, banner, w.t(message), bool(items))

    def _index(self, items):
        if self.position < len(items) and items[self.position].key == self._selected:
            return self.position
        return next((i for i, row in enumerate(items) if row.key == self._selected),
                    min(self.position, max(0, len(items) - 1)))

    def _restore(self, items):
        if items:
            self.select(items, self._index(items))

    def rows(self):
        items = self.data().items
        self._restore(items)
        return PassRows(items, self.w.strings)

    def select(self, items, index):
        if items:
            index = max(0, min(index, len(items)-1))
            if items[index].key != self._selected:
                self._selection_epoch += 1
            self.position, self._selected = index, items[index].key

    def selected(self, data=None):
        items = (data or self.data()).items
        return items[self._index(items)] if items else None

    def selection_identity(self, data):
        row = self.selected(data)
        return row.key if row is not None else None, self._selection_epoch

    def bookmark(self):
        return self.tab, self._selected, self.position, self.w._dvr_selected, self.w.positions.get('DVR', 0)

    def resume_search_origin(self):
        """Rebind an admitted listing to the restored primary frame, without I/O."""
        scope = self._scope()
        if scope is not None and scope[1:] == self._observed_source and self.worker is None:
            self._frame, self._result = scope, self._observed
            self._publication = object()

    def restore_bookmark(self, bookmark):
        self.tab, self._selected, self.position, self.w._dvr_selected, index = bookmark
        self.w.positions['DVR'] = index
        self._selection_epoch += 1

    def hit(self, items, key):
        for index, item in enumerate(items):
            if item.key == key:
                self.select(items, index)
                return True
        return False

    def snapshot(self, data):
        # Retain a phase-local publication token, not recyclable tuple IDs; include stale
        # state so an activation cannot be promoted by a later refresh.
        return self._scope(), self._publication, data.updated_at, data.state, data.stale, data.loading

    def manageable(self):
        if self.config() is None or self.w._live_session.loading or self.notice == 'dvr_series_pending':
            return None
        data = self.data()
        row = self.selected(data)
        if row is None or not row.supported or data.loading or data.stale:
            return None
        try:
            segment(row.key)
        except ManagerError:
            return None
        return row

    def request(self, manager, *, status=False):
        row = None if status else self.manageable()
        scope = self._scope()
        if scope is None or (not status and row is None):
            return
        snapshot, selection = self._result, self._selection_epoch
        source, config = scope[1:3]

        def source_current():
            w = self.w
            session = w._live_session
            return (session is not None and session.incarnation is source and w._guide_runtime._mode is None
                    and w._recording_ui._current_config() == config)

        def current():
            return (scope == self._scope() and (status or (self._result is snapshot
                    and self._selection_epoch == selection and self.selected() is row)))

        def finished(result):
            if result.error == 'pending':
                self.notice = 'dvr_series_pending'
            elif result.error is None:
                self.notice = 'dvr_series_verified'
            elif self.notice == 'dvr_series_verified':
                self.notice = None
            if result.error is None or result.error == 'stale':
                self._observed = self._observed_source = None
                self.refresh()
            self.w._paint_pending = True

        if manager.request_dvr(row, current, finished, source_current=source_current):
            if self.notice == 'dvr_series_verified':
                self.notice = None  # An earlier success says nothing about this request.

    def show(self, dialog, *, advanced=False):
        data, w = self.data(), self.w
        row = self.selected(data)
        if row is None:
            return
        heading = w.t('series_raw' if advanced else 'saved_details_title')
        if not advanced:
            detail = PassRows((row,), w.strings)[0].detail
            dialog.textviewer(heading, detail + '\n\n' + w.t('dvr_pass_stale' if data.stale else 'dvr_pass_observed'))
            return
        pages = text_pages(row.raw)
        index = 0
        scope = presentation_scope(w)
        snapshot, selection = self.snapshot(data), self.selection_identity(data)

        def current():
            data = self.data()
            return (self.passes and scope is presentation_scope(w) and snapshot == self.snapshot(data)
                    and selection == self.selection_identity(data))

        while True:
            dialog.textviewer(heading, pages[index])
            if len(pages) == 1 or not current():
                return
            directions = tuple(i for i in (-1, 1) if 0 <= index+i < len(pages))
            answer = dialog.select(heading, [w.t('previous_page' if i < 0 else 'next_page') for i in directions])
            if type(answer) is not int or not 0 <= answer < len(directions) or not current():
                return
            index += directions[answer]

    def render(self, data):
        w = self.w
        if self.child:
            w._label(112, w.t('dvr_passes' if self.passes else 'search_category_scheduled'))
        rows = w._rows() if data.can_browse else ()
        position = w._render_rows(rows)
        message = w.t('details_rows_status', position=position + 1, count=len(rows)) if rows else data.message
        w._label(113, message)
        if self.passes:
            notice = self.notice if self.config() is not None else None
            footer = ('demo_footer' if w.demo_allowed() else 'dvr_pass_stale'
                      if data.stale and notice != 'dvr_series_pending' else notice or 'dvr_pass_observed')
        else:
            footer = 'demo_scheduled_readonly' if w.demo_allowed() else 'scheduled_readonly'
        w._label(115, w.t(footer))
