"""Build a deterministic, narrowly allowlisted Kodi installation ZIP.

No Kodi imports, network calls, dependencies, or release publication. Run from
any working directory: python3 -B tools/build_alpha.py
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "script.channelsdvr.hub"
DIST = ROOT / "dist"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
ROOT_FILES = frozenset({"addon.xml", "LICENSE", "default.py", "plugin.py"})
EXTENSIONS = frozenset({".py", ".xml", ".po", ".png"})
REQUIRED = frozenset({
    "addon.xml", "default.py", "resources/settings.xml",
    "resources/icon.png",
    "resources/skins/Default/1080i/ChannelsDvr.xml",
    "resources/language/resource.language.en_gb/strings.po",
})
MAX_FILES = 500
MAX_SINGLE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024


def _files(addon: Path) -> list[tuple[str, Path]]:
    if addon.is_symlink() or not addon.is_dir() or addon.name != "script.channelsdvr.hub":
        raise ValueError("invalid add-on directory")
    files: list[tuple[str, Path]] = []
    size = 0
    for directory, dirs, names in os.walk(addon, followlinks=False):
        for name in dirs:
            path = Path(directory) / name
            if path.is_symlink() or name.startswith("."):
                raise ValueError(f"unsafe directory: {path.relative_to(addon)}")
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(addon).as_posix()
            if path.is_symlink() or not path.is_file() or name.startswith("."):
                raise ValueError(f"unsafe file: {relative}")
            if "/" not in relative:
                allowed = relative in ROOT_FILES
            else:
                allowed = relative.startswith("resources/") and path.suffix.lower() in EXTENSIONS
            if not allowed:
                raise ValueError(f"unexpected release file: {relative}")
            stat = path.stat()
            if stat.st_size > MAX_SINGLE_BYTES:
                raise ValueError(f"oversized release file: {relative}")
            size += stat.st_size
            files.append((relative, path))
            if size > MAX_TOTAL_BYTES or len(files) > MAX_FILES:
                raise ValueError("release size/file-count limit exceeded")
    files.sort(key=lambda pair: pair[0])
    missing = REQUIRED - {name for name, _ in files}
    if missing:
        raise ValueError(f"missing release files: {', '.join(sorted(missing))}")
    return files


def _manifest(addon: Path) -> tuple[str, str]:
    try:
        root = ET.parse(addon / "addon.xml").getroot()
    except ET.ParseError as error:
        raise ValueError("invalid addon.xml") from error
    addon_id, version = root.get("id"), root.get("version")
    if (addon_id != addon.name
            or not isinstance(version, str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[~.-][a-zA-Z0-9_.-]+)?", version)):
        raise ValueError("invalid manifest identity or version")
    script = root.find("extension[@point='xbmc.python.script']")
    if script is None or script.get("library") != "default.py":
        raise ValueError("invalid Kodi entry point")
    imports = [(element.get("addon"), element.get("version"))
               for element in root.findall("requires/import")]
    if imports != [("xbmc.python", "3.0.1")]:
        raise ValueError("unexpected Kodi dependency")
    return addon_id, version


def build_archive(addon: Path = ADDON, output: Path = DIST) -> dict:
    """Validate source and produce ZIP plus matching SHA-256 file in output."""
    addon = Path(addon)
    files = _files(addon)
    addon_id, version = _manifest(addon)
    output = Path(output)
    if output.resolve() == addon.resolve() or addon.resolve() in output.resolve().parents:
        raise ValueError("output cannot be within the add-on directory")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{addon_id}-{version}.zip"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output, suffix=".zip.tmp", delete=False) as tmp:
            temporary = Path(tmp.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9, strict_timestamps=True) as zf:
            for relative, path in files:
                entry = zipfile.ZipInfo(f"{addon_id}/{relative}", FIXED_TIME)
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                with path.open("rb") as reader:
                    zf.writestr(entry, reader.read(), compress_type=zipfile.ZIP_DEFLATED,
                                compresslevel=9)
        with zipfile.ZipFile(temporary) as zf:
            if zf.testzip() is not None:
                raise ValueError("release ZIP integrity check failed")
            if zf.namelist() != [f"{addon_id}/{name}" for name, _ in files]:
                raise ValueError("release ZIP entry mismatch")
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        os.replace(temporary, archive)
        temporary = None
        sidecar = output / (archive.name + ".sha256")
        sidecar.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"archive": str(archive), "sha256_file": str(sidecar),
            "sha256": digest, "files": len(files), "addon_id": addon_id,
            "version": version, "status": "packaged_not_native_accepted"}


if __name__ == "__main__":
    print(json.dumps(build_archive(), indent=2))
