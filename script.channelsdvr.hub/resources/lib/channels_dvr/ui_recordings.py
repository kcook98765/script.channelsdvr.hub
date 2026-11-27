"""Recordings-only chrome and local selectors, dispatched by the Hub owner."""
from .library import MODE_LABELS, SORT_LABELS
from .search import normalize_query
from .ui_menu import command
from .ui_modal import dialog_for, modal_input
from .ui_routes import route_fence


TAB_MODES = {f'recording.tab.{mode.lower()}': mode for mode in MODE_LABELS}
TABS = {220: 'recording.tab.recent', 221: 'recording.tab.shows', 222: 'recording.tab.movies',
        223: 'recording.tab.all', 224: 'recording.tab.search'}
SOURCE, SORT, QUERY = 320, 321, 322
TOOLS = {SOURCE: ('recording.source',), SORT: ('recording.sort',), QUERY: ('recording.query',)}
ACTION_SLOTS = (300, 301, *TOOLS, 304, 305)  # Fixed coordinates even when some commands are unavailable.
LABELS = {code: MODE_LABELS[mode] for code, mode in TAB_MODES.items()} | {
    'recording.source': 'recording_source', 'recording.sort': 'recordings_sort',
    'recording.query': 'search_recordings',
}
SOURCES = {'all': 'all_sources', 'dvr': 'dvr_recordings', 'import': 'imported_media',
           'unknown': 'unknown_source'}


def available(w, code):
    if w.view != 'Recordings':
        return False
    if code == 'recording.sort':
        return not w.library.depth
    if code == 'recording.query':
        return (w.library.mode == 'Search' and w._development_blocked is None
                and (w._live_session is not None or w.demo_allowed()))
    return True


def active_tab(w):
    return next(ident for ident, code in TABS.items() if TAB_MODES[code] == w.library.mode)


def _transition(w, change, origin):
    # Cover the native repaint as well as the logical change: a click arriving
    # mid-rebind must not become Select or Back in the new frame.
    with modal_input(w):
        change()
        route_fence(w)
        w.render()
        w._chrome.initialize_focus(origin)


def open_folder(w):
    row = w.library.selected
    if row is None or not row.children:
        return False
    _transition(w, w.library.open_selected, None)
    return True


def execute(w, gui, code, origin):
    if code in TAB_MODES:
        mode = TAB_MODES[code]
        if mode != w.library.mode:
            _transition(w, lambda: w.library.set_mode(mode), origin)
        return

    intent = w._chrome.command_activation(code, LABELS[code])

    def current():
        return w._owns_input() and w._chrome.command_current(intent)

    dialog = dialog_for(w, gui)
    if code == 'recording.query':
        query = _query(w, gui, dialog, current)
        if query is None or not current() or query == w.library.query:
            return
        try:
            normalize_query(query)
        except ValueError:
            dialog.ok(w.t('search_recordings'), w.t('search_limit'))
            return
        _transition(w, lambda: w.library.set_query(query), origin)
        return

    if code == 'recording.source':
        choices, selected, setter = SOURCES, w.library.provenance_filter, w.library.set_provenance_filter
    else:
        choices, selected, setter = SORT_LABELS, w.library.sort_order, w.library.set_sort
    entries = tuple(choices.items())
    index = dialog.select(w.t(LABELS[code]), [w.t(key) for _, key in entries],
                          preselect=tuple(choices).index(selected))
    chosen = command(entries, index)
    if chosen is not None and chosen != selected and current():
        _transition(w, lambda: setter(chosen), origin)


def _query(w, gui, dialog, current):
    # This is still the bounded local recording matcher, not programme Search.
    entries = [('enter', w.t('recording_search_enter')), ('clear', w.t('search_clear'))]
    presets = ('atlas', 'chapter', 'planet', 'no-match-xyz') if w.demo_allowed() else ()
    entries += [(f'preset.{i}', w.t('demo_preset', query=query)) for i, query in enumerate(presets)]
    chosen = command(tuple(entries), dialog.select(w.t('search_recordings'), [label for _, label in entries]))
    if chosen is None or not current():
        return None
    if chosen == 'enter':
        return dialog.input(w.t('recording_search_input'), defaultt=w.library.query,
                            type=gui.INPUT_ALPHANUM) or None
    if chosen == 'clear':
        return ''
    return presets[int(chosen.removeprefix('preset.'))] if w.demo_allowed() else None


def rows_status(w, rows, selected):
    if w.library.mode == 'Search' and not normalize_query(w.library.query):
        status = w.t('recording_search_hint')
    elif rows:
        status = w.t('recording_rows_status', position=selected + 1, count=len(rows))
    else:
        status = w.t('recording_no_matches')
    return status + w.t('results_limited') if w.library.truncated or w.library.coverage_limited else status
