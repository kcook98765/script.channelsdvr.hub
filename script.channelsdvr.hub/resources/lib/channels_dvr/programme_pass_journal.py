"""Unchanged programme envelopes over worker-only portable operation storage."""
from copy import deepcopy
import json

from .metadata_http import _unique_object, _reject_constant, _finite_float
from .programme_pass_commands import ProgrammeError, canonical, require, validate_checkpoint
from .operation_storage import OperationStore, Scope

MAX_BYTES, MAX_HISTORY = 8388608, 32


def journal_state(value):
    require(type(value) is dict and set(value) == {'version', 'server_binding', 'active', 'history'})
    require(type(value['version']) is int and value['version'] == 1)
    binding, active, history = value['server_binding'], value['active'], value['history']
    require(type(history) is list and len(history) <= MAX_HISTORY)
    for seq, receipt in enumerate(history, 1):
        require(type(receipt) is dict and set(receipt) == {'sequence', 'command'})
        require(type(receipt['sequence']) is int and receipt['sequence'] == seq)
        validate_checkpoint(receipt['command'])
        require(receipt['command']['state'] == 'removed' and receipt['command']['plan']['server_binding'] == binding)
    if active is not None:
        require(type(active) is dict and set(active) == {'sequence', 'command'})
        require(type(active['sequence']) is int and active['sequence'] == len(history) + 1)
        validate_checkpoint(active['command'])
        require(active['command']['plan']['server_binding'] == binding)
    else:
        # No history-only pointer clearing/reset; a committed lifecycle stays active.
        require(not history and binding is None)
    require(len(canonical(value).encode('utf-8')) <= MAX_BYTES)
    return deepcopy(value)


def successor_state(saved, command):
    """One root replacement: unchanged predecessor archive + uncertain new intent.

    Capacity is checked before journal.save opens a temporary file. No eviction,
    migration, active=None window or archive-based write/cancellation authority.
    """
    journal_state(saved)
    active = saved['active']
    require(active is not None and active['command']['state'] == 'removed', 'duplicate_blocked')
    require(len(saved['history']) < MAX_HISTORY, 'history_limit')
    require(command['state'] == 'create_unknown' and command['returned_id'] is None
            and command['reply'] is None and command['raw_reply'] is None)
    require(command['plan']['server_binding'] == saved['server_binding'], 'binding_mismatch')
    history = deepcopy(saved['history']) + [deepcopy(active)]
    value = dict(version=1, server_binding=saved['server_binding'], history=history,
                 active=dict(sequence=active['sequence'] + 1, command=deepcopy(command)))
    require(len(canonical(value).encode('utf-8')) <= MAX_BYTES, 'history_limit')
    return journal_state(value)


class ProgrammeJournal:
    def __init__(self, profile):
        try:
            self._store = OperationStore(profile, Scope.PROGRAMME)
        except Exception:
            raise ProgrammeError('checkpoint_failed') from None

    def acquire(self):
        try:
            self._store.acquire()
        except Exception:
            raise ProgrammeError('checkpoint_failed') from None

    def load(self):
        try:
            data = self._store.load()
            if data is None:
                return None
            value = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object,
                                parse_constant=_reject_constant, parse_float=_finite_float)
            journal_state(value)
            return deepcopy(value)
        except Exception:
            self._store.poison()
            raise ProgrammeError('checkpoint_failed') from None

    def save(self, value):
        try:
            journal_state(value)
            data = canonical(value).encode('utf-8')
            self._store.save(data)
        except Exception:
            self._store.poison()
            raise ProgrammeError('checkpoint_failed') from None

    def release(self):
        self._store.release()

    @property
    def pending_reconciliation(self):
        return self._store.pending_reconciliation

    def storage_guard(self, operation):
        try:
            self._store.reconciliation_guard(operation)
        except Exception:
            raise ProgrammeError('checkpoint_failed') from None

    def reconciled(self):
        try:
            self._store.finish_reconciliation()
        except Exception:
            raise ProgrammeError('checkpoint_failed') from None
