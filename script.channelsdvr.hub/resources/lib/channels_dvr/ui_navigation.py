"""Owner-only menu orchestration, extracted from the bounded WindowXML adapter."""
from types import MappingProxyType

from .dvr_ui import DVR_LABELS, DVR_MORE
from .guide_screen import choose_group, choose_time
from .ui_menu import CONNECTION, DEVELOPER, command, select
from .ui_modal import ModalGui, dialog_for, modal_call
from . import ui_recordings, ui_search
from .ui_routes import VIEW_KEYS, back, enter, route_fence

DESTINATIONS = MappingProxyType({'view.' + view.lower(): view for view in VIEW_KEYS})
LABELS = MappingProxyType({
    **{code: VIEW_KEYS[view] for code, view in DESTINATIONS.items()},
    'selection.activate': 'details', 'selection.open': 'details', 'selection.info': 'details',
    'selection.page-up': 'previous_page', 'selection.page-down': 'next_page',
    **ui_search.LABELS,
    **ui_recordings.LABELS, 'guide.collection': 'collection',
    'guide.controls': 'guide_controls',
    'guide.now': 'now', 'guide.time': 'date_time', 'guide.previous': 'previous_hour', 'guide.next': 'next_hour',
    'playback.play': 'play_short', 'recording.schedule': 'schedule', 'recording.status': 'recording_status',
    'programme.tools': 'programme_tools', 'series.manager': 'series_manager',
    'connection.menu': 'connection', 'addon.settings': 'connection_settings', **dict(CONNECTION),
    'dashboard.refresh': 'dashboard_refresh', 'developer.menu': 'developer',
    'window.actions': 'more', 'window.back': 'back', 'window.exit': 'exit',
    **DVR_LABELS,
})
CONNECTION_ACTIONS = frozenset(code for code, _ in CONNECTION)
MORE_COMMANDS = {
    'Dashboard': ('connection.status', 'developer.menu'),
    'Guide': ('guide.controls', 'search.programmes', 'view.search', 'view.settings',
              'guide.previous', 'guide.next', 'playback.play', 'recording.schedule',
              'recording.status', 'programme.tools', 'series.manager', 'developer.menu'),
    'Recordings': ('search.context',),
    'DVR': ('search.context', *DVR_MORE),
    'Search': (),
    'Settings': ('connection.menu', 'developer.menu'),
}

SELECTION_COMMANDS = frozenset(('selection.activate', 'selection.open', 'selection.info', 'window.actions',
                                'playback.play', 'recording.schedule', 'recording.status', 'programme.tools',
                                'search.reveal', 'series.manager', 'dvr.manage', 'dvr.advanced', 'dvr.status'))


def more_entries(w):
    direct = {intent.command for intent in w._chrome.presented.values()
              if intent.context[0] is w._chrome.layout}
    disconnected = w._live_session is None or w._development_blocked is not None
    for code in MORE_COMMANDS[w.view]:
        if code in direct or (code == 'view.settings' and not disconnected):
            continue
        if code == 'view.search' and command_label(w, 'search.programmes') is not None:
            continue
        if code == 'connection.refresh' and disconnected:
            continue
        if (label := command_label(w, code)) is not None:
            yield code, label


def command_label(w, code):
    """One ordered, closed registry for visible and executable command intent."""
    label = LABELS.get(code)
    if label is None or w._closed:
        return None
    view = w.view
    if code in ('dvr.scheduled', 'dvr.passes'):
        return label if view == 'DVR' and not w._dvr.child else None
    if code in ('dvr.manage', 'dvr.advanced', 'dvr.refresh', 'dvr.status'):
        if view != 'DVR' or not w._dvr.passes or w._dvr.child:
            return None
        if code == 'dvr.advanced':
            return label if w._dvr.selected() is not None else None
        if code == 'dvr.manage':
            return label if w._dvr.manageable() is not None else None
        return label if w._dvr.config() is not None else None
    if code == 'connection.refresh' and view == 'DVR' and (w._dvr.passes or w._dvr.child):
        return None
    if code == 'series.manager' and view != 'Guide':
        return None
    if code == 'window.actions':
        return label if next(more_entries(w), None) is not None else None
    if code == 'developer.menu':
        return label if view in ('Dashboard', 'Guide', 'Settings') and w.developer_enabled() else None
    if code == 'connection.menu' and view != 'Settings':
        return None
    if code == 'series.manager':
        return label if w._guide_runtime._mode is None and w._recording_ui._current_config() is not None else None
    if code in ('recording.status', 'programme.tools'):
        session = w._live_session
        if (view != 'Guide' or session is None or w._recording_ui._current_config() is None):
            return None
        if code == 'recording.status' and (session.loading or not w._current_data().can_browse
                                           or w.selected_indexed_event() is None):
            return None
    if code in ui_search.LABELS:
        if code == 'search.programmes':
            return label if view == 'Guide' and w._route.parent is None else None
        if code == 'search.context':
            return label if view in ('Recordings', 'DVR') and w._route.parent is None else None
        if view != 'Search':
            return None
        return w._search_ui.reveal_label() if code == 'search.reveal' else label
    if code in ui_recordings.LABELS:
        return label if ui_recordings.available(w, code) else None
    if code == 'dashboard.refresh':
        return label if view == 'Dashboard' else None
    if code == 'connection.configure' and view == 'Dashboard':
        return 'dashboard_configure'
    if code.startswith('guide.'):
        if view != 'Guide' or w._development_blocked is not None or (w._live_session is None and not w.demo_allowed()):
            return None
        if code in ('guide.time', 'guide.collection', 'guide.controls') and not w._guide_runtime.ready:
            return None
        if code == 'guide.now' and w._route.parent == 'Search':
            if not w._guide_runtime.ready:
                return None
            start, end = w.guide.repository.horizon
            now = w.utc_now() if w._live_session is not None else w.repository.now
            if not start <= now < end:
                return None
    if code in ('selection.page-up', 'selection.page-down'):
        return label if view != 'Dashboard' else None
    if code == 'selection.activate':
        return command_label(w, 'selection.open')
    if code in ('selection.open', 'selection.info', 'playback.play', 'recording.schedule'):
        if view == 'Dashboard':
            return None
        if view == 'Settings':
            return 'open' if code == 'selection.open' else None
        if view == 'Search' and code != 'playback.play':
            if code not in ('selection.open', 'selection.info') or not w._search_ui.actionable():
                return None
            return w._search_ui.open_label() if code == 'selection.open' else label
        if code == 'playback.play':
            if view not in ('Guide', 'Search', 'Recordings') or w._playback_ui.selection(rebind=False) is None:
                return None
            return 'watch_short' if view in ('Guide', 'Search') else label
        if code == 'recording.schedule':
            if view != 'Guide' or w._live_session is None or w.demo_allowed():
                return None
            data, event = w._current_data(), w.selected_indexed_event()
            return 'schedule_short' if (data.can_browse and not data.loading and event is not None
                                        and event.start > w.utc_now()) else None
        data = w._current_data()
        if view == 'Recordings' and data.can_browse and w.library.selected is not None:
            return 'open' if code == 'selection.open' and w.library.selected.children else label
        if view == 'Guide' and (data.can_browse or w._guide_runtime.ready) and w.guide.channel(w.guide.selection.channel):
            return label
        if (view == 'DVR' and data.can_browse and (not w._dvr.child or not (data.stale or data.loading))
                and w._rows()):
            return label
        return None
    return label


def main_menu(w, gui, origin):
    captured = tuple((code, label, w._chrome.command_activation(code, label)) for code, label in more_entries(w))
    if not captured:
        return
    selected = select(dialog_for(w, gui), w.strings, 'more', ((code, label) for code, label, _ in captured))
    if selected is not None:
        intent = next(intent for code, _, intent in captured if code == selected)
        if selected in dict(more_entries(w)) and w._chrome.command_current(intent):
            execute_command(w, gui, selected, origin=origin)


def execute_command(w, gui, selected, *, origin=100):
    """Owner rechecks eligibility even after a matched immutable activation."""
    w._dispatcher.assert_owner()
    if selected == 'view.scheduled':
        selected = 'view.dvr'
    if command_label(w, selected) is None:
        return False
    if selected in DESTINATIONS:
        enter(w, DESTINATIONS[selected])
        return True
    if selected in ('dvr.scheduled', 'dvr.passes'):
        w._dvr.switch(selected, origin)
        return True
    if selected == 'window.back':
        back(w)
        return True
    if selected == 'window.exit':
        w.close()
        return True
    if selected in ui_recordings.LABELS:
        ui_recordings.execute(w, gui, selected, origin)
        return True
    if selected in ui_search.LABELS:
        w._search_ui.execute(gui, selected, origin)
        return True
    if (w.view == 'Recordings' and selected in ('selection.open', 'selection.activate')
            and ui_recordings.open_folder(w)):
        return True
    w._playback_ui.cancel()
    if selected == 'window.actions':
        main_menu(w, gui, origin)
        return True
    if selected == 'guide.controls':
        w._chrome.initialize_focus(306)
        return True
    if selected in ('selection.page-up', 'selection.page-down'):
        w._navigate(3 if selected == 'selection.page-up' else 4, page=True)
        return True
    layout = w._chrome.layout
    route = w._route
    if selected == 'dashboard.refresh':
        w._dashboard_ui.refresh()
    else:
        _execute_workflow(w, gui, selected, origin)
    if not w._closed and layout is w._chrome.layout:
        w.render()
        w._chrome.restore(origin)
    elif not w._closed and route is w._route:
        # Child paths and collection changes can invalidate without painting.
        w.render()
        w._chrome.initialize_focus()
    return True


def _execute_workflow(w, gui, selected, origin):
    if selected in ('selection.activate', 'selection.open', 'selection.info'):
        w._details(allow_search_action=selected != 'selection.info', origin=origin)
    elif selected in CONNECTION_ACTIONS:
        execute_connection(w, gui, selected)
    elif selected == 'connection.menu':
        connection_menu(w, gui)
    elif selected == 'addon.settings':
        settings(w)
    elif selected == 'developer.menu':
        developer_menu(w, gui)
    elif selected == 'series.manager':
        w._series_manager_request(gui)
    elif selected == 'dvr.refresh':
        w._dvr.request_refresh()
    elif selected == 'dvr.advanced':
        w._dvr.show(dialog_for(w, gui), advanced=True)
    elif selected in ('dvr.manage', 'dvr.status'):
        w._dvr.request(w._series_manager(gui), status=selected == 'dvr.status')
    elif selected == 'playback.play':
        w._playback_ui.request(ModalGui(w, gui))
    elif selected in ('recording.status', 'programme.tools'):
        if (w._live_session is None or w.demo_allowed() or w._development_blocked is not None
                or w._saved_config() != w._development_config
                or w.get_setting('development_metadata_enabled') is not True):
            dialog_for(w, gui).ok(w.t('connection'), w.t('connect_first'))
        elif selected == 'programme.tools':
            w._recording_ui.preview.request()
        else:
            w._recording_ui.request(schedule=False)
    elif w._development_blocked is not None or (w._live_session is None and not w.demo_allowed()):
        dialog_for(w, gui).ok(w.t('connection'), w.t('connect_first'))
    elif selected == 'guide.collection':
        choose_group(w, gui)
    elif selected == 'guide.now':
        w._jump_time(w.utc_now() if w._live_session is not None else w.repository.now)
    elif selected == 'guide.time':
        choose_time(w, gui)
    elif selected in ('guide.previous', 'guide.next'):
        anchor = w._intended_anchor if w._intended_anchor is not None else w.guide.selection.anchor
        w._jump_time(anchor + (-3600 if selected == 'guide.previous' else 3600))
    elif selected == 'recording.schedule':
        w._recording_ui.request(schedule=True)


def settings(w):
    if w._addon is None:
        return
    series = getattr(w, '_series_ui', None)
    if series is not None:
        series.close()  # Native Settings can revoke the source before it returns.
    w._guide_runtime.latest_status = None
    modal_call(w, w._addon.openSettings)
    w._revoke_demo()
    if w._live_session is not None:
        config = w._saved_config()
        if w.get_setting('development_metadata_enabled') is not True:
            w._suspend_development(disabled=True)
        elif config is None:
            w._suspend_development()
        elif w._development_blocked is None:
            w._activate(config)


def connection_menu(w, gui):
    selected = select(dialog_for(w, gui), w.strings, 'connection', CONNECTION)
    execute_connection(w, gui, selected)


def execute_connection(w, gui, selected):
    from .build_status_ui import status_text
    if selected == 'connection.configure':
        w._configure_development()
    elif selected == 'connection.connect':
        config = w._saved_config()
        if config is None or w._addon is None:
            dialog_for(w, gui).ok(w.t('connection'), w.t('configure_first'))
            return
        if w._save_development(enabled=True):
            w.transient_demo_mode = False
            w._activate(config)
            w._request_live(w.utc_now())
    elif selected == 'connection.refresh':
        config = w._saved_config()
        if (w._live_session is None or config is None or w._development_config is None
                or w._development_blocked is not None):
            dialog_for(w, gui).ok(w.t('connection'), w.t('connect_first'))
            return
        anchor = w._intended_anchor if w._intended_anchor is not None else w.guide.selection.anchor
        same_scope = config == w._development_config
        w._activate(config)
        if same_scope:
            w._refresh_live()
        else:
            w._request_live(anchor)
    elif selected == 'connection.status':
        session = w._live_session
        body = w.t('not_connected')
        if session is not None:
            body = session.guide().banner if w.view in ('Settings', 'Dashboard') else w._current_data().banner
            if session.guide().updated_at is not None:
                body += '\n' + w.t('connection_counts', channels=len(w.repository.channels) if w.repository.channels else w.t('unknown'),
                    events=len(w.repository.events),
                    recordings=len(w.repository.recordings) if session.recordings().updated_at is not None else w.t('unknown'),
                    jobs=len(w.repository.jobs) if session.jobs().updated_at is not None else w.t('unknown'))
            warnings = session.warning_details()
            if warnings:
                body += '\n\n' + warnings
        elif w.demo_allowed():
            body = w.t('demo_footer')
        artwork = getattr(w, '_artwork_ui', None)
        if artwork is not None:
            body += '\n\n' + artwork.diagnostic_text()
        body += '\n\n' + w._playback_ui.diagnostic_text()
        from .pass_ui import _programme_scope
        _programme_scope(w.path, w.t('connection_status'),
                          body + '\n\n' + status_text(w) + '\n\n' + w.t('disconnect_help'), w.strings,
                          modal_owner=w)
    elif selected == 'connection.disconnect':
        # Explicit connection action only. Demo never alters this saved preference.
        if w._addon is not None and not w._save_development(enabled=False):
            return
        w._close_demo()
    elif selected == 'connection.settings':
        settings(w)
    elif selected == 'connection.storage':
        w._storage_ui.request()  # No settings/configuration lookup or DVR connection required.


def developer_menu(w, gui):
    if not w.developer_enabled():
        return
    selected = select(dialog_for(w, gui), w.strings, 'developer', DEVELOPER)
    # Recheck after modal: a hidden tool is not an admission boundary.
    if not w.developer_enabled():
        w._revoke_demo()
        return
    if selected == 'demo.open':
        w._open_demo()
    elif selected == 'demo.close':
        w._close_demo()
    elif selected == 'diagnostic.index':
        dialog_for(w, gui).textviewer(w.t('index_status'), w._guide_runtime.diagnostic())
    elif selected == 'diagnostic.timing':
        w._diagnostics()
    elif selected == 'diagnostic.connection':
        session = w._live_session
        detail = (f'blocked={w._development_blocked}; loading={session.loading}; '
                  f'error={session.last_error}; reads={session.last_request_count}; '
                  f'warnings={session.snapshot_warning_codes}' if session is not None else 'No live session')
        dialog_for(w, gui).textviewer(w.t('connection_diagnostics'), detail)
    elif selected is not None:
        if not w.demo_allowed():
            dialog_for(w, gui).ok(w.t('developer'), w.t('demo_first'))
            return
        if selected == 'demo.stress':
            w._toggle_stress_fixture()
        elif selected == 'demo.status':
            scenarios = w.data_session.scenario_entries()
            index = dialog_for(w, gui).select(w.t('demo_status'), [label for _, label in scenarios])
            scenario = command(scenarios, index)
            if scenario is not None and w.demo_allowed():
                w.data_session.choose(scenario)
        elif selected == 'demo.exercise':
            from .guide_fixture_data import FixtureMode
            from .guide_fixtures import large_menu, traverse_large
            entries = (('steps60', 'steps_60'), ('steps600', 'steps_600'), ('large', 'demo_500'),
                       ('traverse', 'demo_traverse'), ('dense', 'demo_dense'))
            action = select(dialog_for(w, gui), w.strings, 'demo_exercise', entries)
            if not w.demo_allowed():
                return
            if action in ('steps60', 'steps600'):
                w._exercise(60 if action == 'steps60' else 600)
            elif action == 'large':
                large_menu(w)
            elif action == 'traverse':
                traverse_large(w)
            elif action == 'dense':
                w._guide_runtime.fixture(FixtureMode.DENSE500_48H)
