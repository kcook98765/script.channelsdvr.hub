"""One serial read worker with a latest-pending slot. Close signals, never joins."""
from dataclasses import dataclass
from threading import Condition, Thread

from .playback_descriptors import PlaybackDescriptor, PlaybackError, PlaybackSelection
from .playback_http import PlaybackMetadataClient


@dataclass(frozen=True)
class PlaybackResult:
    selection: PlaybackSelection
    attempt: str
    descriptor: PlaybackDescriptor | None = None
    error: str | None = None


class PlaybackWorker:
    def __init__(self, config, *, client_factory=PlaybackMetadataClient):
        self.config = config
        self._factory = client_factory
        self._condition = Condition()
        self._closed = False
        self._current = None
        self._pending = None
        self._result = None
        self._thread = Thread(target=self._run, name='channels-playback-metadata', daemon=True)
        self._thread.start()

    def submit(self, selection, attempt):
        with self._condition:
            if self._closed:
                return False
            self._current = attempt
            self._pending = (selection, attempt)
            self._result = None
            self._condition.notify()
            return True

    def _cancelled(self, attempt):
        with self._condition:
            return self._closed or self._current != attempt

    def cancel(self):
        with self._condition:
            self._current = self._pending = self._result = None

    def close(self):
        with self._condition:
            self._closed = True
            self._current = self._pending = self._result = None
            self._condition.notify()

    def poll(self):
        with self._condition:
            result, self._result = self._result, None
            return result

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                assert self._pending is not None
                selection, attempt = self._pending
                self._pending = None
            try:
                client = self._factory(self.config, lambda: self._cancelled(attempt))
                descriptor = client.prepare(selection, attempt)
                result = PlaybackResult(selection, attempt, descriptor)
            except PlaybackError as exc:
                result = PlaybackResult(selection, attempt, error=exc.code)
            except Exception:
                result = PlaybackResult(selection, attempt, error='metadata_rejected')
            with self._condition:
                if not self._closed and self._current == attempt:
                    self._result = result
