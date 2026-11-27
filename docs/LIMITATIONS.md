# Limitations and acceptance status

## 0.2.0 ALPHA acceptance

Kodi 21 Omega is the only supported distribution lane. Managed native tests
covered representative changed workflows and preserved settings on upgrade.
Qualification remains **PARTIAL**.

- Recording playback can intermittently stall on Kodi's loading spinner.
  Decoded full-screen playback, background browsing and Stop were observed on
  retained-recording checkpoints. Audible playback and native seek remain open.
- Live confirmation was tested and declined. Actual live playback and tuner
  release await supervised qualification. Compact Now Playing controls are
  incomplete. [Playback #17](https://github.com/kcook98765/script.channelsdvr.hub/issues/17)
- Some recording totals and storage statistics can be Unknown. A reported
  tuner count or idle activity cannot establish free tuner capacity. Dashboard
  text may clip at some viewport sizes. [Statistics #6](https://github.com/kcook98765/script.channelsdvr.hub/issues/6) · [Clipping #9](https://github.com/kcook98765/script.channelsdvr.hub/issues/9)
- Native switching between two admitted DVRs, Guide-origin pass creation and
  some recovery scenarios remain outside the observed coverage. Search uses
  bounded loaded listings and discloses partial coverage.

Testing against a secondary DVR is recommended where practical. This is an
independent integration. Use only an authorized DVR and permitted content.
Provider permission and content authorization for all operations remain
unverified. [Channels terms](https://getchannels.com/tos/) · [Server API](https://getchannels.com/docs/server-api/introduction/)

## Backend compatibility and data

The backend version floor admits valid builds `2026.08.07.0346` or newer. It is not
a universal future-API compatibility guarantee. Changes to response shapes or
behavior can still cause guarded operations to refuse or require investigation.

Loaded data is bounded and may be stale or incomplete. Dashboard server-reported
totals, loaded listing counts, zero and Unknown have different meanings. Preview
matches do not guarantee future recording coverage. Reported tuners and empty
activity lists do not prove future free capacity or exclusive use of a source.

## Feature boundaries

- Hub is not a native PVR replacement or a DVR administration/update tool.
- Arbitrary advanced rule editing is unavailable. Series editing is Name-only;
  advanced conditions are read-only and existing Series require explicit adoption.
- DVR watched/resume synchronization is not provided.
- Playback requires an explicit action and fresh eligibility checks. OK opens
  details by default; the primary Guide opt-in still requires live confirmation.
  There is no autoplay, and Guide exit does not automatically stop playback.
- Native Kodi handles artwork and media. The client does not guarantee artwork
  delivery, formats, cache behavior, audio, seeking or playback on every platform.

## Storage, ownership and uncertain outcomes

Standard-library SQLite uses Hub's own Kodi profile and inherits its privacy.
There is no additional OS ACL guarantee or universal physical-durability claim.
Unsupported or unsafe storage may disable writes.

Read visibility is not write ownership. Confirmations, explicit adoption and
operation history constrain supported mutations. Timeouts or ambiguous responses
can leave an uncertain outcome; they are not permission to retry blindly. Preserve
the history and reconcile before further writes.

Hub does not automatically discover, copy or migrate the old EPG profile. Separate
profiles do not make concurrent writers safe. Reconcile unresolved old operations
and establish an exclusive writer first; see [Identity](IDENTITY.md).

The public offline runner covers a subset of the regressions. Source and static
checks complement the scoped native evidence. See [Development](DEVELOPMENT.md).
