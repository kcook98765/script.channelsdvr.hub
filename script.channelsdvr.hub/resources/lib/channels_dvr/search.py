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

from .models import Channel, ChannelKey, Event, Recording


MAX_SEARCH_RESULTS = 200
MAX_SEARCH_CHANNELS = 100000


def normalize_search_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def recording_search_text(record: Recording) -> str:
    return normalize_search_text(record.title + " " + (record.show or ""))


def matches_tokens(searchable: str, tokens: list[str]) -> bool:
    return all(token in searchable for token in tokens)


def normalize_query(text: str) -> str:
    """Normalize a literal query of at most 128 original Unicode characters."""
    if not isinstance(text, str) or len(text) > 128:
        raise ValueError("Query must be text of at most 128 characters")
    return normalize_search_text(text)


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


def _overlaps(event: Event, horizon: tuple[int, int]) -> bool:
    left, right = horizon
    return (left <= event.start < right if event.end is None
            else event.start < right and event.end > left)


@dataclass(frozen=True, init=False)
class ProgrammeSearchIndex:
    _records: tuple[tuple[Event, str], ...]
    coverage_partial: bool
    coverage_capped: bool

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
        inventory = tuple(islice(channels, MAX_SEARCH_CHANNELS + 1))
        known = frozenset(c.key for c in inventory[:MAX_SEARCH_CHANNELS]
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
            (event, normalize_search_text(event.title + " " + event.description))
            for event in ordered))
        object.__setattr__(self, "coverage_partial", rejected)
        object.__setattr__(self, "coverage_capped",
                           len(captured) > max_events or len(inventory) > MAX_SEARCH_CHANNELS)

    def search(self, text: str, *,
               allowed_channels: tuple[ChannelKey, ...] | None = None,
               horizon: tuple[int, int] | None = None,
               limit: int = 100) -> SearchPage:
        page = self.search_local(text, allowed_channels=allowed_channels, horizon=horizon, limit=limit)
        # Legacy callers receive a combined truncation warning on nonblank queries.
        if page.query and page.coverage_limited and not page.truncated:
            return SearchPage(page.query, page.matches, page.scanned, True, True)
        return page

    def search_local(self, text: str, *,
                     allowed_channels: tuple[ChannelKey, ...] | None = None,
                     horizon: tuple[int, int] | None = None,
                     limit: int = 100) -> SearchPage:
        """Report result overflow separately from incomplete index coverage."""
        query = normalize_query(text)
        _bounded_integer(limit, MAX_SEARCH_RESULTS, "limit")
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
        limited = self.coverage_partial or self.coverage_capped
        if not query or allowed == frozenset():
            return SearchPage(query, (), 0, False, limited)
        tokens = query.split()
        matches: list[Event] = []
        scanned = 0
        more = False
        for event, searchable in self._records:
            scanned += 1
            if allowed is not None and event.channel not in allowed:
                continue
            if horizon is not None and not _overlaps(event, horizon):
                continue
            if matches_tokens(searchable, tokens):
                if len(matches) == limit:
                    more = True
                    break
                matches.append(event)
        return SearchPage(query, tuple(matches), scanned, more, limited)

    def has_programmes(self, channels: tuple[ChannelKey, ...], horizon: tuple[int, int]) -> bool:
        """Does an already validated scope contain any retained airing, regardless of query?"""
        allowed = frozenset(channels)
        return bool(allowed) and any(event.channel in allowed and _overlaps(event, horizon)
                                     for event, _ in self._records)


@dataclass(frozen=True, eq=False)
class SearchRetirement:
    """Frozen refs only; root can cover more Events than bounded records.

    Default UI search builds at most10000 records from10001 inputs; the explicit
    pure API permits at most100000 records. Events/normalized strings stay held
    until worker drain. Owner handoff does no E iteration/copy/deep validation.
    """
    snapshot: tuple[Event, ...] | None = None
    records: tuple[tuple[Event, str], ...] = ()
