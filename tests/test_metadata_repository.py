"""Synthetic worker-boundary tests: no HTTP opener, Kodi, configuration or GUI."""

from dataclasses import FrozenInstanceError, fields, replace
import json
import unittest
from unittest.mock import patch

from channels_dvr import metadata_models as dto
from channels_dvr.metadata_http import MetadataError
from channels_dvr.metadata_models import MetadataBatch
from channels_dvr.metadata_repository import (
    DevelopmentSnapshot, MetadataClient, collect_recordings, load_metadata,
    safe_snapshot_summary,
)
from channels_dvr.normalization import DataState

NOW = 1_800_000_000


def channel(source: str = "source-a", *, hidden: bool = False,
            station: str | None = "station") -> dict[str, object]:
    # Qualified EPG fixtures intentionally include a nonempty public station ID.
    row: dict[str, object] = dict(id="001", source_id=source, number="07.1",
                                  name="Synthetic", hidden=hidden)
    if station is not None:
        row["station_id"] = station
    return row


def guide(source: str = "source-a", *, start: int = NOW, duration: int = 7200,
          title: str = "Synthetic", station: str | None = "station") -> dict[str, object]:
    key: dict[str, object] = {"DeviceID": source, "ChannelID": "001"}
    if station is not None:
        key["Station"] = station
    return {"Channel": key,
            "Airings": [{"ProgramID": "program", "Title": title,
                         "Time": start, "Duration": duration}]}


def file_row(identity: str = "file", *, title: str = "Synthetic") -> dict[str, object]:
    return {"ID": identity, "CreatedAt": NOW, "Duration": 900,
            "Airing": {"Title": title}}


def page(*rows: object, total: int = 0) -> dict[str, object]:
    return {"items": list(rows), "total": total}


class FakeClient:
    namespace = "synthetic"

    def __init__(self) -> None:
        self.status: object = {"identity_verified": True, "version": "2026.08.07.0346"}
        self.channels: object = [channel()]
        self.pages: list[object] = [page()]
        self.windows: list[object] = [[]]
        self.jobs: object = []
        self.calls: list[tuple[str, int, int]] = []

    @staticmethod
    def result(value: object) -> object:
        if isinstance(value, Exception):
            raise value
        return value

    def verify_status(self) -> object:
        self.calls.append(("status", 0, 0))
        return self.result(self.status)

    def public_channels(self) -> object:
        self.calls.append(("channels", 0, 0))
        return self.result(self.channels)

    def guide_window(self, time_s: int, duration: int = 1800) -> object:
        self.calls.append(("guide", time_s, duration))
        return self.result(self.windows.pop(0))

    def files_page(self, limit: int = 100, offset: int = 0) -> object:
        self.calls.append(("files", limit, offset))
        return self.result(self.pages.pop(0))

    def scheduled_jobs(self) -> object:
        self.calls.append(("jobs", 0, 0))
        return self.result(self.jobs)


class MetadataRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        guard = patch("socket.socket", side_effect=AssertionError("No network"))
        guard.start()
        self.addCleanup(guard.stop)
        self.client = FakeClient()
        # Static structural conformance, without constructing any real transport.
        self.protocol: MetadataClient = self.client

    def assert_counts(self, counts: tuple[tuple[str, int], ...]) -> None:
        self.assertTrue(all(type(value) is int for _, value in counts))

    def test_empty_scan_is_complete_but_not_snapshot_consistent(self) -> None:
        batch = collect_recordings(self.protocol)
        self.assertEqual((batch.state, batch.items), (DataState.EMPTY, ()))
        self.assertEqual(dict(batch.counts), dict(pages=1, raw_rows=0, unique=0,
                         total=0, complete=1, duplicates=0, conflicts=0, invalid_rows=0))
        self.assertEqual(batch.warnings, ("offset_scan_not_snapshot_consistent",))

    def test_offsets_advance_by_raw_rows_not_deduplicated_rows(self) -> None:
        self.client.pages = [page(file_row("a"), file_row("a"), total=4),
                             page(file_row("b"), file_row("c"), total=4)]
        batch = collect_recordings(self.client, page_size=2)
        self.assertEqual(self.client.calls, [("files", 2, 0), ("files", 2, 2)])
        self.assertEqual([r.id for r in batch.items], ["a", "b", "c"])
        self.assertEqual(batch.state, DataState.READY)
        self.assertEqual(dict(batch.counts)["raw_rows"], 4)
        self.assertEqual(dict(batch.counts)["complete"], 1)

    def test_repeated_ids_stop_even_at_apparent_terminal_offset(self) -> None:
        self.client.pages = [page(file_row(), total=2), page(file_row(), total=2)]
        batch = collect_recordings(self.client, page_size=1)
        self.assertEqual((batch.state, len(batch.items)), (DataState.PARTIAL, 1))
        self.assertIn("files_no_progress", batch.warnings)
        self.assertEqual(dict(batch.counts)["complete"], 0)

    def test_conflicting_duplicates_first_wins_and_are_partial(self) -> None:
        self.client.pages = [page(file_row(), total=2),
                             page(file_row(title="Different"), total=2)]
        batch = collect_recordings(self.client, page_size=1)
        self.assertEqual(batch.state, DataState.PARTIAL)
        self.assertEqual(batch.items[0].title, "Synthetic")
        self.assertEqual(dict(batch.counts)["conflicts"], 1)
        self.assertIn("conflicting_file", batch.warnings)

    def test_changing_totals_retain_items_without_completeness(self) -> None:
        self.client.pages = [page(file_row("a"), total=3),
                             page(file_row("b"), total=2)]
        batch = collect_recordings(self.client, page_size=1)
        self.assertEqual((batch.state, len(batch.items)), (DataState.PARTIAL, 2))
        self.assertIn("files_total_changed", batch.warnings)
        self.assertEqual((dict(batch.counts)["total"], dict(batch.counts)["complete"]), (3, 0))

    def test_early_empty_is_partial_at_zero_or_later_offset(self) -> None:
        prefixes: tuple[list[object], ...] = ([], [page(file_row(), total=3)])
        for prefix in prefixes:
            with self.subTest(prefix=bool(prefix)):
                client = FakeClient()
                client.pages = prefix + [page(total=3)]
                batch = collect_recordings(client, page_size=1)
                self.assertEqual(batch.state, DataState.PARTIAL)
                self.assertIn("files_early_empty", batch.warnings)
                self.assertEqual(dict(batch.counts)["complete"], 0)

    def test_page_shape_and_raw_limit_checked_before_normalization(self) -> None:
        bad_pages: tuple[object, ...] = (
            [], {}, {"items": [], "total": True}, {"items": [], "total": -1},
            {"items": (), "total": 0}, page(file_row(), file_row(), total=2))
        for bad in bad_pages:
            for prior in (False, True):
                with self.subTest(bad=bad, prior=prior):
                    client = FakeClient()
                    client.pages = ([page(file_row("a"), total=3)] if prior else []) + [bad]
                    with patch("channels_dvr.metadata_repository.dto.files_page",
                               wraps=dto.files_page) as normalize:
                        batch = collect_recordings(client, page_size=1)
                    self.assertEqual(normalize.call_count, int(prior))
                    self.assertEqual(batch.state, DataState.PARTIAL if prior else DataState.MALFORMED)
                    self.assertLessEqual(len(batch.items), 1)
                    self.assert_counts(batch.counts)

    def test_normalizer_schema_failures_and_mismatch_never_complete(self) -> None:
        for payload in (page({"ID": "bad"}, total=1), page(file_row(), total=0)):
            self.client.pages = [payload]
            batch = collect_recordings(self.client)
            self.assertNotIn(batch.state, (DataState.READY, DataState.EMPTY))
            self.assertEqual(dict(batch.counts)["complete"], 0)
        # Explicit normalizer MALFORMED: first failure versus retained prior data.
        for prior in (False, True):
            client = FakeClient()
            client.pages = [page(file_row(), total=2)] if prior else []
            client.pages.append(page(total=2))
            outcomes = ([dto.files_page(page(file_row(), total=2), client.namespace)] if prior else [])
            outcomes += [MetadataBatch((), DataState.MALFORMED, ("invalid_files_shape",))]
            with patch("channels_dvr.metadata_repository.dto.files_page", side_effect=outcomes):
                batch = collect_recordings(client, page_size=1)
            self.assertEqual(batch.state, DataState.PARTIAL if prior else DataState.MALFORMED)

    def test_page_cap_bounds_rows_and_reads(self) -> None:
        self.client.pages = [page(*(file_row(str(i * 100 + j)) for j in range(100)),
                                  total=1001) for i in range(10)]
        batch = collect_recordings(self.client, page_size=100, max_pages=10)
        self.assertEqual((batch.state, len(batch.items)), (DataState.PARTIAL, 1000))
        self.assertEqual(len(self.client.calls), 10)
        self.assertEqual(dict(batch.counts)["raw_rows"], 1000)
        self.assertIn("files_page_limit", batch.warnings)

    def test_transport_error_classification_and_partial_retention(self) -> None:
        cases = {"unsupported": DataState.UNSUPPORTED, "content_type": DataState.MALFORMED,
                 "content_encoding": DataState.MALFORMED, "malformed_json": DataState.MALFORMED,
                 "response_too_large": DataState.MALFORMED, "network_error": DataState.OUTAGE,
                 "http_error": DataState.OUTAGE, "response_timeout": DataState.OUTAGE,
                 "request_budget": DataState.OUTAGE, "secret://private/path": DataState.OUTAGE}
        for code, expected in cases.items():
            for prior in (False, True):
                with self.subTest(code=code, prior=prior):
                    client = FakeClient()
                    error = MetadataError(code)
                    error.args = ("secret://private/path?title=Personal",)
                    client.pages = ([page(file_row(), total=2)] if prior else []) + [error]
                    batch = collect_recordings(client, page_size=1)
                    self.assertEqual(batch.state, DataState.PARTIAL if prior else expected)
                    self.assertNotIn("secret", repr(batch.warnings))
                    self.assertEqual(dict(batch.counts)["complete"], 0)
            client = FakeClient()
            client.jobs = MetadataError(code)
            snapshot = load_metadata(client, time_s=NOW)
            self.assertEqual(snapshot.jobs.state, expected)
            self.assertEqual(snapshot.recordings.state, DataState.EMPTY)

    def test_invalid_limits_rejected_without_reads(self) -> None:
        for value in (True, False, 0, -1, 101, 1.0, "2"):
            with self.subTest(value=value), self.assertRaises(MetadataError):
                collect_recordings(self.client, page_size=value)  # type: ignore[arg-type]
        for value in (True, 0, 11, 2.0):
            with self.assertRaises(MetadataError):
                collect_recordings(self.client, max_pages=value)  # type: ignore[arg-type]
        self.assertEqual(self.client.calls, [])

    def test_identity_failure_propagates_same_exception_before_other_reads(self) -> None:
        error = MetadataError("identity_mismatch")
        self.client.status = error
        with self.assertRaises(MetadataError) as caught:
            load_metadata(self.client, time_s=NOW)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.client.calls, [("status", 0, 0)])

    def test_status_requires_minimal_verified_dictionary(self) -> None:
        statuses: tuple[object, ...] = (
            None, [], {}, {"identity_verified": 1, "version": "v"},
            {"identity_verified": True, "version": 1},
            {"identity_verified": True, "version": ""})
        for status in statuses:
            client = FakeClient()
            client.status = status
            with self.subTest(status=status), self.assertRaises(MetadataError):
                load_metadata(client, time_s=NOW)
            self.assertEqual(client.calls, [("status", 0, 0)])

    def test_empty_snapshot_is_frozen_and_has_requested_horizon(self) -> None:
        for inventory in ([], [channel(hidden=True)]):
            with self.subTest(all_hidden=bool(inventory)):
                client = FakeClient()
                client.channels = inventory
                with patch("channels_dvr.metadata_repository.time.time", return_value=NOW + 10):
                    snapshot = load_metadata(client, time_s=NOW, guide_windows=4)
                self.assertEqual([f.name for f in fields(DevelopmentSnapshot)],
                                 ["namespace", "version", "fetched_at", "horizon", "channels",
                                   "events", "recordings", "jobs", "artwork"])
                art_field = fields(DevelopmentSnapshot)[-1]
                self.assertFalse(art_field.compare)
                self.assertFalse(art_field.repr)
                self.assertTrue(art_field.kw_only)
                with self.assertRaises(FrozenInstanceError):
                    setattr(snapshot, "namespace", "other")
                self.assertEqual(snapshot.horizon, (NOW, NOW + 7200))
                self.assertEqual(snapshot.fetched_at, NOW + 10)
                self.assertEqual(snapshot.channels.state, DataState.EMPTY)
                self.assertEqual((snapshot.events.state, snapshot.events.items), (DataState.EMPTY, ()))
                self.assertEqual(snapshot.events.warnings, ("guide_no_visible_channels",))
                self.assertEqual(dict(snapshot.events.counts), dict(
                    requested_windows=4, completed_windows=0, rows=0, events=0,
                    visible_filtered=0, duplicates=0, conflicts=0))
                self.assertEqual(snapshot.recordings.state, DataState.EMPTY)
                self.assertEqual(snapshot.jobs.state, DataState.EMPTY)
                self.assertEqual([c[0] for c in client.calls], ["status", "channels", "files", "jobs"])
                self.assertIn("guide_no_visible_channels", json.dumps(safe_snapshot_summary(snapshot)))

    def test_populated_snapshot_preserves_unknown_flags(self) -> None:
        self.client.windows = [[guide()]]
        self.client.pages = [page(file_row(), total=1)]
        self.client.jobs = [{"id": "job", "name": "Synthetic", "start_time": NOW,
                             "end_time": NOW + 900}]
        snapshot = load_metadata(self.client, time_s=NOW)
        self.assertTrue(all(b.state == DataState.READY for b in
                            (snapshot.channels, snapshot.events, snapshot.recordings, snapshot.jobs)))
        recording = snapshot.recordings.items[0]
        self.assertEqual((recording.watched_known, recording.resume_known), (False, False))
        self.assertFalse(snapshot.jobs.items[0].padding_known)
        self.assertEqual(snapshot.jobs.items[0].candidates, ())
        self.assertEqual([c[0] for c in self.client.calls],
                         ["status", "channels", "guide", "files", "jobs"])

    def test_four_windows_clip_join_full_keys_dedup_and_first_wins(self) -> None:
        self.client.channels = [channel(), channel("source-b", hidden=True), channel("source-c")]
        rows = [guide(), guide("source-b"), guide("source-c"),
                guide(start=NOW - 900, duration=900), guide(start=NOW + 7200)]
        self.client.windows = [rows, rows, [guide(title="Conflict")], rows]
        snapshot = load_metadata(self.client, time_s=NOW, guide_windows=4)
        batch = snapshot.events
        self.assertEqual(batch.state, DataState.PARTIAL)
        self.assertEqual(len(batch.items), 2)
        self.assertEqual({e.channel.source_id for e in batch.items}, {"source-a", "source-c"})
        self.assertTrue(all(e.title == "Synthetic" for e in batch.items))
        self.assertNotIn("unknown_guide_channel", batch.warnings)
        self.assertIn("conflicting_event", batch.warnings)
        self.assertEqual(dict(batch.counts)["completed_windows"], 4)
        self.assertEqual([c for c in self.client.calls if c[0] == "guide"],
                         [("guide", NOW + i * 1800, 1800) for i in range(4)])
        self.assertEqual(snapshot.horizon, (NOW, NOW + 7200))

    def test_guide_failure_stops_sequence_and_resources_are_independent(self) -> None:
        for failure, expected in ((MetadataError("unsupported"), DataState.UNSUPPORTED),
                                  (MetadataError("network_error"), DataState.OUTAGE),
                                  ({"secret": "private"}, DataState.MALFORMED),
                                  ([{"Channel": {}}], DataState.PARTIAL)):
            for prior in (False, True):
                client = FakeClient()
                client.windows = ([[guide()]] if prior else []) + [failure]
                client.jobs = MetadataError("unsupported")
                snapshot = load_metadata(client, time_s=NOW, guide_windows=4)
                self.assertEqual(snapshot.events.state, DataState.PARTIAL if prior else expected)
                self.assertEqual(dict(snapshot.events.counts)["completed_windows"], int(prior))
                self.assertEqual(len([c for c in client.calls if c[0] == "guide"]), 1 + int(prior))
                self.assertEqual(snapshot.recordings.state, DataState.EMPTY)
                self.assertEqual(snapshot.jobs.state, DataState.UNSUPPORTED)

    def test_all_unjoined_guide_is_partial_without_qualified_events(self) -> None:
        self.client.windows = [[guide("unknown-source")]]
        snapshot = load_metadata(self.client, time_s=NOW, guide_windows=4)
        self.assertEqual((snapshot.events.state, snapshot.events.items), (DataState.PARTIAL, ()))
        self.assertEqual(snapshot.events.warnings, ("unknown_guide_channel",))
        counts = dict(snapshot.events.counts)
        self.assertEqual((counts["rows"], counts["unjoined"], counts["invalid_rows"]), (1, 1, 0))
        self.assertEqual(counts["completed_windows"], 0)
        self.assertEqual([c for c in self.client.calls if c[0] == "guide"], [("guide", NOW, 1800)])
        self.assertEqual(snapshot.recordings.state, DataState.EMPTY)
        self.assertEqual(snapshot.jobs.state, DataState.EMPTY)
        self.assert_counts(snapshot.events.counts)

    def test_bad_channels_retain_failure_and_skip_guide_without_fabricated_keys(self) -> None:
        payloads: tuple[tuple[object, DataState], ...] = (
            ({}, DataState.MALFORMED), ([{"id": "bad"}], DataState.MALFORMED),
            (MetadataError("network_error"), DataState.OUTAGE),
            (MetadataError("content_type"), DataState.MALFORMED),
            (MetadataError("unsupported"), DataState.UNSUPPORTED))
        for payload, expected in payloads:
            client = FakeClient()
            client.channels = payload
            snapshot = load_metadata(client, time_s=NOW)
            self.assertEqual(snapshot.events.items, ())
            self.assertEqual(snapshot.channels.state, expected)
            self.assertEqual(snapshot.events.state, expected)
            self.assertEqual(snapshot.events.warnings, ("guide_channels_unavailable",))
            self.assertFalse(any(c[0] == "guide" for c in client.calls))
            self.assertEqual(snapshot.recordings.state, DataState.EMPTY)
            self.assertEqual(snapshot.jobs.state, DataState.EMPTY)

    def test_station_mapping_setup_gap_is_warning_not_global_partial(self) -> None:
        self.client.channels = ([channel(f"mapped-{i}") for i in range(5)]
                                + [channel(f"gap-{i}", station="") for i in range(8)])
        self.client.windows = [[guide(f"mapped-{i}") for i in range(5)]
                               + [guide(f"gap-{i}", station="") for i in range(8)]]
        snapshot = load_metadata(self.client, time_s=NOW)
        self.assertEqual(snapshot.events.state, DataState.READY)
        self.assertEqual(len(snapshot.channels.items), 13)
        self.assertEqual(len(snapshot.events.items), 5)
        self.assertIn("guide_station_missing", snapshot.events.warnings)
        self.assertEqual(dict(snapshot.events.counts)["station_missing_rows"], 8)
        self.assertEqual(dict(snapshot.events.counts)["unqualified_airings"], 8)

    def test_omitted_station_placeholders_are_not_qualified_snapshot_coverage(self) -> None:
        inventory = ([channel(f"mapped-{i}") for i in range(5)]
                     + [channel(f"gap-{i}", station=None) for i in range(8)])
        rows: list[object] = []
        for i in range(13):
            source = f"mapped-{i}" if i < 5 else f"gap-{i - 5}"
            # Guide omits Station on all rows: only five public IDs qualify.
            row = guide(source, station=None)
            row["Airings"] = [dict(ProgramID=f"program-{j}", Title="Synthetic",
                                    Time=NOW + j * 900, Duration=7200) for j in range(2)]
            rows.append(row)
        all_channels = dto.public_channels(inventory, self.client.namespace, include_hidden=True)
        # Generic sparse DTO behavior remains available outside this repository.
        generic = dto.guide_window(rows, all_channels.items, self.client.namespace, NOW)
        self.assertEqual((generic.state, len(generic.items)), (DataState.READY, 26))
        for windows in (1, 2):
            with self.subTest(windows=windows):
                client = FakeClient()
                client.channels = inventory
                client.windows = [rows for _ in range(windows)]
                snapshot = load_metadata(client, time_s=NOW, guide_windows=windows)
                self.assertEqual(snapshot.channels.state, DataState.READY)
                self.assertEqual(len(snapshot.channels.items), 13)
                self.assertEqual(sum("Guide station unqualified" in c.groups
                                     for c in snapshot.channels.items), 8)
                self.assertEqual(snapshot.events.state, DataState.READY)
                self.assertEqual(len(snapshot.events.items), 10)
                self.assertEqual({e.channel for e in snapshot.events.items},
                                 {c.key for c in snapshot.channels.items
                                  if "Guide station present" in c.groups})
                self.assertEqual(snapshot.events.warnings, ("guide_station_unqualified",))
                counts = dict(snapshot.events.counts)
                for key, value in dict(rows=13, joined_rows=13, airings_seen=26,
                                       accepted=10, station_unqualified_rows=8,
                                       unqualified_airings=16).items():
                    self.assertEqual(counts[key], value * windows)
                self.assertEqual(counts["events"], 10)
                self.assertEqual(counts["station_missing_rows"], 0)
                self.assertEqual(counts["completed_windows"], windows)
                self.assertEqual(snapshot.horizon, (NOW, NOW + windows * 1800))
                self.assertEqual([c for c in client.calls if c[0] == "guide"],
                                 [("guide", NOW + i * 1800, 1800) for i in range(windows)])
                summary = json.dumps(safe_snapshot_summary(snapshot))
                self.assertIn('"guide_station_unqualified"', summary)
                self.assertIn(f'"station_unqualified_rows": {8 * windows}', summary)
                self.assertNotIn("mapped-", summary)
                self.assertNotIn("gap-", summary)
                self.assert_counts(snapshot.events.counts)

    def test_summary_excludes_content_ids_origins_and_error_strings(self) -> None:
        self.client.windows = [[guide(title="PRIVATE_TITLE")]]
        self.client.pages = [page(file_row("PRIVATE_ID", title="PRIVATE_TITLE"), total=1)]
        self.client.jobs = MetadataError("network_error")
        snapshot = load_metadata(self.client, time_s=NOW)
        summary = safe_snapshot_summary(snapshot)
        for version in ("2026.08.07.0346", "2026.08.07.0346-pre", "2026.08.07.0346-rc1"):
            with self.subTest(version=version):
                self.assertEqual(safe_snapshot_summary(replace(snapshot, version=version))["version"], version)
        for version in ("unknown", "192.0.2.1", "PRIVATE_TOKEN_abc123", "20.26.08.07",
                        "2026.13.07.0346", "2026.08.07.2460", "2026.08.07.0346-PRIVATE_TOKEN"):
            with self.subTest(version=version):
                redacted = safe_snapshot_summary(replace(snapshot, version=version))
                self.assertEqual(redacted["version"], "unavailable")
                self.assertNotIn(json.dumps(version), json.dumps(redacted))
        encoded = json.dumps(summary)
        for secret in ("PRIVATE_TITLE", "PRIVATE_ID", "source-a", "synthetic", "origin"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(summary["requested_horizon"], (NOW, NOW + 1800))
        poisoned = replace(snapshot, version="https://private/path",
                           channels=MetadataBatch((), DataState.OUTAGE,
                                                  ("https://private/path", "metadata_outage"),
                                                  (("private/path", 1), ("raw_rows", True))))
        safe = safe_snapshot_summary(poisoned)
        self.assertEqual(safe["version"], "unavailable")
        self.assertNotIn("private", json.dumps(safe))
        for batch in (snapshot.channels, snapshot.events, snapshot.recordings, snapshot.jobs):
            self.assert_counts(batch.counts)

    def test_window_arguments_and_maximum_request_budget(self) -> None:
        invalid_args: tuple[dict[str, object], ...] = (
            {"time_s": True}, {"time_s": -1}, {"time_s": 1.0},
            {"time_s": NOW, "guide_windows": True},
            {"time_s": NOW, "guide_windows": 0},
            {"time_s": NOW, "guide_windows": 5})
        for args in invalid_args:
            with self.assertRaises(MetadataError):
                load_metadata(self.client, **args)  # type: ignore[arg-type]
        self.assertEqual(self.client.calls, [])
        self.client.windows = [[] for _ in range(4)]
        self.client.pages = [page(file_row(str(i)), total=10) for i in range(10)]
        snapshot = load_metadata(self.client, time_s=NOW, guide_windows=4,
                                 page_size=1, max_pages=10)
        self.assertEqual(len(self.client.calls), 17)
        self.assertEqual(snapshot.recordings.state, DataState.READY)
        self.assertEqual(dict(snapshot.recordings.counts)["complete"], 1)


if __name__ == "__main__":
    unittest.main()
