"""Synthetic search contracts; standard library only and no Kodi imports."""

from dataclasses import FrozenInstanceError, replace
import unittest

from channels_dvr.library import LibraryBrowser
from channels_dvr.models import Channel, ChannelKey, Event, Job, Recording
from channels_dvr.search import ProgrammeSearchIndex, SearchPage, airing_key, normalize_query
from channels_dvr.search_projection import (MAX_LISTING_RECORDS, GuideScope, GuideSnapshot,
                                            Listing, ListingCoverage, PassEntry, PassId,
                                            ProgrammeId, RecordingId, ScheduledId, SearchCategory,
                                            project_search)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.channels = tuple(Channel(ChannelKey(ns, source, channel), "1", "Same")
                              for ns, source, channel in (
                                  ("a", "one", "1"), ("a", "two", "1"),
                                  ("b", "one", "1"), ("a", "one", "2")))
        self.key = self.channels[0].key

    def event(self, id="same", start=10, end=20, title="Match", description="", key=None):
        return Event(id, self.key if key is None else key, start, end, title, description)

    def index(self, events, **kwargs):
        return ProgrammeSearchIndex(events, self.channels, **kwargs)

    def test_unicode_normalization_and_original_length(self):
        self.assertEqual(normalize_query("  ＳＴＲＡＳＳＥ\tStraße\nCafe\u0301  "),
                         "strasse strasse café")
        self.assertEqual(normalize_query("ß" * 128), "ss" * 128)
        self.assertEqual(normalize_query("\u2003\t\n"), "")
        page = self.index([self.event(title="ＣＡＦÉ", description="Straße")]).search(
            "cafe\u0301 STRASSE")
        self.assertEqual(len(page.matches), 1)

    def test_query_rejects_nontext_and_oversize_before_normalization(self):
        for value in (None, 7, True, b"match", " " * 129, "x" * 129):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_query(value)
            with self.subTest(search=value), self.assertRaises(ValueError):
                self.index([]).search(value)

    def test_tokens_and_substrings_across_title_and_description(self):
        event = self.event(title="Cooking", description="Summer recipes")
        index = self.index([event, self.event(id="other", title="Cooking only")])
        self.assertEqual(index.search("SUM cook").matches, (event,))
        self.assertEqual(index.search("cook missing").matches, ())
        self.assertEqual(index.search("summer summer").matches, (event,))

    def test_metacharacters_are_literal_not_regex(self):
        literal = self.event(title="[a-z]+ .* (a|b) ? ^ $ \\")
        plain = self.event(id="plain", title="alphabet")
        index = self.index([literal, plain])
        for query in ("[a-z]+", ".*", "(a|b)", "?", "^", "$", "\\"):
            with self.subTest(query=query):
                self.assertEqual(index.search(query).matches, (literal,))

    def test_empty_query_does_not_scan_even_when_coverage_limited(self):
        index = self.index([self.event(), self.event(id="extra")], max_events=1)
        for query in ("", " \t\n"):
            self.assertEqual(index.search(query), SearchPage("", (), 0, False, True))

    def test_airing_identity_retains_namespace_source_channel_and_start(self):
        events = [self.event(key=c.key) for c in self.channels]
        events.append(self.event(start=30, end=40))
        self.assertEqual(airing_key(events[0]), (self.key, "same", 10))
        self.assertEqual(len({airing_key(e) for e in events}), 5)
        page = self.index(events).search("match")
        self.assertEqual(len(page.matches), 5)
        for channel in self.channels:
            selected = self.index(events).search("match", allowed_channels=(channel.key,))
            self.assertEqual({e.channel for e in selected.matches}, {channel.key})

    def test_deterministic_full_identity_order(self):
        events = [self.event(key=c.key) for c in self.channels]
        events += [self.event(id="aaa"), self.event(start=0), self.event(start=40, end=50)]
        expected = tuple(sorted(events, key=lambda e: (
            e.start, e.channel.namespace, e.channel.source_id, e.channel.channel_id, e.id)))
        self.assertEqual(self.index(reversed(events)).search("match").matches, expected)

    def test_duplicate_airing_first_observation_wins(self):
        first = self.event(title="First match")
        duplicate = replace(first, title="Replacement", description="replacement")
        repeated = replace(first, start=30, end=40)
        index = self.index([first, duplicate, repeated])
        self.assertEqual(index.search("match").matches, (first, repeated))
        self.assertEqual(index.search("replacement").matches, ())
        self.assertEqual(index.search("missing").scanned, 2)

    def test_unknown_channel_skipped_not_joined_by_number_or_partial_id(self):
        unknown = self.event(key=ChannelKey("unknown", "one", "1"))
        known = self.event()
        index = self.index([unknown, known])
        self.assertEqual(index.search("match").matches, (known,))
        self.assertEqual(index.search("match").scanned, 1)
        self.assertEqual(index.search("match", allowed_channels=(unknown.channel,)).matches, ())

    def test_allowed_none_empty_and_full_identity(self):
        events = [self.event(key=c.key) for c in self.channels]
        index = self.index(events)
        self.assertEqual(len(index.search("match", allowed_channels=None).matches), 4)
        self.assertEqual(index.search("match", allowed_channels=()),
                         SearchPage("match", (), 0, False, False))
        self.assertEqual(index.search("match", allowed_channels=(self.key, self.key)).matches,
                         (events[0],))

    def test_halfopen_horizon_known_and_unknown_ends(self):
        events = [self.event("ends-left", 0, 10), self.event("overlap", 0, 11),
                  self.event("starts-left", 10, 12), self.event("inside", 19, 21),
                  self.event("starts-right", 20, 30), self.event("unknown-before", 9, None),
                  self.event("unknown-left", 10, None), self.event("unknown-right", 20, None)]
        page = self.index(events).search("match", horizon=(10, 20))
        self.assertEqual({e.id for e in page.matches},
                         {"overlap", "starts-left", "inside", "unknown-left"})
        unknown = next(e for e in page.matches if e.id == "unknown-left")
        self.assertIsNone(unknown.end)
        self.assertEqual(self.index([events[5]]).search("match", horizon=(10, 20)).matches, ())

    def test_invalid_records_and_ends_no_duration_or_unit_guess(self):
        events = [None, self.event(start=True), self.event(title=None),
                  self.event(id=""), self.event(key=ChannelKey("", "one", "1")),
                  self.event("missing", end=None), self.event("backwards", end=9),
                  self.event("bool", end=True), self.event("float", end=12.5),
                  self.event("large", start=10**12, end=10**12 + 1)]
        page = self.index(events).search("match")
        self.assertEqual(len(page.matches), 2)
        self.assertIsNone(page.matches[0].end)
        self.assertEqual(page.matches[-1].start, 10**12)
        self.assertTrue(page.coverage_limited)
        self.assertTrue(page.truncated)

    def test_unbounded_event_input_consumes_only_cap_plus_one(self):
        seen = []

        def events():
            i = 0
            while True:
                seen.append(i)
                yield self.event(str(i), start=i, end=i + 1)
                i += 1

        index = self.index(events(), max_events=3)
        self.assertEqual(seen, [0, 1, 2, 3])
        page = index.search("match")
        self.assertEqual(len(page.matches), 3)
        self.assertEqual(page.scanned, 3)
        self.assertTrue(page.coverage_limited)
        self.assertTrue(page.truncated)
        # Raw observations, not successful validation/dedup, use the budget.
        page = self.index([None, self.event(), self.event(id="later")], max_events=1).search("match")
        self.assertEqual(page, SearchPage("match", (), 0, True, True))

    def test_channel_inventory_is_bounded_with_explicit_incomplete_coverage(self):
        seen = 0

        def channels():
            nonlocal seen
            while True:
                seen += 1
                yield self.channels[0]

        page = ProgrammeSearchIndex([self.event()], channels()).search("match")
        self.assertEqual(seen, 100001)
        self.assertTrue(page.coverage_limited)
        self.assertTrue(page.truncated)

    def test_integer_limits_reject_bool_and_accept_endpoints(self):
        for value in (True, False, 0, -1, 100001, 1.0, "1", None):
            with self.subTest(max_events=value), self.assertRaises(ValueError):
                self.index([], max_events=value)
        for value in (True, False, 0, -1, 201, 1.0, "1", None):
            with self.subTest(limit=value), self.assertRaises(ValueError):
                self.index([]).search("", limit=value)
        for maximum in (1, 100000):
            for limit in (1, 200):
                self.assertFalse(self.index([], max_events=maximum).search("match", limit=limit).truncated)
        for horizon in ((1, 1), (2, 1), (True, 2), (0, False), (0.0, 1), (0,), [0, 1], "01"):
            with self.subTest(horizon=horizon), self.assertRaises(ValueError):
                self.index([]).search("match", horizon=horizon)
        for allowed in ([self.key], ("1",)):
            with self.subTest(allowed=allowed), self.assertRaises(ValueError):
                self.index([]).search("match", allowed_channels=allowed)

    def test_early_stop_extra_match_counts_filtered_and_nonmatching_records(self):
        events = [self.event(str(i), start=i, end=i + 1) for i in range(10)]
        events[1] = replace(events[1], title="Other")
        events[2] = replace(events[2], channel=self.channels[1].key)
        page = self.index(events).search("match", allowed_channels=(self.key,), limit=2)
        self.assertEqual(page.matches, (events[0], events[3]))
        self.assertEqual(page.scanned, 5)
        self.assertTrue(page.truncated)
        self.assertFalse(page.coverage_limited)

    def test_exact_limit_no_match_and_index_overflow_are_distinct(self):
        events = [self.event(str(i), start=i, end=i + 1) for i in range(3)]
        page = self.index(events).search("match", limit=3)
        self.assertEqual(page.scanned, 3)
        self.assertFalse(page.truncated)
        self.assertFalse(page.coverage_limited)
        self.assertEqual(self.index(events).search("absent"),
                         SearchPage("absent", (), 3, False, False))
        incomplete = self.index(events, max_events=2)
        self.assertEqual(incomplete.search("absent"), SearchPage("absent", (), 2, True, True))
        self.assertEqual(incomplete.search("match", allowed_channels=()),
                         SearchPage("match", (), 0, True, True))

    def test_immutable_snapshot_page_and_index(self):
        events = [self.event()]
        channels = list(self.channels)
        index = ProgrammeSearchIndex(events, channels)
        page = index.search("match")
        events.clear()
        channels.clear()
        self.assertEqual(index.search("match"), page)
        self.assertIsInstance(page.matches, tuple)
        with self.assertRaises(FrozenInstanceError):
            page.scanned = 999
        with self.assertRaises(FrozenInstanceError):
            index._records = ()
        with self.assertRaises(FrozenInstanceError):
            page.matches[0].end = 999


class UnifiedSearchTests(unittest.TestCase):
    source = 'fixture-one'
    channel = ChannelKey('ns', 'device', '7')

    def guide(self, events, *, keys=None, allowed=None, **kwargs):
        keys = (self.channel,) if keys is None else keys
        channels = tuple(Channel(key, '7', 'Same channel name') for key in keys)
        return Listing(GuideSnapshot(ProgrammeSearchIndex(events, channels, **kwargs),
                                     GuideScope((10, 20), keys if allowed is None else allowed)))

    def test_all_groups_literal_unicode_and_stable_per_category_order(self):
        other = replace(self.channel, source_id='device-2')
        early = Event('same', self.channel, 10, 13, 'ＣＡＦÉ', 'Straße')
        events = (replace(early, start=15, end=17), replace(early, channel=other), early)
        older = Recording('same', 'ＣＡＦÉ', 'Straße', None, None, False, 100, None,
                          namespace='ns', provenance='dvr')
        newer = replace(older, created=200, provenance='import')
        records = (older, newer, replace(newer, namespace='ns-2'))
        jobs = (Job('same', 'Straße CAFÉ', 12, 15, ()), Job('A', 'Straße CAFÉ', 12, 15, ()))
        passes = (PassEntry('Ａ', 'Straße CAFÉ'), PassEntry('A', 'STRASSE cafe\u0301'),
                  PassEntry('same', 'Tours Straße CAFÉ'))

        def page(reverse):
            def order(rows):
                return rows[::-1] if reverse else rows

            return project_search('  cafe\u0301\tSTRASSE ', source=self.source,
                                  guide=self.guide(order(events), keys=(self.channel, other)),
                                  recordings=Listing(order(records)), scheduled=Listing(order(jobs)),
                                  passes=Listing(order(passes)))

        result = page(False)
        self.assertEqual(result, page(True))
        self.assertEqual(result.query, 'café strasse')
        self.assertEqual(tuple(g.category for g in result.groups), tuple(SearchCategory)[1:])
        self.assertEqual(tuple(tuple(hit.id for hit in group.hits) for group in result.groups), (
            (ProgrammeId(self.source, self.channel, 'same', 10),
             ProgrammeId(self.source, other, 'same', 10),
             ProgrammeId(self.source, self.channel, 'same', 15)),
            (RecordingId(self.source, 'ns', 'import', 'same'),
             RecordingId(self.source, 'ns-2', 'import', 'same'),
             RecordingId(self.source, 'ns', 'dvr', 'same')),
            (ScheduledId(self.source, 'A'), ScheduledId(self.source, 'same')),
            (PassId(self.source, 'A'), PassId(self.source, 'Ａ'), PassId(self.source, 'same')),
        ))
        ids = {hit.id for group in result.groups for hit in group.hits}
        self.assertEqual(len(ids), 11)

    def test_guide_scope_exact_channels_and_half_open_horizon(self):
        other = replace(self.channel, source_id='another-device')
        inside = Event('same', self.channel, 9, 11, 'Match')
        events = (inside, replace(inside, channel=other), replace(inside, id='before', end=10),
                  replace(inside, id='unknown-end', end=None),
                  replace(inside, id='future', start=20, end=21))
        guide = self.guide(events, keys=(self.channel, other), allowed=(self.channel,))
        page = project_search('match', source=self.source, category=SearchCategory.GUIDE, guide=guide)
        self.assertEqual(page.guide_scope, GuideScope((10, 20), (self.channel,)))
        group, = page.groups
        self.assertEqual(tuple(h.id for h in group.hits),
                         (ProgrammeId(self.source, self.channel, 'same', 9),))
        self.assertTrue(group.coverage_complete)
        self.assertFalse(group.truncated)
        empty = project_search('match', source=self.source, category=SearchCategory.GUIDE,
                               guide=self.guide((), keys=()))
        self.assertEqual(empty.guide_scope, GuideScope((10, 20), ()))
        self.assertTrue(empty.groups[0].coverage_complete)
        self.assertEqual(empty.groups[0].hits, ())

    def test_listings_distinguish_admitted_empty_missing_and_retained_fallback(self):
        fallback = Listing((Job('same', 'Match', 12, 15, ()),),
                           ListingCoverage(stale=True, partial=True, unavailable=True, capped=True))
        loading = Listing(coverage=ListingCoverage(loading=True))
        for query in ('missing', 'match', ' \u2003\t'):
            page = project_search(query, source=self.source, recordings=Listing(()),
                                  scheduled=fallback, passes=loading)
            guide, recording, scheduled, passes = page.groups
            self.assertEqual(tuple(g.has_snapshot for g in page.groups), (False, True, True, False))
            self.assertEqual(tuple(g.coverage_complete for g in page.groups), (False, True, False, False))
            self.assertEqual(guide.coverage, ListingCoverage())
            self.assertEqual(recording.hits, ())
            self.assertEqual(scheduled.coverage, fallback.coverage)
            self.assertEqual(len(scheduled.hits), int(query == 'match'))
            self.assertEqual(passes.coverage, loading.coverage)
            self.assertFalse(any(g.truncated for g in page.groups))
            self.assertIsNone(page.guide_scope)
        cold = project_search('match', source=self.source, category=SearchCategory.PASSES,
                              passes=Listing(coverage=ListingCoverage(unavailable=True)))
        self.assertTrue(cold.groups[0].coverage.unavailable)
        self.assertFalse(cold.groups[0].has_snapshot)

    def test_shared_result_limit_reports_only_proven_omissions(self):
        event = Event('same', self.channel, 10, 11, 'Match')
        job = Job('same', 'Match', 10, 11, ())
        listings = dict(guide=self.guide((event,)), scheduled=Listing((job,)),
                        recordings=Listing(()), passes=Listing((PassEntry('same', 'Other'),)))
        for limit, expected in ((1, (False, False, True, False)), (2, (False,) * 4)):
            page = project_search('match', source=self.source, limit=limit, **listings)
            self.assertEqual(sum(len(g.hits) for g in page.groups), limit)
            self.assertEqual(tuple(g.truncated for g in page.groups), expected)
            self.assertTrue(all(g.coverage_complete for g in page.groups))
        # A user can still search just the category omitted by the shared budget.
        page = project_search('match', source=self.source, category=SearchCategory.SCHEDULED,
                              limit=1, **listings)
        group, = page.groups
        self.assertEqual(group.hits[0].id, ScheduledId(self.source, 'same'))
        self.assertIsNone(page.guide_scope)

    def test_input_caps_and_guide_rejections_are_not_result_truncation(self):
        event = Event('same', self.channel, 10, 11, 'Match')
        pass_entry = PassEntry('same', 'Other')
        page = project_search('match', source=self.source, limit=1,
                              guide=self.guide((event, replace(event, id='later')), max_events=1),
                              passes=Listing((pass_entry,) * MAX_LISTING_RECORDS
                                             + (PassEntry('late', 'Match'),)))
        guide, _, _, passes = page.groups
        for group in (guide, passes):
            self.assertEqual(group.coverage, ListingCoverage(capped=True))
            self.assertFalse(group.truncated)
        self.assertEqual(passes.hits, ())
        rejected = self.guide((replace(event, end=9),))
        group = project_search('match', source=self.source, guide=rejected).groups[0]
        self.assertEqual(group.coverage, ListingCoverage(partial=True))
        self.assertFalse(group.truncated)
        complete = self.guide((event, replace(event, id='later')))
        group = project_search('match', source=self.source, guide=complete, limit=1).groups[0]
        self.assertTrue(group.coverage_complete)
        self.assertTrue(group.truncated)

    def test_exact_identity_across_refresh_and_source_replacement(self):
        a = PassEntry('a', 'First Match')
        b = PassEntry('b', 'Second Match')
        selected = PassId(self.source, 'b')

        def project(rows, source=self.source):
            return project_search('match', source=source, category=SearchCategory.PASSES, passes=Listing(rows))

        before = project((a, b, replace(b, name='Replacement Match'),
                          PassEntry('c', 'Other'), PassEntry('c', 'Last Match')))
        self.assertEqual(len(before.groups[0].hits), 2)
        self.assertEqual(before.groups[0].hits[1], before.find(selected))
        after = project((a, replace(b, name='Earlier Match')))
        self.assertEqual(after.groups[0].hits[0], after.find(selected))
        self.assertIsNone(project((a,)).find(selected))
        self.assertIsNone(project((a, b), 'fixture-two').find(selected))
        with self.assertRaises(FrozenInstanceError):
            selected.pass_id = 'a'

    def test_recordings_adapter_uses_leaves_and_matches_legacy_library(self):
        record = Recording('match', 'Cooking', 'Summer recipes', 2, 1, False, 100, None,
                           namespace='ns', provenance='import')
        browser = LibraryBrowser((record, replace(record, id='other', show='Winter recipes')))
        browser.set_provenance_filter('dvr')
        self.assertEqual(browser.rows(), ())
        page = project_search('SUM cook', source=self.source, category=SearchCategory.RECORDINGS,
                              recordings=Listing(browser.search_records))
        self.assertEqual(tuple(h.id for h in page.groups[0].hits),
                         (RecordingId(self.source, 'ns', 'import', 'match'),))
        self.assertEqual(browser.provenance_filter, 'dvr')
        browser.set_provenance_filter('all')
        browser.set_query('SUM cook')
        self.assertEqual(browser.selected_recording(), record)

    def test_query_and_limit_bounds_and_blank_query(self):
        entries = tuple(PassEntry(str(i), 'ss' * 128) for i in range(201))
        guide = self.guide((Event('a', self.channel, 10, 11, 'ss' * 128),))
        for text, limit in ((' ' * 129, 1), (None, 1), ('', True), ('', 0), ('', 201)):
            with self.subTest(text=text, limit=limit), self.assertRaises(ValueError):
                project_search(text, source=self.source, limit=limit)
        page = project_search('ß' * 128, source=self.source, guide=guide, passes=Listing(entries), limit=200)
        self.assertEqual(len(page.groups[0].hits), 1)
        self.assertEqual(len(page.groups[-1].hits), 199)
        self.assertTrue(page.groups[-1].truncated)
        blank = project_search('\u2003', source=self.source, passes=Listing(entries), limit=1)
        self.assertEqual(blank.query, '')
        self.assertTrue(blank.groups[-1].coverage_complete)
        self.assertFalse(any(g.hits or g.truncated for g in blank.groups))


if __name__ == "__main__":
    unittest.main()
