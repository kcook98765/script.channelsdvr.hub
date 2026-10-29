"""Offline controller, browser and mailbox contracts; no Kodi imports."""

from dataclasses import replace
from datetime import datetime, timezone
from threading import Event as Signal, Thread
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from channels_dvr.browser import library_rows, recording_row, scheduled_rows
from channels_dvr.guide import GuideController, Selection, time_label
from channels_dvr.models import Channel, ChannelKey, Event, Recording
from channels_dvr.repository import MockRepository
from channels_dvr.requests import LatestRequest


def repository(count=10, events=(), now=3600):
    repo = MockRepository()
    # Repeated display numbers and backend IDs across sources and namespaces.
    repo.channels = tuple(Channel(ChannelKey(f"ns-{i // 2}", f"src-{i % 2}", "1"),
                                  "1", f"Channel {i}", ("Subset",) if i % 2 else ())
                          for i in range(count))
    repo.events = tuple(events)
    repo.now = now
    repo.horizon = (0, 86400 * 3)
    repo.groups = ("All", "Subset", "Empty")
    return repo


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.repo = repository()
        self.repo.events = tuple(Event("same-id", c.key, 1800, 7200, c.name)
                                 for c in self.repo.channels)
        self.guide = GuideController(self.repo)

    def test_selection_is_source_identity_not_row_or_control(self):
        first = self.guide.selection
        for index in range(1, 9):
            self.guide.vertical(1)
            self.assertEqual(self.guide.selection,
                             Selection(self.repo.channels[index].key, "same-id", first.anchor, 1800))
            self.assertEqual(self.guide.selected_event().title, f"Channel {index}")
        self.assertEqual(self.guide.top, 3)
        self.assertEqual(sum(c.selected for c in self.guide.cells()), 1)
        for _ in range(20):
            self.guide.vertical(-1)
        self.assertEqual(self.guide.selection, first)
        self.assertEqual(self.guide.top, 0)

    def test_explicit_search_focus_reveals_exact_source_and_rejects_removed_or_filtered(self):
        target = next(e for e in self.repo.events if e.channel == self.repo.channels[8].key)
        self.assertTrue(self.guide.focus_event(target))
        self.assertEqual(self.guide.selected_event(), target)
        self.assertEqual(self.guide.top, 3)
        before = (self.guide.selection, self.guide.top, self.guide.viewport)
        self.assertFalse(self.guide.focus_event(replace(target, id="removed")))
        self.assertEqual((self.guide.selection, self.guide.top, self.guide.viewport), before)
        self.guide.set_group("Subset")
        before = (self.guide.selection, self.guide.top, self.guide.viewport)
        self.assertFalse(self.guide.focus_event(target))
        self.assertEqual((self.guide.selection, self.guide.top, self.guide.viewport), before)

    def test_explicit_search_focus_does_not_resolve_overlap_or_repeated_id_by_time(self):
        key = self.repo.channels[0].key
        first = Event("repeat", key, 0, 1000, "Earlier repeat")
        sibling = Event("z", key, 0, 500, "Overlap sibling")
        later = Event("repeat", key, 500, 1000, "Later repeat")
        self.repo.events = (first, sibling, later)
        self.assertTrue(self.guide.focus_event(later))
        self.assertEqual(self.guide.selected_event(), later)
        self.assertTrue(self.guide.focus_event(first))
        self.assertEqual(self.guide.selected_event(), first)
        self.assertEqual(self.guide.selection.event_start, 0)
        self.repo.events += (replace(first, id="outside", start=self.repo.horizon[1],
                                     end=self.repo.horizon[1] + 100),)
        before = self.guide.selection
        self.assertFalse(self.guide.focus_event(self.repo.events[-1]))
        self.assertEqual(self.guide.selection, before)

    def test_refresh_preserves_exact_source_airing_and_anchor_then_rebinds_removed_source(self):
        target = next(e for e in self.repo.events if e.channel == self.repo.channels[8].key)
        self.guide.focus_event(target)
        before = self.guide.selection
        self.repo.events = tuple(replace(e, description="fresh metadata") for e in self.repo.events)
        self.guide.refresh()
        self.assertEqual(self.guide.selection, before)
        self.assertEqual(self.guide.selected_event().description, "fresh metadata")
        self.repo.channels = self.repo.channels[:8]
        self.guide.refresh()
        self.assertNotEqual(self.guide.selection.channel, before.channel)
        self.assertEqual(self.guide.selection.anchor, before.anchor)
        self.assertIn(self.guide.selection.channel, {c.key for c in self.repo.channels})

    def test_refresh_horizon_and_empty_inventory_never_reuse_old_window(self):
        new_start = 86400
        self.repo.horizon = (new_start, new_start + self.guide.span)
        self.repo.channels = ()
        self.repo.events = ()
        self.guide.refresh(new_start + 100)
        self.assertEqual(self.guide.selection, Selection(None, None, new_start + 100))
        self.assertEqual(self.guide.viewport, new_start)
        self.assertFalse(self.guide.cells())
        with self.assertRaises(ValueError):
            self.guide.refresh(True)

    def test_vertical_keeps_anchor_across_different_event_starts_and_gaps(self):
        keys = [c.key for c in self.repo.channels]
        self.repo.events = (Event("a", keys[0], 1800, 7200, "A"),
                            Event("b", keys[1], 3500, 4000, "B"))
        self.guide.vertical(1)
        self.assertEqual(self.guide.selection, Selection(keys[1], "b", 3600, 3500))
        self.guide.vertical(1)
        self.assertEqual(self.guide.selection, Selection(keys[2], None, 3600))
        self.assertIsNone(self.guide.selected_event())
        self.guide.vertical(-1)
        self.guide.vertical(-1)
        self.assertEqual(self.guide.selection, Selection(keys[0], "a", 3600, 1800))

    def test_horizontal_tracks_airings_and_reveals_viewport_both_directions(self):
        key = self.repo.channels[0].key
        self.repo.events = tuple(Event(str(i), key, i * 10800, i * 10800 + 1800, str(i))
                                 for i in range(4))
        self.guide.jump(0)
        self.guide.horizontal(1)
        self.assertEqual(self.guide.selection, Selection(key, "1", 10800, 10800))
        self.assertLessEqual(self.guide.viewport, 10800)
        self.assertGreater(self.guide.viewport + self.guide.span, 10800)
        self.guide.horizontal(-1)
        self.assertEqual(self.guide.selection, Selection(key, "0", 0, 0))
        self.assertEqual(self.guide.viewport, 0)
        self.guide.jump(5000)
        self.assertIsNone(self.guide.selection.event_id)
        self.guide.horizontal(1)
        self.assertEqual(self.guide.selection.event_id, "1")
        self.guide.jump(5000)
        self.guide.horizontal(-1)
        self.assertEqual(self.guide.selection.event_id, "0")

    def test_horizon_is_half_open_and_empty_filter_stays_navigable(self):
        for value, expected in ((-999, 0), (10**9, self.repo.horizon[1] - 1)):
            self.guide.jump(value)
            self.assertEqual(self.guide.selection.anchor, expected)
            self.assertGreaterEqual(self.guide.viewport, self.repo.horizon[0])
            self.assertLessEqual(self.guide.viewport + self.guide.span, self.repo.horizon[1])
        anchor = self.guide.selection.anchor
        self.guide.set_group("Empty")
        self.assertEqual(self.guide.cells(), ())
        self.assertEqual(self.guide.selection, Selection(None, None, anchor))
        self.guide.vertical(1)
        self.guide.horizontal(-1)
        self.guide.jump(3600)
        self.assertEqual(self.guide.selection, Selection(None, None, 3600))
        self.guide.set_group("All")
        self.assertEqual(self.guide.selection.channel, self.repo.channels[0].key)
        with self.assertRaises(ValueError):
            self.guide.set_group("missing")

    def test_filter_retains_existing_identity_and_anchor(self):
        self.guide.vertical(1)
        before = self.guide.selection
        self.guide.set_group("Subset")
        self.assertEqual(self.guide.selection, before)
        self.assertEqual(self.guide.top, 0)

    def test_visible_rows_and_overscan_are_bounded_and_geometry_clipped(self):
        self.guide.top = 2
        for overscan, expected in ((-10, set(range(0, 6))), (0, set(range(0, 6))),
                                   (1, set(range(-1, 7))), (100, set(range(-1, 7)))):
            cells = self.guide.cells(overscan)
            self.assertEqual({c.row for c in cells}, expected)
            self.assertLessEqual(len(cells), self.guide.max_cells)
            for cell in cells:
                self.assertGreaterEqual(cell.x, 0)
                self.assertGreater(cell.width, 0)
                self.assertLessEqual(cell.x + cell.width, 1)

    def test_dense_schedule_caps_materialized_cells_and_resets_truncation(self):
        key = self.repo.channels[0].key
        self.repo.events = tuple(Event(str(i), key, 3600 + i, 3601 + i, "Tiny")
                                 for i in range(1000))
        cells = self.guide.cells()
        # Fair row/work reservations may deliberately leave capacity unused;
        # the cap is a ceiling, not permission for the first row to monopolize it.
        self.assertLessEqual(len(cells), self.guide.max_cells)
        self.assertEqual({c.row for c in cells if 0 <= c.row < 6}, set(range(6)))
        self.assertTrue(self.guide.truncated)
        self.repo.events = ()
        self.assertLess(len(self.guide.cells()), self.guide.max_cells)
        self.assertFalse(self.guide.truncated)

    def assert_no_lane_collisions(self, cells):
        for cell in cells:
            self.assertIn(cell.lane, (0, 1))
            self.assertGreater(cell.width, 0)
            self.assertGreaterEqual(cell.x, 0)
            self.assertLessEqual(cell.x + cell.width, 1)
        for row in {c.row for c in cells}:
            for lane in (0, 1):
                ordered = sorted((c for c in cells if c.row == row and c.lane == lane),
                                 key=lambda c: c.x)
                for previous, current in zip(ordered, ordered[1:]):
                    self.assertLessEqual(previous.x + previous.width, current.x + 1e-12)

    def test_repeated_id_airings_have_distinct_selection_and_details(self):
        key = self.repo.channels[0].key
        first = Event("repeat", key, 3600, 4200, "First airing")
        second = Event("repeat", key, 4800, 5400, "Second airing")
        self.repo.events = (second, first)
        self.guide.jump(4900)
        self.assertEqual(self.guide.selection, Selection(key, "repeat", 4900, 4800))
        self.assertEqual(self.guide.selected_event(), second)
        selected = [c for c in self.guide.cells() if c.selected]
        self.assertEqual([(c.event_id, c.start, c.title) for c in selected],
                         [("repeat", 4800, "Second airing")])
        self.guide.horizontal(-1)
        self.assertEqual(self.guide.selected_event(), first)
        self.guide.horizontal(1)
        self.assertEqual(self.guide.selected_event(), second)
        self.guide.horizontal(1)
        self.assertNotEqual(self.guide.selection.anchor, 4800)
        self.guide.jump(4900)
        before = self.guide.selection
        self.guide.set_group("All")
        self.assertEqual(self.guide.selection, before)

    def test_filter_retained_bottom_channel_remains_visible(self):
        for _ in range(9):
            self.guide.vertical(1)
        before = self.guide.selection
        self.guide.set_group("All")
        self.assertEqual(self.guide.selection, before)
        self.assertEqual(self.guide.top, 4)
        self.guide.set_group("Subset")
        self.assertEqual(self.guide.selection, before)
        selected = [c for c in self.guide.cells() if c.selected]
        self.assertEqual(len(selected), 1)
        self.assertTrue(0 <= selected[0].row < self.guide.rows)

    def test_lanes_reuse_free_lane_beneath_long_airing(self):
        key = self.repo.channels[0].key
        self.repo.events = (Event("a", key, 3600, 10000, "Long"),
                            Event("b", key, 4000, 4500, "Short"),
                            Event("c", key, 5000, 5500, "Later"))
        self.guide.jump(3600)
        cells = self.guide.cells(0)
        events = {c.event_id: c for c in cells if c.event_id is not None}
        self.assertEqual([events[k].lane for k in ("a", "b", "c")], [0, 1, 1])
        self.assertFalse(any(c.overflow for c in cells))
        self.assert_no_lane_collisions(cells)

    def test_three_plus_overlaps_aggregate_without_hiding_selected_airing(self):
        key = self.repo.channels[0].key
        self.repo.events = tuple(Event(str(i), key, 3600 + i * 100, 7000 + i * 100,
                                       f"Airing {i}") for i in range(5))
        for event in self.repo.events:
            self.guide.jump(event.start)
            cells = self.guide.cells(0)
            selected = [c for c in cells if c.selected]
            self.assertEqual([(c.event_id, c.start) for c in selected], [(event.id, event.start)])
            self.assertFalse(selected[0].overflow)
            self.assertEqual(self.guide.selected_event(), event)
            overflow = [c for c in cells if c.overflow]
            self.assertTrue(overflow)
            self.assertTrue(all(c.lane == 1 and 'More overlapping listings' in c.title for c in overflow))
            self.assert_no_lane_collisions(cells)

    def test_dense_overscan_cannot_starve_visible_rows_or_late_selected_airing(self):
        keys = [c.key for c in self.repo.channels]
        self.repo.events = tuple(Event(f"{row}-{i}", key, 3600 + i, 3601 + i, "Tiny")
                                 for row, key in enumerate(keys) for i in range(3000))
        self.guide.vertical(1)
        self.guide.top = 1  # Dense channel 0 is now preceding overscan.
        self.guide.jump(6100)
        # Keep the selected late airing within the same dense viewport prefix.
        self.guide.viewport = 3600
        cells = self.guide.cells()
        self.assertLessEqual(len(cells), 192)
        self.assertEqual({c.row for c in cells if 0 <= c.row < 6}, set(range(6)))
        selected = [c for c in cells if c.selected]
        self.assertEqual([(c.event_id, c.start, c.row) for c in selected],
                         [("1-2500", 6100, 0)])
        self.assertTrue(self.guide.truncated)
        self.assert_no_lane_collisions(cells)

    def test_dense_processing_is_bounded_even_when_overflow_reduces_output(self):
        key = self.repo.channels[0].key
        self.repo.events = tuple(Event(str(i), key, 3600 + i, 10000, "Overlap")
                                 for i in range(3000))
        self.guide.jump(6000)
        self.guide.viewport = 3600
        schedule = self.guide._schedule(key)
        original = type(schedule).overlapping
        visited = []

        def counted(instance, *args, **kwargs):
            for index in original(instance, *args, **kwargs):
                visited.append(index)
                yield index

        with patch.object(type(schedule), "overlapping", counted):
            cells = self.guide.cells()
        self.assertLessEqual(len(visited), self.guide.max_cells + 8)
        self.assertLessEqual(len(cells), self.guide.max_cells)
        self.assertTrue(any(c.selected and c.start == 6000 for c in cells))
        self.assertTrue(self.guide.truncated)
        self.assert_no_lane_collisions(cells)

    def test_exact_cell_cap_does_not_imply_truncation_without_omission(self):
        repo = repository(count=1, now=0)
        key = repo.channels[0].key
        # Consecutive cells exactly fill both the viewport and the hard cap.
        repo.events = tuple(Event(str(i), key, i * 37,
                                 (i + 1) * 37 if i < 191 else 7200, "Tiny")
                            for i in range(192))
        guide = GuideController(repo)
        cells = guide.cells()
        self.assertEqual(len(cells), 192)
        self.assertFalse(guide.truncated)
        self.assert_no_lane_collisions(cells)
        repo.events += (Event("extra", key, 7199, 7200, "Extra"),)
        cells = guide.cells()
        self.assertEqual(len(cells), 192)
        self.assertTrue(guide.truncated)
        self.assert_no_lane_collisions(cells)

    def test_snapshot_index_caches_extents_and_rebuilds_on_tuple_replacement(self):
        class OnceIterable(tuple):
            iterations = 0

            def __iter__(self):
                self.iterations += 1
                if self.iterations > 1:
                    raise AssertionError("Snapshot scanned more than once")
                return super().__iter__()

        key = self.repo.channels[0].key
        events = tuple(Event(str(i), key, 3600 + i * 10, None, "Unknown") for i in range(1000))
        self.repo.events = OnceIterable(reversed(events))
        with patch.object(self.repo, "events_for", side_effect=AssertionError("Horizon scan")):
            schedule = self.guide._schedule(key)
            cached_ends = schedule.ends
            for _ in range(3):
                for i, event in enumerate(events):
                    self.assertEqual(self.guide.extent(event),
                                     event.start + (10 if i < 999 else 1800))
                self.guide.cells()
                self.assertIs(self.guide._schedule(key), schedule)
                self.assertIs(schedule.ends, cached_ends)
        self.assertEqual(self.repo.events.iterations, 1)
        self.repo.events = (events[0], replace(events[1], start=3605))
        self.assertEqual(self.guide.extent(events[0]), 3605)
        self.assertIsNot(self.guide._schedule(key), schedule)

    def test_index_preserves_long_overlap_without_scanning_expired_short_events(self):
        class CountedTuple(tuple):
            reads = 0

            def __getitem__(self, index):
                self.reads += 1
                return super().__getitem__(index)

        key = self.repo.channels[0].key
        long = Event("long", key, 0, 200000, "Long")
        self.repo.events = (long,) + tuple(Event(str(i), key, i, i + 1, "Ended")
                                           for i in range(1, 20000))
        self.guide.jump(100000)
        self.assertEqual(self.guide.selected_event(), long)
        schedule = self.guide._schedule(key)
        schedule.tree = CountedTuple(schedule.tree)
        cells = self.guide.cells(0)
        self.assertEqual([c.event_id for c in cells if c.event_id is not None], ["long"])
        self.assertLess(schedule.tree.reads, 100)
        self.assertFalse(self.guide.truncated)

    def test_gap_overlap_unknown_extent_and_continuation_markers(self):
        key = self.repo.channels[0].key
        self.repo.events = (
            Event("long", key, 3000, 13000, "Long"),
            Event("overlap", key, 4000, 4500, "Overlap"),
            Event("unknown", key, 14000, None, "Unknown"),
            Event("next", key, 14500, 15000, "Next"),
        )
        self.guide.jump(4200)
        self.assertEqual(self.guide.selection.event_id, "overlap")
        cells = {c.event_id: c for c in self.guide.cells(0) if c.channel == key}
        self.assertEqual({"long", "overlap"}, {k for k in cells if k is not None})
        self.assertNotEqual(cells["long"].lane, cells["overlap"].lane)
        self.assertTrue(cells["long"].continues_left)
        self.assertTrue(cells["long"].continues_right)
        unknown = self.repo.events[2]
        self.assertEqual(self.guide.extent(unknown), 14500)
        self.guide.jump(14000)
        cell = next(c for c in self.guide.cells() if c.event_id == "unknown")
        self.assertIn("[?]", cell.title)
        self.guide.jump(13500)
        selected = [c for c in self.guide.cells() if c.selected]
        self.assertEqual(len(selected), 1)
        self.assertIsNone(selected[0].event_id)
        self.assertEqual(selected[0].title, "No listing")
        self.assertEqual((selected[0].start, selected[0].end), (13000, 14000))

    def test_unknown_end_without_successor_is_provisional_not_mutated(self):
        event = Event("sparse", self.repo.channels[0].key, 3600, None, "")
        self.repo.events = (event,)
        self.assertEqual(self.guide.extent(event), 5400)
        self.assertIsNone(event.end)
        self.assertTrue(any(c.title == " [?]" for c in self.guide.cells()))
        self.guide.jump(5400)
        self.assertIsNone(self.guide.selected_event())
        self.assertNotIn("sparse", [c.event_id for c in self.guide.cells()])

    def test_half_open_event_boundaries(self):
        key = self.repo.channels[0].key
        self.repo.events = (Event("a", key, 0, 3600, "A"),
                            Event("b", key, 3600, 7200, "B"))
        self.guide.jump(3600)
        self.assertEqual(self.guide.selection.event_id, "b")
        self.assertNotIn("a", [c.event_id for c in self.guide.cells()])

    def test_dst_fold_spring_forward_and_local_midnight(self):
        zone = ZoneInfo("America/Los_Angeles")
        def stamp(year, month, day, hour, minute=0):
            return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp())
        self.assertEqual(time_label(stamp(2026, 11, 1, 8, 30), zone),
                         "Sun 01 Nov 01:30 -0700")
        self.assertEqual(time_label(stamp(2026, 11, 1, 9, 30), zone),
                         "Sun 01 Nov 01:30 -0800")
        self.assertIn("01:30 -0800", time_label(stamp(2026, 3, 8, 9, 30), zone))
        self.assertIn("03:30 -0700", time_label(stamp(2026, 3, 8, 10, 30), zone))
        self.assertEqual(time_label(stamp(2026, 11, 1, 6, 59), zone),
                         "Sat 31 Oct 23:59 -0700")
        self.assertEqual(time_label(stamp(2026, 11, 1, 7), zone),
                         "Sun 01 Nov 00:00 -0700")


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.repo = MockRepository()

    def test_shows_group_by_season_and_movies_have_children(self):
        rows = library_rows(self.repo, "Shows")
        self.assertEqual([r.label for r in rows], [
            'Unclassified sample | Season ? | Unknown | Library source: Unknown',
            'Synthetic Atlas | Season 1 | DVR | Library source: synthetic',
            'Synthetic Atlas | Season 2 | DVR | Library source: synthetic',
            'Synthetic Workshop | Season ? | DVR | Library source: synthetic',
            'Synthetic Atlas | Season 1 | Imported | Library source: synthetic',
            'Movies | DVR | Library source: synthetic',
            'Movies | Imported | Library source: synthetic',
        ])
        self.assertEqual([r.id for r in rows[1].children], [
            repr(("synthetic", "dvr", "series-2")), repr(("synthetic", "dvr", "series-1"))])
        self.assertEqual([r.id for r in rows[-2].children], [
            repr(("synthetic", "dvr", "movie-1")), repr(("synthetic", "dvr", "movie-2"))])
        self.assertEqual({r.id for group in rows for r in group.children},
                         {repr((r.namespace, r.provenance, r.id)) for r in self.repo.recordings})
        self.assertEqual(sum(len(r.children) for r in rows), 9)
        self.assertEqual(len({r.id for r in rows}), 7)
        self.assertEqual([r.id for r in rows],
                         [r.id for r in library_rows(self.repo, "Shows")])

    def test_recent_tie_break_and_movies_are_leaf_rows(self):
        recent = library_rows(self.repo, "Recent")
        self.assertEqual([r.id for r in recent], [repr((r.namespace, r.provenance, r.id))
                         for r in sorted(self.repo.recordings, key=lambda r: (
                             -r.created, (r.namespace, r.provenance, r.id)))])
        self.assertEqual(len(recent), 9)
        self.assertEqual(len({r.id for r in recent}), 9)
        self.repo.recordings = (replace(self.repo.recordings[0], id="z", created=1),
                                replace(self.repo.recordings[0], id="a", created=1))
        self.assertEqual([r.id for r in library_rows(self.repo, "Recent")], [
            repr(("synthetic", "dvr", "a")), repr(("synthetic", "dvr", "z"))])
        self.repo = MockRepository()
        movies = library_rows(self.repo, "Movies")
        self.assertEqual([r.id for r in movies], [
            repr(("synthetic", "dvr", "movie-1")), repr(("synthetic", "dvr", "movie-2")),
            repr(("synthetic", "import", "movie-1"))])
        self.assertIn("Imported", movies[-1].label)
        self.assertIn("DVR", movies[0].label)
        self.assertTrue(all(not r.children and "Movie" in r.label for r in movies))

    def test_watched_resume_and_sparse_metadata_are_independent(self):
        record = Recording("sparse", "Only title", None, None, None, False, 0, None)
        row = recording_row(record)
        self.assertIn("S? E?", row.label)
        self.assertIn("Duration: Unknown", row.detail)
        self.assertIn("Only title", row.detail)
        self.assertIn("Unwatched", row.label)
        self.assertIn('Unknown | Library source: Unknown', row.label)
        self.assertEqual(row.id, repr(("", "unknown", "sparse")))
        for watched in (True, False):
            row = recording_row(replace(record, watched=watched, resume=7200, duration=7200))
            self.assertIn("Watched" if watched else "Unwatched", row.label)
            self.assertIn('Resume at 7200s', row.label)
        self.assertNotIn("Resume", recording_row(replace(record, watched=True)).label)

    def test_jobs_present_ambiguity_candidates_and_padding_without_actions(self):
        rows = scheduled_rows(self.repo)
        self.assertEqual([r.label.rsplit(" | ", 1)[1] for r in rows],
                         ['Matching listing', 'More than one possible listing', 'More than one possible listing', 'No confirmed listing match'])
        self.assertIn('Source 1 / 1 Demo News, Source 2 / 1 Demo Travel', rows[1].detail)
        self.assertIn('Extra time before/after: 120/300s', rows[0].detail)
        self.assertIn(time_label(self.repo.jobs[0].start - 120), rows[0].detail)
        self.assertIn('not verified DVR associations', rows[1].detail)
        self.assertTrue(all(not r.children for r in rows))
        self.assertTrue(all('This list is read only' in r.detail for r in rows))

    def test_known_padding_retains_exact_legacy_projection(self):
        job = self.repo.jobs[0]
        row = scheduled_rows(self.repo)[0]
        channels = 'Source 1 / 1 Demo News'
        self.assertEqual(row.detail,
                         f"{time_label(job.start)} — {time_label(job.end)}\n"
                         f"Extra time before/after: {job.padding_before}/{job.padding_after}s\n"
                         f"Recording time including extra time: {time_label(job.start - job.padding_before)}"
                         f" — {time_label(job.end + job.padding_after)}\n"
                         f"Possible listing sources/channels: {channels}\n"
                         'Matching listing: Matching listing\n'
                         'Listing matches are not verified DVR associations.\n'
                         "This list is read only. Use Recording status and cancel for a recording verified as this add-on's.")

    def test_unknown_padding_never_readds_or_claims_zero_padding(self):
        for before, after in ((0, 0), (120, 300)):
            job = replace(self.repo.jobs[0], padding_known=False,
                          padding_before=before, padding_after=after)
            self.repo.jobs = (job,)
            with patch("channels_dvr.browser.time_label", side_effect=time_label) as label:
                row, = scheduled_rows(self.repo)
            self.assertEqual([call.args for call in label.call_args_list],
                             [(job.start,), (job.end,)])
            self.assertTrue(row.detail.startswith(
                f"{time_label(job.start)} — {time_label(job.end)}\n"))
            self.assertIn('Extra time is unknown. The scheduled time shown is unchanged; no extra time was added again.\n',
                          row.detail)
            self.assertNotIn('Recording time including extra time:', row.detail)
            self.assertNotIn('Extra time before/after:', row.detail)
            self.assertEqual((job.start, job.end),
                             (MockRepository().jobs[0].start, MockRepository().jobs[0].end))

    def test_empty_candidates_disclose_unresolved_identity(self):
        for known in (False, True):
            self.repo.jobs = (replace(self.repo.jobs[0], candidates=(), padding_known=known),)
            row, = scheduled_rows(self.repo)
            self.assertTrue(row.label.endswith(' | No confirmed listing match'))
            self.assertIn('\nSource and channel could not be confirmed\n', row.detail)
            self.assertNotIn('Possible listing sources/channels:', row.detail)
            self.assertNotIn("no scheduled channels", row.detail.casefold())

    def test_empty_browser_results(self):
        self.repo.recordings = self.repo.jobs = ()
        for mode in ("Shows", "Recent", "Movies"):
            self.assertEqual(library_rows(self.repo, mode), ())
        self.assertEqual(scheduled_rows(self.repo), ())


class MailboxTests(unittest.TestCase):
    def test_new_generation_discards_pending_and_rejects_stale_results(self):
        mailbox = LatestRequest()
        old = mailbox.begin()
        self.assertTrue(mailbox.publish(old, ("old",)))
        current = mailbox.begin()
        self.assertGreater(current, old)
        self.assertIsNone(mailbox.drain())
        self.assertFalse(mailbox.publish(old, ("late",)))
        self.assertTrue(mailbox.publish(current, ("fresh",)))
        self.assertEqual(mailbox.drain(), ("fresh",))
        self.assertIsNone(mailbox.drain())

    def test_mailbox_is_single_slot_latest_value_under_publish_flood(self):
        mailbox = LatestRequest()
        generation = mailbox.begin()
        for value in range(10000):
            self.assertTrue(mailbox.publish(generation, value))
        self.assertEqual(mailbox._queue.maxsize, 1)
        self.assertEqual(mailbox._queue.qsize(), 1)
        self.assertEqual(mailbox.drain(), 9999)
        self.assertIsNone(mailbox.drain())

    def test_delayed_worker_cannot_replace_new_generation_result(self):
        mailbox = LatestRequest()
        old = mailbox.begin()
        ready, release = Signal(), Signal()
        outcomes = []
        def worker():
            ready.set()
            if release.wait(5):
                outcomes.append(mailbox.publish(old, ("stale",)))
        thread = Thread(target=worker, daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(5))
            current = mailbox.begin()
            self.assertTrue(mailbox.publish(current, ("current",)))
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, [False])
        self.assertEqual(mailbox.drain(), ("current",))


if __name__ == "__main__":
    unittest.main()
