"""Bounded plain-command mailbox; only its creating thread may drain it."""

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock, get_ident
from .ui_activation import ButtonActivation, ShortcutActivation

Command = tuple[str, int | ButtonActivation | ShortcutActivation | None]


@dataclass(frozen=True, eq=False)
class PlaybackInput:
    revision: int


class UiDispatcher:
    def __init__(self) -> None:
        self._owner = get_ident()
        self._lock = Lock()
        self._commands: deque[tuple[int, int, Command]] = deque()
        self._closed = False
        self._dropped = 0
        self._revision = 0
        self._active_revision: int | None = None
        self._dispatch_pending = 0
        self._playback_input: PlaybackInput | None = None
        self._input_epoch = 0
        self._modal_depth = 0

    def assert_owner(self) -> None:
        if get_ident() != self._owner:
            raise RuntimeError("GUI owner thread required")

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def post(self, command: Command) -> bool:
        if (type(command) is not tuple or len(command) != 2
                or command[0] not in ("initialize", "action", "click", "chrome")
                 or (command[0] == "initialize" and command[1] is not None)
                 or (command[0] == 'click' and type(command[1]) is not int)
                 or (command[0] == 'action' and type(command[1]) not in (int, ShortcutActivation))
                or (command[0] == 'chrome' and command[1] is not None and type(command[1]) is not ButtonActivation)):
            raise ValueError("Plain UI command required")
        with self._lock:
            # Even a full-mailbox input invalidates a pending playback intent.
            # No GUI, domain command cancellation or SDK work on posting threads.
            self._revision += 1
            if self._closed or (self._modal_depth and command[0] != 'initialize'):
                self._dropped += 1
                return False
            if command[0] == 'initialize':
                self._input_epoch += 1
                if len(self._commands) >= 128:
                    self._dropped += len(self._commands)
                    self._commands.clear()  # a lifecycle fence must not be shed
            elif len(self._commands) >= 128:
                self._dropped += 1
                return False
            self._commands.append((self._revision, self._input_epoch, command))
            return True

    def fence_input(self):
        self.assert_owner()
        with self._lock:
            self._input_epoch += 1

    def startup_idle(self) -> bool:
        """Already-posted input gets a turn before a delayed startup redirect."""
        self.assert_owner()
        with self._lock:
            return self._startup_idle()

    def _startup_idle(self) -> bool:
        return not (self._closed or self._commands or self._dispatch_pending or self._modal_depth)

    @contextmanager
    def startup_transition(self):
        """Claim a quiet transition atomically; never run SDK code under the lock."""
        self.assert_owner()
        with self._lock:
            claimed = self._startup_idle()
            if claimed:
                self._input_epoch += 1
                self._modal_depth += 1
        try:
            yield claimed
        finally:
            if claimed:
                with self._lock:
                    self._modal_depth -= 1
                    self._input_epoch += 1

    @contextmanager
    def modal_input(self):
        self.assert_owner()
        with self._lock:
            self._input_epoch += 1
            self._modal_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._modal_depth -= 1
                self._input_epoch += 1

    @property
    def modal_active(self):
        self.assert_owner()
        return self._modal_depth != 0

    def drain(self, max_commands: int = 16) -> tuple[Command, ...]:
        """Legacy plain snapshot consumer. Runtime command execution uses dispatch."""
        return tuple(command for _, _, command in self._take(max_commands))

    def _take(self, max_commands):
        self.assert_owner()
        if type(max_commands) is not int or not 1 <= max_commands <= 16:
            raise ValueError("Drain limit must be 1..16")
        with self._lock:
            return tuple(self._commands.popleft()
                          for _ in range(min(max_commands, len(self._commands))))

    def dispatch(self, max_commands: int = 16) -> Iterator[Command]:
        """At most16 commands, with the CURRENT command's posting revision.

        Locks only protect short plain-state transitions, never a yielded owner
        handler/modal. All popped-but-not-yet-applied commands remain a barrier.
        """
        self.assert_owner()
        with self._lock:
            if self._dispatch_pending or self._modal_depth:
                return  # nested owner pumps cannot overtake an active batch
        batch = self._take(max_commands)
        with self._lock:
            self._dispatch_pending = len(batch)
        try:
            for revision, epoch, command in batch:
                with self._lock:
                    active = not self._closed and (epoch == self._input_epoch or command[0] == 'initialize')
                    self._active_revision = revision if active else None
                if active:
                    yield command
                with self._lock:
                    self._active_revision = None
                    self._dispatch_pending -= 1
        finally:
            with self._lock:
                self._active_revision = None
                self._dispatch_pending = 0

    def capture_playback_input(self) -> PlaybackInput:
        self.assert_owner()
        with self._lock:
            # Bind to the explicit Play command, NOT a later already-posted input.
            revision = self._active_revision if self._active_revision is not None else self._revision
            lease = PlaybackInput(revision)
            self._playback_input = lease
            return lease

    def _playback_current(self, lease: PlaybackInput | None):
        return (lease is not None and not self._closed and self._playback_input is lease
                and lease.revision == self._revision and not self._commands
                and self._dispatch_pending == 0)

    def playback_input_current(self, lease: PlaybackInput) -> bool:
        self.assert_owner()
        with self._lock:
            return self._playback_current(lease)

    def commit_playback(self, lease: PlaybackInput) -> bool:
        """One-shot linearization point immediately before native Player.play.

        Posts before this atomic point prevent handoff; posts after it are later
        user input and never cause an automatic playback stop. No SDK under lock.
        """
        self.assert_owner()
        with self._lock:
            if not self._playback_current(lease):
                return False
            self._playback_input = None
            return True

    def cancel_playback_input(self, lease: PlaybackInput) -> None:
        self.assert_owner()
        with self._lock:
            if self._playback_input is lease:
                self._playback_input = None

    def close(self) -> None:
        self.assert_owner()
        with self._lock:
            self._closed = True
            self._playback_input = None
            self._commands.clear()
