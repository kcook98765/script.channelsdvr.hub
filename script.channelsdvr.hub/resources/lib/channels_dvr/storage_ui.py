"""Owner-only dialogs and a serial SQLite worker; no settings or backend access."""
from dataclasses import dataclass, field
import secrets
import threading

from .operation_storage import OperationStore, Scope, StorageError, normalize_profile
from .storage_migration import summaries
from .ui_text import FALLBACK, text
from .ui_operation_errors import state_label


@dataclass(frozen=True)
class StorageResult:
    action: str
    token: str | None = field(default=None, repr=False)
    rows: tuple = ()
    legacy: bool = False
    imported: bool = False
    error: bool = False
    scope: Scope | None = None


class StorageWorker:
    def __init__(self, profile):
        self.profile = normalize_profile(profile)
        self._condition = threading.Condition()
        self._closed = self._busy = self._failed = False
        self._intent = self._result = self._inspection = self._token = None
        self._thread: threading.Thread | None = None

    def submit(self, action='status', *, token=None, scope=None):
        if action not in {'status','import','recover'}:
            return False
        with self._condition:
            if self._closed or self._busy or (self._failed and action != 'status'):
                return False
            self._busy, self._result = True, None
            self._intent = (action, token, scope)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='channels-storage', daemon=True)
                self._thread.start()
            self._condition.notify_all()
            return True

    def poll(self):
        with self._condition:
            result, self._result = self._result, None
            return result

    def close(self):
        with self._condition:
            self._closed = True
            self._intent = None
            self._condition.notify_all()  # never join or perform database I/O on owner

    def _execute(self, action, token, scope):
        if action != 'status':
            from .control_lifetime import controls_quiet
            if self._failed or token is None or token != self._token or self._inspection is None or not controls_quiet():
                raise StorageError('checkpoint_failed')
            expected, self._token = self._inspection, None
            if action == 'import':
                OperationStore.import_legacy(self.profile, expected, confirmed=True)
            else:
                if scope is Scope.SERIES:
                    from .series_manager_store import manager_storage_rows
                    if not any(r[0] is Scope.SERIES and r[3] for r in manager_storage_rows(expected)):
                        raise StorageError('checkpoint_failed')
                OperationStore.recover_exclusive(self.profile, expected, scope, confirmed=True)
        inspection = OperationStore.inspect_profile(self.profile)
        from .series_manager_store import manager_storage_rows
        rows = summaries(inspection) + manager_storage_rows(inspection)
        self._inspection, self._token = inspection, secrets.token_hex(16)
        return StorageResult(action, self._token, rows, bool(inspection.legacy), bool(inspection.provenance), scope=scope)

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._intent is not None)
                if self._closed:
                    return
                intent, self._intent = self._intent, None
            if intent is None:
                continue
            try:
                result = self._execute(*intent)
            except Exception:
                self._failed, self._token, self._inspection = True, None, None
                result = StorageResult(intent[0], error=True)
            with self._condition:
                if not self._closed:
                    self._result = result
                self._busy = False


class StorageUI:
    def __init__(self, window):
        self.window = window
        self.worker: StorageWorker | None = None

    def t(self, key, **values):
        return text(getattr(self.window,'strings',FALLBACK), key, **values)

    def request(self):
        import xbmcgui
        import xbmcvfs
        self.window._dispatcher.assert_owner()
        try:
            if self.worker is None:
                profile = normalize_profile(xbmcvfs.translatePath(self.window._addon.getAddonInfo('profile')))
                self.worker = StorageWorker(profile)
            if not self.worker.submit('status'):
                xbmcgui.Dialog().ok(self.t('storage_title'),self.t('storage_busy'))
        except Exception:
            xbmcgui.Dialog().ok(self.t('storage_title'),self.t('storage_failed'))

    def close(self):
        if self.worker is not None:
            self.worker.close()
            self.worker = None

    def body(self, result):
        lines = []
        for scope, state, count, claimed, pending in result.rows:
            lines.append(self.t('storage_status_line', scope=self.t('series_manager' if scope is Scope.SERIES else 'recording' if scope is Scope.RECORDING else 'programme_tools'),
                state=state_label(state,getattr(self.window,'strings',FALLBACK)), count=count,
                claim=self.t('yes' if claimed else 'no'), pending=self.t('yes' if pending else 'no')))
        if result.legacy or result.imported:
            lines.append(self.t('storage_imported' if result.imported else 'storage_legacy'))
        return '\n\n'.join(lines + [self.t('storage_local_warning')])

    def poll(self):
        self.window._dispatcher.assert_owner()
        try:
            self._poll()
        except Exception:
            self.close()
            if self.window._closed:
                return
            try:
                import xbmcgui
                xbmcgui.Dialog().ok(self.t('storage_title'),self.t('storage_failed'))
            except Exception:
                pass  # Unsupported dialog must not break the Guide or leak details.

    def _poll(self):
        import xbmcgui
        from .ui_menu import command
        self.window._dispatcher.assert_owner()
        worker = self.worker
        if worker is None:
            return
        result = worker.poll()
        if result is None or self.window._closed:
            return
        dialog, title = xbmcgui.Dialog(), self.t('storage_title')
        if result.error:
            self.close()
            dialog.ok(title,self.t('storage_failed'))
            return
        if result.action != 'status':
            dialog.ok(title, (self.t('series_manager')+'\n'+self.t('status_read')+'\n'+self.t('storage_local_warning'))
                      if result.scope is Scope.SERIES else self.t('storage_done'))
            return
        entries = [('view','storage_view'),('refresh','storage_refresh')]
        for scope, _, _, claim, _ in result.rows:
            if claim:
                entries.append((scope,'series_recover' if scope is Scope.SERIES else 'storage_recover_recording' if scope is Scope.RECORDING else 'storage_recover_programme'))
        if result.legacy and not result.imported:
            entries.append(('import','storage_import'))
        captured = tuple(entries)
        action = command(captured, dialog.select(title,[self.t(key) for _,key in captured]))
        if self.window._closed or self.worker is not worker:
            return
        if action == 'view':
            dialog.ok(title,self.body(result))
        elif action == 'refresh':
            worker.submit('status')
        elif action is not None:
            if dialog.yesno(title,self.t('storage_exclusive'),defaultbutton=xbmcgui.DLG_YESNO_NO_BTN) is not True:
                return
            if self.window._closed or self.worker is not worker:
                return
            warning = 'storage_import_warning' if action == 'import' else 'storage_reconcile_warning'
            message = (self.t('series_recover')+'\n'+self.t('storage_local_warning')+'\n'+self.t('status_read')
                       +'\n'+self.t('controls_blocked')) if action is Scope.SERIES else self.t(warning)
            if dialog.yesno(title,message,defaultbutton=xbmcgui.DLG_YESNO_NO_BTN) is not True:
                return
            if self.window._closed or self.worker is not worker:
                return
            self.window._recording_ui.close()
            series = getattr(self.window, '_series_ui', None)
            if series is not None:
                series.close()  # Signal only. Worker guard waits for actual ticket completion, never steals it.
            if self.window._closed or self.worker is not worker:
                return
            if not worker.submit('import' if action == 'import' else 'recover',token=result.token,
                                 scope=action if type(action) is Scope else None):
                dialog.ok(title,self.t('storage_busy'))
