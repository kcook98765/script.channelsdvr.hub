"""Pure, synthetic library navigation contracts; no Kodi imports or I/O."""

from dataclasses import replace
from itertools import count
import unittest
from unittest.mock import patch

from channels_dvr.browser import Row, recording_row
from channels_dvr.library import LibraryBrowser, provenance_label, recording_key
from channels_dvr.models import Recording
from channels_dvr.repository import MockRepository


def record(id="one", **fields):
    return replace(Recording(id, "Episode", "Show", 1, 1, False, 100, 1800,
                             namespace="synthetic", provenance="dvr", show_id="show"),
                   **fields)


def bookmark_churn(replacements: int = 200) -> dict[int, int]:
    """Pure reproducible one-record churn probe, also usable as a benchmark."""
    browser = LibraryBrowser((), max_records=1)
    counts: dict[int, int] = {}
    for iteration in range(1, replacements + 1):
        browser.update((record(show_id=f"show-{iteration}"),))
        browser.open_selected()
        browser.open_selected()
        if iteration in (1, 10, 100, replacements):
            counts[iteration] = len(browser._bookmarks)
    return counts


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.records = MockRepository().recordings
        self.browser = LibraryBrowser(self.records)

    def test_defaults_and_fixture_true_hierarchy(self):
        b = self.browser
        self.assertEqual((b.mode, b.query, b.provenance_filter, b.position, b.depth),
                         ("Shows", "", "all", 0, 0))
        self.assertFalse(b.truncated or b.coverage_limited)
        self.assertIsInstance(b.rows(), tuple)
        self.assertEqual([r.label for r in b.rows()], [
            'Synthetic Atlas | DVR | Library source: synthetic',
            'Synthetic Atlas | Imported | Library source: synthetic',
            'Synthetic Workshop | DVR | Library source: synthetic',
            'Unclassified sample | Unknown | Library source: Unknown',
        ])
        self.assertFalse(b.back())
        self.assertTrue(b.open_selected())
        self.assertEqual([r.label for r in b.rows()], ["Season 1", "Season 2"])
        self.assertEqual(b.depth, 1)
        self.assertTrue(b.open_selected())
        self.assertEqual([r.id for r in b.rows()], [
            repr(("synthetic", "dvr", "series-1")),
            repr(("synthetic", "dvr", "series-2"))])
        self.assertEqual(b.depth, 2)
        self.assertIn("Synthetic Atlas | DVR", b.breadcrumb)
        self.assertTrue(b.breadcrumb.endswith("Season 1"))
        self.assertFalse(b.open_selected())

    def test_numeric_seasons_zero_unknown_and_episode_order(self):
        b = LibraryBrowser((record("unknown", season=None), record("s2", season=2),
                            record("s0b", season=0, episode=2),
                            record("s0a", season=0, episode=1)))
        b.open_selected()
        self.assertEqual([r.label for r in b.rows()], ["Season 0", "Season 2", "Season ?"])
        b.open_selected()
        self.assertEqual([r.id for r in b.rows()], [repr(("synthetic", "dvr", "s0a")),
                                                  repr(("synthetic", "dvr", "s0b"))])

    def test_child_cursors_are_separate_and_back_restores_parent_identity(self):
        b = self.browser
        b.open_selected()
        season1 = b.selected.id
        b.open_selected()
        b.move(1)
        episode = b.selected.id
        b.back()
        self.assertEqual(b.selected.id, season1)
        b.move(1)
        season2 = b.selected.id
        b.open_selected()
        self.assertEqual(len(b.rows()), 1)
        b.back()
        self.assertEqual(b.selected.id, season2)
        b.move(-1)
        b.open_selected()
        self.assertEqual(b.selected.id, episode)
        b.back()
        b.back()
        self.assertEqual(b.position, 0)
        self.assertFalse(b.back())

    def test_modes_restore_independent_paths_and_cursors(self):
        b = self.browser
        b.open_selected()
        b.open_selected()
        b.move(1)
        selected = b.selected.id
        b.set_mode("Recent")
        self.assertEqual(len(b.rows()), 9)
        self.assertEqual([r.id for r in b.rows()], [repr(recording_key(r)) for r in sorted(
            self.records, key=lambda r: (-r.created, recording_key(r)))])
        b.move(3)
        recent = b.selected.id
        b.set_mode("Movies")
        self.assertEqual(len(b.rows()), 3)
        self.assertEqual([r.label.split(" | ")[0] for r in b.rows()],
                         ["A Quiet Orbit", "The Paper Planet", "The Paper Planet (imported)"])
        self.assertTrue(all(not r.children for r in b.rows()))
        b.move(2)
        movie = b.selected.id
        b.set_mode("Shows")
        self.assertEqual((b.depth, b.selected.id), (2, selected))
        b.set_mode("Recent")
        self.assertEqual(b.selected.id, recent)
        b.set_mode("Movies")
        self.assertEqual(b.selected.id, movie)

    def test_origin_contexts_restore_paths_and_leaf_cursors(self):
        b = self.browser
        b.open_selected()
        b.open_selected()
        b.move(1)
        all_id = b.selected.id
        b.set_provenance_filter("import")
        self.assertEqual((b.depth, len(b.rows())), (0, 1))
        b.open_selected()
        b.open_selected()
        imported_id = b.selected.id
        b.set_provenance_filter("all")
        self.assertEqual((b.depth, b.selected.id), (2, all_id))
        b.set_provenance_filter("import")
        self.assertEqual((b.depth, b.selected.id), (2, imported_id))
        self.assertNotEqual(all_id, imported_id)

    def test_equivalent_queries_keep_original_text_cursor_and_cached_view(self):
        b = self.browser
        b.set_query(" atlas ")
        b.move(2)
        identity, rows = b.selected.id, b.rows()
        b.set_query("  ＡＴＬＡＳ  ")
        self.assertEqual(b.query, "  ＡＴＬＡＳ  ")
        self.assertEqual(b.selected.id, identity)
        self.assertIs(b.rows(), rows)
        b.set_mode("Recent")
        b.set_mode("Search")
        self.assertEqual(b.selected.id, identity)

    def test_changed_queries_reset_and_invalid_queries_are_atomic(self):
        b = self.browser
        b.set_query("atlas")
        b.move(2)
        b.set_query("chapter")
        self.assertEqual((b.mode, b.position), ("Search", 0))
        b.move(1)
        before = b.query, b.selected, b.breadcrumb
        for invalid in (None, 123, "x" * 129, "ﬃ" * 129):
            with self.assertRaises(ValueError):
                b.set_query(invalid)
            self.assertEqual((b.query, b.selected, b.breadcrumb), before)
        b.set_query("ﬃ" * 128)  # Limit applies before NFKC expands ligatures.
        self.assertEqual(len(b.query), 128)

    def test_many_queries_bound_search_contexts_and_retain_nonsearch_bookmarks(self):
        b = self.browser
        b.open_selected()
        b.open_selected()
        b.move(1)
        episode = b.selected
        for mode in ("Recent", "Movies"):
            for origin in ("all", "dvr", "import", "unknown"):
                b.set_mode(mode)
                b.set_provenance_filter(origin)
                b.move(1)
        views = dict(b._views)
        paths = dict(b._paths)
        bookmarks = dict(b._bookmarks)
        for i in range(200):
            query = f"unique query {i}"
            b.set_query(query)
            self.assertEqual(b.position, 0)
            for origin in ("all", "dvr", "import", "unknown"):
                b.set_provenance_filter(origin)
            expected = {("Search", origin, query)
                        for origin in ("all", "dvr", "import", "unknown")}
            self.assertEqual({c for c in b._views if c[0] == "Search"}, expected)
            self.assertEqual({c for c in b._paths if c[0] == "Search"}, expected)
            self.assertEqual({c for c, _ in b._bookmarks if c[0] == "Search"}, expected)
            self.assertEqual(sum(c[0] == "Search" for c, _ in b._bookmarks), 4)
            self.assertEqual({c: p for c, p in b._paths.items() if c[0] != "Search"}, paths)
            self.assertEqual({k: v for k, v in b._bookmarks.items() if k[0][0] != "Search"},
                             bookmarks)
            for context, view in views.items():
                self.assertIs(b._views[context], view)
        b.set_provenance_filter("all")
        b.set_mode("Shows")
        self.assertEqual((b.depth, b.selected), (2, episode))

    def test_current_query_keeps_four_origin_cursors_but_revisiting_query_resets(self):
        b = self.browser
        b.set_query("a")
        for origin in ("all", "dvr", "import", "unknown"):
            b.set_provenance_filter(origin)
            b.move(1)
        cursors = dict(b._bookmarks)
        views = dict(b._views)
        b.set_query("  Ａ  ")
        self.assertEqual(b.query, "  Ａ  ")
        for origin in ("all", "dvr", "import", "unknown"):
            b.set_provenance_filter(origin)
            key = (("Search", origin, "a"), ())
            self.assertEqual(b._bookmarks[key], cursors[key])
            self.assertIs(b._views[key[0]], views[key[0]])
        b.set_query("chapter")
        b.set_query("a")
        for origin in ("all", "dvr", "import", "unknown"):
            b.set_provenance_filter(origin)
            self.assertEqual(b.position, 0)

    def test_search_is_literal_token_and_casefold_nfkc_title_plus_show(self):
        b = LibraryBrowser((record("hit", title="Straße [x].*", show="ＣＡＦÉ"),
                            record("miss", title="STRASSE", show="Elsewhere")))
        b.set_query("café STRASSE")
        self.assertEqual([r.id for r in b.rows()], [repr(("synthetic", "dvr", "hit"))])
        b.set_query("[x].*")
        self.assertEqual(len(b.rows()), 1)
        b.set_query("^.*$")
        self.assertEqual(b.rows(), ())
        b.set_query("STRASSE missing")
        self.assertEqual(b.rows(), ())

    def test_blank_prompt_no_match_and_search_origin_contexts(self):
        b = self.browser
        b.set_mode("Search")
        self.assertEqual(b.rows(), ())
        self.assertIsNone(b.selected)
        self.assertIn('Enter search text', b.breadcrumb)
        b.set_query("  \t ")
        self.assertEqual(b.rows(), ())
        b.set_query("atlas")
        b.move(2)
        identity = b.selected.id
        b.set_provenance_filter("import")
        self.assertEqual(len(b.rows()), 1)
        self.assertIn("Imported", b.selected.label)
        b.set_provenance_filter("all")
        self.assertEqual(b.selected.id, identity)
        b.set_query("not-in-the-synthetic-library")
        self.assertIsNone(b.selected)
        self.assertFalse(b.truncated)

    def test_search_exact_cap_lookahead_and_flag_reset(self):
        records = tuple(record(str(i)) for i in range(100))
        b = LibraryBrowser(records)
        b.set_query("episode")
        self.assertEqual(len(b.rows()), 100)
        self.assertFalse(b.truncated)
        b.update(records + (record("extra"),))
        self.assertEqual(len(b.rows()), 100)
        self.assertTrue(b.truncated)
        self.assertFalse(b.coverage_limited)
        b.set_mode("Recent")
        self.assertEqual(len(b.rows()), 101)
        self.assertFalse(b.truncated)
        b.set_mode("Search")
        self.assertTrue(b.truncated)
        b.set_query("no match")
        self.assertFalse(b.truncated)

    def test_backend_id_collisions_remain_distinct_opaque_row_ids(self):
        b = self.browser
        b.set_mode("Recent")
        identities = {r.id for r in b.rows()}
        self.assertEqual(len(identities), 9)
        for backend in ("series-2", "movie-1"):
            self.assertIn(repr(("synthetic", "dvr", backend)), identities)
            self.assertIn(repr(("synthetic", "import", backend)), identities)
        strange = record("a', ('b", namespace="n|s", provenance="unknown")
        self.assertEqual(recording_row(strange).id, repr(("n|s", "unknown", "a', ('b")))
        self.assertEqual(recording_key(strange), ("n|s", "unknown", "a', ('b"))

    def test_show_identity_partitions_namespace_origin_known_id_and_fallback(self):
        records = (record("a"), record("b", namespace="other"),
                   record("c", provenance="import"), record("d", show_id="different"),
                   record("e", show_id=None), record("f", show_id=None, show="Different"))
        b = LibraryBrowser(records)
        self.assertEqual(len(b.rows()), 6)
        self.assertEqual(len({r.id for r in b.rows()}), 6)
        original = next(r.id for r in b.rows() if r.id == repr(
            ("show", "synthetic", "dvr", "id", "show")))
        b.update((replace(records[0], show="Renamed"),) + records[1:])
        self.assertIn(original, {r.id for r in b.rows()})
        fallback = LibraryBrowser((record("a", show=None, show_id=None),
                                   record("b", show=None, show_id=None, title="Other")))
        self.assertEqual(len(fallback.rows()), 2)

    def test_unknown_origin_is_explicit_and_filters_apply_to_every_mode(self):
        self.assertEqual([provenance_label(c) for c in ("dvr", "import", "unknown")],
                         ["DVR", "Imported", "Unknown"])
        for mode in ("Shows", "Recent", "Movies", "Search"):
            b = LibraryBrowser(self.records)
            b.set_mode(mode)
            if mode == "Search":
                b.set_query("sample")
            b.set_provenance_filter("unknown")
            self.assertEqual(len(b.rows()), 0 if mode == "Movies" else 1)
            self.assertTrue(all("Unknown" in row.label for row in b.rows()))
        self.assertEqual(recording_key(self.records[-1]), ("", "unknown", "unknown-1"))

    def test_malformed_models_rejected_and_coverage_disclosed(self):
        malformed = [None, {}, "record"]
        for fields in ({"id": ""}, {"title": None}, {"show": 5}, {"namespace": None},
                       {"provenance": "DVR"}, {"provenance": None}, {"show_id": ""},
                       {"show_id": 1}, {"is_movie": 1}, {"watched": 0},
                       {"created": "100"}, {"created": True}, {"duration": -1},
                       {"duration": float('inf')}, {"resume": -1}, {"resume": True},
                       {"season": -1}, {"episode": "2"}, {"episode": False},
                       {"created": 10**100}):
            malformed.append(replace(record(), **fields))
        b = LibraryBrowser(tuple(malformed) + (record("valid"),))
        b.set_mode("Recent")
        self.assertEqual([r.id for r in b.rows()], [repr(("synthetic", "dvr", "valid"))])
        self.assertTrue(b.coverage_limited)
        b.update((record("valid"),))
        self.assertFalse(b.coverage_limited)

    def test_no_provenance_timestamp_duration_or_progress_unit_guessing(self):
        from channels_dvr.guide import time_label

        item = record("import-word-in-id", title="Imported DVR episode", provenance="unknown",
                      created=1700000000, duration=1, resume=7200000, watched=False)
        b = LibraryBrowser((item,))
        b.set_mode("Recent")
        self.assertIn("Unknown", b.selected.label)
        self.assertIn('Resume at 7200000s', b.selected.label)
        self.assertIn("Duration: 1s", b.selected.detail)
        self.assertIn(f"Added: {time_label(1700000000)}", b.selected.detail)
        self.assertIn("Unwatched", b.selected.label)
        self.assertFalse(b.coverage_limited)
        b.set_provenance_filter("import")
        self.assertEqual(b.rows(), ())

    def test_known_flags_reject_wrong_types_without_truthiness(self):
        for flag in ("watched_known", "resume_known"):
            for value in (None, 0, 1, "false", "true", [], {}, 0.0, 1.0):
                with self.subTest(flag=flag, value=value):
                    b = LibraryBrowser((record("bad", **{flag: value}), record("good")))
                    b.set_mode("Recent")
                    self.assertEqual([r.id for r in b.rows()],
                                     [repr(("synthetic", "dvr", "good"))])
                    self.assertTrue(b.coverage_limited)

    def test_unknown_watch_and_resume_project_independently_in_library(self):
        for watched in (False, True):
            for resume in (0, 7200):
                for watched_known in (False, True):
                    for resume_known in (False, True):
                        with self.subTest(watched=watched, resume=resume,
                                          watched_known=watched_known, resume_known=resume_known):
                            item = record(watched=watched, resume=resume, duration=None,
                                          provenance="unknown", watched_known=watched_known,
                                          resume_known=resume_known)
                            b = LibraryBrowser((item,))
                            b.set_mode("Recent")
                            self.assertFalse(b.coverage_limited)
                            state = ("Watched" if watched else "Unwatched") if watched_known else (
                                'Watch status unknown')
                            if not resume_known:
                                state += ' | Resume position unknown'
                            elif resume:
                                state += f' | Resume at {resume}s'
                            self.assertEqual(b.selected.label,
                                             f"Episode | S1 E1 | {state} | Unknown | "
                                              'Library source: synthetic')
                            self.assertIn(f"\n{state}\n", b.selected.detail)
                            self.assertIn("Duration: Unknown", b.selected.detail)
                            self.assertEqual((item.watched, item.resume), (watched, resume))

    def test_deduplication_full_key_first_wins(self):
        first = record("same", title="First")
        b = LibraryBrowser((first, replace(first, title="Later duplicate"),
                            replace(first, provenance="import"), replace(first, namespace="other")))
        b.set_mode("Recent")
        self.assertEqual(len(b.rows()), 3)
        self.assertEqual(len({r.id for r in b.rows()}), 3)
        self.assertTrue(all(r.label.startswith("First |") for r in b.rows()))
        self.assertFalse(b.coverage_limited)

    def test_insert_remove_update_preserves_selection_by_id_then_clamps(self):
        a, b, c = record("a", created=30), record("b", created=20), record("c", created=10)
        browser = LibraryBrowser((a, b, c))
        browser.set_mode("Recent")
        browser.move(1)
        identity = browser.selected.id
        inserted = record("new", created=40)
        browser.update((inserted, a, replace(b, title="Fresh details", resume=500), c))
        self.assertEqual((browser.position, browser.selected.id), (2, identity))
        self.assertIn("Fresh details", browser.selected.label)
        self.assertIn('Resume at 500s', browser.selected.detail)
        browser.update((inserted, a, c))
        self.assertEqual((browser.position, browser.selected.id), (2, repr(recording_key(c))))
        browser.update((a,))
        self.assertEqual(browser.position, 0)

    def test_update_rebinds_folder_ids_and_replaces_captured_leaf_metadata(self):
        b = self.browser
        b.open_selected()
        b.open_selected()
        b.move(1)
        identity, old = b.selected.id, b.selected
        changed = tuple(replace(r, show="Renamed Atlas", title="New title", watched=True)
                        if r.show_id == "atlas" else r for r in self.records)
        b.update(changed)
        self.assertEqual((b.depth, b.selected.id), (2, identity))
        self.assertIsNot(b.selected, old)
        self.assertIn("New title", b.selected.label)
        self.assertIn("Renamed Atlas", b.breadcrumb)
        self.assertIn("Renamed Atlas", b.selected.detail)
        b.back()
        self.assertEqual(b.selected.label, "Season 1")
        self.assertIn("New title", b.selected.children[0].label)

    def test_removed_folders_clamp_to_nearest_existing_ancestor(self):
        b = self.browser
        b.open_selected()
        b.open_selected()
        b.update(tuple(r for r in self.records if not (r.show_id == "atlas" and r.season == 1)))
        self.assertEqual(b.depth, 1)
        self.assertEqual([r.label for r in b.rows()], ["Season 2"])
        b.update(tuple(r for r in self.records if r.show_id != "atlas"))
        self.assertEqual(b.depth, 0)
        self.assertFalse(b.back())
        self.assertNotIn("Synthetic Atlas | DVR", b.breadcrumb)
        b.update(())
        self.assertEqual((b.rows(), b.position, b.selected), ((), 0, None))
        self.assertFalse(b.open_selected())

    def test_inactive_contexts_rebind_before_reinsertion(self):
        b = self.browser
        b.set_provenance_filter("dvr")
        b.open_selected()
        b.open_selected()
        b.set_mode("Movies")
        b.move(1)
        movie = b.selected.id
        b.update(tuple(r for r in self.records if r.show_id != "atlas"))
        b.update(self.records)
        self.assertEqual(b.selected.id, movie)
        b.set_mode("Shows")
        self.assertEqual(b.depth, 0)  # Removed path is not resurrected on reinsertion.
        self.assertTrue(all(isinstance(r, Row) for r in b.rows()))

    def test_removed_folder_bookmarks_bounded_over_200_catalogue_replacements(self):
        self.assertEqual(bookmark_churn(), {1: 3, 10: 3, 100: 3, 200: 3})

    def test_pruning_keeps_existing_inactive_folder_cursors_across_contexts(self):
        b = self.browser
        removed_paths = set()
        kept_bookmarks = {}
        for origin in ("all", "dvr", "import", "unknown"):
            b.set_mode("Shows")
            b.set_provenance_filter(origin)
            for show_index, show in enumerate(b.rows()):
                b.move(show_index - b.position)
                b.open_selected()
                for season_index, season in enumerate(b.rows()):
                    b.move(season_index - b.position)
                    b.open_selected()
                    b.move(1)
                    b.back()
                b.back()
            # Record folder bookmarks only: root selections can legitimately
            # change when their selected child disappears on update.
            for key, bookmark in b._bookmarks.items():
                context, path = key
                if context != b._context() or not path:
                    continue
                workshop = repr(("show", "synthetic", "dvr", "id", "workshop"))
                atlas = ("show", "synthetic", "dvr", "id", "atlas")
                if path[0] == workshop or path[-1] == repr(("season", atlas, 2)):
                    removed_paths.add(key)
                else:
                    kept_bookmarks[key] = bookmark
            for mode in ("Recent", "Movies", "Search"):
                if mode == "Search":
                    b.set_query("atlas")
                else:
                    b.set_mode(mode)
                b.move(1)
        flat_bookmarks = {k: v for k, v in b._bookmarks.items() if k[0][0] != "Shows"}
        # Leave the active cursor inside the folder about to disappear.
        b.set_mode("Shows")
        b.set_provenance_filter("all")
        b.move(2 - b.position)
        b.open_selected()
        b.open_selected()
        self.assertIn("Workshop", b.breadcrumb)
        replacement = tuple(replace(r, title=r.title + " refreshed") if r.show_id is not None else r
                            for r in self.records if r.show_id != "workshop"
                            and not (r.show_id == "atlas" and r.season == 2))
        b.update(replacement)
        self.assertEqual(b.depth, 0)
        self.assertTrue(removed_paths)
        self.assertTrue(kept_bookmarks)
        self.assertTrue(removed_paths.isdisjoint(b._bookmarks))
        for key, bookmark in kept_bookmarks.items():
            self.assertEqual(b._bookmarks[key], bookmark)
        for key, bookmark in flat_bookmarks.items():
            self.assertEqual(b._bookmarks[key], bookmark)
        self.assertEqual(sum(c[0] == "Search" for c in b._views), 4)
        b.move(-b.position)  # Unrelated, still-existing Atlas folder.
        b.open_selected()
        b.open_selected()
        self.assertEqual(b.selected.id, repr(("synthetic", "dvr", "series-2")))
        self.assertIn("refreshed", b.selected.label)
        b.back()
        b.back()
        b.update(())
        self.assertTrue(all(not path for _, path in b._bookmarks))

    def test_unbounded_input_consumes_only_cap_plus_one_before_validation(self):
        consumed: list[int] = []

        def endless():
            for i in count():
                consumed.append(i)
                yield record(str(i)) if i else None

        b = LibraryBrowser(endless(), max_records=3)
        self.assertEqual(consumed, [0, 1, 2, 3])
        b.set_mode("Recent")
        self.assertEqual(len(b.rows()), 2)
        self.assertTrue(b.coverage_limited)
        b.set_query("episode")
        self.assertTrue(b.coverage_limited)
        self.assertFalse(b.truncated)  # Input coverage is distinct from match-page omission.
        consumed.clear()
        b.update(endless())
        self.assertEqual(consumed, [0, 1, 2, 3])

    def test_snapshot_and_views_reused_for_arrows_identical_and_equal_tuples(self):
        class OnceTuple(tuple):
            iterations = 0

            def __iter__(self):
                self.iterations += 1
                if self.iterations > 1:
                    raise AssertionError("Snapshot rescanned")
                return super().__iter__()

        records = OnceTuple(self.records)
        b = LibraryBrowser(records)
        rows, index = b.rows(), b._index
        with patch("channels_dvr.library.recording_row", side_effect=AssertionError("Reprojection")):
            for _ in range(10):
                b.move(1)
                b.move(-1)
                b.update(records)
                self.assertIs(b.rows(), rows)
            b.update(tuple(list(self.records)))
            self.assertIs(b.rows(), rows)
            self.assertIs(b._index, index)
        self.assertEqual(records.iterations, 1)
        b.set_mode("Recent")
        recent = b.rows()
        b.set_mode("Movies")
        b.set_mode("Recent")
        self.assertIs(b.rows(), recent)

    def test_watched_resume_duration_unknown_and_season_zero_projection(self):
        for watched in (False, True):
            item = record(watched=watched, resume=7200, duration=None, season=0, episode=0)
            row = recording_row(item)
            self.assertIn("Watched" if watched else "Unwatched", row.label)
            self.assertIn('Resume at 7200s', row.label)
            self.assertIn("Duration: Unknown", row.detail)
            self.assertIn("S0 E0", row.label)
            self.assertIn('DVR | Library source: synthetic', row.label)
            self.assertIn('Details only. For eligible DVR recordings, choose Play recording.', row.detail)
            self.assertNotIn('from the menu', row.detail)
            self.assertIn('watched/resume changes are not supported.', row.detail)
        self.assertNotIn("Resume", recording_row(record(watched=True, resume=0)).label)

    def test_empty_navigation_and_invalid_configuration(self):
        b = LibraryBrowser(())
        for direction in (-100, 100):
            b.move(direction)
            self.assertEqual(b.position, 0)
        self.assertFalse(b.back() or b.open_selected())
        for maximum in (0, -1, True, False, 1.5, "10", 100001):
            with self.assertRaises(ValueError):
                LibraryBrowser((), max_records=maximum)
        for method, value in ((b.set_mode, "shows"), (b.set_provenance_filter, "Imported")):
            with self.assertRaises(ValueError):
                method(value)
        self.assertEqual((b.mode, b.provenance_filter), ("Shows", "all"))
        for maximum in (1, 100000):
            self.assertEqual(LibraryBrowser((), max_records=maximum).rows(), ())


if __name__ == "__main__":
    unittest.main()
