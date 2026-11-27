# Hub Settings and startup (#16) — integration handoff

- The Hub Settings destination keeps Configure, Connect, Refresh, Status and Disconnect separate. Storage and read-only diagnostics are also available from More. Kodi add-on preferences use the existing settings dialog and configuration readback. Native preferences do not enable the connection or rewrite its address by themselves.
- Launch without an argument starts on Dashboard. A closed startup preference may redirect only after the initial configured source publishes accepted listings and the owner can claim the still-current launch intent. Missing, disabled, rejected and failed admission stays on Dashboard. Explicit launch arguments, manual routes and accepted focus travel take precedence. A refresh or a later Connect does not re-arm startup. No search, playback, or DVR control is dispatched by startup.
- Guide content OK, Info, Details, and pointer selection open Details. Explicit Watch remains a separate command with its existing checks. A stored legacy `live` value does not enable OK-to-watch in Phase 6.
- Bookmarks use the existing codec and profile store. The owner sends only identity-scoped snapshots to one serialized worker; source changes discard queued old work and ignore late reads. An already started write is confined to its original server slot. Writes follow explicit changes, not launch, paint, or restore, and shutdown does not block on profile I/O. True missing slots can be initialized; unreadable, unsupported, and wrong-source slots are kept intact. An explicit tab choice wins over the pending restore for its own connection incarnation. Reopen resolves Guide group/channel at Now inside the accepted horizon, a valid recording leaf and its Back cursors, browse options, and the DVR tab. A different connection starts at Now; deferred Guide requests never carry time between connections. Search drill-ins never replace the primary Guide bookmark.

## Phase 7 Guide OK handoff

Do not expose the live choice or wire Guide OK to Watch until #17 is accepted. If enabled then, require a fresh, positive, non-stale accepted Guide publication, the exact current source and primary Guide airing, and a known end time with `start <= now < end`. Recheck those facts before preparation, on confirmation and again at the native handoff; do not copy the looser age policy of the separate explicit Watch command. Keep the existing one-use confirmation and fresh playback descriptor checks. Any failed or expired OK eligibility falls back to Details before preparation, or cancels an in-flight request; never substitute a different airing or channel. Keep the fresh and expired listing regressions when reintroducing the option.

## Unified Search bookmarks

The Search input chooser offers recent terms only for the currently admitted source. Choosing a recent term prefills the stock Search input; cancelling that input does nothing. An explicit valid submission searches the current category and updates the bounded, newest-first `recent_terms` through the existing serialized bookmark worker. The chooser revalidates the source after modal return. Demo and disconnected inputs are not saved, and restore never fills the active query, changes tabs, runs a search, or writes a bookmark. Result IDs, airing times and drafts stay out of the profile.

The Settings preferences row still uses the stock Kodi add-on settings dialog. The fake regression checks dispatch, input fencing and parent focus on return. Native Omega dialog visibility and focus remain a separate checkpoint.

## Independent gates

From the worktree root, the focused offline cohort is:

```sh
PYTHONPATH=script.channelsdvr.hub/resources/lib:tests python3 -B -m unittest test_navigation test_search test_connection_config test_bookmark_storage test_dashboard test_package test_native_playback -v
```

Run the matching Omega Toolkit static lane separately. The fake cohort is not native UI/playback or physical-storage qualification.
