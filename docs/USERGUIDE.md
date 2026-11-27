# Channels DVR Hub user guide

This guide covers Channels DVR Hub for Kodi 21 / Omega and describes source
behavior, not platform acceptance. See the README for published packages and the
distinction between published releases and development candidates.

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
Hub**. The Program script opens a Dashboard with Guide, Recordings, DVR, Search
and Settings navigation. It is not a native PVR replacement.

Dashboard cards are read-only summaries. Reported totals and accepted loaded
listings can differ. Unknown / unavailable is not zero or No, and a reported tuner
count is not a count of free tuners.

### Dashboard statistics

Statistics are read immediately when Dashboard first becomes eligible on the
explicitly connected source. **Refresh statistics** requests another read; repeated
clicks during a read use the same in-flight cycle. Displayed values stay in place
while it runs. This does not refresh guide or recording listings.

In add-on settings → **Dashboard → Automatic statistics refresh**, choose **1, 2,
5 (default), 10, 15 or 30 minutes**, or **Disabled**. Disabled stops periodic reads,
not the initial or manual read. Invalid/missing saved choices safely use 5 minutes.
The interval runs from the previous cycle's completion, including a failed cycle.
Reads pause away from Dashboard; returning, including Back, refreshes if due.
Selecting the already active Dashboard does not restart the interval. These
settings do not enable a connection or change any recording/pass permissions.

One small **Last updated** label above the cards shows the local absolute
date/time (including UTC offset) of the last successful coherent statistics cycle,
or **Never**. Kodi's regional date/time order is used when available, with a local
`YYYY-MM-DD HH:MM:SS ±HHMM` fallback. All routes must be accepted and no older
values retained for the timestamp to advance. A fresh unreported optional field
is Unknown, not a failed read. Failed routes or actual retention of earlier field
values are marked partial/stale and do not advance the successful timestamp.
One separate consolidated warning indicates partial, failed or stale statistics.
Retrying does not hide that warning; a healthy statistics read clears the relevant
condition. There is no routine Refreshing/Complete status or elapsed server runtime.

A changed source or accepted backend-version scope cannot reuse earlier
statistics as fresh values. Complete statistics become stale after the selected
interval (5 minutes when automatic refresh is disabled), measured monotonically
from acceptance rather than by wall-clock changes. This is presentation only,
not a recording/pass eligibility or action-safety check.

**Independently loaded listings (cache)** describes accepted listing scans, not
the statistics request. Counts and the guide window can differ from server totals.
Counts have no individual status or age labels. Independent listing expiry does not
show a statistics warning or repaint Dashboard. Statistics success does not renew
listing caches or change their cadence. The timestamp does not date these listings.
Dashboard has
no rolling clock or age repaint; a freshness-boundary warning changes only once.
Other views keep their own status, refresh and safety behavior.

An on-time automatic statistics read does not briefly show a stale warning while
running. This presentation grace allows one second of dispatch jitter and lasts
at most the read's 25-second budget. Partial/failed results, delayed reads, late
return to Dashboard and stale statistics with automatic refresh disabled still warn.

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

### Search local listings

Use the Hub's **Search** destination for **All**, **Guide programmes**,
**Recordings**, **Scheduled recordings** or **Passes**. From Guide, Recordings or
DVR, the Search command under **More** opens that category and Back returns to
the invoking view. The Recordings tab named Search is still the local library
filter. In unified Search, enter up to 128 characters. All groups results by
category and shows up to 100 across the groups; select a category to search it
separately if earlier groups fill the page.

Search does not ask the DVR for more listings. **Coverage** shows each selected
category's availability and limits, including the accepted Guide time window
and selected channel collection. An accepted empty listing is not the same as
an unavailable listing. Live passes are searchable only after an accepted
Passes browse from the current source. Older or incomplete listings can remain
visible with a warning and do not guarantee that a result can be opened.

Use **Info** for details, **Open recording** for recording details, or **Show
in Guide / DVR** to reveal the selected listing. Back from a reveal returns to
the Search result if it is still present. These actions do not start playback
or schedule, cancel or change anything on the DVR.

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
