"""Pure, route-specific public metadata adapters; no transport or cache writes.

Contracts target build 2026.08.07.0346. Populated files/jobs remain synthetic
contracts, not live qualification. Epoch values are seconds, without unit guessing.
Warnings and count keys are static codes; input values only enter model fields.
"""

from dataclasses import dataclass
import math
from typing import Generic, TypeVar, cast

from .models import Channel, ChannelKey, Event, Job, Recording, recording_duration
from .normalization import DataState
from .artwork import ArtBuilder, channel_key, event_key, recording_key, job_key

T = TypeVar("T")


@dataclass(frozen=True)
class MetadataBatch(Generic[T]):
    items: tuple[T, ...]
    state: DataState
    warnings: tuple[str, ...] = ()
    counts: tuple[tuple[str, int], ...] = ()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid_object")
    return cast(dict[str, object], value)


def _text(value: object, *, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError("invalid_text")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid_integer")
    return value


def _optional_integer(value: object) -> int | None:
    return None if value is None else _integer(value)


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("invalid_boolean")
    return value


def _warn(warnings: list[str], code: str) -> None:
    if code not in warnings:
        warnings.append(code)


def _batch(items: list[T], counts: dict[str, int], warnings: list[str],
           *, partial: bool = False, malformed: bool = False) -> MetadataBatch[T]:
    state = (DataState.MALFORMED if malformed else DataState.PARTIAL if partial
             else DataState.READY if items else DataState.EMPTY)
    return MetadataBatch(tuple(items), state, tuple(warnings), tuple(counts.items()))


def public_channels(payload: object, namespace: str, *,
                    include_hidden: bool = False, art: ArtBuilder | None = None) -> MetadataBatch[Channel]:
    """Validate atomically, deduplicate full keys, then apply visibility locally."""
    counts = dict(raw_rows=0, accepted=0, channels=0, hidden=0, filtered=0,
                  duplicates=0, conflicts=0, invalid_rows=0, missing_names=0)
    warnings: list[str] = []
    channels: dict[ChannelKey, Channel] = {}
    if (not isinstance(payload, list) or not isinstance(namespace, str)
            or not namespace.strip() or type(include_hidden) is not bool):
        return _batch([], counts, ["invalid_channels_shape"], malformed=True)
    counts["raw_rows"] = len(payload)
    for value in payload:
        try:
            row = _object(value)
            key = ChannelKey(namespace, _text(row.get("source_id"), required=True),
                             _text(row.get("id"), required=True))
            number = _text(row.get("number"), required=True)
            name = _text(row.get("name", ""))
            hidden = _boolean(row.get("hidden", False))
            favorite = _boolean(row.get("favorited", False))
            station = _text(row["station_id"]) if "station_id" in row else None
            if not name:
                name = number
                counts["missing_names"] += 1
                _warn(warnings, "missing_channel_name")
            groups: tuple[str, ...] = ("Hidden" if hidden else "Visible",)
            if favorite:
                groups += ("Favorites",)
            if station is not None:
                groups += ("Guide station present" if station else "Guide station missing",)
            else:
                groups += ("Guide station unqualified",)
            channel = Channel(key, number, name, groups)
        except ValueError:
            counts["invalid_rows"] += 1
            continue
        if key in channels:
            counts["duplicates"] += 1
            if channels[key] != channel:
                counts["conflicts"] += 1
                _warn(warnings, "conflicting_channel")
            continue
        channels[key] = channel
        if art is not None:
            art.add(channel_key(key), row.get('logo_url'))
    if counts["invalid_rows"]:
        _warn(warnings, "invalid_channel")
        return _batch([], counts, warnings, malformed=True)
    counts["accepted"] = len(channels)
    counts["hidden"] = sum("Hidden" in c.groups for c in channels.values())
    items = [c for c in channels.values() if include_hidden or "Hidden" not in c.groups]
    counts["channels"] = len(items)
    counts["filtered"] = counts["accepted"] - len(items)
    return _batch(items, counts, warnings, partial=bool(counts["conflicts"]))


def guide_window(payload: object, channels: tuple[Channel, ...], namespace: str,
                  time_s: int, duration: int = 1800, *,
                  require_station: bool = False, art: ArtBuilder | None = None) -> MetadataBatch[Event]:
    """Strict full-key joins and half-open overlap for the qualified 1800s window.

    Current development HDHR build policy: an explicitly blank Station leaves
    joined airings unqualified, even with ProgramIDs. This known setup gap is a
    warning, not a schema/transport failure. With require_station, other joined
    rows need a public Guide station present group or nonempty guide Station.
    Without it, omitted Station retains the generic sparse parsing contract.
    This is not a universal no-EPG rule for other sources or XMLTV setups.
    """
    counts = dict(raw_rows=0, joined_rows=0, rows_with_airings=0, airings_seen=0,
                  accepted=0, events=0, unknown_duration=0, missing_program_id=0,
                  unjoined=0, invalid_rows=0, invalid_airings=0, filtered=0,
                  duplicates=0, conflicts=0, station_missing_rows=0,
                  unqualified_airings=0, station_unqualified_rows=0)
    warnings: list[str] = []
    try:
        start = _integer(time_s)
        if type(duration) is not int or duration != 1800:
            raise ValueError("invalid_window")
        _boolean(require_station)
        _text(namespace, required=True)
    except ValueError:
        return _batch([], counts, ["invalid_guide_window"], malformed=True)
    if not isinstance(payload, list):
        return _batch([], counts, ["invalid_guide_shape"], malformed=True)
    end = start + duration
    known = {c.key for c in channels if c.key.namespace == namespace}
    station_present = {c.key for c in channels if c.key.namespace == namespace
                       and "Guide station present" in c.groups}
    events: dict[tuple[ChannelKey, str, int], Event] = {}
    partial = False
    counts["raw_rows"] = len(payload)
    for value in payload:
        try:
            row = _object(value)
            channel = _object(row.get("Channel"))
            key = ChannelKey(namespace, _text(channel.get("DeviceID"), required=True),
                             _text(channel.get("ChannelID"), required=True))
            for field in ("Number", "Name", "Station"):
                if field in channel:
                    _text(channel[field])
            airings = row.get("Airings")
            if not isinstance(airings, list):
                raise ValueError("invalid_airings")
        except ValueError:
            counts["invalid_rows"] += 1
            _warn(warnings, "invalid_guide_row")
            partial = True
            continue
        counts["airings_seen"] += len(airings)
        counts["rows_with_airings"] += bool(airings)
        if key not in known:
            counts["unjoined"] += 1
            _warn(warnings, "unknown_guide_channel")
            partial = True
            continue
        counts["joined_rows"] += 1
        if art is not None:
            art.add(channel_key(key), channel.get('Image'))
        if channel.get("Station") == "":
            counts["station_missing_rows"] += 1
            counts["unqualified_airings"] += len(airings)
            _warn(warnings, "guide_station_missing")
            continue
        if require_station and key not in station_present and not channel.get("Station"):
            counts["station_unqualified_rows"] += 1
            counts["unqualified_airings"] += len(airings)
            _warn(warnings, "guide_station_unqualified")
            continue
        for value in airings:
            try:
                airing = _object(value)
                program_id = airing.get("ProgramID")
                if program_id is None or program_id == "":
                    counts["missing_program_id"] += 1
                    _warn(warnings, "missing_program_id")
                    raise ValueError("missing_program_id")
                event_id = _text(program_id, required=True)
                title = _text(airing.get("Title"), required=True)
                event_start = _integer(airing.get("Time"))
                event_end: int | None = None
                raw_duration = airing.get("Duration")
                if raw_duration is not None:
                    length = _integer(raw_duration)
                    if length == 0:
                        raise ValueError("invalid_duration")
                    event_end = event_start + length
                else:
                    counts["unknown_duration"] += 1
                    _warn(warnings, "unknown_guide_duration")
                summary = _text(airing.get("Summary", ""))
                full_summary = _text(airing.get("FullSummary", ""))
                event = Event(event_id, key, event_start, event_end, title,
                              full_summary or summary)
            except ValueError:
                counts["invalid_airings"] += 1
                _warn(warnings, "invalid_guide_airing")
                partial = True
                continue
            counts["accepted"] += 1
            identity = (key, event.id, event.start)
            if identity in events:
                counts["duplicates"] += 1
                if events[identity] != event:
                    counts["conflicts"] += 1
                    _warn(warnings, "conflicting_event")
                    partial = True
                continue
            # Store even out-of-window identities so dedup remains first-wins.
            events[identity] = event
            if art is not None and event.start < end and (event.end > start if event.end is not None else event.start >= start):
                art.add(event_key(event), airing.get('Image'))
    items = [e for e in events.values() if e.start < end and
             (e.end > start if e.end is not None else e.start >= start)]
    counts["events"] = len(items)
    counts["filtered"] = len(events) - len(items)
    return _batch(items, counts, warnings, partial=partial)


def files_page(payload: object, namespace: str, *, art: ArtBuilder | None = None) -> MetadataBatch[Recording]:
    """Validate one page, not catalog completeness.

    READY/EMPTY describe only this page. A total larger than its item count is
    normal paging, including an empty page with a positive total. The collector
    must track offsets and scan completeness separately; one page proves neither.
    """
    counts = dict(raw_rows=0, total=0, accepted=0, recordings=0, invalid_rows=0,
                  unknown_duration=0, fractional_duration=0, unknown_watched=0,
                  unknown_resume=0, duplicates=0, conflicts=0)
    warnings: list[str] = []
    try:
        envelope = _object(payload)
        rows = envelope.get("items")
        total = _integer(envelope.get("total"))
        _text(namespace, required=True)
        if not isinstance(rows, list):
            raise ValueError("invalid_items")
    except ValueError:
        return _batch([], counts, ["invalid_files_shape"], malformed=True)
    counts["total"], counts["raw_rows"] = total, len(rows)
    partial = total < len(rows)
    if partial:
        _warn(warnings, "files_total_mismatch")
    recordings: dict[str, Recording] = {}
    for value in rows:
        try:
            row = _object(value)
            recording_id = _text(row.get("ID"), required=True)
            created = _integer(row.get("CreatedAt"))
            airing = _object(row.get("Airing"))
            show = _text(airing.get("Title"), required=True)
            episode_title = _optional_text(airing.get("EpisodeTitle"))
            show_id = _optional_text(airing.get("SeriesID"))
            season = _optional_integer(airing.get("SeasonNumber"))
            episode = _optional_integer(airing.get("EpisodeNumber"))
            categories = airing.get("Categories", [])
            if not isinstance(categories, list):
                raise ValueError("invalid_categories")
            movie = "Movie" in [_text(c) for c in categories]
            raw_duration = row.get("Duration")
            length: int | float | None = None
            fractional = False
            if raw_duration is not None:
                length = recording_duration(raw_duration)
                fractional = type(length) is float and not length.is_integer()
            resume, resume_known = 0, False
            # AdminFile PlaybackTime is top-level; Resume/PlayedAt are unqualified.
            if "PlaybackTime" in row:
                resume = _integer(row["PlaybackTime"])
                resume_known = True
            recording = Recording(
                recording_id, episode_title or show, show, season, episode, movie,
                created, length, watched=False, resume=resume, namespace=namespace,
                provenance="unknown", show_id=show_id, watched_known=False,
                resume_known=resume_known)
        except ValueError:
            counts["invalid_rows"] += 1
            _warn(warnings, "invalid_file")
            partial = True
            continue
        counts["accepted"] += 1
        if length is None:
            counts["unknown_duration"] += 1
            _warn(warnings, "unknown_file_duration")
        if fractional:
            counts["fractional_duration"] += 1
        counts["unknown_watched"] += 1
        _warn(warnings, "unknown_file_watched")
        if not resume_known:
            counts["unknown_resume"] += 1
            _warn(warnings, "unknown_file_resume")
        if recording_id in recordings:
            counts["duplicates"] += 1
            if recordings[recording_id] != recording:
                counts["conflicts"] += 1
                _warn(warnings, "conflicting_file")
                partial = True
            continue
        recordings[recording_id] = recording
        if art is not None:
            art.add(recording_key(recording), airing.get('Image'), ('Recordings', repr((
                recording.namespace, recording.provenance, recording.id))))
    counts["recordings"] = len(recordings)
    return _batch(list(recordings.values()), counts, warnings, partial=partial)


def scheduled_jobs(payload: object, namespace: str, *, art: ArtBuilder | None = None) -> MetadataBatch[Job]:
    """Keep scheduled intervals unchanged; channel joins and padding are unknown."""
    counts = dict(raw_rows=0, accepted=0, jobs=0, invalid_rows=0,
                  unknown_channel_join=0, unknown_padding=0, duplicates=0, conflicts=0)
    warnings: list[str] = []
    if not isinstance(payload, list) or not isinstance(namespace, str) or not namespace.strip():
        return _batch([], counts, ["invalid_jobs_shape"], malformed=True)
    counts["raw_rows"] = len(payload)
    jobs: dict[str, Job] = {}
    partial = False
    for value in payload:
        try:
            row = _object(value)
            job_id = _text(row.get("id"), required=True)
            start, end = _integer(row.get("start_time")), _integer(row.get("end_time"))
            if end <= start:
                raise ValueError("invalid_interval")
            item = _object(row.get("item", {}))
            name = _text(row.get("name", ""))
            title = _text(row.get("title", ""))
            item_title = _text(item.get("title", ""))
            label = _text(name or title or item_title, required=True)
            program_id = _optional_text(item.get("program_id"))
            job = Job(job_id, label, start, end, (),
                      (program_id,) if program_id else (), padding_known=False)
        except ValueError:
            counts["invalid_rows"] += 1
            _warn(warnings, "invalid_job")
            partial = True
            continue
        counts["accepted"] += 1
        counts["unknown_channel_join"] += 1
        counts["unknown_padding"] += 1
        _warn(warnings, "unknown_job_channel_join")
        _warn(warnings, "unknown_job_padding")
        if job_id in jobs:
            counts["duplicates"] += 1
            if jobs[job_id] != job:
                counts["conflicts"] += 1
                _warn(warnings, "conflicting_job")
                partial = True
            continue
        jobs[job_id] = job
        if art is not None:
            art.add(job_key(namespace, job), item.get('image_url'), ('Scheduled', job.id))
    counts["jobs"] = len(jobs)
    return _batch(list(jobs.values()), counts, warnings, partial=partial)
