"""Pure Hub bookmark schema and startup policy. No settings, paths or I/O."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
import json
import re
from typing import TYPE_CHECKING

from .library import MODE_LABELS, SORT_LABELS
from .models import ChannelKey
from .newbuild_compatibility import PROFILE_BINDING_ANCHOR_V1, profile_binding
from .search import normalize_query

if TYPE_CHECKING:
    from .live_loader import DevelopmentConfig


VERSION = 1
MAX_BOOKMARK_BYTES = 8192
MAX_RECENT_TERMS = 8
_PRIMARY = ('Dashboard', 'Guide', 'Recordings', 'DVR')
_CATEGORIES = tuple(MODE_LABELS)
_SORTS = tuple(SORT_LABELS)
_PROVENANCE = ('dvr', 'import', 'unknown')
_FILTERS = ('all', *_PROVENANCE)
_NAMESPACE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
_LOCATOR = re.compile(r'\b(?:https?|ftp|file|plugin|special|smb|nfs|upnp|rtsp|rtmp|data|magnet):\S', re.I)


@dataclass(frozen=True, slots=True)
class BookmarkScope:
    namespace: str = field(repr=False)
    server_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if (type(self.namespace) is not str or not _NAMESPACE.fullmatch(self.namespace)
                or type(self.server_key) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', self.server_key)):
            raise ValueError('invalid_bookmark_scope')

    @classmethod
    def from_config(cls, config: 'DevelopmentConfig') -> 'BookmarkScope':
        # DevelopmentConfig's metadata namespace label can be shared by two
        # origins. Reuse the canonical profile binding of origin + namespace,
        # with its fixed anchor: server build changes must not move bookmarks.
        # This is a local partition key, not authentication or write authority.
        return cls(config.namespace, profile_binding(config.origin, config.namespace,
                                                     PROFILE_BINDING_ANCHOR_V1))


def _choice(value: object, choices: tuple[str, ...]) -> str | None:
    return value if type(value) is str and value in choices else None


def _identifier(value: object) -> str | None:
    return value if type(value) is str and _IDENTIFIER.fullmatch(value) and not _LOCATOR.search(value) else None


def _safe_text(value: object) -> str | None:
    if (type(value) is not str or not 1 <= len(value) <= 128 or not value.isprintable()
            or not value.strip() or '//' in value or '\\' in value or value.lstrip().startswith('/')
            or _LOCATOR.search(value)):
        return None
    return value


def _recent_terms(value: object) -> tuple[str, ...]:
    if type(value) is not tuple and type(value) is not list:
        return ()
    accepted: list[str] = []
    seen: set[str] = set()
    # Input is newest first. Bound work even on a dirty or duplicate-heavy list.
    for item in value[:4 * MAX_RECENT_TERMS]:
        if type(item) is not str or len(item) > 128:
            continue
        term = _safe_text(item.strip())
        if term is None:
            continue
        key = normalize_query(term)
        if key and key not in seen:
            accepted.append(term)
            seen.add(key)
            if len(accepted) == MAX_RECENT_TERMS:
                break
    return tuple(accepted)


@dataclass(frozen=True, slots=True)
class HubBookmarks:
    """One source's allowlisted data. All keys are navigation hints, not actions."""

    source: BookmarkScope
    guide_group: str | None = field(default=None, repr=False)
    guide_channel: ChannelKey | None = field(default=None, repr=False)
    recordings_category: str = 'Shows'
    recordings_filter: str = 'all'
    recordings_sort: str | None = None  # unset -> LibraryBrowser's category default
    # Exactly browser.recording_key(record): no Row payload, title, path or index.
    recording_selection: tuple[str, str, str] | None = field(default=None, repr=False)
    dvr_tab: str | None = None  # stable key only; the DVR route owns its available tabs
    recent_terms: tuple[str, ...] = field(default=(), repr=False)
    last_view: str = 'Dashboard'

    def __post_init__(self) -> None:
        if type(self.source) is not BookmarkScope:
            raise ValueError('invalid_bookmark_scope')
        channel = self.guide_channel
        if (type(channel) is not ChannelKey or type(channel.namespace) is not str
                or channel.namespace != self.source.namespace
                or not _identifier(channel.source_id) or not _identifier(channel.channel_id)):
            channel = None
        category = _choice(self.recordings_category, _CATEGORIES)
        origin = _choice(self.recordings_filter, _FILTERS)
        selected = self.recording_selection
        # Library Search's active query is not stored. Its old selection must
        # not be applied to an unrelated query or to a repaired category/filter.
        if (not category or category == 'Search' or not origin
                or type(selected) is not tuple or len(selected) != 3
                or type(selected[0]) is not str or selected[0] != self.source.namespace
                or not _choice(selected[1], _PROVENANCE)
                or (origin != 'all' and selected[1] != origin) or not _identifier(selected[2])):
            selected = None
        object.__setattr__(self, 'guide_group', _safe_text(self.guide_group))
        object.__setattr__(self, 'guide_channel', channel)
        object.__setattr__(self, 'recordings_category', category or 'Shows')
        object.__setattr__(self, 'recordings_filter', origin or 'all')
        object.__setattr__(self, 'recordings_sort', _choice(self.recordings_sort, _SORTS))
        object.__setattr__(self, 'recording_selection', selected)
        object.__setattr__(self, 'dvr_tab', _identifier(self.dvr_tab))
        object.__setattr__(self, 'recent_terms', _recent_terms(self.recent_terms))
        object.__setattr__(self, 'last_view', _choice(self.last_view, _PRIMARY) or 'Dashboard')

    def guide_target(
        self, source: BookmarkScope, groups: Mapping[str, tuple[ChannelKey, ...]],
    ) -> tuple[str | None, ChannelKey | None]:
        """Resolve against the current source's groups, ordered default first."""
        same_source = source == self.source
        group = self.guide_group if same_source else None
        if group not in groups:
            group = next(iter(groups), None)
        channel = self.guide_channel if same_source else None
        fallback: ChannelKey | None = None
        for key in groups[group] if group is not None else ():
            if key.namespace == source.namespace:
                if key == channel:
                    return group, key
                if fallback is None:
                    fallback = key
        return group, fallback  # The route supplies Now, never a saved time.

    def matching_recording_row(self, source: BookmarkScope, row_ids: Iterable[str]) -> str | None:
        """Find one exact LibraryBrowser leaf in the current category/filter.

        The caller provides current eligible row IDs, not an old selection or
        offset. None means use the browser's normal default.
        """
        if source != self.source or self.recording_selection is None:
            return None
        candidate = repr(self.recording_selection)  # LibraryBrowser leaf ID; never parsed.
        found = False
        for row_id in row_ids:
            if row_id == candidate:
                if found:
                    return None
                found = True
        return candidate if found else None

    def dvr_target(self, source: BookmarkScope, tabs: tuple[str, ...]) -> str | None:
        """Resolve a stable tab key against available tabs, ordered default first."""
        if source == self.source and self.dvr_tab in tabs:
            return self.dvr_tab
        return tabs[0] if tabs else None


def encode_bookmarks(bookmarks: HubBookmarks) -> bytes:
    """Encode only v1 fields; namespace is implicit in each local identity."""
    if type(bookmarks) is not HubBookmarks:
        raise ValueError('invalid_bookmarks')
    channel = bookmarks.guide_channel
    selection = bookmarks.recording_selection
    root = {
        'version': VERSION,
        'source': bookmarks.source.server_key,
        'guide_group': bookmarks.guide_group,
        'guide_channel': (channel.source_id, channel.channel_id) if channel else None,
        'recordings_category': bookmarks.recordings_category,
        'recordings_filter': bookmarks.recordings_filter,
        'recordings_sort': bookmarks.recordings_sort,
        'recording_selection': selection[1:] if selection else None,
        'dvr_tab': bookmarks.dvr_tab,
        'recent_terms': bookmarks.recent_terms,
        'last_view': bookmarks.last_view,
    }
    raw = json.dumps(root, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_BOOKMARK_BYTES:
        raise ValueError('bookmark_size')
    return raw


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('duplicate_bookmark_key')
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError('invalid_bookmark_constant')


def _pair(value: object) -> tuple[str, str] | None:
    if type(value) is list and len(value) == 2:
        first, second = (_identifier(part) for part in value)
        if first is not None and second is not None:
            return first, second
    return None


def decode_bookmarks(source: BookmarkScope, raw: bytes | None) -> HubBookmarks | None:
    """Return no snapshot for missing, corrupt, unsupported or wrong-source bytes.

    Unrecognized fields are never copied, and invalid hints in a valid v1
    envelope fall back independently. This does not authorize overwriting it.
    """
    if type(source) is not BookmarkScope or type(raw) is not bytes or not 0 < len(raw) <= MAX_BOOKMARK_BYTES:
        return None
    try:
        root = json.loads(raw.decode('utf-8'), object_pairs_hook=_strict_object,
                          parse_constant=_invalid_constant)
    except (ValueError, RecursionError):
        return None
    if (type(root) is not dict or type(root.get('version')) is not int
            or root['version'] != VERSION or root.get('source') != source.server_key):
        return None
    channel = _pair(root.get('guide_channel'))
    selection = _pair(root.get('recording_selection'))
    return HubBookmarks(
        source,
        guide_group=_safe_text(root.get('guide_group')),
        guide_channel=ChannelKey(source.namespace, *channel) if channel else None,
        recordings_category=_choice(root.get('recordings_category'), _CATEGORIES) or '',
        recordings_filter=_choice(root.get('recordings_filter'), _FILTERS) or '',
        recordings_sort=_choice(root.get('recordings_sort'), _SORTS),
        recording_selection=(source.namespace, *selection) if selection else None,
        dvr_tab=_identifier(root.get('dvr_tab')),
        recent_terms=_recent_terms(root.get('recent_terms')),
        last_view=_choice(root.get('last_view'), _PRIMARY) or 'Dashboard',
    )


class StartupAdmission(Enum):
    PENDING = 'pending'
    ADMITTED = 'admitted'
    UNAVAILABLE = 'unavailable'  # missing, disabled, rejected or unavailable


def startup_destination(preference: object = None, *, admission: StartupAdmission,
                        intent_current: bool, launch_override: bool = False,
                        source: BookmarkScope | None = None,
                        bookmarks: HubBookmarks | None = None) -> str | None:
    """Choose an automatic route; None means no automatic transition.

    The owner supplies current/unused launch intent and separately handles
    explicit launch routes, manual navigation and connection status. 'Last'
    is a policy token, not a Kodi setting value or a translated label.
    """
    if intent_current is not True or launch_override is not False or admission is StartupAdmission.PENDING:
        return None
    if admission is not StartupAdmission.ADMITTED:
        return 'Dashboard'
    choice = _choice(preference, (*_PRIMARY, 'Last'))
    if choice == 'Last':
        if type(source) is BookmarkScope and type(bookmarks) is HubBookmarks and bookmarks.source == source:
            return bookmarks.last_view
        return 'Dashboard'
    return choice or 'Dashboard'
