"""Bounded read-only composition for a worker's plain-data boundary (no GUI).

The requested guide horizon is not universal server coverage. Offset scans are
not atomic snapshots: even a complete stable-total scan cannot prove consistency
under concurrent server changes, and partial data must not replace a full cache.
Identity verification belongs to load_metadata; standalone collectors require an
already verified client. Sort/order and the raw request budget belong to client.
"""

from dataclasses import dataclass, field
import time
from typing import Protocol, TypeVar

from . import metadata_models as dto
from .metadata_http import MetadataError
from .metadata_models import MetadataBatch
from .models import Channel, ChannelKey, Event, Job, Recording
from .normalization import DataState
from .newbuild_compatibility import parse_build
from .artwork import ArtBuilder, ArtInventory

T = TypeVar("T")
_SUCCESS = (DataState.READY, DataState.EMPTY)
_SCAN_WARNING = "offset_scan_not_snapshot_consistent"
_MALFORMED_CODES = frozenset({
    "content_type", "content_encoding", "malformed_json", "response_too_large",
})


class MetadataClient(Protocol):
    """Only the five closed reads; transport origins are deliberately absent."""

    @property
    def namespace(self) -> str: ...

    def verify_status(self) -> object: ...
    def public_channels(self) -> object: ...
    def guide_window(self, time_s: int, duration: int = 1800) -> object: ...
    def files_page(self, limit: int = 100, offset: int = 0) -> object: ...
    def scheduled_jobs(self) -> object: ...


@dataclass(frozen=True)
class DevelopmentSnapshot:
    namespace: str
    version: str
    fetched_at: int
    horizon: tuple[int, int]
    channels: MetadataBatch[Channel]
    events: MetadataBatch[Event]
    recordings: MetadataBatch[Recording]
    jobs: MetadataBatch[Job]
    artwork: ArtInventory = field(default_factory=ArtInventory, compare=False, repr=False, kw_only=True)


def _limits(page_size: int, max_pages: int) -> None:
    if (type(page_size) is not int or not 1 <= page_size <= 100
            or type(max_pages) is not int or not 1 <= max_pages <= 10):
        raise MetadataError("invalid_argument")


def _warnings(target: list[str], codes: tuple[str, ...]) -> None:
    for code in codes:
        if code not in target:
            target.append(code)


def _failure(error: MetadataError) -> tuple[DataState, str]:
    # Never retain exception messages, URLs, HTTP bodies or arbitrary error codes.
    if error.code == "unsupported":
        return DataState.UNSUPPORTED, "metadata_unsupported"
    if error.code in _MALFORMED_CODES:
        return DataState.MALFORMED, "metadata_malformed"
    return DataState.OUTAGE, "metadata_outage"


def _batch(items: tuple[T, ...], state: DataState, warnings: list[str],
           counts: dict[str, int]) -> MetadataBatch[T]:
    return MetadataBatch(items, state, tuple(warnings), tuple(counts.items()))


def collect_recordings(client: MetadataClient, *, page_size: int = 100,
                       max_pages: int = 4, art: ArtBuilder | None = None) -> MetadataBatch[Recording]:
    """Scan at most 1000 raw rows; advance offsets by raw, not deduplicated, rows.

    pages counts attempted reads, total is the first valid reported total, and
    complete is 1 only for a fully traversed READY/EMPTY chain. It is not atomic.
    """
    _limits(page_size, max_pages)
    counts = dict(pages=0, raw_rows=0, unique=0, total=0, complete=0,
                  duplicates=0, conflicts=0, invalid_rows=0)
    warnings = [_SCAN_WARNING]
    records: dict[tuple[str, str, str], Recording] = {}
    total: int | None = None
    offset = 0
    state = DataState.PARTIAL
    for _ in range(max_pages):
        counts["pages"] += 1
        try:
            payload = client.files_page(limit=page_size, offset=offset)
        except MetadataError as error:
            state, warning = _failure(error)
            warnings.append(warning)
            if records:
                state = DataState.PARTIAL
            break
        # Enforce the raw envelope and limit BEFORE normalization/deduplication.
        rows = payload.get("items") if isinstance(payload, dict) else None
        reported = payload.get("total") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            counts["raw_rows"] += len(rows)
        if (not isinstance(rows, list) or len(rows) > page_size
                or type(reported) is not int or reported < 0):
            warnings.append("invalid_files_page")
            state = DataState.PARTIAL if records else DataState.MALFORMED
            break
        page = dto.files_page(payload, client.namespace, art=art)
        _warnings(warnings, page.warnings)
        counts["invalid_rows"] += dict(page.counts).get("invalid_rows", 0)
        if page.state == DataState.MALFORMED:
            state = DataState.PARTIAL if records else DataState.MALFORMED
            break
        changed = total is not None and total != reported
        if total is None:
            total = reported
            counts["total"] = total
        added = 0
        for item in page.items:
            key = (item.namespace, item.provenance, item.id)
            if key in records:
                counts["duplicates"] += 1
                if records[key] != item:
                    counts["conflicts"] += 1
                    _warnings(warnings, ("conflicting_file",))
            else:
                records[key] = item
                added += 1
        counts["duplicates"] += dict(page.counts).get("duplicates", 0)
        counts["conflicts"] += dict(page.counts).get("conflicts", 0)
        if changed:
            warnings.append("files_total_changed")
            break
        if page.state not in _SUCCESS:
            state = (DataState.PARTIAL if records else DataState.MALFORMED)
            break
        if counts["conflicts"]:
            break
        if rows and not added:
            warnings.append("files_no_progress")
            break
        offset += len(rows)
        if offset > total:
            warnings.append("files_total_mismatch")
            break
        if offset == total:
            counts["complete"] = 1
            state = DataState.READY if records else DataState.EMPTY
            break
        if not rows:
            warnings.append("files_early_empty")
            break
    else:
        warnings.append("files_page_limit")
    counts["unique"] = len(records)
    return _batch(tuple(records.values()), state, warnings, counts)


def _guide(client: MetadataClient, all_channels: MetadataBatch[Channel],
           visible: MetadataBatch[Channel], time_s: int,
            windows: int, art: ArtBuilder | None = None) -> MetadataBatch[Event]:
    counts = dict(requested_windows=windows, completed_windows=0, rows=0,
                  events=0, visible_filtered=0, duplicates=0, conflicts=0)
    warnings: list[str] = []
    if all_channels.state not in _SUCCESS:
        return _batch((), all_channels.state, ["guide_channels_unavailable"], counts)
    if not visible.items:
        return _batch((), DataState.EMPTY, ["guide_no_visible_channels"], counts)
    visible_keys = {c.key for c in visible.items}
    events: dict[tuple[ChannelKey, str, int], Event] = {}
    state = DataState.EMPTY
    partial = False
    for i in range(windows):
        try:
            payload = client.guide_window(time_s + i * 1800, duration=1800)
        except MetadataError as error:
            state, warning = _failure(error)
            _warnings(warnings, (warning,))
            if events:
                state = DataState.PARTIAL
            break
        page = dto.guide_window(payload, all_channels.items, client.namespace,
                                time_s + i * 1800, duration=1800, require_station=True, art=art)
        _warnings(warnings, page.warnings)
        for key, value in page.counts:
            # Keep normalizer row observations, including station setup gaps;
            # summed window counts do not represent unique channel coverage.
            target = "rows" if key == "raw_rows" else key
            counts[target] = counts.get(target, 0) + value
        for event in page.items:
            if event.channel not in visible_keys:
                counts["visible_filtered"] += 1
                continue
            identity = (event.channel, event.id, event.start)
            if identity in events:
                counts["duplicates"] += 1
                if events[identity] != event:
                    counts["conflicts"] += 1
                    _warnings(warnings, ("conflicting_event",))
                    partial = True
            else:
                events[identity] = event
        if page.state not in _SUCCESS:
            # A schema/join failure terminates the sequence without retrying.
            state = DataState.PARTIAL if events else page.state
            break
        counts["completed_windows"] += 1
        state = DataState.PARTIAL if partial else DataState.READY if events else DataState.EMPTY
    counts["events"] = len(events)
    return _batch(tuple(events.values()), state, warnings, counts)


def load_metadata(client: MetadataClient, *, time_s: int, guide_windows: int = 1,
                  page_size: int = 100, max_pages: int = 4) -> DevelopmentSnapshot:
    """Explicit status first, then independent bounded resources, without retries.

    At maximum this makes 17 reads, within the transport's default budget of 24.
    No implicit device/source browsing, cache replacement or GUI binding occurs.
    """
    _limits(page_size, max_pages)
    if (type(time_s) is not int or time_s < 0 or type(guide_windows) is not int
            or not 1 <= guide_windows <= 4):
        raise MetadataError("invalid_argument")
    status = client.verify_status()  # MetadataError must propagate unchanged.
    if (not isinstance(status, dict) or status.get("identity_verified") is not True
            or not isinstance(status.get("version"), str) or not status["version"]):
        raise MetadataError("identity_mismatch")
    version = status["version"]
    art = ArtBuilder()
    try:
        payload = client.public_channels()
    except MetadataError as error:
        state, warning = _failure(error)
        all_channels: MetadataBatch[Channel] = MetadataBatch((), state, (warning,))
        channels = all_channels
    else:
        all_channels = dto.public_channels(payload, client.namespace, include_hidden=True)
        channels = dto.public_channels(payload, client.namespace, art=art)
    events = _guide(client, all_channels, channels, time_s, guide_windows, art)
    recordings = collect_recordings(client, page_size=page_size, max_pages=max_pages, art=art)
    try:
        jobs = dto.scheduled_jobs(client.scheduled_jobs(), client.namespace, art=art)
    except MetadataError as error:
        state, warning = _failure(error)
        jobs = MetadataBatch((), state, (warning,))
    return DevelopmentSnapshot(client.namespace, version, int(time.time()),
                               (time_s, time_s + guide_windows * 1800),
                               channels, events, recordings, jobs, artwork=art.freeze())


# Summary is an allowlisted diagnostic projection, even for constructed snapshots.
_SAFE_WARNINGS = frozenset({
    _SCAN_WARNING, "metadata_unsupported", "metadata_malformed", "metadata_outage",
    "invalid_files_page", "files_total_changed", "files_no_progress",
    "files_early_empty", "files_page_limit", "guide_channels_unavailable",
    "guide_no_visible_channels",
    "invalid_channels_shape", "missing_channel_name", "conflicting_channel",
    "invalid_channel", "invalid_guide_window", "invalid_guide_shape",
    "invalid_guide_row", "unknown_guide_channel", "guide_station_missing",
    "guide_station_unqualified",
    "missing_program_id", "unknown_guide_duration", "invalid_guide_airing",
    "conflicting_event", "invalid_files_shape", "files_total_mismatch",
    "invalid_file", "unknown_file_duration", "fractional_duration_unknown",
    "unknown_file_watched", "unknown_file_resume", "conflicting_file",
    "invalid_jobs_shape", "invalid_job", "unknown_job_channel_join",
    "unknown_job_padding", "conflicting_job",
})
_SAFE_COUNTS = frozenset("""
    pages raw_rows unique total complete duplicates conflicts invalid_rows
    requested_windows completed_windows rows events visible_filtered accepted
    channels hidden filtered missing_names joined_rows rows_with_airings
    airings_seen unknown_duration missing_program_id unjoined invalid_airings
    station_missing_rows station_unqualified_rows unqualified_airings recordings fractional_duration
    unknown_watched unknown_resume jobs unknown_channel_join unknown_padding
""".split())


def safe_snapshot_summary(snapshot: DevelopmentSnapshot) -> dict[str, object]:
    """Safe version, requested UTC seconds and resource diagnostics; no model data."""
    version = snapshot.version if parse_build(snapshot.version) is not None else 'unavailable'
    resources: dict[str, object] = {}
    for name, batch in (("channels", snapshot.channels), ("events", snapshot.events),
                        ("recordings", snapshot.recordings), ("jobs", snapshot.jobs)):
        resources[name] = {
            "state": batch.state.value,
            "item_count": len(batch.items),
            "counts": {k: v for k, v in batch.counts
                       if k in _SAFE_COUNTS and type(v) is int},
            "warnings": [w for w in batch.warnings if w in _SAFE_WARNINGS],
        }
    return {"version": version, "fetched_at": snapshot.fetched_at,
            "requested_horizon": snapshot.horizon, "resources": resources}
