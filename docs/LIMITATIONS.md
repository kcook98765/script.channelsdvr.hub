# Limitations and acceptance status

## Pre-release status

Channels DVR Hub `0.1.0` targets Kodi 21 / Omega. **Hub has not yet been installed
or natively accepted under its own identity.** Results from an earlier EPG identity
do not establish Hub acceptance. Source tests, fake adapters and static validation
are useful evidence, but are not full functional or release acceptance.

Broader platform coverage, audio, seeking and native Series create/read/update/
delete workflows and adoption remain unaccepted. Implemented source behavior must
not be presented as completed native validation. A portable design and an
all-platform manifest are not proof that every host, skin or embedded Python
environment works.

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
  details, there is no autoplay, and Guide exit does not automatically stop playback.
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

The public offline runner covers an explicit 15-module subset (372 tests in the
initial Python 3.12 run). Broader Dashboard UI, recording/pass lifecycle, recovery
and archive-replay cohorts are not included. See [Development](DEVELOPMENT.md).
A passing subset is not full regression coverage or native acceptance.
