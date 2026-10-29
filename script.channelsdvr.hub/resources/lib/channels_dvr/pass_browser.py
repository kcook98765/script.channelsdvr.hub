"""Pure bounded preview metadata and worker-prepared, inert display text."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import unicodedata
from .ui_text import FALLBACK, text


def display_text(value: object, limit: int, strings=FALLBACK) -> str | None:
    if type(value) is not str:
        return None
    # Bound work before scanning optional text. Full-width brackets cannot be
    # interpreted as Kodi formatting, including unknown/future formatting tags.
    brackets = {"[": "［", "]": "］"}
    rendered = "".join(" " if unicodedata.category(c).startswith("C") else brackets.get(c, c)
                   for c in value[:limit]).strip()
    if not rendered:
        return None
    return rendered + (text(strings, 'truncated') if len(value) > limit else '')


def utc(seconds: int) -> str:
    stamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return stamp.strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass(frozen=True)
class PreviewMatch:
    response_index: int
    program_id: str = field(repr=False)
    title: str = field(repr=False)
    episode_title: str | None = field(repr=False)
    season: int | None
    episode: int | None
    start: int
    end: int
    duration: int
    channels: tuple[str, ...] = field(repr=False)
    channel: str | None = field(repr=False)
    summary: str | None = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "channels", tuple(self.channels))

    @property
    def identity(self) -> tuple:
        # Preserve every response row, even exact duplicate observations.
        return (self.program_id, self.start, self.end, self.channels, self.channel, self.response_index)


@dataclass(frozen=True)
class PreviewSnapshot:
    observed_at: int
    matches: tuple[PreviewMatch, ...] = field(repr=False)
    labels: tuple[str, ...] = field(repr=False)
    details: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        for name in ("matches", "labels", "details"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not 0 <= len(self.matches) <= 1000 or not len(self.matches) == len(self.labels) == len(self.details):
            raise ValueError("invalid_preview_snapshot")


def prepare_snapshot(matches: tuple[PreviewMatch, ...], observed_at: int, strings=FALLBACK) -> PreviewSnapshot:
    """Called on worker only: sort and format the complete accepted dataset."""
    ordered = tuple(sorted(matches, key=lambda m: (m.start, m.response_index)))
    labels, details = [], []
    for m in ordered:
        title = display_text(m.title, 4096, strings) or text(strings, 'unavailable')
        channels = ', '.join(display_text(c, 256, strings) or text(strings, 'unavailable') for c in m.channels)
        channel = display_text(m.channel, 256, strings) or text(strings, 'unavailable')
        season = str(m.season) if m.season is not None else text(strings, 'unknown')
        episode = str(m.episode) if m.episode is not None else text(strings, 'unknown')
        phase = text(strings, 'preview_past' if m.end <= observed_at else
                     'preview_current' if m.start <= observed_at else 'preview_future')
        labels.append(text(strings, 'preview_row', start=utc(m.start), title=title, index=m.response_index + 1))
        details.append(text(strings, 'preview_fields', title=title,
            episode_title=m.episode_title or text(strings, 'unavailable'), season=season, episode=episode,
            start=utc(m.start), end=utc(m.end), duration=text(strings, 'seconds', count=m.duration), phase=phase,
            channels=channels or text(strings, 'unavailable'), channel=channel,
            summary=m.summary or text(strings, 'unavailable'), time=utc(observed_at)))
    return PreviewSnapshot(observed_at, ordered, tuple(labels), tuple(details))


def page_entries(snapshot: PreviewSnapshot, page: int, strings=FALLBACK) -> tuple[tuple[str, ...], tuple]:
    start = page * 50
    stop = min(start + 50, len(snapshot.matches))
    if type(page) is not int or page < 0 or start >= len(snapshot.matches):
        raise ValueError("invalid_preview_page")
    labels = snapshot.labels[start:stop]
    indices: tuple = tuple(range(start, stop))
    if page:
        labels += (text(strings, 'previous_page'),)
        indices += ("previous",)
    if stop < len(snapshot.matches):
        labels += (text(strings, 'next_page'),)
        indices += ("next",)
    return labels, indices
