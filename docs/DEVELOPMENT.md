# Development

Channels DVR Hub is pre-release `0.1.0` source targeting Kodi 21 / Omega. Its sole
executable manifest extension is a Program script using `default.py`.
`plugin.py` is documentation-only, not a video-plugin entry point.

## Source layout

- `script.channelsdvr.hub/`: installable add-on source and manifest.
- `script.channelsdvr.hub/resources/lib/channels_dvr/`: Python implementation.
- `script.channelsdvr.hub/resources/`: settings, translations, artwork and UI assets.
- `tests/`: the selected pure-Python and explicit fake-adapter regression modules.
- `tools/test_offline.py`: the bounded offline runner.

The runtime uses Python's standard library, including SQLite for operation state.
Artwork and playback are delegated to Kodi. No external image-processing package
or player is required. Kodi SDK modules and entry points must execute inside Kodi;
outside Kodi, tests use explicit fakes rather than executing SDK modules or stubs.

## Offline baseline and public runner

**Python 3.12 is the initial development baseline**, not a statement about Kodi's
embedded interpreter or a guarantee for every Python version. The public
offline subset needs only the standard library. IANA time-zone data is optional:
tests requiring an unavailable zone should be reported as skipped, not silently
treated as passed.

From the repository root, the documented runner invocation is:

```sh
python3 -B tools/test_offline.py --quiet
```

The public scope is an **explicit 15-module subset with 372 declared
test methods**. This is a scope declaration, not a claim that 372 tests have run
or passed. It omits broader Dashboard UI, recovery, lifecycle and archive-dependent
cohorts; it must not be described as full test discovery or full acceptance.

The exact module names are in `MODULES` in the runner. It does not discover other
tests from the local development archive. Complete output and a JSON summary are
written to **`.local/test-results/`**, excluded from Git. Any test failure, import
error or blocked network/process operation makes the command exit nonzero.

The initial repository intentionally omits workstation validation tools, live API
experiments and historical fixtures. A clone does not require those private files.

## Test boundaries

The runner installs a process-local audit tripwire for socket and selected process
operations. This is **defense in depth, not a sandbox**: it neither isolates the
filesystem nor proves arbitrary test code cannot have side effects. Review the
selected modules and their imports before treating a cohort as offline.

Pure models, temporary SQLite databases and fake Kodi adapters can check contracts
and failure paths. They do not prove live backend behavior, native window focus,
artwork delivery, playback, physical storage durability or platform acceptance.
Static validation likewise does not execute or accept the Kodi UI.

Preserve confirmation, verified ownership, source binding and uncertain-write
barriers when changing code. An ambiguous write requires reconciliation before
retrying. A fresh profile is not a recovery shortcut. Keep secrets out of Git.
See [Identity](IDENTITY.md) for the old EPG boundary and [Limitations](LIMITATIONS.md)
for the remaining acceptance gaps.
