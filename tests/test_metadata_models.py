"""Synthetic route contracts only; no DVR, Kodi, private fixtures or transport."""

from dataclasses import FrozenInstanceError, fields
import unittest
from unittest.mock import patch

from channels_dvr.metadata_models import (
    MetadataBatch, files_page, guide_window, public_channels, scheduled_jobs,
)
from channels_dvr.models import Channel, ChannelKey, Event, Job, Recording
from channels_dvr.normalization import DataState

NS = "synthetic"
NOW = 1_800_000_000


def channel(**changes: object) -> dict[str, object]:
    return dict(id="001", source_id="source-a", number="07.1", name="Synthetic",
                station_id="station") | changes


def airing(**changes: object) -> dict[str, object]:
    return dict(ProgramID="program", Title="Synthetic show", Time=NOW,
                Duration=900, Summary="Short", FullSummary="Long") | changes


def guide_row(airings: object = None, **channel_changes: object) -> dict[str, object]:
    return {"Channel": dict(DeviceID="source-a", ChannelID="001", Number="07.1",
                            Name="Synthetic", Station="station") | channel_changes,
            "Airings": [airing()] if airings is None else airings}


def file_row(**changes: object) -> dict[str, object]:
    return {"ID": "file", "CreatedAt": NOW, "Duration": 900,
            "Airing": {"Title": "Synthetic show", "EpisodeTitle": "Episode",
                       "SeriesID": "series", "SeasonNumber": 0, "EpisodeNumber": 2,
                       "Categories": ["Series"]}} | changes


def job_row(**changes: object) -> dict[str, object]:
    return {"id": "job", "name": "Scheduled", "start_time": NOW - 60,
            "end_time": NOW + 960,
            "item": {"title": "Synthetic show", "program_id": "program"}} | changes


class MetadataModelsTests(unittest.TestCase):
    def setUp(self) -> None:
        guard = patch("socket.socket", side_effect=AssertionError("No network"))
        guard.start()
        self.addCleanup(guard.stop)
        self.channels = public_channels([channel()], NS).items

    def test_batch_interface_is_frozen_and_generic(self) -> None:
        batch: MetadataBatch[Channel] = MetadataBatch((), DataState.EMPTY)
        self.assertEqual([f.name for f in fields(batch)],
                         ["items", "state", "warnings", "counts"])
        self.assertEqual((batch.warnings, batch.counts), ((), ()))
        with self.assertRaises(FrozenInstanceError):
            setattr(batch, "state", DataState.READY)

    def test_channels_filter_locally_and_keep_groups(self) -> None:
        payload = [channel(favorited=True), channel(id="hidden", hidden=True, station_id="")]
        batch = public_channels(payload, NS)
        self.assertEqual(batch.state, DataState.READY)
        self.assertEqual(batch.items[0].groups, ("Visible", "Favorites", "Guide station present"))
        self.assertEqual(dict(batch.counts), dict(raw_rows=2, accepted=2, channels=1,
                         hidden=1, filtered=1, duplicates=0, conflicts=0,
                         invalid_rows=0, missing_names=0))
        all_items = public_channels(payload, NS, include_hidden=True).items
        self.assertEqual(all_items[1].groups, ("Hidden", "Guide station missing"))
        self.assertEqual(public_channels([payload[1]], NS).state, DataState.EMPTY)

    def test_channels_missing_name_falls_back_with_warning(self) -> None:
        row = channel()
        del row["name"]
        batch = public_channels([row], NS)
        self.assertEqual(batch.items[0].name, "07.1")
        self.assertEqual(batch.warnings, ("missing_channel_name",))
        self.assertEqual(batch.state, DataState.READY)
        self.assertEqual(dict(batch.counts)["missing_names"], 1)

    def test_channels_source_collisions_preserve_string_identity(self) -> None:
        batch = public_channels([channel(), channel(source_id="source-b")], NS)
        self.assertEqual(len(batch.items), 2)
        self.assertEqual(batch.items[0].key, ChannelKey(NS, "source-a", "001"))
        self.assertEqual(batch.items[0].number, "07.1")
        self.assertNotEqual(batch.items[0].key, batch.items[1].key)

    def test_channels_invalid_fields_reject_entire_batch(self) -> None:
        for changes in ({"id": 1}, {"source_id": ""}, {"number": 7},
                        {"name": None}, {"hidden": 1}, {"favorited": "false"},
                        {"station_id": 42}):
            with self.subTest(changes=changes):
                batch = public_channels([channel(), channel(**changes)], NS)
                self.assertEqual((batch.state, batch.items), (DataState.MALFORMED, ()))
                self.assertEqual(dict(batch.counts)["invalid_rows"], 1)
        row = channel()
        del row["source_id"]
        self.assertEqual(public_channels([row], NS).state, DataState.MALFORMED)

    def test_channels_full_key_dedup_first_wins_conflicts_partial(self) -> None:
        exact = public_channels([channel(), channel()], NS)
        self.assertEqual((exact.state, len(exact.items)), (DataState.READY, 1))
        batch = public_channels([channel(), channel(hidden=True)], NS)
        self.assertEqual(batch.state, DataState.PARTIAL)
        self.assertEqual(batch.items[0].groups, ("Visible", "Guide station present"))
        self.assertEqual(dict(batch.counts)["duplicates"], 1)
        self.assertEqual(dict(batch.counts)["conflicts"], 1)

    def test_channels_empty_and_malformed_outer_shapes(self) -> None:
        self.assertEqual(public_channels([], NS).state, DataState.EMPTY)
        payloads: tuple[object, ...] = (None, {}, (), {"items": []}, "error")
        for payload in payloads:
            self.assertEqual(public_channels(payload, NS).state, DataState.MALFORMED)
        self.assertEqual(public_channels([], "").state, DataState.MALFORMED)

    def test_channels_station_groups_distinguish_explicit_blank_from_omitted(self) -> None:
        omitted = channel(id="omitted")
        del omitted["station_id"]
        batch = public_channels([channel(), channel(id="blank", station_id=""), omitted], NS)
        self.assertEqual(batch.state, DataState.READY)
        self.assertEqual(batch.warnings, ())
        self.assertEqual([c.groups for c in batch.items],
                         [("Visible", "Guide station present"),
                          ("Visible", "Guide station missing"),
                          ("Visible", "Guide station unqualified")])

    def test_channels_station_groups_keep_synthetic_54_channel_visibility(self) -> None:
        payload = [channel(id=str(i), hidden=i >= 13,
                           station_id="station" if i < 5 else "") for i in range(54)]
        batch = public_channels(payload, NS)
        self.assertEqual(batch.state, DataState.READY)
        self.assertEqual(len(batch.items), 13)
        self.assertEqual(dict(batch.counts)["hidden"], 41)
        self.assertEqual(sum("Guide station present" in c.groups for c in batch.items), 5)
        self.assertEqual(sum("Guide station missing" in c.groups for c in batch.items), 8)
        all_items = public_channels(payload, NS, include_hidden=True).items
        self.assertEqual(len(all_items), 54)
        self.assertTrue(all("Hidden" in c.groups for c in all_items[13:]))

    def test_guide_strict_source_join_no_number_station_or_airing_source_join(self) -> None:
        channels = public_channels([channel(), channel(source_id="source-b")], NS).items
        payload = [guide_row(), guide_row(DeviceID="source-b"),
                   guide_row([airing(Source="source-a")], DeviceID="ANY", Station=""),
                   guide_row(ChannelID="other")]
        batch = guide_window(payload, channels, NS, NOW)
        self.assertEqual(batch.state, DataState.PARTIAL)
        self.assertEqual([e.channel.source_id for e in batch.items], ["source-a", "source-b"])
        self.assertEqual(dict(batch.counts), dict(raw_rows=4, joined_rows=2,
                         rows_with_airings=4, airings_seen=4, accepted=2, events=2,
                         unknown_duration=0, missing_program_id=0, unjoined=2,
                         invalid_rows=0, invalid_airings=0, filtered=0,
                         duplicates=0, conflicts=0, station_missing_rows=0,
                         unqualified_airings=0, station_unqualified_rows=0))

    def test_guide_blank_station_omits_program_ids_without_global_failure(self) -> None:
        batch = guide_window([guide_row([airing(), airing(Time=NOW + 900)], Station="")],
                             self.channels, NS, NOW)
        self.assertEqual((batch.state, batch.items), (DataState.EMPTY, ()))
        self.assertEqual(batch.warnings, ("guide_station_missing",))
        self.assertEqual(dict(batch.counts)["joined_rows"], 1)
        self.assertEqual(dict(batch.counts)["airings_seen"], 2)
        self.assertEqual(dict(batch.counts)["accepted"], 0)
        self.assertEqual(dict(batch.counts)["station_missing_rows"], 1)
        self.assertEqual(dict(batch.counts)["unqualified_airings"], 2)
        empty = guide_window([guide_row([], Station="")], self.channels, NS, NOW)
        self.assertEqual(empty.state, DataState.EMPTY)
        self.assertEqual(empty.warnings, ("guide_station_missing",))
        self.assertEqual(dict(empty.counts)["unqualified_airings"], 0)

    def test_guide_five_mapped_eight_station_gaps_preserve_qualified_events(self) -> None:
        channels = public_channels([channel(id=str(i), station_id="station" if i < 5 else "")
                                    for i in range(13)], NS).items
        payload = [guide_row([airing(), airing(Time=NOW + 900)], ChannelID=str(i),
                             Station="station" if i < 5 else "") for i in range(13)]
        batch = guide_window(payload, channels, NS, NOW)
        self.assertEqual(batch.state, DataState.READY)
        self.assertEqual(batch.warnings, ("guide_station_missing",))
        self.assertEqual(dict(batch.counts), dict(raw_rows=13, joined_rows=13,
                         rows_with_airings=13, airings_seen=26, accepted=10, events=10,
                         unknown_duration=0, missing_program_id=0, unjoined=0,
                         invalid_rows=0, invalid_airings=0, filtered=0,
                         duplicates=0, conflicts=0, station_missing_rows=8,
                         unqualified_airings=16, station_unqualified_rows=0))
        self.assertEqual({e.channel.channel_id for e in batch.items},
                         {str(i) for i in range(5)})

    def test_guide_omitted_station_is_not_inferred_from_public_groups(self) -> None:
        channels = public_channels([channel(station_id="")], NS).items
        omitted = guide_row()
        channel_fields = omitted["Channel"]
        assert isinstance(channel_fields, dict)
        del channel_fields["Station"]
        for row in (omitted, guide_row()):
            batch = guide_window([row], channels, NS, NOW)
            self.assertEqual((batch.state, len(batch.items)), (DataState.READY, 1))
            self.assertEqual(batch.warnings, ())
            self.assertEqual(dict(batch.counts)["station_missing_rows"], 0)
            self.assertEqual(dict(batch.counts)["unqualified_airings"], 0)

    def test_guide_station_qualification_preserves_source_collision_boundaries(self) -> None:
        channels = public_channels([channel(station_id=""),
                                    channel(source_id="source-b")], NS).items
        batch = guide_window([guide_row(Station=""), guide_row(DeviceID="source-b")],
                             channels, NS, NOW)
        self.assertEqual(batch.state, DataState.READY)
        self.assertEqual([e.channel for e in batch.items],
                         [ChannelKey(NS, "source-b", "001")])
        self.assertEqual(dict(batch.counts)["joined_rows"], 2)
        self.assertEqual(dict(batch.counts)["station_missing_rows"], 1)
        self.assertEqual(dict(batch.counts)["unqualified_airings"], 1)

    def test_guide_require_station_five_present_eight_omitted(self) -> None:
        public_rows = [channel(id=str(i)) for i in range(13)]
        payload = [guide_row([airing(), airing(Time=NOW + 900)], ChannelID=str(i))
                   for i in range(13)]
        for i in range(5, 13):
            del public_rows[i]["station_id"]
            channel_fields = payload[i]["Channel"]
            assert isinstance(channel_fields, dict)
            del channel_fields["Station"]
        channels = public_channels(public_rows, NS).items
        self.assertEqual(len(channels), 13)
        self.assertEqual(sum("Guide station present" in c.groups for c in channels), 5)
        self.assertEqual(sum("Guide station unqualified" in c.groups for c in channels), 8)
        batch = guide_window(payload, channels, NS, NOW, require_station=True)
        self.assertEqual(batch.state, DataState.READY)
        self.assertEqual(batch.warnings, ("guide_station_unqualified",))
        self.assertEqual(dict(batch.counts), dict(raw_rows=13, joined_rows=13,
                         rows_with_airings=13, airings_seen=26, accepted=10, events=10,
                         unknown_duration=0, missing_program_id=0, unjoined=0,
                         invalid_rows=0, invalid_airings=0, filtered=0,
                         duplicates=0, conflicts=0, station_missing_rows=0,
                         unqualified_airings=16, station_unqualified_rows=8))
        self.assertEqual({e.channel.channel_id for e in batch.items},
                         {str(i) for i in range(5)})
        generic = guide_window(payload, channels, NS, NOW)
        self.assertEqual((generic.state, len(generic.items)), (DataState.READY, 26))
        self.assertEqual(generic.warnings, ())
        self.assertEqual(dict(generic.counts)["station_unqualified_rows"], 0)

    def test_guide_require_station_qualifies_either_joined_source_of_metadata(self) -> None:
        public_without_station = channel()
        del public_without_station["station_id"]
        channels = public_channels([public_without_station,
                                    channel(source_id="source-b")], NS).items
        guide_without_station = guide_row(DeviceID="source-b")
        channel_fields = guide_without_station["Channel"]
        assert isinstance(channel_fields, dict)
        del channel_fields["Station"]
        batch = guide_window([guide_row(), guide_without_station], channels, NS, NOW,
                             require_station=True)
        self.assertEqual((batch.state, len(batch.items)), (DataState.READY, 2))
        self.assertEqual(batch.warnings, ())
        self.assertEqual([e.channel.source_id for e in batch.items], ["source-a", "source-b"])
        unjoined = guide_window([guide_row(DeviceID="other")], channels, NS, NOW,
                                require_station=True)
        self.assertEqual(unjoined.state, DataState.PARTIAL)
        self.assertEqual(unjoined.warnings, ("unknown_guide_channel",))
        self.assertEqual(dict(unjoined.counts)["station_unqualified_rows"], 0)

    def test_guide_require_station_unknown_and_blank_counts_are_exclusive(self) -> None:
        public = channel()
        del public["station_id"]
        channels = public_channels([public], NS).items
        omitted = guide_row()
        channel_fields = omitted["Channel"]
        assert isinstance(channel_fields, dict)
        del channel_fields["Station"]
        unknown = guide_window([omitted], channels, NS, NOW, require_station=True)
        self.assertEqual((unknown.state, unknown.items), (DataState.EMPTY, ()))
        self.assertEqual(unknown.warnings, ("guide_station_unqualified",))
        self.assertEqual(dict(unknown.counts)["station_unqualified_rows"], 1)
        self.assertEqual(dict(unknown.counts)["station_missing_rows"], 0)
        both = guide_window([omitted, guide_row(Station="")], channels, NS, NOW,
                            require_station=True)
        self.assertEqual(both.state, DataState.EMPTY)
        self.assertEqual(both.warnings, ("guide_station_unqualified", "guide_station_missing"))
        self.assertEqual(dict(both.counts)["station_unqualified_rows"], 1)
        self.assertEqual(dict(both.counts)["station_missing_rows"], 1)
        self.assertEqual(dict(both.counts)["unqualified_airings"], 2)
        blank_with_public_present = guide_window([guide_row(Station="")], self.channels,
                                                NS, NOW, require_station=True)
        self.assertEqual(blank_with_public_present.state, DataState.EMPTY)
        self.assertEqual(dict(blank_with_public_present.counts)["station_unqualified_rows"], 0)
        self.assertEqual(dict(blank_with_public_present.counts)["station_missing_rows"], 1)

    def test_guide_require_station_flag_is_strict_boolean(self) -> None:
        invalid: tuple[object, ...] = (0, 1, None, "true", [], {})
        for flag in invalid:
            with self.subTest(flag=flag):
                batch = guide_window([], self.channels, NS, NOW,
                                     require_station=flag)  # type: ignore[arg-type]
                self.assertEqual((batch.state, batch.items), (DataState.MALFORMED, ()))
                self.assertEqual(batch.warnings, ("invalid_guide_window",))

    def test_guide_namespace_is_never_wildcarded(self) -> None:
        batch = guide_window([guide_row()], self.channels, "other", NOW)
        self.assertEqual((batch.state, batch.items), (DataState.PARTIAL, ()))
        self.assertEqual(dict(batch.counts)["unjoined"], 1)

    def test_guide_join_types_are_strict(self) -> None:
        for changes in ({"DeviceID": 1}, {"ChannelID": 1}, {"ChannelID": ""},
                        {"Number": 7}, {"Name": False}, {"Station": 1}):
            with self.subTest(changes=changes):
                batch = guide_window([guide_row(**changes)], self.channels, NS, NOW)
                self.assertEqual((batch.state, batch.items), (DataState.PARTIAL, ()))
                self.assertEqual(dict(batch.counts)["invalid_rows"], 1)

    def test_guide_half_open_overlap_and_seconds_end_sum(self) -> None:
        airings = [airing(ProgramID="ends-at-start", Time=NOW - 900),
                   airing(ProgramID="overlaps", Time=NOW - 899),
                   airing(ProgramID="starts-at-end", Time=NOW + 1800),
                   airing(ProgramID="last-second", Time=NOW + 1799)]
        batch = guide_window([guide_row(airings)], self.channels, NS, NOW)
        self.assertEqual([e.id for e in batch.items], ["overlaps", "last-second"])
        self.assertEqual(batch.items[0].end, NOW + 1)
        self.assertEqual(batch.items[1].end, NOW + 2699)
        self.assertEqual(batch.items[0].description, "Long")
        self.assertEqual(dict(batch.counts)["filtered"], 2)

    def test_guide_unknown_duration_only_includes_starts_inside(self) -> None:
        airings = []
        null_airings = []
        for index, time in enumerate((NOW - 1, NOW, NOW + 1799, NOW + 1800)):
            row = airing(ProgramID=str(index), Time=time)
            null_airings.append(row | {"Duration": None})
            del row["Duration"]
            airings.append(row)
        batch = guide_window([guide_row(airings)], self.channels, NS, NOW)
        self.assertEqual(guide_window([guide_row(null_airings)], self.channels, NS, NOW),
                         batch)
        self.assertEqual([e.id for e in batch.items], ["1", "2"])
        self.assertTrue(all(e.end is None for e in batch.items))
        self.assertEqual(batch.state, DataState.READY)
        self.assertEqual(dict(batch.counts)["unknown_duration"], 4)
        self.assertIn("unknown_guide_duration", batch.warnings)

    def test_guide_invalid_airings_preserve_valid_subset(self) -> None:
        cases: tuple[dict[str, object], ...] = (
                        {"Time": True}, {"Time": -1}, {"Time": 1.0},
                        {"Time": "123"}, {"Duration": False}, {"Duration": -1},
                        {"Duration": 0}, {"Title": 42},
                        {"Summary": []})
        for changes in cases:
            with self.subTest(changes=changes):
                batch = guide_window([guide_row([airing(), airing(**changes)])],
                                     self.channels, NS, NOW)
                self.assertEqual((batch.state, len(batch.items)), (DataState.PARTIAL, 1))
                self.assertEqual(dict(batch.counts)["invalid_airings"], 1)

    def test_guide_program_identity_required_repeats_and_conflicts(self) -> None:
        missing = airing()
        del missing["ProgramID"]
        batch = guide_window([guide_row([airing(), airing(Time=NOW + 900),
                             airing(), airing(Title="Conflict"), missing,
                             airing(ProgramID=""), airing(ProgramID=7)])],
                             self.channels, NS, NOW)
        self.assertEqual(batch.state, DataState.PARTIAL)
        self.assertEqual([e.start for e in batch.items], [NOW, NOW + 900])
        self.assertEqual(batch.items[0].title, "Synthetic show")
        self.assertEqual(dict(batch.counts)["duplicates"], 2)
        self.assertEqual(dict(batch.counts)["conflicts"], 1)
        self.assertEqual(dict(batch.counts)["missing_program_id"], 2)

    def test_guide_empty_airings_are_not_unmapped(self) -> None:
        batch = guide_window([guide_row([])], self.channels, NS, NOW)
        self.assertEqual((batch.state, batch.items), (DataState.EMPTY, ()))
        self.assertEqual(dict(batch.counts)["joined_rows"], 1)
        self.assertEqual(dict(batch.counts)["unjoined"], 0)
        self.assertEqual(guide_window([], self.channels, NS, NOW).state, DataState.EMPTY)
        all_unjoined = guide_window([guide_row([], DeviceID="missing")],
                                   self.channels, NS, NOW)
        self.assertEqual(all_unjoined.state, DataState.PARTIAL)

    def test_guide_invalid_shape_window_and_row_distinguish_states(self) -> None:
        payloads: tuple[object, ...] = (None, {}, (), {"items": []})
        for payload in payloads:
            self.assertEqual(guide_window(payload, self.channels, NS, NOW).state,
                             DataState.MALFORMED)
        for time in (True, -1, 1.5):
            self.assertEqual(guide_window([], self.channels, NS, time).state,  # type: ignore[arg-type]
                             DataState.MALFORMED)
        for duration in (False, 1799, 1800.0, 1801):
            self.assertEqual(guide_window([], self.channels, NS, NOW, duration).state,  # type: ignore[arg-type]
                             DataState.MALFORMED)
        rows: tuple[object, ...] = (None, {}, {"Channel": {}, "Airings": []},
                                   guide_row("not-list"))
        for row in rows:
            self.assertEqual(guide_window([row], self.channels, NS, NOW).state,
                             DataState.PARTIAL)

    def test_files_synthetic_metadata_fields(self) -> None:
        batch = files_page({"items": [file_row()], "total": 1}, NS)
        self.assertEqual(batch.state, DataState.READY)
        self.assertEqual(batch.items[0], Recording("file", "Episode", "Synthetic show",
                         0, 2, False, NOW, 900, namespace=NS, show_id="series",
                         watched_known=False, resume_known=False))
        self.assertEqual(dict(batch.counts), dict(raw_rows=1, total=1, accepted=1,
                         recordings=1, invalid_rows=0, unknown_duration=0,
                         fractional_duration=0, unknown_watched=1, unknown_resume=1,
                         duplicates=0, conflicts=0))
        sparse = files_page({"items": [file_row(Airing={"Title": "Only show"})],
                             "total": 1}, NS).items[0]
        self.assertEqual((sparse.title, sparse.show, sparse.show_id, sparse.season,
                          sparse.episode), ("Only show", "Only show", None, None, None))

    def test_files_seconds_and_duration_unknowns_without_truncation(self) -> None:
        for raw, expected in ((900, 900), (900.0, 900.0), (0.0, 0.0), (0, 0),
                              (1.25, 1.25), (None, None)):
            batch = files_page({"items": [file_row(CreatedAt=NOW * 1000,
                                Duration=raw)], "total": 1}, NS)
            self.assertEqual(batch.items[0].created, NOW * 1000)
            self.assertEqual(batch.items[0].duration, expected)
            self.assertNotIn("fractional_duration_unknown", batch.warnings)
            self.assertIs(type(batch.items[0].duration), type(expected))
        row = file_row()
        del row["Duration"]
        self.assertIsNone(files_page({"items": [row], "total": 1}, NS).items[0].duration)

    def test_files_invalid_numeric_and_required_fields_are_partial(self) -> None:
        missing_created = file_row()
        del missing_created["CreatedAt"]
        bad: list[object] = [missing_created, None]
        cases: tuple[dict[str, object], ...] = (
            {"ID": 1}, {"ID": ""}, {"CreatedAt": True}, {"CreatedAt": -1},
            {"CreatedAt": 1.0}, {"Duration": True}, {"Duration": -0.5},
            {"Duration": -1}, {"Duration": float("nan")},
            {"Duration": float("inf")}, {"Duration": "900"}, {"Airing": []},
            {"Airing": {"Title": 1}}, {"Airing": {"Title": "Show", "SeasonNumber": True}},
            {"Airing": {"Title": "Show", "EpisodeNumber": -1}},
            {"Airing": {"Title": "Show", "Categories": [1]}})
        bad.extend(file_row(**changes) for changes in cases)
        for row in bad:
            with self.subTest(row=row):
                batch = files_page({"items": [file_row(), row], "total": 2}, NS)
                self.assertEqual((batch.state, len(batch.items)), (DataState.PARTIAL, 1))
                self.assertEqual(dict(batch.counts)["invalid_rows"], 1)

    def test_files_watched_is_unknown_resume_has_explicit_contract(self) -> None:
        cases: tuple[dict[str, object], ...] = ({}, {"PlaybackTime": 0},
                                               {"PlaybackTime": 123})
        for playback_fields in cases:
            batch = files_page({"items": [file_row(**playback_fields, Watched=True,
                                PlayedAt=NOW, UpdatedAt=NOW * 1000)], "total": 1}, NS)
            self.assertEqual(batch.state, DataState.READY)
            item = batch.items[0]
            self.assertEqual((item.watched, item.watched_known), (False, False))
            known = "PlaybackTime" in playback_fields
            self.assertEqual(item.resume_known, known)
            self.assertEqual(item.resume, playback_fields.get("PlaybackTime", 0))
            self.assertEqual(dict(batch.counts)["unknown_resume"], int(not known))
            self.assertEqual("unknown_file_resume" in batch.warnings, not known)
        invalid_playback: tuple[object, ...] = (None, True, False, -1, 1.5, "1")
        for playback_time in invalid_playback:
            with self.subTest(playback_time=playback_time):
                batch = files_page({"items": [file_row(PlaybackTime=playback_time)],
                                    "total": 1}, NS)
                self.assertEqual((batch.state, batch.items), (DataState.PARTIAL, ()))
                self.assertEqual(dict(batch.counts)["invalid_rows"], 1)
                self.assertEqual(batch.warnings, ("invalid_file",))

    def test_files_nested_resume_and_played_at_never_qualify_resume(self) -> None:
        unknown = files_page({"items": [file_row()], "total": 1}, NS)
        nested_values: tuple[object, ...] = (
            {"PlaybackTime": 123}, {"PlaybackTime": 0}, {"PlaybackTime": True},
            None, False, "ignored")
        for nested in nested_values:
            with self.subTest(nested=nested):
                batch = files_page({"items": [file_row(Resume=nested,
                                    PlayedAt=NOW)], "total": 1}, NS)
                self.assertEqual(batch, unknown)
                qualified = files_page({"items": [file_row(Resume=nested,
                                        PlaybackTime=0, PlayedAt=NOW)], "total": 1}, NS)
                self.assertEqual(qualified.state, DataState.READY)
                self.assertEqual((qualified.items[0].resume,
                                  qualified.items[0].resume_known), (0, True))

    def test_files_origin_and_movie_never_inferred_from_paths(self) -> None:
        row = file_row(Path="synthetic/Movies/file.ts", Job="job", Completed=True,
                       Source="dvr", provenance="import", namespace="other",
                       PlaybackTime=17, art="https://secret.invalid/art")
        item = files_page({"items": [row], "total": 1}, NS).items[0]
        self.assertEqual((item.namespace, item.provenance, item.is_movie,
                          item.resume, item.resume_known), (NS, "unknown", False, 17, True))
        movie = file_row(Airing={"Title": "Movie", "Categories": ["Movie"]})
        self.assertTrue(files_page({"items": [movie], "total": 1}, NS).items[0].is_movie)

    def test_files_envelope_totals_validate_page_not_scan_completeness(self) -> None:
        payloads: tuple[object, ...] = (
                        [], (), None, {}, {"items": []}, {"items": [], "total": True},
                        {"items": [], "total": -1}, {"items": [], "total": 0.0},
                        {"items": (), "total": 0})
        for payload in payloads:
            self.assertEqual(files_page(payload, NS).state, DataState.MALFORMED)
        empty = files_page({"items": [], "total": 0}, NS)
        self.assertEqual((empty.state, empty.warnings), (DataState.EMPTY, ()))
        for rows, total, state in (([], 1, DataState.EMPTY),
                                   ([file_row()], 2, DataState.READY)):
            batch = files_page({"items": rows, "total": total}, NS)
            self.assertEqual(batch.state, state)
            self.assertEqual(dict(batch.counts)["total"], total)
            self.assertEqual(dict(batch.counts)["raw_rows"], len(rows))
            self.assertNotIn("files_total_mismatch", batch.warnings)
        inconsistent = files_page({"items": [file_row()], "total": 0}, NS)
        self.assertEqual(inconsistent.state, DataState.PARTIAL)
        self.assertEqual(len(inconsistent.items), 1)
        self.assertIn("files_total_mismatch", inconsistent.warnings)

    def test_files_dedup_first_wins_and_secret_unknowns_ignored(self) -> None:
        secret = "synthetic-secret-marker"
        original = files_page({"items": [file_row()], "total": 1}, NS)
        ignored = files_page({"items": [file_row(media={"url": secret},
                             unknown={"error": secret}, Path=secret)], "total": 1}, NS)
        self.assertEqual(ignored, original)
        duplicate = files_page({"items": [file_row(), file_row()], "total": 2}, NS)
        self.assertEqual((duplicate.state, len(duplicate.items)), (DataState.READY, 1))
        batch = files_page({"items": [file_row(), file_row(Duration=10)], "total": 2}, NS)
        self.assertEqual((batch.state, batch.items[0].duration), (DataState.PARTIAL, 900))
        self.assertEqual(dict(batch.counts)["conflicts"], 1)

    def test_jobs_exact_interval_no_padding_double_application(self) -> None:
        batch = scheduled_jobs([job_row(padding_before=60, padding_after=60,
                                channels=["07.1"], updated_at=NOW * 1000)], NS)
        self.assertEqual(batch.state, DataState.READY)
        self.assertEqual(batch.items[0], Job("job", "Scheduled", NOW - 60,
                         NOW + 960, (), ("program",), padding_known=False))
        self.assertEqual(batch.warnings, ("unknown_job_channel_join", "unknown_job_padding"))
        self.assertEqual(dict(batch.counts), dict(raw_rows=1, accepted=1, jobs=1,
                         invalid_rows=0, unknown_channel_join=1, unknown_padding=1,
                         duplicates=0, conflicts=0))

    def test_jobs_title_fallback_lowercase_item_and_optional_program(self) -> None:
        for changes, expected in (({"name": "", "title": "Title"}, "Title"),
                                   ({"name": ""}, "Synthetic show")):
            self.assertEqual(scheduled_jobs([job_row(**changes)], NS).items[0].title, expected)
        row = {"id": "job", "start_time": NOW, "end_time": NOW + 10,
               "item": {"title": "Only item"}, "Airing": {"ProgramID": "ignored"}}
        item = scheduled_jobs([row], NS).items[0]
        self.assertEqual((item.title, item.event_ids, item.candidates), ("Only item", (), ()))

    def test_jobs_invalid_rows_are_partial_with_valid_subset(self) -> None:
        cases: tuple[dict[str, object], ...] = (
                        {"id": 1}, {"id": ""}, {"start_time": True},
                        {"start_time": -1}, {"end_time": NOW - 60},
                        {"end_time": 1.0}, {"item": []}, {"name": 1},
                        {"item": {"program_id": 7}})
        for changes in cases:
            with self.subTest(changes=changes):
                batch = scheduled_jobs([job_row(), job_row(**changes)], NS)
                self.assertEqual((batch.state, len(batch.items)), (DataState.PARTIAL, 1))
                self.assertEqual(dict(batch.counts)["invalid_rows"], 1)
        for row in ({}, None, {"id": "job", "start_time": NOW, "end_time": NOW + 1}):
            self.assertEqual(scheduled_jobs([row], NS).state, DataState.PARTIAL)

    def test_jobs_empty_and_malformed_outer_shapes(self) -> None:
        batch = scheduled_jobs([], NS)
        self.assertEqual((batch.state, batch.items, batch.warnings), (DataState.EMPTY, (), ()))
        self.assertTrue(all(count == 0 for _, count in batch.counts))
        payloads: tuple[object, ...] = (None, {}, (), {"items": []}, "error")
        for payload in payloads:
            self.assertEqual(scheduled_jobs(payload, NS).state, DataState.MALFORMED)

    def test_jobs_dedup_and_all_routes_ignore_unknown_secret_fields(self) -> None:
        exact = scheduled_jobs([job_row(), job_row()], NS)
        self.assertEqual((exact.state, len(exact.items)), (DataState.READY, 1))
        conflict = scheduled_jobs([job_row(), job_row(end_time=NOW + 1000)], NS)
        self.assertEqual((conflict.state, conflict.items[0].end), (DataState.PARTIAL, NOW + 960))
        secret = "synthetic-secret-marker"
        self.assertEqual(scheduled_jobs([job_row(error=secret, art=secret)], NS),
                         scheduled_jobs([job_row()], NS))
        self.assertEqual(public_channels([channel(logo=secret, error=secret)], NS),
                         public_channels([channel()], NS))
        self.assertEqual(guide_window([guide_row([airing(Image=secret, error=secret)])],
                                     self.channels, NS, NOW),
                         guide_window([guide_row()], self.channels, NS, NOW))
        batches: list[MetadataBatch[Channel] | MetadataBatch[Event] |
                      MetadataBatch[Recording] | MetadataBatch[Job]] = [
                   public_channels([channel(name=secret), channel(id=1)], NS),
                   guide_window([guide_row([airing(Title=secret), airing(Time=secret)])],
                                self.channels, NS, NOW),
                   files_page({"items": [file_row(ID=secret), {"error": secret}], "total": 2}, NS),
                   scheduled_jobs([job_row(name=secret), {"error": secret}], NS)]
        for batch in batches:
            self.assertNotIn(secret, repr((batch.warnings, batch.counts)))
            self.assertTrue(all(type(count) is int and count >= 0 for _, count in batch.counts))
        self.assertIsInstance(self.channels[0], Channel)
        self.assertIsInstance(guide_window([guide_row()], self.channels, NS, NOW).items[0], Event)


if __name__ == "__main__":
    unittest.main()
