"""Fixed, explicit synthetic UI fixtures. No settings, I/O, threads or SDK imports.

The 500-row traversal performs at most ONE normal navigation per owner poll.
Any input cancels it; replacing the guide/view/session or closing rejects late
ticks. This is a fixed fixture exercise, not a caller-supplied command executor.
"""

from dataclasses import dataclass, replace

from .guide import GuideController, Selection
from .guide_fixture_data import FixtureMode
from .library import LibraryBrowser
from .models import Channel, ChannelKey, Event
from .normalization import DataState
from .repository import MockRepository
from .session import OfflineDataSession
from .ui_text import FALLBACK, text


def _t(window, key, **values):
    return text(getattr(window, 'strings', FALLBACK), key, **values)


def stress_repository():
    repo = MockRepository()
    now, channels = repo.now, repo.channels
    events = [
        Event("repeat", channels[0].key, now, now + 600, "Repeated ID airing A"),
        Event("repeat", channels[0].key, now + 600, now + 1200, "Repeated ID airing B"),
        Event("long", channels[2].key, now - 3600, now + 10800, "Long continuation"),
        Event("unknown", channels[3].key, now - 60, None, "Unknown duration"),
    ]
    events.extend(Event(f"tiny-{i}", channels[0].key, now + 1200 + i * 5,
                        now + 1205 + i * 5, "Five-second synthetic cell")
                  for i in range(1200))
    events.extend(Event(f"overlap-{i}", channels[1].key, now - 100 + i * 10,
                        now + 900 + i * 60, f"Overlap {i + 1}") for i in range(4))
    events.extend(Event("repeat", channel.key, now - 300, now + 1800,
                        "Other source-aware channel") for channel in channels[4:])
    repo.events = tuple(events)
    return repo


def large_repository():
    repo = MockRepository()
    # Fixed synthetic clock, aligned to 30 minutes; exactly the normal 2h scope.
    repo.now = 1_800_000_000
    repo.horizon = (repo.now, repo.now + 7200)
    repo.channels = tuple(Channel(
        ChannelKey("guide-scaling-synthetic", f"source-{i % 2}", str(i // 2)),
        str(i // 2), f"SYNTHETIC row {i + 1}/500",
        ("Visible",) + (("Favorites",) if i % 2 == 0 else ())) for i in range(500))
    repo.events = tuple(Event(
        f"synthetic-{i}-{slot}", channel.key,
        repo.now + slot * 900, repo.now + (slot + 1) * 900,
        f"Synthetic row {i + 1} slot {slot + 1}", "Fixed offline scaling fixture; no backend data.")
        for i, channel in enumerate(repo.channels) for slot in range(8))
    repo.groups = ("All", "Visible", "Favorites", "Empty")
    repo.recordings, repo.jobs = (), ()
    return repo


def install_fixture(window, kind):
    window._dispatcher.assert_owner()
    if window._closed or window._live_session is not None or not window.demo_allowed():
        return
    if kind not in ("normal", "stress", "large500"):
        raise ValueError("Unknown fixed synthetic fixture")
    cancel_large(window, "fixture replaced")
    if kind == "large500":
        window._guide_runtime.fixture(FixtureMode.LARGE500)
    else:
        window._guide_runtime.mode(kind)
    window.view = "Guide"


def large_menu(window):
    install_fixture(window, "normal" if window._large_fixture else "large500")


@dataclass(frozen=True)
class _Traversal:
    guide: GuideController
    repository: MockRepository
    session: OfflineDataSession
    channels: tuple[Channel, ...]
    events: tuple[Event, ...]
    anchor: int
    viewport: int
    updated_at: int
    completed: int = 0


def cancel_large(window, reason):
    state = window._large_exercise
    if state is not None:
        window._large_exercise = None
        window.exercise_summary = _t(window, 'traversal_stopped', completed=state.completed, reason=reason)


def _mode_ready(window):
    return (not window._closed and window._live_session is None and window.demo_allowed()
            and window._development_blocked is None and window.view == "Guide"
            and window._large_fixture)


def _data_ready(data):
    # Retained stale data may be browsed manually, but cannot certify this exercise.
    return (data.can_browse and data.state == DataState.READY
            and not data.loading and not data.stale and data.updated_at is not None)


def _context_ready(window, state):
    if (not _mode_ready(window) or window.guide is not state.guide
            or window.repository is not state.repository
            or window.data_session is not state.session
            or not state.guide.prepared_ready
            or state.guide.group != "All" or state.guide.channels is not state.channels
            or state.repository.channels is not state.channels
            or state.repository.events is not state.events
            or state.guide.viewport != state.viewport):
        return False
    data = window._current_data()
    return (_data_ready(data) and data.items is state.events
            and data.updated_at == state.updated_at
            and state.repository.events is state.events)


def _at_row(state, row):
    event = state.events[row * 8]
    key = state.channels[row].key
    return (event.channel == key and state.guide.selection == Selection(
                key, event.id, state.anchor, event.start)
            and state.guide.selected_event() is event
            and state.guide.top == max(0, row - state.guide.rows + 1))


def traverse_large(window):
    cancel_large(window, "restarted")
    # Never turn an unavailable scenario into Healthy or bypass _navigate's guard.
    if not _mode_ready(window):
        window.exercise_summary = _t(window, 'traversal_stopped', completed=0, reason='mode unavailable')
        return
    data = window._current_data()
    repo, guide = window.repository, window.guide
    if (not _data_ready(data) or data.items is not repo.events
            or len(repo.channels) != 500 or len(repo.events) != 4000):
        window.exercise_summary = _t(window, 'traversal_stopped', completed=0, reason='data unavailable')
        return
    guide.set_group("All")
    if not guide.focus_event(repo.events[0]):
        window.exercise_summary = _t(window, 'traversal_stopped', completed=0, reason='first row unavailable')
        return
    # _data_ready above narrows the admission logically; explicit narrowing also
    # keeps the captured timestamp an int without a cast or optional-type escape.
    updated_at = data.updated_at
    if updated_at is None:
        return
    state = _Traversal(guide, repo, window.data_session, guide.channels, repo.events,
                       repo.events[0].start, guide.viewport, updated_at)
    window._large_exercise = state
    if not _context_ready(window, state) or not _at_row(state, 0):
        cancel_large(window, "first row/snapshot changed")
        return
    window.exercise_summary = _t(window, 'traversal_running', completed=0)


def tick_large(window):
    state = window._large_exercise
    if state is None:
        return
    if not _context_ready(window, state) or not _at_row(state, state.completed):
        cancel_large(window, "readiness/selection changed")
        return
    window._navigate(4)
    # Navigation may be guarded, misordered, cancelled, or replaced reentrantly.
    if window._large_exercise is not state:
        return
    completed = state.completed + 1
    if not _context_ready(window, state) or not _at_row(state, completed):
        cancel_large(window, "expected next source-qualified row not verified")
        return
    window._large_exercise = replace(state, completed=completed) if completed < 499 else None
    if completed == 499:
        window.exercise_summary = _t(window, 'traversal_done')
    else:
        window.exercise_summary = _t(window, 'traversal_running', completed=completed)
