# Channels DVR Hub for Kodi

**Unofficial Kodi Program add-on · Kodi 21 Omega · Published version 0.1.2 ALPHA (work in progress)**

Channels DVR Hub is an independent, pre-release client for an existing Channels DVR
server. Its goal is to provide guide, recording and pass workflows inside Kodi,
with explicit playback through Kodi's native player. It is **not** a Kodi PVR
replacement or a DVR server administration/update tool.

> **Alpha status:** The 0.1.2 package is published for testing, but connected
> DVR behavior, video/audio playback, seeking, scheduling, pass operations and
> failure recovery have **not** completed native acceptance testing for this
> release. Do not rely on this alpha for important recordings or DVR changes.
> This project is not affiliated with or endorsed by Channels or Fancy Bits, LLC.

## Install on Kodi 21 Omega

**Recommended: install from the Kodi repository** to use Kodi's normal
dependency handling and update mechanism.

1. In Kodi, open **Settings → File manager → Add source**.
2. Enter `https://kcook98765.github.io/kcook-lab-repo/` and name the source
   `kcook-lab-repo`.
3. Open **Settings → Add-ons → Install from zip file**. If Kodi requests it,
   enable **Unknown sources** in **Settings → System → Add-ons**, and review
   Kodi's warning.
4. Select `kcook-lab-repo` and install `repository.ai.kodi-0.1.1.zip`
   (or the current approved repository installer displayed there).
5. Open **Install from repository → kcook-lab-repo → Program add-ons →
   Channels DVR Hub**, then choose **Install**.
6. Launch it from **Add-ons → Program add-ons → Channels DVR Hub**.

**Alternative: install the add-on ZIP directly.**

1. Open the [Channels DVR Hub 0.1.2 Alpha GitHub release]
   (https://github.com/kcook98765/kcook-lab-repo/releases/tag/channelsdvr-hub-0.1.2-alpha).
2. Download **`script.channelsdvr.hub-0.1.2.zip`** (not the source-code ZIP).
3. Make the downloaded ZIP accessible to your Kodi device.
4. In Kodi, use **Settings → Add-ons → Install from zip file** and choose the
   downloaded ZIP. Enable **Unknown sources** if prompted.
5. Open **Add-ons → Program add-ons → Channels DVR Hub**.

Direct ZIP installation does not by itself enroll the add-on for updates from
this repository. For ongoing updates, install the repository as described above.

**Links:** [Published release and direct ZIP download]
(https://github.com/kcook98765/kcook-lab-repo/releases/tag/channelsdvr-hub-0.1.2-alpha)
· [Kodi repository project](https://github.com/kcook98765/kcook-lab-repo)
· [Report a problem](https://github.com/kcook98765/script.channelsdvr.hub/issues)

## Requirements and first connection

- **Kodi 21 Omega** is the only published, targeted Kodi release for this alpha;
  other Kodi versions and platforms have not been accepted.
- An existing, authorized **Channels DVR server** and any necessary subscription
  or content entitlements.
- Channels DVR backend build **`2026.08.07.0346` or newer**. This is a minimum
  version check, not a guarantee of compatibility with every later backend.
- Network access from Kodi to your own DVR server.

Hub starts with **no server address configured**. Use the add-on's
**Connection** settings to enter your DVR's numeric IP address; the published
0.1.2 package uses HTTP port **8089** if a port is omitted. Do not expose an
unauthenticated DVR server to the public internet.

## Functionality implemented in source

- **Dashboard and navigation:** Guide, Recordings, Scheduled, Pass list,
  Search and Connection. Counts distinguish server-reported totals from
  loaded, potentially stale or unknown data.
- **Guide and recordings:** Browse programme details, shows, seasons, episodes,
  recent recordings and movies; locally search loaded data. DVR, Imported
  and Unknown recording provenance are distinguished.
- **Deliberate playback:** **OK opens details**, not playback. An explicit
  **Play** action can hand an eligible completed recording or advertised live
  channel to Kodi's native player. Live tuning may need an additional
  confirmation; available tuner capacity is not assumed.
- **Recording and pass workflows:** Source support for future recording requests,
  programme-only passes and Series pass creation/management, with confirmation,
  ownership and uncertain-result safeguards. Existing Series passes require
  explicit adoption; Series editing is **name-only** and advanced conditions
  remain read-only.
- **Local state:** Operation history is stored in standard-library SQLite
  within Hub's own Kodi profile. Artwork and media are handled by Kodi.

These are **implemented source features, not confirmed working end-to-end** in
the published 0.1.2 alpha. There is no automatic old-EPG-profile migration,
watched/resume synchronization, arbitrary advanced-rule editing or native PVR
replacement. Backend write operations may persist after exiting Kodi. If a
write has an uncertain outcome, reconcile it before retrying.

## Development status and source versions

The [published 0.1.2 Alpha ZIP]
(https://github.com/kcook98765/kcook-lab-repo/releases/tag/channelsdvr-hub-0.1.2-alpha)
is hosted in **kcook-lab-repo** and listed in its Omega Program add-ons catalog.
The current source checkout in this repository still declares **0.1.0** in
`script.channelsdvr.hub/addon.xml`; that source-tree version is **not** the
version of the published 0.1.2 package. Use the versioned release ZIP or
repository feed for installation instead of packaging this checkout yourself.

The release has static/offline and packaging verification, but **0.1.2 has not
been natively exercised against a connected DVR**. Prior results for 0.1.0
or the former EPG add-on identity are not evidence that this release passed.

Development has used [Kodi Lab](https://forum.kodi.tv/showthread.php?tid=388999),
a project for managing and inspecting Kodi environments during agent-assisted
add-on development. That tooling does not substitute for live feature
acceptance. Contributions, bug reports and testing feedback are welcome via
[GitHub issues](https://github.com/kcook98765/script.channelsdvr.hub/issues).

## Documentation

- [User guide](docs/USERGUIDE.md): navigation, configuration and guarded actions.
- [Development](docs/DEVELOPMENT.md): source layout and offline-test scope.
- [Limitations](docs/LIMITATIONS.md): unsupported features and acceptance gaps.
- [Identity](docs/IDENTITY.md): separate profile and reconciliation boundaries.

**Notice:** This is an unofficial integration. Use only your own authorized
server and content. Provider authorization for all API operations used here
has not been obtained or verified; see the distribution release notes for
the unresolved limitations. Keep secrets out of Git.

Source licensed under the [MIT license](LICENSE).
