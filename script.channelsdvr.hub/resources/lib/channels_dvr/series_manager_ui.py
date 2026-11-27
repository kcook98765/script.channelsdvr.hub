"""Owner-only injected dialogs; frozen DTOs cross from the Series worker."""
import json
import hashlib
from dataclasses import dataclass
from collections.abc import Callable

from .dvr_catalog import STATUS_MESSAGES
from .series_manager_worker import SeriesManagerWorker
from .series_manager_models import ManagerView, RuleRow
from .ui_menu import command
from .ui_text import text
from .series_manager_presentation import (rule_label, rule_details, episode_label, episode_details,
                                        text_pages, RowPages, MAX_READER_BYTES)

ACTION_KEYS = {'create': 'series_create', 'adopt': 'series_adopt', 'rename': 'series_rename',
    'pause': 'series_pause', 'resume': 'series_resume', 'remove': 'series_remove',
    'refresh_consent': 'series_reconfirm'}


@dataclass(frozen=True)
class DvrReview:
    row: RuleRow | None
    current: Callable[[], bool]
    finished: Callable[[ManagerView], None]
    source_current: Callable[[], bool]


class SeriesManagerUI:
    def __init__(self, *, assert_owner, dialog, profile, config, selection, viewer,
                 strings, default_no, source_scope, worker_factory=SeriesManagerWorker,
                 presentation_scope=lambda: None, status_finished=lambda result: None):
        self._owner, self._dialog, self._profile, self._config = assert_owner, dialog, profile, config
        self._selection, self._viewer, self.strings = selection, viewer, strings
        self._factory = worker_factory
        self._context = source_scope
        self._scope = None
        self._pending = False
        self._reviewing = False
        self._default_no = default_no  # Supplied Kodi DLG_YESNO_NO_BTN, never a guessed ID.
        self.worker: SeriesManagerWorker | None = None
        self._binding = None
        self._event = None
        self._generation = 0
        self._readers = {}
        self._presentation_scope = presentation_scope
        self._status_finished = status_finished
        self._presentation = None
        self._dvr: DvrReview | None = None
        self._confirmed: str | None = None

    def t(self, key, **values):
        return text(self.strings, key, **values)

    def close(self):
        self._generation += 1
        if self.worker is not None:
            self.worker.close()
        self.worker, self._binding, self._event = None, None, None
        self._scope, self._pending = None, False
        self._dvr = None
        self._confirmed = None
        self._readers.clear()

    def _valid(self, generation):
        self._owner()
        return (generation == self._generation and self.worker is not None
                and self._binding is not None and self._config() == self._binding
                and self._scope == self._context()
                and (self._event is None or self._selection() == self._event))

    def _modal(self, generation, method, *args, **kwargs):
        if not self._review_current(generation):
            self.close()
            return None
        answer = getattr(self._dialog(), method)(*args, **kwargs)
        if not self._review_current(generation):
            self.close()
            return None
        return answer

    def _review_current(self, generation):
        return (self._valid(generation) and self._presentation is self._presentation_scope()
                and (self._dvr is None or self._dvr.current()))

    def _show(self, generation, body):
        if not self._review_current(generation):
            self.close()
            return False
        try:
            digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
            if digest not in self._readers:
                pages = text_pages(body)
                # Ephemeral reader bookmarks only, never durable receipt/history eviction.
                if len(self._readers) >= 8 or sum(sum(len(p.encode('utf-8')) for p in row[0]) for row in self._readers.values()) + len(body.encode('utf-8')) > MAX_READER_BYTES:
                    self._readers.clear()
                self._readers[digest] = (pages, 0)
            pages, page = self._readers[digest]
            while self._review_current(generation):
                heading = self.t('series_manager') + (f' {page+1}/{len(pages)}' if len(pages) > 1 else '')
                shown = self._viewer(heading, pages[page]) is True
                if not shown or not self._review_current(generation):
                    self.close()
                    return False
                if len(pages) == 1:
                    return True
                entries: tuple[tuple[str, str], ...] = ((('previous', 'previous_page'),) if page else ())
                entries += ((('next', 'next_page'),) if page+1 < len(pages) else ())
                entries += (('close', 'close'),)
                action = command(entries, self._modal(generation, 'select', heading, [self.t(key) for _, key in entries], preselect=len(entries)-1))
                if not self._review_current(generation):
                    self.close()
                    return False
                if action not in {'next', 'previous'}:
                    return True
                page += 1 if action == 'next' else -1
                self._readers[digest] = (pages, page)
        except Exception:
            self.close()
            return False
        self.close()
        return False

    def _queue(self, generation, operation, **values):
        if not self._review_current(generation):
            self.close()
            return False
        assert self.worker is not None
        if not self.worker.submit(operation, **values):
            self._modal(generation, 'ok', self.t('series_manager'), self.t('pending_operation'))
            return False
        self._pending = True
        self._confirmed = operation if operation in ACTION_KEYS and values.get('confirmed') is True else None
        self._presentation = self._presentation_scope()
        return True

    def _begin(self):
        self._owner()
        if self._reviewing:
            return
        config = self._config()
        if config is None:
            self._dialog().ok(self.t('series_manager'), self.t('connect_first'))
            return
        if self._pending and self._confirmed and self._dvr is not None and self._dvr.source_current():
            self._dialog().ok(self.t('series_manager'), self.t('pending_operation'))
            return
        if self.worker is not None and (self._binding != config or self._scope != self._context()):
            self.close()
        if self.worker is not None and self._pending:
            self._dialog().ok(self.t('series_manager'), self.t('pending_operation'))
            return
        if self.worker is None:
            try:
                self.worker = self._factory(config, self._profile())
                self._binding = config
                self._scope = self._context()
            except Exception:
                self.close()
                self._dialog().ok(self.t('series_manager'), self.t('profile_blocked'))
                return
        self._event = None
        self._dvr = None
        self._presentation = self._presentation_scope()
        return self._generation

    def request(self):
        generation = self._begin()
        if generation is None:
            return
        event_at_open = self._selection()
        entries = (('list', 'series_manager'), ('create', 'series_create'), ('status', 'status_read'))
        selected = command(entries, self._modal(generation, 'select', self.t('series_manager'),
                                                [self.t(k) for _, k in entries]))
        if selected == 'list':
            self._queue(generation, 'list')
        elif selected == 'status':
            self._queue(generation, 'status')
        elif selected == 'create':
            event = self._selection()
            if event is None or event != event_at_open:
                self._modal(generation, 'ok', self.t('series_manager'), self.t('pass_selection_unavailable'))
                return
            self._event = event
            self._queue(generation, 'prepare', event=event)

    def request_dvr(self, row, current, finished, *, source_current):
        self._owner()
        if not current() or (row is not None and not row.supported):
            return False
        generation = self._begin()
        if generation is None:
            return False
        self._dvr = DvrReview(row, current, finished, source_current)
        if row is None:
            return self._queue(generation, 'status')
        return self._queue(generation, 'details', key=row.key)

    def _browse(self, generation, rows, provenances=()):
        # Typed key lookup against captured immutable rows, never parse display text.
        if not rows:
            self._show(generation, self.t('live_empty_message'))
            return
        page = 0
        kinds = dict(provenances)
        pager = RowPages(rows, lambda row: rule_label(row, kinds.get(row.key), self.strings), self.t('saved_details_title'))
        while self._valid(generation):
            start, stop, labels = pager.page(page)
            selected_rows = rows[start:stop]
            entries = tuple(('row', row) for row in selected_rows)
            if page:
                entries += (('previous', None),)
            if stop < len(rows):
                entries += (('next', None),)
            labels = list(labels) + [self.t('series_'+kind) for kind, row in entries if kind != 'row']
            answer = self._modal(generation, 'select', self.t('series_manager'), labels)
            if type(answer) is not int or not 0 <= answer < len(entries):
                return
            kind, row = entries[answer]
            if kind == 'row':
                self._queue(generation, 'details', key=row.key)
                return
            page += 1 if kind == 'next' else -1

    def _preview(self, generation, result):
        if not result.matches:
            return self._show(generation, self.t('preview_empty', count=0))
        page = 0
        pager = RowPages(result.matches, lambda row: episode_label(row, self.strings), self.t('preview_details'))
        while self._valid(generation):
            start, stop, labels = pager.page(page)
            rows = result.matches[start:stop]
            entries = tuple(('row', row) for row in rows)
            if page:
                entries += (('previous', None),)
            if stop < len(result.matches):
                entries += (('next', None),)
            labels = list(labels) + [self.t('series_'+kind) for kind, row in entries if kind != 'row']
            answer = self._modal(generation, 'select', self.t('preview_details'), labels)
            if type(answer) is not int or not 0 <= answer < len(entries):
                return self._valid(generation)
            kind, row = entries[answer]
            if kind != 'row':
                page += 1 if kind == 'next' else -1
                continue
            if not self._show(generation, episode_details(row, result.observed_at, self.strings)):
                return False
            raw = self._modal(generation, 'select', self.t('preview_details'), [self.t('series_raw')])
            if type(raw) is int and raw == 0:
                if not self._show(generation, row.raw):
                    return False
        return False

    def poll(self):
        self._owner()
        if self._reviewing:
            return
        self._reviewing = True
        try:
            self._poll()
        finally:
            self._reviewing = False

    def _poll(self):
        self._owner()
        if self.worker is None:
            return
        generation = self._generation
        confirmed, dvr = self._confirmed, self._dvr
        if not (dvr.source_current() if confirmed and dvr is not None else self._valid(generation)):
            self.close()
            return
        result = self.worker.poll()
        if type(result) is not ManagerView:
            return
        self._pending, self._confirmed = False, None
        if dvr is not None:
            if confirmed is not None:
                if result.operation != confirmed:
                    result = ManagerView(confirmed, error='pending')
                # The result is from an already confirmed, exact command. Keep
                # source-wide status/readback, but never open over a different row.
                if self._review_current(generation):
                    self._show(generation, self.t(self._error_key(result.error) if result.error else 'dvr_series_verified'))
                if dvr.source_current():
                    dvr.finished(result)
                self._dvr = None
            else:
                self._dvr_result(generation, result, dvr)
            return
        if result.operation == 'status' and self._valid(generation):
            # Status is source-wide, but dialogs and the follow-up list are not.
            self._status_finished(result)
        if self._presentation is not self._presentation_scope():
            if result.operation == 'prepare':
                self.close()  # Drop the unconfirmed review without cross-route activation.
            return
        if result.error is not None:
            self._show(generation, self.t(self._error_key(result.error)))
            return
        if result.operation == 'list':
            self._browse(generation, result.rows, result.provenances)
        elif result.operation == 'prepare':
            if result.plan is None or result.preview is None:
                self.close()
                return
            body = self.t('series_scope', title=result.plan.title,
                details=json.dumps(result.plan.body(), ensure_ascii=False, indent=2))
            if not self._show(generation, body):
                return
            if not self._preview(generation, result):
                return
            # Viewer Close/Back is NOT consent. Always a separate default-No YesNo.
            answer = self._modal(generation, 'yesno', self.t('series_create'), self.t('series_consent'), defaultbutton=self._default_no)
            self._queue(generation, 'create' if answer is True else 'abort',
                        **dict(token=result.token, confirmed=True) if answer is True else {})
            self._event = None
        elif result.operation == 'details':
            self._details(generation, result)
        elif result.operation == 'status':
            key = STATUS_MESSAGES.get(result.status_outcome) if result.status_outcome else None
            if key is None or self._show(generation, self.t(key)):
                self._queue(generation, 'list')
        elif result.operation != 'abort':
            self._queue(generation, 'list')

    @staticmethod
    def _error_key(error):
        return {'pending': 'dvr_series_pending', 'storage': 'profile_blocked', 'activity': 'series_activity',
                'capacity': 'series_capacity', 'expired': 'error_expired',
                'unchanged': 'series_name_unchanged'}.get(error, 'series_stopped')

    def _dvr_result(self, generation, result, review):
        if self._presentation is not self._presentation_scope() or not review.current():
            self.close()  # Already-completed transport outcomes remain journaled.
            return
        if result.error is not None:
            self._show(generation, self.t(self._error_key(result.error)))
        elif result.operation == 'details':
            if review.row is None or result.row != review.row:
                self._show(generation, self.t('dvr_pass_changed'))
                if review.current():
                    review.finished(ManagerView('details', error='stale'))
                self.close()
                return
            if result.control_blocked:
                self._show(generation, self.t('dvr_series_pending'))
                result = ManagerView('details', error='pending')
            else:
                self._details(generation, result)
                return
        elif result.operation == 'status':
            key = STATUS_MESSAGES.get(result.status_outcome) if result.status_outcome else None
            if key is not None:
                self._show(generation, self.t(key))
        elif result.operation != 'abort':
            self._show(generation, self.t('dvr_series_verified'))
        if result.operation != 'abort' and review.current():
            review.finished(result)
        self._dvr = None

    def _details(self, generation, result):
        if result.row is None:
            self.close()
            return
        body = rule_details(result.row, result.provenance, result.actions, self.strings)
        if result.activity_blocked:
            body += '\n\n' + self.t('series_activity')
        if result.row.supported:
            body = self.t('series_scope', title='', details=body)
        if not self._show(generation, body):
            return
        entries = tuple((action, ACTION_KEYS[action]) for action in result.actions
                        if action in ACTION_KEYS and action != 'create' and type(result.token) is str)
        entries += (('raw', 'series_raw'),)
        action = None
        while self._review_current(generation):
            action = command(entries, self._modal(generation, 'select', self.t('series_manager'), [self.t(k) for _, k in entries]))
            if action != 'raw':
                break
            if not self._show(generation, result.row.raw):
                return
        if action is None:
            self._queue(generation, 'abort')
            return
        values = dict(token=result.token, confirmed=True)
        if action == 'rename':
            old_name = result.row.payload()['Name']
            name = self._modal(generation, 'input', self.t('series_rename'), defaultt=old_name)
            if type(name) is not str or not name.strip():
                self._queue(generation, 'abort')
                return
            if name == old_name:
                self._modal(generation, 'ok', self.t('series_rename'), self.t('series_name_unchanged'))
                self._queue(generation, 'abort')
                return
            values['name'] = name
            if not self._show(generation, name):
                return
        answer = self._modal(generation, 'yesno', self.t(ACTION_KEYS[action]), self.t('series_consent'), defaultbutton=self._default_no)
        self._queue(generation, action if answer is True else 'abort', **values if answer is True else {})
