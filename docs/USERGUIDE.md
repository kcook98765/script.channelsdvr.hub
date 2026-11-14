# Channels DVR Hub user guide

Hub `0.1.1` is pre-release source for Kodi 21 / Omega. It has not yet been installed
or natively accepted under the Hub identity. The workflows below describe source
behavior, not a completed installation or platform acceptance test.

## Configuration and entry

The DVR address starts **blank**. In **Settings > DVR IP address** or
**Connection > Configure DVR address**, enter the approved DVR's numeric IP address
(for example `192.0.2.10`). Both accept the same formats: a bare IP,
`192.0.2.10:8089`, `http://192.0.2.10`, or `http://192.0.2.10:8089`, with an
optional trailing `/` on the HTTP address. HTTP port **8089** is used when omitted.
IPv6 literals are accepted; use brackets in HTTP addresses, for example
`http://[2001:db8::10]:8089`. Hostnames, HTTPS, other ports, credentials and paths
are not supported. The project supplies no default server address.

Save the address, then choose **Connection > Connect**. This also turns on
**Connect automatically when Hub opens** for future opens. Saving an address
alone does not enable a disconnected Hub. Turning the automatic connection
setting off disconnects Hub. Reading a saved address normalizes it in memory
without rewriting the saved value or legacy version setting.
The minimum valid backend build is `2026.08.07.0346` or newer. A newer version
passing the floor does not establish compatibility with every future API change.

Once installed, the intended entry is **Add-ons → Program add-ons → Channels DVR
Hub**. The Program script opens a Dashboard with Guide, Recordings, Scheduled,
Pass list, Search and Connection navigation. It is not a native PVR replacement.

Dashboard cards are read-only summaries. Reported totals and accepted loaded
listings can differ; check their ages and availability. Unknown is not zero, and
a reported tuner count is not a count of free tuners.

## Browsing and playback

- Browse the Guide and open programme details with OK. Gaps and missing data do
  not imply that a programme is available to record or play.
- Browse recordings by show, season and episode, Recent or Movies, or search
  loaded data locally. DVR, Imported and Unknown provenance stay distinct.
- **OK opens details; it does not start playback.** An explicit Play action can
  hand an eligible completed DVR recording or advertised live channel to Kodi's
  native player after fresh checks. Imported or unknown-provenance entries are
  not automatically eligible for DVR playback.
- Live tuning requires separate consent when shared tuner availability is unknown.
  Channels DVR may use another available source; a guide selection does not prove
  a particular tuner is free or will be used.
- There is no autoplay. Closing the Guide or changing the connected source does
  not automatically stop playback. Use Kodi's playback controls to stop it.

Kodi loads, decodes, scales and caches artwork. Missing artwork can leave text-only
presentation; the client does not provide a separate image-processing pipeline.

## Scheduling and passes

Preparing a future recording or previewing a pass is separate from confirming a
save. Review the selected programme, time, scope and preview before confirmation.
Preview matches do not guarantee future coverage, and saved backend rules remain
on the DVR after Kodi exits.

Management is limited to supported, verified owned operations. Seeing a backend
item in a list does not itself establish authority to change it. Existing Series
passes require **explicit adoption** before supported management; matching names
or similar conditions do not establish ownership. Series management implements
create, adoption, Name editing, pause/resume and removal in source. Editing is
**Name-only**; advanced rule conditions are read-only.

Keep confirmations and ownership checks intact. If a write has an uncertain
outcome, do not repeat it blindly or use a new profile to bypass the barrier.
Retain operation history and reconcile current backend state first. Unavailable
or unsafe storage can block writes rather than permit an untracked operation.

## Local state and readiness

Operation history uses standard-library SQLite in Hub's own Kodi profile and
inherits that profile's privacy. It is not an additional OS-level privacy or
durability guarantee. Keep secrets out of Git.

Former EPG settings and journals are not copied automatically. Before Hub writes,
reconcile unresolved old operations and establish an exclusive writer as described
in [Identity](IDENTITY.md). See [Limitations](LIMITATIONS.md) for native acceptance
gaps, especially Series CRUD/adoption, audio, seeking and broader platforms.
