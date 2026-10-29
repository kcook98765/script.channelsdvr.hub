"""Bounded, offline recordings catalogue and identity-bookmarked navigation.

Rows contain plain model projections only. IDs are opaque repr(tuple) strings:
navigation compares them directly and never parses them. Input coverage and the
100-match Search page limit are disclosed independently.
"""

from collections.abc import Iterable
from itertools import islice
from typing import TypeAlias
import unicodedata

from .browser import Row, origin_label, provenance_label, recording_key, recording_row
from .models import Recording, recording_duration
from .search import normalize_query
from .ui_text import FALLBACK, text


_Context: TypeAlias = tuple[str, str, str]  # mode, origin, normalized Search query
_Path: TypeAlias = tuple[str, ...]  # opaque folder Row IDs
_Bookmark: TypeAlias = tuple[str | None, int]  # selected Row ID, fallback offset
_Entry: TypeAlias = tuple[Recording, str, Row]
_ShowIdentity: TypeAlias = tuple[str, str, str, str, str]
_Member: TypeAlias = tuple[Recording, Row]


def _text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _valid(record: Recording) -> bool:
    """Reject malformed model fields, without inferring origins or units."""
    if not isinstance(record, Recording):
        return False
    try:
        if record.duration is not None:
            recording_duration(record.duration)
    except ValueError:
        return False
    return (
        isinstance(record, Recording)
        and isinstance(record.id, str) and bool(record.id)
        and isinstance(record.title, str)
        and (record.show is None or isinstance(record.show, str))
        and isinstance(record.namespace, str)
        and isinstance(record.provenance, str)
        and record.provenance in ("dvr", "import", "unknown")
        and (record.show_id is None or
             isinstance(record.show_id, str) and bool(record.show_id))
        and type(record.is_movie) is bool and type(record.watched) is bool
        and type(record.watched_known) is bool and type(record.resume_known) is bool
        and type(record.created) is int
        and type(record.resume) is int and record.resume >= 0
        and all(value is None or type(value) is int and value >= 0
                for value in (record.season, record.episode))
    )


class LibraryBrowser:
    """Shows/Recent/Movies/Search with independent origin and folder cursors.

    Shows sort by display title, origin and tagged show identity; seasons sort
    numerically (including zero), unknown last; episodes sort numerically then
    newest-first. Recent sorts newest-first with full identity tie breaks.
    Movies sort by title. A changed normalized query starts at its first match;
    equivalent query spellings preserve the cursor and the original query text.
    Only the current query's four origin contexts are retained, not query history.
    """

    def __init__(self, records: Iterable[Recording], *, max_records: int = 10000, strings=FALLBACK) -> None:
        if type(max_records) is not int or not 1 <= max_records <= 100000:
            raise ValueError("max_records must be an integer from 1 to 100000")
        self._max_records: int = max_records
        self.strings = strings
        self._mode: str = "Shows"
        self._query: str = ""
        self._normalized: str = ""
        self._filter: str = "all"
        self._source: tuple[Recording, ...] | None = None
        self._records: tuple[Recording, ...] | None = None
        self._index: tuple[_Entry, ...] = ()
        self._views: dict[_Context, tuple[tuple[Row, ...], bool]] = {}
        self._paths: dict[_Context, _Path] = {}
        self._bookmarks: dict[tuple[_Context, _Path], _Bookmark] = {}
        self._rows: tuple[Row, ...] = ()
        self._position: int = 0
        self._coverage_limited: bool = False
        self._truncated: bool = False
        self._breadcrumb: str = ""
        self.update(records)

    def bind_strings(self, strings):
        """Owner initialization only; normal components receive their snapshot at construction."""
        if self.strings is strings:
            return
        records = self._records or ()
        self.strings = strings
        self._source = self._records = None
        self.update(records)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def query(self) -> str:
        return self._query

    @property
    def provenance_filter(self) -> str:
        return self._filter

    @property
    def position(self) -> int:
        return self._position

    @property
    def selected(self) -> Row | None:
        return self._rows[self._position] if self._rows else None

    def selected_recording(self) -> Recording | None:
        """Typed leaf lookup; opaque Row IDs are compared, never parsed/evaluated."""
        row = self.selected
        if row is None or row.children:
            return None
        matches = [record for record, _, leaf in self._index if leaf.id == row.id]
        return matches[0] if len(matches) == 1 else None

    @property
    def breadcrumb(self) -> str:
        return self._breadcrumb

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def coverage_limited(self) -> bool:
        return self._coverage_limited

    @property
    def depth(self) -> int:
        return len(self._paths.get(self._context(), ()))

    def rows(self) -> tuple[Row, ...]:
        return self._rows

    def _context(self) -> _Context:
        return self._mode, self._filter, self._normalized if self._mode == "Search" else ""

    def _remember(self) -> None:
        context = self._context()
        path = self._paths.get(context, ())
        self._bookmarks[context, path] = (
            self.selected.id if self.selected else None, self._position)

    def update(self, records: Iterable[Recording]) -> None:
        if isinstance(records, tuple) and records is self._source:
            return
        captured = tuple(islice(records, self._max_records + 1))
        limited = len(captured) > self._max_records
        unique: dict[tuple[str, str, str], Recording] = {}
        for record in captured[:self._max_records]:
            if not _valid(record):
                limited = True
                continue
            unique.setdefault(recording_key(record), record)
        accepted = tuple(unique.values())
        self._source = records if isinstance(records, tuple) else None
        self._coverage_limited = limited
        if accepted == self._records:
            return
        self._remember()
        index: list[_Entry] = []
        retained: list[Recording] = []
        for record in accepted:
            try:
                row = recording_row(record, self.strings)
            except (ValueError, OverflowError, OSError):
                # Unrepresentable timestamps are invalid, not guessed milliseconds.
                self._coverage_limited = True
                continue
            retained.append(record)
            index.append((record, _text(record.title + " " + (record.show or "")), row))
        self._records = tuple(retained)
        self._index = tuple(index)
        self._views.clear()
        self._prune_bookmarks()
        # Rebind inactive contexts too: a removed folder cannot reappear as an
        # old captured subtree when its mode/filter is revisited later.
        for context in tuple(self._paths):
            self._resolve(context)
        self._load()

    def _prune_bookmarks(self) -> None:
        """Keep only current folder paths, including still-valid inactive ones.

        Walk each context's show/season tree once, without visiting episode
        leaves. Then check each bookmark by set membership rather than doing
        a catalogue/tree search per bookmark. Flat modes have only a root path.
        """
        valid_paths: dict[_Context, set[_Path]] = {}
        for context in {context for context, _ in self._bookmarks}:
            paths: set[_Path] = {()}
            if context[0] == "Shows":
                root, _ = self._root(context)
                for show in root:
                    if not show.children:
                        continue
                    paths.add((show.id,))
                    paths.update((show.id, season.id) for season in show.children
                                 if season.children)
            valid_paths[context] = paths
        self._bookmarks = {key: bookmark for key, bookmark in self._bookmarks.items()
                           if key[1] in valid_paths[key[0]]}

    def _root(self, context: _Context) -> tuple[tuple[Row, ...], bool]:
        if context in self._views:
            return self._views[context]
        mode, origin, query = context
        entries = tuple(entry for entry in self._index
                        if origin == "all" or entry[0].provenance == origin)
        more = False
        if mode == "Shows":
            rows = self._shows(entries, self.strings)
        elif mode == "Movies":
            rows = tuple(entry[2] for entry in sorted(
                (e for e in entries if e[0].is_movie),
                key=lambda e: (_text(e[0].title), recording_key(e[0]))))
        else:
            ordered = sorted(entries, key=lambda e: (-e[0].created, recording_key(e[0])))
            if mode == "Search":
                matches: list[Row] = []
                if query:
                    for entry in ordered:
                        if all(token in entry[1] for token in query.split()):
                            if len(matches) == 100:
                                more = True
                                break
                            matches.append(entry[2])
                rows = tuple(matches)
            else:
                rows = tuple(e[2] for e in ordered)
        self._views[context] = rows, more
        return rows, more

    @staticmethod
    def _shows(entries: tuple[_Entry, ...], strings=FALLBACK) -> tuple[Row, ...]:
        shows: dict[_ShowIdentity, list[_Member]] = {}
        for record, _, row in entries:
            if record.is_movie:
                continue
            identity = ("show", record.namespace, record.provenance,
                        "id" if record.show_id is not None else "title",
                        record.show_id if record.show_id is not None else
                        record.show or record.title)
            shows.setdefault(identity, []).append((record, row))
        result: list[tuple[str, Row]] = []
        for identity, members in shows.items():
            # Pick display metadata deterministically even if input order changes.
            representative = min(members, key=lambda e: (-e[0].created, recording_key(e[0])))[0]
            label = text(strings, 'lib_group', title=representative.show or representative.title,
                         origin=origin_label(representative.namespace, representative.provenance, strings))
            # Locale is presentation only; preserve the canonical title/origin order.
            # This legacy English key is internal sort identity, never displayed.
            sort_label = (f'{representative.show or representative.title} | '
                f'{provenance_label(representative.provenance)} | Namespace: '
                f"{representative.namespace or '(unspecified)'}")
            seasons: dict[int | None, list[_Member]] = {}
            for record, row in members:
                seasons.setdefault(record.season, []).append((record, row))
            children: list[Row] = []
            for season in sorted(seasons, key=lambda s: (s is None, s if s is not None else 0)):
                episodes = sorted(seasons[season], key=lambda e: (
                    e[0].episode is None, e[0].episode if e[0].episode is not None else 0,
                    -e[0].created, recording_key(e[0])))
                children.append(Row(repr(("season", identity, season)),
                                     text(strings, 'lib_season', season=season if season is not None else '?'),
                                     text(strings, 'lib_browse_episodes', title=label),
                                    tuple(e[1] for e in episodes)))
            result.append((sort_label, Row(repr(identity), label, text(strings, 'lib_browse_seasons'), tuple(children))))
        return tuple(row for _, row in sorted(result, key=lambda item: (_text(item[0]), item[1].id)))

    def _resolve(self, context: _Context) -> tuple[tuple[Row, ...], bool, list[str]]:
        rows, more = self._root(context)
        valid_path: list[str] = []
        labels: list[str] = []
        for identity in self._paths.get(context, ()):
            folder = next((row for row in rows if row.id == identity and row.children), None)
            if folder is None:
                break
            valid_path.append(identity)
            labels.append(folder.label)
            rows = folder.children
        self._paths[context] = tuple(valid_path)
        return rows, more, labels

    def _load(self) -> None:
        context = self._context()
        self._rows, self._truncated, labels = self._resolve(context)
        path = self._paths[context]
        identity, offset = self._bookmarks.get((context, path), (None, 0))
        self._position = next((i for i, row in enumerate(self._rows) if row.id == identity),
                              min(offset, max(0, len(self._rows) - 1)))
        origin = text(self.strings, 'all_sources') if self._filter == 'all' else provenance_label(self._filter, self.strings)
        mode_key = {'Shows': 'shows', 'Recent': 'recent', 'Movies': 'movies', 'Search': 'search'}[self._mode]
        root = text(self.strings, 'lib_breadcrumb', mode=text(self.strings, mode_key), origin=origin)
        if self._mode == "Search":
            root = text(self.strings, 'lib_search_breadcrumb', breadcrumb=root,
                        query=self._query if self._normalized else text(self.strings, 'search_query_prompt'))
        self._breadcrumb = " / ".join((root, *labels))
        self._remember()

    def move(self, direction: int) -> None:
        self._position = max(0, min(self._position + direction, max(0, len(self._rows) - 1)))
        self._remember()

    def open_selected(self) -> bool:
        selected = self.selected
        if selected is None or not selected.children:
            return False
        self._remember()
        context = self._context()
        self._paths[context] += (selected.id,)
        self._load()
        return True

    def back(self) -> bool:
        context = self._context()
        path = self._paths[context]
        if not path:
            return False
        self._remember()
        self._paths[context] = path[:-1]
        self._load()
        return True

    def set_mode(self, mode: str) -> None:
        if mode not in ("Shows", "Recent", "Movies", "Search"):
            raise ValueError("Unknown library mode")
        self._remember()
        self._mode = mode
        self._load()

    def set_query(self, query: str) -> None:
        normalized = normalize_query(query)
        self._remember()
        changed = normalized != self._normalized
        self._query, self._normalized = query, normalized
        self._mode = "Search"
        if changed:
            # No history contract: discard every previous Search query/origin
            # before loading the new query. Other mode/folder bookmarks survive.
            self._views = {context: view for context, view in self._views.items()
                           if context[0] != "Search"}
            self._paths = {context: path for context, path in self._paths.items()
                           if context[0] != "Search"}
            self._bookmarks = {key: bookmark for key, bookmark in self._bookmarks.items()
                               if key[0][0] != "Search"}
        self._load()

    def set_provenance_filter(self, provenance: str) -> None:
        if provenance not in ("all", "dvr", "import", "unknown"):
            raise ValueError("Unknown provenance filter")
        self._remember()
        self._filter = provenance
        self._load()
