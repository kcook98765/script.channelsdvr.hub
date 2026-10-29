"""Fixed, bounded metadata GETs only. Never opens a media descriptor URL."""
import json
from time import monotonic
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from .metadata_http import _NoRedirect, _origin, _unique_object, _finite_float, _reject_constant
from .newbuild_compatibility import supported_build
from .playback_descriptors import (PlaybackError, PlaybackDescriptor, LibraryRecording,
    component, require, recording_url, live_url, live_label, admit_activity)


def _json_preflight(text):
    """Conservative lexical budgets BEFORE DOM allocation, not a JSON decoder.

    Strings (including object keys), container starts and scalar tokens each
    consume a token. Only escapes/quotes and structural delimiters are classified;
    stdlib JSON remains the syntax, duplicate-key and numeric/type authority.
    State is constant space; an enormous string does not consume extra tokens.
    """
    quoted = escaped = atom = False
    depth = tokens = 0
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char.isspace() or char in ',:]}':
            atom = False
            if char in ']}':
                depth -= 1
                require(depth >= 0)
            continue
        if char == '"':
            quoted, atom = True, False
            tokens += 1
        elif char in '[{':
            depth += 1
            require(depth <= 32)
            tokens += 1
            atom = False
        elif not atom:
            tokens += 1
            atom = True
        require(tokens <= 100000)


def _validate_json(value):
    """O(depth) iterator frames, never a worklist proportional to child count."""
    frames = [iter((value,))]
    count = 0
    while frames:
        try:
            node = next(frames[-1])
        except StopIteration:
            frames.pop()
            continue
        count += 1
        require(count <= 100000 and len(frames)-1 <= 32)
        if type(node) is dict:
            frames.append(iter(node.values()))
        elif type(node) is list:
            frames.append(iter(node))


class PlaybackMetadataClient:
    def __init__(self, config, cancelled=lambda: False, *, opener=None, clock=monotonic):
        self.config = config
        self.origin = _origin(config.origin)
        self.cancelled = cancelled
        self.clock = clock
        self.deadline = clock() + 25
        self.remaining = 8
        self.opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())

    def check(self):
        if self.cancelled():
            raise PlaybackError('cancelled')
        if self.clock() >= self.deadline:
            raise PlaybackError('timeout')

    def _get(self, path, *, playlist=False):
        self.check()
        require(self.remaining > 0)
        self.remaining -= 1
        response = None
        cap = 2 * 1024 * 1024 if playlist else 4 * 1024 * 1024
        request_deadline = min(self.deadline, self.clock() + 5)
        try:
            response = self.opener.open(Request(self.origin + path, method='GET', headers={
                'Accept': 'audio/x-mpegurl, application/vnd.apple.mpegurl, text/plain' if playlist else 'application/json',
                'Accept-Encoding': 'identity'}), timeout=min(5, self.deadline-self.clock()))
            require(response.status == 200)
            require(all(v.strip().lower() == 'identity' for v in response.headers.get_all('Content-Encoding', [])))
            types = response.headers.get_all('Content-Type', [])
            mime = types[0].split(';', 1)[0].strip().lower() if len(types) == 1 else ''
            require(mime in ({'audio/x-mpegurl', 'application/x-mpegurl', 'application/vnd.apple.mpegurl',
                             'audio/mpegurl', 'text/plain'} if playlist else {'application/json'}))
            chunks, size = [], 0
            while True:
                self.check()
                if self.clock() >= request_deadline:
                    raise PlaybackError('timeout')
                chunk = response.read1(min(65536, cap + 1 - size))
                size += len(chunk)
                require(size <= cap)
                if not chunk:
                    break
                chunks.append(chunk)
            self.check()
            if self.clock() >= request_deadline:
                raise PlaybackError('timeout')
            wire = b''.join(chunks)
            if playlist:
                return wire
            text = wire.decode('utf-8')
            _json_preflight(text)
            self.check()
            value = json.loads(text, object_pairs_hook=_unique_object,
                               parse_float=_finite_float, parse_constant=_reject_constant)
            _validate_json(value)
            self.check()
            if self.clock() >= request_deadline:
                raise PlaybackError('timeout')
            return value
        except HTTPError as exc:
            code = 'file_missing' if exc.code == 404 and path.startswith('/dvr/files/') else 'network_error'
            exc.close()
            raise PlaybackError(code) from None
        except PlaybackError:
            raise
        except Exception:
            raise PlaybackError('network_error') from None
        finally:
            if response is not None:
                response.close()

    def status(self):
        value = self._get('/status')
        if type(value) is not dict or not supported_build(value.get('version')):
            raise PlaybackError('unsupported_build')
        return value['version']

    def prepare(self, selection, attempt):
        require(selection.identity.namespace == self.config.namespace)
        version = self.status()
        resolved_label = None
        if selection.kind == 'recording':
            require(type(selection.identity) is LibraryRecording)
            file = self._get('/dvr/files/' + component(selection.identity.file_id) + '?raw=false')
            public = self._get('/api/v1/episodes?source=recordings')
            url = recording_url(self.origin, selection.identity, file, public)
            mime, pinned = 'application/vnd.apple.mpegurl', False
        else:
            require(selection.kind == 'live')
            channels = self._get('/api/v1/channels')
            devices = self._get('/devices')
            playlist = self._get('/devices/ANY/channels.m3u?format=ts&codec=copy', playlist=True)
            admit_activity(self._get('/dvr/activity'))
            url, pinned = live_url(self.origin, selection.identity, channels, devices, playlist)
            resolved_label = live_label(selection.identity, channels)
            mime = 'video/mp2t'
        require(self.status() == version)
        self.check()
        qualified = (LibraryRecording(self.config.namespace, 'dvr', selection.identity.file_id)
                     if type(selection.identity) is LibraryRecording else None)
        return PlaybackDescriptor(selection, url, mime, attempt, self.clock(), source_pinned=pinned,
                                  recording=qualified, resolved_label=resolved_label)
