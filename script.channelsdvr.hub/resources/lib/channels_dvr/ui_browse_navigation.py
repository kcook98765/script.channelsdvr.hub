"""Bounded owner selectors; captured IDs/values never derive from translated labels."""
from bisect import bisect_right

from .guide import time_label
from .search import normalize_query
from .ui_menu import SEARCH, command, select
from .ui_modal import dialog_for
from .ui_routes import enter, route_fence


def admitted(w, gui):
    if w._closed or w._development_blocked is not None or (w._live_session is None and not w.demo_allowed()):
        dialog_for(w, gui).ok(w.t('connection'), w.t('connect_first'))
        return False
    return True


def search_menu(w, gui, *, recordings=False):
    if not admitted(w, gui):
        return
    presets = ('atlas', 'chapter', 'planet', 'no-match-xyz') if recordings else ('special', 'programme 4', 'unavailable', 'no-match-xyz')
    entries = tuple((code, w.t('recording_search_enter') if recordings and code == 'search.enter' else w.t(key))
                    for code, key in SEARCH)
    if w.demo_allowed():
        entries += tuple((f'preset.{i}', w.t('demo_preset', query=query)) for i, query in enumerate(presets))
    captured = tuple(entries)
    title = w.t('search_recordings' if recordings else 'search_programmes')
    chosen = command(captured, dialog_for(w, gui).select(title, [label for _, label in captured]))
    if chosen is None or not admitted(w, gui):
        return
    value = None
    if chosen == 'search.enter':
        candidate = dialog_for(w, gui).input(w.t('recording_search_input' if recordings else 'search_input'),
            defaultt=w.library.query if recordings else w.search_query, type=gui.INPUT_ALPHANUM)
        if candidate:
            value = candidate
    elif chosen == 'search.clear':
        value = ''
    elif chosen == 'search.previous':
        if recordings:
            w._set_library_mode('Search')
        target = 'Recordings' if recordings else 'Search'
        if w.view != target:
            enter(w, target)
    elif chosen is not None and chosen.startswith('preset.') and w.demo_allowed():
        value = presets[int(chosen.removeprefix('preset.'))]
    if value is not None and admitted(w, gui):
        try:
            normalize_query(value)
            if recordings:
                context = w.library._context()
                w.library.set_query(value)
                if w.view != 'Recordings':
                    enter(w, 'Recordings')
                elif w.library._context() != context:
                    route_fence(w)
            else:
                w._set_search_query(value)
                if w.view != 'Search':
                    enter(w, 'Search')
        except ValueError:
            dialog_for(w, gui).ok(title, w.t('search_limit'))


def recording_tools(w, gui):
    if not admitted(w, gui):
        return
    entries = (('recording.search', 'search_recordings'), ('recording.source', 'recording_source'),
               ('recording.shows', 'browse_shows'), ('recording.recent', 'recent'), ('recording.movies', 'movies'))
    chosen = select(dialog_for(w, gui), w.strings, 'recording_tools', entries)
    if chosen is None or not admitted(w, gui):
        return
    if chosen == 'recording.search':
        search_menu(w, gui, recordings=True)
    elif chosen == 'recording.source':
        sources = (('all', 'all_sources'), ('dvr', 'dvr_recordings'), ('import', 'imported_media'), ('unknown', 'unknown_source'))
        selected = command(sources, dialog_for(w, gui).select(w.t('recording_source'), [w.t(key) for _, key in sources],
            preselect=next(i for i, row in enumerate(sources) if row[0] == w.library.provenance_filter)))
        if selected is not None and admitted(w, gui):
            changed = selected != w.library.provenance_filter
            if changed:
                w.library.set_provenance_filter(selected)
            if w.view != 'Recordings':
                enter(w, 'Recordings')
            elif changed:
                route_fence(w)
    elif chosen in ('recording.shows', 'recording.recent', 'recording.movies'):
        w._set_library_mode({'recording.shows': 'Shows', 'recording.recent': 'Recent', 'recording.movies': 'Movies'}[chosen])
        if w.view != 'Recordings':
            enter(w, 'Recordings')


def time_navigation(w, gui):
    if not admitted(w, gui):
        return
    entries = (('time.choose', 'choose_date'), ('time.first', 'earliest'), ('time.last', 'latest'))
    chosen = select(dialog_for(w, gui), w.strings, 'date_time', entries)
    if chosen is None or not admitted(w, gui):
        return
    start, end = w.repository.horizon
    if chosen in ('time.first', 'time.last'):
        w._jump_time(start if chosen == 'time.first' else end - 1)
    elif chosen == 'time.choose':
        if w._live_session is not None:
            now = max(0, w.utc_now())
            hour = now - now % 3600
            hours = tuple(range(max(0, hour - 86400), hour + 86401, 3600))
        else:
            hours = tuple(range(start, end, 3600))
        preselect = max(0, min(len(hours) - 1, bisect_right(hours, w.guide.selection.anchor) - 1))
        # The selected UTC instant is data, never a localized caption.
        captured = tuple((hour, '') for hour in hours)
        instant = command(captured, dialog_for(w, gui).select(w.t('date_offset'), [time_label(hour) for hour in hours], preselect=preselect))
        if instant is not None and admitted(w, gui):
            w._jump_time(instant)
