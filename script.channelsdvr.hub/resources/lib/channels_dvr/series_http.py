"""Closed Series transport. No arbitrary routes, retries, credentials or media.

Cooperative 60s operation / 5s request / 32 requests / 4MiB response limits.
Mutation wire is checkpointed before media/status/JSON/deadline refusal; decoded
valid returned identity is checkpointed before DTO qualification. Invalid identity
data remains losslessly in wire, never becomes a route or resemblance lookup.
"""
import base64
import json
import re
import time
import hashlib
from dataclasses import dataclass, field
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .metadata_http import (ChannelsMetadataClient, MetadataError, _NoRedirect, _origin, _unique_object, _reject_constant, _finite_float)
from .newbuild_compatibility import profile_binding, version_admitted
from .series_models import canonical, identity
from .series_manager_models import GeneralSeriesPlan, ManagerError, need
from .series_manager_store import checked_dispatch
from .series_manager_store import consume_dispatch
from .pass_http import conditions

_MANAGER_READS = object()
_STATUS_REFUSAL = object()


@dataclass(frozen=True, init=False, repr=False)
class SourceStatusRefusal:
    _request: object = field(repr=False)
    _origin: str = field(repr=False)
    _namespace: str = field(repr=False)
    _minimum: str = field(repr=False)
    _observed_at: float = field(repr=False)
    _wire_digest: str = field(repr=False)
    _seal: object = field(repr=False)

    def __repr__(self):
        return 'SourceStatusRefusal(private=True)'


class SourceStatusError(ManagerError):
    def __init__(self, refusal):
        super().__init__('source')
        self.refusal = refusal


def bind_status_refusal(refusal, request):
    from .series_reconciliation import _request, MAX_AGE
    _, source = _request(request)
    need(type(refusal) is SourceStatusRefusal and refusal._seal is _STATUS_REFUSAL, 'source')
    need(getattr(request, '_manager', False) is True and refusal._origin == source.origin
         and refusal._namespace == source.namespace and refusal._minimum == source.expected_version, 'source')
    now = time.monotonic()
    need(request._issued_at <= refusal._observed_at <= now and now-refusal._observed_at <= MAX_AGE, 'source')
    bound = object.__new__(SourceStatusRefusal)
    for name in ('_origin', '_namespace', '_minimum', '_observed_at', '_wire_digest', '_seal'):
        object.__setattr__(bound, name, getattr(refusal, name))
    object.__setattr__(bound, '_request', request)
    return bound


def checked_status_refusal(refusal, request):
    need(type(refusal) is SourceStatusRefusal and refusal._request is request, 'source')
    bind_status_refusal(refusal, request)  # Recheck seal, exact source and freshness on apply.


@dataclass(frozen=True, init=False, repr=False)
class ManagerReadSnapshot:
    _request: object = field(repr=False)
    _client: object = field(repr=False)
    _status_client: ChannelsMetadataClient = field(repr=False)
    _json: str | None = field(repr=False)
    _seal: object = field(repr=False)
    _failure: str | None = field(repr=False)

    def __repr__(self):
        return 'ManagerReadSnapshot(private=True)'


def checked_manager_reads(observation, request):
    need(type(observation) is ManagerReadSnapshot and observation._seal is _MANAGER_READS
         and observation._request is request and type(observation._client) is SeriesHTTP)
    return observation


def segment(key):
    identity(key)
    need(key.lower() not in {'.', '..', 'new', 'test', 'priority', 'pause', 'resume', 'batch'})
    return key  # identity() permits URI-unreserved ASCII only.


class SeriesHTTP:
    def __init__(self, config, *, opener=None, clock=time.monotonic):
        self._origin = _origin(config.origin)
        self._minimum = config.expected_version
        self.binding = profile_binding(self._origin, config.namespace, self._minimum)
        self.namespace = config.namespace
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
        self._clock = clock
        self._deadline = clock() + 60
        self._remaining = 32
        self._actual = None
        self._verified = False
        self._sent = False
        self._last_status_wire = None

    def __repr__(self):
        return 'SeriesHTTP(private=True)'

    def verify_status(self):
        self._verified = False
        result = self._request('status')
        if type(result) is not dict or not version_admitted(self._minimum, result.get('version')):
            raise SourceStatusError(self._status_refusal(self._last_status_wire))
        actual = result['version']
        if self._actual is not None and self._actual != actual:
            raise SourceStatusError(self._status_refusal(self._last_status_wire))
        self._actual = actual
        self._verified = True
        return actual

    def _status_refusal(self, wire):
        # Issued only from this closed status transport's qualified original wire.
        need(type(wire) is dict and wire['status'] == 200 and wire['complete'] is True
             and wire['media'] == 'json' and wire['encoding'] == 'identity', 'source')
        raw = base64.b64decode(wire['raw_base64'], validate=True)
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant, parse_float=_finite_float)
        reported = value.get('version') if type(value) is dict else None
        need(not version_admitted(self._minimum, reported) or (self._actual is not None and reported != self._actual), 'source')
        receipt = object.__new__(SourceStatusRefusal)
        for name, item in dict(_request=None, _origin=self._origin, _namespace=self.namespace,
            _minimum=self._minimum, _observed_at=time.monotonic(), _wire_digest=hashlib.sha256(raw).hexdigest(),
            _seal=_STATUS_REFUSAL).items():
            object.__setattr__(receipt, name, item)
        return receipt

    def rules(self):
        return self._request('rules')

    def jobs(self):
        return self._request('jobs')

    def public_jobs(self):
        return self._request('public_jobs')

    def public_channels(self):
        return self._request('channels')

    def guide_window(self, start):
        need(type(start) is int and 0 <= start <= 4102444800)
        return self._request('guide', suffix='?' + urlencode(dict(time=start, duration=1800, raw='false')))

    def rule_detail(self, key):
        return self._request('detail', suffix=segment(key))

    def preview(self, plan):
        need(type(plan) is GeneralSeriesPlan)
        return self._request('preview', body=plan.conditions())

    def manager_snapshot(self, request):
        try:
            return self._manager_snapshot(request)
        except SourceStatusError as error:
            return bind_status_refusal(error.refusal, request)  # No following request after refusal.

    def _manager_snapshot(self, request):
        """Trusted producer: real status receipt plus whole, unprojected named GETs.

        All registered rules occur in the complete full-record rule GET. The
        pending operation additionally requires its exact named route, including
        transport-qualified404/null. No callback can substitute an absence flag.
        """
        from .series_reconciliation import _request
        from .series_manager_store import decode, encode, candidate_key
        snapshot, source = _request(request)
        need(getattr(request, '_manager', False) is True and self._origin == source.origin
             and self.namespace == source.namespace and self._minimum == source.expected_version, 'source')
        actual = self.verify_status()
        value = None
        client = self
        captured: dict = {}
        class StatusOpener:
            def open(self, request, *, timeout):
                need(client._remaining > 0 and client._clock() < client._deadline, 'network')
                client._remaining -= 1
                response = client._opener.open(request, timeout=timeout)
                chunks: list[bytes] = []
                class StatusResponse:
                    status = response.status
                    headers = response.headers
                    def read1(self, amount):
                        chunk = response.read1(amount)
                        if type(chunk) is bytes:
                            size = sum(len(c) for c in chunks)
                            chunks.append(chunk[:max(0, 4194304-size)])
                            if not chunk:
                                types = response.headers.get_all('Content-Type', [])
                                captured.update(raw_base64=base64.b64encode(b''.join(chunks)).decode('ascii'),
                                    status=response.status, complete=True,
                                    media='json' if len(types) == 1 and types[0].split(';',1)[0].strip().lower() == 'application/json' else 'other',
                                    encoding='identity' if all(v.strip().lower() == 'identity' for v in response.headers.get_all('Content-Encoding', [])) else 'other')
                        return chunk
                    def close(self):
                        response.close()
                return StatusResponse()
        status = ChannelsMetadataClient(self._origin, namespace=self.namespace,
            expected_version=self._minimum, timeout=min(5, max(.001, self._deadline-self._clock())),
            request_budget=1, opener=StatusOpener())
        try:
            result = status.verify_status()
        except MetadataError as error:
            if error.code == 'identity_mismatch':
                raise SourceStatusError(self._status_refusal(captured)) from None
            raise ManagerError('network') from None
        need(self._clock() < self._deadline, 'network')
        if result['version'] != actual:
            raise SourceStatusError(self._status_refusal(captured))
        failure = None
        if snapshot.payload is not None and failure is None:
            root = decode(snapshot.payload)
            try:
                value = dict(rules=self.rules(), jobs=self.jobs(), public=self.public_jobs(), details={})
                if root['pending'] is not None:
                    entry = root['history'][root['pending']]
                    key = candidate_key(entry)
                    if key is not None:
                        value['details'][key] = self.rule_detail(key)
                need(self.verify_status() == actual, 'source')
            except SourceStatusError:
                raise
            except ManagerError as error:
                if error.code not in {'schema', 'source'}:
                    raise
                failure = 'schema'  # Genuine producer seals semantic refusal, never a GUI flag.
                value = None
        observation = object.__new__(ManagerReadSnapshot)
        for key, item in dict(_request=request, _client=self, _status_client=status,
            _json=encode(value).decode('ascii') if value is not None else None, _seal=_MANAGER_READS, _failure=failure).items():
            object.__setattr__(observation, key, item)
        return observation

    def mutate(self, proof, *, capture):
        checked_dispatch(proof, self.binding)
        need(callable(capture) and not self._sent, 'consent')
        if proof.operation == 'create':
            need(proof.identity is None and proof.body is not None)
            suffix = ''
        else:
            suffix = segment(proof.identity)
            if proof.operation in {'pause', 'resume'}:
                suffix += '/' + proof.operation
            need((proof.body is not None) is (proof.operation == 'rename'))
        self._sent = True  # no retry even if opening/reading/checkpoint fails
        return self._request(proof.operation, suffix=suffix,
            body=json.loads(proof.body) if proof.body is not None else None, capture=capture, proof=proof)

    def _request(self, operation, *, suffix='', body=None, capture=None, proof=None):
        routes = {'status': ('GET', '/status'), 'rules': ('GET', '/dvr/rules'),
            'jobs': ('GET', '/dvr/jobs'), 'public_jobs': ('GET', '/api/v1/jobs'),
            'channels': ('GET', '/api/v1/channels'), 'guide': ('GET', '/devices/ANY/guide'),
            'detail': ('GET', '/dvr/rules/'), 'preview': ('POST', '/dvr/rules/test'),
            'create': ('POST', '/dvr/rules/new'), 'rename': ('PUT', '/dvr/rules/'),
            'pause': ('PUT', '/dvr/rules/'), 'resume': ('PUT', '/dvr/rules/'),
            'remove': ('DELETE', '/dvr/rules/')}
        need(operation in routes and (operation == 'status' or self._verified), 'source')
        if operation in {'create', 'rename', 'pause', 'resume', 'remove'}:
            checked_dispatch(proof, self.binding)
            need(operation == proof.operation and callable(capture), 'consent')
            expected_suffix = '' if operation == 'create' else segment(proof.identity)
            if operation in {'pause', 'resume'}:
                expected_suffix += '/' + operation
            need(suffix == expected_suffix and (canonical(body) if body is not None else None) == proof.body, 'consent')
            consume_dispatch(proof, self.binding)  # Same proof cannot be replayed through a NEW client.
        else:
            need(proof is None and capture is None)
            if operation == 'preview':
                need(type(body) is dict and type(body.get('EQ')) is dict)
                need(suffix == '' and canonical(body) == canonical(conditions(body['EQ'].get('SeriesID'))))
            else:
                need(body is None)
                if operation == 'detail':
                    need(segment(suffix) == suffix)
                elif operation == 'guide':
                    need(type(suffix) is str and re.fullmatch(r'\?time=[0-9]{1,10}&duration=1800&raw=false', suffix) is not None)
                    need(0 <= int(suffix.split('=', 1)[1].split('&', 1)[0]) <= 4102444800)
                else:
                    need(suffix == '')
        need(self._remaining > 0 and self._clock() < self._deadline, 'network')
        self._remaining -= 1
        method, route = routes[operation]
        data = canonical(body).encode('ascii') if body is not None else None
        if operation in {'pause', 'resume'}:
            data = b''
        request = Request(self._origin + route + suffix, data=data, method=method,
            headers={'Accept': 'application/json', 'Accept-Encoding': 'identity',
                     'Content-Type': 'application/json', 'User-Agent': 'ChannelsSeriesManager/1'})
        response = None
        raw = b''
        complete = False
        status, media, encoding = 0, 'other', 'other'
        deadline = min(self._deadline, self._clock() + 5)
        failure = False
        try:
            try:
                response = self._opener.open(request, timeout=5)
            except HTTPError as error:
                response = error  # named404 must have the actual bounded body
            response_status = response.status
            need(type(response_status) is int and 100 <= response_status <= 599, 'network')
            assert isinstance(response_status, int)
            status = response_status
            types = response.headers.get_all('Content-Type', [])
            media = 'json' if len(types) == 1 and types[0].split(';', 1)[0].strip().lower() == 'application/json' else 'other'
            encoding = 'identity' if all(v.strip().lower() == 'identity' for v in response.headers.get_all('Content-Encoding', [])) else 'other'
            chunks, size = [], 0
            try:
                while True:
                    need(self._clock() < deadline, 'network')
                    amount = min(65536, 4194305 - size)
                    chunk = response.read1(amount)
                    need(type(chunk) is bytes and len(chunk) <= amount, 'network')
                    if not chunk:
                        complete = True
                        break
                    chunks.append(chunk[:4194304-size])
                    size += len(chunk)
                    need(size <= 4194304, 'network')
            finally:
                raw = b''.join(chunks)
        except Exception:
            failure = True
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    failure = True
            wire = dict(raw_base64=base64.b64encode(raw).decode('ascii'), status=status if type(status) is int else 0,
                         complete=complete, media=media, encoding=encoding)
            if operation == 'status':
                self._last_status_wire = wire
            if capture is not None:
                capture(wire)  # Also captures failed reads/prefixes; callback failure bars readback.
        try:
            decoded = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                                 parse_constant=_reject_constant, parse_float=_finite_float) if raw else None
        except (ValueError, UnicodeError, RecursionError):
            need(not failure and complete, 'network')
            raise ManagerError('schema') from None
        if capture is not None and type(decoded) is dict:
            candidate = decoded.get('ID')
            try:
                identity(candidate)
            except Exception:
                candidate = None
            if candidate is not None:
                capture(wire, candidate)  # Retain ID before ANY DTO, status, header or deadline refusal.
        need(not failure and complete, 'network')
        need(self._clock() < deadline, 'network')
        need(encoding == 'identity' and (media == 'json' or (operation == 'remove' and raw == b'')))
        if operation == 'detail' and status == 404:
            need(media == 'json' and raw == b'null')
            return None  # Only this named, bound GET can return qualified absence.
        need(status == 200 or (operation == 'remove' and status == 204), 'network')
        need(operation == 'remove' or decoded is not None)
        return decoded
