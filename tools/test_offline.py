"""Run pure/fake-adapter tests with a process-local network/process tripwire.

This is defense in depth, not an OS sandbox. Never executes Kodi stubs.
"""

import io
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "test_data", "test_guide", "test_search", "test_library", "test_metrics",
    "test_ui_dispatch", "test_metadata_http", "test_metadata_models",
    "test_metadata_repository", "test_live_loader", "test_session",
    "test_package", "test_artwork_native", "test_native_playback",
    "test_series_manager",
)


def main() -> int:
    if any(argument != "--quiet" for argument in sys.argv[1:]):
        raise ValueError("only --quiet is supported")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / "script.channelsdvr.hub" / "resources" / "lib"))
    sys.path.insert(0, str(ROOT / "tests"))
    blocked = []

    def offline_guard(event, args):
        if event.startswith("socket.") or event in ("subprocess.Popen", "os.system"):
            blocked.append(event)
            raise RuntimeError("Offline test guard blocked network/process operation")

    sys.addaudithook(offline_guard)
    suite = unittest.defaultTestLoader.loadTestsFromNames(MODULES)
    stream = io.StringIO()
    started = time.perf_counter()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    elapsed = time.perf_counter() - started
    text = stream.getvalue()
    evidence = ROOT / ".local" / "test-results"
    evidence.mkdir(parents=True, exist_ok=True)
    prefix = evidence / time.strftime("unit-%Y%m%dT%H%M%SZ", time.gmtime())
    prefix.with_suffix(".txt").write_text(text, encoding="utf-8")
    summary = {
        "tests": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "skipped": len(result.skipped),
        "blocked_operations": blocked,
        "passed": result.wasSuccessful() and not blocked,
        "scope": "Pure Python and explicit fake GUI; not Kodi runtime acceptance",
        "modules": list(MODULES),
        "seconds": round(elapsed, 3),
    }
    prefix.with_suffix(".json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if "--quiet" in sys.argv[1:] and summary["passed"]:
        print(json.dumps({**summary, "full_output": str(prefix.with_suffix(".txt").relative_to(ROOT))}, indent=2))
    else:
        print(text, end="")
    return 0 if result.wasSuccessful() and not blocked else 1


if __name__ == "__main__":
    raise SystemExit(main())
