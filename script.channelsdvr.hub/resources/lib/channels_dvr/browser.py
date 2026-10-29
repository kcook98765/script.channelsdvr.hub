"""Pure offline library/schedule presentation, with persistent per-view cursor."""

from dataclasses import dataclass

from .guide import time_label
from .models import Recording, recording_duration
from .repository import MockRepository, job_match, recording_groups
from .guide_projection import ChannelProjection
from .ui_text import FALLBACK, text


@dataclass(frozen=True)
class Row:
    id: str
    label: str
    detail: str
    children: tuple["Row", ...] = ()


def recording_row(record: Recording, strings=FALLBACK) -> Row:
    state = text(strings, 'lib_watched' if record.watched else 'lib_unwatched')
    if not record.watched_known:
        state = text(strings, 'lib_watch_unknown')
    if not record.resume_known:
        state = text(strings, 'lib_state_resume', state=state, resume=text(strings, 'lib_resume_unknown'))
    elif record.resume:
        state = text(strings, 'lib_state_resume', state=state, resume=text(strings, 'lib_resume', seconds=record.resume))
    duration = text(strings, 'unknown')
    if record.duration is not None:
        try:
            duration = f'{recording_duration(record.duration)}s'
        except ValueError:
            pass  # Malformed direct callers do not display NaN/inf as known time.
    episode = (text(strings, 'lib_episode', season=record.season if record.season is not None else '?',
                    episode=record.episode if record.episode is not None else '?')
                if not record.is_movie else text(strings, 'lib_movie'))
    origin = origin_label(record.namespace, record.provenance, strings)
    return Row(repr(recording_key(record)),
               text(strings, 'lib_row', title=record.title, episode=episode, state=state, origin=origin),
               text(strings, 'recording_details', title=record.show or record.title, episode=episode,
                    state=state, origin=origin, duration=duration, time=time_label(record.created)))


def recording_key(record: Recording) -> tuple[str, str, str]:
    """Backend identity only, never a URL or a Kodi database ID."""
    return record.namespace, record.provenance, record.id


def provenance_label(code: str, strings=FALLBACK) -> str:
    return text(strings, {'dvr': 'lib_dvr', 'import': 'lib_imported', 'unknown': 'unknown'}[code])


def origin_label(namespace: str, provenance: str, strings=FALLBACK) -> str:
    return text(strings, 'lib_origin', origin=provenance_label(provenance, strings),
                namespace=namespace or text(strings, 'unknown'))


def library_rows(repository: MockRepository, mode: str, strings=FALLBACK) -> tuple[Row, ...]:
    if mode == "Recent":
        return tuple(recording_row(r, strings) for r in sorted(
            repository.recordings, key=lambda r: (-r.created, recording_key(r))))
    if mode == "Movies":
        return tuple(recording_row(r, strings) for r in repository.recordings if r.is_movie)
    return tuple(Row(repr(("group", group.namespace, group.provenance,
                          group.is_movie, "id" if group.show_id is not None else "title",
                          group.show_id if group.show_id is not None else group.show,
                          group.season)),
                      text(strings, 'lib_group', title=text(strings, 'movies') if group.is_movie else
                           text(strings, 'lib_group_season', title=group.show,
                                season=text(strings, 'lib_season', season=group.season if group.season is not None else '?')),
                           origin=origin_label(group.namespace, group.provenance, strings)),
                      text(strings, 'lib_browse_recordings'),
                      tuple(recording_row(r, strings) for r in group.recordings))
                 for group in recording_groups(repository.recordings))


def scheduled_rows(repository: MockRepository, strings=FALLBACK, *, demo=False) -> tuple[Row, ...]:
    result = []
    projection = ChannelProjection(strings).update(repository.channels)
    for job in repository.jobs:
        match = job_match(job, repository.events)
        channels = ', '.join(text(strings, 'scheduled_candidate', source=projection.source_label(key),
            channel=(f'{projection.by_key[key].number} {projection.by_key[key].name}'
                     if key in projection.by_key else text(strings, 'channel_unavailable')))
            for key in job.candidates)
        channel_detail = (text(strings, 'scheduled_candidates', channels=channels) if job.candidates
                          else text(strings, 'scheduled_source_unknown'))
        if job.padding_known:
            padding = text(strings, 'scheduled_padding', before=job.padding_before, after=job.padding_after,
                           start=time_label(job.start - job.padding_before), end=time_label(job.end + job.padding_after))
        else:
            padding = text(strings, 'scheduled_padding_unknown')
        status = text(strings, 'match_' + match.status.value)
        result.append(Row(job.id, text(strings, 'scheduled_row', title=job.title, match=status),
            text(strings, 'scheduled_detail', start=time_label(job.start), end=time_label(job.end),
                 padding=padding, channels=channel_detail, match=status,
                 policy=text(strings, 'scheduled_demo_policy' if demo else 'scheduled_policy'))))
    return tuple(result)
