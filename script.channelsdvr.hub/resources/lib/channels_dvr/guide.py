"""Pure UTC timeline geometry and identity-based remote navigation.

Intervals are half-open. Unknown ends get a labelled, provisional 30 minute
display extent, clipped to the next start; they are never playback durations.
Two occupied-until lanes preserve ordinary overlaps. Further overlaps merge
into an explicitly labelled lane-1 aggregate; a selected airing is pinned in
lane 0 and never merged. Visible rows share the budget before overscan. Dense
rows may be incomplete (truncated=True), including gaps; omitted data is not
represented as a gap. No Kodi imports.
"""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from itertools import accumulate
from types import MappingProxyType
from typing import Mapping

from .models import Channel, ChannelKey, Event
from .guide_index import IndexInput, IndexProduct, Schedule, matches_input
from .guide_index import _schedule as _prepare_schedule
from .guide_projection import ChannelProjection
from .ui_text import FALLBACK, text
from .repository import MockRepository


# One factory-owned, immutable empty value, prepared at module import only.
# No runtime controller/query calls the builder, even for an empty snapshot.
_EMPTY_SCHEDULE = _prepare_schedule([], lambda: None)
_EMPTY_SCHEDULES: Mapping[ChannelKey, Schedule] = MappingProxyType({})


@dataclass(frozen=True)
class Selection:
    channel: ChannelKey | None
    event_id: str | None
    anchor: int
    event_start: int | None = None


@dataclass(frozen=True)
class Cell:
    channel: ChannelKey
    event_id: str | None
    row: int
    x: float
    width: float
    lane: int
    title: str
    start: int
    end: int
    selected: bool
    continues_left: bool = False
    continues_right: bool = False
    overflow: bool = False


class _Schedule:
    """Snapshot-owned interval index; queries do not sort or scan the horizon."""

    def __init__(self, events):
        # First observation wins for duplicate airing identities, not repeated IDs.
        unique: dict[tuple[str, int], Event] = {}
        for event in events:
            unique.setdefault((event.id, event.start), event)
        self.events = tuple(sorted(unique.values(), key=lambda e: (e.start, e.id)))
        self.starts = tuple(e.start for e in self.events)
        ends = [0] * len(self.events)
        successor = None
        for i in range(len(ends) - 1, -1, -1):
            event = self.events[i]
            if i + 1 < len(ends) and self.starts[i + 1] > event.start:
                successor = self.starts[i + 1]
            if event.end is not None and event.end > event.start:
                ends[i] = event.end
            else:
                ends[i] = min(event.start + 1800, successor
                              if successor is not None else event.start + 1800)
        self.ends = tuple(ends)
        self.prefix_max = tuple(accumulate(ends, max))
        self.positions = {(e.id, e.start): i for i, e in enumerate(self.events)}
        self.first_ids: dict[str, int] = {}
        for i, event in enumerate(self.events):
            self.first_ids.setdefault(event.id, i)
        ended = sorted((end, i) for i, end in enumerate(ends))
        self.end_times = tuple(end for end, _ in ended)
        self.last_ended = tuple(accumulate((i for _, i in ended), max))
        self.size = 1 << (max(1, len(ends)) - 1).bit_length()
        tree = [min(ends, default=0) - 1] * (2 * self.size)
        tree[self.size:self.size + len(ends)] = ends
        for i in range(self.size - 1, 0, -1):
            tree[i] = max(tree[2 * i], tree[2 * i + 1])
        self.tree = tuple(tree)

    def overlapping(self, left, right, reverse=False):
        """Yield lazily, pruning ended subtrees even beneath a long overlap.

        Prefix maxima and starts bound the query with bisect. The max-end tree
        avoids a linear scan through expired short events beneath a long one.
        Each yielded match costs at most logarithmic traversal; consumers stop
        after their rendering work budget plus one lookahead.
        """
        first = bisect_right(self.prefix_max, left)
        last = bisect_left(self.starts, right)

        def visit(node, lo, hi):
            if hi <= first or lo >= last or self.tree[node] <= left:
                return
            if hi - lo == 1:
                yield lo
                return
            mid = (lo + hi) // 2
            children = ((2 * node, lo, mid), (2 * node + 1, mid, hi))
            for child in reversed(children) if reverse else children:
                yield from visit(*child)

        yield from visit(1, 0, self.size)


def time_label(value: int, zone: tzinfo | None = None) -> str:
    """Offset is displayed so repeated DST hours are distinguishable."""
    stamp = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=value)).astimezone(zone)
    return stamp.strftime("%a %d %b %H:%M %z")


class GuideController:
    """Data-only guide model with legacy sync and explicit prepared runtime modes.

    Runtime MUST pass deferred=True at every construction route. Pending queries
    use the constant empty index; raw cells are geometry, not permission to label
    accepted EMPTY or enable actions. The runtime coordinator owns live origin,
    current presentation, freshness and action admission, and pins repositories
    while retaining an old display. No session/permission state is cached here.
    """

    rows = 6
    span = 7200
    max_cells = 192

    def __init__(self, repository: MockRepository, *, projection: ChannelProjection | None = None,
                  deferred: bool = False, strings=FALLBACK):
        if type(deferred) is not bool:
            raise ValueError("deferred must be boolean")
        self.repository = repository
        self.strings = strings
        self.deferred = deferred
        self._current_product: IndexProduct | None = None
        self.group = "All"
        self.channels: tuple[Channel, ...] = ()
        self.projection = projection if projection is not None else ChannelProjection(strings)
        self._rows_snapshot: tuple[Channel, ...] | None = None
        self._rows_group: str | None = None
        self._rows: dict[ChannelKey, int] = {}
        self._bind_channels()
        self.top = 0
        self.viewport = repository.now - repository.now % 1800
        self.selection = Selection(None, None, repository.now)
        self.truncated = False
        self._snapshot: tuple[Event, ...] | None = None
        self._schedules: Mapping[ChannelKey, _Schedule | Schedule] = _EMPTY_SCHEDULES
        self._empty = _EMPTY_SCHEDULE
        if self.channels:
            self._at(self.channels[0].key, repository.now)

    @property
    def current_product(self) -> IndexProduct | None:
        """Borrowed installed reference, including when repository refs mismatch.

        Before swap/clear/controller destruction, the caller must offer this
        reference to the worker's bounded retirement slot and obtain its hold.
        This controller does not implement that worker or its acknowledgement.
        """
        return self._current_product

    def _matches_repository(self, product: object, expected: IndexInput) -> bool:
        # Factory validation already walked E. Only exact refs/scalars here;
        # in particular () identity alone is never an accepted-empty receipt.
        return (type(expected) is IndexInput and type(product) is IndexProduct
                and matches_input(product, expected)
                and type(product.events) is tuple and type(product.channels) is tuple
                and product.events is expected.events is self.repository.events
                and product.channels is expected.channels is self.repository.channels
                and expected.horizon == self.repository.horizon
                and type(product.schedules) is MappingProxyType)

    @property
    def prepared_ready(self) -> bool:
        """Exact installed data binding only; NOT canBrowse/TTL/action authority."""
        product = self._current_product
        return (self.deferred and product is not None
                and self._schedules is product.schedules
                and self._matches_repository(product, product.source))

    def adopt_prepared(self, product: IndexProduct, expected: IndexInput) -> bool:
        """Install an exact factory receipt in O(1) + O(C) projection/query work.

        Owner-thread API. Runtime must validate its full submitted context and
        accepted origin first; expected is the EXACT submitted IndexInput object,
        not an equal reconstruction. Failure changes no display state. Successful
        adoption rebinds/clamps selection with the installed indexed queries.

        TWO-PHASE retirement REQUIRED before replacing a different current product:
        caller offers current_product to the bounded worker slot, which must hold
        it before this call. After success, release ALL owner references/holders,
        THEN commit retirement so the worker may drop its hold. If the slot is
        occupied, defer adoption. Rejection leaves the old product installed;
        reconcile/release the offer according to the caller's worker protocol.
        O(1) reference assignment is NOT O(1) destruction: without that prior hold,
        replacing the last old reference may free O(E) objects on this thread.
        No unbounded retired-product cache or worker/permission logic lives here.
        """
        if not self.deferred or not self._matches_repository(product, expected):
            return False
        self._current_product = product
        self._schedules = product.schedules
        self._snapshot = product.events
        self.refresh()
        return True

    def detach_product(self) -> IndexProduct | None:
        """Transfer the installed reference and clear prepared data, not selection.

        Owner-thread API with the SAME prior worker retirement hold/owner-release/
        commit contract as adopt_prepared. Retain the return value until its worker
        hold is established; ignoring it can cause O(E) last-reference destruction
        on owner. Controller destruction needs the same protocol. No disposal is
        claimed off-thread by this method; caller owns lifetime of DTO holders too.
        """
        product = self._current_product
        self._current_product = None
        self._schedules = _EMPTY_SCHEDULES
        self._snapshot = None
        return product

    def _bind_channels(self) -> None:
        projection = self.projection.update(self.repository.channels)
        if projection.channels is not self._rows_snapshot or self.group != self._rows_group:
            # Freeze mutable collections at this explicit snapshot/filter boundary.
            self.channels = tuple(self.repository.channels_for(self.group))
            self._rows = {}
            for index, channel in enumerate(self.channels):
                self._rows.setdefault(channel.key, index)
            self._rows_snapshot, self._rows_group = projection.channels, self.group

    def channel(self, key):
        row = self._rows.get(key)
        return self.channels[row] if row is not None else None

    def _events(self, channel: ChannelKey) -> tuple[Event, ...]:
        return self._schedule(channel).events

    def _schedule(self, channel: ChannelKey) -> _Schedule | Schedule:
        if self.deferred:
            return (self._schedules.get(channel, self._empty)
                    if self.prepared_ready else self._empty)
        snapshot = self.repository.events
        if snapshot is not self._snapshot:
            grouped: dict[ChannelKey, list[Event]] = {}
            for event in snapshot:
                grouped.setdefault(event.channel, []).append(event)
            self._schedules = {key: _Schedule(events) for key, events in grouped.items()}
            self._snapshot = snapshot
        return self._schedules.get(channel, self._empty)

    def extent(self, event: Event) -> int:
        schedule = self._schedule(event.channel)
        index = schedule.positions.get((event.id, event.start))
        if index is not None and schedule.events[index] == event:
            return schedule.ends[index]
        if event.end is not None and event.end > event.start:
            return event.end
        later = bisect_right(schedule.starts, event.start)
        return min(event.start + 1800, schedule.starts[later]
                   if later < len(schedule.starts) else event.start + 1800)

    def selected_event(self) -> Event | None:
        if self.selection.channel is None or self.selection.event_id is None:
            return None
        schedule = self._schedule(self.selection.channel)
        index = (schedule.positions.get((self.selection.event_id, self.selection.event_start))
                 if self.selection.event_start is not None
                 else schedule.first_ids.get(self.selection.event_id))
        return schedule.events[index] if index is not None else None

    def _at(self, channel: ChannelKey, anchor: int) -> None:
        schedule = self._schedule(channel)
        # Most recent start wins navigation at an overlap, without hiding others.
        index = next(schedule.overlapping(anchor, anchor + 1, reverse=True), None)
        event = schedule.events[index] if index is not None else None
        self.selection = Selection(channel, event.id if event else None, anchor,
                                   event.start if event else None)

    def _clamp_time(self, value: int) -> int:
        start, end = self.repository.horizon
        return max(start, min(value, end - 1))

    def _reveal(self) -> None:
        anchor = self.selection.anchor
        if not self.viewport <= anchor < self.viewport + self.span:
            self.viewport = anchor - anchor % 1800
        start, end = self.repository.horizon
        self.viewport = max(start, min(self.viewport, end - self.span))

    def vertical(self, direction: int) -> None:
        if not self.channels:
            return
        key = self.selection.channel
        index = self._rows.get(key, 0) if key is not None else 0
        index = max(0, min(index + direction, len(self.channels) - 1))
        self._at(self.channels[index].key, self.selection.anchor)
        self.top = max(0, min(self.top, index))
        if index >= self.top + self.rows:
            self.top = index - self.rows + 1

    def horizontal(self, direction: int) -> None:
        key = self.selection.channel
        if key is None:
            return
        schedule = self._schedule(key)
        events = schedule.events
        selected = self.selected_event()
        if selected is not None:
            index = schedule.positions[(selected.id, selected.start)] + direction
            event = events[index] if 0 <= index < len(events) else None
        elif direction > 0:
            index = bisect_right(schedule.starts, self.selection.anchor)
            event = events[index] if index < len(events) else None
        else:
            index = bisect_right(schedule.end_times, self.selection.anchor) - 1
            event = events[schedule.last_ended[index]] if index >= 0 else None
        if event is not None:
            anchor = self._clamp_time(event.start)
            self.selection = Selection(key, event.id, anchor, event.start)
        else:
            self._at(key, self._clamp_time(self.selection.anchor + direction * 1800))
        self._reveal()

    def jump(self, timestamp: int) -> None:
        anchor = self._clamp_time(timestamp)
        if self.selection.channel is not None:
            self._at(self.selection.channel, anchor)
        else:
            self.selection = Selection(None, None, anchor)
        self.viewport = anchor - anchor % 1800
        self._reveal()

    def focus_event(self, event: Event) -> bool:
        """Explicit search-to-guide selection by complete airing identity.

        Reject removed/filtered/out-of-horizon airings without changing position.
        In an overlap, don't re-resolve by time and accidentally choose a sibling.
        """
        row = self._rows.get(event.channel)
        if row is None:
            return False
        schedule = self._schedule(event.channel)
        index = schedule.positions.get((event.id, event.start))
        if index is None:
            return False
        current = schedule.events[index]
        start, end = self.repository.horizon
        if current.start >= end or self.extent(current) <= start:
            return False
        self.selection = Selection(current.channel, current.id,
                                   self._clamp_time(current.start), current.start)
        self.top = max(0, min(self.top, row))
        if row >= self.top + self.rows:
            self.top = row - self.rows + 1
        self._reveal()
        return True

    def refresh(self, anchor: int | None = None) -> None:
        """Rebind an accepted repository snapshot without merging channel IDs.

        Keep the exact selected airing when its identity and viewing anchor are
        still valid; otherwise resolve at the intended time on an eligible key.
        No network, cache or GUI work belongs in this controller.
        """
        if anchor is not None and type(anchor) is not int:
            raise ValueError("anchor must be integer UTC seconds")
        previous = self.selection
        target = self._clamp_time(previous.anchor if anchor is None else anchor)
        if self.group not in self.repository.groups:
            self.group = "All"
        self._bind_channels()
        row = self._rows.get(previous.channel) if previous.channel is not None else None
        if row is None:
            row = 0
            key = self.channels[0].key if self.channels else None
        else:
            key = self.channels[row].key
        current = self.selected_event() if key == previous.channel else None
        if key is None:
            self.selection = Selection(None, None, target)
        elif (current is not None and target == previous.anchor
              and current.start <= target < self.extent(current)):
            self.selection = Selection(key, current.id, target, current.start)
        else:
            self._at(key, target)
        self.top = max(0, min(self.top, row, max(0, len(self.channels) - self.rows)))
        if row >= self.top + self.rows:
            self.top = row - self.rows + 1
        self._reveal()

    def set_group(self, group: str) -> None:
        if group not in self.repository.groups:
            raise ValueError("Unknown local group")
        self.group = group
        self._bind_channels()
        previous = self.selection
        key = self.selection.channel
        if key not in self._rows:
            key = self.channels[0].key if self.channels else None
        if key is None:
            self.selection = Selection(None, None, self.selection.anchor)
        else:
            if key != previous.channel or self.selected_event() is None:
                self._at(key, previous.anchor)
        index = self._rows.get(key, 0) if key is not None else 0
        self.top = max(0, min(self.top, index, max(0, len(self.channels) - self.rows)))
        if index >= self.top + self.rows:
            self.top = index - self.rows + 1

    def _row_cells(self, index: int, budget: int) -> tuple[list[Cell], int]:
        """Bound both output and candidate work; pin selection before iteration."""
        if budget <= 0:
            self.truncated = True
            return [], 0
        key = self.channels[index].key
        schedule = self._schedule(key)
        left, right = self.viewport, self.viewport + self.span
        result: list[Cell] = []
        selected = self.selected_event() if key == self.selection.channel else None
        pin = (selected if selected is not None and selected.start < right
               and self.extent(selected) > left else None)

        def cell(start, end, event=None, lane=0, chosen=False, overflow=False):
            title = (event.title + (" [?]" if event.end is None else "")
                     if event is not None else text(self.strings, 'no_listing'))
            if overflow:
                title = text(self.strings, 'overlapping_listings')
            return Cell(key, event.id if event else None, index - self.top,
                        (max(left, start) - left) / self.span,
                        (min(right, end) - max(left, start)) / self.span, lane, title,
                        start, end, chosen,
                        start < left, end > right, overflow)

        if pin is not None:
            result.append(cell(pin.start, self.extent(pin), pin, chosen=True))
        elif (key == self.selection.channel and self.selection.event_id is None
              and left <= self.selection.anchor < right):
            at = bisect_right(schedule.starts, self.selection.anchor)
            start = max(left, schedule.prefix_max[at - 1] if at else left)
            end = min(right, schedule.starts[at] if at < len(schedule.starts) else right)
            if start <= self.selection.anchor < end:
                result.append(cell(start, end, chosen=True))

        def gap(start, end):
            if any(c.selected and c.event_id is None and c.start == start and c.end == end
                   for c in result):
                return
            if len(result) < budget:
                result.append(cell(start, end))
            else:
                self.truncated = True

        cursor = left
        lane_end = [left, left]
        lane_one: int | None = None
        matches = schedule.overlapping(left, right)
        exhausted = False
        visited = 0
        for _ in range(budget):
            event_index = next(matches, None)
            if event_index is None:
                exhausted = True
                break
            visited += 1
            event = schedule.events[event_index]
            start, end = event.start, schedule.ends[event_index]
            if start > cursor:
                # Prefer an actual airing over a gap when only one slot remains.
                if len(result) + 1 < budget or event == pin:
                    gap(cursor, start)
                else:
                    self.truncated = True
            cursor = max(cursor, end)
            if event == pin:
                lane_end[0] = max(lane_end[0], min(right, end))
                continue
            free_zero = (max(left, start) >= lane_end[0] and
                         (pin is None or end <= pin.start or start >= self.extent(pin)))
            lane = 0 if free_zero else 1
            if lane == 1 and max(left, start) < lane_end[1]:
                # Chronological insertion means only the last lane-1 cell can
                # overlap. Unioning it cannot collide with another lane-1 cell.
                assert lane_one is not None  # A busy lane has a materialized cell.
                previous = result[lane_one]
                result[lane_one] = cell(previous.start, max(previous.end, end),
                                        lane=1, overflow=True)
            elif len(result) < budget:
                result.append(cell(start, end, event, lane))
                if lane == 1:
                    lane_one = len(result) - 1
            else:
                self.truncated = True
                break
            lane_end[lane] = max(lane_end[lane], min(right, end))
        else:
            exhausted = next(matches, None) is None
            if not exhausted:
                self.truncated = True
        if exhausted and cursor < right:
            gap(cursor, right)
        return result, max(visited, len(result))

    def cells(self, overscan: int = 1) -> tuple[Cell, ...]:
        """Reserve a fair share for every visible row, then spend on overscan.

        Each row visits at most its share of candidates plus one lookahead.
        Selection is materialized first, even if it lies beyond that prefix.
        The returned order is channel/time/lane, independent of priority order.
        """
        result = []
        self.truncated = False
        remaining = self.max_cells
        visible = list(range(self.top, min(len(self.channels), self.top + self.rows)))
        extra = []
        if overscan > 0 and visible:
            if self.top > 0:
                extra.append(self.top - 1)
            if visible[-1] + 1 < len(self.channels):
                extra.append(visible[-1] + 1)
        for indices in (visible, extra):
            for offset, index in enumerate(indices):
                share = remaining // (len(indices) - offset)
                cells, spent = self._row_cells(index, share)
                result.extend(cells)
                remaining -= spent
        return tuple(sorted(result, key=lambda c: (c.row, c.x, c.lane)))
