"""Pure selected-SeriesID qualification and single-request cancellable worker."""

from dataclasses import dataclass, field
from threading import Event as Signal, Lock, Thread
import time

from . import metadata_models as dto
from .live_loader import DevelopmentConfig
from .metadata_http import MetadataError
from .models import Event
from .normalization import DataState
from .pass_http import ChannelsPassClient, condition_digest, series_text
from .pass_browser import PreviewMatch, PreviewSnapshot, display_text, prepare_snapshot


class PreviewError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code if code in {"selection_changed", "schema_unqualified", "series_unsupported", "closed"} else "schema_unqualified"
        super().__init__(self.code)


def selected_series(channels_payload, guide_payload, namespace: str, selected: Event) -> str:
    """Fresh qualified full-key join including title/end/description; never infer IDs."""
    if type(selected) is not Event or selected.channel.namespace != namespace:
        raise PreviewError("selection_changed")
    all_channels = dto.public_channels(channels_payload, namespace, include_hidden=True)
    channels = dto.public_channels(channels_payload, namespace)
    events = dto.guide_window(guide_payload, all_channels.items, namespace, selected.start,
                              require_station=True)
    if (all_channels.state not in (DataState.READY, DataState.EMPTY)
            or channels.state not in (DataState.READY, DataState.EMPTY)
            or events.state not in (DataState.READY, DataState.EMPTY)):
        raise PreviewError("schema_unqualified")
    # Validate every known row first; visibility limits selection, not schema scope.
    visible_keys = {c.key for c in channels.items}
    matches = [e for e in events.items if e.channel in visible_keys and (e.channel, e.id, e.start) ==
               (selected.channel, selected.id, selected.start)]
    if len(matches) != 1 or matches[0] != selected:
        raise PreviewError("selection_changed")
    candidates = [a for row in guide_payload
                  if (row["Channel"].get("DeviceID"), row["Channel"].get("ChannelID")) ==
                  (selected.channel.source_id, selected.channel.channel_id)
                  for a in row["Airings"]
                  if (a.get("ProgramID"), a.get("Time")) == (selected.id, selected.start)]
    if len(candidates) != 1:
        raise PreviewError("schema_unqualified")
    try:
        return series_text(candidates[0].get("SeriesID"))
    except MetadataError:
        raise PreviewError("series_unsupported") from None


def _text(value, limit=256):
    if (type(value) is not str or not value.strip() or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise PreviewError("schema_unqualified")
    return value


def preview_count(payload: object, series_id: str) -> int:
    """All-or-error count of this bounded response, not a future-coverage promise."""
    series_text(series_id)
    if type(payload) is not list or len(payload) > 1000:
        raise PreviewError("schema_unqualified")
    for row in payload:
        if type(row) is not dict:
            raise PreviewError("schema_unqualified")
        _text(row.get("ProgramID"))
        _text(row.get("Title"), 4096)
        start, duration = row.get("Time"), row.get("Duration")
        if (type(start) is not int or not 0 <= start <= 4102444800
                or type(duration) is not int or not 1 <= duration <= 604800):
            raise PreviewError("schema_unqualified")
        if "SeriesID" in row and series_text(row["SeriesID"]) != series_id:
            raise PreviewError("schema_unqualified")
        channels = row.get("Channels")
        if "Channels" in row:
            if type(channels) is not list or not 1 <= len(channels) <= 256:
                raise PreviewError("schema_unqualified")
            for channel in channels:
                _text(channel)
            if "Channel" in row and row["Channel"] != "":
                _text(row["Channel"])
        else:
            _text(row.get("Channel"))
    return len(payload)


def preview_matches(payload: object, series_id: str, *, observed_at: int, strings=None) -> PreviewSnapshot:
    from .ui_text import FALLBACK
    display_strings = FALLBACK if strings is None else strings
    """Preserve the exact core validator; publish only after every row passes."""
    preview_count(payload, series_id)
    assert isinstance(payload, list)  # Narrow only after the unchanged exact-type validator.
    if type(observed_at) is not int or observed_at < 0:
        raise PreviewError("schema_unqualified")
    matches = []
    def number(value):
        return value if type(value) is int and 0 <= value <= 1000000 else None
    for index, row in enumerate(payload):
        matches.append(PreviewMatch(
            index, row["ProgramID"], row["Title"], display_text(row.get("EpisodeTitle"), 1024, display_strings),
            number(row.get("SeasonNumber")), number(row.get("EpisodeNumber")),
            row["Time"], row["Time"] + row["Duration"], row["Duration"],
            tuple(row.get("Channels", ())), row.get("Channel") or None,
            display_text(row.get("FullSummary"), 16384, display_strings) or display_text(row.get("Summary"), 16384, display_strings)))
    return prepare_snapshot(tuple(matches), observed_at, display_strings)


def _client(config):
    return ChannelsPassClient(config.origin, namespace=config.namespace,
                              expected_version=config.expected_version, request_budget=4)


@dataclass(frozen=True)
class PreviewResult:
    generation: int
    event: Event = field(repr=False)
    server_binding: str = field(repr=False)
    condition_sha256: str = field(repr=False)
    count: int | None = None
    error: str | None = None
    snapshot: PreviewSnapshot | None = field(default=None, repr=False)


class PassPreviewWorker:
    """One explicit request per instance; close signals, never joins or does I/O."""
    def __init__(self, config: DevelopmentConfig, selected: Event, generation: int,
                 *, client_factory=_client, strings=None) -> None:
        from types import MappingProxyType
        from .ui_text import FALLBACK
        self._strings = MappingProxyType(dict(FALLBACK if strings is None else strings))
        self.config, self.selected, self.generation = config, selected, generation
        self._factory = client_factory
        self._closed = Signal()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._result: PreviewResult | None = None

    def start(self) -> None:
        if self._thread is not None or self._closed.is_set():
            return
        self._thread = Thread(target=self._run, name="channels-pass-preview", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            self._result = None

    def poll(self) -> PreviewResult | None:
        with self._lock:
            result, self._result = self._result, None
            return None if self._closed.is_set() else result

    def _check(self) -> None:
        if self._closed.is_set():
            raise PreviewError("closed")

    def _run(self) -> None:
        binding = digest = ""
        count, error = None, None
        snapshot = None
        try:
            self._check()
            client = self._factory(self.config)
            binding = client.server_binding_sha256
            client.verify_status()
            self._check()
            channels = client.public_channels()
            self._check()
            guide = client.guide_window(self.selected.start)
            self._check()
            series = selected_series(channels, guide, self.config.namespace, self.selected)
            digest = condition_digest(series)
            self._check()
            payload = client.preview(series)
            self._check()
            observed_at = int(time.time())
            snapshot = preview_matches(payload, series, observed_at=observed_at, strings=self._strings)
            count = len(snapshot.matches)
            self._check()
        except (MetadataError, PreviewError) as failure:
            error = failure.code
        except Exception:
            error = "schema_unqualified"
        with self._lock:
            if not self._closed.is_set():
                self._result = PreviewResult(self.generation, self.selected, binding, digest,
                                             count if error is None else None, error,
                                             snapshot if error is None else None)
