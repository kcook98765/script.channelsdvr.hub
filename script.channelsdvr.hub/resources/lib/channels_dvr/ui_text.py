"""Pure, stable English catalog and immutable owner-captured text.

Capture once after the real Addon is assigned. Only strings cross to workers;
neither the provider nor a window is retained. Language changes apply on reopen.
Provider data is a format VALUE, never a message key or a template.
"""
from string import Formatter
from types import MappingProxyType
from collections.abc import Callable, Mapping
from .ui_operations_catalog import ROWS as OPERATION_ROWS
from .ui_final_catalog import ROWS as FINAL_ROWS
from .ui_build_catalog import ROWS as BUILD_ROWS
from .ui_build_catalog import RESERVED_IDS as BUILD_RESERVED_IDS
from .ui_artwork_catalog import ROWS as ARTWORK_ROWS
from .ui_artwork_catalog import RESERVED_IDS as ARTWORK_RESERVED_IDS
from .storage_catalog import ROWS as STORAGE_ROWS
from .series_manager_catalog import ROWS as SERIES_ROWS
from .playback_catalog import ROWS as PLAYBACK_ROWS
from .dashboard_catalog import ROWS as DASHBOARD_ROWS

_ROWS = (
    ('connection', 30000, 'Connection'),
    ('address', 30001, 'DVR address'),
    ('address_help', 30002, 'Enter only the approved development DVR address, without credentials or a path. Leave blank until configured.'),
    ('build', 30003, 'Minimum supported DVR version'),
    ('build_help', 30004, 'DVR versions 2026.08.07.0346 and newer are accepted. The minimum is fixed by this add-on; the saved legacy build value is retained but does not change it. API changes require manual testing and an add-on update if needed. Development DVR only; no account or production-server access.'),
    ('connect_on_open', 30005, 'Connect when the guide opens'),
    ('connect_help', 30006, 'Load listings from the configured DVR when the guide opens. Turning this off leaves the guide disconnected. Scheduling and pass changes always need separate confirmation; existing DVR rules keep running.'),
    ('developer', 30100, 'Developer tools'),
    ('show_developer', 30101, 'Show developer tools'),
    ('developer_help', 30102, 'Show demo listings and fixed diagnostic tools. Does not change connection permissions or recording safeguards. Demo listings open only when you choose Open demo guide.'),
    ('guide', 30106, 'Guide'),
    ('recordings', 30107, 'Recordings'),
    ('scheduled', 30108, 'Scheduled'),
    ('search_programmes', 30109, 'Search programmes'),
    ('recording_tools', 30110, 'Recording tools'),
    ('collection', 30111, 'Channel collection'),
    ('now', 30112, 'Now'),
    ('date_time', 30113, 'Date and time'),
    ('previous_hour', 30114, 'Previous hour'),
    ('next_hour', 30115, 'Next hour'),
    ('schedule', 30116, 'Schedule selected programme'),
    ('recording_status', 30117, 'Recording status and cancel'),
    ('programme_tools', 30118, 'Programme pass tools'),
    ('settings', 30119, 'Settings'),
    ('exit', 30120, 'Exit'),
    ('configure', 30121, 'Configure DVR address'),
    ('connect', 30122, 'Connect'),
    ('refresh', 30123, 'Refresh listings'),
    ('connection_status', 30124, 'Connection status'),
    ('disconnect', 30125, 'Disconnect'),
    ('connection_settings', 30126, 'Connection settings'),
    ('connect_first', 30127, 'Connect to your DVR first.'),
    ('configure_first', 30128, 'In Settings or Connection > Configure DVR address, set a numeric IP, then Connect. Uses HTTP port 8089; no hostnames.'),
    ('not_connected', 30129, 'Not connected'),
    ('connection_prompt', 30130, 'Connect to your DVR to load listings. Open Connection to configure an approved DVR address and connect.'),
    ('disconnect_help', 30131, 'Disconnecting this guide does not stop DVR recordings or remove saved passes.'),
    ('header', 30132, 'Channels DVR Hub | {view}'),
    ('demo_guide', 30133, 'Demo guide'),
    ('listings_footer', 30135, 'DVR listings • [?] End time unknown • Times include UTC offset'),
    ('demo_footer', 30136, 'Sample listings — not your DVR'),
    ('no_listings', 30137, 'No listings loaded'),
    ('preparing_guide', 30138, 'Preparing guide…'),
    ('guide_unavailable', 30139, 'Guide unavailable'),
    ('empty_update', 30140, 'No listings in the last successful update'),
    ('empty_view', 30141, '{view}: {status}'),
    ('refresh_hint', 30142, 'Menu: Connection → Refresh listings.'),
    ('demo_hint', 30143, 'Developer tools → Demo data status → Recover. OK explains this Demo state.'),
    ('search', 30144, 'Search'),
    ('recent', 30145, 'Recent recordings'),
    ('movies', 30146, 'Movies'),
    ('open_demo', 30147, 'Open demo guide'),
    ('close_demo', 30148, 'Close demo guide'),
    ('demo_status', 30149, 'Demo data status'),
    ('demo_stress', 30150, 'Demo guide stress test / restore'),
    ('demo_exercise', 30151, 'Fixed UI exercises'),
    ('index_status', 30152, 'Guide index status'),
    ('timing', 30153, 'UI timing diagnostics'),
    ('connection_diagnostics', 30154, 'Detailed connection diagnostics'),
    ('demo_first', 30155, 'Open a demo guide from Developer tools to use sample scenarios and exercises.'),
    ('all_channels', 30156, 'All channels'),
    ('visible_channels', 30157, 'Visible channels'),
    ('favourites', 30158, 'Favourites'),
    ('station_unavailable', 30159, 'Station details unavailable'),
    ('station_blank', 30160, 'Station details blank'),
    ('general', 30161, 'General'),
    ('empty_collection', 30162, 'Empty demo collection'),
    ('connection_counts', 30163, 'Channels: {channels}; programmes: {events}\nRecordings: {recordings}; scheduled items: {jobs}'),
    ('partial_read', 30164, 'Listings were read in parts and may have changed during loading.'),
    ('search_enter', 30165, 'Enter search text…'),
    ('search_previous', 30166, 'Previous results'),
    ('search_clear', 30167, 'Clear search'),
    ('search_input', 30168, 'Search programmes (up to 128 characters)'),
    ('search_limit', 30169, 'Use up to 128 characters for your search.'),
    ('choose_date', 30170, 'Choose date and hour'),
    ('earliest', 30171, 'Earliest listing time'),
    ('latest', 30172, 'Latest listing time'),
    ('date_offset', 30173, 'Date and hour (with UTC offset)'),
    ('preview', 30174, 'Preview matching airings'),
    ('saved_series', 30175, 'Saved passes for this series (read only)'),
    ('prepare_pass', 30176, 'Prepare programme pass'),
    ('pass_status', 30177, 'Programme pass status and remove'),
    ('operation_history', 30178, 'Saved operation history (read only)'),
    ('history_warning', 30179, "Saved local history, not the DVR's current state. It does not prove ownership or allow changes. Archived records cannot Save or Remove. No automatic changes or retries."),
    ('history_local', 30180, "This view uses the worker's last saved copy. Reopening it does not read the DVR or reload history from disk."),
    ('archived', 30181, 'Archived'),
    ('current_record', 30182, 'Current saved record'),
    ('unavailable', 30183, 'Unavailable'),
    ('not_recorded', 30184, 'Not recorded'),
    ('history_row', 30185, '#{sequence} {location} | Saved status: {state} | {title}'),
    ('history_details', 30186, 'Operation #{sequence} — {location}\nSaved status: {state}\nSelected programme: {title}\nStart: {start}\nEnd: {end}\nProgramme-only identifiers: EQ.SeriesID + EQ.ProgramID (private values not shown)\nDVR returned an identifier: {returned}\nSaved removal check: {proof}; not a current absence check.\n\n{local}\n\n{warning}'),
    ('history_empty', 30187, 'No local history is present in this saved copy. This does not prove that the DVR has no rules.'),
    ('time_unavailable', 30188, 'Time unavailable (outside the supported range)'),
    ('state_create_unknown', 30189, 'Creation not confirmed'),
    ('state_owned', 30190, 'Verified at the saved observation'),
    ('state_remove_unknown', 30191, 'Removal not confirmed'),
    ('state_removed', 30192, 'Removal saved'),
    ('yes', 30193, 'Yes'),
    ('no', 30194, 'No'),
) + OPERATION_ROWS + FINAL_ROWS + BUILD_ROWS + ARTWORK_ROWS + STORAGE_ROWS + SERIES_ROWS + PLAYBACK_ROWS + DASHBOARD_ROWS
IDS = MappingProxyType({key: number for key, number, _ in _ROWS})
FALLBACK = MappingProxyType({key: value for key, _, value in _ROWS})
RESERVED_IDS = BUILD_RESERVED_IDS | ARTWORK_RESERVED_IDS | frozenset((30103, 30104, 30105, 30134, 30498, 30703, 30710))
if set(IDS.values()) & RESERVED_IDS:
    raise ValueError('Retired localization IDs must never be reused')


def _fields(template: str) -> frozenset[str]:
    fields = []
    for _, field, spec, conversion in Formatter().parse(template):
        if field is not None:
            if not field.isidentifier() or spec or conversion:
                raise ValueError('only named value placeholders are supported')
            fields.append(field)
    return frozenset(fields)


def capture(getter: Callable[[int], str] | None = None, *, keys=None) -> Mapping[str, str]:
    """Owner calls the supplied Kodi Addon getter once per ID, no lazy callback."""
    values = {}
    required = tuple(FALLBACK) if keys is None else tuple(dict.fromkeys(keys))
    for key in required:
        english = FALLBACK[key]
        value = english
        if getter is not None:
            try:
                translated = getter(IDS[key])
                if (type(translated) is str and translated.strip()
                        and translated.strip() not in (str(IDS[key]), '#' + str(IDS[key]))
                        and _fields(translated) == _fields(english)):
                    value = translated
            except Exception:
                pass  # SDK failure or bad translation: deterministic English.
        values[key] = value
    return MappingProxyType(values)


def text(strings: Mapping[str, str], key: str, **values: object) -> str:
    english = FALLBACK[key]
    template = strings.get(key, english)
    try:
        return template.format(**values)
    except (ValueError, KeyError, AttributeError, IndexError, TypeError):
        return english.format(**values)
