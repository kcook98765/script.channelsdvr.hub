"""Release ZIP builder tests; no Kodi, network or live backend."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("alpha_builder", ROOT / "tools/build_alpha.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class ReleaseBuilderTests(unittest.TestCase):
    def test_repository_archive_is_deterministic_and_installable_shape(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            first = builder.build_archive(output=out)
            data = (out / Path(first["archive"]).name).read_bytes()
            second = builder.build_archive(output=out)
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(data, Path(second["archive"]).read_bytes())
            self.assertEqual(second["status"], "packaged_not_native_accepted")
            with zipfile.ZipFile(second["archive"]) as zf:
                names = zf.namelist()
                self.assertIn("script.channelsdvr.hub/addon.xml", names)
                self.assertTrue(all(name.startswith("script.channelsdvr.hub/") for name in names))
                self.assertFalse(any("/tests/" in name or "__pycache__" in name for name in names))
            self.assertEqual(Path(second["sha256_file"]).read_text(),
                             f'{second["sha256"]}  {Path(second["archive"]).name}\n')

    def test_rejects_unexpected_file_and_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "script.channelsdvr.hub"
            path.mkdir()
            (path / "stray.secret").write_text("secret")
            with self.assertRaisesRegex(ValueError, "unexpected release file"):
                builder._files(path)
            (path / "stray.secret").unlink()
            (path / "link.py").symlink_to(Path(__file__))
            with self.assertRaisesRegex(ValueError, "unsafe file"):
                builder._files(path)

    def test_rejects_missing_required_content(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "script.channelsdvr.hub"
            path.mkdir()
            (path / "addon.xml").write_text('<addon id="script.channelsdvr.hub" version="0.1.0"/>')
            with self.assertRaisesRegex(ValueError, "missing release files"):
                builder._files(path)


if __name__ == "__main__":
    unittest.main()
