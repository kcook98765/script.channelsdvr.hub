# Development

Channels DVR Hub targets Kodi 21 / Omega. Its sole
executable manifest extension is a Program script using `default.py`.
`plugin.py` is documentation-only, not a video-plugin entry point.

## Source layout

- `script.channelsdvr.hub/`: installable add-on source and manifest.
- `script.channelsdvr.hub/resources/lib/channels_dvr/`: Python implementation.
- `script.channelsdvr.hub/resources/`: settings, translations, artwork and UI assets.
- `tests/`: the selected pure-Python and explicit fake-adapter regression modules.
- `tools/test_offline.py`: the bounded offline runner.
- `tools/package.py`: deterministic, exact-commit Omega candidate packaging.
- `tools/generate_artwork.py`: original icon/texture generation and byte checks.

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

## Focused Dashboard regressions (issue #4)

`tests/test_dashboard.py` is a separate targeted cohort, **not** part of the
15-module public runner above. Run the affected Dashboard/owner/config/package/live
paths, including the reconciled connection parser, with explicit fakes:

```sh
PYTHONPATH=script.channelsdvr.hub/resources/lib:tests python3 -B -m unittest test_dashboard test_ui_dispatch test_package test_live_loader test_connection_config -v
```

Coverage uses deterministic clocks, explicit fake Kodi controls/owner loops, fake
HTTP and a synchronized worker: initial/manual/disabled scheduling, Back/return
cadence, source/token rejection, healthy unknown fields versus retained partial
results, last success, regional absolute timestamps (including padding modifiers),
independent listing-cache state, and publication without hide/show or focus churn.
The original idle-repaint and changed-card hide/text/show regressions are included;
idle polls must neither format cards/timestamps nor translate strings. No Kodi
SDK/stubs or DVR is executed. The public runner's audit guard does not wrap this
standalone command; review this explicit cohort before execution.

The separate packager provenance cohort uses harmless Git commands in temporary
synthetic repositories, including fixture commits and index concealment flags:

```sh
PYTHONPATH=tests python3 -B -m unittest test_packager_provenance -v
```

Do not run it under the public runner's subprocess-forbidding guard or confuse its
fixture commits with changes to the source repository. Static checks require a
matched Omega interpreter, Kodistubs and checker/cache; `--branch omega` alone does
not select stubs. For private qualification use the exact reviewed, committed
add-on root (currently unpublished numeric `0.1.3`), checking for retained-version
collisions first. Preserve the add-on ID, profile and operation history. Source/ZIP
digests and native evidence stay private. Static/fake-GUI passes are not native
qualification or a release; prior `0.1.2` / `0.1.3~issue4` evidence does not qualify
the combined candidate.

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

## Local alpha packaging

Run `python3 -B tools/package.py` after committing reviewed runtime inputs. It
selects only Git-tracked runtime files, requires that subtree to match the source
commit, and produces an Omega-only candidate under `dist/omega/` with canonical
ZIP ordering/timestamps/modes, a SHA-256 sidecar and source/tool provenance. It
rejects replacement of different existing bytes. No Kodi code is imported.

The versioned packager derives from the separately hashed tool retained with the
0.1.1 handoff; the new source tracks it and narrows proposed lanes to Omega.
Frozen 0.1.1 packages remain unchanged. Packaging is not native acceptance,
rights approval, repository admission or publication authorization.
