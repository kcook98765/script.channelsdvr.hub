"""Deterministic synthetic repository. Construction performs no I/O.

The half-open, three-day horizon includes the US fall-back transition. Channel
and event IDs deliberately collide across sources; channel numbers are labels.
"""

from collections.abc import Iterable
from datetime import datetime, timezone
from itertools import groupby

from .models import (
    Channel, ChannelKey, Event, Job, JobMatch, MatchStatus, Recording, RecordingGroup,
)


def recording_groups(recordings: Iterable[Recording]) -> tuple[RecordingGroup, ...]:
    """Series first, then movies, each partitioned by namespace and provenance.

    Within each origin, series sort/group by tagged show identity then season:
    unknown IDs use show/title text, known IDs use show_id even after a rename.
    Movies share one group per origin. Items are newest first, then ID.
    Watched and resume are independent facts and never inferred from duration.
    """
    def key(recording):
        if recording.is_movie:
            return (True, recording.namespace, recording.provenance, False, "", -1)
        known_show = recording.show_id is not None
        identity = recording.show_id if known_show else recording.show or recording.title
        return (False, recording.namespace, recording.provenance, known_show, identity,
                recording.season if recording.season is not None else -1)

    ordered = sorted(recordings, key=lambda r: (key(r), -r.created, r.id))
    groups = []
    for (movie, namespace, provenance, known_show, identity, season), items in groupby(
            ordered, key):
        members = tuple(items)
        groups.append(RecordingGroup(
            None if movie else members[0].show or members[0].title,
            None if season == -1 else season, movie, members, namespace, provenance,
            identity if known_show else None,
        ))
    return tuple(groups)


def job_match(job: Job, events: Iterable[Event]) -> JobMatch:
    """Resolve only within candidate channels; never pick the first ambiguity.

    Explicit IDs are authoritative, not titles or padded time ranges. Multiple
    requested IDs remain ambiguous even if only one is present in this scan.
    Without IDs, require exact unpadded start/end. An incomplete event scan can
    still omit competing events: this result describes only the supplied data.
    """
    matches = tuple(dict.fromkeys(
        event for event in events
        if event.channel in job.candidates and (
            event.id in job.event_ids if job.event_ids
            else (event.start == job.start and event.end == job.end)
        )
    ))
    if len(set(job.event_ids)) > 1 or len(matches) > 1:
        return JobMatch(MatchStatus.AMBIGUOUS, None, matches)
    if len(matches) == 1:
        return JobMatch(MatchStatus.MATCHED, matches[0], matches)
    return JobMatch(MatchStatus.UNRESOLVED, None)


class MockRepository:
    """All fixtures are generated locally and exposed as immutable tuples."""

    channels: tuple[Channel, ...]
    events: tuple[Event, ...]
    recordings: tuple[Recording, ...]
    jobs: tuple[Job, ...]
    horizon: tuple[int, int]
    now: int
    groups: tuple[str, ...]

    def __init__(self) -> None:
        self.now = int(datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc).timestamp())
        start = int(datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp())
        self.horizon = (start, start + 3 * 86400)
        names = ("Demo News", "Demo Science", "Demo Stories", "Demo Arts",
                 "Demo Sports", "Demo Travel", "Demo Nature", "Demo Music",
                 "Demo Cinema", "Demo Weather")
        self.channels = tuple(
            Channel(ChannelKey("synthetic", "antenna" if i < 5 else "cable",
                               str(i % 5 + 1)),
                    str(i % 5 + 1), name,
                    ("Favorites", "General") if i % 2 == 0 else ("General",))
            for i, name in enumerate(names)
        )
        self.groups = ("All", "Favorites", "General", "Empty")
        events = []
        for day in range(3):
            for index, channel in enumerate(self.channels):
                base = start + day * 86400 + index * 60
                # A 30-minute gap, a 10-minute overlap, and a five-minute item.
                for slot, offset, duration in ((0, 0, 3600), (1, 5400, 7200),
                                               (2, 12000, 300), (3, 18000, 18000)):
                    events.append(Event(
                        f"d{day}-c{index % 5}-s{slot}", channel.key,
                        base + offset, base + offset + duration,
                        f"{channel.name}: synthetic programme {slot + 1}",
                        "Synthetic offline listing; no artwork supplied.",
                    ))
                events.append(Event(f"open-{day}-{index}", channel.key,
                                    base + 42000, None, "End time unavailable"))
        # At now, include short, long and ambiguous source-colliding listings.
        for channel in (self.channels[0], self.channels[5]):
            events.append(Event("dst-special", channel.key, self.now - 1800,
                                self.now + 1800, "Synthetic fall-back special"))
        self.events = tuple(sorted(events, key=lambda e: (
            e.start, e.channel.source_id, e.channel.channel_id, e.id)))
        self.recordings = (
            Recording("series-1", "A first chapter", "Synthetic Atlas", 1, 1,
                      False, self.now - 7200, 1800, True, 0,
                      namespace="synthetic", provenance="dvr", show_id="atlas"),
            Recording("series-2", "A second chapter", "Synthetic Atlas", 1, 2,
                      False, self.now - 3600, 1800, False, 900,
                      namespace="synthetic", provenance="dvr", show_id="atlas"),
            Recording("series-3", "A new season", "Synthetic Atlas", 2, 1,
                      False, self.now - 1800, None,
                      namespace="synthetic", provenance="dvr", show_id="atlas"),
            Recording("series-4", "An unnumbered story", "Synthetic Workshop",
                      None, None, False, self.now - 10000, 1200, True, 300,
                      namespace="synthetic", provenance="dvr", show_id="workshop"),
            Recording("movie-1", "The Paper Planet", None, None, None, True,
                      self.now - 600, 7200, False, 7200,
                      namespace="synthetic", provenance="dvr"),
            Recording("movie-2", "A Quiet Orbit", None, None, None, True,
                      self.now - 86400, None, namespace="synthetic", provenance="dvr"),
            Recording("series-2", "Imported second chapter", "Synthetic Atlas", 1, 2,
                      False, self.now - 3600, None, True, 120,
                      namespace="synthetic", provenance="import", show_id="atlas-import"),
            Recording("movie-1", "The Paper Planet (imported)", None, None, None, True,
                      self.now - 600, 7200, False,
                      namespace="synthetic", provenance="import"),
            Recording("unknown-1", "Unclassified sample", None, None, None, False,
                      self.now - 1200, None),
        )
        self.jobs = (
            Job("job-1", "Padded special", self.now - 1800, self.now + 1800,
                (self.channels[0].key,), ("dst-special",), 120, 300),
            Job("job-2", "Ambiguous source", self.now - 1800, self.now + 1800,
                (self.channels[0].key, self.channels[5].key), ("dst-special",),
                60, 60),
            Job("job-3", "Unresolved event alternatives", self.now + 3600,
                self.now + 7200, (self.channels[1].key,),
                ("not-present-a", "not-present-b"), 30, 90),
            Job("job-4", "Missing listing", self.now + 7200, self.now + 9000,
                (self.channels[2].key,), ("not-present",)),
        )

    def channels_for(self, group: str) -> tuple[Channel, ...]:
        if group == "All":
            return self.channels
        if group == "Empty":
            return ()
        return tuple(channel for channel in self.channels if group in channel.groups)

    def events_for(self, key: ChannelKey) -> tuple[Event, ...]:
        return tuple(event for event in self.events if event.channel == key)
