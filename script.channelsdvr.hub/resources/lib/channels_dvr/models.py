"""Immutable, source-aware offline records; timestamps are UTC epoch seconds."""

from dataclasses import dataclass
from enum import Enum
import math


@dataclass(frozen=True)
class ChannelKey:
    namespace: str
    source_id: str
    channel_id: str


@dataclass(frozen=True)
class Channel:
    key: ChannelKey
    number: str
    name: str
    groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class Event:
    id: str
    channel: ChannelKey
    start: int
    end: int | None
    title: str
    description: str = ""


@dataclass(frozen=True)
class Recording:
    id: str
    title: str
    show: str | None
    season: int | None
    episode: int | None
    is_movie: bool
    created: int
    duration: int | float | None
    watched: bool = False
    resume: int = 0
    namespace: str = ""
    provenance: str = "unknown"
    show_id: str | None = None
    watched_known: bool = True
    resume_known: bool = True


def recording_duration(value: object) -> int | float:
    """Known nonnegative seconds, preserving integer/float type and precision.

    Zero is display metadata, not a completed-playback admission. The fresh
    playback resolver separately requires strictly positive completed duration.
    Timestamps, progress, Event/Job and journal schemas are not duration fields.
    """
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and math.isfinite(value) and value >= 0:
        return value
    raise ValueError('invalid_recording_duration')


@dataclass(frozen=True)
class Job:
    id: str
    title: str
    start: int
    end: int
    candidates: tuple[ChannelKey, ...]
    event_ids: tuple[str, ...] = ()
    padding_before: int = 0
    padding_after: int = 0
    padding_known: bool = True


@dataclass(frozen=True)
class RecordingGroup:
    show: str | None
    season: int | None
    is_movie: bool
    recordings: tuple[Recording, ...]
    namespace: str = ""
    provenance: str = "unknown"
    show_id: str | None = None


class MatchStatus(str, Enum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class JobMatch:
    status: MatchStatus
    event: Event | None
    candidates: tuple[Event, ...] = ()
