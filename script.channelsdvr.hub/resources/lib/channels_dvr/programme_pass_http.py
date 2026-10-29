"""Closed DEV programme-pass HTTP. Fixed routes, no proxies/redirects/retries.

4MiB responses, 5s cooperative socket/read timeout, 96 requests per operation.
No job DELETE, updates, pause, caller-selected URL/method/body or name adoption.
"""
import json
import re
from threading import RLock
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener
from .newbuild_compatibility import profile_binding, version_admitted

from .metadata_http import (ChannelsMetadataClient, MetadataError, _NoRedirect,
                            _finite_float, _http_code, _origin, _reject_constant, _unique_object)
from .programme_pass_commands import ProgrammeCommand, ProgrammePlan, ProgrammeError, RuleAbsent, require, text


def segment(value):
    value = text(value)
    require(value.lower() not in {'.', '..', 'new', 'test', 'priority', 'pause', 'resume', 'batch'})
    return quote(value, safe='', encoding='utf-8', errors='strict')


class ProgrammePassClient:
    def __init__(self, origin, *, namespace, expected_version, opener=None, clock=time.time):
        ChannelsMetadataClient(origin, namespace=namespace, expected_version=expected_version,
                               timeout=5, max_bytes=4194304, request_budget=96, opener=opener)
        self._origin, self._version = _origin(origin), expected_version
        self.server_binding_sha256 = profile_binding(self._origin, namespace, expected_version)
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
        self._clock, self._verified, self._remaining = clock, False, 96
        self._sent, self._gate = set(), RLock()

    def verify_status(self): return self._send('status')
    def public_channels(self): return self._send('channels')
    def guide_window(self, start): return self._send('guide', start=start)
    def rules(self): return self._send('rules')
    def jobs(self): return self._send('jobs')
    def public_jobs(self): return self._send('public')
    def rule_detail(self, identity): return self._send('rule', identity=identity)
    def job_detail(self, identity): return self._send('job', identity=identity)
    def public_job_detail(self, identity): return self._send('public_job', identity=identity)
    def preview(self, plan): return self._send('preview', plan=plan)
    def create(self, command, *, confirmed=False): return self._send('create', command=command, confirmed=confirmed)
    def remove_owned(self, command, *, confirmed=False): return self._send('remove', command=command, confirmed=confirmed)

    def _send(self, operation, **params):
        with self._gate:
            routes = {'status': '/status', 'channels': '/api/v1/channels', 'guide': '/devices/ANY/guide',
                      'rules': '/dvr/rules', 'jobs': '/dvr/jobs', 'public': '/api/v1/jobs',
                      'rule': '/dvr/rules/', 'job': '/dvr/jobs/', 'public_job': '/api/v1/jobs/',
                      'preview': '/dvr/rules/test', 'create': '/dvr/rules/new', 'remove': '/dvr/rules/'}
            if operation not in routes:
                raise MetadataError('unknown_operation')
            expected = ({'start'} if operation == 'guide' else {'identity'} if operation in {'rule', 'job', 'public_job'}
                        else {'plan'} if operation == 'preview' else {'command', 'confirmed'}
                        if operation in {'create', 'remove'} else set())
            if set(params) != expected:
                raise MetadataError('invalid_argument')
            command: ProgrammeCommand | None = None
            path, method, data = routes[operation], 'GET', None
            if operation in {'create', 'remove'}:
                candidate = params['command']
                require(params['confirmed'] is True, 'confirmation_required')
                if type(candidate) is not ProgrammeCommand:
                    raise ProgrammeError('ownership_unverified')
                command = candidate
                require(command.client is self, 'ownership_unverified')
                command.plan.validate()
                # EVERY dispatch entry checks the current durable command, not a bare ID.
                command.admit(operation, self.server_binding_sha256, params['confirmed'])
                if operation == 'create':
                    method, data = 'POST', json.dumps(command.plan.body(), allow_nan=False).encode('utf-8')
                else:
                    method, path = 'DELETE', path + segment(command.identity)
            elif operation == 'preview':
                p = params['plan']
                require(type(p) is ProgrammePlan and p.server_binding == self.server_binding_sha256, 'binding_mismatch')
                p.fresh(self._clock())
                method, data = 'POST', json.dumps(p.conditions(), allow_nan=False).encode('utf-8')
            elif operation in {'rule', 'job', 'public_job'}:
                path += segment(params['identity'])
            elif operation == 'guide':
                start = params['start']
                require(type(start) is int and 0 <= start <= 4102444800)
                path += '?' + urlencode(dict(time=start, duration=1800, raw='false'))
            if operation == 'status':
                self._verified = False
            elif not self._verified:
                raise MetadataError('identity_not_verified')
            if self._remaining <= 0 or operation in self._sent:
                raise MetadataError('request_budget')
            self._remaining -= 1
            if operation in {'preview', 'create', 'remove'}:
                self._sent.add(operation)
            headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity', 'User-Agent': 'ProgrammePassDEV/1'}
            if data is not None:
                headers['Content-Type'] = 'application/json'
            request = Request(self._origin + path, method=method, data=data, headers=headers)
            deadline, response = time.monotonic() + 5, None
            try:
                try:
                    response = self._opener.open(request, timeout=5)
                except HTTPError as error:
                    response = error
                status = response.status
                if type(status) is not int:
                    raise MetadataError('network_error')
                if (status not in
                        ({200, 201} if operation == 'create' else {200, 204} if operation == 'remove'
                         else {200, 404} if operation == 'rule' else {200})):
                    raise MetadataError(_http_code(status), status)
                if any(v.strip().lower() != 'identity' for v in response.headers.get_all('Content-Encoding', [])):
                    raise MetadataError('content_encoding', status)
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise MetadataError('response_timeout', status)
                    amount = min(65536, 4194305 - size)
                    chunk = response.read1(amount)
                    if type(chunk) is not bytes or len(chunk) > amount:
                        raise MetadataError('network_error', status)
                    size += len(chunk)
                    if size > 4194304:
                        raise MetadataError('response_too_large', status)
                    if not chunk:
                        break
                    chunks.append(chunk)
                raw = b''.join(chunks)
                types = response.headers.get_all('Content-Type', [])
                media = types[0].split(';', 1)[0].strip().lower() if len(types) == 1 else ''
                if operation == 'remove' and not raw:
                    value = None
                else:
                    if media != 'application/json' and not re.fullmatch(r'application/[a-z0-9!#$&^_.+-]+\+json', media):
                        raise MetadataError('content_type', status)
                    try:
                        value = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant,
                                           parse_float=_finite_float, object_pairs_hook=_unique_object)
                    except (ValueError, UnicodeError, RecursionError):
                        raise MetadataError('malformed_json', status) from None
                if operation == 'create':
                    # Known decoded identity survives untimeliness, validation failure and restart.
                    if command is None:
                        raise ProgrammeError('ownership_unverified')
                    command.decoded_create(value, raw.decode('utf-8'))
                if time.monotonic() >= deadline:
                    raise MetadataError('response_timeout', status)
                if operation == 'status':
                    if type(value) is not dict or not version_admitted(self._version, value.get('version')):
                        raise MetadataError('identity_mismatch', status)
                    self._verified = True
                    return {'identity_verified': True, 'version': value['version']}
                if operation == 'rule' and status == 404:
                    if value is not None or media != 'application/json':
                        raise MetadataError('unsupported', status)
                    return RuleAbsent(params['identity'], self.server_binding_sha256)
                return value
            except MetadataError:
                raise
            except Exception as error:
                if isinstance(error, ProgrammeError):
                    raise
                raise MetadataError('network_error') from None
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
