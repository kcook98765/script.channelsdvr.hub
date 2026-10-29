"""Fixed worker-only synthetic DTO generation; no repository/owner helpers or I/O.

These are new index-worker fixtures, not an implicit replacement for the existing
UI repository fixture. Revision affects titles/version, never the data clock.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .models import Channel, ChannelKey, Event


class FixtureMode(str, Enum):
    NORMAL = "normal"
    STRESS = "stress"
    LARGE500 = "large500"
    DENSE500_48H = "dense500_48h"


@dataclass(frozen=True, eq=False, slots=True)
class FixtureSnapshot:
    mode: FixtureMode
    now: int
    revision: int
    namespace: str
    version: str
    horizon: tuple[int, int]
    fetched_at: int
    channels: tuple[Channel, ...]
    events: tuple[Event, ...]
    groups: tuple[str, ...] = ("All", "Visible", "Favorites", "Empty")


def generate_fixture(mode: FixtureMode, now: int, revision: int, *,
                     check: Callable[[], None]) -> FixtureSnapshot:
    """Called only by the closed fixture intent on MetadataLoader's one worker."""
    if (type(mode) is not FixtureMode or type(now) is not int or now < 0
            or type(revision) is not int or revision < 0):
        raise ValueError("invalid_fixture_input")
    check()
    large = mode in (FixtureMode.LARGE500, FixtureMode.DENSE500_48H)
    count = 500 if large else 12
    slots = 96 if mode is FixtureMode.DENSE500_48H else 8
    duration = 1800 if mode is FixtureMode.DENSE500_48H else 900
    namespace = "guide-scaling-synthetic" if large else "guide-worker-synthetic"
    channels = []
    events = []
    for row in range(count):
        check()
        key = ChannelKey(namespace, f"source-{row % 2}", str(row // 2))
        channels.append(Channel(key, str(row // 2), f"SYNTHETIC row {row + 1}/{count}",
                                ("Visible",) + (("Favorites",) if row % 2 == 0 else ())))
        for slot in range(slots):
            events.append(Event(f"synthetic-{row}-{slot}", key, now + slot * duration,
                                now + (slot + 1) * duration,
                                f"Synthetic row {row + 1} slot {slot + 1} revision {revision}",
                                "Fixed offline worker fixture; no backend data."))
    if mode is FixtureMode.STRESS:
        key = channels[0].key
        events.extend((Event("repeat", key, now, now + 600, "Repeated ID A"),
                       Event("repeat", key, now + 600, now + 1200, "Repeated ID B"),
                       Event("long", channels[2].key, now - 3600, now + 10800, "Long"),
                       Event("unknown", channels[3].key, now - 60, None, "Unknown")))
        for index in range(1200):
            if index % 1024 == 0:
                check()
            events.append(Event(f"tiny-{index}", key, now + 1200 + index * 5,
                                now + 1205 + index * 5, "Five-second synthetic cell"))
    check()
    catalog, accepted = tuple(channels), tuple(events)
    check()
    return FixtureSnapshot(mode, now, revision, namespace, f"fixture-{revision}",
                           (now, now + slots * duration), now, catalog, accepted)
