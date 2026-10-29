"""Pure immutable command tables. Labels never become internal state."""
from .ui_text import text

MAIN = (
    ('view.guide', 'guide'), ('view.recordings', 'recordings'),
    ('view.scheduled', 'scheduled'), ('search.programmes', 'search_programmes'),
    ('recording.tools', 'recording_tools'), ('guide.collection', 'collection'),
    ('guide.now', 'now'), ('guide.time', 'date_time'),
    ('guide.previous', 'previous_hour'), ('guide.next', 'next_hour'),
    ('connection.menu', 'connection'), ('recording.schedule', 'schedule'),
    ('recording.status', 'recording_status'), ('programme.tools', 'programme_tools'),
    ('addon.settings', 'settings'),
    ('series.manager', 'series_manager'),
)
CONNECTION = (
    ('connection.configure', 'configure'), ('connection.connect', 'connect'),
    ('connection.refresh', 'refresh'), ('connection.status', 'connection_status'),
    ('connection.disconnect', 'disconnect'), ('connection.settings', 'connection_settings'),
    ('connection.storage', 'storage_title'),
)
PROGRAMME = (
    ('programme.preview', 'preview'), ('programme.saved', 'saved_series'),
    ('programme.prepare', 'prepare_pass'), ('programme.status', 'pass_status'),
    ('programme.history', 'operation_history'),
)
SEARCH = (('search.enter', 'search_enter'), ('search.previous', 'search_previous'),
          ('search.clear', 'search_clear'))
DEVELOPER = (
    ('demo.open', 'open_demo'), ('demo.close', 'close_demo'),
    ('demo.status', 'demo_status'), ('demo.stress', 'demo_stress'),
    ('demo.exercise', 'demo_exercise'), ('diagnostic.index', 'index_status'),
    ('diagnostic.timing', 'timing'), ('diagnostic.connection', 'connection_diagnostics'),
)
VIEW_KEYS = {'Dashboard': 'dashboard', 'Guide': 'guide', 'Recordings': 'recordings', 'Scheduled': 'scheduled', 'Search': 'search'}
GROUP_KEYS = {'All': 'all_channels', 'Visible': 'visible_channels', 'Favorites': 'favourites',
              'Guide station unqualified': 'station_unavailable', 'Guide station missing': 'station_blank',
              'General': 'general', 'Empty': 'empty_collection'}


def main_entries(developer: bool, playback_label='play_recording'):
    return MAIN + (('playback.play', playback_label),) + ((('developer.menu', 'developer'),) if developer is True else ()) + (('window.exit', 'exit'),)


def command(entries, answer):
    return entries[answer][0] if type(answer) is int and 0 <= answer < len(entries) else None


def select(dialog, strings, heading, entries):
    # Capture before opening the modal, which may reenter settings/window logic.
    captured = tuple(entries)
    return command(captured, dialog.select(text(strings, heading), [text(strings, key) for _, key in captured]))


def group_label(strings, group):
    key = GROUP_KEYS.get(group)
    return text(strings, key) if key else group
