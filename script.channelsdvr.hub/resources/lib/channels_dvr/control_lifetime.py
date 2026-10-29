"""Process-local observational control lifetimes, including detached close.

No owner/GUI/transport/profile references or I/O. A running thread retains its
worker and ticket through the real finally chain. Weak registration also lets a
never-started, unreachable worker disappear without manufacturing a close/write.
This is not a cross-interpreter/process lock proof. Existing journal locks remain.
"""
from threading import Lock
from weakref import WeakSet

_lock = Lock()
_tickets: WeakSet = WeakSet()
_LIMIT = 128


class ControlWorkerTicket:
    def __init__(self) -> None:
        self._done = False
        with _lock:
            if len(_tickets) >= _LIMIT:
                raise RuntimeError('control_lifetime_capacity')
            _tickets.add(self)

    def finish(self) -> None:
        with _lock:
            self._done = True
            _tickets.discard(self)


def controls_quiet() -> bool:
    with _lock:
        return not _tickets
