"""Plain-data worker mailbox: only the owner drains and mutates GUI state.

No workers are needed for the small in-memory prototype. This boundary is
exercised offline before any later asynchronous adapter is allowed to use it.
"""

from queue import Empty, Queue
from threading import Lock
from typing import Generic, TypeVar

T = TypeVar("T")


class LatestRequest(Generic[T]):
    def __init__(self) -> None:
        self._generation = 0
        self._lock = Lock()
        self._queue: Queue[tuple[int, T]] = Queue(maxsize=1)

    def begin(self) -> int:
        with self._lock:
            self._generation += 1
            self._clear()
            return self._generation

    def _clear(self) -> None:
        try:
            self._queue.get_nowait()
        except Empty:
            pass

    def publish(self, generation: int, data: T) -> bool:
        with self._lock:
            if generation != self._generation:
                return False
            self._clear()
            self._queue.put_nowait((generation, data))
            return True

    def drain(self) -> T | None:
        with self._lock:
            try:
                generation, data = self._queue.get_nowait()
            except Empty:
                return None
            return data if generation == self._generation else None
