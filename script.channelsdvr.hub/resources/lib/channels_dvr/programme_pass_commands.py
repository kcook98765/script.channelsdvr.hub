"""Bounded programme-only pass lifecycle. No Kodi, disk, URLs or implicit writes.

Explicit repeat lifecycles retain bounded private history. Preview membership is a sampled observation,
not a promise of future coverage or persisted time/source containment.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Protocol

from .pass_preview import selected_series, PreviewError
from .recording_worker import selected_plan
from .recording_commands import internal_jobs, public_jobs, RecordingError
from .metadata_http import _unique_object, _reject_constant, _finite_float

MAPS = ('EQ', 'NE', 'GT', 'LT', 'IN', 'NI')
STATES = frozenset(('create_unknown', 'owned', 'remove_unknown', 'removed'))
FLAGS = ('Recording', 'Active', 'Running', 'Skipped', 'Failed', 'Completed',
         'Error', 'Dead', 'Started', 'Cancelled')


class ProgrammeError(Exception):
    def __init__(self, code):
        self.code = code if code in {
            'schema_unqualified', 'selection_changed', 'near_start', 'preview_not_one',
            'confirmation_required', 'expired', 'binding_mismatch', 'duplicate_blocked',
            'ownership_unverified', 'ineligible', 'checkpoint_failed', 'closed',
            'history_limit', 'operation_in_progress', 'programme_past', 'programme_current',
            'outside_window', 'series_unsupported'} else 'schema_unqualified'
        super().__init__(self.code)


def require(ok, code='schema_unqualified'):
    if not ok:
        raise ProgrammeError(code)


def prepare_timing(event, now):
    """Closed timing diagnosis, never a replacement for the fresh full-key join."""
    require(event.end > now, 'programme_past')
    require(event.start > now, 'programme_current')
    require(event.start - now >= 3600, 'near_start')
    require(event.start - now <= 172800, 'outside_window')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def text(value: object, limit: int = 256) -> str:
    if type(value) is not str:
        raise ProgrammeError('schema_unqualified')
    require(bool(value.strip()) and len(value) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))
    return value


def finite(value):
    require(type(value) in (int, float) and math.isfinite(value) and 946684800 <= value <= 4102444800, 'expired')
    return value


def receipt_json(raw):
    """Bounded transport-equivalent decoding for an UNQUALIFIED receipt only.

    Raw text must encode as actual UTF-8. JSON escapes may decode to lone
    surrogates: ensure_ascii canonical storage can retain those inertly without
    treating optional Unicode/schema rejection as loss of a known returned ID.
    Duplicate keys, nonfinite numbers, invalid raw UTF-8 and >4MiB still reject.
    This decoder never establishes ownership or validates any other checkpoint
    field; strict_json remains mandatory for every qualified receipt.
    """
    try:
        require(type(raw) is str and 0 < len(raw.encode('utf-8', errors='strict')) <= 4194304)
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProgrammeError('schema_unqualified') from None


def strict_json(raw):
    """Private serialized snapshots use the same duplicate/finite JSON contract.

    UTF-8 validity includes decoded strings, not just JSON's ASCII escape bytes.
    This is structural validation, not authentication against a forged journal.
    """
    def unicode_values(value, depth=0):
        require(depth <= 64)
        if type(value) is str:
            value.encode('utf-8', errors='strict')
        elif type(value) is list:
            for item in value:
                unicode_values(item, depth + 1)
        elif type(value) is dict:
            for key, item in value.items():
                key.encode('utf-8', errors='strict')
                unicode_values(item, depth + 1)
    try:
        value = receipt_json(raw)
        unicode_values(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProgrammeError('schema_unqualified') from None


def mentions_owner(row, identity):
    """Observation only: ANY literal reference counts, even in a conflicting row."""
    return type(row) is dict and any(type(row.get(key)) is str and row[key] == identity
                                     for key in ('RuleID', 'rule_id'))


def owner_reference(row):
    """Validate every supplied alias; missing is unknown, explicit empty is not."""
    require(type(row) is dict)
    references = []
    for key in ('RuleID', 'rule_id'):
        if key in row:
            value = row[key]
            require(value is None or type(value) is str, 'ownership_unverified')
            if value not in (None, ''):
                text(value)
            references.append(value or None)
    require(not references or all(value == references[0] for value in references), 'ownership_unverified')
    return references[0] if references else None


def owner_pairs(snapshot):
    """Explicit internal/public contradictions cannot grant ownership or absence."""
    for key, internal in snapshot['jobs'].items():
        public = snapshot['public'][key]
        left, right = owner_reference(internal), owner_reference(public)
        if any(alias in internal for alias in ('RuleID', 'rule_id')) and any(
                alias in public for alias in ('RuleID', 'rule_id')):
            require(left == right, 'ownership_unverified')


def identity_safe(identity, baseline):
    text(identity)
    require(type(baseline) is dict and identity not in baseline['rules'], 'ownership_unverified')
    require(not any(mentions_owner(row, identity) for kind in ('jobs', 'public')
                    for row in baseline[kind].values()), 'ownership_unverified')
    owner_pairs(baseline)


def inactive(row):
    require(type(row) is dict)
    # Type-sensitive: e.g. true is not integer zero; unknown flag shapes fail closed.
    for key in ('FileID', 'file_id'):
        require(key not in row or canonical(row[key]) in ('null', '""'), 'ineligible')
    for key in (*FLAGS, *(k.lower() for k in FLAGS)):
        require(key not in row or canonical(row[key]) in ('null', 'false', '""'), 'ineligible')
    for key in ('State', 'state', 'Status', 'status'):
        require(key not in row or row[key] in (None, '', 'scheduled', 'pending'), 'ineligible')


def selectors(row):
    values = row.get('Channels', [row.get('Channel')])
    require(type(values) is list and len(values) == 1)
    text(values[0], 128)
    require('Channel' not in row or row['Channel'] in (None, '', values[0]))
    return values


def rule_rows(payload):
    require(type(payload) is list and len(payload) <= 1000)
    result = {}
    for row in payload:
        require(type(row) is dict and len(row) <= 128 and len(canonical(row)) <= 131072)
        identity = text(row.get('ID'))
        text(row.get('Name'), 512)
        require(identity not in result)
        for key in MAPS:
            require(key in row and (row[key] is None or type(row[key]) is dict))
        for key in ('PaddingStart', 'PaddingEnd', 'KeepNum', 'Limit'):
            require(type(row.get(key)) is int and 0 <= row[key] <= 10**9)
        require(type(row.get('KeepOnly')) is str and type(row.get('Rerecord')) is bool)
        result[identity] = deepcopy(row)
    return result


@dataclass(frozen=True, repr=False)
class ProgrammePlan:
    """Immutable serialized complete selected airing and rule body; no mutable aliases."""
    airing_json: str
    body_json: str
    server_binding: str
    prepared_at: float

    @classmethod
    def selected(cls, channels, guide, namespace, event, *, binding, now, nonce):
        finite(now)
        require(type(binding) is str and re.fullmatch('[0-9a-f]{64}', binding))
        require(type(nonce) is str and re.fullmatch('[0-9a-f]{32}', nonce))
        try:
            series = selected_series(channels, guide, namespace, event)
            recording = selected_plan(channels, guide, namespace, event, now)
        except PreviewError as error:
            raise ProgrammeError(error.code) from None
        except RecordingError as error:
            # selected_series has already verified the full selected source/event.
            # selected_plan validates the airing before its 30min/48h timing gate.
            # Diagnose that closed failure without weakening either validator.
            if error.code == 'near_start':
                prepare_timing(event, now)
            raise ProgrammeError('selection_changed') from None
        except Exception:
            raise ProgrammeError('selection_changed') from None
        prepare_timing(event, now)
        airing = recording.body()['Airing']
        require(type(airing.get('Time')) is int and type(airing.get('Duration')) is int)
        require(3600 <= airing['Time'] - now <= 172800, 'near_start')
        selectors(airing)
        inactive(airing)
        body = dict(Name='DEV programme pass ' + nonce,
                    EQ={'SeriesID': series, 'ProgramID': text(airing.get('ProgramID'))},
                    NE=None, GT=None, LT=None, IN=None, NI=None,
                    PaddingStart=0, PaddingEnd=0, KeepOnly='', KeepNum=0, Rerecord=False, Limit=0)
        return cls(canonical(airing), canonical(body), binding, now)

    def validate(self):
        finite(self.prepared_at)
        require(type(self.server_binding) is str and re.fullmatch('[0-9a-f]{64}', self.server_binding))
        a, b = self.airing(), self.body()
        require(canonical(a) == self.airing_json and canonical(b) == self.body_json)
        text(a.get('ProgramID')); text(a.get('SeriesID')); text(a.get('Title'), 512)
        require(type(a.get('Time')) is int and 946684800 <= a['Time'] <= 4102444800)
        require(type(a.get('Duration')) is int and 1 <= a['Duration'] <= 43200)
        require(3600 <= a['Time'] - self.prepared_at <= 172800)
        selectors(a); inactive(a)
        require(type(b.get('Name')) is str and re.fullmatch('DEV programme pass [0-9a-f]{32}', b['Name']))
        expected = dict(Name=b['Name'], EQ={'SeriesID': a['SeriesID'], 'ProgramID': a['ProgramID']},
                        NE=None, GT=None, LT=None, IN=None, NI=None,
                        PaddingStart=0, PaddingEnd=0, KeepOnly='', KeepNum=0, Rerecord=False, Limit=0)
        require(canonical(b) == canonical(expected))
        require(len(self.airing_json) + len(self.body_json) < 32768)

    def airing(self): return strict_json(self.airing_json)
    def body(self): return strict_json(self.body_json)
    def conditions(self): return {k: self.body()[k] for k in MAPS}
    def private(self):
        return dict(airing_json=self.airing_json, body_json=self.body_json,
                    server_binding=self.server_binding, prepared_at=self.prepared_at)

    def fresh(self, now):
        self.validate()
        elapsed = finite(now) - self.prepared_at
        require(0 <= elapsed <= 60, 'expired')
        require(3600 <= self.airing()['Time'] - now <= 172800, 'near_start')

    def candidate(self, row):
        require(type(row) is dict)
        a = self.airing()
        require(all(k in row and canonical(row[k]) == canonical(a[k])
                    for k in ('ProgramID', 'SeriesID', 'Time', 'Duration', 'Title')))
        require(selectors(row) == selectors(a))
        inactive(row)

    def preview(self, rows, now):
        self.fresh(now)
        require(type(rows) is list and len(rows) == 1, 'preview_not_one')
        self.candidate(rows[0])
        require(rows[0]['Time'] > now, 'near_start')
        return canonical(rows)

    def rule(self, row, identity, *, computed, eligible=True):
        require(type(row) is dict and row.get('ID') == identity, 'ownership_unverified')
        require(all(k in row and canonical(row[k]) == canonical(v) for k, v in self.body().items()), 'ownership_unverified')
        if computed or 'Paused' in row:
            require(type(row.get('Paused')) is bool, 'ownership_unverified')
            if eligible:
                require(row['Paused'] is False, 'ineligible')
        if computed or 'NumJobs' in row:
            require(type(row.get('NumJobs')) is int and 0 <= row['NumJobs'] <= 1000, 'ownership_unverified')
            if eligible:
                require(row['NumJobs'] <= 1, 'ineligible')
        for key in ('Priority', 'UpdatedAt'):
            require(key not in row or row[key] is None or type(row[key]) is int, 'ownership_unverified')
        require('Duplicates' not in row or row['Duplicates'] is None or type(row['Duplicates']) is bool,
                'ownership_unverified')
        require('Image' not in row or row['Image'] is None or type(row['Image']) is str, 'ownership_unverified')
        for key in (*FLAGS, *(name.lower() for name in FLAGS)):
            require(key not in row or row[key] is None or type(row[key]) is bool or row[key] == '',
                    'ownership_unverified')
        for key in ('FileID', 'file_id', 'State', 'state', 'Status', 'status'):
            require(key not in row or row[key] is None or type(row[key]) is str, 'ownership_unverified')
        if eligible:
            inactive(row)


def original_reply(plan, row, identity):
    plan.rule(row, identity, computed=False, eligible=False)
    # Unknown response-only extensions remain quarantined in this bounded client.
    # Known computed fields are typed, but are NOT immutable ownership identity.
    allowed = set(plan.body()) | {'ID', 'NumJobs', 'Paused', 'Priority', 'UpdatedAt', 'Duplicates', 'Image'}
    allowed |= set(FLAGS) | {key.lower() for key in FLAGS}
    allowed |= {'FileID', 'file_id', 'State', 'state', 'Status', 'status'}
    require(set(row) <= allowed, 'ownership_unverified')


def qualified_job_pair(plan, identity, key, internal, public):
    """Same historical qualification for live reads and retained private rosters."""
    require(type(internal) is dict and type(public) is dict)
    require(internal.get('ID') == key and public.get('id') == key, 'ownership_unverified')
    require(owner_reference(internal) == identity, 'ownership_unverified')
    if any(alias in public for alias in ('RuleID', 'rule_id')):
        require(owner_reference(public) == identity, 'ownership_unverified')
    a = internal.get('Airing')
    plan.candidate(a)
    require(type(internal.get('Time')) is int and internal['Time'] == a['Time'])
    require(type(internal.get('Duration')) is int and internal['Duration'] == a['Duration'])
    require(selectors(internal) == selectors(a))
    inactive(internal)
    require(type(public.get('start_time')) is int and public['start_time'] == a['Time'])
    require(type(public.get('end_time')) is int and public['end_time'] == a['Time'] + a['Duration'])
    for field, expected in (('Time', a['Time']), ('Duration', a['Duration']),
                            ('ProgramID', a['ProgramID']), ('SeriesID', a['SeriesID'])):
        if field in public:
            require(canonical(public[field]) == canonical(expected))
    if 'Channels' in public or 'Channel' in public:
        require(selectors(public) == selectors(a))
    item = public.get('item')
    require(type(item) is dict and item.get('program_id') == a['ProgramID'])
    if 'series_id' in item:
        require(item['series_id'] == a['SeriesID'])
    inactive(public)
    inactive(item)


@dataclass(frozen=True, repr=False)
class RuleAbsent:
    identity: str
    server_binding: str


class ProgrammeClient(Protocol):
    server_binding_sha256: str
    def verify_status(self) -> object: ...
    def rules(self) -> object: ...
    def jobs(self) -> object: ...
    def public_jobs(self) -> object: ...
    def rule_detail(self, identity: str) -> object: ...
    def job_detail(self, identity: str) -> object: ...
    def public_job_detail(self, identity: str) -> object: ...
    def create(self, command: 'ProgrammeCommand', *, confirmed: bool) -> object: ...
    def remove_owned(self, command: 'ProgrammeCommand', *, confirmed: bool) -> object: ...


def fresh_removed_absence(client: ProgrammeClient, saved, *, now, check_open=lambda: None):
    """Fresh successor admission, NEVER terminal receipt recognition or a checkpoint.

    Read the immutable predecessor's binding, named rule, full rules and BOTH job
    lists, including retained/detached IDs. A copy is not resumed with a writable
    callback: new observations cannot rewrite a removed receipt or its archives.
    """
    validate_checkpoint(saved)
    require(saved['state'] == 'removed', 'duplicate_blocked')
    binding, identity = saved['plan']['server_binding'], text(saved['returned_id'])
    require(client.server_binding_sha256 == binding, 'binding_mismatch')
    require(finite(now) >= saved['last_clock'], 'expired')
    check_open()
    client.verify_status()
    rules = rule_rows(client.rules())
    try:
        jobs, public = internal_jobs(client.jobs()), public_jobs(client.public_jobs())
    except Exception:
        raise ProgrammeError('schema_unqualified') from None
    require(set(jobs) == set(public))
    snapshot = dict(rules=rules, jobs=jobs, public=public)
    owner_pairs(snapshot)
    detail = client.rule_detail(identity)
    require(identity not in rules and type(detail) is RuleAbsent
            and detail.identity == identity and detail.server_binding == binding, 'ownership_unverified')
    require(not any(mentions_owner(row, identity) for kind in ('jobs', 'public')
                    for row in snapshot[kind].values()), 'ineligible')
    require(not ((set(saved['roster']) | set(saved['associated_ids'])) &
                 (set(jobs) | set(public))), 'ineligible')
    client.verify_status()
    require(client.server_binding_sha256 == binding, 'binding_mismatch')
    check_open()


class ProgrammeCommand:
    """Worker-confined one-shot authority; write-ahead and decoded receipts are durable.

    Admission is consumed by the common HTTP dispatch, including direct _send.
    A restored unknown intent can only read. The returned ID is never discovered
    by scanning names or by borrowing another lifecycle's receipt.
    """
    def __init__(self, client: ProgrammeClient, plan: ProgrammePlan, *, checkpoint, clock,
                 check_open=lambda: None, predecessor=None):
        require(type(plan) is ProgrammePlan and callable(checkpoint))
        plan.validate()
        self.client, self.plan = client, plan
        self._checkpoint, self.clock, self.check_open = checkpoint, clock, check_open
        self.state = 'ready'
        self.identity: str | None = None
        self.baseline: dict[str, dict[str, dict]] | None = None
        self.reply: object = None
        self.raw_reply: str | None = None
        self.preview_json: str | None = None
        self.roster: dict[str, dict[str, dict]] = {}
        self.roster_frozen = False
        self.associated_ids: set[str] = set()
        self.absence_proof: dict[str, object] | None = None
        self._last_clock = plan.prepared_at
        self.can_remove = False
        self.reason: str | None = None
        self._arm: str | None = None
        self._poison = self._reply_bad = False
        self._predecessor_json: str | None = None
        if predecessor is not None:
            validate_checkpoint(predecessor)
            require(predecessor['state'] == 'removed', 'duplicate_blocked')
            require(predecessor['plan']['server_binding'] == plan.server_binding, 'binding_mismatch')
            require(ProgrammePlan(**predecessor['plan']).body()['Name'] != plan.body()['Name'], 'duplicate_blocked')
            self._predecessor_json = canonical(predecessor)

    def fresh_predecessor(self):
        """Ephemeral successor proof survives intent fsync, never grants resume retry."""
        if self._predecessor_json is not None:
            # This immutable string was serialized from a validated checkpoint,
            # not an HTTP receipt (whose separate 4MiB transport cap still holds).
            fresh_removed_absence(self.client, json.loads(self._predecessor_json),
                                 now=self._now(), check_open=self.check_open)

    def bound(self):
        require(self.client.server_binding_sha256 == self.plan.server_binding, 'binding_mismatch')

    def private_checkpoint(self):
        return deepcopy(dict(state=self.state, returned_id=self.identity, plan=self.plan.private(),
                             baseline=self.baseline, reply=self.reply, raw_reply=self.raw_reply,
                             preview_json=self.preview_json, roster=self.roster,
                             roster_frozen=self.roster_frozen, associated_ids=sorted(self.associated_ids),
                             last_clock=self._last_clock, absence_proof=self.absence_proof,
                             reply_bad=self._reply_bad))

    def _now(self):
        now = finite(self.clock())
        require(now >= self._last_clock, 'expired')
        self._last_clock = now
        return now

    def persist(self):
        require(not self._poison, 'checkpoint_failed')
        try:
            self._checkpoint(self.private_checkpoint())
        except Exception as error:
            self._poison, self.can_remove, self._arm = True, False, None
            if isinstance(error, ProgrammeError) and error.code == 'history_limit':
                raise
            raise ProgrammeError('checkpoint_failed') from None

    @classmethod
    def resume(cls, client, saved, **kwargs):
        validate_checkpoint(saved)
        p = ProgrammePlan(**saved['plan'])
        require(client.server_binding_sha256 == p.server_binding, 'binding_mismatch')
        c = cls(client, p, **kwargs)
        c.state, c.identity = saved['state'], saved['returned_id']
        c.baseline, c.reply, c.raw_reply = deepcopy(saved['baseline']), deepcopy(saved['reply']), saved['raw_reply']
        c.preview_json, c.roster, c._reply_bad = saved['preview_json'], deepcopy(saved['roster']), saved['reply_bad']
        c.roster_frozen = saved['roster_frozen']
        c.associated_ids = set(saved['associated_ids'])
        c._last_clock = saved['last_clock']
        c.absence_proof = deepcopy(saved['absence_proof'])
        return c

    def prepare(self, preview):
        require(self.state == 'ready', 'duplicate_blocked')
        self.preview_json = self.plan.preview(preview, self._now())
        return 'prepared'

    def snapshots(self):
        self.bound()
        self.client.verify_status()
        rules = rule_rows(self.client.rules())
        raw_jobs, raw_public = self.client.jobs(), self.client.public_jobs()
        # Retain ANY explicit linked identity before schema/alias qualification.
        # A malformed other alias or a later detached row cannot erase this read.
        observed = set()
        if self.identity is not None:
            for payload, key in ((raw_jobs, 'ID'), (raw_public, 'id')):
                if type(payload) is list and len(payload) <= 1000:
                    for row in payload:
                        if mentions_owner(row, self.identity):
                            observed.add(text(row.get(key)))
            if not observed <= self.associated_ids:
                require(len(self.associated_ids | observed) <= 1000, 'ineligible')
                self.associated_ids |= observed
                self.persist()
        try:
            jobs, public = internal_jobs(raw_jobs), public_jobs(raw_public)
        except Exception:
            raise ProgrammeError('schema_unqualified') from None
        require(set(jobs) == set(public))
        snapshot = dict(rules=rules, jobs=jobs, public=public)
        owner_pairs(snapshot)
        return snapshot

    def _write(self, confirmed):
        require(confirmed is True, 'confirmation_required')
        require(not self._poison, 'checkpoint_failed')
        self.bound()
        self.check_open()

    def save(self, *, confirmed=False):
        self._write(confirmed)
        require(self.state == 'ready' and self.baseline is None, 'duplicate_blocked')
        preview_json = self.preview_json
        if preview_json is None:
            raise ProgrammeError('preview_not_one')
        self.plan.preview(json.loads(preview_json), self._now())
        self.fresh_predecessor()
        baseline = self.snapshots()
        eq = self.plan.conditions()['EQ']
        require(not any(type(r.get('EQ')) is dict and all(r['EQ'].get(k) == v for k, v in eq.items())
                        for r in baseline['rules'].values()), 'duplicate_blocked')
        require(canonical(baseline) == canonical(self.snapshots()), 'ownership_unverified')
        self.baseline, self.state = baseline, 'create_unknown'
        self.persist()  # intent BEFORE dispatch; failed/untimely attempts remain latched
        self.fresh_predecessor()
        require(canonical(baseline) == canonical(self.snapshots()), 'ownership_unverified')
        self.plan.fresh(self._now())  # fsync and all final reads consume the same TTL
        self._write(confirmed)
        self._arm = 'create'
        try:
            self.client.create(self, confirmed=True)
        except Exception as error:
            if self._poison:
                raise ProgrammeError('checkpoint_failed') from None
            if isinstance(error, ProgrammeError) and error.code == 'closed':
                raise
            return self.state
        finally:
            self._arm = None
        return self.status()

    def admit(self, operation, binding, confirmed):
        self._write(confirmed)
        require(binding == self.plan.server_binding and self._arm == operation, 'ownership_unverified')
        require(self.baseline is not None)
        self._arm = None  # burn before any final read, including a failing proof
        if operation == 'create':
            require(self.state == 'create_unknown' and self.identity is None)
            self.fresh_predecessor()  # direct common HTTP dispatch cannot skip this proof
            require(canonical(self.snapshots()) == canonical(self.baseline), 'ownership_unverified')
            self.plan.fresh(self._now())
        elif operation == 'remove':
            require(self.state == 'remove_unknown' and self.identity is not None)
            self._identity_safe()
            self._owned()  # common dispatch rechecks FULL proof, including rule/job flags
            self._future()
        else:
            raise ProgrammeError('ownership_unverified')
        self._write(confirmed)  # close/config/session signal during final proof blocks dispatch

    def decoded_create(self, value, raw):
        require(self.state == 'create_unknown' and self.reply is None)
        self.reply, self.raw_reply = deepcopy(value), raw
        try:
            self.identity = text(value.get('ID')) if type(value) is dict else None
        except ProgrammeError:
            self.identity = None
        # Checkpoint decoded identity/raw bytes FIRST, initially unqualified. A
        # validation/deadline failure cannot manufacture a lost returned ID.
        self._reply_bad = True
        self.persist()
        # Retain even a colliding/invalid candidate, but never grant it authority.
        try:
            self._identity_safe()
            require(canonical(strict_json(raw)) == canonical(value))
            original_reply(self.plan, value, self.identity)
        except Exception:
            return
        self._reply_bad = False
        self.persist()  # immediately after decode, BEFORE transport deadline check

    def _identity_safe(self):
        identity_safe(self.identity, self.baseline)

    def _future(self):
        now = self._now()
        require(now >= self.plan.prepared_at, 'expired')
        require(self.plan.airing()['Time'] - now > 95, 'near_start')

    def _associated(self, snapshot, *, qualify):
        identity = text(self.identity)
        baseline = self.baseline
        if baseline is None:
            raise ProgrammeError('ownership_unverified')
        owner_pairs(snapshot)
        associated = {k: r for k, r in snapshot['jobs'].items() if owner_reference(r) == identity}
        # A public association not visible internally is not permission to omit it.
        for key, row in snapshot['public'].items():
            if mentions_owner(row, identity):
                require(key in associated, 'ownership_unverified')
        require(len(associated) <= 1, 'ineligible')
        require(set(associated) == self.associated_ids, 'ineligible')
        for key, row in associated.items():
            require(key not in baseline['jobs'] and key not in baseline['public'], 'ownership_unverified')
            public = snapshot['public'][key]
            qualified_job_pair(self.plan, identity, key, row, public)
            require(canonical(self.client.job_detail(key)) == canonical(row), 'ownership_unverified')
            require(canonical(self.client.public_job_detail(key)) == canonical(public), 'ownership_unverified')
            if key in self.roster:
                require(canonical(self.roster[key]) == canonical(dict(internal=row, public=public)), 'ineligible')
        if qualify:
            # Existing retained/detached IDs cannot silently disappear from proof.
            require((set(self.roster) == set(associated) if self.roster_frozen
                     else set(self.roster) <= set(associated)), 'ineligible')
        return {key: dict(internal=row, public=snapshot['public'][key]) for key, row in associated.items()}

    def _rule_proof(self):
        identity = text(self.identity)
        self._identity_safe()
        require(not self._reply_bad and self.reply is not None, 'ownership_unverified')
        original_reply(self.plan, self.reply, identity)
        snapshot = self.snapshots()
        row = snapshot['rules'].get(identity)
        self.plan.rule(row, identity, computed=True, eligible=False)
        detail = self.client.rule_detail(identity)
        require(canonical(detail) == canonical(row), 'ownership_unverified')
        # Original body+ID are immutable; server-computed NumJobs/Priority/UpdatedAt
        # may legitimately change. Current WHOLE list/detail must still agree.
        return snapshot, row

    def _owned(self):
        identity = text(self.identity)
        snapshot, row = self._rule_proof()
        self.plan.rule(row, identity, computed=True)
        roster = self._associated(snapshot, qualify=True)
        require(row['NumJobs'] == len(roster), 'ineligible')
        self._future()
        # A final named read catches flags changing during job/detail qualification.
        require(canonical(self.client.rule_detail(identity)) == canonical(row), 'ineligible')
        return roster

    def _absence(self):
        identity = text(self.identity)
        self._identity_safe()
        snapshot = self.snapshots()
        detail = self.client.rule_detail(identity)
        require(identity not in snapshot['rules'] and type(detail) is RuleAbsent
                and detail.identity == identity and detail.server_binding == self.plan.server_binding,
                'ownership_unverified')
        require(not any(mentions_owner(row, identity) for kind in ('jobs', 'public')
                        for row in snapshot[kind].values()), 'ineligible')
        require(not ((set(self.roster) | self.associated_ids) &
                     (set(snapshot['jobs']) | set(snapshot['public']))), 'ineligible')
        self.client.verify_status()
        return dict(identity=identity, server_binding=self.plan.server_binding, checked_at=self._now(),
                    rule_list_absent=True, named_detail_absent=True, linked_jobs_absent=True,
                    roster_ids_absent=True, associated_ids_absent=True, final_pin_verified=True,
                    detail_contract='GET404/application-json/null', roster_ids=sorted(self.roster),
                    associated_ids=sorted(self.associated_ids))

    def status(self):
        self.bound()
        require(not self._poison, 'checkpoint_failed')
        self.can_remove, self.reason = False, None
        if self.state == 'removed':
            return self.state  # durable receipt recognition, no fresh absence claim
        if self.state == 'remove_unknown':
            try:
                proof = self._absence()
            except Exception:
                self.reason = 'ownership_unverified'
                return self.state
            self.absence_proof, self.state = proof, 'removed'
            try:
                self.persist()
            except Exception:
                self.state = 'remove_unknown'
                self.absence_proof = None
                raise
            return self.state
        if self.identity is None or self._reply_bad:
            return self.state
        try:
            self._rule_proof()
        except Exception as error:
            self.reason = error.code if isinstance(error, ProgrammeError) else 'ownership_unverified'
            return self.state
        # Rule ownership and removal eligibility are distinct. Unsupported/active
        # associations retain the owned rule and a read-only ineligibility reason.
        self.state = 'owned'
        try:
            roster = self._owned()
        except Exception as error:
            self.reason = error.code if isinstance(error, ProgrammeError) else 'ownership_unverified'
            self.persist()
            return self.state
        self.roster, self.state = roster, 'owned'
        self.roster_frozen = True
        self.persist()
        self.can_remove = True
        return self.state

    def remove_owned(self, *, confirmed=False):
        self._write(confirmed)
        require(self.state == 'owned', 'ownership_unverified')
        self.can_remove = False
        self.roster = self._owned()
        self.state = 'remove_unknown'
        self.persist()
        self.roster = self._owned()  # repeat FULL proof/flags/lead after fsync
        self._write(confirmed)
        self._future()
        self._arm = 'remove'
        try:
            self.client.remove_owned(self, confirmed=True)
        except Exception:
            if self._poison:
                raise ProgrammeError('checkpoint_failed') from None
            return self.state
        finally:
            self._arm = None
        return self.status()  # DELETE reply itself never establishes absence


def _validate_checkpoint(value):
    require(type(value) is dict and set(value) == {'state', 'returned_id', 'plan', 'baseline',
            'reply', 'raw_reply', 'preview_json', 'roster', 'roster_frozen', 'associated_ids',
            'last_clock', 'absence_proof', 'reply_bad'})
    require(type(value['state']) is str and value['state'] in STATES)
    require(type(value['plan']) is dict and set(value['plan']) == {'airing_json', 'body_json', 'server_binding', 'prepared_at'})
    p = ProgrammePlan(**value['plan']); p.validate()
    require(finite(value['last_clock']) >= p.prepared_at)
    require(type(value['preview_json']) is str)
    require(p.preview(strict_json(value['preview_json']), p.prepared_at) == value['preview_json'])
    base = value['baseline']
    require(type(base) is dict and set(base) == {'rules', 'jobs', 'public'})
    strict_json(canonical(base))  # receipt quarantine never relaxes baseline Unicode
    require(all(type(base[k]) is dict for k in base))
    require(canonical(rule_rows(list(base['rules'].values()))) == canonical(base['rules']))
    require(canonical(internal_jobs(list(base['jobs'].values()))) == canonical(base['jobs']))
    require(canonical(public_jobs(list(base['public'].values()))) == canonical(base['public']))
    require(set(base['jobs']) == set(base['public']))
    owner_pairs(base)
    require(type(value['roster']) is dict and len(value['roster']) <= 1 and type(value['reply_bad']) is bool)
    strict_json(canonical(value['roster']))  # qualified historical roster stays strict
    require(type(value['roster_frozen']) is bool)
    require(type(value['associated_ids']) is list and len(value['associated_ids']) <= 1000)
    require(value['associated_ids'] == sorted({text(key) for key in value['associated_ids']}))
    require(set(value['roster']) <= set(value['associated_ids']))
    for key, row in value['roster'].items():
        text(key)
        require(key not in base['jobs'] and key not in base['public'])
        require(type(row) is dict and set(row) == {'internal', 'public'})
        qualified_job_pair(p, value['returned_id'], key, row['internal'], row['public'])
    if value['returned_id'] is not None:
        text(value['returned_id'])
        require(type(value['reply']) is dict and value['reply'].get('ID') == value['returned_id'])
    if value['state'] in ('owned', 'remove_unknown', 'removed'):
        require(value['returned_id'] is not None and not value['reply_bad'])
        identity_safe(value['returned_id'], base)
        original_reply(p, value['reply'], value['returned_id'])
    if value['roster']:
        require(value['roster_frozen'] and value['state'] in ('owned', 'remove_unknown', 'removed'))
    if value['raw_reply'] is None:
        require(value['reply'] is None and value['returned_id'] is None and value['state'] == 'create_unknown')
    else:
        quarantine = value['state'] == 'create_unknown' and value['reply_bad'] is True
        if quarantine:
            require(not value['roster'] and not value['roster_frozen'] and not value['associated_ids'])
        decode = receipt_json if quarantine else strict_json
        require(canonical(decode(value['raw_reply'])) == canonical(value['reply']))
        if not value['reply_bad']:
            identity_safe(value['returned_id'], base)
            original_reply(p, value['reply'], value['returned_id'])
    proof = value['absence_proof']
    if value['state'] != 'removed':
        require(proof is None)
        return
    require(type(proof) is dict and set(proof) == {
        'identity', 'server_binding', 'checked_at', 'rule_list_absent', 'named_detail_absent',
        'linked_jobs_absent', 'roster_ids_absent', 'associated_ids_absent', 'final_pin_verified',
        'detail_contract', 'roster_ids', 'associated_ids'})
    require(proof['identity'] == value['returned_id'] and proof['server_binding'] == p.server_binding)
    require(finite(proof['checked_at']) == value['last_clock'] and proof['checked_at'] >= p.prepared_at)
    require(proof['detail_contract'] == 'GET404/application-json/null')
    require(proof['roster_ids'] == sorted(value['roster']) and proof['associated_ids'] == value['associated_ids'])
    require(all(proof[key] is True for key in ('rule_list_absent', 'named_detail_absent',
            'linked_jobs_absent', 'roster_ids_absent', 'associated_ids_absent', 'final_pin_verified')))


def validate_checkpoint(value):
    """Fail closed locally before HTTP, including terminal receipt recognition.

    A coherent forged entire private document is not authenticated by this schema;
    it validates retained protocol evidence and rejects corrupt/incomplete labels.
    Historical roster checks deliberately use the frozen plan, not today's clock.
    """
    try:
        _validate_checkpoint(value)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        raise ProgrammeError('schema_unqualified') from None
