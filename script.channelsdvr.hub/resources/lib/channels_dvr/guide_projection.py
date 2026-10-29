"""One current immutable channel DTO projection, never a permissions cache.

Production inputs are tuples of frozen Channel/ChannelKey DTOs with tuple groups.
Mutable collections are compared via a fresh tuple on every read; they must not
take the identity shortcut. Replacing a frozen DTO is the supported edit contract.
No event admission, namespace fallback, I/O or SDK operations belong here.
"""

from .guide_gap import station_footer
from .ui_text import FALLBACK, text


class ChannelProjection:
    def __init__(self, strings=FALLBACK):
        self.strings = strings
        self.snapshot = None
        self.channels = ()
        self.by_key = {}
        self.sources = {}
        self.footer = ""

    def update(self, channels):
        if isinstance(channels, tuple) and channels is self.snapshot:
            return self
        frozen = channels if isinstance(channels, tuple) else tuple(channels)
        if not isinstance(channels, tuple) and frozen == self.channels:
            return self
        self.snapshot = channels if isinstance(channels, tuple) else None
        self.channels = frozen
        self.by_key = {}
        for channel in frozen:
            self.by_key.setdefault(channel.key, channel)
        self.sources = {source: index + 1 for index, source in enumerate(
            dict.fromkeys(c.key.source_id for c in frozen))}
        self.footer = station_footer(frozen, self.strings)
        return self

    def source_label(self, key):
        ordinal = self.sources.get(key.source_id)
        return text(self.strings, 'source_ordinal', ordinal=ordinal) if ordinal is not None else text(self.strings, 'source_unavailable')
