# Channels DVR Hub — alpha tester guide (draft; not yet released)

**Status: preparation only. No Hub-identity native acceptance or public ZIP is claimed.**
Initial target: Kodi **21 Omega** and Channels DVR build **2026.08.07.0346 or newer**.
This is an unofficial Channels client, not affiliated with Channels or a replacement for Kodi PVR.

## Test environment: use a second DVR

For write-operation testing, use a **separate, disposable Channels DVR server** with its
own configuration and **distinct recording directory and database/state**. A second
process pointing at production data is not isolation. Avoid shared production tuners
or recording sources when experiments could interrupt active recordings. Verify your
server licensing, source permissions and tuner sharing independently.

If you cannot isolate the backend, **browse-only testing** is the safer scope:
do not create, adopt, edit, pause, cancel or remove schedules/passes/recordings.
Live tuning may consume shared tuner capacity even on an isolated DVR.

Keep a record of your test server build and test Kodi platform; never post private
server URLs, tokens, logs containing credentials, or recording names without review.

## Installation (only after a validated prerelease is published)

1. Obtain the versioned ZIP from this project's GitHub **pre-release**, not a source
   checkout or an unverified third-party mirror.
2. Verify its SHA-256 using the accompanying `.sha256` file.
3. Kodi: **Settings → Add-ons → Install from zip file**. Select the downloaded ZIP.
   Kodi may require enabling installation from unknown sources; assess that risk
   before enabling it and prefer turning it off afterward.
4. Open **Add-ons → Program add-ons → Channels DVR Hub**.
5. Enter the **test** DVR origin under Connection; it deliberately starts blank.
   Confirm the exact target before using any write action.
6. Do not point Hub and the former `script.channelsdvr.epg` client at the same
   mutable state simultaneously. Reconcile uncertain old writes before Hub writes.

## Suggested tester scenarios

- [ ] Fresh install and launch, then Kodi restart; add-on still opens.
- [ ] Blank server and offline server show actionable non-destructive errors.
- [ ] Connect to the intended test server; report build and connection state.
- [ ] Dashboard and Guide render; move focus, scroll, navigate back.
- [ ] Browse recordings, schedule list, passes and search; check stale/unknown labels.
- [ ] OK opens details, not playback; Play explicitly opens a completed test recording.
- [ ] Stop, resume, seek and audio behavior; note codec and Kodi host.
- [ ] Live tune only when spare tuner/source resources are known; then stop playback.
- [ ] With **disposable test content only**, schedule a future recording, verify
      backend result; test cancellation after explicit confirmation.
- [ ] With **disposable test rules only**, preview, create and manage a pass;
      review ownership checks and explicit confirmation.
- [ ] Simulate a disconnected/timeout response without retrying an ambiguous write.
      Preserve the profile/operation history for investigation.

**Important:** Kodi UI, backend mutation, Series CRUD/adoption, audio, seeking and
platform acceptance are **not yet qualified**. Do not mark a scenario passed based
solely on offline fake-adapter tests. Record **PASS / FAIL / BLOCKED / NOT TESTED**.

## Bug reports

Open a GitHub issue with: add-on version and release ZIP hash; Kodi version and OS;
Channels DVR build; affected screen and action; expected/actual behavior; whether
the outcome of a write is **uncertain**; reproduction steps using disposable
data; redacted Kodi log (if appropriate). Do not repeat a timed-out create/delete
to reproduce a failure. Remove IPs, server URLs, credentials, recording metadata
and device IDs from attachments.
