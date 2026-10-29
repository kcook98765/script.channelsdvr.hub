"""Worker-only bounded Series root. Claims/CAS are provided by OperationStore."""
import base64
from copy import deepcopy
from dataclasses import dataclass, field
import json
import secrets
from threading import Lock

from .operation_storage import OperationStore, Scope
from .metadata_http import _unique_object, _reject_constant, _finite_float
from .series_models import RuleSnapshot, identity, Provenance, canonical
from .series_manager_models import PROTOCOL, MAX_RECORDS, MAX_HISTORY, SeriesRecord, need, ManagerError
from .newbuild_compatibility import supported_build
from . import series_contracts as contracts

MAX_BYTES = 8388608
# Lossless base64 wire + bounded decoded ID, successor snapshot and envelope room.
RESERVE = 5592408 + 262144
SUCCESSOR_RESERVE = 196608
_DISPATCH = object()
_CONSUMED = object()
_DISPATCH_GATE = Lock()


def encode(value):
    from .series_reconciliation import _canonical
    return _canonical(value).encode('ascii')


def hex64(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def validate(root):
    need(type(root) is dict and set(root) == {'protocol', 'binding', 'records', 'history', 'pending', 'blocked', 'observations'})
    need(root['protocol'] == PROTOCOL)
    need(hex64(root['binding']) and type(root['blocked']) is bool)
    need(type(root['observations']) is list and len(root['observations']) <= MAX_HISTORY)
    for observation in root['observations']:
        need(type(observation) is dict and set(observation) == {'index', 'outcome', 'actual', 'rules', 'jobs', 'public'})
        need(type(observation['index']) is int and -1 <= observation['index'] < len(root['history']))
        need(observation['outcome'] in {'reconciled', 'blocked', 'observed'} and supported_build(observation['actual']))
        need(all(hex64(observation[k]) for k in ('rules', 'jobs', 'public')))
    need(not any(o['outcome'] == 'blocked' for o in root['observations']) or root['blocked'] is True)
    need(type(root['records']) is list and len(root['records']) <= MAX_RECORDS)
    need(type(root['history']) is list and len(root['history']) <= MAX_HISTORY)
    keys = set()
    for row in root['records']:
        SeriesRecord.from_payload(row, root['binding'])
        need(type(row) is dict and set(row) == {'id', 'provenance', 'receipt', 'initial', 'last', 'removed', 'scope_token'})
        key = identity(row['id'])
        need(key not in keys)
        keys.add(key)
        need(row['provenance'] in {p.value for p in Provenance} and type(row['removed']) is bool)
        need(type(row['receipt']) is str and len(row['receipt']) == 64)
        need(type(row['scope_token']) is str and len(row['scope_token']) == 64)
        for name in ('initial', 'last'):
            need(RuleSnapshot.parse(row[name]).payload()['ID'] == key)
    tokens = set()
    for entry in root['history']:
        need(type(entry) is dict and set(entry) == {'operation', 'id', 'token', 'before', 'body',
             'dispatched', 'wire', 'returned_id', 'state', 'actual', 'baseline_ids'})
        need(entry['operation'] in {'create', 'adopt', 'rename', 'pause', 'resume', 'remove'})
        need(type(entry['dispatched']) is bool and entry['state'] in {'intent', 'unknown', 'complete'})
        need(hex64(entry['token']) and entry['token'] not in tokens and supported_build(entry['actual']))
        tokens.add(entry['token'])
        need((entry['state'] != 'intent' or entry['dispatched'] is False)
             and (entry['state'] != 'unknown' or entry['dispatched'] is True))
        if entry['operation'] == 'create':
            from .series_manager_models import GeneralSeriesPlan
            body = entry['body']
            need(type(body) is dict and type(body.get('EQ')) is dict)
            need(canonical(body) == canonical(GeneralSeriesPlan(body['EQ'].get('SeriesID'), body.get('Name')).body()))
            need(entry['id'] is None and entry['before'] is None)
            ids = entry['baseline_ids']
            need(type(ids) is list and len(ids) <= 10000 and len(set(ids)) == len(ids))
            for key in ids: identity(key)
        else:
            need(entry['baseline_ids'] is None and type(entry['before']) is dict)
            need(entry['before']['ID'] == entry['id'])
            if entry['operation'] == 'rename':
                planned = entry['body']
                need(type(planned) is dict and set(planned) == set(entry['before']))
                expected = dict(entry['before'], Name=planned.get('Name'))
                need(canonical(planned) == canonical(expected))
            else:
                need(entry['body'] is None)
        if entry['operation'] == 'adopt':
            need(entry['dispatched'] is False and entry['wire'] is None and entry['returned_id'] is None)
        else:
            need(entry['state'] != 'complete' or entry['dispatched'] is True
                 or any(o['index'] == root['history'].index(entry) and o['outcome'] == 'reconciled' for o in root['observations']))
        if entry['id'] is not None:
            identity(entry['id'])
        if entry['before'] is not None:
            RuleSnapshot.parse(entry['before'])
        if entry['body'] is not None:
            canonical(entry['body'])
        if entry['returned_id'] is not None:
            identity(entry['returned_id'])
        wire = entry['wire']
        if wire is not None:
            need(type(wire) is dict and set(wire) == {'raw_base64', 'status', 'complete', 'media', 'encoding'})
            need(type(wire['complete']) is bool and type(wire['status']) is int)
            need(wire['media'] in {'json', 'other'} and wire['encoding'] in {'identity', 'other'})
            need(len(base64.b64decode(wire['raw_base64'], validate=True)) <= 4194304)
    pending = root['pending']
    need(pending is None or (type(pending) is int and 0 <= pending == len(root['history']) - 1
                             and root['history'][pending]['state'] != 'complete'))
    need(all(e['state'] == 'complete' for e in root['history'][:pending] if pending is not None)
         if pending is not None else all(e['state'] == 'complete' for e in root['history']))
    for row in root['records']:
        initial = [e for e in root['history'] if e['token'] == row['scope_token']]
        need(len(initial) == 1 and initial[0]['state'] == 'complete')
        entry = initial[0]
        if row['provenance'] == 'created':
            need(entry['operation'] == 'create' and entry['dispatched'] is True
                 and entry['returned_id'] == row['id'] and entry['wire'] is not None)
            reply = wire_payload(entry['wire'])
            contracts.validate_create(entry['body'], row['id'], reply, RuleSnapshot.parse(row['initial']), root['binding'], receipt_id=row['receipt'])
        else:
            need(entry['operation'] == 'adopt' and entry['dispatched'] is False
                 and canonical(entry['before']) == canonical(row['initial']))
    encode(root)
    return root


def decode(raw):
    need(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES)
    root = json.loads(raw, object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant, parse_float=_finite_float)
    validate(root)
    need(encode(root) == raw)
    return root


def wire_payload(wire):
    need(type(wire) is dict and wire['complete'] is True and wire['status'] in (200, 204)
         and wire['encoding'] == 'identity')
    raw = base64.b64decode(wire['raw_base64'], validate=True)
    need(wire['media'] == 'json' or not raw)
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant,
                      parse_float=_finite_float) if raw else None


def candidate_key(entry):
    """Positive original reply ID only; never a current-list/name/Series search.

    A raw-wire commit may succeed before its decoded-ID annotation fails. Read-only
    recovery can extract that ID from the complete qualified original response.
    This helper alone grants no ownership, mutation or receipt validity.
    """
    if entry['operation'] != 'create':
        return entry['id']
    if entry['returned_id'] is not None:
        return entry['returned_id']
    try:
        payload = wire_payload(entry['wire'])
        return identity(payload.get('ID')) if type(payload) is dict else None
    except Exception:
        return None


@dataclass(frozen=True, init=False, repr=False)
class DispatchProof:
    operation: str
    binding: str = field(repr=False)
    identity: str | None = field(repr=False)
    body: str | None = field(repr=False)
    token: str = field(repr=False)
    _seal: object = field(repr=False)

    def __repr__(self):
        return 'DispatchProof(private=True)'


def checked_dispatch(proof, binding):
    need(type(proof) is DispatchProof and getattr(proof, '_seal', None) is _DISPATCH
         and proof.binding == binding and proof.operation in {'create', 'rename', 'pause', 'resume', 'remove'}, 'consent')
    return proof


def consume_dispatch(proof, binding):
    with _DISPATCH_GATE:
        checked_dispatch(proof, binding)
        object.__setattr__(proof, '_seal', _CONSUMED)


class SeriesManagerStore:
    def __init__(self, profile, binding, *, store_factory=OperationStore):
        self.store = store_factory(profile, Scope.SERIES)
        self.binding = binding
        self.root: dict = {}
        self.failed = False

    def open(self):
        try:
            self.store.acquire()
            raw = self.store.load()
            self.root = (dict(protocol=PROTOCOL, binding=self.binding, records=[], history=[], pending=None, blocked=False, observations=[])
                         if raw is None else decode(raw))
            need(self.root['binding'] == self.binding, 'source')
            # Barriers are reconciled only by the typed normal read-only path.
        except Exception:
            self.failed = True
            self.store.poison()
            raise ManagerError('storage') from None

    def save(self):
        try:
            need(not self.failed, 'storage')
            validate(self.root)
            self.store.save(encode(self.root))
        except Exception:
            self.failed = True
            self.store.poison()
            raise ManagerError('storage') from None

    def record(self, key):
        need(bool(self.root), 'storage')
        return next((r for r in self.root['records'] if r['id'] == key), None)

    def typed_record(self, key):
        row = self.record(key)
        return SeriesRecord.from_payload(row, self.binding) if row is not None else None

    def reserve(self, operation, *, existing=False):
        need(bool(self.root) and not self.failed, 'storage')
        need(self.root['pending'] is None, 'pending')
        need(self.root['blocked'] is False and not self.store.pending_reconciliation, 'pending')
        need(len(self.root['history']) < MAX_HISTORY, 'capacity')
        if operation == 'create' or (operation == 'adopt' and not existing):
            need(len(self.root['records']) < MAX_RECORDS, 'capacity')
        self._admit(self.root)

    @staticmethod
    def _admit(root):
        # Measure the actual complete prospective envelope, not a guessed fixed
        # allowance for an unbounded-in-aggregate baseline ID list.
        need(len(encode(root)) + RESERVE + SUCCESSOR_RESERVE <= MAX_BYTES, 'capacity')

    def intent(self, operation, token, before, body, actual, *, baseline_ids=None):
        key = before.payload()['ID'] if before is not None else None
        self.reserve(operation, existing=operation == 'adopt' and self.record(key) is not None)
        prospective = deepcopy(self.root)
        prospective['history'].append(dict(operation=operation, id=key, token=token,
            before=before.payload() if before is not None else None, body=deepcopy(body), actual=actual,
            dispatched=False, wire=None, returned_id=None, state='intent',
            baseline_ids=list(baseline_ids or ()) if operation == 'create' else None))
        prospective['pending'] = len(prospective['history']) - 1
        validate(prospective)
        self._admit(prospective)  # Includes every actual baseline ID before any save/HTTP.
        self.root = prospective
        self.save()  # Durable intent, separate from burn.

    def dispatch(self):
        need(self.root is not None and self.root['pending'] is not None, 'pending')
        prospective = deepcopy(self.root)
        entry = prospective['history'][prospective['pending']]
        need(not entry['dispatched'] and entry['operation'] != 'adopt', 'pending')
        entry['dispatched'], entry['state'] = True, 'unknown'
        validate(prospective)
        self._admit(prospective)  # Burn state and full headers/body/baseline must still fit.
        self.root = prospective
        self.save()  # Durable one-way dispatch burn BEFORE handing proof to HTTP.
        proof = object.__new__(DispatchProof)
        for key, value in dict(operation=entry['operation'], binding=self.binding, identity=entry['id'],
            body=canonical(entry['body']) if entry['body'] is not None else None,
            token=entry['token'], _seal=_DISPATCH).items():
            object.__setattr__(proof, key, value)
        return proof

    def capture(self, wire, returned_id=None):
        need(self.root is not None and self.root['pending'] is not None, 'pending')
        entry = self.root['history'][self.root['pending']]
        need(entry['dispatched'], 'pending')
        entry['wire'] = deepcopy(wire)
        if returned_id is not None:
            entry['returned_id'] = identity(returned_id)
        self.save()  # No cast, semantic refusal or deadline check may precede this.

    def finish(self, snapshot=None, provenance=None):
        need(self.root is not None and self.root['pending'] is not None, 'pending')
        entry = self.root['history'][self.root['pending']]
        if snapshot is not None:
            row = snapshot.payload()
            existing = self.record(row['ID'])
            if existing is None:
                need(provenance in {'created', 'adopted'})
                self.root['records'].append(dict(id=row['ID'], provenance=provenance,
                    receipt=secrets.token_hex(32), initial=row, last=row, removed=False,
                    scope_token=entry['token']))
            else:
                need(provenance is None)
                existing['last'] = row
        elif entry['operation'] == 'remove':
            self.record(entry['id'])['removed'] = True
        else:
            need(False)
        entry['state'] = 'complete'
        self.root['pending'] = None
        self.save()

    def close(self):
        self.store.release()

    def quarantine(self):
        if not self.failed and not self.store.pending_reconciliation and self.root.get('pending') is not None:
            self.root['blocked'] = True
            self.save()

    def reconcile(self, config, client_factory):
        from .series_reconciliation import SeriesSource, SeriesReconciliationProof
        need(not self.failed, 'storage')
        source = SeriesSource(config.origin, config.namespace, config.expected_version, None)
        request = self.store.begin_series_manager_reconciliation(source)
        client = client_factory(config)
        observation = client.manager_snapshot(request)
        proof = SeriesReconciliationProof.manager_v1(request, observation)
        disposition = self.store.apply_series_manager_reconciliation(proof)
        raw = self.store.load()
        self.root = (decode(raw) if raw is not None else dict(protocol=PROTOCOL, binding=self.binding,
                     records=[], history=[], pending=None, blocked=False, observations=[]))
        return disposition

    def status_challenge(self, config):
        from .series_reconciliation import SeriesSource
        return self.store.begin_series_manager_reconciliation(SeriesSource(config.origin, config.namespace, config.expected_version, None))

    def quarantine_status(self, request, refusal):
        from .series_http import bind_status_refusal
        from .series_reconciliation import manager_refusal_proof
        try:
            proof = manager_refusal_proof(request, bind_status_refusal(refusal, request))
            self.store.apply_series_manager_reconciliation(proof)
            raw = self.store.load()
            if raw is not None:
                self.root = decode(raw)
        except Exception:
            self.failed = True
            self.store.poison()
            raise ManagerError('storage') from None


def manager_storage_rows(inspection):
    """Only known manager roots/actual SQL-None claims; legacy/v3 stays excluded."""
    rows = []
    for snapshot in inspection.roots:
        if snapshot.scope is not Scope.SERIES:
            continue
        if snapshot.payload is None:
            if snapshot.owner is not None or snapshot.barrier is not None:
                rows.append((Scope.SERIES, 'none', 0, snapshot.owner is not None, snapshot.barrier is not None))
        else:
            try:
                root = decode(snapshot.payload)
            except Exception:
                continue
            state = 'blocked' if root['blocked'] else 'create_unknown' if root['pending'] is not None else 'owned'
            rows.append((Scope.SERIES, state, len(root['history']), snapshot.owner is not None, snapshot.barrier is not None))
    return tuple(rows)
