"""Closed Series storage proofs; no network, SDK, filesystem or SQL execution.

Absent roots, the explicitly versioned v3 fixture and the closed Series-manager
protocol have separate proof factories. Fixture exclusion gates and unknown
envelopes are never inferred from dictionaries. GET producers supply full reads.
"""
import base64
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import time

from .operation_storage import Scope, Snapshot
from .metadata_http import ChannelsMetadataClient, StatusSample, status_sample, _origin, _unique_object, _reject_constant, _finite_float
from .newbuild_compatibility import BASE_MINIMUM_DVR_BUILD, parse_build, supported_build, version_admitted
from .series_models import RuleSnapshot, RuleRef, RuleAuthority, Provenance, SparseMutationReply, identity, require
from . import series_contracts as contracts

MAX_BYTES = 8388608
MAX_AGE = 30.0  # Fresh read evidence only, NEVER a claim lease/takeover deadline.
PROTOCOL = 'series-zero-match-v3-production-contracts'
PHASES = ('prepare','create','pause','rename','resume','remove')
STATES = ('prepared','created','paused','renamed','resumed','complete')
_REQUEST, _READS, _PROOF = object(), object(), object()
_QUARANTINE = object()


def _hex(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None


def _canonical(value):
    budget = [500000]
    def visit(item,depth):
        budget[0] -= 1
        require(budget[0] >= 0 and depth <= 32,'shape')
        kind = type(item)
        if item is None or kind is bool: return
        if kind is int: require(abs(item) <= 9223372036854775806,'shape')
        elif kind is float: require(math.isfinite(item),'shape')
        elif kind is str: require(len(item) <= MAX_BYTES and '\x00' not in item,'shape')
        elif kind is list:
            for child in item: visit(child,depth+1)
        elif kind is dict:
            require(all(type(k) is str and 0 < len(k) <= 4096 for k in item),'shape')
            for k,v in item.items(): visit(k,depth+1); visit(v,depth+1)
        else: require(False,'shape')
    visit(value,0)
    text = json.dumps(value,sort_keys=True,ensure_ascii=True,allow_nan=False,separators=(',',':'))
    require(len(text.encode('ascii')) <= MAX_BYTES,'shape')
    return text


def _loads(raw):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES,'shape')
    value = json.loads(raw.decode('utf-8'),object_pairs_hook=_unique_object,
                       parse_constant=_reject_constant,parse_float=_finite_float)
    require(_canonical(value).encode('ascii') == raw,'shape')
    return value


@dataclass(frozen=True, repr=False)
class SeriesSource:
    origin: str = field(repr=False)
    namespace: str = field(repr=False)
    expected_version: str = field(repr=False)
    actual_version: str | None = field(repr=False)

    def __post_init__(self):
        require(type(self.origin) is str and _origin(self.origin) == self.origin,'shape')
        require(type(self.namespace) is str and len(self.namespace) <= 4096
                and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*',self.namespace) is not None,'shape')
        require(type(self.expected_version) is str and parse_build(self.expected_version) is not None,'shape')
        require(self.actual_version is None or supported_build(self.actual_version),'shape')

    def fixture_binding(self):
        return dict(origin=self.origin,namespace=self.namespace,minimum_build=BASE_MINIMUM_DVR_BUILD,actual_build=self.actual_version)

    def __repr__(self): return 'SeriesSource(private=True)'


def _mint(cls,seal,**values):
    result = object.__new__(cls)
    for key,value in dict(values,_seal=seal).items(): object.__setattr__(result,key,value)
    return result


@dataclass(frozen=True, init=False, repr=False)
class SeriesReconciliationRequest:
    _snapshot: Snapshot
    _source: SeriesSource
    _issued_at: float
    _seal: object

    def __repr__(self): return 'SeriesReconciliationRequest(private=True)'


def issue_request(snapshot,source):
    require(type(snapshot) is Snapshot and snapshot.scope is Scope.SERIES and snapshot.owner is not None
            and snapshot.barrier is not None and type(source) is SeriesSource,'shape')
    return _mint(SeriesReconciliationRequest,_REQUEST,_snapshot=snapshot,_source=source,_issued_at=time.monotonic())


def issue_manager_request(snapshot, source):
    require(type(snapshot) is Snapshot and snapshot.scope is Scope.SERIES and snapshot.owner is not None
            and type(source) is SeriesSource, 'shape')
    if snapshot.payload is not None:
        from .series_manager_store import decode
        from .newbuild_compatibility import profile_binding
        root = decode(snapshot.payload)  # No unknown/legacy protocol reinterpretation.
        require(root['binding'] == profile_binding(source.origin, source.namespace, source.expected_version), 'stale_reference')
    return _mint(SeriesReconciliationRequest, _REQUEST, _snapshot=snapshot, _source=source,
                 _issued_at=time.monotonic(), _manager=True)


def _request(request):
    require(type(request) is SeriesReconciliationRequest and getattr(request,'_seal',None) is _REQUEST,'shape')
    return request._snapshot,request._source


def challenged_snapshot(request):
    """Immutable sealed basis, independent of any store's moving load baseline."""
    return _request(request)[0]


def _status(request,client,expected_sample=None):
    _,source = _request(request)
    require(type(client) is ChannelsMetadataClient,'shape')
    sample = status_sample(client)
    require(type(sample) is StatusSample and (expected_sample is None or sample is expected_sample),'stale_reference')
    assert sample is not None
    now = time.monotonic()
    require(math.isfinite(now) and request._issued_at <= sample.observed_at <= now
            and now - sample.observed_at <= MAX_AGE,'stale_reference')
    require(client._origin == source.origin and client.namespace == source.namespace
            and client._expected_version == source.expected_version,'stale_reference')
    require(supported_build(sample.reported_version) and version_admitted(source.expected_version,sample.reported_version)
            and (source.actual_version is None or sample.reported_version == source.actual_version),'stale_reference')
    return sample


def _array(value):
    require(type(value) is list and len(value) <= 10000 and all(type(r) is dict for r in value),'shape')
    _canonical(value)


def _activity(value):
    require(type(value) is dict and set(value) == {'streams','recordings','workers','other','queues'},'shape')
    require(all(type(value[k]) is list and not value[k] for k in ('streams','recordings','workers','other')))
    require(type(value['queues']) is dict and len(value['queues']) <= 256
            and all(type(v) is int and v == 0 for v in value['queues'].values()))


@dataclass(frozen=True, init=False, repr=False)
class SeriesReadOnlySnapshot:
    _request: SeriesReconciliationRequest
    _client: ChannelsMetadataClient
    _sample: StatusSample
    _json: str
    _identity: str
    _seal: object

    @classmethod
    def from_complete_reads(cls,request,status_client,*,rules,jobs,public,activity,detail,detail_identity,detail_status,detail_media_type):
        """Trusted GET producer supplies whole arrays, never filtered/projected rows.

        Complete RuleSnapshot DTOs preserve unknown fields. Named absence needs
        exact status/media/literal-null, not a boolean. This is not a transport.
        """
        sample = _status(request,status_client)
        require(type(rules) is tuple and len(rules) <= 10000 and all(type(r) is RuleSnapshot for r in rules),'shape')
        rows = [r.payload() for r in rules]
        require(len({r['ID'] for r in rows}) == len(rows),'shape')
        _array(jobs); _array(public); _activity(activity)
        require(type(detail_status) is int and detail_status in (200,404) and detail_media_type == 'application/json','shape')
        require((detail_status == 200 and type(detail) is RuleSnapshot) or (detail_status == 404 and detail is None),'shape')
        identity(detail_identity)
        if detail is not None: require(detail.payload()['ID'] == detail_identity)
        value = dict(snapshot=dict(rules=rows,jobs=jobs,public=public,activity=activity),
                     detail=detail.payload() if detail is not None else None,detail_absent=detail_status == 404)
        return _mint(cls,_READS,_request=request,_client=status_client,_sample=sample,_json=_canonical(value),_identity=detail_identity)

    def __repr__(self): return 'SeriesReadOnlySnapshot(private=True)'


def _body(nonce):
    return dict(Name='DEV Series v3 '+nonce,EQ=dict(SeriesID='dev-series-v3-'+nonce,Title='dev-title-v3-'+nonce),
        NE=None,GT=None,LT=None,IN=None,NI=None,PaddingStart=0,PaddingEnd=0,KeepOnly='',KeepNum=0,Rerecord=False,Limit=0)


def _wire(entry):
    require(type(entry) is dict and set(entry) <= {'phase','status','raw_base64','complete','decoded'}
            and {'phase','status','raw_base64','complete'} <= set(entry),'shape')
    require(entry['phase'] in PHASES and type(entry['status']) is int and 100 <= entry['status'] <= 599
            and type(entry['complete']) is bool and type(entry['raw_base64']) is str,'shape')
    raw = base64.b64decode(entry['raw_base64'],validate=True)
    require(len(raw) <= 4194304,'shape')
    require(not entry['complete'] or 'decoded' in entry)
    if 'decoded' in entry:
        # Never accept the fixture's storage-bound projection as a full reply.
        value = json.loads(raw.decode('utf-8'),object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant,parse_float=_finite_float) if raw else None
        require(_canonical(value) == _canonical(entry['decoded']))
    return entry


def _basis(snapshot,source):
    r = _loads(snapshot.payload)
    require(type(r) is dict and set(r) == {'protocol','secret','nonce','binding','state','attempts','baseline',
        'returned_id','candidate_id','wire','captures','last_rule','authority','reconciled','contract_blocked','preview_at'},'shape')
    require(r['protocol'] == PROTOCOL and _hex(r['secret']) and _hex(r['nonce']),'shape')
    require(r['contract_blocked'] is False and r['binding'] == source.fixture_binding()
            and source.actual_version is not None,'stale_reference')
    attempts = r['attempts']
    require(type(attempts) is list and 2 <= len(attempts) <= 6,'shape')
    for index,attempt in enumerate(attempts):
        require(type(attempt) is dict and set(attempt) == {'phase','intent','body','dispatched'}
                and attempt['phase'] == PHASES[index] and _hex(attempt['intent'])
                and type(attempt['dispatched']) is bool,'shape')
        phase,body = attempt['phase'],attempt['body']
        expected = _body(r['nonce'])
        if phase == 'prepare': require(body == {k:expected[k] for k in ('EQ','NE','GT','LT','IN','NI')})
        elif phase == 'create': require(_canonical(body) == _canonical(expected))
        elif phase == 'rename':
            require(type(body) is dict and body.get('ID') == r['returned_id'] and body.get('Name') == expected['Name']+' renamed')
        else: require(body is None)
    require(len({a['intent'] for a in attempts}) == len(attempts),'shape')
    phase = attempts[-1]['phase']
    require(r['state'] == phase+'_unknown' and phase in PHASES[1:],'shape')
    identity(r['returned_id']); require(r['candidate_id'] == r['returned_id'])
    require(type(r['preview_at']) in (int,float) and math.isfinite(r['preview_at']) and r['preview_at'] > 0,'shape')
    baseline = r['baseline']
    require(type(baseline) is dict and set(baseline) == {'rules','jobs','public','activity'},'shape')
    for key in ('rules','jobs','public'): _array(baseline[key])
    _activity(baseline['activity'])
    base_rules = [RuleSnapshot.parse(row) for row in baseline['rules']]
    require(len({row.payload()['ID'] for row in base_rules}) == len(base_rules),'shape')
    require(all(row.payload()['ID'] != r['returned_id'] for row in base_rules))
    require(type(r['captures']) is list and len(r['captures']) < 24,'shape')
    for capture in r['captures']:
        require(type(capture) is dict and set(capture) == {'phase','stage','snapshot','detail','detail_absent'}
                and capture['phase'] in PHASES and capture['stage'] in ('before','after','recovery')
                and type(capture['detail_absent']) is bool,'shape')
        require(type(capture['snapshot']) is dict and set(capture['snapshot']) == {'rules','jobs','public','activity'},'shape')
        for key in ('rules','jobs','public'): _array(capture['snapshot'][key])
    require(type(r['reconciled']) is list and len(set(r['reconciled'])) == len(r['reconciled'])
            and all(p in PHASES[1:len(attempts)] for p in r['reconciled']) and phase not in r['reconciled'],'shape')
    require(type(r['wire']) is list and len(r['wire']) <= len(attempts),'shape')
    require(len({w.get('phase') for w in r['wire'] if type(w) is dict}) == len(r['wire']),'shape')
    for entry in r['wire']:
        _wire(entry); require(entry['phase'] in PHASES[:len(attempts)],'shape')
    create = [w for w in r['wire'] if w['phase'] == 'create']
    require(len(create) == 1 and create[0]['complete'] is True and create[0]['status'] == 200 and 'decoded' in create[0])
    create_row = RuleSnapshot.parse(create[0]['decoded'])
    binding = hashlib.sha256(_canonical(r['binding']).encode('ascii')).hexdigest()
    created = contracts.validate_create(_body(r['nonce']),r['returned_id'],create[0]['decoded'],create_row,binding,receipt_id=attempts[1]['intent'])
    if phase == 'create': require(r['last_rule'] is None and r['authority'] is None)
    else:
        before = RuleSnapshot.parse(r['last_rule']); auth = r['authority']
        require(type(auth) is dict and set(auth) == {'provenance','receipt_id','ref'} and auth['provenance'] == 'created')
        authority = RuleAuthority(RuleRef(**auth['ref']),Provenance.CREATED,auth['receipt_id'])
        require(authority.receipt_id == created.receipt_id and authority.ref.identity == r['returned_id'])
        contracts.assert_fresh(authority.ref,before,binding)
        expected = _body(r['nonce']); old = before.payload()
        require(all(k in old and _canonical(old[k]) == _canonical(v) for k,v in expected.items() if k != 'Name'))
        require(old['Name'] == expected['Name'] + (' renamed' if phase in ('resume','remove') else ''))
        require(old['Paused'] is (phase in ('rename','resume')) and old['NumJobs'] == 0 and old['Priority'] == 0)
        if phase == 'pause': require(before._json == create_row._json)
        if phase == 'rename':
            planned = dict(old,Name=expected['Name']+' renamed')
            require(_canonical(attempts[-1]['body']) == _canonical(planned))
    return r,phase,binding,created


def _mentions(value,r):
    if type(value) is str: return r['nonce'] in value or value == r['returned_id']
    # Numeric references are not qualified by the rule DTO contract. A possible
    # owner alias is a reason to STOP, never to normalize/adopt it as ownership.
    if type(value) is int and r['returned_id'].isascii() and r['returned_id'].isdecimal():
        return value == int(r['returned_id'])
    if type(value) is list: return any(_mentions(v,r) for v in value)
    if type(value) is dict: return any(_mentions(k,r) or _mentions(v,r) for k,v in value.items())
    return False


def _arrays(a,b): return sorted(_canonical(v) for v in a) == sorted(_canonical(v) for v in b)


def _ref(ref):
    return dict(binding=ref.binding,identity=ref.identity,revision=ref.revision,digest=ref.digest)


@dataclass(frozen=True, init=False, repr=False)
class SeriesReconciliationProof:
    _request: SeriesReconciliationRequest
    _client: ChannelsMetadataClient
    _sample: StatusSample
    _successor: bytes | None
    _seal: object

    @classmethod
    def manager_v1(cls, request, observation):
        from .series_http import checked_manager_reads, SourceStatusRefusal
        if type(observation) is SourceStatusRefusal:
            return manager_refusal_proof(request, observation)
        from .series_manager_reconciliation import successor
        from .newbuild_compatibility import profile_binding
        snapshot, source = _request(request)
        require(getattr(request, '_manager', False) is True, 'shape')
        observation = checked_manager_reads(observation, request)
        sample = _status(request, observation._status_client)
        binding = profile_binding(source.origin, source.namespace, source.expected_version)
        if snapshot.payload is None:
            require(snapshot.revision == 0 and snapshot.fingerprint is None and observation._json is None
                    and observation._failure is None, 'shape')
            result, disposition = None, 'clear'
        else:
            from .series_manager_models import ManagerError
            from .series_manager_store import decode, encode
            try:
                require(observation._failure is None and observation._json is not None, 'shape')
                result, disposition = successor(snapshot.payload, binding, sample.reported_version, json.loads(observation._json))
            except ManagerError as error:
                if error.code in {'activity', 'capacity'}:
                    raise
                root = decode(snapshot.payload)
                root['blocked'] = True
                result, disposition = encode(root), 'blocked'
            except Exception:
                root = decode(snapshot.payload)
                root['blocked'] = True
                result, disposition = encode(root), 'blocked'
        return _mint(cls, _PROOF, _request=request, _client=observation._status_client,
                     _sample=sample, _successor=result, _manager=True, _disposition=disposition)

    @classmethod
    def absent_root(cls,request,status_client):
        snapshot,_ = _request(request)
        require(snapshot.payload is None and snapshot.revision == 0 and snapshot.fingerprint is None,'shape')
        sample = _status(request,status_client)
        return _mint(cls,_PROOF,_request=request,_client=status_client,_sample=sample,_successor=None)

    @classmethod
    def v3_zero_match(cls,request,observation):
        snapshot,source = _request(request)
        require(type(observation) is SeriesReadOnlySnapshot and getattr(observation,'_seal',None) is _READS
                and observation._request is request,'shape')
        sample = _status(request,observation._client,observation._sample)
        r,phase,binding,created = _basis(snapshot,source)
        require(observation._identity == r['returned_id'],'stale_reference')
        entry = json.loads(observation._json); current,baseline = entry['snapshot'],r['baseline']
        owned = [row for row in current['rules'] if row['ID'] == r['returned_id']]
        foreign = [row for row in current['rules'] if row['ID'] != r['returned_id']]
        require(not any(_mentions(row,r) for row in foreign))
        require(_arrays(foreign,baseline['rules']) and all(_arrays(current[k],baseline[k]) for k in ('jobs','public'))
                and _canonical(current['activity']) == _canonical(baseline['activity']))
        require(not any(_mentions(current[k],r) for k in ('jobs','public','activity')))
        require(len(owned) == (0 if phase == 'remove' else 1))
        row = owned[0] if owned else None
        wires = [w for w in r['wire'] if w['phase'] == phase]
        require(len(wires) <= 1,'shape')
        wire = wires[0] if wires else None
        if wire is not None: require(wire['status'] in ((200,204) if phase == 'remove' else (200,)))
        if phase == 'remove':
            require(entry['detail_absent'] is True and entry['detail'] is None)
            if wire is not None and 'decoded' in wire: contracts.validate_remove_reply(wire['decoded'])
        else:
            assert row is not None
            require(entry['detail_absent'] is False and _canonical(entry['detail']) == _canonical(row))
            require(type(row['NumJobs']) is int and row['NumJobs'] == 0 and row['Priority'] == 0)
            require(all(k not in row or row[k] is False for k in ('Active','Running','Recording','Started','active','running','recording','started')))
            after = RuleSnapshot.parse(row)
            if phase == 'create':
                assert wire is not None
                created = contracts.validate_create(_body(r['nonce']),r['returned_id'],wire['decoded'],after,binding,receipt_id=r['attempts'][-1]['intent'])
                r['authority'] = dict(provenance=created.provenance.value,receipt_id=created.receipt_id,ref=_ref(created.ref))
            else:
                before = RuleSnapshot.parse(r['last_rule'])
                planned = _body(r['nonce'])['Name']+' renamed' if phase == 'rename' else None
                contracts.validate_readback(before,after,phase,planned_name=planned)
                if wire is not None and 'decoded' in wire:
                    contracts.validate_mutation(before,SparseMutationReply.parse(wire['decoded']),after,phase,planned_name=planned)
                r['authority']['ref'] = _ref(RuleRef.from_snapshot(after,binding))
        # Only these reviewed successor fields change. All intents/wire/original
        # captures/receipts remain intact; a mismatched contract has no proof.
        r['captures'].append(dict(phase=phase,stage='recovery',**entry))
        r['last_rule'],r['state'] = deepcopy(row),STATES[PHASES.index(phase)]
        r['reconciled'].append(phase)
        return _mint(cls,_PROOF,_request=request,_client=observation._client,_sample=sample,_successor=_canonical(r).encode('ascii'))

    def __repr__(self): return 'SeriesReconciliationProof(private=True)'


def checked_successor(proof,request):
    require(getattr(proof, '_manager', False) is False, 'shape')
    require(type(proof) is SeriesReconciliationProof and getattr(proof,'_seal',None) is _PROOF
            and proof._request is request,'shape')
    _status(request,proof._client,proof._sample)
    return proof._successor


def checked_manager_successor(proof, request):
    if type(proof) is ManagerQuarantineProof:
        from .series_http import checked_status_refusal
        require(proof._seal is _QUARANTINE and proof._request is request, 'shape')
        checked_status_refusal(proof._refusal, request)
        return proof._successor, 'blocked'  # This proof class can never clear a marker.
    require(getattr(proof, '_manager', False) is True and getattr(request, '_manager', False) is True, 'shape')
    require(type(proof) is SeriesReconciliationProof and getattr(proof, '_seal', None) is _PROOF
            and proof._request is request, 'shape')
    _status(request, proof._client, proof._sample)
    require(proof._disposition in {'clear', 'pending', 'blocked'}, 'shape')
    return proof._successor, proof._disposition


@dataclass(frozen=True, init=False, repr=False)
class ManagerQuarantineProof:
    _request: SeriesReconciliationRequest
    _refusal: object
    _successor: bytes | None
    _seal: object

    def __repr__(self):
        return 'ManagerQuarantineProof(private=True)'


def manager_refusal_proof(request, refusal):
    from .series_http import checked_status_refusal
    from .series_manager_store import decode, encode
    snapshot, _ = _request(request)
    require(getattr(request, '_manager', False) is True, 'shape')
    checked_status_refusal(refusal, request)
    result = None
    if snapshot.payload is not None:
        root = decode(snapshot.payload)
        root['blocked'] = True
        result = encode(root)
    return _mint(ManagerQuarantineProof, _QUARANTINE, _request=request, _refusal=refusal, _successor=result)
