"""Exercise exact-commit packaging with real Git index concealment flags."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'tools/package.py'
SPEC = importlib.util.spec_from_file_location('hub_packager', SCRIPT)
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)


class PackagerProvenanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.git('init', '-q')
        addon = self.root / packager.ADDON_ID
        addon.mkdir()
        (addon / 'addon.xml').write_text(
            '<addon id="script.channelsdvr.hub" version="0.1.2">'
            '<extension point="xbmc.python.script" library="default.py"/></addon>')
        (addon / 'default.py').write_text('VALUE = 1\n')
        (addon / 'LICENSE').write_text('Synthetic test fixture, no release authority.\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
                 'commit', '-qm', 'Fixture')
        bound = patch.object(packager, 'ROOT', self.root)
        bound.start()
        self.addCleanup(bound.stop)

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root).decode().strip()

    def test_clean_inventory_and_bytes_bind_one_commit(self):
        version, files, commit = packager.read_runtime()
        self.assertEqual(version, '0.1.2')
        self.assertEqual(files['default.py'], b'VALUE = 1\n')
        self.assertEqual(commit, self.git('rev-parse', 'HEAD'))
        self.assertEqual(set(files), {'addon.xml', 'default.py', 'LICENSE'})

    def test_assume_unchanged_cannot_hide_runtime_drift(self):
        self.assert_hidden_drift_rejected('--assume-unchanged')

    def test_skip_worktree_cannot_hide_runtime_drift(self):
        self.assert_hidden_drift_rejected('--skip-worktree')

    def assert_hidden_drift_rejected(self, flag):
        name = packager.ADDON_ID + '/default.py'
        self.git('update-index', flag, name)
        (self.root / name).write_text('VALUE = 2\n')
        self.assertEqual(self.git('status', '--porcelain'), '')
        with self.assertRaisesRegex(ValueError, 'Runtime bytes differ from source commit'):
            packager.read_runtime()

    def test_ordinary_runtime_edit_still_rejects(self):
        (self.root / packager.ADDON_ID / 'default.py').write_text('VALUE = 2\n')
        with self.assertRaisesRegex(ValueError, 'Runtime must match its source commit'):
            packager.read_runtime()


if __name__ == '__main__':
    unittest.main()
