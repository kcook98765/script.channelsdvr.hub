"""Minimum-policy copy;30539..30549 remain reserved historical, never reused."""
RESERVED_IDS = frozenset(range(30539, 30550))
ROWS = (
    ('minimum_build_status', 30550, 'Minimum supported DVR version: {minimum}\nReported DVR version: {reported}\nVersion check passed: {verified}'),
    ('minimum_build_policy', 30551, 'DVR versions 2026.08.07.0346 and newer are accepted. This version check does not prove API compatibility. API changes require manual testing and an add-on update if needed. No per-build setting update or confirmation is required.'),
    ('minimum_build_failed', 30552, 'The reported DVR version is below the minimum or is not a valid DVR timestamp. Listings are blocked until the version check passes.'),
    ('minimum_build_pending', 30553, 'No current status observation. Choose Connect to check the configured DVR.'),
    ('minimum_build_observation_expired', 30554, 'This is a historical status observation. Choose Connect for a fresh check. It does not renew saved listings or allow recording changes.'),
    ('compact_live_banner', 30555, '{status} | {age}{cache}'),
    ('compact_cache_suffix', 30556, ' | {cache}'),
    ('compact_cache_saved', 30557, 'Saved listings'),
    ('compact_cache_stale', 30558, 'Saved listings (stale)'),
    ('listing_warning_hint', 30559, 'Listing information limited — see Connection status.'),
    ('warning_details_heading', 30560, 'Listing information'),
    ('file_scan_detail', 30561, 'Recording files were read in separate pages, not as an atomic snapshot. Listings may have changed during loading.'),
    ('public_station_detail', 30562, '{details}\nThese counts describe public station fields in the saved channel list, not channels without programme listings. DVR station mapping and tuning availability remain unverified.'),
    ('guide_station_detail', 30563, 'Some Guide station details could not be confirmed. This does not establish missing programme listings or the DVR channel mapping.'),
    ('warning_read_unavailable', 30564, 'Some DVR information could not be read. Previous listings, if present, retain their original update time.'),
    ('warning_response_unusable', 30565, 'Some returned listing data could not be used. Failed or incomplete updates do not replace saved listings.'),
    ('warning_response_unsupported', 30566, 'Some DVR information is not supported by this add-on and was not used.'),
    ('warning_file_incomplete', 30567, 'The recording-file scan was incomplete or changed during reading. It is not a complete or atomic view of DVR files.'),
    ('warning_unknown_fields', 30568, 'Some listing identifiers, timing, watch/resume or padding fields could not be confirmed. Unknown values remain unknown.'),
    ('warning_no_visible_channels', 30569, 'The channel list returned no visible channels for this view.'),
)
