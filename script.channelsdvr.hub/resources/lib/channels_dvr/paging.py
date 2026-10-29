"""Bounded collection of supplied pages; never fetches or executes requests.

Query includes resource, filters, ordering and page size. Cursor None means the
first page (and next_cursor None means terminal). Completion means the cursor
chain ended, NOT that the server provided a consistent point-in-time snapshot.
Dedup keeps the first observation; a later scan may legitimately differ.
"""

from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from typing import Generic, TypeVar

from .normalization import DataState

T = TypeVar("T")


@dataclass(frozen=True)
class Query:
    resource: str
    filters: tuple[tuple[str, str], ...] = ()
    order: tuple[str, ...] = ("id",)
    page_size: int = 100

    def __post_init__(self):
        if not isinstance(self.resource, str) or not self.resource:
            raise ValueError("resource must be explicit")
        if type(self.page_size) is not int or not 1 <= self.page_size <= 1000:
            raise ValueError("page_size must be 1..1000")
        if not isinstance(self.order, tuple) or not self.order or any(
            not isinstance(field, str) or not field for field in self.order
        ):
            raise ValueError("explicit nonempty ordering required")
        if not isinstance(self.filters, tuple) or any(
            not isinstance(pair, tuple) or len(pair) != 2
            or any(not isinstance(part, str) for part in pair)
            for pair in self.filters
        ):
            raise ValueError("filters must be immutable text pairs")
        if len({name for name, _ in self.filters}) != len(self.filters):
            raise ValueError("duplicate filter names")


@dataclass(frozen=True)
class Page(Generic[T]):
    query: Query
    cursor: str | None
    items: tuple[T, ...]
    next_cursor: str | None
    state: DataState = DataState.READY
    detail: str = ""


@dataclass(frozen=True)
class ScanResult(Generic[T]):
    items: tuple[T, ...]
    state: DataState
    complete: bool
    pages: int
    detail: str = ""


def collect_pages(query: Query, pages: Iterable[Page[T]],
                  identity: Callable[[T], Hashable], *, max_pages: int = 20,
                  max_items: int = 1000) -> ScanResult[T]:
    """Use stable source-aware identity, not display numbers or list offsets.

    Input exhaustion without a terminal page, repeated cursors, continuation
    without new identities, and limits produce PARTIAL, never a complete empty
    result. Iterator exceptions propagate; pass outage as an explicit Page.
    """
    if any(type(limit) is not int or limit < 1 for limit in (max_pages, max_items)):
        raise ValueError("positive integer scan bounds required")
    items: list[T] = []
    identities: set[Hashable] = set()
    cursors: set[str | None] = set()
    cursor, count = None, 0

    def result(state, detail="", complete=False):
        return ScanResult(tuple(items), state, complete, count, detail)

    iterator = iter(pages)
    while count < max_pages:
        try:
            page = next(iterator)
        except StopIteration:
            return result(DataState.PARTIAL, "input ended before terminal page")
        count += 1
        if not isinstance(page, Page) or page.query != query or page.cursor != cursor:
            return result(DataState.MALFORMED, "page does not match query/cursor")
        if not isinstance(page.state, DataState):
            return result(DataState.MALFORMED, "invalid page state")
        if page.state not in (DataState.READY, DataState.EMPTY):
            return result(page.state, page.detail)
        if (not isinstance(page.items, tuple) or len(page.items) > query.page_size
                or (page.next_cursor is not None and (
                    not isinstance(page.next_cursor, str) or not page.next_cursor))
                or (page.state == DataState.EMPTY and page.items)):
            return result(DataState.MALFORMED, "invalid page shape")
        cursors.add(cursor)
        added = 0
        for item in page.items:
            try:
                key = identity(item)
                duplicate = key in identities
            except (TypeError, ValueError, KeyError, AttributeError):
                return result(DataState.MALFORMED, "invalid item identity")
            if duplicate:
                continue
            if len(items) >= max_items:
                return result(DataState.PARTIAL, "item limit reached")
            identities.add(key)
            items.append(item)
            added += 1
        if page.next_cursor is None:
            return result(DataState.READY if items else DataState.EMPTY, complete=True)
        if page.next_cursor in cursors or not added:
            return result(DataState.PARTIAL, "nonprogressing cursor or identities")
        if len(items) >= max_items:
            return result(DataState.PARTIAL, "item limit reached")
        cursor = page.next_cursor
    return result(DataState.PARTIAL, "page limit reached")
