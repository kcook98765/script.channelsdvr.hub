# Hub #17: player ownership contract (source only)

[`tests/player_ownership_model.py`](../tests/player_ownership_model.py) is an
offline prototype outside the installable add-on. It is not wired to the window,
worker, or native player and does not authorize the existing Play handoff.

- `begin()` replaces the one tracked intent and withdraws prior control
  authority; it does not stop prior playback. `mark_sent()` records a dispatch
  once, after the existing selection/source lease, live confirmation and fresh
  recheck, and one-use native admission. An async send is not an AV start.
- After a send, only exact media **and** the independently verified session for
  that admission can establish `active`. Different media or a proved different
  session is `unrelated` before confirmation (the async send may still start)
  and `replaced` afterward (retired). A failed, conflicting, or incomplete read
  is `unconfirmed` and withholds controls. A positive idle read after confirmed
  activity retires the attempt even when the cause is unknown. `stopped`,
  `ended`, and `error` require positive idle plus separately attributable
  terminal evidence for the admitted session. Idle during startup alone is not
  a failure; the owner expires unproved sends at a bounded deadline, without retry.
- The owner mints a single-use ticket **before** each read; a newer read, a new
  intent, or retirement invalidates it. Do not re-stamp old observations. A
  control recheck requires an already-active intent and a separate, synchronous
  read from the attempt captured by the UI command. Neither a cached `active`
  state nor a regular poll authorizes a control. Cancel/close only detaches.

## Omega 21.3 observation (source, not natively qualified)

Hub's parent WindowXML receives activation via `onClick`. The raw
`ACTION_MOUSE_LEFT_CLICK` notification does not enter the command mailbox;
actual click callbacks (even stale targets) and every other action still do.
This handles either callback order without pairing, timing, or reissuing a
Play lease. None of the existing final handoff or live consent guards is removed.
The owner rechecks the active Hub window, its modal fence, and Kodi's topmost
modal before opening live confirmation, on return, and at final admission.
On Omega 21.3, `getCurrentWindowDialogId()` must return `WINDOW_INVALID` (9999);
the base window can still be Hub when an unrelated modal covers it. A failed
read blocks playback. This check does not change navigation or dialog focus
return. A raw-only click with no Hub command or sampled foreground change does
not revoke Play. These point-in-time checks do not establish continuous
foreground authority, infer a pointer target, or detect non-modal overlays.

Live review requires a fresh descriptor (0–30 seconds in the same monotonic
clock) at the dialog boundary. Only Yes on that exact display label and source
scope can authorize a new read; the new build, advertised source and pin flag
must match exactly, in addition to the existing input, selection and source
lease. The first locator is not retained in consent. A logical scope is not
server authentication, and no recheck establishes physical tuner capacity.

The script calls `Player.play` once, with `windowed=False` by default and
`windowed=True` only when the native background preference is literally true.
This requests presentation, not playback success. After
an admitted recording send attempt, `NativePlayer.recording_readback()` can
sample the recording on the owner. The readback in Connection status
is a closed code (`recording_exact_video`, `recording_player_idle`,
`recording_other_active`, or `recording_readback_unknown`). The sample does not
require a callback, does not run continuously, and is absent without an admitted
recording send attempt. A stable but different locator is a nonmatch, never a
normalized alias; an external player, failed or inconsistent reads are unknown.
Even a stable exact locator identifies media, not a native start. No new player
controls or backend checks are wired. If `Player.play`
raises after admission, the status is `handoff_unknown`: the same readback is
available, but neither it nor advisory callbacks prove what that send did.
There is no automatic retry. A new explicit Play detaches the older observer
without stopping Kodi, so its callbacks and readback never describe the new intent.

## Preview limits and background gate

In the earlier private retained-file run, explicit Recording Play reached Kodi
and opened one recording stream, but video remained at the spinner during the
observation. Physical Kodi Stop returned to browsing with idle readback, and
the observed stream later cleared. The Watch Live check reached default No and
was declined without tuning. A bounded retest on prior candidate `6dfd117`
showed fullscreen video for the retained recording, with Kodi start and close
logs. This does not qualify audio, a live Yes, or the revised source here.

The separate silent synthetic MP4 fixture on Omega/Estuary established fullscreen
pixels with `windowed=False`, keyboard pause/resume, and return to its validation
window with exact-video-to-idle readback on Kodi Stop. `windowed=True` left that
window foreground while the file played and also returned to idle on Stop.
This qualifies the SDK behavior in that fixture, not this merged Hub WindowXML's
background browsing, focus, or route restoration, nor decoded provider media.
Physical Guide OK callback delivery on the merged Hub also remains unqualified.

The standalone native Playback settings both default off and are independent of
Phase 6 route/bookmark settings. The background flag is sampled only at handoff;
it issues no window/focus, restart, or control command. Guide OK is limited to a
focused content activation on the active primary Guide. A fresh READY listing
and an exact visible indexed airing with a known end and `start <= now < end`
are required. The same accepted snapshot, index ticket and airing must remain
current through the default-No question and final handoff, under the original
input lease and a five-minute OK deadline. The second metadata result still
must match the reviewed station label, provider scope and pin. Pointer hits,
Info, past/future or unknown-end airings, gaps, and Search do not use this opt-in.

## Future ownership integration (plan)

1. Sample on the window's owning execution path (`process_pending` /
   `PlaybackUI.poll`, or immediately in the owner-side control handler). The
   metadata worker stays read-only and does not call the Kodi SDK. Kodi player
   callbacks may signal a need for an owner read; the observer instance and its
   captured attempt are not evidence of which native session generated it.
2. The readback uses `xbmc.Player.isPlaying()` and, while active,
   `getPlayingFile()`, with `isPlayingVideo()` / `isPlayingAudio()` as context.
   Bracket the file read with activity/type checks; don't treat paused playback,
   lost focus, a failed file read, or lack of video as idle. Use positive inactivity reads
   for idle. If needed, corroborate in-process via `xbmc.executeJSONRPC`
   using read-only `Player.GetActivePlayers` and `Player.GetItem`.
   These calls are not atomic and the Kodi `playerid`, type,
   player wrapper and file are not per-start identity. Ambiguous, unsupported,
   rewritten or inconsistent readbacks remain unconfirmed.
3. Keep locators in the adapter. For exact comparable observations, feed the
   reducer a keyed digest (e.g. HMAC-SHA-256 with a process-local key) of the
   full locator, with the same encoding and key as the admitted locator. Do not
   strip queries, normalize paths, or match display titles or substrings.
   Never log or expose locators, digests, keys, item payloads, or private
   tickets/session identities. Only publish the closed status.
4. Reserve an opaque session identity at send only if the adapter has a way to
   independently correlate it later to that native start, including in the
   face of a same-file restart/takeover. Do not synthesize correlation from
   callback order, an observed idle-to-active edge, a player slot, or exact
   media alone. The Omega reads above do **not** establish such correlation or
   securely attribute stop/end/error callbacks. Without additional qualified
   evidence, pass no session and leave compact controls unavailable. Even with
   a matching read, don't dispatch a control if the native operation could
   target a replaced player between read and dispatch.
5. Keep browsing suspension and route/selection restoration independent of
   this model. The desired full-screen default and optional background mode
   need merged-Hub qualification (see [Omega Player.cpp](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/Player.cpp));
   no custom browse restoration or Stop behavior is wired. Physical Stop,
   active-to-idle readback, and backend stream/tuner release are separate claims.

## Focused offline check

```sh
PYTHONPATH=script.channelsdvr.hub/resources/lib:tests python3 -B -m unittest test_navigation test_ui_dispatch test_playback_adapter test_native_playback test_player_ownership -v
PYTHONPATH=script.channelsdvr.hub/resources/lib:tests python3 -B -m unittest test_package -v
```

The cohort uses explicit doubles, not Kodi modules/stubs or providers. For later
add-on validation use the isolated Omega lane: record `stub-lanes` and run
Toolkit `validate` with `--branch omega` under the same interpreter. This is
separate from the pure regressions and from native acceptance.
