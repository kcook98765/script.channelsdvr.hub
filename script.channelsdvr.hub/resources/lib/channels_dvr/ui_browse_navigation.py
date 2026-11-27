"""Bounded owner selectors; captured IDs/values never derive from translated labels."""
from .search import normalize_query
from .ui_menu import SEARCH, command
from .ui_modal import dialog_for
from .ui_routes import enter


def admitted(w, gui):
    if w._closed or w._development_blocked is not None or (w._live_session is None and not w.demo_allowed()):
        dialog_for(w, gui).ok(w.t('connection'), w.t('connect_first'))
        return False
    return True


def search_menu(w, gui):
    if not admitted(w, gui):
        return
    presets = ('special', 'programme 4', 'unavailable', 'no-match-xyz')
    entries = tuple((code, w.t(key)) for code, key in SEARCH)
    if w.demo_allowed():
        entries += tuple((f'preset.{i}', w.t('demo_preset', query=query)) for i, query in enumerate(presets))
    captured = tuple(entries)
    title = w.t('search_programmes')
    chosen = command(captured, dialog_for(w, gui).select(title, [label for _, label in captured]))
    if chosen is None or not admitted(w, gui):
        return
    value = None
    if chosen == 'search.enter':
        candidate = dialog_for(w, gui).input(w.t('search_input'),
            defaultt=w.search_query, type=gui.INPUT_ALPHANUM)
        if candidate:
            value = candidate
    elif chosen == 'search.clear':
        value = ''
    elif chosen == 'search.previous':
        if w.view != 'Search':
            enter(w, 'Search')
    elif chosen is not None and chosen.startswith('preset.') and w.demo_allowed():
        value = presets[int(chosen.removeprefix('preset.'))]
    if value is not None and admitted(w, gui):
        try:
            normalize_query(value)
            w._set_search_query(value)
            if w.view != 'Search':
                enter(w, 'Search')
        except ValueError:
            dialog_for(w, gui).ok(title, w.t('search_limit'))
