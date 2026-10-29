"""Pure offline session checks; resources/lib must be on PYTHONPATH."""

from dataclasses import FrozenInstanceError, fields
import unittest
from unittest.mock import patch

from channels_dvr.cache import SnapshotCache
from channels_dvr.models import Event, Job, Recording
from channels_dvr.normalization import DataResult, DataState
from channels_dvr.repository import MockRepository
from channels_dvr.session import OfflineDataSession, Presentation


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.repo = MockRepository()
        self.session = OfflineDataSession(self.repo)
        self.resources = (self.session.guide, self.session.recordings, self.session.jobs)

    def test_exact_frozen_presentation_and_scenarios(self):
        self.assertEqual([f.name for f in fields(Presentation)],
                         "items state loading stale updated_at age_seconds banner message can_browse".split())
        self.assertEqual(OfflineDataSession.SCENARIOS, (
            ('healthy', 'Recover demo data'), ('loading', 'Demo: keep loading'),
            ('outage', 'Demo: connection unavailable'), ('malformed', 'Demo: invalid response'),
            ('unsupported', 'Demo: unsupported response'), ('partial', 'Demo: incomplete update'),
            ('expired', 'Demo: old saved listings'), ('empty', 'Demo: successful empty result'),
            ('cold_outage', 'Demo: clear saved listings and simulate disconnection')))
        with self.assertRaises(FrozenInstanceError):
            self.session.guide().loading = True
        with self.assertRaises(AttributeError):
            self.session.scenario = "outage"

    def test_healthy_resources_are_typed_and_isolated(self):
        expected = ((self.repo.events, Event), (self.repo.recordings, Recording),
                    (self.repo.jobs, Job))
        for resource, (items, model) in zip(self.resources, expected):
            view = resource()
            self.assertEqual(view.items, items)
            self.assertTrue(all(isinstance(item, model) for item in view.items))
            self.assertEqual(view.state, DataState.READY)
            self.assertEqual((view.updated_at, view.age_seconds), (self.repo.now, 0))
            self.assertTrue(view.can_browse)
            self.assertFalse(view.stale or view.loading)

    def test_healthy_render_is_memoized_without_timestamp_refresh(self):
        cache = self.session._guide_cache
        with patch.object(cache, "update", wraps=cache.update) as update:
            first = self.session.guide()
            self.assertEqual(self.session.guide(), first)
            self.assertEqual(update.call_count, 1)
            self.session.choose("healthy")
            self.assertEqual(self.session.guide().updated_at, self.repo.now + 60)
            self.session.guide()
            self.assertEqual(update.call_count, 2)

    def test_failures_preserve_all_cached_items_timestamp_and_age(self):
        originals = [resource() for resource in self.resources]
        for index, code in enumerate(("outage", "malformed", "unsupported", "partial"), 1):
            self.session.choose(code)
            for resource, original in zip(self.resources, originals):
                view = resource()
                self.assertEqual(view.state, DataState(code))
                self.assertEqual(view.items, original.items)
                self.assertEqual(view.updated_at, original.updated_at)
                self.assertEqual(view.age_seconds, index * 60)
                self.assertTrue(view.stale and view.can_browse)
                self.assertIn('Previous update; may be out of date', view.banner)

    def test_cold_failures_never_seed_or_fabricate_empty(self):
        for code in ("outage", "malformed", "unsupported", "partial"):
            session = OfflineDataSession(self.repo)
            session.choose(code)
            for resource in (session.guide, session.recordings, session.jobs):
                view = resource()
                self.assertEqual(view.state, DataState(code))
                self.assertEqual(view.items, ())
                self.assertIsNone(view.updated_at)
                self.assertIsNone(view.age_seconds)
                self.assertTrue(view.stale)
                self.assertFalse(view.can_browse)
                self.assertIn('No saved listings', view.banner)

    def test_empty_replaces_only_accessed_cache_and_is_not_failure(self):
        original = [resource() for resource in self.resources]
        self.session.choose("empty")
        empty = self.session.recordings()
        self.assertEqual((empty.items, empty.state), ((), DataState.EMPTY))
        self.assertEqual(empty.updated_at, self.repo.now + 60)
        self.assertFalse(empty.stale or empty.can_browse)
        self.assertEqual(self.session._guide_cache.get(
            self.session._queries["events"], now=self.repo.now + 60).items, original[0].items)
        self.session.choose("outage")
        view = self.session.recordings()
        self.assertEqual((view.items, view.state), ((), DataState.OUTAGE))
        self.assertEqual((view.updated_at, view.age_seconds), (empty.updated_at, 60))
        self.assertIn('Previous update; may be out of date', view.banner)

    def test_warm_loading_does_not_update_and_requires_manual_recovery(self):
        originals = [resource() for resource in self.resources]
        self.session.choose("loading")
        with patch.object(SnapshotCache, "update", side_effect=AssertionError("write")):
            for resource, original in zip(self.resources, originals):
                view = resource()
                self.assertTrue(view.loading and view.can_browse)
                self.assertIsNone(view.state)
                self.assertEqual(view.items, original.items)
                self.assertEqual((view.updated_at, view.age_seconds), (original.updated_at, 60))
                self.assertIn('Recover demo data', view.message)
                self.assertEqual(resource(), view)
        self.assertEqual(self.session.scenario, "loading")

    def test_cold_loading_and_expiry_do_not_seed_empty_snapshots(self):
        for code in ("loading", "expired"):
            session = OfflineDataSession(self.repo)
            session.choose(code)
            with patch.object(SnapshotCache, "update", side_effect=AssertionError("write")):
                for resource in (session.guide, session.recordings, session.jobs):
                    view = resource()
                    self.assertEqual(view.items, ())
                    self.assertIsNone(view.state)
                    self.assertIsNone(view.updated_at)
                    self.assertFalse(view.can_browse)
                    self.assertIn('No saved listings', view.banner)

    def test_expired_uses_original_timestamp_and_explicit_old_age(self):
        originals = [resource() for resource in self.resources]
        self.session.choose("expired")
        with patch.object(SnapshotCache, "update", side_effect=AssertionError("write")):
            for resource, original in zip(self.resources, originals):
                view = resource()
                self.assertEqual(view.items, original.items)
                self.assertEqual(view.updated_at, original.updated_at)
                self.assertEqual(view.age_seconds, 361)
                self.assertTrue(view.stale and view.can_browse)
                self.assertIn('may be out of date', view.banner)
                self.assertIn('Updated 361s ago', view.banner)

    def test_recovery_uses_captured_fixtures_after_repository_rebinding(self):
        captured = (self.repo.events, self.repo.recordings, self.repo.jobs)
        self.repo.events = self.repo.recordings = self.repo.jobs = ()
        self.session.choose("empty")
        for resource in self.resources:
            self.assertEqual(resource().items, ())
        self.session.choose("healthy")
        for resource, items in zip(self.resources, captured):
            view = resource()
            self.assertEqual(view.items, items)
            self.assertFalse(view.stale)
            self.assertEqual(view.updated_at, self.repo.now + 120)

    def test_invalid_choice_is_atomic_including_clock_and_memoization(self):
        first = self.session.guide()
        with self.assertRaises(ValueError):
            self.session.choose("unknown")
        self.assertEqual(self.session.scenario, "healthy")
        self.assertEqual(self.session.guide(), first)
        self.session.choose("outage")
        self.assertEqual(self.session.guide().age_seconds, 60)

    def test_fixture_queries_are_distinct_and_full_horizon_not_viewport(self):
        other = OfflineDataSession(self.repo, fixture_id="other")
        for name, query in self.session._queries.items():
            self.assertNotEqual(query, other._queries[name])
            scope = dict(query.filters)
            self.assertEqual(scope["namespace"], "synthetic")
            self.assertEqual(scope["fixture_id"], "normal")
            self.assertEqual((int(scope["horizon_start"]), int(scope["horizon_end"])),
                             self.repo.horizon)
            self.assertEqual(scope["catalog"], "complete-all-channels")
            self.assertNotIn("group", scope)
        guide = self.session.guide()
        self.assertEqual({e.channel for e in guide.items}, {c.key for c in self.repo.channels})
        self.assertEqual({(e.start - self.repo.horizon[0]) // 86400 for e in guide.items},
                         {0, 1, 2})

    def test_partial_does_not_purge_or_accept_partial_items(self):
        for resource in self.resources:
            original = resource()
            self.assertGreater(len(original.items), 1)
        self.session.choose("partial")
        for resource, captured in zip(self.resources,
                                      (self.repo.events, self.repo.recordings, self.repo.jobs)):
            view = resource()
            self.assertEqual(view.items, captured)
            self.assertEqual(view.state, DataState.PARTIAL)
            self.assertIn('Incomplete sample update was not used', view.message)

    def test_cold_outage_clears_every_cache_then_recovery_restores(self):
        originals = [resource() for resource in self.resources]
        self.session.choose("cold_outage")
        for resource in self.resources:
            view = resource()
            self.assertEqual((view.items, view.state), ((), DataState.OUTAGE))
            self.assertIsNone(view.updated_at)
        self.session.choose("healthy")
        for resource, original in zip(self.resources, originals):
            self.assertEqual(resource().items, original.items)

    def test_failure_application_is_memoized_per_resource_and_revision(self):
        self.session.guide()
        self.session.choose("outage")
        with patch.object(SnapshotCache, "update", autospec=True,
                          side_effect=SnapshotCache.update) as update:
            for resource in self.resources:
                first = resource()
                self.assertEqual(resource(), first)
            self.assertEqual(update.call_count, 3)
            self.session.choose("outage")
            for resource in self.resources:
                resource()
            self.assertEqual(update.call_count, 6)

    def test_safe_messages_ignore_failure_detail_and_fixture_metadata(self):
        secret = "SECRET-token-private-address"
        session = OfflineDataSession(self.repo, fixture_id=secret)
        session.guide()
        session.choose("malformed")
        real_update = SnapshotCache.update

        def inject(cache, query, result, **kwargs):
            return real_update(cache, query, DataResult(result.items, result.state, secret),
                               **kwargs)

        with patch.object(SnapshotCache, "update", autospec=True, side_effect=inject):
            view = session.guide()
        self.assertEqual(view.state, DataState.MALFORMED)
        for text in (view.banner, view.message):
            self.assertNotIn(secret, text)
            self.assertIn('Demo', text)
            self.assertIn('Response could not be used', text)
            self.assertIn('Previous update; may be out of date', text)
            self.assertIn('Updated 60s ago', text)
        for code, _ in session.SCENARIOS:
            session.choose(code)
            view = session.guide()
            self.assertIn('Demo', view.banner)
            self.assertNotIn(secret, view.banner + view.message)


if __name__ == "__main__":
    unittest.main()
