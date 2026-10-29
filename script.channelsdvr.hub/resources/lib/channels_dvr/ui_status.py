"""Pure status projection; text caching never caches age, TTL or permissions."""
from .ui_text import FALLBACK, text

STATUS_KEYS = {'ready': 'live_ready', 'empty': 'live_empty', 'outage': 'live_outage',
               'partial': 'live_partial', 'malformed': 'live_malformed', 'unsupported': 'live_unsupported'}


def status_banner(state, loading, updated_at, age, stale, strings=FALLBACK, *, demo=False,
                  compact=False):
    key = 'live_loading' if loading else STATUS_KEYS.get(state.value if state else '', 'no_listings')
    if demo and key == 'live_ready':
        key = 'demo_ready'
    if compact:
        cache = (text(strings, 'cache_none') if updated_at is None else
                 text(strings, 'compact_cache_stale') if stale else
                 text(strings, 'compact_cache_saved') if loading else '')
        result = text(strings, 'compact_live_banner', status=text(strings, key),
            age=text(strings, 'update_age_unknown') if age is None else text(strings, 'updated_ago', count=age),
            cache=text(strings, 'compact_cache_suffix', cache=cache) if cache else '')
        return text(strings, 'demo_banner', status=result) if demo else result
    result = text(strings, 'status_banner', status=text(strings, key),
        cache=text(strings, 'cache_none' if updated_at is None else 'cache_previous' if stale else 'cache_fresh'),
        age=text(strings, 'update_age_unknown') if age is None else text(strings, 'updated_ago', count=age))
    return text(strings, 'demo_banner', status=result) if demo else result


# Closed code-to-copy groups. No backend field, exception or warning string is
# interpolated into the normal scroller. Counts come from the saved public DTO
# projection, not from Guide coverage, tuning or station-mapping inference.
_WARNING_GROUPS = (
    ('warning_read_unavailable', frozenset(('metadata_outage', 'guide_channels_unavailable'))),
    ('warning_response_unusable', frozenset((
        'metadata_malformed', 'invalid_files_page', 'invalid_channels_shape',
        'missing_channel_name', 'conflicting_channel', 'invalid_channel',
        'invalid_guide_window', 'invalid_guide_shape', 'invalid_guide_row',
        'unknown_guide_channel', 'invalid_guide_airing', 'conflicting_event',
        'invalid_files_shape', 'files_total_mismatch', 'invalid_file', 'conflicting_file',
        'invalid_jobs_shape', 'invalid_job', 'unknown_job_channel_join', 'conflicting_job'))),
    ('warning_response_unsupported', frozenset(('metadata_unsupported',))),
    ('warning_file_incomplete', frozenset((
        'files_total_changed', 'files_no_progress', 'files_early_empty', 'files_page_limit'))),
    ('warning_unknown_fields', frozenset((
        'missing_program_id', 'unknown_guide_duration', 'unknown_file_duration',
        'fractional_duration_unknown', 'unknown_file_watched', 'unknown_file_resume', 'unknown_job_padding'))),
    ('warning_no_visible_channels', frozenset(('guide_no_visible_channels',))),
)


def listing_warning_details(codes, public_details='', guide_warnings=(), strings=FALLBACK):
    """Full read-only meaning, independent of banner length and browse admission."""
    codes = frozenset(code for code in codes if type(code) is str)
    guide_codes = frozenset(code for code in guide_warnings if type(code) is str)
    parts = []
    if 'offset_scan_not_snapshot_consistent' in codes:
        parts.append(text(strings, 'partial_read') + '\n' + text(strings, 'file_scan_detail'))
    if public_details:
        parts.append(text(strings, 'public_station_detail', details=public_details.strip(' |')))
    if (codes | guide_codes) & {'guide_station_unqualified', 'guide_station_missing'}:
        parts.append(text(strings, 'guide_station_detail'))
    parts.extend(text(strings, key) for key, group in _WARNING_GROUPS if codes & group)
    return (text(strings, 'warning_details_heading') + '\n' + '\n\n'.join(parts)) if parts else ''
