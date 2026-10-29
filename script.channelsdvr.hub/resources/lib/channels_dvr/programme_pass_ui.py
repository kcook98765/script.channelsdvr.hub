"""Plain owner coordinator. Dialog/profile/config/scope are injected; no Kodi imports."""
from dataclasses import dataclass, field

from .programme_pass_worker import ProgrammePassWorker
from .pass_browser import display_text, utc
from .programme_pass_history import ProgrammeHistoryResult, ProgrammeHistorySnapshot
from .ui_text import FALLBACK, text
from .ui_operation_errors import error_message, uncertainty
from .ui_menu import command

SCOPE = FALLBACK['programme_scope']


def lifecycle_scope(result, strings=FALLBACK):
    if result.state == 'prepared':
        current = max(0, result.sequence - 1)
        prospective = result.history_count + int(current > 0)
        return text(strings, 'lifecycle_prepared', current=current, count=result.history_count,
                    sequence=result.sequence, prospective=prospective) + text(strings, 'programme_capacity')
    body = text(strings, 'lifecycle_status', sequence=result.sequence, count=result.history_count)
    body += text(strings, 'programme_capacity')
    if result.state in ('create_unknown', 'remove_unknown') or result.error is not None:
        return body
    return (body + text(strings, 'lifecycle_next', sequence=result.next_sequence,
                count=result.history_count + int(result.sequence > 0))
            + text(strings, 'prepare_available' if result.can_prepare else 'prepare_unavailable'))


def friendly_error(code, strings=FALLBACK):
    """Closed local copy only: never display an arbitrary transport/backend string."""
    return error_message(code, strings)


def prepared_scope(result, strings=FALLBACK):
    return text(strings, 'prepared_scope', title=display_text(result.event.title, 512, strings),
                start=utc(result.event.start), end=utc(result.event.end),
                lifecycle=lifecycle_scope(result, strings), scope=text(strings, 'programme_scope'),
                confirmation=text(strings, 'scope_confirmation'))


@dataclass(frozen=True, eq=False)
class SourceScope:
    session: object = field(repr=False)
    generation: object
    repository: object = field(repr=False)
    events: object = field(repr=False)
    channels: object = field(repr=False)
    loading: object

    def __eq__(self, other):
        return (type(other) is SourceScope and self.generation == other.generation
                and self.loading == other.loading and self.session is other.session
                and self.repository is other.repository and self.events is other.events
                and self.channels is other.channels)


class ProgrammePassUI:
    def __init__(self, *, assert_owner, dialog, profile, config, context, worker_factory=ProgrammePassWorker,
                 scope_display=None, message_display=None, strings=FALLBACK):
        self._owner, self._dialog, self._profile = assert_owner, dialog, profile
        self._config, self._context, self._factory = config, context, worker_factory
        self._scope_display = scope_display
        self._message_display = message_display
        self.worker: ProgrammePassWorker | None = None
        self._generation = 0
        self._scope = self._event = self._binding = None
        self._prepare = False
        self.strings = strings

    def t(self, key, **values):
        return text(self.strings, key, **values)

    def close(self):
        self._generation += 1
        if self.worker is not None:
            self.worker.close()  # signal only, never join or command/journal close
        self.worker = None
        self._scope = self._event = self._binding = None

    def _valid(self, generation):
        self._owner()
        scope, event, qualified = self._context()
        return (generation == self._generation and self.worker is not None
                and self._config() == self._binding and self._binding is not None
                and scope == self._scope and (not self._prepare or (qualified and event == self._event)))

    def request_history(self):
        self._request(False, history=True)

    def request(self, prepare):
        self._request(prepare)

    def _request(self, prepare, *, history=False):
        self._owner()
        config = self._config()
        scope, event, qualified = self._context()
        operation = 'history' if history else 'prepare' if prepare else 'status'
        if config is None or (prepare and not qualified):
            message = self.t('connect_first' if config is None else 'pass_selection_unavailable')
            self._dialog().ok(self.t('programme'), message)
            return
        if self.worker is not None:
            # Explicit same-context status can reuse the held lock; no new worker.
            if self._valid(self._generation):
                if self._queue(self._generation, self.worker, operation,
                               **({} if history else dict(event=event if prepare else None))):
                    if not history:
                        self._prepare, self._event = prepare, event
                return
            self.close()
        self._scope, self._event, self._binding, self._prepare = scope, event, config, prepare
        try:
            self.worker = self._factory(config, self._profile())
        except Exception:
            self.close()
            self._dialog().ok(self.t('programme'), self.t('profile_blocked'))
            return
        generation = self._generation
        if not self._valid(generation):
            self.close()
            return
        if not self._queue(generation, self.worker, operation,
                            **({} if history else dict(event=event if prepare else None))):
            self.close()  # no accepted admission: discard this provisional context

    def _queue(self, generation, worker: ProgrammePassWorker | None, operation, **kwargs):
        if not self._valid(generation) or worker is None or self.worker is not worker:
            self.close()
            return False
        if worker.submit(operation, **kwargs):
            return True
        self._modal(generation, 'ok', self.t('programme'), self.t('pending_operation'))
        return False

    def _modal(self, generation, method, *args, **kwargs):
        if not self._valid(generation):
            self.close()
            return None
        answer = getattr(self._dialog(), method)(*args, **kwargs)
        if not self._valid(generation):
            self.close()
            return None
        return answer

    def _show_scope(self, generation, result):
        return self._viewer(generation, self._scope_display, self.t('programme_scope_title'),
                             lambda: prepared_scope(result, self.strings), self.t('scope_failed'))

    def _show_message(self, generation, heading, text):
        return self._viewer(generation, self._message_display, heading,
                             lambda: text + '\n\n' + self.t('return_previous'), self.t('message_failed'))

    def _viewer(self, generation, display, heading, render, failure_message):
        if not self._valid(generation):
            self.close()
            return False
        try:
            text = render()
            if display is None or display(heading, text) is not True:
                raise RuntimeError('viewer unavailable')
        except Exception:
            # Revoke the preview/token before reporting a closed local UI failure.
            valid = self._valid(generation)
            self.close()
            if valid:
                self._dialog().ok(self.t('programme_stopped'), failure_message + '\n' + self.t('controls_blocked'))
                self._owner()  # no follow-up admission after this compact error modal
            return False
        if not self._valid(generation):
            self.close()
            return False
        return True

    def poll(self):
        self._owner()
        if self.worker is None:
            return
        generation = self._generation
        if not self._valid(generation):
            self.close()
            return
        result = self.worker.poll()
        if result is None:
            return
        worker = self.worker
        if isinstance(result, ProgrammeHistoryResult):
            self._history_result(generation, result)
            return  # Never fall through to owned actions, including on error.
        if result.state == 'prepared' and result.error is None:
            if not self._prepare or result.operation != 'prepare' or result.event != self._event:
                self.close()
                return
            if not self._show_scope(generation, result):
                return
            answer = self._modal(generation, 'yesno', self.t('save_question'), self.t('save_confirm'))
            if self.worker is None:
                return
            if answer is True:
                self._queue(generation, worker, 'save', token=result.token, confirmed=True)
            else:
                self._queue(generation, worker, 'abort')
            return
        # After save the stored active plan, never the current guide event, owns cleanup.
        if result.operation != 'prepare':
            self._prepare = False
        if result.error is not None:
            message = lifecycle_scope(result, self.strings) + friendly_error(result.error, self.strings)
            if result.state in ('create_unknown', 'remove_unknown') and result.error != 'checkpoint_failed':
                message += '\n' + uncertainty(result.state, self.strings)
            if not self._show_message(generation, self.t('programme_stopped'), message):
                return
            if result.error == 'checkpoint_failed':
                return
        if result.state in ('create_unknown', 'remove_unknown'):
            if not self._show_message(generation, self.t('pass_uncertain'),
                                       lifecycle_scope(result, self.strings) + uncertainty(result.state, self.strings)):
                return
            entries: tuple[tuple[str, str], ...] = (('status', self.t('status_read')),)
            choice = command(entries, self._modal(generation, 'select',
                self.t('unknown_heading', sequence=result.sequence, count=result.history_count), [label for _, label in entries]))
            if choice == 'status' and self.worker is worker:
                self._queue(generation, worker, 'status')
        elif result.state == 'owned' and result.can_remove and result.error is None:
            entries = (('remove', self.t('remove_this')), ('status', self.t('status_read')))
            choice = command(entries, self._modal(generation, 'select',
                self.t('pass_owned_heading', sequence=result.sequence, count=result.history_count), [label for _, label in entries]))
            if self.worker is None:
                return
            if choice == 'remove':
                answer = self._modal(generation, 'yesno', self.t('remove_question'), self.t('remove_confirm'))
                if answer is True and self.worker is worker:
                    self._queue(generation, worker, 'remove', token=result.token, confirmed=True)
            elif choice == 'status':
                self._queue(generation, worker, 'status')
        elif result.error is None:
            message = {'ready': self.t('pass_none'), 'removed': self.t('pass_removed'),
                'owned': self.t('pass_ineligible', reason=friendly_error(result.reason or 'ownership_unverified', self.strings))}
            self._show_message(generation, self.t('programme_status'), lifecycle_scope(result, self.strings)
                               + message.get(result.state, self.t('saved_operation')))

    def _history_result(self, generation, result):
        if result.error is not None or not isinstance(result.snapshot, ProgrammeHistorySnapshot):
            self._viewer(generation, self._message_display, self.t('operation_history'),
                          lambda: self.t('history_unavailable') + '\n' + self.t('history_no_observation')
                          + '\n\n' + self.t('history_warning'), self.t('history_failed'))
            return
        snapshot = result.snapshot
        if not snapshot.rows:
            self._viewer(generation, self._message_display, self.t('operation_history'),
                          lambda: snapshot.notice, self.t('history_failed'))
            return
        bookmark = 0
        while self._valid(generation):
            try:
                choice = self._modal(generation, 'select', self.t('history_heading'),
                                     [row.label for row in snapshot.rows], preselect=bookmark)
            except Exception:
                valid = self._valid(generation)
                self.close()
                if valid:
                    self._dialog().ok(self.t('history_stopped'), self.t('history_list_failed'))
                    self._owner()
                return
            if not self._valid(generation):
                self.close()
                return
            if type(choice) is not int or not 0 <= choice < len(snapshot.rows):
                return
            bookmark = choice
            if not self._viewer(generation, self._message_display, self.t('operation_details'),
                                 lambda: snapshot.rows[choice].details, self.t('history_failed')):
                return
