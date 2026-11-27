# Hub #17: player ownership contract (source only)

`player_ownership.py` is a pure, owner-local contract. It is not wired to the
window, worker, or native player. It neither authorizes the existing Play
handoff nor diagnoses the pre-handoff `selection_changed` failure.

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
Play lease. The existing final handoff and live consent gates are unchanged.

The script requests native video with `Player.play(..., windowed=False)` once.
That is the non-windowed start request, not proof of actual fullscreen. After
a successful recording handoff, `NativePlayer.recording_readback()` can sample
the last handed-off recording on the owner. The readback in Connection status
is a closed code (`recording_exact_video`, `recording_player_idle`,
`recording_other_active`, or `recording_readback_unknown`). The sample does not
require a callback, does not run continuously, and is absent before a successful
recording handoff. A stable but different locator is a nonmatch, never a
normalized alias; an external player, failed or inconsistent reads are unknown.
Even a stable exact locator identifies media, not a native start. No new player
controls, backend checks, or playback preferences are wired.

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
   need separate native qualification (see [Omega Player.cpp](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/Player.cpp));
   no background preference or browsing behavior is wired. Physical Stop,
   active-to-idle readback, and backend stream/tuner release are separate claims.

## Focused offline check

```sh
PYTHONPATH=script.channelsdvr.hub/resources/lib:tests python3 -B -m unittest test_navigation test_ui_dispatch test_playback_adapter test_player_ownership -v
PYTHONPATH=script.channelsdvr.hub/resources/lib:tests python3 -B -m unittest test_package.PackageTests.test_gui_settings_protocol_and_callbacks_are_closed_boundaries test_package.PackageTests.test_ast_no_playback_resolver_external_execution_or_persistent_writes -v
```

The cohort uses explicit doubles, not Kodi modules/stubs or providers. For later
add-on validation use the isolated Omega lane: record `stub-lanes` and run
Toolkit `validate` with `--branch omega` under the same interpreter. This is
separate from the pure regressions and from native acceptance.
