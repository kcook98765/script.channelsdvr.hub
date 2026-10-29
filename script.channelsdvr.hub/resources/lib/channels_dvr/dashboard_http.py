"""Closed Dashboard GET client. Never accepts caller paths or server URLs."""
from dataclasses import dataclass
import json
from time import monotonic, time
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError

from .metadata_http import _NoRedirect, _origin, _unique_object, _finite_float, _reject_constant
from .newbuild_compatibility import supported_build
from .playback_http import _json_preflight, _validate_json
from .dashboard_models import parse_resource, short_text

ROUTES = (('dvr', '/dvr'), ('activity', '/dvr/activity'), ('sources', '/devices'), ('passes', '/dvr/rules'))


@dataclass(frozen=True)
class DashboardResult:
    version: str | None
    fetched_at: float
    resources: tuple
    error: bool
    # None supports plain synthetic DTOs; transport always supplies an explicit
    # status bookend verdict. Error alone also includes optional-resource failure.
    coherent: bool | None = None
    unsupported: bool = False


class DashboardStatusReadClient:
    def __init__(self, config, cancelled=lambda: False, *, opener=None, clock=monotonic, wall=time):
        self.origin = _origin(config.origin)
        self.cancelled, self.clock, self.wall = cancelled, clock, wall
        self.deadline = clock() + 25
        self.remaining_bytes = 8 * 1024 * 1024
        self.remaining_reads = 6
        self.opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())

    def check(self):
        if self.cancelled() or self.clock() >= self.deadline:
            raise ValueError('cancelled_or_timeout')

    def _get(self, path):
        if path not in ('/status',) + tuple(p for _, p in ROUTES):
            raise ValueError('route')
        self.check()
        if self.remaining_reads <= 0:
            raise ValueError('budget')
        self.remaining_reads -= 1
        end = min(self.deadline, self.clock()+5)
        response = None
        try:
            response = self.opener.open(Request(self.origin+path, method='GET', headers={
                'Accept': 'application/json', 'Accept-Encoding': 'identity'}), timeout=min(5, end-self.clock()))
            if response.status != 200:
                raise ValueError('status')
            types = response.headers.get_all('Content-Type', [])
            if len(types) != 1 or types[0].split(';', 1)[0].strip().lower() != 'application/json':
                raise ValueError('type')
            if any(v.strip().lower() != 'identity' for v in response.headers.get_all('Content-Encoding', [])):
                raise ValueError('encoding')
            chunks, size = [], 0
            while True:
                self.check()
                if self.clock() >= end:
                    raise ValueError('timeout')
                chunk = response.read1(min(65536, 4*1024*1024+1-size, self.remaining_bytes+1))
                size += len(chunk)
                self.remaining_bytes -= len(chunk)
                if size > 4*1024*1024 or self.remaining_bytes < 0:
                    raise ValueError('size')
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b''.join(chunks).decode('utf-8')
            _json_preflight(raw)
            self.check()
            value = json.loads(raw, object_pairs_hook=_unique_object, parse_float=_finite_float,
                               parse_constant=_reject_constant)
            _validate_json(value)
            self.check()
            if self.clock() >= end:
                raise ValueError('timeout')
            return value
        except HTTPError as exc:
            exc.close()
            raise ValueError('http_error') from None
        finally:
            if response is not None:
                response.close()

    def read(self):
        resources: list[tuple[str, tuple | None]] = []
        version = None
        names = ('server',) + tuple(n for n, _ in ROUTES)
        try:
            first = self._get('/status')
            version = short_text(first.get('version')) if type(first) is dict else None
            if not supported_build(version):
                return DashboardResult(version, self.wall(), tuple((n, None) for n in names), True,
                                       coherent=False, unsupported=version is not None)
            resources.append(('server', tuple(parse_resource('server', first, self.wall()).items())))
            for name, path in ROUTES:
                self.check()
                try:
                    values = tuple(parse_resource(name, self._get(path), self.wall()).items())
                except Exception:
                    values = None
                resources.append((name, values))
            last = self._get('/status')
            if type(last) is not dict or last.get('version') != version:
                raise ValueError('changed_version')
            self.check()
            return DashboardResult(version, self.wall(), tuple(resources), any(v is None for _, v in resources), coherent=True)
        except Exception:
            return DashboardResult(version, self.wall(), tuple((n, None) for n in names), True, coherent=False)
