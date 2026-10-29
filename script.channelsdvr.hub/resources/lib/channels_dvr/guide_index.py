"""Pure immutable interval products; construction is CPU work, not admission.

Call build_index on the metadata worker. Inputs retain exact builtin tuple refs;
no SDK, repositories, clients or I/O belong here. matches_input is a constant-time
request receipt, NOT evidence that a session accepted metadata. Runtime must also
require a positive cache-origin receipt before installing or authorizing actions.

Cancellation is cooperative at most every 1024 loop elements and around sorts and
freezes. Builtin sort, allocation, tuple freeze and CPython refcount release are
not preemptible; running this on a thread is not a hard latency guarantee.
"""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Iterator, Mapping, TypeVar

from .models import Channel, ChannelKey, Event

_SEAL = object()
_CHUNK = 1024
T = TypeVar("T")


def _noop() -> None:
    pass


@dataclass(frozen=True, eq=False, slots=True)
class IndexInput:
    events: tuple[Event, ...]
    channels: tuple[Channel, ...]
    namespace: str
    version: str
    horizon: tuple[int, int]
    fetched_at: int
    work_generation: int
    ticket: object
    owner: object
    metadata_generation: int | None = None


@dataclass(frozen=True, eq=False, init=False, slots=True)
class Schedule:
    events: tuple[Event, ...]
    starts: tuple[int, ...]
    ends: tuple[int, ...]
    prefix_max: tuple[int, ...]
    positions: Mapping[tuple[str, int], int]
    first_ids: Mapping[str, int]
    end_times: tuple[int, ...]
    last_ended: tuple[int, ...]
    size: int
    tree: tuple[int, ...]

    def __init__(self) -> None:
        raise TypeError("Schedules are factory-owned")

    def overlapping(self, left: int, right: int, reverse: bool = False) -> Iterator[int]:
        first = bisect_right(self.prefix_max, left)
        last = bisect_left(self.starts, right)

        # A recursive nested generator captures itself and this Schedule in a
        # closure cycle, surviving exhaustion until arbitrary-thread cyclic GC.
        # The depth-first stack contains only scalar nodes, at most O(log n).
        # A live iterator is still a borrow; exhaust/close/drop clears its frame.
        # Push the later child first to preserve lazy forward/reverse ordering.
        stack = [(1, 0, self.size)]
        while stack:
            node, lo, hi = stack.pop()
            if hi <= first or lo >= last or self.tree[node] <= left:
                continue
            if hi - lo == 1:
                yield lo
                continue
            mid = (lo + hi) // 2
            if reverse:
                stack.append((2 * node, lo, mid))
                stack.append((2 * node + 1, mid, hi))
            else:
                stack.append((2 * node + 1, mid, hi))
                stack.append((2 * node, lo, mid))


@dataclass(frozen=True, eq=False, init=False, slots=True)
class IndexProduct:
    source: IndexInput
    events: tuple[Event, ...]
    channels: tuple[Channel, ...]
    schedules: Mapping[ChannelKey, Schedule]
    input_events: int
    indexed_events: int
    tree_entries: int
    _seal: object

    def __init__(self) -> None:
        raise TypeError("Index products are factory-owned")


def matches_input(product: object, source: IndexInput) -> bool:
    """Exact process-local builder receipt, not a security or acceptance token."""
    return (type(product) is IndexProduct and getattr(product, "_seal", None) is _SEAL
            and getattr(product, "source", None) is source)


def _key(key: ChannelKey) -> bool:
    return (type(key) is ChannelKey and type(key.namespace) is str
            and type(key.source_id) is str and type(key.channel_id) is str)


def _tick(index: int, check: Callable[[], None]) -> None:
    if index % _CHUNK == 0:
        check()


def _freeze(items: list[T], check: Callable[[], None]) -> tuple[T, ...]:
    # Allocation/copy inside this builtin call cannot be cancelled mid-call.
    check()
    result = tuple(items)
    check()
    return result


def _schedule(events: list[Event], check: Callable[[], None]) -> Schedule:
    unique: dict[tuple[str, int], Event] = {}
    for index, event in enumerate(events):
        _tick(index, check)
        unique.setdefault((event.id, event.start), event)
    check()
    ordered = sorted(unique.values(), key=lambda e: (e.start, e.id))
    check()
    # Release the dedup dictionary on the worker, never transfer a mutable alias.
    del unique
    starts: list[int] = []
    positions: dict[tuple[str, int], int] = {}
    first_ids: dict[str, int] = {}
    for index, event in enumerate(ordered):
        _tick(index, check)
        starts.append(event.start)
        positions[(event.id, event.start)] = index
        first_ids.setdefault(event.id, index)
    check()
    ends = [0] * len(ordered)
    successor = None
    for offset, index in enumerate(range(len(ordered) - 1, -1, -1)):
        _tick(offset, check)
        event = ordered[index]
        if index + 1 < len(ends) and starts[index + 1] > event.start:
            successor = starts[index + 1]
        if event.end is not None and event.end > event.start:
            ends[index] = event.end
        else:
            ends[index] = min(event.start + 1800, successor
                              if successor is not None else event.start + 1800)
    prefix: list[int] = []
    maximum = None
    ended: list[tuple[int, int]] = []
    for index, end in enumerate(ends):
        _tick(index, check)
        maximum = end if maximum is None else max(maximum, end)
        prefix.append(maximum)
        ended.append((end, index))
    check()
    ended.sort()
    check()
    end_times: list[int] = []
    last_ended: list[int] = []
    last = -1
    for offset, (end, index) in enumerate(ended):
        _tick(offset, check)
        end_times.append(end)
        last = max(last, index)
        last_ended.append(last)
    size = 1 << (max(1, len(ends)) - 1).bit_length()
    # prefix[-1] is a maximum, not the sentinel minimum used by the old oracle.
    minimum = 0
    for index, end in enumerate(ends):
        _tick(index, check)
        minimum = end if index == 0 else min(minimum, end)
    check()
    tree = [minimum - 1] * (2 * size)
    check()
    for index, end in enumerate(ends):
        _tick(index, check)
        tree[size + index] = end
    for offset, index in enumerate(range(size - 1, 0, -1)):
        _tick(offset, check)
        tree[index] = max(tree[2 * index], tree[2 * index + 1])
    result = object.__new__(Schedule)
    object.__setattr__(result, "events", _freeze(ordered, check))
    object.__setattr__(result, "starts", _freeze(starts, check))
    object.__setattr__(result, "ends", _freeze(ends, check))
    object.__setattr__(result, "prefix_max", _freeze(prefix, check))
    object.__setattr__(result, "end_times", _freeze(end_times, check))
    object.__setattr__(result, "last_ended", _freeze(last_ended, check))
    object.__setattr__(result, "tree", _freeze(tree, check))
    object.__setattr__(result, "size", size)
    check()
    object.__setattr__(result, "positions", MappingProxyType(positions))
    object.__setattr__(result, "first_ids", MappingProxyType(first_ids))
    check()
    return result


def build_index(source: IndexInput, *, check: Callable[[], None] = _noop) -> IndexProduct:
    """Worker-only CPU factory; preserve input tuple refs and old query semantics.

    The check function belongs to the worker implementation; it is never part of
    IndexInput or a transported owner callback. No metadata permission is minted.
    """
    check()
    if (type(source) is not IndexInput or type(source.events) is not tuple
            or type(source.channels) is not tuple or type(source.namespace) is not str
            or type(source.version) is not str or type(source.horizon) is not tuple
            or len(source.horizon) != 2 or any(type(t) is not int for t in source.horizon)
            or source.horizon[0] >= source.horizon[1] or type(source.fetched_at) is not int
            or source.fetched_at < 0 or type(source.work_generation) is not int
            or source.work_generation < 0 or type(source.ticket) is not object
            or type(source.owner) is not object
            or (source.metadata_generation is not None and (
                type(source.metadata_generation) is not int or source.metadata_generation < 0))):
        raise ValueError("invalid_index_input")
    for index, channel in enumerate(source.channels):
        _tick(index, check)
        if (type(channel) is not Channel or not _key(channel.key)
                or type(channel.number) is not str or type(channel.name) is not str
                or type(channel.groups) is not tuple):
            raise ValueError("invalid_index_catalog")
        for offset, group in enumerate(channel.groups):
            _tick(offset, check)
            if type(group) is not str:
                raise ValueError("invalid_index_catalog")
    grouped: dict[ChannelKey, list[Event]] = {}
    for index, event in enumerate(source.events):
        _tick(index, check)
        if (type(event) is not Event or not _key(event.channel) or type(event.id) is not str
                or type(event.start) is not int
                or (event.end is not None and type(event.end) is not int)
                or type(event.title) is not str or type(event.description) is not str):
            raise ValueError("invalid_index_event")
        grouped.setdefault(event.channel, []).append(event)
    schedules: dict[ChannelKey, Schedule] = {}
    indexed = tree_entries = 0
    for key, events in grouped.items():
        check()
        schedule = _schedule(events, check)
        schedules[key] = schedule
        indexed += len(schedule.events)
        tree_entries += len(schedule.tree)
    check()
    product = object.__new__(IndexProduct)
    object.__setattr__(product, "source", source)
    object.__setattr__(product, "events", source.events)
    object.__setattr__(product, "channels", source.channels)
    object.__setattr__(product, "schedules", MappingProxyType(schedules))
    object.__setattr__(product, "input_events", len(source.events))
    object.__setattr__(product, "indexed_events", indexed)
    object.__setattr__(product, "tree_entries", tree_entries)
    object.__setattr__(product, "_seal", _SEAL)
    check()
    return product
