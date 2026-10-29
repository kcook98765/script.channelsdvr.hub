"""Synthetic pure tests. Run with resources/lib on PYTHONPATH."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
import unittest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from channels_dvr.cache import SnapshotCache
from channels_dvr.models import (
    Channel, ChannelKey, Event, Job, MatchStatus, Recording, RecordingGroup,
)
from channels_dvr.normalization import (
    DataResult, DataState, event_timestamp, job_timestamp, normalize_channels,
    normalize_events, normalize_jobs, normalize_recordings, recording_timestamp,
    timestamp_milliseconds, timestamp_seconds, unavailable,
)
from channels_dvr.paging import Page, Query, ScanResult, collect_pages
from channels_dvr.repository import MockRepository, job_match, recording_groups


class ModelTests(unittest.TestCase):
    def test_exact_fields_and_frozen_records(self):
        expected = {
            ChannelKey: "namespace source_id channel_id",
            Channel: "key number name groups",
            Event: "id channel start end title description",
            Recording: ("id title show season episode is_movie created duration watched resume "
                        "namespace provenance show_id watched_known resume_known"),
            RecordingGroup: "show season is_movie recordings namespace provenance show_id",
            Job: "id title start end candidates event_ids padding_before padding_after padding_known",
        }
        for model, names in expected.items():
            self.assertEqual([f.name for f in fields(model)], names.split())
        key = ChannelKey("demo", "source", "1")
        objects = (key, Channel(key, "1", "Demo"), Event("e", key, 0, None, "E"),
                   Recording("r", "R", None, None, None, False, 0, None),
                   Job("j", "J", 0, 1, (key,)))
        for obj in objects:
            with self.assertRaises(FrozenInstanceError):
                setattr(obj, fields(obj)[0].name, "changed")
        self.assertEqual(objects[1].groups, ())
        self.assertEqual(objects[2].description, "")
        self.assertEqual((objects[3].watched, objects[3].resume), (False, 0))
        self.assertEqual((objects[4].event_ids, objects[4].padding_before,
                          objects[4].padding_after), ((), 0, 0))
        self.assertIs(objects[3].watched_known, True)
        self.assertIs(objects[3].resume_known, True)
        self.assertIs(objects[4].padding_known, True)

    def test_recording_origin_defaults_preserve_positional_constructors(self):
        recording = Recording("r", "R", None, None, None, True, 123, None, True, 120)
        group = RecordingGroup(None, None, True, (recording,))
        for obj in (recording, group):
            self.assertEqual((obj.namespace, obj.provenance, obj.show_id),
                             ("", "unknown", None))
            with self.assertRaises(FrozenInstanceError):
                obj.provenance = "dvr"
        self.assertEqual((recording.watched, recording.resume), (True, 120))

    def test_known_flags_append_after_legacy_positional_fields(self):
        recording = Recording("r", "R", None, None, None, False, 123, None,
                              True, 120, "synthetic", "unknown", "show", False, False)
        job = Job("j", "J", 100, 200, (), ("e",), 60, 90, False)
        self.assertEqual((recording.show_id, recording.watched, recording.resume),
                         ("show", True, 120))
        self.assertIs(recording.watched_known, False)
        self.assertIs(recording.resume_known, False)
        self.assertEqual((job.padding_before, job.padding_after), (60, 90))
        self.assertIs(job.padding_known, False)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = MockRepository()

    def test_deterministic_horizon_and_source_identity(self):
        other = MockRepository()
        for name in ("now", "horizon", "groups", "channels", "events", "recordings", "jobs"):
            self.assertEqual(getattr(self.repo, name), getattr(other, name))
        self.assertEqual(datetime.fromtimestamp(self.repo.now, timezone.utc).isoformat(),
                         "2026-11-01T08:30:00+00:00")
        start, end = self.repo.horizon
        self.assertEqual(end - start, 3 * 86400)
        self.assertGreater(len(self.repo.channels), 8)
        first, duplicate = self.repo.channels[0], self.repo.channels[5]
        self.assertEqual((first.number, first.key.channel_id),
                         (duplicate.number, duplicate.key.channel_id))
        self.assertNotEqual(first.key, duplicate.key)
        for event in self.repo.events_for(first.key):
            self.assertEqual(event.channel, first.key)
        self.assertEqual(self.repo.events_for(replace(first.key, namespace="other")), ())
        self.assertEqual(self.repo.channels_for("All"), self.repo.channels)
        self.assertEqual(self.repo.channels_for("Empty"), ())
        self.assertEqual(self.repo.channels_for("unknown"), ())
        self.assertTrue(self.repo.channels_for("Favorites"))
        self.assertEqual({(event.start - start) // 86400 for event in self.repo.events},
                         {0, 1, 2})
        self.assertTrue(all(start <= e.start < end for e in self.repo.events))
        self.assertTrue(all(e.end is None or e.end <= end for e in self.repo.events))

    def test_gaps_overlaps_and_unknown_ends(self):
        events = self.repo.events_for(self.repo.channels[0].key)
        self.assertTrue(any(e.end is None for e in events))
        known = [e for e in events if e.end is not None]
        self.assertTrue(any(e.end - e.start <= 300 for e in known))
        self.assertTrue(any(e.end - e.start >= 18000 for e in known))
        self.assertTrue(any(a.end < b.start for a, b in zip(known, known[1:])))
        self.assertTrue(any(a.start < b.start < a.end for a, b in zip(known, known[1:])))

    def test_dst_fold_is_two_distinct_instants(self):
        try:
            zone = ZoneInfo("America/Los_Angeles")
        except ZoneInfoNotFoundError:
            self.skipTest("system timezone database unavailable")
        first = datetime.fromtimestamp(self.repo.now, zone)
        second = datetime.fromtimestamp(self.repo.now + 3600, zone)
        self.assertEqual((first.hour, first.minute), (1, 30))
        self.assertEqual((second.hour, second.minute), (1, 30))
        self.assertEqual((first.fold, second.fold), (0, 1))
        self.assertNotEqual(first.utcoffset(), second.utcoffset())

    def test_recording_grouping_and_independent_completion(self):
        groups = recording_groups(self.repo.recordings)
        atlas = [g for g in groups if g.show_id == "atlas" and g.provenance == "dvr"]
        self.assertEqual([g.season for g in atlas], [1, 2])
        self.assertEqual([r.id for r in atlas[0].recordings], ["series-2", "series-1"])
        movies = next(g for g in groups if g.is_movie and g.provenance == "dvr")
        self.assertEqual([r.id for r in movies.recordings], ["movie-1", "movie-2"])
        self.assertFalse(movies.recordings[0].watched)
        self.assertEqual(movies.recordings[0].resume, movies.recordings[0].duration)
        self.assertTrue(any(r.watched and r.resume > 0 for r in self.repo.recordings))
        self.assertTrue(any(r.duration is None for r in self.repo.recordings))
        self.assertEqual(recording_groups(()), ())
        unknown = replace(self.repo.recordings[0], show=None, season=None)
        self.assertEqual(recording_groups((unknown,))[0].show, unknown.title)

    def test_original_recordings_and_appended_origin_fixtures(self):
        now = self.repo.now
        original = (
            Recording("series-1", "A first chapter", "Synthetic Atlas", 1, 1,
                      False, now - 7200, 1800, True, 0),
            Recording("series-2", "A second chapter", "Synthetic Atlas", 1, 2,
                      False, now - 3600, 1800, False, 900),
            Recording("series-3", "A new season", "Synthetic Atlas", 2, 1,
                      False, now - 1800, None),
            Recording("series-4", "An unnumbered story", "Synthetic Workshop",
                      None, None, False, now - 10000, 1200, True, 300),
            Recording("movie-1", "The Paper Planet", None, None, None, True,
                      now - 600, 7200, False, 7200),
            Recording("movie-2", "A Quiet Orbit", None, None, None, True,
                      now - 86400, None),
        )
        self.assertEqual(len(self.repo.recordings), 9)
        self.assertTrue(all(r.watched_known is True and r.resume_known is True
                            for r in self.repo.recordings))
        self.assertTrue(all(j.padding_known is True for j in self.repo.jobs))
        for actual, expected, show_id in zip(
                self.repo.recordings[:6], original,
                ("atlas", "atlas", "atlas", "workshop", None, None)):
            self.assertEqual(actual, replace(expected, namespace="synthetic",
                                             provenance="dvr", show_id=show_id))
        episode, movie, unknown = self.repo.recordings[6:]
        self.assertEqual(episode, Recording(
            "series-2", "Imported second chapter", "Synthetic Atlas", 1, 2,
            False, now - 3600, None, True, 120, "synthetic", "import", "atlas-import"))
        self.assertEqual(movie, Recording(
            "movie-1", "The Paper Planet (imported)", None, None, None, True,
            now - 600, 7200, False, 0, "synthetic", "import"))
        self.assertEqual(unknown, Recording(
            "unknown-1", "Unclassified sample", None, None, None, False,
            now - 1200, None))
        self.assertEqual(len({(r.namespace, r.provenance, r.id)
                              for r in self.repo.recordings}), 9)
        groups = recording_groups(reversed(self.repo.recordings))
        self.assertEqual(
            [(g.is_movie, g.namespace, g.provenance, g.show_id, g.season) for g in groups],
            [(False, "", "unknown", None, None),
             (False, "synthetic", "dvr", "atlas", 1),
             (False, "synthetic", "dvr", "atlas", 2),
             (False, "synthetic", "dvr", "workshop", None),
             (False, "synthetic", "import", "atlas-import", 1),
             (True, "synthetic", "dvr", None, None),
             (True, "synthetic", "import", None, None)])
        self.assertEqual(sum(len(g.recordings) for g in groups), 9)
        self.assertTrue(episode.watched)
        self.assertIsNone(episode.duration)
        self.assertEqual(episode.resume, 120)

    def test_group_identity_uses_namespace_origin_and_known_show_id(self):
        base = self.repo.recordings[0]
        variants = (
            base,
            replace(base, namespace="other"),
            replace(base, provenance="import"),
            replace(base, show_id="different-show"),
            replace(base, show_id=None),
            replace(base, title="New label", show="Renamed show", created=base.created + 1),
        )
        groups = recording_groups(variants)
        self.assertEqual(len(groups), 5)
        renamed = next(g for g in groups if len(g.recordings) == 2)
        self.assertEqual((renamed.show, renamed.show_id), ("Renamed show", "atlas"))
        self.assertEqual(renamed.recordings, (variants[-1], base))
        unknown = replace(base, show=None, show_id=None, season=None)
        fallback = recording_groups((unknown, replace(unknown, title="Other title")))
        self.assertEqual([g.show for g in fallback], ["A first chapter", "Other title"])
        movie = replace(base, is_movie=True, show=None, show_id=None, season=None)
        self.assertEqual(len(recording_groups((movie, replace(movie, namespace="other"),
                                              replace(movie, provenance="import")))), 3)

    def test_jobs_do_not_resolve_ambiguities_or_use_padding_as_identity(self):
        matches = [job_match(job, self.repo.events) for job in self.repo.jobs]
        self.assertEqual([m.status for m in matches], [MatchStatus.MATCHED,
                         MatchStatus.AMBIGUOUS, MatchStatus.AMBIGUOUS,
                         MatchStatus.UNRESOLVED])
        self.assertIsNone(matches[1].event)
        self.assertEqual(len(matches[1].candidates), 2)
        job, event = self.repo.jobs[0], matches[0].event
        self.assertGreater(job.padding_before + job.padding_after, 0)
        self.assertEqual(job_match(replace(job, event_ids=()), (event,)).event, event)
        self.assertEqual(job_match(replace(job, candidates=()), (event,)).status,
                         MatchStatus.UNRESOLVED)
        self.assertEqual(job_match(replace(job, event_ids=(event.id, "missing")),
                                   (event,)).status, MatchStatus.AMBIGUOUS)
        self.assertEqual(job_match(job, (event, event)).status, MatchStatus.MATCHED)
        self.assertEqual(job_match(job, (event, replace(event, title="conflict"))).status,
                         MatchStatus.AMBIGUOUS)


class NormalizationTests(unittest.TestCase):
    key = {"namespace": "demo", "source_id": "one", "channel_id": "1"}

    def test_explicit_timestamp_routes_and_no_heuristics(self):
        seconds = MockRepository().now
        self.assertEqual(event_timestamp(seconds), seconds)
        self.assertEqual(job_timestamp(seconds), seconds)
        self.assertEqual(recording_timestamp(seconds * 1000 + 999), seconds)
        self.assertEqual(timestamp_seconds(seconds * 1000), seconds * 1000)
        self.assertEqual(timestamp_milliseconds(-1), -1)
        for value in (None, True, "123", 123.0, float("nan")):
            for parser in (timestamp_seconds, timestamp_milliseconds):
                with self.assertRaises(ValueError):
                    parser(value)

    def test_route_fields_defaults_and_units(self):
        channel = normalize_channels([dict(key=self.key, number="01.2", name="Demo")])
        self.assertEqual(channel.items[0].number, "01.2")
        event = normalize_events([dict(id="e", channel=self.key, start=1, end=None,
                                       title="Demo")])
        self.assertIsNone(event.items[0].end)
        recording = normalize_recordings([dict(
            id="r", title="Demo", show=None, season=None, episode=None,
            is_movie=True, created=123456, duration=None, watched=False, resume=900)])
        self.assertEqual(recording.items[0].created, 123)
        self.assertEqual(recording.items[0].resume, 900)
        self.assertEqual((recording.items[0].namespace, recording.items[0].provenance,
                          recording.items[0].show_id), ("", "unknown", None))
        job = normalize_jobs([dict(id="j", title="Demo", start=1, end=2,
                                   candidates=[self.key], padding_before=60)])
        self.assertEqual(job.items[0].padding_before, 60)
        self.assertIs(recording.items[0].watched_known, True)
        self.assertIs(recording.items[0].resume_known, True)
        self.assertIs(job.items[0].padding_known, True)
        for result in (channel, event, recording, job):
            self.assertEqual(result.state, DataState.READY)
            self.assertTrue(result.complete)

    def test_known_flags_are_explicit_independent_booleans(self):
        row = dict(id="r", title="Demo", show=None, season=None, episode=None,
                   is_movie=False, created=123456, duration=None, watched=True,
                   resume=120, PlayedAt=999, Completed=True)
        for watched_known in (False, True):
            for resume_known in (False, True):
                result = normalize_recordings([dict(
                    row, watched_known=watched_known, resume_known=resume_known)])
                self.assertEqual(result.state, DataState.READY)
                recording = result.items[0]
                self.assertIs(recording.watched_known, watched_known)
                self.assertIs(recording.resume_known, resume_known)
                self.assertEqual((recording.watched, recording.resume, recording.duration,
                                  recording.provenance), (True, 120, None, "unknown"))
        sparse = dict(row)
        del sparse["watched"], sparse["resume"]
        recording = normalize_recordings([sparse]).items[0]
        self.assertEqual((recording.watched, recording.resume), (False, 0))
        self.assertIs(recording.watched_known, True)
        self.assertIs(recording.resume_known, True)
        job_row = dict(id="j", title="Demo", start=100, end=200, candidates=[],
                       padding_before=60, padding_after=90)
        for known in (False, True):
            result = normalize_jobs([dict(job_row, padding_known=known)])
            self.assertEqual(result.state, DataState.READY)
            job = result.items[0]
            self.assertIs(job.padding_known, known)
            self.assertEqual((job.start, job.end, job.padding_before, job.padding_after),
                             (100, 200, 60, 90))

    def test_known_flags_reject_nonbooleans_atomically(self):
        recording = dict(id="r", title="Demo", show=None, season=None, episode=None,
                         is_movie=False, created=123456, duration=None)
        job = dict(id="j", title="Demo", start=1, end=2, candidates=[])
        for parser, row, flags in (
                (normalize_recordings, recording, ("watched_known", "resume_known")),
                (normalize_jobs, job, ("padding_known",))):
            for flag in flags:
                for value in (None, 0, 1, "false", "true", [], {}, 0.0, 1.0):
                    with self.subTest(flag=flag, value=value):
                        result = parser([row, dict(row, **{flag: value})])
                        self.assertEqual(result.state, DataState.MALFORMED)
                        self.assertEqual(result.items, ())
                        self.assertFalse(result.complete)
                        self.assertEqual(result.detail, "expected boolean")

    def test_recording_classification_is_explicit_and_never_inferred(self):
        row = dict(id="r", title="Demo", show=None, season=None, episode=None,
                   is_movie=True, created=123456, duration=None, watched=True,
                   resume=120, path="/synthetic/imports/movie", completed=True)
        for provenance in ("dvr", "import", "unknown"):
            result = normalize_recordings([dict(row, namespace="synthetic",
                                               provenance=provenance, show_id="show")])
            self.assertEqual(result.state, DataState.READY)
            recording = result.items[0]
            self.assertEqual((recording.namespace, recording.provenance, recording.show_id),
                             ("synthetic", provenance, "show"))
            self.assertEqual((recording.created, recording.duration, recording.watched,
                              recording.resume), (123, None, True, 120))
        for changes in ({}, {"is_movie": False}, {"completed": False},
                        {"watched": False, "duration": 120}, {"resume": 0}):
            recording = normalize_recordings([dict(row, **changes)]).items[0]
            self.assertEqual((recording.namespace, recording.provenance, recording.show_id),
                             ("", "unknown", None))
        self.assertIsNone(normalize_recordings([dict(row, show_id=None)]).items[0].show_id)

    def test_recording_classification_rejects_wrong_types_and_unsupported_codes(self):
        row = dict(id="r", title="Demo", show=None, season=None, episode=None,
                   is_movie=False, created=123456, duration=None)
        invalid = {
            "namespace": (None, 1, False, [], {}),
            "provenance": (None, 1, False, [], {}, "", "DVR", "imported", "other"),
            "show_id": (1, False, [], {}),
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    result = normalize_recordings([row, dict(row, **{field: value})])
                    self.assertEqual(result.state, DataState.MALFORMED)
                    self.assertEqual(result.items, ())
                    self.assertTrue(result.detail)

    def test_empty_malformed_unsupported_and_outage_are_distinct(self):
        for parser in (normalize_channels, normalize_events, normalize_recordings,
                       normalize_jobs):
            self.assertEqual(parser([]).state, DataState.EMPTY)
            self.assertTrue(parser([]).complete)
            for payload in (None, {}, "", [None], [{}]):
                self.assertEqual(parser(payload).state, DataState.MALFORMED)
                self.assertFalse(parser(payload).complete)
        valid = dict(id="e", channel=self.key, start=1, end=2, title="Demo")
        for invalid in (dict(valid, end=1), dict(valid, start=True),
                        dict(valid, channel="1"), dict(valid, title=None)):
            result = normalize_events([valid, invalid])
            self.assertEqual(result.state, DataState.MALFORMED)
            self.assertEqual(result.items, ())
        for state in (DataState.UNSUPPORTED, DataState.OUTAGE):
            result = unavailable(state, "synthetic condition")
            self.assertEqual(result.state, state)
            self.assertFalse(result.complete)
        with self.assertRaises(ValueError):
            unavailable(DataState.EMPTY)


class PagingTests(unittest.TestCase):
    def setUp(self):
        self.query = Query("events", (("group", "All"),), ("start", "id"), 2)

    def scan(self, pages, **bounds):
        return collect_pages(self.query, pages, lambda item: item[0], **bounds)

    def test_dedup_first_observation_without_snapshot_assumption(self):
        pages = [Page(self.query, None, (("a", "old"), ("b", "b")), "next"),
                 Page(self.query, "next", (("a", "changed"), ("c", "c")), None)]
        result = self.scan(pages)
        self.assertTrue(result.complete)
        self.assertEqual(result.items, (("a", "old"), ("b", "b"), ("c", "c")))
        self.assertEqual(result.pages, 2)
        repo = MockRepository()
        channels = (repo.channels[0], repo.channels[5])
        result = collect_pages(self.query, [Page(self.query, None, channels, None)],
                               lambda c: c.key)
        self.assertEqual(result.items, channels)

    def test_explicit_empty_and_failure_states(self):
        self.assertEqual(self.scan([Page(self.query, None, (), None)]).state,
                         DataState.EMPTY)
        self.assertFalse(self.scan([]).complete)
        for state in (DataState.OUTAGE, DataState.UNSUPPORTED, DataState.MALFORMED):
            pages = [Page(self.query, None, (("a", 1),), "next"),
                     Page(self.query, "next", (), None, state, "synthetic failure")]
            result = self.scan(pages)
            self.assertFalse(result.complete)
            self.assertEqual(result.items, (("a", 1),))
            self.assertEqual(result.state, state)

    def test_query_shape_and_cursor_contracts(self):
        pages = [Page(replace(self.query, filters=(("group", "Other"),)), None, (), None),
                 Page(self.query, "wrong", (), None),
                 Page(self.query, None, (("a", 1),) * 3, None),
                 Page(self.query, None, (), ""),
                 Page(self.query, None, (("a", 1),), None, DataState.EMPTY)]
        for page in pages:
            self.assertEqual(self.scan([page]).state, DataState.MALFORMED)
        for kwargs in ({"resource": ""}, {"page_size": 0}, {"page_size": True},
                       {"order": ()}, {"filters": (("a", "b"), ("a", "c"))}):
            with self.assertRaises(ValueError):
                Query(**dict({"resource": "events"}, **kwargs))

    def test_nonprogress_and_bounds_do_not_overconsume(self):
        first = Page(self.query, None, (("a", 1),), "next")
        for second in (Page(self.query, "next", (("b", 1),), "next"),
                       Page(self.query, "next", (("a", 2),), "another"),
                       Page(self.query, "next", (), "another")):
            result = self.scan([first, second])
            self.assertFalse(result.complete)
            self.assertIn("nonprogress", result.detail)
        def bounded_input():
            yield first
            self.fail("collector exceeded max_pages")
        result = self.scan(bounded_input(), max_pages=1)
        self.assertEqual(result.state, DataState.PARTIAL)
        self.assertEqual(result.pages, 1)
        result = self.scan([Page(self.query, None, (("a", 1), ("b", 2)), None)],
                           max_items=1)
        self.assertEqual(result.items, (("a", 1),))
        self.assertFalse(result.complete)
        self.assertTrue(self.scan([Page(self.query, None, (("a", 1),), None)],
                                  max_items=1).complete)
        with self.assertRaises(ValueError):
            self.scan([], max_pages=0)


class CacheTests(unittest.TestCase):
    def test_stale_fallback_preserves_snapshot_and_original_age(self):
        cache, query = SnapshotCache(), Query("recordings")
        self.assertIsNone(cache.get(query, now=0))
        cache.update(query, DataResult(("old",), DataState.READY), now=10, ttl=20)
        self.assertFalse(cache.get(query, now=29).stale)
        self.assertTrue(cache.get(query, now=30).stale)
        for state in (DataState.OUTAGE, DataState.UNSUPPORTED, DataState.MALFORMED,
                      DataState.PARTIAL):
            view = cache.update(query, ScanResult(("new partial",), state, False, 1),
                                now=31, ttl=1000)
            self.assertEqual(view.items, ("old",))
            self.assertEqual(view.state, state)
            self.assertTrue(view.stale)
            self.assertEqual(view.updated_at, 10)
            self.assertEqual(cache.get(query, now=32), view)
        view = cache.update(query, DataResult((), DataState.EMPTY), now=40, ttl=20)
        self.assertEqual(view.items, ())
        self.assertEqual(view.state, DataState.EMPTY)
        self.assertFalse(view.stale)
        self.assertEqual(view.updated_at, 40)

    def test_query_isolation_and_no_fabricated_empty_on_outage(self):
        cache, query = SnapshotCache(), Query("events")
        failure = cache.update(query, unavailable(DataState.OUTAGE), now=0, ttl=10)
        self.assertEqual(failure.state, DataState.OUTAGE)
        self.assertIsNone(failure.updated_at)
        self.assertTrue(failure.stale)
        other = replace(query, filters=(("channel", "one"),))
        self.assertIsNone(cache.get(other, now=0))
        cache.update(query, DataResult(("good",), DataState.READY), now=1, ttl=10)
        # An inconsistent or incomplete result must not erase the saved snapshot.
        for result in (DataResult((), DataState.READY),
                       ScanResult((), DataState.EMPTY, False, 1)):
            view = cache.update(query, result, now=2, ttl=100)
            self.assertEqual(view.items, ("good",))
            self.assertTrue(view.stale)
        with self.assertRaises(ValueError):
            cache.update(query, DataResult((), DataState.EMPTY), now=2, ttl=-1)


if __name__ == "__main__":
    unittest.main()
