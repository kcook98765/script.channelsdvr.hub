# Channels DVR Hub for Kodi

**0.2.0 ALPHA · Kodi 21 Omega only · Unofficial Program add-on**

Channels DVR Hub connects Kodi to your existing Channels DVR. It provides a
Dashboard, a full-screen Guide, recording browsing, scheduling and pass
management, Search, and Settings. Managed Omega qualification was partial.
Use a secondary Channels DVR for testing where practical and check DVR changes
in the server before relying on them.

[Changelog](CHANGELOG.md) · [Known limitations](docs/LIMITATIONS.md) ·
[User guide](docs/USERGUIDE.md) · [Report an issue](https://github.com/kcook98765/script.channelsdvr.hub/issues)

## Install or update from kcook-lab-repo

On Kodi 21 Omega, open **Settings → Add-ons → Install from repository →
kcook-lab-repo → Program add-ons → Channels DVR Hub**. Choose **Install** for a
new installation or check the add-on's **Update** menu for 0.2.0. If Kodi still
shows 0.1.4, open the repository's information screen and choose **Check for
updates**, then reopen the Hub entry.

If you need the repository installer, add
`https://kcook98765.github.io/kcook-lab-repo/` in **Settings → File manager →
Add source**. In **Settings → Add-ons → Install from zip file**, select the
source and install the current `repository.ai.kodi` ZIP. Kodi may prompt you to
review and enable **Unknown sources**. Launch Hub from **Add-ons → Program
add-ons** after installation. The distribution repository tracks which build
is available through the public catalog.

[Repository installer and instructions](https://github.com/kcook98765/kcook-lab-repo)

## Configure your DVR

The DVR address starts blank. In Hub **Settings → Configure DVR address**, enter
an authorized DVR's numeric IP address and select **Connect**. HTTP port 8089 is
used when omitted. The backend version floor is `2026.08.07.0346`; future builds
may change response behavior. Use an authorized server and content with the
necessary subscriptions. Keep the DVR on your trusted network.

## Development and licensing

The release package is built from committed runtime files. Developers can use
`python3 -B tools/package.py` for a local Omega build and provenance. The GitHub
auto-generated source archive is for source inspection. See the [development
notes](docs/DEVELOPMENT.md), [profile identity](docs/IDENTITY.md), [material
origins](docs/MATERIALS.md), and [OpenCode distribution
integration](docs/OPENCODE-DISTRIBUTION.md).

Source is licensed under the [MIT license](LICENSE). This independent project
has no affiliation with or endorsement from Channels or Fancy Bits, LLC.
Provider authorization for all API operations and content rights remain
unverified. See [known limitations](docs/LIMITATIONS.md).
