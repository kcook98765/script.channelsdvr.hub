# Changelog

## 0.2.0 ALPHA for Kodi 21 Omega

- Shared Dashboard, Guide, Recordings, DVR, Search and Settings navigation with
  contextual actions, logical Back and focus restoration.
- Recordings adds Recent, TV Shows, Movies, All and Search categories, with
  source filters, sorting and show → season → episode browsing.
- DVR brings Scheduled and Passes together with supported confirmation,
  ownership and readback safeguards.
- The full-screen Guide adds proportional programme cells, channel logos,
  time and group controls, artwork and programme details.
- Unified Search groups results across loaded guide, recordings, scheduled
  recordings and passes and shows where listing coverage is incomplete.
- Settings brings connection and diagnostic tools together with native Kodi
  preferences. Startup and browsing memory are configurable per DVR.
- Kodi playback defaults to full screen. Background browsing is optional.
  Programme selection opens details by default and optional live selection
  still requires explicit confirmation.

Managed Omega testing covered installation, upgrade, and representative
navigation, DVR and retained-recording workflows. Qualification remains partial.
See the [known limitations](docs/LIMITATIONS.md) before testing.

## 0.1.4 ALPHA for Kodi 21 Omega

The Dashboard uses one absolute Last updated timestamp and a single warning when
statistics are stale, partial or unavailable. Manual refresh and configurable
automatic refresh were retained.

[0.1.4 distribution release](https://github.com/kcook98765/kcook-lab-repo/releases/tag/channelsdvr-hub-0.1.4-alpha)
