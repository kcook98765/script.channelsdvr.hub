"""Serial FIFO control worker. Owner close signals only; no callbacks or Kodi.

Exactly one pending/admitted operation/result. No latest-wins write replacement.
An in-flight write may finish durable receipt storage; late UI results are dropped.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Condition, Thread
import time
from uuid import uuid4

from .models import Event
from .programme_pass_commands import ProgrammeCommand, ProgrammeError, ProgrammePlan, require, canonical, fresh_removed_absence
from .programme_pass_http import ProgrammePassClient
from .programme_pass_journal import ProgrammeJournal, journal_state, successor_state, MAX_HISTORY
from .programme_pass_history import ProgrammeHistoryResult, history_snapshot
from .newbuild_compatibility import profile_binding
from .control_lifetime import ControlWorkerTicket


def _history_binding(config):
    # Exactly the existing client binding, without constructing a client/opener.
    return profile_binding(config.origin, config.namespace, config.expected_version)


def _client(config):
    return ProgrammePassClient(config.origin, namespace=config.namespace, expected_version=config.expected_version)


@dataclass(frozen=True)
class ProgrammeResult:
    operation: str
    state: str
    token: str
    event: Event | None = field(default=None, repr=False)
    can_remove: bool = False
    reason: str | None = None
    error: str | None = None
    history_count: int = 0
    sequence: int = 0
    can_prepare: bool = False
    next_sequence: int = 1


class ProgrammePassWorker:
    def __init__(self, config, profile, *, client_factory=_client, journal_factory=ProgrammeJournal, clock=time.time,
                 strings=None):
        from types import MappingProxyType
        from .ui_text import FALLBACK
        # Frozen plain values only. No owner, window or SDK callback crosses.
        self._strings = MappingProxyType(dict(FALLBACK if strings is None else strings))
        self.config, self._profile = config, profile
        self._factory, self._journal_factory, self._clock = client_factory, journal_factory, clock
        self._condition = Condition()
        self._closed, self._busy = False, False
        self._intent: tuple[str, Event | None, str, bool] | None = None
        self._result: ProgrammeResult | ProgrammeHistoryResult | None = None
        self._thread: Thread | None = None
        self._journal: ProgrammeJournal | None = None
        self._command: ProgrammeCommand | None = None
        self._saved: dict | None = None
        self._event: Event | None = None
        self._token: str | None = None
        self._admitted: str | None = None
        self._poison = False
        self._failure_code = 'checkpoint_failed'
        self._epoch = 0
        self._lifetime = ControlWorkerTicket()

    def submit(self, operation, *, event=None, token='', confirmed=False):
        require(operation in {'prepare', 'save', 'status', 'remove', 'abort', 'history'})
        with self._condition:
            if self._closed or self._busy or self._intent is not None or self._result is not None:
                return False
            if operation in {'save', 'remove'}:
                if confirmed is not True or not self._token or token != self._token:
                    return False
                self._admitted, self._token = token, None  # consume BEFORE queue
            self._intent = (operation, event, token, confirmed)
            self._busy = True
            if self._thread is None:
                self._thread = Thread(target=self._run, name='channels-programme-pass', daemon=True)
                self._thread.start()
            self._condition.notify()
            return True

    def poll(self):
        with self._condition:
            result, self._result = self._result, None
            return result

    def close(self):
        with self._condition:
            self._closed, self._intent, self._result = True, None, None
            self._token = self._admitted = None
            self._condition.notify()
            if self._thread is None:
                self._lifetime.finish()

    def _check_open(self):
        with self._condition:
            require(not self._closed, 'closed')
            require(not self._poison, self._failure_code)

    def _storage(self):
        require(not self._poison, 'checkpoint_failed')
        if self._journal is None:
            journal = self._journal_factory(self._profile)
            try:
                journal.acquire()
                saved = journal.load()
                if saved is not None:
                    journal_state(saved)
            except Exception:
                journal.release()
                self._poison = True
                raise ProgrammeError('checkpoint_failed') from None
            self._journal, self._saved = journal, saved

    def _checkpoint(self, plan=None, *, successor=False):
        epoch = self._epoch
        initial = deepcopy(self._saved)
        active = initial['active'] if initial is not None else None
        expected_plan = deepcopy(plan.private() if plan is not None else active['command']['plan'] if active else None)
        sequence = active['sequence'] if active else 1
        rollover = successor
        def persist(command):
            nonlocal rollover, sequence, expected_plan
            require(epoch == self._epoch and not self._poison, 'checkpoint_failed')
            try:
                journal = self._journal
                if journal is None:
                    raise ProgrammeError('checkpoint_failed')
                saved = self._saved
                if expected_plan is None:
                    expected_plan = deepcopy(command['plan'])
                require(command['plan'] == expected_plan, 'checkpoint_failed')
                if rollover:
                    require(canonical(saved) == canonical(initial), 'checkpoint_failed')
                    value = successor_state(saved, command)
                else:
                    history: list[dict] = []
                    if saved is not None and saved['active'] is not None:
                        journal_state(saved)
                        current = saved['active']
                        require(current is not None and current['sequence'] == sequence, 'checkpoint_failed')
                        require(saved['server_binding'] == command['plan']['server_binding'], 'binding_mismatch')
                        require(current['command']['plan'] == expected_plan, 'checkpoint_failed')
                        history = deepcopy(saved['history'])
                    else:
                        require(initial is None or (initial['active'] is None and initial['history'] == []
                                                    and initial['server_binding'] is None), 'checkpoint_failed')
                    value = dict(version=1, server_binding=command['plan']['server_binding'],
                                 active=dict(sequence=sequence, command=command), history=history)
                journal_state(value)
                journal.save(value)
            except Exception as error:
                self._poison, self._token = True, None
                if isinstance(error, ProgrammeError) and error.code == 'history_limit':
                    self._failure_code = 'history_limit'
                self._epoch += 1
                raise ProgrammeError(self._failure_code) from None
            self._saved = deepcopy(value)
            sequence, rollover = value['active']['sequence'], False
        return persist

    def _discard_preview(self):
        if self._command is not None and self._command.state == 'ready':
            self._command = self._event = None
            self._epoch += 1
        self._token = None

    def _summary(self):
        saved = self._saved
        count = len(saved['history']) if saved is not None else 0
        active = saved['active'] if saved is not None else None
        sequence = active['sequence'] if active is not None else 0
        removed = bool(active is not None and active['command']['state'] == 'removed')
        eligible = (active is None or removed) and count < MAX_HISTORY and not self._poison
        eligible = eligible and not getattr(self._journal,'pending_reconciliation',False)
        next_sequence = sequence + 1
        if self._command is not None and self._command.state == 'ready':
            sequence = next_sequence
        return dict(history_count=count, sequence=sequence, can_prepare=eligible, next_sequence=next_sequence)

    def _execute(self, operation, event, token, confirmed):
        self._check_open()
        if operation in {'save', 'remove'}:
            require(confirmed is True and token and token == self._admitted, 'confirmation_required')
            self._admitted = None
        self._storage()
        guard = getattr(self._journal, 'storage_guard', None)
        if guard is not None:
            guard(operation)
        if operation in {'abort', 'status'}:
            self._discard_preview()
        client = self._factory(self.config)  # construction has no network I/O
        if self._saved is not None and self._saved['server_binding'] is not None:
            require(client.server_binding_sha256 == self._saved['server_binding'], 'binding_mismatch')
        if self._command is None and self._saved is not None and self._saved['active'] is not None:
            self._command = ProgrammeCommand.resume(client, self._saved['active']['command'],
                checkpoint=self._checkpoint(), clock=self._clock, check_open=self._check_open)
        elif self._command is not None:
            require(self._command.plan.server_binding == client.server_binding_sha256, 'binding_mismatch')
            self._command.client = client
        if operation == 'prepare':
            saved = self._saved if self._saved is not None and self._saved['active'] is not None else None
            require(saved is None or saved['active']['command']['state'] == 'removed', 'duplicate_blocked')
            require(self._command is None or self._command.state in {'ready', 'removed'}, 'duplicate_blocked')
            if saved is not None:
                require(len(saved['history']) < MAX_HISTORY, 'history_limit')
            self._discard_preview()  # a failed replacement preview cannot reuse an earlier Save plan
            self._token = None
            self._epoch += 1
            if type(event) is not Event:
                raise ProgrammeError('selection_changed')
            if saved is not None:
                fresh_removed_absence(client, saved['active']['command'], now=self._clock(), check_open=self._check_open)
            else:
                client.verify_status()
            self._check_open()
            channels = client.public_channels()
            self._check_open()
            guide = client.guide_window(event.start)
            self._check_open()
            plan = ProgrammePlan.selected(channels, guide, self.config.namespace, event,
                         binding=client.server_binding_sha256, now=self._clock(), nonce=uuid4().hex)
            if saved is not None:
                names = [ProgrammePlan(**row['command']['plan']).body()['Name']
                         for row in saved['history'] + [saved['active']]]
                require(plan.body()['Name'] not in names, 'duplicate_blocked')
            prepared_command = ProgrammeCommand(client, plan, checkpoint=self._checkpoint(plan, successor=saved is not None),
                              clock=self._clock, check_open=self._check_open,
                              predecessor=saved['active']['command'] if saved is not None else None)
            preview = client.preview(plan)
            self._check_open()
            prepared_command.prepare(preview)
            self._command, self._event = prepared_command, event
            state = 'prepared'
        elif operation == 'abort':
            self._token = None
            state = self._command.state if self._command else 'ready'
        elif operation == 'save':
            command = self._command
            if command is None or command.state != 'ready':
                raise ProgrammeError('duplicate_blocked')
            state = command.save(confirmed=True)
        elif operation == 'remove':
            command = self._command
            if command is None:
                raise ProgrammeError('ownership_unverified')
            state = command.remove_owned(confirmed=True)
        else:
            state = self._command.status() if self._command is not None else 'ready'
        if operation == 'status' and getattr(self._journal, 'pending_reconciliation', False):
            qualified = bool(self._command and self._command.can_remove and state == 'owned')
            if self._command is None and (self._saved is None or self._saved['active'] is None):
                # A claim can outlive a crash before its first checkpoint. None
                # is no intent, not an empty envelope to synthesize or adopt.
                if self._saved is None:
                    require(client.server_binding_sha256 == _history_binding(self.config), 'binding_mismatch')
                client.verify_status()  # No intent/empty envelope grants no ownership.
                self._check_open()
                if self._saved is None:
                    require(client.server_binding_sha256 == _history_binding(self.config), 'binding_mismatch')
                qualified = True
            if self._command is not None and state == 'removed':
                fresh_removed_absence(client, self._command.private_checkpoint(), now=self._clock(), check_open=self._check_open)
                qualified = True
            if qualified:
                assert self._journal is not None
                try:
                    self._journal.reconciled()
                except Exception:
                    self._poison, self._token = True, None
                    self._epoch += 1
                    raise ProgrammeError('checkpoint_failed') from None
        self._token = uuid4().hex
        return ProgrammeResult(operation, state, self._token, self._event,
                               bool(self._command and self._command.can_remove),
                               self._command.reason if self._command else None, **self._summary())

    def _history(self):
        self._check_open()
        self._storage()  # Initial acquire/load only; may create the fixed lock directory/file.
        # NEVER reload a held journal here: load advances its root-CAS baseline.
        # _saved is replaced only by this serial worker after its own durable save.
        return ProgrammeHistoryResult(history_snapshot(self._saved, _history_binding(self.config), self._strings))

    def _run(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or self._intent is not None)
                    if self._closed:
                        return
                    intent, self._intent = self._intent, None
                    if intent is None:
                        self._busy = False
                        continue  # no admitted operation: never dispatch fallback arguments
                try:
                    result = self._history() if intent[0] == 'history' else self._execute(*intent)
                except Exception as error:
                    if intent[0] == 'history':
                        # Dedicated type cannot enter owned Remove/reconcile flow.
                        code = 'binding_mismatch' if isinstance(error, ProgrammeError) and error.code == 'binding_mismatch' else 'checkpoint_failed'
                        result = ProgrammeHistoryResult(error=code)
                        with self._condition:
                            if not self._closed:
                                self._result = result
                            self._busy = False
                        continue
                    code = error.code if hasattr(error, 'code') else 'schema_unqualified'
                    state = self._command.state if self._command else 'blocked'
                    self._token = uuid4().hex
                    if self._poison:
                        code, state = self._failure_code, 'blocked'
                    result = ProgrammeResult(intent[0], state, self._token, error=code, **self._summary())
                with self._condition:
                    if not self._closed:
                        self._result = result
                    self._busy = False
        finally:
            self._epoch += 1
            if self._journal is not None:
                try:
                    self._journal.release()
                except Exception:
                    pass
            self._lifetime.finish()  # only AFTER all persistence and journal release
