# First alpha readiness — 2026-10-29

Baseline reviewed: `main` commit `9c966f10ce41338d4bb050b328b792695eede9ae`.
This is a **static source and documentation audit**, not an executed regression or
native Kodi/Channels DVR certification.

## Confirmed source structure

- Add-on ID `script.channelsdvr.hub`; Program script `default.py`; `addon.xml`
  version `0.1.0`; `xbmc.python` 3.0.1; Kodi 21 Omega target.
- One installable add-on directory, with local `resources/lib/channels_dvr`,
  settings, strings, skins and artwork; no third-party runtime Python dependency.
- The public `tools/test_offline.py` is a guarded, **selected** 15-module runner.
  It documents 372 declared cases, not a fresh execution result or full coverage.
- Explicitly blank server origin; backend minimum build 2026.08.07.0346.
- Source implements explicit playback, guarded writes, ownership and uncertainty
  checkpoints, and a separate profile from `script.channelsdvr.epg`.
- No GitHub releases at audit time; source documentation states Hub has never been
  installed/natively accepted under its own identity.

## Phase 1 release assets

- `tools/build_alpha.py`: deterministic ZIP and SHA-256 sidecar with a
  strict package-file allowlist, file-count/size limits and no server access.
- `tests/test_release_builder.py`: deterministic and rejection-path tests.
- `docs/ALPHA-TESTING.md`: isolated secondary server, installation/testing,
  redaction and no-automatic-retry guidance.
- No add-on version bump, behavioral mutation, release tag or public release
  until native acceptance and owner approval.

## Gates for OpenCode on Claw (not yet executed)

1. Confirm clean clone and Python 3.12; run
   `python3 -B tools/test_offline.py --quiet` and
   `python3 -B -m unittest discover -s tests -p test_release_builder.py -v`.
   Public runner is deliberately limited; consider separately qualified tests
   for high-risk recording/pass lifecycles and UI integration.
2. Build: `python3 -B tools/build_alpha.py`; verify checksum with
   `cd dist && sha256sum -c script.channelsdvr.hub-0.1.0.zip.sha256`.
   Verify top-level ZIP directory and actual installation into a **fresh**
   managed Kodi 21.3 instance. No archive should contain private configuration.
3. Native checks: first launch, Connection, Dashboard, Guide, remote focus,
   recordings, explicit Play, audio and seek, close/restart and error states.
4. Isolated DVR: read-only checks first, then carefully staged disposable
   recording and pass writes; record receipts and unknown outcomes. Never use
   a production DVR as the write target.
5. Decide whether the stable manifest version remains `0.1.0` for first alpha,
   or must be changed with coordinated package/test/docs updates. Never ship a
   ZIP version not matching its manifest.
6. Redact logs, review known limitations, attach checksum and publish an
   explicitly marked **GitHub prerelease** only after sign-off.

## Remaining constraints and risks

- Kodi/embedded Python/platform behavior untested under Hub identity.
- Live backend API compatibility is not established by the minimum build floor.
- Broad Dashboard, write lifecycle, recovery and archive cohorts are not in
  the selected public offline runner.
- All-platform manifest is not an all-platform test claim; Nexus, Matrix and
  Piers are not alpha gates.
- Secondary DVR must have truly separate configuration and storage; shared
  tuner capacity and migration of old EPG state need manual safeguards.

## Scope policy

Do not auto-migrate EPG history, change ownership semantics, replay an ambiguous
write, add feature work, or publish the GitHub release during Phase 1.
