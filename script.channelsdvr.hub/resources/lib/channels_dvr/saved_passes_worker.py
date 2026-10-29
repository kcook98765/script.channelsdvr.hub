"""One explicit selected-Series saved-rules read; five GETs, no retry or writes."""
from dataclasses import dataclass, field
from threading import Event as Signal, Lock, Thread
import time

from .live_loader import DevelopmentConfig
from .metadata_http import MetadataError
from .models import Event
from .pass_http import ChannelsPassClient
from .pass_preview import PreviewError, selected_series
from .saved_passes import SavedSnapshot, saved_snapshot


def _client(config):
    return ChannelsPassClient(config.origin, namespace=config.namespace,
                              expected_version=config.expected_version, request_budget=5)


@dataclass(frozen=True)
class SavedResult:
    generation: int
    event: Event = field(repr=False)
    server_binding: str = field(repr=False)
    error: str | None = None
    snapshot: SavedSnapshot | None = field(default=None, repr=False)


class SavedPassesWorker:
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
        self._result: SavedResult | None = None

    def start(self) -> None:
        if self._thread is not None or self._closed.is_set():
            return
        self._thread = Thread(target=self._run, name="channels-saved-passes", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            self._result = None

    def poll(self) -> SavedResult | None:
        with self._lock:
            result, self._result = self._result, None
            return None if self._closed.is_set() else result

    def _check(self) -> None:
        if self._closed.is_set():
            raise PreviewError("closed")

    def _run(self) -> None:
        binding, error, snapshot = "", None, None
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
            self._check()
            payload = client.rules()
            self._check()
            observed_at = int(time.time())
            snapshot = saved_snapshot(payload, series, observed_at=observed_at, strings=self._strings)
            self._check()
            client.verify_status()
            self._check()
            if client.server_binding_sha256 != binding:
                raise MetadataError("identity_mismatch")
        except (MetadataError, PreviewError) as failure:
            error = failure.code
        except Exception:
            error = "schema_unqualified"
        with self._lock:
            if not self._closed.is_set():
                self._result = SavedResult(self.generation, self.selected, binding, error,
                                           snapshot if error is None else None)
