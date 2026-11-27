"""Owner-side startup and bookmark coordination; only the mailbox worker does I/O."""
from dataclasses import replace
from threading import Condition, Thread
from typing import Any

from .bookmark_storage import BookmarkReadState, BookmarkStore
from .browser import recording_key
from .guide import Selection
from .hub_bookmarks import BookmarkScope, HubBookmarks, StartupAdmission, startup_destination
from .live_session import LiveDataSession
from .normalization import DataState
from .operation_storage import StorageError
from .ui_routes import enter

_PRIMARY = ('Dashboard', 'Guide', 'Recordings', 'DVR')
_DVR_TABS = ('scheduled', 'passes')
_POSITIVE = (DataState.READY, DataState.EMPTY)


def preference(w, key: str) -> str:
    try:
        value = w.get_setting(key)
        return value if type(value) is str else ''
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ''


class BookmarkWorker:
    """One serial worker, one latest pending operation, no GUI or DVR access."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._closed = False
        self._thread: Thread | None = None
        self._store: BookmarkStore | None = None
        self._pending: tuple[BookmarkStore, HubBookmarks | None] | None = None
        self._loaded: tuple[BookmarkStore, HubBookmarks | BookmarkReadState] | None = None

    def bind(self, store: BookmarkStore | None) -> bool:
        with self._condition:
            if self._closed:
                return False
            self._store, self._loaded = store, None
            self._pending = (store, None) if store is not None else None
            if store is not None and self._thread is None:
                self._thread = Thread(target=self._loop, name='hub-bookmarks', daemon=True)
                try:
                    self._thread.start()
                except RuntimeError:
                    self._thread = self._store = self._pending = None
                    return False
            self._condition.notify_all()
            return True

    def submit(self, store: BookmarkStore, snapshot: HubBookmarks) -> None:
        with self._condition:
            if not self._closed and store is self._store:
                self._pending = store, snapshot
                self._condition.notify_all()

    def take(self) -> tuple[BookmarkStore, HubBookmarks | BookmarkReadState] | None:
        with self._condition:
            loaded, self._loaded = self._loaded, None
            return loaded

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._loaded = None
            if self._pending is not None and self._pending[1] is None:
                self._pending = None
            self._condition.notify_all()  # no join or filesystem I/O on the owner

    def _loop(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                pending, self._pending = self._pending, None
                if pending is None:
                    return
            store, snapshot = pending  # lease taken before any later binding change
            if snapshot is not None:
                store.save(snapshot)
                continue
            loaded = store.read()
            with self._condition:
                if not self._closed and store is self._store:
                    self._loaded = store, loaded


class HubSessionUI:
    def __init__(self, window, *, worker=None) -> None:
        self.w = window
        self._worker = BookmarkWorker() if worker is None else worker
        self._startup = None
        self.startup_failed = False
        self._binding: tuple | None = None
        self._store: BookmarkStore | None = None
        self._model: HubBookmarks | None = None
        self._loaded = False
        self._writes_allowed = False
        self._overlay: dict[str, Any] = {}
        self._unrestored: set[str] = set()
        self._touched: set[str] = set()
        self._touch_binding: tuple | None = None

    def arm_startup(self, config) -> None:
        self._startup = config

    def cancel_startup(self) -> None:
        self._startup = None

    def source_changed(self) -> None:
        self.cancel_startup()
        self.startup_failed = False
        self._forget_source()
        self._sync_touches(None)

    def _forget_source(self) -> None:
        if self._store is not None:
            self._worker.bind(None)
        self._binding = self._store = self._model = None
        self._loaded = False
        self._writes_allowed = False
        self._overlay.clear()
        self._unrestored.clear()

    def _sync_touches(self, binding: tuple | None) -> None:
        if binding != self._touch_binding:
            self._touch_binding = binding
            self._touched.clear()

    def keep_tab(self, view: str) -> None:
        """An accepted explicit tab choice wins even before its bookmark read."""
        binding, _ = self._source()
        self._sync_touches(binding)
        if binding is not None:
            self._touched.add(view)
            if binding == self._binding:
                self._unrestored.discard(view)

    def close(self) -> None:
        # Retain the last already authorized save only if the source is still current.
        if self._binding is not None:
            binding, admission = self._source()
            if self._binding != binding or admission is not StartupAdmission.ADMITTED:
                self._forget_source()
        self._worker.close()

    def _source(self) -> tuple[tuple | None, StartupAdmission]:
        w = self.w
        runtime, session = w._guide_runtime, w._live_session
        if runtime._mode is not None:
            return None, StartupAdmission.PENDING
        if not isinstance(session, LiveDataSession):
            return None, StartupAdmission.UNAVAILABLE
        config = w._recording_ui._current_config()
        if config is None or w.transient_demo_mode:
            return None, StartupAdmission.UNAVAILABLE
        binding = config, session.incarnation
        if session.loading or runtime._request is not None or session._generation is None:
            return binding, StartupAdmission.PENDING
        if (session.last_error is not None or session.guide_origin is None
                or any(any(session.summary_health(resource)) for resource in ('channels', 'events'))):
            return binding, StartupAdmission.UNAVAILABLE
        return binding, StartupAdmission.ADMITTED

    def poll(self) -> bool:
        w = self.w
        binding, admission = self._source()
        self._sync_touches(binding)
        if self._binding is not None and binding != self._binding:
            self._forget_source()
        if admission is StartupAdmission.PENDING:
            return False
        if self._startup is not None and (binding is None or self._startup != binding[0]
                                          or admission is not StartupAdmission.ADMITTED):
            self.startup_failed = binding is not None and self._startup == binding[0]
            self.cancel_startup()  # the window already starts on Dashboard
            return self.startup_failed
        if binding is None or admission is not StartupAdmission.ADMITTED:
            return False
        changed_hint, self.startup_failed = self.startup_failed, False
        if self._binding is None:
            scope = BookmarkScope.from_config(binding[0])
            self._binding, self._model = binding, HubBookmarks(scope)
            try:
                self._store = BookmarkStore(w._recording_ui._programme_profile(), scope)
            except (RuntimeError, AttributeError, TypeError, ValueError, StorageError):
                self._store = None
            if self._store is not None and not self._worker.bind(self._store):
                self._store = None
            self._loaded = self._store is None
        completed = self._worker.take() if not self._loaded else None
        if completed is not None and completed[0] is self._store:
            assert self._model is not None
            saved = completed[1]
            if isinstance(saved, HubBookmarks) and saved.source == self._model.source:
                base = saved
                self._writes_allowed = True
            else:
                base = HubBookmarks(self._model.source)
                self._writes_allowed = saved is BookmarkReadState.MISSING
            self._model = replace(base, **self._overlay)
            self._loaded = True
            self._restore_pending()
            if self._overlay and self._model != base:
                self._save()
            self._overlay.clear()
        if not self._loaded or not w._owns_input() or not w._dispatcher.startup_idle():
            return changed_hint
        changed = self._restore() or changed_hint
        if self._startup is not None:
            if not w._guide_runtime.ready and not w._guide_runtime.error:
                return changed
            assert self._model is not None
            target = startup_destination(preference(w, 'hub_startup_view'), admission=admission,
                intent_current=True, source=self._model.source, bookmarks=self._model)
            if target and target != w.view:
                with w._dispatcher.startup_transition() as claimed:
                    if claimed:
                        self.cancel_startup()
                        enter(w, target, automatic=True)
                        return False  # enter already painted the route and initialized focus
                return changed
            self.cancel_startup()
        return changed

    def _restore_pending(self) -> None:
        self._unrestored = {'Guide', 'Recordings', 'DVR'} - self._touched

    def _restore(self) -> bool:
        w, model = self.w, self._model
        if not self._unrestored or model is None or w._route.parent is not None:
            return False
        changed = False
        if 'Guide' in self._unrestored and w._guide_runtime.ready:
            guide = w.guide
            if model.guide_group is not None or model.guide_channel is not None:
                group = model.guide_group if model.guide_group in guide.repository.groups else 'All'
                guide.set_group(group)
                channel = guide.channel(model.guide_channel) if model.guide_channel is not None else None
                if group != 'All' and (not guide.channels or (model.guide_channel is not None and channel is None)):
                    group = 'All'
                    guide.set_group(group)
                    channel = guide.channel(model.guide_channel) if model.guide_channel is not None else None
                now = w.utc_now()
                start, end = guide.repository.horizon
                anchor = min(max(now, start), end - 1)
                guide.restore_bookmark((group, Selection(channel.key if channel else None, None, anchor),
                                        0, anchor - anchor % 1800))
                changed = True
            self._unrestored.discard('Guide')
        if 'Recordings' in self._unrestored:
            data = w._live_session.recordings()
            if data.state in _POSITIVE and not data.stale:
                browser = w.library
                # The current route need not have visited the library yet.
                browser.update(data.items)
                browser.set_mode('Shows' if model.recordings_category == 'Search' else model.recordings_category)
                browser.set_provenance_filter(model.recordings_filter)
                if model.recordings_sort is not None:
                    browser.set_sort(model.recordings_sort)
                if model.recording_selection is not None:
                    browser.restore_recording_selection(model.recording_selection)
                self._unrestored.discard('Recordings')
                changed = True
        if 'DVR' in self._unrestored:
            tab = model.dvr_target(model.source, _DVR_TABS)
            if tab is not None:
                w._dvr.tab = 'dvr.' + tab
                w._dvr.refresh()
                changed = True
            self._unrestored.discard('DVR')
        return changed

    def input_state(self) -> tuple | None:
        w = self.w
        if self._binding is None or w._route.parent is not None:
            return None
        if w.view == 'Guide' and w._guide_runtime.ready:
            return w.view, w.guide.group, w.guide.selection
        if w.view == 'Recordings':
            browser = w.library
            return w.view, browser.navigation_scope, browser.selected.id if browser.selected else None
        if w.view == 'DVR':
            return w.view, w._dvr.tab
        return (w.view,)

    def note_input(self, before: tuple | None) -> None:
        after = self.input_state()
        model, w = self._model, self.w
        if before is None or after is None or before == after or model is None or w._closed:
            return
        binding, admission = self._source()
        if binding != self._binding:
            return
        view = after[0]
        if view == before[0] and view in ('Guide', 'Recordings', 'DVR'):
            self._touched.add(view)
            self._unrestored.discard(view)
        if admission is not StartupAdmission.ADMITTED:
            return
        fields: dict[str, Any] = {}
        if view != before[0] and view in _PRIMARY:
            fields = {'last_view': view}
        elif view == before[0] and view in ('Guide', 'Recordings', 'DVR'):
            if view == 'Guide' and len(before) == len(after) == 3:
                if (before[1], before[2].channel) != (after[1], after[2].channel):
                    fields = {'guide_group': after[1], 'guide_channel': after[2].channel}
            elif view == 'Recordings' and w.library.mode != 'Search':
                data = w._live_session.recordings()
                if data.state in _POSITIVE and not data.stale:
                    record = w.library.selected_recording()
                    fields = dict(recordings_category=w.library.mode, recordings_filter=w.library.provenance_filter,
                                  recordings_sort=w.library.sort_order,
                                  recording_selection=recording_key(record) if record is not None and record in data.items else None)
            elif view == 'DVR':
                fields = {'dvr_tab': w._dvr.tab.removeprefix('dvr.')}
        if fields:
            self._model = replace(model, **fields)
            if not self._loaded:
                self._overlay.update(fields)
            elif self._model != model:
                self._save()

    def _save(self) -> None:
        if self._writes_allowed and self._store is not None and self._model is not None:
            self._worker.submit(self._store, self._model)
