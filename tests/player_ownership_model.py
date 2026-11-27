"""Offline ownership prototype; deliberately outside the installable add-on.

The existing source/selection/consent gate remains the authority for sending
Play. This model tracks that send and subsequent observations, not permission
to open media. It retains one attempt and at most one outstanding read ticket.
"""
from dataclasses import dataclass, field
from enum import Enum


class PlayerState(str, Enum):
    IDLE = 'idle'
    PENDING = 'pending'
    SENT = 'sent'
    ACTIVE = 'active'
    UNRELATED = 'unrelated'
    REPLACED = 'replaced'
    STOPPED = 'stopped'
    ENDED = 'ended'
    ERROR = 'error'
    UNCONFIRMED = 'unconfirmed'


class PlayerAttempt:
    """Local identity for one Hub intent, not a Kodi session identifier."""
    __slots__ = ()


class PlayerSession:
    """Adapter-local identity for one independently verifiable native start.

    Use the same object for the expected and independently read-back session
    only if proven to be the same native incarnation. Never infer this from a
    callback, the file alone, or a reusable player slot. Use None if unavailable.
    """
    __slots__ = ()


class ReadTicket:
    """Single-use scope minted by the owner immediately before player readback."""
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class MediaFingerprint:
    """Adapter-supplied keyed 32-byte digest of the full, exact media locator."""
    _digest: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self._digest) is not bytes or len(self._digest) != 32:
            raise ValueError('invalid media fingerprint')


class ReadbackKind(Enum):
    ACTIVE = 'active'
    IDLE = 'idle'
    UNKNOWN = 'unknown'


_TERMINAL = (PlayerState.STOPPED, PlayerState.ENDED, PlayerState.ERROR)


@dataclass(frozen=True, slots=True, repr=False)
class PlayerReadback:
    """Private owner sample; never build it by re-stamping buffered evidence.

    ACTIVE needs exact media; session can be unknown. IDLE is a positive idle
    read, not a failed read. Attach a terminal reason and session only if the
    outcome is independently attributable to that incarnation, not a callback
    seen near the idle sample. Otherwise report IDLE without either field.
    """
    ticket: ReadTicket
    kind: ReadbackKind
    media: MediaFingerprint | None = None
    session: PlayerSession | None = None
    terminal: PlayerState | None = None


class PlayerOwnership:
    """Single-slot state machine. Public presentation is the closed state only."""
    __slots__ = ('_state', '_attempt', '_media', '_session', '_confirmed',
                 '_ticket', '_control_read')

    def __init__(self) -> None:
        self._attempt: PlayerAttempt | None = None
        self._media: MediaFingerprint | None = None
        self._session: PlayerSession | None = None
        self._ticket: ReadTicket | None = None
        self._confirmed = False
        self._control_read = False
        self._state = PlayerState.IDLE

    @property
    def state(self) -> PlayerState:
        return self._state

    def _retire(self, state: PlayerState) -> None:
        self._attempt = self._media = self._session = self._ticket = None
        self._confirmed = self._control_read = False
        self._state = state

    def begin(self) -> PlayerAttempt:
        """Supersede any tracked intent/playback without acting on the player."""
        self._retire(PlayerState.PENDING)
        attempt = PlayerAttempt()
        self._attempt = attempt
        return attempt

    def mark_sent(self, attempt: PlayerAttempt, media: MediaFingerprint,
                  session: PlayerSession | None = None) -> bool:
        """Record a single dispatch after existing handoff gates pass; not AV start."""
        if (attempt is not self._attempt or self._state is not PlayerState.PENDING
                or type(media) is not MediaFingerprint
                or (session is not None and type(session) is not PlayerSession)):
            return False
        self._media, self._session = media, session
        self._state = PlayerState.SENT
        return True

    def cancel(self, attempt: PlayerAttempt) -> bool:
        """Detach the current attempt (e.g. on invalidation or close), never Stop."""
        if attempt is None or attempt is not self._attempt:
            return False
        self._retire(PlayerState.IDLE)
        return True

    def expire_startup(self, attempt: PlayerAttempt) -> bool:
        """Retire an unproved send at an owner-defined deadline. No retry."""
        if attempt is not self._attempt or self._media is None or self._confirmed:
            return False
        self._retire(PlayerState.UNCONFIRMED)
        return True

    def begin_read(self, attempt: PlayerAttempt, *, for_control: bool = False) -> ReadTicket | None:
        """Fence older reads. For a control, use the attempt captured by its UI intent."""
        if (attempt is not self._attempt or self._media is None
                or (for_control is True and self._state is not PlayerState.ACTIVE)):
            return None
        ticket = ReadTicket()
        self._ticket, self._control_read = ticket, for_control is True
        return ticket

    def apply_readback(self, sample: PlayerReadback) -> bool:
        """Update state; True only for a fresh matching *control* read, once.

        The caller must sample synchronously at point of use for controls and
        still ensure the Kodi operation cannot target a different incarnation.
        Routine polls never authorize an operation, even when state is ACTIVE.
        """
        if (type(sample) is not PlayerReadback or self._ticket is None
                or sample.ticket is not self._ticket):
            return False
        control = self._control_read
        self._ticket, self._control_read = None, False

        session = sample.session
        if session is not None and type(session) is not PlayerSession:
            self._state = PlayerState.UNCONFIRMED
        elif (sample.kind is ReadbackKind.ACTIVE and sample.terminal is None
              and type(sample.media) is MediaFingerprint):
            if (sample.media != self._media
                    or (session is not None and self._session is not None
                        and session is not self._session)):
                if self._confirmed:
                    self._retire(PlayerState.REPLACED)
                else:
                    self._state = PlayerState.UNRELATED
            elif session is not None and session is self._session:
                self._confirmed = True
                self._state = PlayerState.ACTIVE
                return control
            else:
                self._state = PlayerState.UNCONFIRMED
        elif sample.kind is ReadbackKind.IDLE and sample.media is None:
            reason = sample.terminal
            if (session is not None and session is self._session
                    and type(reason) is PlayerState and reason in _TERMINAL):
                self._retire(reason)
            elif self._confirmed:
                # Definitive idle loses ownership even if the cause is unknown.
                self._retire(PlayerState.UNCONFIRMED)
            else:
                self._state = PlayerState.UNCONFIRMED  # async startup may still complete
        else:
            self._state = PlayerState.UNCONFIRMED
        return False
