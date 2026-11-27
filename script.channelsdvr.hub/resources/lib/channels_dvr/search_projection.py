"""Unified Search over explicitly accepted local listings, not a provider query.

Supply only snapshots bound to the current source and scope, or None if no
snapshot was accepted. Listing health is independent of retained data and the
result limit. Neither a hit nor its identity is admission to open or act on it.
"""

from dataclasses import dataclass, replace
from enum import Enum
from heapq import nsmallest
from itertools import islice
from typing import Callable, Generic, Iterator, TypeAlias, TypeVar

from .browser import recording_key
from .models import ChannelKey, Job, Recording
from .search import (MAX_SEARCH_CHANNELS, MAX_SEARCH_RESULTS, ProgrammeSearchIndex,
                     airing_key, matches_tokens, normalize_query, normalize_search_text,
                     recording_search_text)


class SearchCategory(str, Enum):
    ALL = 'all'
    GUIDE = 'guide'
    RECORDINGS = 'recordings'
    SCHEDULED = 'scheduled'
    PASSES = 'passes'


_GROUP_ORDER = (SearchCategory.GUIDE, SearchCategory.RECORDINGS,
                SearchCategory.SCHEDULED, SearchCategory.PASSES)
MAX_LISTING_RECORDS = 10000
T = TypeVar('T')


@dataclass(frozen=True)
class ListingCoverage:
    """Orthogonal listing flags; unavailable can accompany a retained fallback."""

    loading: bool = False
    stale: bool = False
    partial: bool = False
    unavailable: bool = False
    capped: bool = False


@dataclass(frozen=True)
class Listing(Generic[T]):
    """None means no admission; a present but empty snapshot is positive evidence."""

    snapshot: T | None = None
    coverage: ListingCoverage = ListingCoverage()


@dataclass(frozen=True)
class GuideScope:
    """Half-open window and exact channel keys admitted for this search."""

    horizon: tuple[int, int]
    channels: tuple[ChannelKey, ...]


@dataclass(frozen=True)
class GuideSnapshot:
    index: ProgrammeSearchIndex
    scope: GuideScope


@dataclass(frozen=True)
class PassEntry:
    """Only the exact ID and plain name from the admitted read-only listing."""

    id: str
    name: str

    def __post_init__(self) -> None:
        if type(self.id) is not str or not self.id or type(self.name) is not str:
            raise ValueError('invalid pass search entry')


@dataclass(frozen=True)
class ProgrammeId:
    source: str
    channel: ChannelKey
    event_id: str
    start: int


@dataclass(frozen=True)
class RecordingId:
    source: str
    namespace: str
    provenance: str
    recording_id: str


@dataclass(frozen=True)
class ScheduledId:
    source: str
    job_id: str


@dataclass(frozen=True)
class PassId:
    source: str
    pass_id: str


SearchId: TypeAlias = ProgrammeId | RecordingId | ScheduledId | PassId


@dataclass(frozen=True)
class SearchHit:
    id: SearchId
    title: str
    when: int | None = None  # accepted start time or recording Added time


@dataclass(frozen=True)
class SearchGroup:
    category: SearchCategory
    hits: tuple[SearchHit, ...]
    has_snapshot: bool
    coverage: ListingCoverage
    truncated: bool  # at least one known matching hit omitted by the result budget

    @property
    def coverage_complete(self) -> bool:
        """Complete only inside the supplied local scope, independently of truncation."""
        return self.has_snapshot and self.coverage == ListingCoverage()


@dataclass(frozen=True)
class SearchResults:
    query: str
    groups: tuple[SearchGroup, ...]
    guide_scope: GuideScope | None

    def find(self, identity: SearchId) -> SearchHit | None:
        """Find an exact hit on this page for selection restoration, not activation."""
        return next((hit for group in self.groups for hit in group.hits if hit.id == identity), None)


_Entry: TypeAlias = tuple[SearchHit, str, tuple[int, str, tuple[str, ...]]]


def _recording(source: str, record: Recording) -> _Entry:
    key = recording_key(record)
    return (SearchHit(RecordingId(source, *key), record.title, record.created),
            recording_search_text(record), (-record.created, '', key))


def _scheduled(source: str, job: Job) -> _Entry:
    return (SearchHit(ScheduledId(source, job.id), job.title, job.start),
            normalize_search_text(job.title), (job.start, '', (job.id,)))


def _pass(source: str, entry: PassEntry) -> _Entry:
    name = normalize_search_text(entry.name)
    return SearchHit(PassId(source, entry.id), entry.name), name, (0, name, (entry.id,))


def _flat_group(category: SearchCategory, listing: Listing[tuple[T, ...]],
                entry: Callable[[str, T], _Entry], source: str, tokens: list[str],
                remaining: int) -> SearchGroup:
    records, coverage = listing.snapshot, listing.coverage
    if records is None:
        return SearchGroup(category, (), False, coverage, False)
    if type(records) is not tuple:
        raise ValueError('search listing must be an accepted tuple')
    if len(records) > MAX_LISTING_RECORDS:
        coverage = replace(coverage, capped=True)
    if not tokens:
        return SearchGroup(category, (), True, coverage, False)

    def matches(items: tuple[T, ...]) -> Iterator[_Entry]:
        seen: set[SearchId] = set()
        for record in islice(items, MAX_LISTING_RECORDS):
            candidate = entry(source, record)
            identity = candidate[0].id
            if identity not in seen:
                seen.add(identity)  # first exact observation wins, not first match
                if matches_tokens(candidate[1], tokens):
                    yield candidate

    ranked = nsmallest(remaining + 1, matches(records), key=lambda item: item[2])
    return SearchGroup(category, tuple(item[0] for item in ranked[:remaining]),
                       True, coverage, len(ranked) > remaining)


def _guide_group(listing: Listing[GuideSnapshot], source: str, text: str,
                 limit: int) -> tuple[SearchGroup, GuideScope | None]:
    snapshot, coverage = listing.snapshot, listing.coverage
    if snapshot is None:
        return SearchGroup(SearchCategory.GUIDE, (), False, coverage, False), None
    index, scope = snapshot.index, snapshot.scope
    if (type(scope.horizon) is not tuple or type(scope.channels) is not tuple
            or len(scope.channels) > MAX_SEARCH_CHANNELS):
        raise ValueError('invalid bounded Guide scope')
    page = index.search_local(text, allowed_channels=scope.channels, horizon=scope.horizon, limit=limit)
    coverage = replace(coverage, partial=coverage.partial or index.coverage_partial,
                       capped=coverage.capped or index.coverage_capped)
    hits = tuple(SearchHit(ProgrammeId(source, *airing_key(event)), event.title, event.start)
                 for event in page.matches)
    return SearchGroup(SearchCategory.GUIDE, hits, True, coverage, page.truncated), scope


def project_search(text: str, *, source: str, category: SearchCategory = SearchCategory.ALL,
                   guide: Listing[GuideSnapshot] = Listing(),
                   recordings: Listing[tuple[Recording, ...]] = Listing(),
                   scheduled: Listing[tuple[Job, ...]] = Listing(),
                   passes: Listing[tuple[PassEntry, ...]] = Listing(),
                   limit: int = 100) -> SearchResults:
    """Rank within each group and fill one shared budget in fixed group order.

    Guide and scheduled rank by earliest start, recordings by newest Added,
    and passes by normalized name; exact identity breaks ties within each group.
    The caller's opaque source key stays stable on refresh, but must change on
    source/fixture replacement. Flat listings search at most the first 10,000
    accepted records per category. A blank query returns coverage without hits.
    """
    query = normalize_query(text)
    if not isinstance(category, SearchCategory):
        raise ValueError('unknown search category')
    if type(source) is not str or not source.strip():
        raise ValueError('search source key is required')
    if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise ValueError(f'limit must be an integer from 1 to {MAX_SEARCH_RESULTS}')
    tokens, remaining = query.split(), limit
    groups: list[SearchGroup] = []
    scope = None
    for kind in _GROUP_ORDER if category is SearchCategory.ALL else (category,):
        if kind is SearchCategory.GUIDE:  # always first, with the full limit
            group, scope = _guide_group(guide, source, text, limit)
        elif kind is SearchCategory.RECORDINGS:
            group = _flat_group(kind, recordings, _recording, source, tokens, remaining)
        elif kind is SearchCategory.SCHEDULED:
            group = _flat_group(kind, scheduled, _scheduled, source, tokens, remaining)
        else:
            group = _flat_group(kind, passes, _pass, source, tokens, remaining)
        groups.append(group)
        remaining -= len(group.hits)
    return SearchResults(query, tuple(groups), scope)
