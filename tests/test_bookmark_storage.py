"""Synthetic temporary profiles and injected failures; no Kodi or provider access."""

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from channels_dvr import bookmark_storage
from channels_dvr.bookmark_storage import BookmarkStore
from channels_dvr.hub_bookmarks import MAX_BOOKMARK_BYTES, BookmarkScope, HubBookmarks, encode_bookmarks
from channels_dvr.live_loader import DevelopmentConfig
from channels_dvr.operation_storage import StorageError


class BookmarkStorageTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='hub-bookmark-test-')
        self.addCleanup(temp.cleanup)
        self.profile = Path(temp.name)
        self.directory = self.profile / 'hub-bookmarks'
        self.a, self.b = (self.scope(host) for host in ('192.0.2.10', '192.0.2.11'))
        self.store = BookmarkStore(temp.name, self.a)
        self.path = self.directory / (self.a.server_key + '.json')
        self.saved = HubBookmarks(self.a, guide_group='Favorites', dvr_tab='scheduled',
                                  recent_terms=('Café',), last_view='Guide')
        self.payload = encode_bookmarks(self.saved)

    @staticmethod
    def scope(host):
        return BookmarkScope.from_config(DevelopmentConfig.from_address(host))

    def test_construction_is_lexical_and_read_never_creates_the_namespace(self):
        with patch.object(bookmark_storage.os, 'lstat', side_effect=AssertionError('unexpected filesystem check')):
            BookmarkStore(str(self.profile), self.a)
            for invalid in ('relative', 'special://profile/addon_data', '//server/share/profile'):
                with self.subTest(profile=invalid), self.assertRaisesRegex(StorageError, '^profile_unavailable$'):
                    BookmarkStore(invalid, self.a)
        self.assertIsNone(self.store.load())
        self.assertFalse(self.directory.exists())
        missing_profile = self.profile / 'missing-profile'
        missing = BookmarkStore(str(missing_profile), self.a)
        self.assertIsNone(missing.load())
        self.assertFalse(missing.save(self.saved))
        self.assertFalse(missing_profile.exists())

    def test_servers_with_same_metadata_namespace_have_separate_slots(self):
        other = BookmarkStore(str(self.profile), self.b)
        saved_b = HubBookmarks(self.b, last_view='DVR')
        other_path = self.directory / (self.b.server_key + '.json')
        history = self.profile / 'operation-history.sqlite3'
        history.write_bytes(b'untouched synthetic history')
        self.assertEqual(self.a.namespace, self.b.namespace)
        self.assertTrue(self.store.save(self.saved))
        self.assertIsNone(other.load())
        self.assertTrue(other.save(saved_b))
        self.assertEqual(self.path.read_bytes(), self.payload)
        self.assertEqual(other_path.read_bytes(), encode_bookmarks(saved_b))
        self.assertEqual(set(self.directory.iterdir()), {self.path, other_path})
        alias = self.scope(' HTTP://192.0.2.10:8089/ ')
        self.assertEqual(BookmarkStore(str(self.profile), alias).load(), self.saved)
        self.assertEqual(other.load(), saved_b)
        self.assertEqual(history.read_bytes(), b'untouched synthetic history')
        with patch.object(other, '_check_directory', side_effect=AssertionError('wrong-source write reached I/O')):
            self.assertFalse(other.save(self.saved))
        other_path.write_bytes(self.payload)  # Misfiled bytes must still fail the codec's scope check.
        self.assertIsNone(other.load())

    def test_unreadable_format_falls_back_without_rewriting_or_using_staging(self):
        self.directory.mkdir()
        orphan = self.directory / '.bookmark-interrupted.tmp'
        orphan.write_bytes(self.payload)
        self.assertIsNone(self.store.load())
        old = json.loads(self.payload)
        old['version'] = 0
        future = dict(old, version=2)
        for raw in (self.payload[:-1], b'\xff', b'{}', json.dumps(old).encode(), json.dumps(future).encode(),
                    self.payload + b' ' * (MAX_BOOKMARK_BYTES + 1 - len(self.payload))):
            with self.subTest(size=len(raw)):
                self.path.write_bytes(raw)
                self.assertIsNone(self.store.load())
                self.assertEqual(self.path.read_bytes(), raw)
                self.assertEqual(orphan.read_bytes(), self.payload)
                self.assertEqual(set(self.directory.iterdir()), {self.path, orphan})

    def test_failed_replacement_leaves_previous_good_bytes_and_cleans_staging(self):
        self.assertTrue(self.store.save(self.saved))
        next_state = replace(self.saved, last_view='DVR')
        staged = []

        def fail_replacement(temp_path, destination):
            candidate = Path(temp_path)
            staged.append(candidate)
            self.assertEqual(candidate.parent, self.directory)
            self.assertTrue(candidate.name.startswith('.bookmark-'))
            self.assertTrue(candidate.name.endswith('.tmp'))
            self.assertEqual(Path(destination), self.path)
            self.assertEqual(candidate.read_bytes(), encode_bookmarks(next_state))
            self.assertEqual(self.path.read_bytes(), self.payload)
            self.assertEqual(self.store.load(), self.saved)
            raise OSError('synthetic replacement failure')

        with patch.object(bookmark_storage.os, 'replace', side_effect=fail_replacement) as attempt:
            self.assertFalse(self.store.save(next_state))
            attempt.assert_called_once()
        self.assertFalse(staged[0].exists())
        self.assertEqual(self.store.load(), self.saved)
        self.assertTrue(self.store.save(next_state))
        self.assertEqual(self.store.load(), next_state)

    def test_failed_sync_never_attempts_replacement(self):
        self.assertTrue(self.store.save(self.saved))
        with (patch.object(bookmark_storage.os, 'fsync', side_effect=OSError('synthetic sync failure')) as sync,
              patch.object(bookmark_storage.os, 'replace') as publish):
            self.assertFalse(self.store.save(replace(self.saved, last_view='DVR')))
            sync.assert_called_once()
            publish.assert_not_called()
        self.assertEqual(set(self.directory.iterdir()), {self.path})
        self.assertEqual(self.store.load(), self.saved)

    def test_nonregular_slot_is_not_read_or_replaced(self):
        self.path.mkdir(parents=True)
        self.assertIsNone(self.store.load())
        self.assertFalse(self.store.save(self.saved))
        self.assertTrue(self.path.is_dir())
        self.assertEqual(set(self.directory.iterdir()), {self.path})

    def test_symlinked_namespace_and_slot_are_not_followed(self):
        redirected = self.profile / 'unowned'
        redirected.mkdir()
        target = redirected / self.path.name
        target.write_bytes(self.payload)
        try:
            self.directory.symlink_to(redirected, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest('Directory symlinks unavailable')
        self.assertIsNone(self.store.load())
        self.assertFalse(self.store.save(replace(self.saved, last_view='DVR')))
        self.assertEqual(target.read_bytes(), self.payload)
        self.directory.unlink()
        self.directory.mkdir()
        self.path.symlink_to(target)
        self.assertIsNone(self.store.load())
        self.assertFalse(self.store.save(self.saved))
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_bytes(), self.payload)


if __name__ == '__main__':
    unittest.main()
