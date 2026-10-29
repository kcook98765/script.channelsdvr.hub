"""Small owner-thread preview coordinator; transport and parsing stay on worker."""

import xbmcgui
import time
from types import SimpleNamespace

from .pass_browser import PreviewSnapshot, page_entries, utc
from .pass_preview import PassPreviewWorker, PreviewResult
from .saved_passes import SavedSnapshot, page_entries as saved_entries, snapshot_age, saved_list_heading
from .saved_passes_worker import SavedPassesWorker
from .programme_pass_ui import ProgrammePassUI, SourceScope
from .programme_pass_worker import ProgrammePassWorker
from .ui_menu import PROGRAMME, select
from .ui_text import FALLBACK, text
from .ui_operation_errors import error_message

_SAVED_SCOPE_WARNING = FALLBACK['saved_scope_warning']


def _programme_scope(addon_path, heading, text, strings=FALLBACK):
    # Import only on explicit controls scope opening, never in pure coordinators.
    from .programme_scope_ui import show_scope
    return show_scope(addon_path, heading, text, strings=strings)


class PassPreviewUI:
    def __init__(self, recording_ui) -> None:
        self.recording_ui = recording_ui
        self.window = recording_ui.window
        self.worker: PassPreviewWorker | SavedPassesWorker | None = None
        self.generation = 0
        self.browser: PreviewSnapshot | SavedSnapshot | None = None
        self._scope = None
        self.controls: ProgrammePassUI | None = None  # lazy explicit controls choice

    @property
    def strings(self):
        return getattr(self.window, 'strings', FALLBACK)

    def t(self, key, **values):
        return text(self.strings, key, **values)

    def close(self) -> None:
        if self.controls is not None:
            self.controls.close()
            self.controls = None
        self._read_close()

    def _read_close(self) -> None:
        self.generation += 1
        self.browser = None
        self._scope = None
        if self.worker is not None:
            self.worker.close()
            self.worker = None

    def _selection(self):
        w = self.window
        return w.selected_indexed_event() if w.view == "Guide" else None

    def request(self) -> None:
        self.window._dispatcher.assert_owner()
        config = self.recording_ui._current_config()
        event = self._selection()
        if config is None or self.window._live_session is None:
            xbmcgui.Dialog().ok(self.t('preview_title'), self.t('connect_first'))
            return
        if self.worker is not None or self.browser is not None:
            pending = self.worker or SimpleNamespace(config=config, selected=event)
            generation = self.generation
            if not self._valid(pending, generation):
                self.close()
                return
            xbmcgui.Dialog().ok(self.t('preview_title'), self.t('preview_pending'))
            if not self._valid(pending, generation):
                self.close()
            return
        self._read_close()
        self._scope = self._data_scope()
        admission = SimpleNamespace(config=config, selected=event)
        generation = self.generation
        strings = getattr(self.window, 'strings', FALLBACK)
        selected = select(xbmcgui.Dialog(), strings, 'programme_tools', PROGRAMME)
        choice = {'programme.preview': 0, 'programme.saved': 1, 'programme.prepare': 2,
                  'programme.status': 3, 'programme.history': 4}.get(selected)
        if choice in (2, 3, 4):
            self.recording_ui.control_touched = True
            if (self.generation != generation or not self._same_scope()
                    or self.recording_ui._current_config() != config):
                self.close()
                return
            if choice == 2 and not self._valid(admission, generation):
                self.close()
                return
            if self.controls is None:
                self.controls = ProgrammePassUI(assert_owner=self.window._dispatcher.assert_owner,
                    dialog=xbmcgui.Dialog, profile=self.recording_ui._programme_profile,
                    config=self.recording_ui._current_config, context=self._control_context,
                    worker_factory=lambda config, profile: ProgrammePassWorker(config, profile, strings=strings),
                    scope_display=lambda heading, text: _programme_scope(self.window.path, heading, text, strings),
                    message_display=lambda heading, text: _programme_scope(self.window.path, heading, text, strings),
                    strings=strings)
            self._read_close()
            if choice == 4:
                self.controls.request_history()
            else:
                self.controls.request(choice == 2)
            return
        if not self._valid(admission, generation) or choice not in (0, 1):
            self._read_close()
            return
        factory = PassPreviewWorker if choice == 0 else SavedPassesWorker
        self.worker = factory(config, event, generation, strings=strings)
        self.worker.start()

    def poll(self) -> None:
        self.window._dispatcher.assert_owner()
        if self.controls is not None:
            self.controls.poll()
        worker = self.worker
        if worker is None:
            return
        if (not self._same_scope()
                or self.recording_ui._current_config() != worker.config
                or self._selection() != worker.selected
                or not self.window._current_data().can_browse or self.window._live_session.loading):
            self._read_close()
            return
        result = worker.poll()
        if result is None:
            return
        if (result.generation != self.generation or result.event != self._selection()
                or not self._valid(worker, self.generation)):
            self._read_close()
            return
        # Retire transport before any modal; retain the admission scope until Back.
        worker.close()
        self.worker = None
        generation = self.generation
        if isinstance(worker, SavedPassesWorker):
            try:
                self._saved_result(worker, generation, result)
            finally:
                self._read_close()
            return
        if not isinstance(result, PreviewResult):
            self._read_close()
            return
        if result.error is not None:
            message = error_message(result.error, self.strings, 'read')
        elif result.snapshot is not None and result.count == len(result.snapshot.matches) and result.count:
            self.browser = result.snapshot
            try:
                self._browse(worker, generation)
            finally:
                self._read_close()
            return
        elif result.count == 0 and result.snapshot is not None and not result.snapshot.matches:
            message = self.t('preview_empty', count=result.count)
        else:
            message = error_message('schema_unqualified', self.strings, 'read')
        xbmcgui.Dialog().ok(self.t('preview_title'), message + '\n' + self.t('preview_notice'))
        # Revalidate after the modal too; no follow-up or re-request on dismissal.
        self._valid(worker, generation)
        self._read_close()

    def _data_scope(self):
        session = self.window._live_session
        repository = getattr(session, "repository", None)
        return ((session, session._generation, repository,
                 getattr(repository, "events", None), getattr(repository, "channels", None))
                if session is not None else None)

    def _control_context(self):
        session = self.window._live_session
        repository = getattr(session, "repository", None)
        # Retain references and compare identity, not DTO equality or recyclable IDs.
        scope = SourceScope(session, getattr(session, "_generation", None), repository,
                            getattr(repository, "events", None), getattr(repository, "channels", None),
                            getattr(session, "loading", None))
        event = self._selection()
        qualified = (event is not None and session is not None and not session.loading
                     and self.window._current_data().can_browse)
        return scope, event, qualified

    def _same_scope(self):
        current = self._data_scope()
        return (self._scope is not None and current is not None
                and self._scope[1] == current[1]
                and all(self._scope[i] is current[i] for i in (0, 2, 3, 4)))

    def _valid(self, worker, generation) -> bool:
        return (self.generation == generation and self.generation == getattr(worker, "generation", generation)
                and self._same_scope()
                and self.recording_ui._current_config() == worker.config
                and self._selection() == worker.selected
                and self.window._current_data().can_browse
                and self.window._live_session is not None and not self.window._live_session.loading)

    def _browse(self, worker, generation) -> None:
        snapshot = self.browser
        if not isinstance(snapshot, PreviewSnapshot):
            return
        page, bookmark = 0, 0
        bookmarks = {}
        while self._valid(worker, generation):
            labels, indices = page_entries(snapshot, page, self.strings)
            age = int(time.time()) - snapshot.observed_at
            age_label = self.t('age_seconds', count=age) if age >= 0 else self.t('age_unknown')
            heading = self.t('preview_heading', start=page * 50 + 1,
                stop=min((page + 1) * 50, len(snapshot.matches)), count=len(snapshot.matches),
                age=age_label, time=utc(snapshot.observed_at))
            choice = xbmcgui.Dialog().select(heading, list(labels), preselect=bookmark)
            if not self._valid(worker, generation) or type(choice) is not int or not 0 <= choice < len(indices):
                return
            selected = indices[choice]
            if selected in ("next", "previous"):
                bookmarks[page] = bookmark
                page += 1 if selected == "next" else -1
                bookmark = bookmarks.get(page, 0)
                continue
            bookmark = choice
            if not self._valid(worker, generation):
                return
            xbmcgui.Dialog().textviewer(self.t('preview_details'), snapshot.details[selected])
            if not self._valid(worker, generation):
                return

    def _saved_result(self, worker, generation, result) -> None:
        if not self._valid(worker, generation):
            return
        snapshot = result.snapshot
        if result.error is not None or not isinstance(snapshot, SavedSnapshot):
            message = error_message(result.error or 'schema_unqualified', self.strings, 'read')
        elif not snapshot.rules:
            message = (self.t('saved_empty') + '\n' + self.t('saved_scope') + '\n'
                        + snapshot_age(snapshot, int(time.time()), self.strings))
        else:
            self.browser = snapshot
            if not self._valid(worker, generation):
                return
            xbmcgui.Dialog().ok(self.t('saved_scope_title'), self.t('saved_scope_warning'))
            if not self._valid(worker, generation):
                return
            self._browse_saved(worker, generation, snapshot)
            return
        if self._valid(worker, generation):
            if result.error is None and isinstance(snapshot, SavedSnapshot) and not snapshot.rules:
                xbmcgui.Dialog().textviewer(self.t('saved_title'), message)
            else:
                xbmcgui.Dialog().ok(self.t('saved_title'), message)
            self._valid(worker, generation)

    def _browse_saved(self, worker, generation, snapshot) -> None:
        page, bookmark = 0, 0
        bookmarks = {}
        while self._valid(worker, generation):
            labels, indices = saved_entries(snapshot, page, self.strings)
            heading = saved_list_heading(snapshot, page, int(time.time()), self.strings)
            if not self._valid(worker, generation):
                return
            choice = xbmcgui.Dialog().select(heading, list(labels), preselect=bookmark)
            if not self._valid(worker, generation) or type(choice) is not int or not 0 <= choice < len(indices):
                return
            selected = indices[choice]
            if selected in ("next", "previous"):
                bookmarks[page] = bookmark
                page += 1 if selected == "next" else -1
                bookmark = bookmarks.get(page, 0)
                continue
            bookmark = choice
            if not self._valid(worker, generation):
                return
            text = snapshot_age(snapshot, int(time.time()), self.strings) + "\n\n" + snapshot.details[selected]
            if not self._valid(worker, generation):
                return
            xbmcgui.Dialog().textviewer(self.t('saved_details_title'), text)
            if not self._valid(worker, generation):
                return
