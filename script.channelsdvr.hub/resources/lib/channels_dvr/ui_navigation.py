"""Owner-only menu orchestration, extracted from the bounded WindowXML adapter."""
from .ui_menu import CONNECTION, DEVELOPER, command, group_label, main_entries, select


def main_menu(w, gui):
    entries = main_entries(w.developer_enabled(), w._playback_ui.menu_label())
    selected = select(gui.Dialog(), w.strings, 'brand', entries)
    if selected is None:
        return
    execute_command(w, gui, selected, origin=w._chrome.focus())


def execute_command(w, gui, selected, *, origin=100):
    """Closed common command handler shared by buttons and optional chooser."""
    allowed = {key for key, _ in main_entries(True)} | {
        'view.dashboard', 'view.search', 'connection.configure', 'connection.connect',
        'dashboard.refresh', 'search.clear', 'search.guide', 'window.actions'}
    if selected not in allowed:
        return
    w._playback_ui.cancel()
    if selected == 'window.exit':
        w.close()
        return
    if selected.startswith('view.'):
        w._dashboard_ui.suspend()
        w.view = {'view.dashboard': 'Dashboard', 'view.guide': 'Guide', 'view.recordings': 'Recordings',
                  'view.scheduled': 'Scheduled', 'view.search': 'Search'}[selected]
        w._guide_runtime.invalidate_view()
    elif selected == 'window.actions':
        main_menu(w, gui)
        return
    elif selected == 'dashboard.refresh':
        w._dashboard_ui.refresh()
    elif selected == 'search.clear':
        w._set_search_query('')
    elif selected == 'search.guide':
        event = w._selected_search_event()
        if event is not None and w.guide.focus_event(event):
            w.view = 'Guide'
            w._guide_runtime.invalidate_view()
    elif selected in ('connection.configure', 'connection.connect'):
        execute_connection(w, gui, selected)
    elif selected == 'connection.menu':
        connection_menu(w, gui)
    elif selected == 'addon.settings':
        settings(w)
    elif selected == 'developer.menu':
        developer_menu(w, gui)
    elif selected == 'series.manager':
        w._series_manager_request(gui)
    elif selected == 'playback.play':
        w._playback_ui.request(gui)
    elif selected in ('recording.status', 'programme.tools'):
        if (w._live_session is None or w.demo_allowed() or w._development_blocked is not None
                or w._saved_config() != w._development_config
                or w.get_setting('development_metadata_enabled') is not True):
            gui.Dialog().ok(w.t('connection'), w.t('connect_first'))
        elif selected == 'programme.tools':
            w._recording_ui.preview.request()
        else:
            w._recording_ui.request(schedule=False)
    elif w._development_blocked is not None or (w._live_session is None and not w.demo_allowed()):
        gui.Dialog().ok(w.t('connection'), w.t('connect_first'))
    elif selected == 'search.programmes':
        w._search_menu()
    elif selected == 'recording.tools':
        w._recording_tools()
    elif selected == 'guide.collection':
        groups = tuple(w.repository.groups)
        # Captured immutable keys: provider group names are data, not translated.
        answer = gui.Dialog().select(w.t('collection'), [group_label(w.strings, group) for group in groups])
        chosen = command(tuple((group, '') for group in groups), answer)
        if chosen is not None:
            w.guide.set_group(chosen)
            w._guide_runtime.invalidate_view()
            w.view = 'Guide'
    elif selected == 'guide.now':
        w._jump_time(w.utc_now() if w._live_session is not None else w.repository.now)
    elif selected == 'guide.time':
        w._time_navigation()
    elif selected in ('guide.previous', 'guide.next'):
        anchor = w._intended_anchor if w._intended_anchor is not None else w.guide.selection.anchor
        w._jump_time(anchor + (-3600 if selected == 'guide.previous' else 3600))
    elif selected in ('recording.schedule', 'recording.status', 'programme.tools'):
        if w._live_session is None or w.demo_allowed():
            gui.Dialog().ok(w.t('connection'), w.t('connect_first'))
        elif selected == 'programme.tools':
            w._recording_ui.preview.request()
        else:
            w._recording_ui.request(schedule=selected == 'recording.schedule')
    if not w._closed:
        w.render()
        w._chrome.restore(100 if selected.startswith('view.') or selected == 'search.guide' else origin)


def settings(w):
    if w._addon is None:
        return
    w._guide_runtime.latest_status = None
    w._addon.openSettings()
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
    selected = select(gui.Dialog(), w.strings, 'connection', CONNECTION)
    execute_connection(w, gui, selected)


def execute_connection(w, gui, selected):
    from .build_status_ui import status_text
    if selected == 'connection.configure':
        w._configure_development()
    elif selected == 'connection.connect':
        config = w._saved_config()
        if config is None or w._addon is None:
            gui.Dialog().ok(w.t('connection'), w.t('configure_first'))
            return
        if w._save_development(enabled=True):
            w.transient_demo_mode = False
            w._activate(config)
            w._request_live(w.utc_now())
    elif selected == 'connection.refresh':
        config = w._saved_config()
        if (w._live_session is None or config is None or w._development_config is None
                or w._development_blocked is not None):
            gui.Dialog().ok(w.t('connection'), w.t('connect_first'))
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
            body = w._current_data().banner
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
                         body + '\n\n' + status_text(w) + '\n\n' + w.t('disconnect_help'), w.strings)
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
    selected = select(gui.Dialog(), w.strings, 'developer', DEVELOPER)
    # Recheck after modal: a hidden tool is not an admission boundary.
    if not w.developer_enabled():
        w._revoke_demo()
        return
    if selected == 'demo.open':
        w._open_demo()
    elif selected == 'demo.close':
        w._close_demo()
    elif selected == 'diagnostic.index':
        gui.Dialog().textviewer(w.t('index_status'), w._guide_runtime.diagnostic())
    elif selected == 'diagnostic.timing':
        w._diagnostics()
    elif selected == 'diagnostic.connection':
        session = w._live_session
        detail = (f'blocked={w._development_blocked}; loading={session.loading}; '
                  f'error={session.last_error}; reads={session.last_request_count}; '
                  f'warnings={session.snapshot_warning_codes}' if session is not None else 'No live session')
        gui.Dialog().textviewer(w.t('connection_diagnostics'), detail)
    elif selected is not None:
        if not w.demo_allowed():
            gui.Dialog().ok(w.t('developer'), w.t('demo_first'))
            return
        if selected == 'demo.stress':
            w._toggle_stress_fixture()
        elif selected == 'demo.status':
            scenarios = w.data_session.scenario_entries()
            index = gui.Dialog().select(w.t('demo_status'), [label for _, label in scenarios])
            scenario = command(scenarios, index)
            if scenario is not None and w.demo_allowed():
                w.data_session.choose(scenario)
        elif selected == 'demo.exercise':
            from .guide_fixture_data import FixtureMode
            from .guide_fixtures import large_menu, traverse_large
            entries = (('steps60', 'steps_60'), ('steps600', 'steps_600'), ('large', 'demo_500'),
                       ('traverse', 'demo_traverse'), ('dense', 'demo_dense'))
            action = select(gui.Dialog(), w.strings, 'demo_exercise', entries)
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
