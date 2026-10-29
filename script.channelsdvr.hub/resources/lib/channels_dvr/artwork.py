"""Immutable presentation-only references. No SDK, I/O or domain DTO changes."""
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from .models import ChannelKey, Event, Job, Recording

ArtKey = tuple[str, ...]
MAX_REFERENCES = 8192


def channel_key(key: ChannelKey) -> ArtKey:
    return ('channel', key.namespace, key.source_id, key.channel_id)


def event_key(event: Event) -> ArtKey:
    return ('event', *channel_key(event.channel)[1:], event.id, str(event.start))


def recording_key(record: Recording) -> ArtKey:
    return ('recording', record.namespace, record.provenance, record.id)


def job_key(namespace: str, job: Job) -> ArtKey:
    return ('job', namespace, job.id, str(job.start), str(job.end))


@dataclass(frozen=True, repr=False, eq=False)
class ArtInventory:
    references: Mapping[ArtKey, str] = field(default_factory=lambda: MappingProxyType({}))
    rows: Mapping[tuple[str, str], ArtKey] = field(default_factory=lambda: MappingProxyType({}))
    logos: Mapping[ArtKey, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))


class ArtBuilder:
    """Worker-local: <=8192 total references, <=2 distinct ordered strings per logo.

    Captures accepted normalizer rows only, never starts an artwork request.
    Overflow/missing/bad art is an ordinary presentation miss.
    """
    def __init__(self):
        self._refs: dict[ArtKey, str] = {}
        self._rows: dict[tuple[str, str], ArtKey] = {}
        self._logos: dict[ArtKey, tuple[str, ...]] = {}
        self._count = 0

    def add(self, key: ArtKey, value: object, row: tuple[str, str] | None = None):
        if (type(value) is not str or not value or len(value) > 2048
                or self._count >= MAX_REFERENCES):
            return
        try:
            if len(value.encode('utf-8')) > 2048:
                return
        except UnicodeEncodeError:
            return
        if key and key[0] == 'channel':
            candidates = self._logos.get(key, ())
            if value in candidates or len(candidates) >= 2:
                return
            self._logos[key] = candidates + (value,)
        elif key in self._refs:
            return
        self._refs.setdefault(key, value)  # existing flat primary/media contract
        self._count += 1
        if row is not None:
            self._rows.setdefault(row, key)

    def freeze(self) -> ArtInventory:
        return ArtInventory(MappingProxyType(dict(self._refs)), MappingProxyType(dict(self._rows)),
                            MappingProxyType(dict(self._logos)))
