"""Native playback copy; stable IDs independent of Series/local storage copy."""
ROWS = (
    ('playback', 30700, 'Playback'),
    ('play_live', 30701, 'Watch selected channel LIVE NOW'),
    ('play_recording', 30702, 'Play selected recording from beginning'),
    ('play_failed', 30704, 'Playback could not be prepared or handed to Kodi. No automatic retry. A file may be missing, incomplete or unsupported, the DVR may be busy, or metadata may be unavailable. See Connection status for the playback state.'),
    ('play_source_pin', 30705, 'The advertised URL selects this DVR source; hardware pinning is not established.'),
    ('play_source_any', 30706, 'The DVR chooses the source (ANY); the selected guide source is not pinned.'),
    ('play_live_confirm', 30707, 'Shared tuner availability is UNKNOWN. Empty DVR activity does not prove a free shared tuner. Confirm only if you know capacity is free. Never stop or evict other use.\n\n{source}\n\nWatch this channel LIVE NOW? This does not play the selected past/future programme from its beginning.'),
    ('play_diagnostic', 30708, 'Native playback state: {state}\nA handoff is not confirmed AV/audio/seek/stop success. Terminal callbacks are advisory. Kodi owns media HTTP and player controls; the DVR may internally remux/transcode. Recordings start from the beginning; DVR resume/watched synchronization is not implemented. Exiting or disconnecting this guide does not stop playback.'),
    ('recording_details', 30709, '{title}\n{episode}\n{state}\n{origin}\nDuration: {duration}\nAdded: {time}\nDetails only. For eligible DVR recordings, choose Play recording. Playback starts from the beginning; watched/resume changes are not supported.'),
    ('live_consent_expired', 30711, 'Live confirmation expired. Choose Watch selected channel LIVE NOW again to prepare a new request. No tune was sent by this request.'),
    ('live_recheck_notice', 30712, 'Channel: {channel}\nYes checks this same channel again before tuning. No does not tune. Confirmation is one use and expires five minutes after Yes. Shared tuner availability remains UNKNOWN.'),
    ('play_read_hint_required', 30713, 'Select a recording file or channel in accepted, connected DVR listings. Selections are identity hints only; Play must read current DVR metadata. Folders, scheduled jobs, imports, demo and failed or incomplete listings cannot play.'),
)
