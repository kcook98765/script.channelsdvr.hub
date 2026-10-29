"""Strict *synthetic* payload contracts, not claims about any vendor API.

Collections are lists/tuples of mappings using the model field names. Events
and jobs use epoch seconds; recording creation uses epoch milliseconds. Duration,
resume and padding always use seconds. Unknown nullable values must be explicit
None. No envelopes, numeric strings, float rounding or timestamp-unit guessing.
Transport adapters may report unsupported/outage explicitly with unavailable().
Recording origin is explicit text only; absent namespace/provenance mean unknown
origin, never a real host or a classification inferred from other fields.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

from .models import Channel, ChannelKey, Event, Job, Recording, recording_duration

T = TypeVar("T")


class DataState(str, Enum):
    READY = "ready"
    EMPTY = "empty"
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"
    OUTAGE = "outage"
    PARTIAL = "partial"


@dataclass(frozen=True)
class DataResult(Generic[T]):
    items: tuple[T, ...]
    state: DataState
    detail: str = ""

    @property
    def complete(self) -> bool:
        return self.state in (DataState.READY, DataState.EMPTY)


def unavailable(state: DataState, detail: str = "") -> DataResult:
    if state not in (DataState.UNSUPPORTED, DataState.OUTAGE):
        raise ValueError("unavailable requires unsupported or outage")
    return DataResult((), state, detail)


def _integer(value):
    if type(value) is not int:
        raise ValueError("expected integer, not bool, float or numeric text")
    return value


def timestamp_seconds(value: int) -> int:
    return _integer(value)


def timestamp_milliseconds(value: int) -> int:
    """Floor to the containing UTC second, including pre-epoch values."""
    return _integer(value) // 1000


def event_timestamp(value: int) -> int:
    return timestamp_seconds(value)


def recording_timestamp(value: int) -> int:
    return timestamp_milliseconds(value)


def job_timestamp(value: int) -> int:
    return timestamp_seconds(value)


def _text(value):
    if not isinstance(value, str):
        raise ValueError("expected text")
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValueError("expected boolean")
    return value


def _nonnegative(value):
    value = _integer(value)
    if value < 0:
        raise ValueError("expected nonnegative value")
    return value


def _optional(value, parser):
    return None if value is None else parser(value)


def _sequence(value):
    if not isinstance(value, (list, tuple)):
        raise ValueError("expected list or tuple")
    return value


def _key(value):
    if not isinstance(value, Mapping):
        raise ValueError("expected channel key mapping")
    return ChannelKey(*(_text(value[name]) for name in
                        ("namespace", "source_id", "channel_id")))


def _channel(row):
    return Channel(_key(row["key"]), _text(row["number"]), _text(row["name"]),
                   tuple(_text(g) for g in _sequence(row.get("groups", ()))))


def _event(row):
    start = event_timestamp(row["start"])
    end = _optional(row["end"], event_timestamp)
    if end is not None and end <= start:
        raise ValueError("event end must follow start")
    return Event(_text(row["id"]), _key(row["channel"]), start, end,
                 _text(row["title"]), _text(row.get("description", "")))


def _recording(row):
    provenance = _text(row.get("provenance", "unknown"))
    if provenance not in ("dvr", "import", "unknown"):
        raise ValueError("unsupported recording provenance")
    return Recording(
        _text(row["id"]), _text(row["title"]), _optional(row["show"], _text),
        _optional(row["season"], _nonnegative),
        _optional(row["episode"], _nonnegative), _boolean(row["is_movie"]),
        recording_timestamp(row["created"]),
        _optional(row["duration"], recording_duration),
        _boolean(row.get("watched", False)), _nonnegative(row.get("resume", 0)),
        _text(row.get("namespace", "")), provenance,
        _optional(row.get("show_id"), _text),
        _boolean(row.get("watched_known", True)),
        _boolean(row.get("resume_known", True)),
    )


def _job(row):
    start, end = job_timestamp(row["start"]), job_timestamp(row["end"])
    if end <= start:
        raise ValueError("job end must follow start")
    return Job(_text(row["id"]), _text(row["title"]), start, end,
               tuple(_key(k) for k in _sequence(row["candidates"])),
               tuple(_text(i) for i in _sequence(row.get("event_ids", ()))),
               _nonnegative(row.get("padding_before", 0)),
               _nonnegative(row.get("padding_after", 0)),
               _boolean(row.get("padding_known", True)))


def _collection(payload, parser):
    try:
        rows = _sequence(payload)
        if any(not isinstance(row, Mapping) for row in rows):
            raise ValueError("expected record mappings")
        items = tuple(parser(row) for row in rows)
    except (KeyError, TypeError, ValueError) as error:
        # Reject the batch atomically; skipping malformed rows could erase data.
        return DataResult((), DataState.MALFORMED, str(error))
    return DataResult(items, DataState.READY if items else DataState.EMPTY)


def normalize_channels(payload) -> DataResult[Channel]:
    return _collection(payload, _channel)


def normalize_events(payload) -> DataResult[Event]:
    return _collection(payload, _event)


def normalize_recordings(payload) -> DataResult[Recording]:
    return _collection(payload, _recording)


def normalize_jobs(payload) -> DataResult[Job]:
    return _collection(payload, _job)
