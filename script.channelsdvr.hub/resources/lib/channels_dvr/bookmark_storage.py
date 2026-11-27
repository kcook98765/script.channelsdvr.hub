"""Worker-only I/O for Hub bookmarks, separate from operation roots.

The caller supplies the translated, existing private add-on profile and an
admitted source. It serializes saves and fences stale session intents before
dispatch. No worker is started here and Kodi callbacks must not call this API.
"""

import os
import stat
import tempfile

from .hub_bookmarks import (
    MAX_BOOKMARK_BYTES, BookmarkScope, HubBookmarks, decode_bookmarks, encode_bookmarks,
)
from .operation_storage import normalize_profile


BOOKMARK_DIRECTORY = 'hub-bookmarks'


def _reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _regular(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not _reparse(info)


def _require_directory(path: str) -> None:
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or _reparse(info):
        raise OSError('bookmark_directory_unavailable')


class BookmarkStore:
    """One canonical server slot. Construction is lexical; only load/save do I/O.

    As with legacy profile reads, portable entry checks assume a private profile
    and trusted ancestors; they are not a descriptor-relative no-follow sandbox.
    """

    def __init__(self, profile: str, source: BookmarkScope) -> None:
        if type(source) is not BookmarkScope:
            raise ValueError('invalid_bookmark_scope')
        self._profile = normalize_profile(profile)
        self._source = source
        self._directory = os.path.join(self._profile, BOOKMARK_DIRECTORY)
        self._path = os.path.join(self._directory, source.server_key + '.json')

    def _check_directory(self, *, create: bool) -> None:
        _require_directory(self._profile)
        if create:
            try:
                os.mkdir(self._directory, mode=0o700)
            except FileExistsError:
                pass
        _require_directory(self._directory)

    def load(self) -> HubBookmarks | None:
        """Read at most limit + 1 bytes; missing/invalid data is never repaired here."""
        try:
            self._check_directory(create=False)
            if not _regular(os.lstat(self._path)):
                return None
            with open(self._path, 'rb') as stream:
                if not _regular(os.fstat(stream.fileno())):
                    return None
                raw = stream.read(MAX_BOOKMARK_BYTES + 1)
        except (OSError, ValueError):
            return None
        return decode_bookmarks(self._source, raw)

    def save(self, bookmarks: HubBookmarks) -> bool:
        """Publish a same-source model atomically, or leave the previous file intact."""
        if type(bookmarks) is not HubBookmarks or bookmarks.source != self._source:
            return False
        temp_path: str | None = None
        try:
            payload = encode_bookmarks(bookmarks)
            self._check_directory(create=True)
            try:
                if not _regular(os.lstat(self._path)):
                    return False
            except FileNotFoundError:
                pass
            # Exclusive private staging; close before replace for Windows. A
            # process interruption can leave staging bytes, but readers ignore them.
            with tempfile.NamedTemporaryFile(mode='wb', dir=self._directory, prefix='.bookmark-',
                                             suffix='.tmp', delete=False) as stream:
                temp_path = stream.name
                if stream.write(payload) != len(payload):
                    return False
                stream.flush()
                os.fsync(stream.fileno())
            if temp_path is None:
                return False
            os.replace(temp_path, self._path)
            temp_path = None
            return True
        except (OSError, ValueError):
            return False
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass  # Only the temp name created by this attempt is eligible.
