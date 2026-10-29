"""Owner-only explicit DEV recording dialogs; worker owns HTTP and private I/O."""

import xbmcgui
import xbmcvfs

from .event_details_ui import event_start, frame_context, recording_scope
from .pass_browser import display_text
from .pass_ui import PassPreviewUI, _programme_scope
from .recording_worker import RecordingWorker
from .operation_storage import normalize_profile
from .ui_text import FALLBACK, text
from .ui_operation_errors import error_message, state_label, uncertainty
from .ui_menu import command


class RecordingUI:
    def __init__(self, window) -> None:
        self.window = window
        self.worker: RecordingWorker | None = None
        self.control_touched = False  # conservative for the rest of this window
        self._prepare_context = None
        self.preview = PassPreviewUI(self)

    def t(self, key, **values):
        return text(getattr(self.window, 'strings', FALLBACK), key, **values)

    def _choose(self, heading, entries):
        captured = tuple(entries)
        return command(captured, xbmcgui.Dialog().select(heading, [label for _, label in captured]))

    def _show(self, heading, body):
        try:
            return _programme_scope(self.window.path, heading, body,
                                    getattr(self.window, 'strings', FALLBACK)) is True
        except Exception:
            self.close()
            xbmcgui.Dialog().ok(self.t('recording'), self.t('message_failed') + '\n' + self.t('controls_blocked'))
            return False

    def close(self) -> None:
        self._prepare_context = None
        self.preview.close()
        if self.worker is not None:
            self.worker.close()  # signal only; never RecordingCommand.close()
            self.worker = None

    def _current_config(self):
        w = self.window
        try:
            if (w._closed or w.transient_demo_mode is True or w._live_session is None or w._development_blocked is not None
                    or w.get_setting("development_metadata_enabled") is not True):
                return None
            config = w._saved_config()
            return config if config == w._development_config else None
        except (RuntimeError, AttributeError, TypeError, ValueError):
            return None

    def _get_worker(self):
        config = self._current_config()
        if self.worker is not None and config != self.worker.config:
            self.close()
        if config is None:
            xbmcgui.Dialog().ok(self.t('recording'), self.t('connect_first'))
            return None
        if self.worker is None:
            self.control_touched = True
            try:
                # Kodi Omega getAddonInfo('profile') + xbmcvfs.translatePath.
                # This only obtains the owned path; no filesystem I/O on owner.
                profile = normalize_profile(xbmcvfs.translatePath(self.window._addon.getAddonInfo("profile")))
                self.worker = RecordingWorker(config, profile)
            except Exception:
                xbmcgui.Dialog().ok(self.t('recording'), self.t('profile_blocked'))
                return None
        return self.worker

    def _programme_profile(self):
        """Lazy owner-only SDK path lookup, with no filesystem I/O."""
        self.window._dispatcher.assert_owner()
        profile = xbmcvfs.translatePath(self.window._addon.getAddonInfo("profile"))
        try:
            return normalize_profile(profile)
        except Exception:
            raise ValueError("profile_unavailable") from None

    def request(self, schedule: bool) -> None:
        self.window._dispatcher.assert_owner()
        event = self.window.selected_indexed_event() if self.window.view == "Guide" else None
        if schedule and event is None:
            xbmcgui.Dialog().ok(self.t('recording'), self.t('future_selection'))
            return
        worker = self._get_worker()
        if worker is None:
            return
        if schedule and (event is None or not self.window._current_data().can_browse
                         or self.window._live_session.loading):
            xbmcgui.Dialog().ok(self.t('recording'), self.t('future_selection'))
            return
        context = frame_context(self.window) if schedule else None
        if not worker.submit("prepare" if schedule else "status", event=event):
            xbmcgui.Dialog().ok(self.t('recording'), self.t('pending_operation'))
        elif schedule:
            self._prepare_context = context

    def _prepared_current(self, worker: RecordingWorker | None, event) -> bool:
        if worker is None:
            return False
        return (self.worker is worker and not getattr(worker, '_closed', False)
                and self._current_config() == worker.config
                and self._prepare_context is not None
                and frame_context(self.window) == self._prepare_context
                and self.window.view == 'Guide' and event is not None
                and self.window.selected_indexed_event() == event)

    def poll(self) -> None:
        self.window._dispatcher.assert_owner()
        self.preview.poll()
        worker = self.worker
        if worker is None:
            return
        if self._current_config() != worker.config:
            self.close()
            return
        result = worker.poll()
        if result is None or result.operation == "abort":
            return
        if result.state == "prepared" and result.error is None:
            if not self._prepared_current(worker, result.event):
                if not worker.submit("abort"):
                    self.close()
                xbmcgui.Dialog().ok(self.t('recording'), self.t('recording_selection_changed'))
                return
            assert result.event is not None
            try:
                if recording_scope(self.window, result.event) is not True:
                    raise RuntimeError('scope_unavailable')
            except Exception:
                self.close()
                xbmcgui.Dialog().ok(self.t('recording'), self.t('recording_scope_failed'))
                return
            if not self._prepared_current(worker, result.event):
                self.close()
                return
            try:
                confirmed = xbmcgui.Dialog().yesno(
                    self.t('schedule_question'), self.t('schedule_confirm',
                        title=display_text(result.event.title, 80, getattr(self.window, 'strings', FALLBACK)) or self.t('untitled'), start=event_start(result.event)))
            except Exception:
                self.close()
                return
            # Scope dismissal is never confirmation. Recheck both modal boundaries.
            if not self._prepared_current(worker, result.event):
                self.close()
            elif confirmed is True:
                if not worker.submit("schedule", token=result.token, confirmed=True):
                    self.close()
            else:
                if not worker.submit("abort"):
                    self.close()
            return
        if result.error is not None:
            strings = getattr(self.window, 'strings', FALLBACK)
            xbmcgui.Dialog().ok(self.t('recording'), self.t('error_status',
                message=error_message(result.error, strings, 'recording'), state=state_label(result.state, strings)))
            if result.error == "checkpoint_failed":
                return  # storage uncertainty never offers read/write follow-up actions
        if result.state in {"create_unknown", "cancel_unknown"}:
            if not self._show(self.t('outcome_unknown'), uncertainty(result.state,
                    getattr(self.window, 'strings', FALLBACK), 'recording')):
                return
            if self.worker is not worker or self._current_config() != worker.config:
                self.close()
                return
            choice = self._choose(self.t('outcome_unknown'), (('status', self.t('status_read')),))
            if choice == 'status' and self.worker is worker and self._current_config() == worker.config:
                worker.submit("status")
        elif result.state == "owned" and result.can_cancel and result.error is None:
            choice = self._choose(self.t('recording_owned_heading'),
                                  (('cancel', self.t('cancel_this')), ('status', self.t('status_read'))))
            if choice == 'cancel':
                confirmed = xbmcgui.Dialog().yesno(self.t('cancel_question'), self.t('cancel_confirm'))
                if confirmed is True and self.worker is worker and self._current_config() == worker.config:
                    worker.submit("cancel", token=result.token, confirmed=True)
                elif self._current_config() != worker.config:
                    self.close()
            elif choice == 'status' and self.worker is worker and self._current_config() == worker.config:
                worker.submit("status")
        elif result.state == "cancelled" and result.error is None:
            choice = self._choose(self.t('cancelled_saved'),
                (('rollover', self.t('rollover')), ('history', self.t('archived_count', count=result.history_count))))
            if choice == 'rollover' and self.worker is worker and self._current_config() == worker.config:
                confirmed = xbmcgui.Dialog().yesno(
                    self.t('rollover_question'), self.t('rollover_confirm'))
                if self._current_config() != worker.config:
                    self.close()
                elif confirmed is True and self.worker is worker:
                    worker.submit("rollover", token=result.token, confirmed=True)
            elif choice == 'history' and self.worker is worker and self._current_config() == worker.config:
                self._show(self.t('recording_history'), self.t('recording_history_body', count=result.history_count))
        elif result.error is None:
            message = {'none': self.t('recording_ready' if result.history_count else 'recording_none'),
                       'owned': self.t('recording_ineligible')}
            reasons = {'near_start', 'rule_job', 'active_job', 'detail_unqualified', 'detail_unavailable'}
            reason = result.cancel_ineligible_reason
            body = message.get(result.state, self.t('saved_operation'))
            if result.state == 'owned' and reason in reasons:
                body += '\n' + self.t('cancel_reason', reason=self.t('cancel_' + reason))
            self._show(self.t('recording_status_title'), body + '\n' + self.t('archived_count', count=result.history_count))
