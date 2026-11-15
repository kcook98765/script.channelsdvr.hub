# Channels DVR Hub for Kodi

**ID `script.channelsdvr.hub` · version `0.1.2` · Program add-on · Kodi 21 / Omega**

An unofficial, pre-release Channels DVR client focused on recordings and passes,
with a guide and explicit native Kodi playback. It is not affiliated with Channels,
does not replace Kodi's PVR system, and does not administer or update the DVR server.

**Alpha candidate, not yet published:** This 0.1.2 candidate has not been installed
or natively accepted. Historical Hub 0.1.0 and EPG observations do not accept it. Source and fake-adapter tests are not full feature acceptance.
The repository contains source, offline tests and a reproducible local candidate
packager. No Hub release is approved or published.

Prepare the Omega candidate with `python3 -B tools/package.py` after the runtime
changes have been reviewed and committed. See [Development](docs/DEVELOPMENT.md).

## Implemented in source

- A Program-script Dashboard with Guide, Recordings, Scheduled, Pass list, Search
  and Connection navigation. Counts distinguish server reports from loaded data;
  stale, unavailable and unknown values are not presented as current facts.
- Recording browsing and local search with DVR / Imported / Unknown provenance.
  **OK opens details.** Playback requires an explicit **Play** action; there is no
  autoplay and leaving the Guide does not stop playback.
- Future single-recording scheduling and programme-only / Series pass management
  with separate confirmations, verified ownership and uncertainty guards. Existing
  Series require explicit adoption; editing is **Name-only**, with advanced rule
  conditions read-only.
- Standard-library SQLite operation storage in Hub's own Kodi profile, native Kodi
  artwork loading and native Kodi playback. No external image-processing library
  or external player is required.

## Requirements and configuration

The source targets **Kodi 21 / Omega**. The Channels DVR server origin defaults to
**blank**: configure your chosen server through Connection before connecting.
No server address is supplied by this project.

The minimum valid backend build is **`2026.08.07.0346` or newer**, separate from
Hub's `0.1.2` version. Passing that version floor is an admission check, not proof
that every future server API or behavior is compatible. Platform, audio, seek and
native Series CRUD/adoption acceptance remain incomplete.

Hub has a separate add-on identity and profile from the former EPG add-on. There
is no automatic old-profile discovery, copying or migration. Reconcile unresolved
old operations and establish an exclusive writer before any Hub writes.

## Kodi distribution preparation

The canonical distribution repository is
**https://github.com/kcook98765/kcook-lab-repo**; this source repository remains
https://github.com/kcook98765/script.channelsdvr.hub. Do not repoint its origin.
Use the separately installed `/kodi-release-prepare` command or `github-operator`
from this source project without selecting the distribution project. See
[OpenCode setup restoration and the exact 0.1.2 evidence binding](docs/OPENCODE-DISTRIBUTION.md).
Preparation supplies no native acceptance or publication authority.

## Documentation

- [User guide](docs/USERGUIDE.md): configuration, navigation and guarded actions.
- [Material origins](docs/MATERIALS.md): code provenance, generated artwork and notices.
- [Development](docs/DEVELOPMENT.md): source layout and offline-test scope.
- [Limitations](docs/LIMITATIONS.md): unsupported features and acceptance gaps.
- [Identity](docs/IDENTITY.md): separate profile and reconciliation boundaries.

Keep secrets out of Git. Licensed under the [MIT license](LICENSE).
