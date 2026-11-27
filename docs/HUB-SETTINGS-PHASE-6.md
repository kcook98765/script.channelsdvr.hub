# Hub Settings and startup (#16) — integration handoff

- The Hub Settings destination keeps Configure, Connect, Refresh, Status and Disconnect separate. Storage and read-only diagnostics are also available from More. Kodi add-on preferences use the existing settings dialog and configuration readback. Native preferences do not enable the connection or rewrite its address by themselves.
- Launch without an argument starts on Dashboard. A closed startup preference may redirect only after the initial configured source publishes accepted listings and the owner can claim the still-current launch intent. Missing, disabled, rejected and failed admission stays on Dashboard. Explicit launch arguments and manual routes take precedence. A refresh or a later Connect does not re-arm startup. No search, playback, or DVR control is dispatched by startup.
- Guide content OK defaults to Details. The opt-in action requires the same accepted airing with a known end time to be on now, at request and again at the existing live confirmation/recheck/handoff gates. Info, Details, and pointer selection still open Details. A failed eligibility check does not substitute another airing or channel.
- Bookmarks use the existing codec and profile store. The owner sends only identity-scoped snapshots to one serialized worker; source changes discard queued old work and ignore late reads. An already started write is confined to its original server slot. Writes follow explicit changes, not launch, paint, or restore, and shutdown does not block on profile I/O. Reopen resolves Guide group/channel at Now inside the accepted horizon, valid recording leaf and browse options, and the DVR tab. Search drill-ins never replace the primary Guide bookmark.

## Unified Search handoff

Keep `recent_terms` in the v1 schema. Load it only for the matching binding into the unified Search history display (no restored active query, initial search, result row, airing time, or draft). When the user explicitly submits a valid term, merge that term into the current source's newest-first list using the codec's bounds and deduplication. Route it through the same owner-side overlay and serialized store submission; drop stale source/session receipts. Do not capture Settings inputs, cancelled edits or legacy Search state. This slice deliberately does not add that Search mapping.

## Independent gates

From the worktree root, the focused offline cohort is:

```sh
PYTHONPATH=script.channelsdvr.hub/resources/lib:tests python3 -B -m unittest test_navigation test_connection_config test_bookmark_storage test_dashboard test_package test_native_playback -v
```

Run the matching Omega Toolkit static lane separately. The fake cohort is not native UI/playback or physical-storage qualification.
