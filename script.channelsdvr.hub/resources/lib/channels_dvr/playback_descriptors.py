"""Pure playback metadata joins. No I/O, media probes, sessions or Kodi imports.

The bounded M3U reader/path classification is refactored from the offline
tools/playback_descriptors.py. No research-only origin/IP or artwork policy.
"""
from dataclasses import dataclass, field
import math
import re
from urllib.parse import quote, unquote, urljoin, urlsplit

from .metadata_http import _origin
from .models import ChannelKey, Recording
from .pass_browser import display_text


class PlaybackError(Exception):
    """Closed diagnostic code only; never propagate URLs/response/exception text."""
    def __init__(self, code='metadata_rejected'):
        self.code = code if code in {'metadata_rejected', 'file_missing', 'busy',
            'cancelled', 'timeout', 'network_error', 'unsupported_build'} else 'metadata_rejected'
        super().__init__(self.code)


def require(condition):
    if not condition:
        raise PlaybackError()


def component(value):
    require(type(value) is str and 0 < len(value) <= 512 and bool(value.strip())
            and value not in ('.', '..') and not any(
                ord(c) < 32 or ord(c) == 127 or c in '/?#\\|;%' for c in value))
    try:
        return quote(value, safe='', encoding='utf-8', errors='strict')
    except UnicodeError:
        raise PlaybackError() from None


@dataclass(frozen=True)
class LibraryRecording:
    namespace: str
    provenance: str
    file_id: str

    @classmethod
    def selected(cls, record: Recording):
        require(type(record) is Recording and record.provenance in ('dvr', 'unknown'))
        component(record.id)
        return cls(record.namespace, record.provenance, record.id)


@dataclass(frozen=True)
class PlaybackSelection:
    kind: str
    identity: ChannelKey | LibraryRecording
    source_generation: int
    title: str = field(compare=False)


@dataclass(frozen=True)
class PlaybackDescriptor:
    selection: PlaybackSelection
    url: str = field(repr=False)
    mime: str
    attempt: str
    prepared_at: float
    source_pinned: bool = False
    transport: str = 'native_http'
    availability: str = 'UNKNOWN'
    start_from_beginning: bool = True
    resume_sync: bool = False
    art: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    recording: LibraryRecording | None = None
    resolved_label: str | None = None


def rows(value, limit=10000):
    require(type(value) is list and len(value) <= limit and all(type(r) is dict for r in value))
    return value


def path_segments(path):
    require(type(path) is str and len(path) <= 8192 and path.startswith('/') and '//' not in path)
    result = []
    for segment in path.split('/')[1:]:
        require(re.search(r'%(?![0-9a-fA-F]{2})', segment) is None)
        try:
            decoded = unquote(segment, encoding='utf-8', errors='strict')
            component(decoded)
        except (ValueError, UnicodeError):
            raise PlaybackError() from None
        result.append(decoded)
    return tuple(result)


def media_url(origin, value):
    """Same configured origin, without headers, credentials or path normalization."""
    try:
        require(type(value) is str and 0 < len(value) <= 8192 and not value.startswith('//'))
        require(not any(ord(c) <= 32 or ord(c) == 127 or c in '|\\' for c in value))
        require(re.search(r'%(?![0-9a-fA-F]{2})', value) is None)
        decoded = unquote(value, encoding='utf-8', errors='strict')
        require(not any(ord(c) < 32 or ord(c) == 127 or c in '|\\' for c in decoded))
        path_segments(urlsplit(value).path)  # BEFORE urljoin removes dot segments
        parsed = urlsplit(urljoin(origin + '/', value))
        require(parsed.scheme + '://' + parsed.netloc == _origin(origin)
                and parsed.username is None and parsed.password is None and not parsed.fragment
                and '#' not in value)
        path_segments(parsed.path)
        return parsed.geturl()
    except Exception:
        raise PlaybackError() from None


def m3u(body):
    """Bounded UTF-8 extended-M3U export, not a media/HLS manifest reader."""
    try:
        require(type(body) is bytes and len(body) <= 2 * 1024 * 1024)
        lines = body.decode('utf-8-sig').splitlines()
        require(lines and lines[0] == '#EXTM3U' and len(lines) <= 30000)
        entries: list[tuple[dict[str, str], str]] = []
        pending = None
        for line in lines[1:]:
            require(len(line) <= 16384 and not any(ord(c) < 32 for c in line))
            if not line:
                continue
            if line.startswith('#EXTINF:'):
                require(pending is None)
                match = re.fullmatch(r'#EXTINF:-?[0-9]+((?:\s+[A-Za-z0-9-]+="[^"\r\n]*")*)\s*,.*', line)
                require(match is not None)
                assert match is not None
                attrs = {}
                for k, v in re.findall(r'([A-Za-z0-9-]+)="([^"\r\n]*)"', match[1]):
                    require(k not in attrs)
                    attrs[k] = v
                pending = attrs
            elif line.startswith('#'):
                require(not line.startswith(('#KODIPROP', '#EXTVLCOPT', '#EXT-X-')))
            else:
                require(pending is not None and len(entries) < 10000)
                assert pending is not None
                entries.append((pending, line))
                pending = None
        require(pending is None)
        return entries
    except Exception:
        raise PlaybackError() from None


def recording_url(origin, selected, file, public_recordings):
    """public_recordings MUST be the fixed ?source=recordings request context.

    Existing internal-file rows deliberately have unknown display provenance.
    Only this exact-ID fresh public join promotes one to a known DVR recording;
    neither a missing response source field nor a filesystem Path is authority.
    """
    require(type(selected) is LibraryRecording and selected.provenance in ('dvr', 'unknown'))
    rid = selected.file_id
    component(rid)
    require(type(file) is dict and type(file.get('ID')) is str and file['ID'] == rid)
    require(file.get('Completed') is True and file.get('Processed') is True)
    duration = file.get('Duration')
    require(type(duration) in (int, float) and math.isfinite(duration) and duration > 0)
    require(all(k not in file or file[k] is False for k in ('Corrupt', 'Failed', 'Deleted', 'Recording')))
    matches = [r for r in rows(public_recordings) if r.get('id') == rid]
    require(len(matches) == 1)
    public = matches[0]
    require('source' not in public or public['source'] == 'recordings')
    if 'program_id' in public:
        require(type(file.get('Airing')) is dict and type(public['program_id']) is str
                and bool(public['program_id']) and file['Airing'].get('ProgramID') == public['program_id'])
    return media_url(origin, '/dvr/files/' + component(rid) + '/hls')


def live_url(origin, key, channels, devices, playlist):
    require(type(key) is ChannelKey)
    component(key.source_id)
    component(key.channel_id)
    catalog = rows(channels)
    for entry in catalog:
        component(entry.get('id'))
        component(entry.get('source_id'))
    matches = [r for r in catalog if r.get('id') == key.channel_id and r.get('source_id') == key.source_id]
    require(len(matches) == 1)
    public = matches[0]
    number = public.get('number')
    require(type(number) is str and bool(number))
    sources = [r for r in rows(devices, 256) if r.get('DeviceID') == key.source_id]
    require(len(sources) == 1)
    source_channels = rows(sources[0].get('Channels'))
    mapped = [c for c in source_channels if c.get('ID') == key.channel_id or c.get('GuideNumber') == number]
    require(len(mapped) == 1)
    device_channel = mapped[0]
    if 'ID' in device_channel:
        require(device_channel['ID'] == key.channel_id)
    else:
        require(public['id'] == number and len([c for c in catalog if c.get('number') == number]) == 1
                and device_channel.get('GuideNumber') == number)
    accepted = []
    for attrs, value in m3u(playlist):
        try:
            url = media_url(origin, value)
            parts = path_segments(urlsplit(url).path)
        except PlaybackError:
            continue
        if (len(parts) != 5 or parts[0] != 'devices' or parts[2] != 'channels'
                or parts[4] != 'stream.mpg' or parts[3] != key.channel_id
                or parts[1] not in (key.source_id, 'ANY')):
            continue
        # tvg-id is an external guide ID, not a backend-channel authority.
        require('channel-id' not in attrs or attrs['channel-id'] == key.channel_id)
        require('tvg-chno' not in attrs or attrs['tvg-chno'] == number)
        if parts[1] == 'ANY':
            require(len([c for c in catalog if c.get('id') == key.channel_id]) == 1)
        accepted.append((url, parts[1] != 'ANY'))
    require(len(accepted) == 1)
    return accepted[0]


def live_label(key, channels):
    """Fresh station display only; never selected EPG title or tune identity."""
    matches = [r for r in rows(channels) if r.get('id') == key.channel_id and r.get('source_id') == key.source_id]
    require(len(matches) == 1)
    def label(value):
        if type(value) is not str:
            return None
        # Escape invalid Unicode units for the SDK without altering provider data.
        safe = ''.join(f'\\u{ord(c):04X}' if 0xD800 <= ord(c) <= 0xDFFF else c for c in value[:512])
        return display_text(safe, 512)
    number = label(matches[0].get('number'))
    require(number is not None)
    name = label(matches[0].get('name'))
    return display_text(number + (' ' + name if name and name != number else ''), 512)


def admit_activity(activity):
    """Conservative DVR-only BUSY gate. Empty is NOT shared hardware availability."""
    require(type(activity) is dict)
    for name in ('streams', 'recordings', 'workers', 'other'):
        if rows(activity.get(name), 4096):
            raise PlaybackError('busy')
    queues = activity.get('queues')
    require(type(queues) is dict and len(queues) <= 256)
    if any(type(v) is not int or v != 0 for v in queues.values()):
        raise PlaybackError('busy')
