"""Immutable, bounded search over offline model records, not API payloads.

Channel inventory is capped at 100,000 input records plus one lookahead;
event input is capped at max_events plus one lookahead, before validation or
deduplication. Either overflow makes coverage incomplete. No duration or
timestamp units are inferred. Scanned counts retained records examined,
including records rejected by filters and the extra match proving truncation.
"""

from dataclasses import dataclass
from itertools import islice
from typing import Iterable
import unicodedata

from .models import Channel, ChannelKey, Event


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def normalize_query(text: str) -> str:
    """Normalize a literal query of at most 128 original Unicode characters."""
    if not isinstance(text, str) or len(text) > 128:
        raise ValueError("Query must be text of at most 128 characters")
    return _normalize(text)


def airing_key(event: Event) -> tuple[ChannelKey, str, int]:
    return event.channel, event.id, event.start


@dataclass(frozen=True)
class SearchPage:
    query: str
    matches: tuple[Event, ...]
    scanned: int
    truncated: bool
    coverage_limited: bool


def _bounded_integer(value: int, maximum: int, name: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")


def _valid_key(key: ChannelKey) -> bool:
    return isinstance(key, ChannelKey) and all(
        isinstance(part, str) and bool(part)
        for part in (key.namespace, key.source_id, key.channel_id)
    )


@dataclass(frozen=True, init=False)
class ProgrammeSearchIndex:
    _records: tuple[tuple[Event, str], ...]
    _coverage_limited: bool

    @property
    def retirement_records(self) -> tuple[tuple[Event, str], ...]:
        """Borrow privately constructed immutable records without copying E.

        Owner puts these refs in the worker's retirement hold before clearing
        this index. No mutable search object or callback crosses threads.
        """
        return self._records

    def __init__(self, events: Iterable[Event], channels: Iterable[Channel], *,
                 max_events: int = 10000):
        _bounded_integer(max_events, 100000, "max_events")
        inventory = tuple(islice(channels, 100001))
        known = frozenset(c.key for c in inventory[:100000]
                          if isinstance(c, Channel) and _valid_key(c.key))
        captured = tuple(islice(events, max_events + 1))
        rejected = False
        unique: dict[tuple[ChannelKey, str, int], Event] = {}
        for event in captured[:max_events]:
            if (not isinstance(event, Event) or not _valid_key(event.channel)
                    or event.channel not in known
                    or not isinstance(event.id, str) or not event.id
                    or type(event.start) is not int
                    or not isinstance(event.title, str)
                    or not isinstance(event.description, str)):
                rejected = True
                continue
            # A missing end is unknown. A malformed interval is not missing data:
            # reject it and disclose incomplete coverage, never repair silently.
            if event.end is not None and (type(event.end) is not int
                                          or event.end <= event.start):
                rejected = True
                continue
            unique.setdefault(airing_key(event), event)
        ordered = sorted(unique.values(), key=lambda e: (
            e.start, e.channel.namespace, e.channel.source_id,
            e.channel.channel_id, e.id))
        object.__setattr__(self, "_records", tuple(
            (event, _normalize(event.title + " " + event.description))
            for event in ordered))
        object.__setattr__(self, "_coverage_limited",
                           rejected or len(captured) > max_events or len(inventory) > 100000)

    def search(self, text: str, *,
               allowed_channels: tuple[ChannelKey, ...] | None = None,
               horizon: tuple[int, int] | None = None,
               limit: int = 100) -> SearchPage:
        query = normalize_query(text)
        _bounded_integer(limit, 200, "limit")
        if horizon is not None:
            if (not isinstance(horizon, tuple) or len(horizon) != 2
                    or any(type(t) is not int for t in horizon)
                    or horizon[0] >= horizon[1]):
                raise ValueError("horizon must be an increasing pair of integers")
        if allowed_channels is not None and (
                not isinstance(allowed_channels, tuple)
                or any(not _valid_key(key) for key in allowed_channels)):
            raise ValueError("allowed_channels must be a tuple of ChannelKey values")
        allowed = None if allowed_channels is None else frozenset(allowed_channels)
        if not query:
            return SearchPage(query, (), 0, False, self._coverage_limited)
        if allowed == frozenset():
            return SearchPage(query, (), 0, self._coverage_limited,
                              self._coverage_limited)
        tokens = query.split()
        matches: list[Event] = []
        scanned = 0
        more = False
        for event, searchable in self._records:
            scanned += 1
            if allowed is not None and event.channel not in allowed:
                continue
            if horizon is not None:
                left, right = horizon
                if event.end is None:
                    if not left <= event.start < right:
                        continue
                elif not (event.start < right and event.end > left):
                    continue
            if all(token in searchable for token in tokens):
                if len(matches) == limit:
                    more = True
                    break
                matches.append(event)
        return SearchPage(query, tuple(matches), scanned,
                          more or self._coverage_limited, self._coverage_limited)


@dataclass(frozen=True, eq=False)
class SearchRetirement:
    """Frozen refs only; root can cover more Events than bounded records.

    Default UI search builds at most10000 records from10001 inputs; the explicit
    pure API permits at most100000 records. Events/normalized strings stay held
    until worker drain. Owner handoff does no E iteration/copy/deep validation.
    """
    snapshot: tuple[Event, ...] | None = None
    records: tuple[tuple[Event, str], ...] = ()
