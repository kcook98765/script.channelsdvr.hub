"""Hub route/pointer regressions with inert controls, and pure bookmark policy tests."""
from dataclasses import replace
from functools import partial
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from channels_dvr.dvr_ui import TABS as DVR_TABS
from channels_dvr.guide import GuideController
from channels_dvr.guide_fixtures import stress_repository
from channels_dvr.guide_index import IndexInput, build_index
from channels_dvr.guide_render import GRID_LEFT, ROW_TOP
from channels_dvr.guide_screen import DESCRIPTION, METADATA, NOW, TITLE, day_choices, paint_clock, timeline_x
from channels_dvr.hub_bookmarks import (
    BookmarkScope, HubBookmarks, MAX_BOOKMARK_BYTES, MAX_RECENT_TERMS,
    StartupAdmission, decode_bookmarks, encode_bookmarks, startup_destination,
)
from channels_dvr.library import LibraryBrowser
from channels_dvr.live_loader import DevelopmentConfig, IndexResult, LoadResult
from channels_dvr.live_session import LiveDataSession
from channels_dvr.metadata_models import MetadataBatch
from channels_dvr.metadata_repository import DevelopmentSnapshot
from channels_dvr.models import ChannelKey, Event, Recording
from channels_dvr.normalization import DataState
from channels_dvr.repository import MockRepository
from channels_dvr.search import airing_key
from channels_dvr.series_manager_models import GeneralSeriesPlan, ManagerView, rule_rows
from channels_dvr.session import OfflineDataSession
from channels_dvr.ui_chrome import (ACTIONS, CONTENT, CONTEXT, DOWN, INFO, LEFT, NAV, NAV_BACK,
                                   PAGE_DOWN, RIGHT, UP, WHEEL_DOWN, WHEEL_UP)
from channels_dvr.ui_navigation import command_label, execute_command
from channels_dvr.ui_recordings import QUERY, SORT, SOURCE, TABS as RECORDINGS_TABS
from channels_dvr.ui_routes import PRIMARY, Route, launch_view

ADDON = Path(__file__).resolve().parents[1] / 'script.channelsdvr.hub'
ADAPTER = ADDON / 'resources/lib/channels_dvr/kodi_ui.py'


class Control:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.ident = None
        self.properties = {}

    def getId(self):
        return self.ident

    def __getattr__(self, name):
        if name.startswith('set'):
            return lambda *args: self.properties.update({name: args})
        raise AttributeError(name)


class WindowXML:
    def __init__(self, *args, **kwargs):
        ids = (100, 110, *range(112, 118), *range(120, 132), *range(140, 144),
               *range(150, 156), *NAV, *RECORDINGS_TABS, *DVR_TABS, *ACTIONS, *range(400, 406))
        self.controls = {ident: Control() for ident in ids}
        self.serial = 3000
        self.focus_id = 100
        self.focus_writes = []
        self.native_close = 0

    def getControl(self, ident):
        return self.controls[ident]

    def getFocusId(self):
        return self.focus_id

    def setFocusId(self, ident):
        self.focus_writes.append(ident)
        self.focus_id = ident

    def addControl(self, control):
        self.serial += 1
        control.ident = self.serial
        self.controls[self.serial] = control

    def addControls(self, controls):
        for control in controls:
            self.addControl(control)

    def removeControl(self, control):
        del self.controls[control.ident]

    def close(self):
        self.native_close += 1


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.front = 10000
        self.dialog = SimpleNamespace(select=Mock(return_value=-1), textviewer=Mock(), ok=Mock(), input=Mock(return_value=''))
        self.gui = SimpleNamespace(WindowXML=WindowXML, ControlLabel=Control, ControlImage=Control, ControlButton=Control,
                                   getCurrentWindowId=lambda: self.front, Dialog=lambda: self.dialog, INPUT_ALPHANUM=0)
        fakes = patch.dict(sys.modules, {'xbmc': SimpleNamespace(), 'xbmcgui': self.gui, 'xbmcvfs': SimpleNamespace()})
        fakes.start()
        self.addCleanup(fakes.stop)
        # Isolate the adapter's SDK base class from the separate Dashboard fakes.
        spec = spec_from_file_location('channels_dvr._phase1_fake_adapter', ADAPTER)
        self.adapter = module_from_spec(spec)
        spec.loader.exec_module(self.adapter)
        self.w = self.window()

    def window(self, start='Dashboard', *, configure=None):
        w = self.adapter.ChannelsWindow(start_view=start)
        w._addon = SimpleNamespace(getSettingBool=lambda key: key == 'ShowDeveloperTools', getSettingString=lambda _: '',
                                   setSettingBool=Mock(), setSettingString=Mock(), openSettings=Mock())
        w._guide_runtime.poll = Mock(return_value=False)
        w._guide_runtime.close = Mock()
        w._artwork_ui = SimpleNamespace(poll=lambda: False, update=lambda: None, draw=lambda gui: None,
                                       ready=lambda: False, reset=lambda: None, close=lambda: None, detached=lambda: None)
        w._metadata_loader.request = self.forbidden('metadata')
        w._playback_ui.request = self.forbidden('play')
        w._recording_ui.request = self.forbidden('recording')
        w._save_development = self.forbidden('settings write')
        if configure is not None:
            configure(w)
        w.onInit()
        w.process_pending()
        self.addCleanup(w.shutdown)
        return w

    @staticmethod
    def forbidden(label):
        return Mock(side_effect=AssertionError('unexpected ' + label))

    def seed(self):
        w = self.w
        w.transient_demo_mode = True
        w.repository = repository = MockRepository()
        w.library = LibraryBrowser(repository.recordings, strings=w.strings)
        w.data_session = OfflineDataSession(repository, strings=w.strings)
        w.guide = GuideController(repository, strings=w.strings)
        runtime_class = type(w._guide_runtime)
        ready = patch.object(runtime_class, 'ready', property(lambda runtime: runtime.window.demo_allowed()))
        ready.start()
        self.addCleanup(ready.stop)
        # This cohort exercises the prepared presentation boundary. The real
        # index/transport publication tests live in the guide/loader cohorts.
        w._guide_runtime.selected = lambda event=None: event or w.guide.selected_event()
        w.render()
        return repository

    def post_action(self, code):
        self.w.onAction(SimpleNamespace(getId=lambda: code))

    def action(self, code):
        self.post_action(code)
        self.w.process_pending()

    def click(self, ident):
        self.w.focus_id = ident  # Kodi focuses the hit before dispatching CLICKED.
        self.w.onClick(ident)
        self.w.process_pending()

    def enter(self, view):
        self.assertTrue(execute_command(self.w, self.gui, 'view.' + view.lower()))

    def hits(self, kind):
        return {intent.target[1:]: ident for ident, intent in self.w._chrome.presented.items()
                if intent.target and intent.target[0] == kind}

    def test_primary_navigation_and_back_from_all_regions(self):
        w = self.w
        self.assertEqual([w._chrome.capture(i).command for i in NAV],
                         ['view.' + view.lower() for view in PRIMARY])
        self.assertEqual(w.getFocusId(), 200)
        self.assertEqual(w.controls[310].properties['setWidth'], (280,))
        self.action(UP)
        self.assertEqual(w.getFocusId(), 200)
        for view in PRIMARY[1:]:
            for focus in (CONTENT, 200, 304):
                with self.subTest(view=view, focus=focus):
                    self.enter(view)
                    self.assertEqual(w.getFocusId(), {'Guide': 304, 'Recordings': 202,
                                                       'DVR': 230, 'Search': 204, 'Settings': CONTENT}[view])
                    w.focus_id = focus
                    self.action(NAV_BACK)
                    self.assertEqual(w.view, 'Dashboard')
                    self.assertEqual(w.getFocusId(), 200)
        self.action(DOWN)
        self.assertEqual(w.getFocusId(), 304)
        self.action(NAV_BACK)
        self.assertTrue(w._closed)
        self.assertEqual(w.native_close, 1)

    def test_kodi_launch_names_and_scheduled_alias(self):
        for view in PRIMARY:
            self.assertEqual(launch_view(view), view)
        for legacy, target in (('Scheduled', 'DVR'), ('unknown', 'Dashboard'), ('', 'Dashboard')):
            self.assertEqual(launch_view(legacy), target)
        launch = Mock()
        addon = SimpleNamespace(getAddonInfo=lambda _: str(ADDON))
        fake_entry = SimpleNamespace(open_window=launch)
        with patch.dict(sys.modules, {'xbmcaddon': SimpleNamespace(Addon=lambda: addon),
                                     'channels_dvr.kodi_ui': fake_entry}), patch.object(sys, 'path', sys.path.copy()):
            for arguments, view in (([], None), (['Dashboard'], 'Dashboard'), (['unknown'], 'Dashboard'),
                                    (['Scheduled'], 'DVR'), (['Guide'], 'Guide'),
                                    (['Recordings'], 'Recordings'), (['Search'], 'Search'), (['Guide', 'bad'], 'Dashboard')):
                with patch.object(sys, 'argv', ['default.py', *arguments]):
                    runpy.run_path(str(ADDON / 'default.py'), run_name='__main__')
                launch.assert_called_with(str(ADDON), addon, start_view=view)

    def pending_hub_startup(self, preference, *, explicit=None, saved=None, enabled=True):
        config = DevelopmentConfig.from_address('192.0.2.10')
        now = 3600
        key = ChannelKey(config.namespace, 'device', '1')
        record = Recording('record', 'Episode', 'Show', 1, 1, False, 1000, 1800,
                           namespace=config.namespace, provenance='dvr', show_id='show')
        prefs = {'server_origin': config.origin, 'hub_startup_view': preference}
        worker = Mock()

        def configure(w):
            w._addon.getSettingString = lambda name: prefs.get(name, '')
            w._addon.getSettingBool = lambda name: name == 'development_metadata_enabled' and enabled
            w._hub_session._worker = worker
            w._recording_ui._programme_profile = lambda: str(ADDON)
            w.utc_now = lambda: now + 60
            w._dvr.poll = Mock(return_value=False)
            w._dashboard_ui.poll = Mock(return_value=False)
            w._dashboard_ui.worker.request = self.forbidden('Dashboard transport in startup fake')
            w._set_search_query = self.forbidden('startup search')

            def activate(value):
                w._hub_session.source_changed()
                w._development_config = value
                w._live_session = LiveDataSession(value.namespace, value.expected_version, clock=lambda: now + 60)
                w.repository = w._live_session.repository
                w._live_session.begin(1, now, guide_windows=1)

            w._activate = activate
            w._request_live = Mock()  # the accepted result below replaces transport

        w = self.window(start=explicit, configure=configure)
        worker.take.side_effect = lambda: (w._hub_session._store, saved)
        patch_ready = patch.object(type(w._guide_runtime), 'ready', property(lambda runtime: runtime.window is w))
        patch_ready.start()
        self.addCleanup(patch_ready.stop)

        def publish(*, failure=False, prime_library=True, additional_recordings=()):
            from channels_dvr.models import Channel
            session = w._live_session
            snapshot = None if failure else DevelopmentSnapshot(config.namespace, config.expected_version, now,
                (now, now + 1800), MetadataBatch((Channel(key, '1', 'Station', ('Favorites',)),), DataState.READY),
                MetadataBatch((Event('event', key, now, now + 600, 'Programme'),), DataState.READY),
                MetadataBatch((*additional_recordings, record), DataState.READY), MetadataBatch((), DataState.EMPTY))
            self.assertTrue(session.apply(LoadResult(session._generation, now, 1, snapshot, None)))
            w.guide = GuideController(w.repository, strings=w.strings)
            if prime_library:
                w.library.update(w.repository.recordings)

        return w, worker, prefs, config, publish

    def test_configured_startup_choices_wait_for_admission_and_do_not_save(self):
        for pref, destination in (('Dashboard', 'Dashboard'), ('Guide', 'Guide'), ('Recordings', 'Recordings'),
                                  ('DVR', 'DVR'), ('Search', 'Dashboard'), ('bad', 'Dashboard')):
            with self.subTest(pref=pref):
                w, worker, _, _, publish = self.pending_hub_startup(pref)
                self.assertEqual(w.view, 'Dashboard')
                worker.bind.assert_not_called()
                publish()
                w.process_pending()
                self.assertEqual(w.view, destination)
                worker.submit.assert_not_called()
                w._playback_ui.request.assert_not_called()
                w._recording_ui.request.assert_not_called()
                w._request_live.assert_called_once()

    def test_explicit_launch_manual_route_and_failed_initial_admission_supersede_startup(self):
        w, worker, _, _, publish = self.pending_hub_startup('Guide', explicit='Dashboard')
        publish()
        w.process_pending()
        self.assertEqual(w.view, 'Dashboard')
        worker.submit.assert_not_called()
        w, _, _, _, publish = self.pending_hub_startup('Guide')
        publish()
        w._guide_runtime.poll.side_effect = lambda: (w.onClick(205), False)[1]
        w.process_pending()  # user route was posted while the accepted result was polled
        self.assertEqual(w.view, 'Dashboard')
        w._guide_runtime.poll.side_effect = None
        w.process_pending()
        self.assertEqual(w.view, 'Settings')
        w, _, _, _, publish = self.pending_hub_startup('Guide')
        publish(failure=True)
        w.process_pending()
        self.assertEqual(w.view, 'Dashboard')
        self.assertEqual(w.controls[112].properties['setLabel'], (w.t('refresh_hint'),))
        w._live_session.begin(2, 3600, guide_windows=1)
        publish()
        w.process_pending()
        self.assertEqual(w.view, 'Dashboard')  # a later successful refresh is not startup
        w, worker, _, _, _ = self.pending_hub_startup('Guide', enabled=False)
        self.assertEqual(w.view, 'Dashboard')
        w._request_live.assert_not_called()
        worker.bind.assert_not_called()

    def test_last_startup_restores_only_current_source_and_safe_navigation_state(self):
        scope = BookmarkScope.from_config(DevelopmentConfig.from_address('192.0.2.10'))
        other = BookmarkScope.from_config(DevelopmentConfig.from_address('192.0.2.11'))
        model = HubBookmarks(scope, guide_group='Favorites', guide_channel=ChannelKey(scope.namespace, 'device', '1'),
                             recordings_category='Shows', recordings_filter='dvr', recordings_sort='oldest',
                             recording_selection=(scope.namespace, 'dvr', 'record'), dvr_tab='passes', last_view='Guide')
        w, worker, prefs, _, publish = self.pending_hub_startup('Last', saved=model)
        publish()
        w.process_pending()
        self.assertEqual((w.view, w.guide.group, w.guide.selection.anchor), ('Guide', 'Favorites', w.utc_now()))
        self.assertEqual(w.guide.selection.channel, model.guide_channel)
        self.assertEqual((w.library.depth, w.library.sort_order, w.library.provenance_filter), (2, 'oldest', 'dvr'))
        self.assertEqual(w.library.selected_recording().id, 'record')
        self.assertEqual(w._dvr.tab, 'dvr.passes')
        worker.submit.assert_not_called()
        w.onAction(SimpleNamespace(getId=lambda: NAV_BACK))
        w.process_pending()
        self.assertEqual(worker.submit.call_args.args[1].last_view, 'Dashboard')
        prefs['server_origin'] = '192.0.2.11'
        w.process_pending()
        worker.bind.assert_called_with(None)
        self.assertIsNone(w._hub_session._binding)
        w, worker, _, _, publish = self.pending_hub_startup('Last', saved=HubBookmarks(other, last_view='DVR'))
        publish()
        w.process_pending()
        self.assertEqual(w.view, 'Dashboard')
        worker.submit.assert_not_called()

    def test_cold_recordings_restore_resolves_exact_leaf_without_visiting_recordings(self):
        scope = BookmarkScope.from_config(DevelopmentConfig.from_address('192.0.2.10'))
        identity = (scope.namespace, 'dvr', 'record')
        bookmark = HubBookmarks(scope, recording_selection=identity)
        decoy = Recording('record', 'Decoy', 'A decoy show', 1, 1, False, 999, 1800,
                          namespace=scope.namespace, provenance='import', show_id='decoy')
        for explicit, view in ((None, 'Dashboard'), ('Guide', 'Guide')):
            with self.subTest(initial_view=view):
                w, worker, _, _, publish = self.pending_hub_startup('Dashboard', explicit=explicit, saved=bookmark)
                self.assertEqual(w.view, view)
                publish(prime_library=False, additional_recordings=(decoy,))
                self.assertEqual(w._live_session.recordings().state, DataState.READY)
                self.assertEqual(w.library.rows(), ())
                w.process_pending()
                self.assertEqual(w.view, view)
                self.assertEqual(w.library.depth, 2)
                selected = w.library.selected_recording()
                self.assertIsNotNone(selected)
                self.assertEqual((selected.namespace, selected.provenance, selected.id), identity)
                worker.submit.assert_not_called()

    def test_guide_ok_defaults_to_details_and_opt_in_rechecks_exact_on_now_airing(self):
        w, _, prefs, _, publish = self.pending_hub_startup('Guide')
        publish()
        w.process_pending()
        self.assertEqual(w.view, 'Guide')
        w._guide_runtime.selected = lambda event=None: event or w.guide.selected_event()
        event = w.selected_indexed_event()
        assert event is not None and event.end is not None
        w.utc_now = lambda: event.start
        w._show_event_details = Mock()
        w._playback_ui.selection = Mock(return_value=object())
        self.assertTrue(execute_command(w, self.gui, 'selection.activate'))
        w._show_event_details.assert_called_once_with(event)
        w._playback_ui.request.assert_not_called()
        prefs['hub_guide_ok'] = 'live'
        w._playback_ui.request = Mock()
        w.render()
        w._playback_ui.request.assert_not_called()
        self.assertTrue(execute_command(w, self.gui, 'selection.info'))
        self.assertEqual(w._show_event_details.call_count, 2)
        w._playback_ui.request.assert_not_called()
        self.assertTrue(execute_command(w, self.gui, 'selection.activate'))
        eligible = w._playback_ui.request.call_args.kwargs['eligible']
        self.assertTrue(eligible())
        w.utc_now = lambda: event.end
        self.assertFalse(eligible())
        w._playback_ui.request.reset_mock()
        self.assertTrue(execute_command(w, self.gui, 'selection.activate'))
        w._playback_ui.request.assert_not_called()
        self.assertEqual(w._show_event_details.call_count, 3)

    def test_guide_hides_bar_and_back_is_not_a_focus_hop(self):
        self.seed()
        self.click(201)
        w = self.w
        self.assertEqual(w.view, 'Guide')
        self.assertFalse(set(NAV) & w._chrome.presented.keys())
        self.assertTrue(all(w.controls[i].properties['setEnabled'] == (False,) for i in NAV))
        self.assertTrue(self.hits('cell'))
        self.assertEqual(w.getFocusId(), CONTENT)
        self.action(NAV_BACK)
        self.assertEqual(w.view, 'Dashboard')
        self.assertEqual(w.getFocusId(), 200)

    def test_guide_group_day_now_controls_and_primary_bookmark(self):
        repo = self.seed()
        self.enter('Guide')
        w = self.w
        self.assertEqual([w._chrome.capture(i).command for i in (306, 307, 308)],
                         ['guide.collection', 'guide.time', 'guide.now'])
        self.dialog.select.side_effect = lambda _, labels: labels.index(w.t('guide_controls'))
        self.action(CONTEXT)
        self.assertEqual(w.getFocusId(), 306)
        self.dialog.select.side_effect = None
        self.dialog.select.return_value = 1  # Favorites
        self.click(306)
        self.assertEqual(w.guide.group, 'Favorites')

        days = day_choices(*repo.horizon)
        self.dialog.select.side_effect = [0, 0]
        self.click(307)
        self.assertEqual(w.guide.selection.anchor, days[0][2][0])
        self.assertEqual(w.controls[NOW].properties['setVisible'], (False,))
        self.assertEqual(w.getFocusId(), 307)
        self.click(308)
        self.assertEqual(w.guide.selection.anchor, repo.now)
        self.assertEqual(w.controls[NOW].properties['setPosition'],
                         (timeline_x(repo.now, w.guide.viewport, w.guide.span), 332))
        self.assertEqual(w.controls[NOW].properties['setVisible'], (True,))
        self.assertEqual(w.getFocusId(), 308)
        self.action(UP)  # rail to content
        self.action(RIGHT)
        saved = w.guide.bookmark()
        self.action(NAV_BACK)
        self.assertEqual(w.controls[TITLE].properties['setVisible'], (False,))
        self.assertEqual(w.controls[NOW].properties['setVisible'], (False,))
        self.enter('Guide')
        self.assertEqual(w.guide.bookmark(), saved)
        w._metadata_loader.request.assert_not_called()
        w._playback_ui.request.assert_not_called()

    def test_date_rejects_post_modal_viewport_change_and_cancel_keeps_focus(self):
        repo = self.seed()
        self.enter('Guide')
        w = self.w
        original = w.guide.bookmark()
        self.dialog.select.side_effect = [0, -1]
        self.click(307)
        self.assertEqual(w.guide.bookmark(), original)
        self.assertEqual(w.getFocusId(), 307)

        def alter_selection(_heading, _labels, **_kwargs):
            w.guide.jump(repo.horizon[1] - 1)
            return 0

        self.dialog.select.side_effect = alter_selection
        self.dialog.select.reset_mock()
        self.click(307)
        self.dialog.select.assert_called_once()  # Reject before asking for a time.
        self.assertEqual(w.guide.selection.anchor, repo.horizon[1] - 1)
        self.assertEqual(w.getFocusId(), 307)

    def test_date_shortcuts_jump_to_exact_retained_endpoints_without_loading(self):
        repo = self.seed()
        self.enter('Guide')
        w = self.w
        for key, instant in (('latest', repo.horizon[1] - 1), ('earliest', repo.horizon[0])):
            with self.subTest(key=key):
                self.dialog.select.reset_mock()
                self.dialog.select.side_effect = lambda _, labels, **kw: labels.index(w.t(key))
                self.click(307)
                self.dialog.select.assert_called_once()
                self.assertEqual(w.guide.selection.anchor, instant)
                self.assertEqual(w.getFocusId(), 307)
        w._metadata_loader.request.assert_not_called()

    def test_now_survives_index_adoption_after_live_metadata_has_settled(self):
        ready = type(self.w._guide_runtime).ready
        old = self.seed()
        self.enter('Guide')
        w = self.w
        runtime = w._guide_runtime
        repo = MockRepository()
        start = old.horizon[1]
        repo.horizon, repo.now = (start, start + 7200), start
        now = start + 1200
        event = Event('new', repo.channels[0].key, now, now + 900, 'New horizon')
        repo.events = (event,)
        owner = object()
        source = IndexInput(repo.events, repo.channels, 'synthetic', 'fixture', repo.horizon,
                            start, 1, object(), owner)
        product = build_index(source)
        w.repository = repo
        w._live_session = SimpleNamespace(loading=False, incarnation=owner, _generation=1, _catalog_epoch=1,
            channel_projection=None, guide=w.data_session.guide, accepts_index_origin=lambda p: p is product,
            cached_event_refs=lambda: (), cached_resource_refs=lambda: ())
        w.transient_demo_mode = False
        w._intended_anchor = start
        runtime.result = IndexResult(1, source, source, product)
        runtime.context = runtime._context(source)
        runtime.context.applied = True  # Metadata advanced while the old controller is still installed.
        with (patch.object(type(runtime), 'ready', ready), patch.object(w, 'utc_now', return_value=now),
              patch.object(w._metadata_loader, 'owns_result', return_value=True),
              patch.object(w._metadata_loader, 'replace_display', return_value=object()),
              patch.object(w._metadata_loader, 'commit_retirement')):
            self.assertFalse(runtime.ready)
            w.render()
            self.click(308)
            self.assertEqual(w._intended_anchor, now)
            self.assertTrue(runtime._consume())
            self.assertTrue(runtime.ready)
            self.assertEqual(w.guide.selection.anchor, now)
            self.assertIs(w.guide.selected_event(), event)
            self.assertIsNone(w._intended_anchor)
        w._metadata_loader.request.assert_not_called()

    def test_retained_guide_drops_hits_and_current_details_without_moving_focus(self):
        self.seed()
        self.enter('Guide')
        w = self.w
        w.focus_writes.clear()
        w._show_event_details = Mock()
        with patch.object(GuideController, 'prepared_ready', True), patch.object(type(w._guide_runtime), 'ready', False):
            w.render()
            self.assertTrue(w.pool)
            self.assertFalse(self.hits('cell'))
            self.assertIsNone(w._chrome.capture(307))
            self.assertIn(w.t('preparing_guide'), w.controls[TITLE].properties['setLabel'][0])
            self.assertEqual(w.controls[DESCRIPTION].properties['setText'], ('',))
            self.assertFalse(w.focus_writes)
            w.onClick(CONTENT)
            w.process_pending()
            w._show_event_details.assert_not_called()

    def test_clock_update_does_not_rebuild_hits_or_move_focus(self):
        repo = self.seed()
        self.enter('Guide')
        w = self.w
        active = w._chrome.presented
        w.focus_writes.clear()
        with patch.object(w, '_live_session', object()), patch.object(w, 'utc_now', return_value=repo.now + 300) as now:
            paint_clock(w)
            self.assertEqual(w.controls[NOW].properties['setPosition'],
                             (timeline_x(repo.now + 300, w.guide.viewport, w.guide.span), 332))
            now.return_value = w.guide.viewport + w.guide.span
            paint_clock(w)
            self.assertEqual(w.controls[NOW].properties['setVisible'], (False,))
            self.assertEqual(w._guide_line.properties['setVisible'], (False,))
        self.assertIs(w._chrome.presented, active)
        self.assertFalse(w.focus_writes)

    def test_now_line_is_foreground_and_axis_and_line_stop_at_short_horizon(self):
        repo = self.seed()
        w = self.w
        start = repo.horizon[0]
        end = start + 1800
        repo.horizon, repo.now = (start, end), end - 1
        key = repo.channels[0].key
        repo.events = (Event('long', key, start, end, 'Occupied row'),)
        w.data_session = OfflineDataSession(repo, strings=w.strings)
        w.guide = GuideController(repo, strings=w.strings)
        self.enter('Guide')
        right = timeline_x(end, start, w.guide.span)
        first_line = w._guide_line
        hit = self.hits('cell')[(key, 'long', start)]
        self.assertEqual(first_line.properties['setPosition'], (right - 1, ROW_TOP))
        self.assertEqual(first_line.properties['setWidth'], (1,))
        self.assertEqual(first_line.properties['setVisible'], (True,))
        self.assertEqual(w.controls[NOW].properties['setWidth'], (1,))
        self.assertTrue(w.controls[140].properties['setLabel'][0])
        self.assertTrue(all(w.controls[i].properties['setLabel'] == ('',) for i in range(141, 144)))
        self.assertIsNone(w._chrome.capture(first_line.ident))
        self.assertIsNotNone(w._chrome.capture(hit))
        with patch.object(w, '_live_session', object()), patch.object(w, 'utc_now', return_value=start + 300):
            paint_clock(w)
        x = first_line.properties['setPosition'][0]
        hit_x, hit_y, hit_width, _ = w.controls[hit].args[:4]
        self.assertTrue(hit_x < x < hit_x + hit_width)
        self.assertEqual(hit_y, ROW_TOP)
        self.assertEqual(first_line.properties['setWidth'], (4,))
        self.assertGreater(first_line.ident, max(control.ident for pair in w.pool for control in pair))

        repo.events = (Event('first', key, start, start + 900, 'First'),
                       Event('second', key, start + 900, end, 'Second'))
        w.data_session = OfflineDataSession(repo, strings=w.strings)
        w.guide.refresh()
        w.render()
        line = w._guide_line
        self.assertIn(first_line, [image for image, _ in w.pool])
        self.assertGreater(line.ident, max(control.ident for pair in w.pool for control in pair))
        self.assertEqual(line.properties['setHeight'], (477,))
        self.action(NAV_BACK)
        self.assertEqual(line.properties['setVisible'], (False,))

    def test_stress_guide_keeps_mouse_and_draw_budgets_and_skips_overflow_targets(self):
        self.seed()
        w = self.w
        w.repository = repo = stress_repository()
        w.data_session = OfflineDataSession(repo, 'stress', strings=w.strings)
        w.guide = GuideController(repo, strings=w.strings)
        self.enter('Guide')
        self.assertLessEqual(len(w.pool), GuideController.max_cells)
        hits = self.hits('cell')
        self.assertLessEqual(len(hits), GuideController.max_cells)
        overflow = [cell for cell in w.guide.cells(0) if cell.overflow]
        self.assertTrue(overflow)
        self.assertTrue(all((cell.channel, None, cell.start) not in hits for cell in overflow))
        self.assertIn(w.t('guide_dense_notice'), w.controls[113].properties['setLabel'][0])

    def test_modal_back_and_queued_select_are_not_parent_actions(self):
        self.seed()
        self.enter('Guide')
        w = self.w
        w._show_event_details = Mock()

        def cancel_dropdown(*args, **kwargs):
            self.assertTrue(w._dispatcher.modal_active)
            self.post_action(NAV_BACK)
            w.onClick(CONTENT)
            w.process_pending()  # no reentrant parent dispatch or poll
            return -1

        self.dialog.select.side_effect = cancel_dropdown
        w.focus_id = 306  # Guide Collection button.
        w.focus_writes.clear()
        w.onClick(306)
        self.post_action(NAV_BACK)  # already captured in the same owner batch
        w.onClick(CONTENT)
        w.process_pending()
        self.assertEqual(w.view, 'Guide')
        self.assertEqual(w.getFocusId(), 306)
        self.assertEqual(w.focus_writes, [])
        w._show_event_details.assert_not_called()
        self.action(NAV_BACK)
        self.assertEqual(w.view, 'Dashboard')

    def test_back_queued_before_route_or_window_transition_cannot_exit(self):
        w = self.w
        w.onClick(201)
        self.post_action(NAV_BACK)
        w.onClick(200)
        w.process_pending()
        self.assertEqual(w.view, 'Guide')
        self.post_action(NAV_BACK)
        w.onInit()
        w.process_pending()
        self.assertEqual(w.view, 'Guide')
        self.front += 1
        self.action(NAV_BACK)
        self.assertEqual(w.view, 'Guide')
        self.front -= 1
        self.action(NAV_BACK)
        self.assertEqual(w.view, 'Dashboard')

    def test_recordings_tree_and_search_return_to_exact_logical_parent(self):
        self.seed()
        self.enter('Recordings')
        w = self.w
        show = w.library.rows()[1].id
        self.click(self.hits('row')[(show,)])
        self.assertEqual(w.library.depth, 1)
        season = w.library.selected.id
        self.click(CONTENT)
        self.assertEqual(w.library.depth, 2)
        episode = w.library.selected.id
        w.library.set_query('chapter')
        w.library.set_provenance_filter('import')
        w.render()
        search_row = w.library.selected.id
        self.enter('Dashboard')
        self.enter('Recordings')
        self.assertEqual(w.library.selected.id, search_row)
        for focus, depth, identity in ((202, 2, episode), (304, 1, season), (100, 0, show)):
            w.focus_id = focus
            self.action(NAV_BACK)
            self.assertEqual(w.library.depth, depth)
            self.assertEqual(w.library.selected.id, identity)
            self.assertEqual(w.library.provenance_filter, 'all')
        self.action(NAV_BACK)
        self.assertEqual(w.view, 'Dashboard')

    def test_recordings_tabs_and_local_controls_have_reachable_distinct_native_ids(self):
        self.seed()
        self.enter('Recordings')
        w = self.w
        self.assertEqual([w.controls[i].properties['setLabel'][0] for i in RECORDINGS_TABS],
                         ['Recent', w.t('recordings_active_tab', tab='TV Shows'), 'Movies', 'All', 'Search'])
        self.assertEqual(w.getFocusId(), CONTENT)
        self.action(LEFT)
        self.assertEqual(w.getFocusId(), 221)  # Active TV Shows tab.
        self.action(LEFT)
        self.assertEqual(w.getFocusId(), 220)
        w.onClick(w.getFocusId())  # Remote Select on the focused native tab.
        w.process_pending()
        self.assertEqual((w.library.mode, w.getFocusId()), ('Recent', 220))
        self.action(DOWN)
        self.action(RIGHT)
        self.assertEqual(w.getFocusId(), SOURCE)  # No walk to the last row.
        self.dialog.select.return_value = 2
        w.onClick(w.getFocusId())
        w.process_pending()
        self.assertEqual((w.library.provenance_filter, len(w.library.rows())), ('import', 2))
        self.action(RIGHT)
        self.assertEqual(w.getFocusId(), SORT)
        self.dialog.select.return_value = 0
        w.onClick(w.getFocusId())
        w.process_pending()
        self.assertEqual(w.library.sort_order, 'title')
        self.assertIn(w.t('recordings_title'), w.library.breadcrumb)
        self.click(222)
        self.assertEqual((w.library.mode, len(w.library.rows())), ('Movies', 1))
        self.click(223)
        self.assertEqual((w.library.mode, len(w.library.rows())), ('All', 2))
        self.click(224)
        self.assertEqual((w.library.mode, w.getFocusId()), ('Search', 224))
        self.assertEqual(w.controls[113].properties['setLabel'], (w.t('recording_search_hint'),))
        self.dialog.select.return_value = 2  # Synthetic "atlas" preset, not programme Search.
        self.click(QUERY)
        self.assertEqual((w.library.query, len(w.library.rows()), w.search_query), ('atlas', 1, ''))
        self.dialog.select.return_value = 0  # Turn off the imported-only filter.
        self.click(SOURCE)
        self.assertEqual((w.library.provenance_filter, len(w.library.rows())), ('all', 4))
        self.action(NAV_BACK)
        self.assertEqual((w.library.mode, w.library.provenance_filter), ('All', 'import'))
        self.enter('Guide')
        for ident in (*RECORDINGS_TABS, *DVR_TABS, SOURCE, SORT, QUERY):
            self.assertIsNone(w._chrome.capture(ident))
            self.assertEqual(w.controls[ident].properties['setEnabled'], (False,))
            self.assertEqual(w.controls[ident].properties['setVisible'], (False,))
        w._playback_ui.request.assert_not_called()
        w._recording_ui.request.assert_not_called()
        w._save_development.assert_not_called()
        w._metadata_loader.request.assert_not_called()

    def test_wheel_scrolls_guide_and_recordings_without_opening_or_moving_focus(self):
        self.seed()
        self.enter('Guide')
        w = self.w
        w._show_event_details = Mock()
        w.focus_id = next(iter(self.hits('cell').values()))
        w.focus_writes.clear()
        selection = w.guide.selection
        self.action(WHEEL_DOWN)
        self.assertNotEqual(w.guide.selection.channel, selection.channel)
        self.assertEqual(w.guide.selection.anchor, selection.anchor)
        self.action(WHEEL_UP)
        self.assertEqual(w.guide.selection, selection)
        self.assertFalse(w.focus_writes)
        w._show_event_details.assert_not_called()

        self.enter('Recordings')
        self.click(223)  # All
        w.library.move(5)
        w.render()
        hit = self.hits('row')[(w.library.selected.id,)]
        w.focus_id = hit
        w.focus_writes.clear()
        self.action(WHEEL_DOWN)
        self.assertEqual((w.library.position, w.library.top), (6, 6))
        self.assertIsNone(w._chrome.capture(hit))
        self.action(WHEEL_UP)  # The native focus ID still refers to the retired hit.
        self.assertEqual((w.library.position, w.library.top), (5, 0))
        self.assertFalse(w.focus_writes)
        self.dialog.textviewer.assert_not_called()

    def test_recordings_reject_queued_and_mid_transition_clicks_and_stale_chooser(self):
        self.seed()
        self.enter('Recordings')
        w = self.w
        old_hit = next(iter(self.hits('row').values()))
        w.focus_id = 220
        render = w.render

        def render_with_input():
            self.assertTrue(w._dispatcher.modal_active)
            render()
            self.post_action(NAV_BACK)
            w.onClick(222)  # Callback during the repaint, even after native publication.

        with patch.object(w, 'render', side_effect=render_with_input):
            w.onClick(220)
            w.onClick(old_hit)
            self.post_action(NAV_BACK)
            w.process_pending()
        w.process_pending()
        self.assertEqual((w.view, w.library.mode, w.library.depth), ('Recordings', 'Recent', 0))
        self.assertNotIn(old_hit, w.controls)
        self.dialog.textviewer.assert_not_called()

        def moved_during_chooser(*args, **kwargs):
            w.library.set_mode('Movies')
            return 2  # Oldest; stale for the new tab.

        self.dialog.select.side_effect = moved_during_chooser
        self.click(SORT)
        self.assertEqual((w.library.mode, w.library.sort_order), ('Movies', 'title'))
        self.dialog.select.side_effect = None
        w.render()
        self.dialog.select.return_value = -1
        self.click(SOURCE)
        self.assertEqual((w.getFocusId(), w.library.provenance_filter), (SOURCE, 'all'))

    def test_recordings_partial_and_removed_row_refresh_never_retarget_mouse(self):
        repo = self.seed()
        self.enter('Recordings')
        w = self.w
        self.click(223)  # All
        self.action(DOWN)
        self.action(PAGE_DOWN)
        self.assertEqual((w.library.position, w.library.top), (6, 6))
        row = w.library.rows()[8]
        hit = self.hits('row')[(row.id,)]
        self.assertEqual(w.controls[hit].args[:4], (80, 475, 1760, 72))
        self.click(hit)
        self.dialog.textviewer.assert_called_once_with(row.label, row.detail)
        self.click(220)
        self.click(223)
        self.assertEqual((w.library.selected.id, w.library.top), (row.id, 6))

        w.data_session.choose('partial')
        w.render()
        self.assertEqual(w.library.selected.id, row.id)  # Retained accepted snapshot, not the partial page.
        self.assertIn(w.t('live_partial'), w.controls[114].properties['setLabel'][0])
        stale = self.hits('row')[(row.id,)]
        repo.recordings = tuple(r for r in repo.recordings if r != w.library.selected_recording())
        w.data_session = OfflineDataSession(repo, strings=w.strings)
        w.focus_id = CONTENT
        w.focus_writes.clear()
        w.onClick(stale)  # Current data changed but native targets have not been repainted.
        w.process_pending()
        self.dialog.textviewer.assert_called_once()
        w.render()
        self.assertEqual((w.library.position, w.library.top), (7, 5))
        self.assertIsNone(w._chrome.capture(stale))
        self.assertNotIn((row.id,), self.hits('row'))
        self.assertEqual(w.focus_writes, [])
        w.data_session.choose('empty')
        w.render()
        self.assertEqual((w.library.selected, w.library.position, w.library.top), (None, 0, 0))
        self.assertFalse(self.hits('row'))
        self.assertEqual(w.controls[112].properties['setLabel'], (w.library.breadcrumb,))
        self.assertIn(w.t('recordings_all'), w.library.breadcrumb)
        w._playback_ui.request.assert_not_called()

    def test_recordings_disconnect_hides_breadcrumb_while_retirement_is_busy(self):
        self.seed()
        self.enter('Recordings')
        w = self.w
        self.click(CONTENT)  # Synthetic Atlas (DVR)
        self.click(CONTENT)  # Season 1
        library = w.library
        breadcrumb = library.breadcrumb
        self.assertIn('Synthetic Atlas', breadcrumb)
        self.assertTrue(breadcrumb.endswith('Season 1'))
        self.assertEqual(w.controls[112].properties['setLabel'], (breadcrumb,))
        hit = next(iter(self.hits('row').values()))

        with patch.object(w._metadata_loader, 'offer_retirement', return_value=None) as offer:
            w._close_demo()
            w.onClick(hit)  # Old hit arrives before the blocked view has repainted.
            w.process_pending()
        offer.assert_called_once()
        self.assertIs(w.library, library)
        self.assertEqual(w.library.breadcrumb, breadcrumb)
        self.assertEqual(w.controls[112].properties['setLabel'], (
            w.t('empty_view', view=w.t('recordings'), status=w.t('not_connected')),))
        self.assertFalse(self.hits('row'))
        self.dialog.textviewer.assert_not_called()
        w._playback_ui.request.assert_not_called()

    def test_recordings_play_is_explicit_and_stale_or_ineligible_intent_cannot_dispatch(self):
        self.seed()
        w = self.w
        data = w.data_session.recordings()
        w.transient_demo_mode = False
        w._live_session = SimpleNamespace(loading=False, incarnation=object(), _generation=1,
                                          _catalog_epoch=object(), recordings=lambda: data)
        w._development_config = config = SimpleNamespace(namespace='synthetic')
        w._saved_config = lambda: config
        w._addon.getSettingBool = lambda _: True
        w._playback_ui.request = Mock()  # Observation only; never submit to the real worker.
        self.enter('Recordings')
        self.assertIsNone(w._chrome.capture(301))  # A show folder is not playable.
        self.click(220)
        self.assertEqual(w.library.selected_recording().provenance, 'dvr')
        stale = w._chrome.capture(301)
        self.assertIsNotNone(stale)
        self.click(301)
        w._playback_ui.request.assert_called_once()
        w._playback_ui.request.reset_mock()

        w.focus_id = 223
        w.onClick(223)
        w.onClick(301)
        w.process_pending()
        self.assertEqual(w.library.mode, 'All')
        self.assertFalse(w._chrome.click(stale, self.gui))
        self.assertIsNotNone(w._chrome.capture(301))
        data = replace(data, state=DataState.PARTIAL)
        self.click(301)  # A partial refresh disqualifies Play before the repaint.
        w.render()
        self.assertIsNone(w._chrome.capture(301))
        self.assertTrue(self.hits('row'))
        data = replace(data, state=DataState.READY)
        self.dialog.select.return_value = 2  # Imported media: details only.
        self.click(SOURCE)
        self.assertEqual(w.library.selected_recording().provenance, 'import')
        self.assertIsNone(w._chrome.capture(301))
        w._playback_ui.request.assert_not_called()

    def test_search_drill_in_returns_to_results_and_preserves_primary_guide(self):
        repo = self.seed()
        w = self.w
        self.enter('Guide')
        w.guide.vertical(3)
        primary = w.guide.bookmark()
        self.enter('Search')
        w._set_search_query('synthetic')
        w.render()
        self.action(DOWN)  # from the empty Search fallback into the newly available results
        self.action(PAGE_DOWN)
        result = w._selected_search_event()
        self.assertEqual(w.positions['Search'], 6)
        page_ids = set(self.hits('row'))
        self.click(302)  # Show in Guide
        self.assertEqual(w._route, Route('Guide', 'Search'))
        self.assertEqual(airing_key(w.guide.selected_event()), airing_key(result))
        with patch.object(type(w._guide_runtime), 'ready', False):
            bookmark = w.guide.bookmark()
            w.render()
            self.assertIsNone(w._chrome.capture(308))
            self.assertFalse(execute_command(w, self.gui, 'guide.now'))
            self.assertEqual(w.guide.bookmark(), bookmark)
        # Successful publication can replace the controller even without a
        # source switch. The bookmark must not depend on keeping it alive.
        w.guide = GuideController(repo, strings=w.strings)
        self.assertTrue(w.guide.focus_event(result))
        with patch.object(w, '_live_session', object()), patch.object(type(w._guide_runtime), 'ready', True):
            w._jump_time(repo.horizon[1] + 3600)
        self.assertEqual(w.guide.selection.anchor, repo.horizon[1] - 1)
        w._metadata_loader.request.assert_not_called()
        w.positions['Search'] = 0
        w.search_selection = None
        self.action(NAV_BACK)
        self.assertEqual(w.view, 'Search')
        self.assertEqual(w.search_selection, airing_key(result))
        self.assertEqual(w.positions['Search'], 6)
        self.assertEqual(set(self.hits('row')), page_ids)
        self.assertEqual(w.getFocusId(), 302)
        self.enter('Guide')
        self.assertEqual(w.guide.bookmark(), primary)

    def test_old_search_result_dialog_also_enters_a_child(self):
        self.seed()
        self.enter('Search')
        w = self.w
        w._set_search_query('special')
        w.render()
        event = w._selected_search_event()
        self.dialog.select.return_value = 1  # legacy result chooser: Show in Guide
        self.click(300)
        self.assertEqual(w._route, Route('Guide', 'Search'))
        self.assertEqual(w.guide.selected_event(), event)
        self.action(NAV_BACK)
        self.assertEqual(w._selected_search_event(), event)
        self.assertEqual(w.getFocusId(), 300)

    def test_list_mouse_hit_is_identity_bound_and_data_never_steals_focus(self):
        repo = self.seed()
        self.enter('DVR')
        w = self.w
        old_hit = self.hits('row')[('job-2',)]
        stale = w._chrome.capture(old_hit)
        self.assertEqual(w.controls[old_hit].args[:4], (80, 345, 1760, 85))
        self.click(old_hit)
        self.assertEqual(w._dvr_selected, 'job-2')
        self.dialog.textviewer.assert_called_once()
        self.assertIn('Ambiguous source', self.dialog.textviewer.call_args.args[0])

        self.enter('Settings')
        repo.jobs = tuple(reversed(repo.jobs))
        w.data_session = OfflineDataSession(repo, strings=w.strings)
        self.enter('DVR')
        self.assertEqual(w._dvr_selected, 'job-2')
        self.assertEqual(w.positions['DVR'], 2)
        fresh_hit = self.hits('row')[('job-2',)]
        self.assertNotEqual(fresh_hit, old_hit)
        w.focus_id = 304
        w.focus_writes.clear()
        w.onClick(old_hit)  # late native callback after replacement
        w._dispatcher.post(('chrome', stale))  # already captured activation
        w.process_pending()
        self.assertEqual(self.dialog.textviewer.call_count, 1)

        repo.jobs = tuple(job for job in repo.jobs if job.id != 'job-2')
        w.data_session = OfflineDataSession(repo, strings=w.strings)
        # The owner has not yet repainted the replacement data: reject the
        # displayed ID by current data proof, not only by SDK control removal.
        w.onClick(fresh_hit)
        w.process_pending()
        self.assertEqual(self.dialog.textviewer.call_count, 1)
        w.render()
        self.assertEqual(w.positions['DVR'], 2)
        self.assertEqual(w._dvr_selected, repo.jobs[2].id)
        self.assertEqual(w.getFocusId(), 304)
        self.assertEqual(w.focus_writes, [])

    def test_guide_hit_selects_other_source_by_airing_not_the_current_cursor(self):
        self.seed()
        self.enter('Guide')
        w = self.w
        w._show_event_details = Mock()
        initial = w.guide.selected_event()
        key, ident = next((key, ident) for key, ident in self.hits('cell').items()
                          if key[1] is not None and key[0] != initial.channel)
        self.click(ident)
        self.assertEqual(airing_key(w.guide.selected_event()), (key[0], key[1], key[2]))
        w._show_event_details.assert_called_once_with(w.guide.selected_event())

    def test_single_second_selection_never_paints_over_adjacent_pointer_hit(self):
        repo = self.seed()
        w = self.w
        start = repo.horizon[0]
        repo.now = start + 900  # Keep the unrelated Now marker out of the shared x.
        key = repo.channels[0].key
        short = Event('short', key, start, start + 1, 'One second')
        next_event = Event('next', key, start + 1, start + 61, 'Next')
        repo.events = (short, next_event)
        w.data_session = OfflineDataSession(repo, strings=w.strings)
        w.guide = GuideController(repo, strings=w.strings)
        w.guide.jump(next_event.start)
        self.enter('Guide')
        self.action(LEFT)
        self.assertIs(w.guide.selected_event(), short)
        self.assertEqual(w.controls[TITLE].properties['setLabel'], (short.title,))
        self.assertTrue(w.controls[120].properties['setLabel'][0].startswith('› '))
        self.assertNotIn(airing_key(short), self.hits('cell'))
        hit = self.hits('cell')[airing_key(next_event)]
        self.assertEqual(w.controls[hit].args[:2], (GRID_LEFT, ROW_TOP))
        images = [image for image, _ in w.pool[:w._visible_count]
                  if image.properties['setPosition'] == (GRID_LEFT, ROW_TOP)]
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].properties['setImage'][0].endswith('cell.png'))
        w._show_event_details = Mock()
        self.click(hit)
        w._show_event_details.assert_called_once_with(next_event)

    def test_overlarge_accepted_end_renders_localized_unavailable_and_owner_survives(self):
        repo = self.seed()
        w = self.w
        event = Event('overlarge', repo.channels[0].key, repo.now, 253402300800, 'Still selectable')
        repo.events = (event,)
        w.data_session = OfflineDataSession(repo, strings=w.strings)
        w.guide = GuideController(repo, strings=w.strings)
        self.enter('Guide')
        self.assertEqual(w.controls[TITLE].properties['setLabel'], (event.title,))
        self.assertIn(w.t('time_unavailable'), w.controls[METADATA].properties['setLabel'][0])
        self.assertIn(airing_key(event), self.hits('cell'))
        self.action(NAV_BACK)
        self.assertEqual(w.view, 'Dashboard')
        self.assertFalse(w._closed)

    def test_guide_hit_matches_painted_clip_and_cannot_survive_an_unpainted_jump(self):
        self.seed()
        self.enter('Guide')
        w = self.w
        w._show_event_details = Mock()
        key, ident = next((key, ident) for key, ident in self.hits('cell').items() if key[1] is not None)
        x, y, width, height = w.controls[ident].args[:4]
        self.assertTrue(any(image.properties.get('setPosition') == (x, y)
                            and image.properties.get('setWidth') == (width,)
                            and image.properties.get('setHeight') == (height,) for image, _ in w.pool))
        old = w._chrome.capture(ident)
        w.guide.jump(w.repository.horizon[0])
        w._dispatcher.post(('chrome', old))
        w.process_pending()
        w._show_event_details.assert_not_called()
        w.render()
        self.assertNotIn(ident, self.hits('cell').values())
        key, ident = next((key, ident) for key, ident in self.hits('cell').items() if key[1] is not None)

        def details(event):
            self.assertEqual(airing_key(event), key)
            self.assertEqual(w.controls[TITLE].properties['setLabel'], (event.title,))

        w._show_event_details.side_effect = details
        self.click(ident)
        w._show_event_details.assert_called_once()
        w._playback_ui.request.assert_not_called()

    def test_shortcut_intent_and_read_only_navigation(self):
        self.seed()
        self.enter('Guide')
        w = self.w
        w._show_event_details = Mock()
        old = w.guide.selection
        self.post_action(PAGE_DOWN)
        self.post_action(INFO)  # captured the pre-page event; must not open its replacement
        w.process_pending()
        self.assertNotEqual(w.guide.selection.channel, old.channel)
        w._show_event_details.assert_not_called()
        self.action(INFO)
        if w.guide.selected_event() is not None:
            w._show_event_details.assert_called_once_with(w.guide.selected_event())
        self.action(CONTEXT)
        self.dialog.select.assert_called_once()
        labels = self.dialog.select.call_args.args[1]
        self.assertIn(w.t('search_programmes'), labels)
        self.assertNotIn(w.t('collection'), labels)
        self.assertNotIn(w.t('dashboard'), labels)
        self.assertNotIn(w.t('connection'), labels)
        self.assertNotIn(w.t('watch_short'), labels)
        self.assertNotIn(w.t('schedule_short'), labels)
        w._playback_ui.request.assert_not_called()
        w._recording_ui.request.assert_not_called()
        w._save_development.assert_not_called()
        w._metadata_loader.request.assert_not_called()

    def test_more_revalidates_recording_status_after_native_dropdown(self):
        self.seed()
        self.enter('Guide')
        w = self.w
        # An admitted presentation with the real registry and a fake worker.
        w._live_session = SimpleNamespace(loading=False, incarnation=object(), _generation=1, _catalog_epoch=1,
                                          guide=w.data_session.guide)
        config = SimpleNamespace(namespace='synthetic')
        w.transient_demo_mode = False
        w._development_config = config
        w._saved_config = lambda: config
        w._addon.getSettingBool = lambda _: True
        w._guide_runtime.presentation = lambda data: data
        w._recording_ui._current_config = Mock(return_value=config)
        w._recording_ui.request = Mock()
        first, second = w.repository.events[:2]
        w.guide.focus_event(first)
        w.render()

        def change_context(_heading, labels):
            w.guide.focus_event(second)
            return labels.index(w.t('recording_status'))

        self.dialog.select.side_effect = change_context
        self.action(CONTEXT)
        w._recording_ui.request.assert_not_called()
        self.assertEqual(w.view, 'Guide')
        w.render()
        self.dialog.select.side_effect = lambda _, labels: labels.index(w.t('recording_status'))
        self.action(CONTEXT)
        w._recording_ui.request.assert_called_once_with(schedule=False)

    def test_storage_poll_fences_only_when_native_dialog_runs(self):
        from channels_dvr.storage_ui import StorageResult

        self.enter('Settings')
        w = self.w
        worker = Mock()
        w._storage_ui.worker = worker
        w._storage_ui._presentation = w._chrome.layout
        worker.poll.return_value = None
        self.post_action(NAV_BACK)
        w._storage_ui.poll()
        w.process_pending()
        self.assertEqual(w.view, 'Dashboard')

        self.enter('Settings')
        w._storage_ui._presentation = w._chrome.layout
        worker.poll.side_effect = [StorageResult('status'), None]
        w.onClick(CONTENT)
        self.post_action(NAV_BACK)

        def cancel(*args):
            self.assertTrue(w._dispatcher.modal_active)
            self.post_action(NAV_BACK)
            w.onClick(CONTENT)
            return -1

        self.dialog.select.side_effect = cancel
        w._storage_ui.poll()
        w.process_pending()
        self.assertEqual(w.view, 'Settings')
        self.assertEqual(w.getFocusId(), CONTENT)
        worker.submit.assert_not_called()
        w._save_development.assert_not_called()

    def test_hit_cap_and_removal_failure_quarantines_until_cleanup(self):
        from channels_dvr.ui_hits import ContentHits, Hit

        native = WindowXML()
        hits = ContentHits(native)
        plan = tuple(Hit((0, i, 1, 1), ('row', str(i))) for i in range(GuideController.max_cells + 1))
        targets = hits.sync(self.gui, 'first', plan)
        self.assertEqual(len(targets), GuideController.max_cells)
        original_remove = native.removeControl
        first = native.controls[targets[0][0]]

        def remove_once(control):
            if control is first:
                raise RuntimeError('retire was refused')
            original_remove(control)

        with patch.object(native, 'removeControl', side_effect=remove_once):
            self.assertEqual(hits.sync(self.gui, 'second', plan), ())
            self.assertEqual(first.properties['setEnabled'], (False,))
            self.assertEqual(first.properties['setVisible'], (False,))
            self.assertEqual(hits.sync(self.gui, 'third', plan), ())
        hits.clear()
        self.assertNotIn(first.ident, native.controls)

    def test_hit_setup_failure_retires_native_control(self):
        from channels_dvr.ui_hits import ContentHits, Hit

        native = WindowXML()
        hits = ContentHits(native)
        button = Control()
        button.setNavigation = Mock(side_effect=RuntimeError('native setter refused'))
        gui = SimpleNamespace(ControlButton=lambda *args, **kwargs: button)
        self.assertEqual(hits.sync(gui, 'scope', (Hit((0, 0, 1, 1), ('row', 'r')),)), ())
        self.assertNotIn(button.ident, native.controls)

    def test_disconnected_tabs_and_settings_do_not_open_operation_workflows(self):
        w = self.w
        w._series_manager = self.forbidden('Series control')
        self.enter('DVR')
        for ident, key in ((230, 'scheduled'), (231, 'dvr_passes')):
            self.assertEqual(w.controls[ident].properties['setVisible'], (True,))
            self.assertEqual(w.controls[ident].properties['setEnabled'], (True,))
            label = ('> ' if ident == 230 else '') + w.t(key)
            self.assertEqual(w.controls[ident].properties['setLabel'], (label,))
        self.assertEqual(w._dvr.tab, 'dvr.scheduled')
        self.action(RIGHT)
        self.assertEqual((w.getFocusId(), w._dvr.tab), (231, 'dvr.scheduled'))
        w.onClick(w.getFocusId())
        w.process_pending()
        self.assertEqual(w.getFocusId(), 231)
        self.assertEqual(w.controls[230].properties['setLabel'], (w.t('scheduled'),))
        self.assertEqual(w.controls[231].properties['setLabel'], ('> ' + w.t('dvr_passes'),))
        self.click(303)
        w._series_manager.assert_not_called()
        self.assertEqual(w._dvr.tab, 'dvr.passes')
        self.assertFalse(w._current_data().can_browse)
        self.enter('Settings')
        self.assertEqual([row.id for row in w._rows()], [
            'connection.configure', 'connection.connect', 'connection.refresh', 'connection.status',
            'connection.disconnect', 'connection.settings', 'connection.storage', 'connection.diagnostics',
        ])
        self.assertEqual(w.controls[113].properties['setLabel'], (w.t('connection_prompt'),))
        for ident in (*DVR_TABS, *RECORDINGS_TABS):
            self.assertEqual(w.controls[ident].properties['setVisible'], (False,))
            self.assertEqual(w.controls[ident].properties['setEnabled'], (False,))
        w._addon.openSettings.assert_not_called()
        self.click(self.hits('row')[('connection.settings',)])
        w._addon.openSettings.assert_called_once()
        w._addon.setSettingBool.assert_not_called()

    def test_tabs_are_view_local_and_queued_clicks_do_not_cross_views(self):
        self.seed()
        w = self.w
        self.assertFalse(DVR_TABS.keys() & RECORDINGS_TABS.keys())
        self.enter('Recordings')
        recording_tab = w._chrome.capture(222)
        self.assertEqual(recording_tab.command, 'recording.tab.movies')
        self.assertFalse(DVR_TABS.keys() & w._chrome.presented.keys())
        self.enter('DVR')
        for ident in (*RECORDINGS_TABS, SOURCE, SORT, QUERY):
            self.assertIsNone(w._chrome.capture(ident))
            self.assertEqual(w.controls[ident].properties['setEnabled'], (False,))
        passes_tab = w._chrome.capture(231)
        self.assertEqual(passes_tab.command, 'dvr.passes')
        self.assertFalse(execute_command(w, self.gui, recording_tab.command))
        w._dispatcher.post(('chrome', recording_tab))
        w.process_pending()
        self.assertEqual((w._dvr.tab, w.library.mode), ('dvr.scheduled', 'Shows'))
        self.enter('Recordings')
        self.assertFalse(execute_command(w, self.gui, passes_tab.command))
        w._dispatcher.post(('chrome', passes_tab))
        w.process_pending()
        self.assertEqual((w._dvr.tab, w.library.mode), ('dvr.scheduled', 'Shows'))

    def test_demo_tabs_restore_both_ids_and_child_back_keeps_the_pass_parent(self):
        self.seed()
        w = self.w
        w._dvr._factory = self.forbidden('DVR read in demo')
        self.enter('DVR')
        self.click(self.hits('row')[('job-2',)])
        self.click(231)
        self.assertEqual(w.getFocusId(), 231)
        self.action(DOWN)
        self.assertEqual(w.getFocusId(), CONTENT)
        self.action(UP)
        self.assertEqual(w.getFocusId(), 231)
        self.dialog.textviewer.side_effect = lambda *_: self.post_action(NAV_BACK)
        self.click(self.hits('row')[('demo-pass-2',)])
        self.assertEqual(w.view, 'DVR')
        self.assertEqual(w._dvr.selected().key, 'demo-pass-2')
        body = self.dialog.textviewer.call_args.args[1]
        self.assertIn(w.t('dvr_pass_readonly'), body)
        self.assertIn(w.t('unknown'), body)
        self.assertIsNone(command_label(w, 'dvr.manage'))
        self.assertIsNone(command_label(w, 'dvr.status'))
        w.data_session.choose('partial')
        w.render()
        self.assertEqual(w._current_data().state, DataState.PARTIAL)
        self.assertTrue(w._current_data().stale)
        self.assertEqual(w._dvr.selected().key, 'demo-pass-2')
        w.data_session.choose('healthy')
        self.click(230)
        self.assertEqual(w._dvr_selected, 'job-2')
        self.click(231)
        self.enter('Settings')
        self.click(self.hits('row')[('connection.settings',)])
        self.action(NAV_BACK)
        self.assertEqual(w.view, 'Dashboard')
        self.enter('DVR')
        self.assertEqual((w._dvr.tab, w._dvr.selected().key), ('dvr.passes', 'demo-pass-2'))
        w._dvr._factory.assert_not_called()
        w._recording_ui.request.assert_not_called()

    def connected_pass_fixture(self):
        self.seed()
        w = self.w
        w.transient_demo_mode = False
        config = SimpleNamespace(namespace='fake-dvr-source')
        w._development_config = config
        w._live_session = SimpleNamespace(jobs=w.data_session.jobs, incarnation=object(), _generation=1,
                                         _catalog_epoch=1, loading=False)
        w._dashboard_ui.poll = Mock(return_value=False)
        w._recording_ui._current_config = Mock(return_value=config)
        workers = []

        def factory(binding, profile):
            self.assertIs(binding, config)
            self.assertIsNone(profile)
            worker = SimpleNamespace(close=Mock(), poll=Mock(return_value=None), submit=Mock(return_value=True),
                                     finished=True)
            workers.append(worker)
            return worker

        w._dvr._factory = factory
        rows = rule_rows([dict(ID='test-series', Paused=False, NumJobs=0, Priority=0, UpdatedAt=1,
                               **GeneralSeriesPlan('SH00000001', 'Fake Series').body()),
                          dict(ID='test-advanced', Name='Fake advanced')])
        self.enter('DVR')
        self.click(231)
        return workers, rows

    def pass_result(self, worker, rows):
        worker.poll.return_value = ManagerView('browse', rows=rows, observed_at=self.w.utc_now())
        self.w.process_pending()

    def test_late_pass_reads_do_not_publish_or_replay_and_refresh_restores_exact_id(self):
        workers, rows = self.connected_pass_fixture()
        w = self.w
        w._series_manager = self.forbidden('Series control')
        self.assertTrue(w._current_data().loading)
        self.click(306)  # A second refresh while the same read is active coalesces.
        self.assertEqual(len(workers), 1)
        workers[0].finished = False
        self.click(230)
        workers[0].close.assert_called_once()
        workers[0].poll.return_value = ManagerView('browse', rows=(rows[0],))
        self.click(231)
        self.assertTrue(w._current_data().loading)
        self.assertEqual(self.hits('row'), {})
        self.assertEqual(len(workers), 1)
        workers[0].finished = True
        w.process_pending()
        self.pass_result(workers[1], rows)
        self.click(self.hits('row')[('test-advanced',)])
        self.assertIsNone(command_label(w, 'dvr.manage'))
        self.click(306)
        self.pass_result(workers[2], tuple(reversed(rows)))
        self.assertEqual((w._dvr.selected().key, w._dvr.position), ('test-advanced', 0))
        self.click(306)
        self.pass_result(workers[3], rows[:1])
        self.assertEqual((w._dvr.selected().key, w._dvr.position), ('test-series', 0))
        stale = w._chrome.capture(303)
        self.click(230)
        w._dispatcher.post(('chrome', stale))
        w.process_pending()
        w._series_manager.assert_not_called()
        for worker in workers:
            worker.submit.assert_called_once_with('browse')

    def test_series_entry_routes_exact_selected_row_and_explicit_status_only(self):
        workers, rows = self.connected_pass_fixture()
        w = self.w
        manager = Mock()
        w._series_manager = Mock(return_value=manager)
        self.pass_result(workers[0], rows)
        self.click(CONTENT)
        manager.request_dvr.assert_not_called()
        self.assertEqual(w.controls[303].properties['setLabel'], (w.t('dvr_manage'),))
        self.assertEqual(w.controls[306].properties['setLabel'], (w.t('refresh'),))
        previous_right = 0
        for ident in (300, 303, 306, 304, 305):
            props = w.controls[ident].properties
            x, y = props['setPosition']
            right = (x + props['setWidth'][0]) / 3
            self.assertLess(previous_right, x/3)
            self.assertLessEqual(right, 640)
            self.assertLessEqual((y+50)/3, 360)
            previous_right = right
        w._dvr.notice = 'dvr_series_verified'
        self.click(303)
        subject, current, finish = manager.request_dvr.call_args.args
        self.assertIs(subject, rows[0])
        self.assertTrue(current())
        self.assertIsNone(w._dvr.notice)
        self.click(self.hits('row')[('test-advanced',)])
        self.assertFalse(current())
        self.assertTrue(manager.request_dvr.call_args.kwargs['source_current']())
        finish(ManagerView('rename', error='pending'))
        w.process_pending()
        self.assertEqual(w._dvr.notice, 'dvr_series_pending')
        self.assertEqual(w.controls[115].properties['setLabel'], (w.t('dvr_series_pending'),))
        self.click(self.hits('row')[('test-series',)])
        self.assertIsNone(command_label(w, 'dvr.manage'))
        self.dialog.select.side_effect = lambda _, labels: labels.index(w.t('dvr_series_status'))
        self.click(304)
        subject, status_current, _ = manager.request_dvr.call_args.args
        self.assertIsNone(subject)
        self.assertTrue(status_current())
        self.click(230)
        self.assertFalse(current())
        self.assertFalse(status_current())
        self.assertIsNone(command_label(w, 'recording.status'))

    def test_confirmed_series_success_refreshes_without_moving_the_other_selection(self):
        workers, rows = self.connected_pass_fixture()
        w = self.w
        manager = Mock()
        w._series_manager = Mock(return_value=manager)
        self.pass_result(workers[0], rows)
        self.click(303)
        _, review_current, finish = manager.request_dvr.call_args.args
        self.click(self.hits('row')[('test-advanced',)])
        self.assertFalse(review_current())
        finish(ManagerView('pause'))
        w.process_pending()
        self.assertTrue(w._current_data().loading)
        workers[1].submit.assert_called_once_with('browse')
        paused = rule_rows([dict(rows[0].payload(), Paused=True)])[0]
        self.pass_result(workers[1], (paused, rows[1]))
        self.assertEqual(w._dvr.selected().key, 'test-advanced')
        self.assertIs(w._current_data().items[0].payload()['Paused'], True)
        self.assertEqual(w.controls[115].properties['setLabel'], (w.t('dvr_series_verified'),))

    def test_pass_read_states_never_offer_controls_from_unsupported_or_old_data(self):
        workers, rows = self.connected_pass_fixture()
        w = self.w
        for index, (error, state) in enumerate((('network', DataState.OUTAGE), ('source', DataState.UNSUPPORTED),
                                               ('schema', DataState.MALFORMED), (None, DataState.EMPTY))):
            workers[index].poll.return_value = ManagerView('browse', error=error, observed_at=w.utc_now())
            w.process_pending()
            self.assertEqual(w._current_data().state, state)
            self.assertFalse(self.hits('row'))
            self.assertIsNone(w._chrome.capture(303))
            self.click(306)
            self.assertTrue(w._current_data().loading)
        self.pass_result(workers[4], rows)
        stamp = w._current_data().updated_at
        self.assertIsNotNone(w._chrome.capture(303))
        w.focus_writes.clear()
        w.utc_now = lambda: stamp + 61
        w.process_pending()
        self.assertTrue(w._current_data().stale)
        self.assertTrue(self.hits('row'))
        self.assertIsNone(w._chrome.capture(303))
        self.assertEqual(w.focus_writes, [])

    def test_guide_more_never_offers_series_when_disconnected_or_in_demo(self):
        w = self.w
        w._series_manager_request = Mock()
        for demo in (False, True):
            with self.subTest(demo=demo):
                self.enter('Dashboard')
                if demo:
                    self.seed()
                self.enter('Guide')
                self.dialog.select.reset_mock()
                self.click(304)
                self.dialog.select.assert_called_once()
                self.assertNotIn(w.t('series_manager'), self.dialog.select.call_args.args[1])
                self.assertFalse(execute_command(w, self.gui, 'series.manager'))
        w._series_manager_request.assert_not_called()


class HubBookmarkTests(unittest.TestCase):
    def setUp(self):
        self.a = BookmarkScope.from_config(DevelopmentConfig.from_address('192.0.2.10'))
        self.b = BookmarkScope.from_config(DevelopmentConfig.from_address('192.0.2.11'))
        self.channel = ChannelKey(self.a.namespace, 'antenna', '001')
        self.saved = HubBookmarks(
            self.a, guide_group='Favorites', guide_channel=self.channel,
            recordings_category='All', recordings_filter='dvr', recordings_sort='oldest',
            recording_selection=(self.a.namespace, 'dvr', '001'),
            dvr_tab='passes', recent_terms=('Café', 'Weather'), last_view='Recordings',
        )
        self.raw = encode_bookmarks(self.saved)

    def test_round_trip_is_closed_and_does_not_include_server_address(self):
        root = json.loads(self.raw)
        self.assertEqual(set(root), {
            'version', 'source', 'guide_group', 'guide_channel', 'recordings_category',
            'recordings_filter', 'recordings_sort', 'recording_selection', 'dvr_tab',
            'recent_terms', 'last_view',
        })
        self.assertEqual(decode_bookmarks(self.a, self.raw), self.saved)
        self.assertNotIn(b'192.0.2.', self.raw)
        root.update(guide_time=123, programme={'title': 'not navigation'},
                    draft={'term': 'never keep'}, url='http://example.invalid/', password='not a bookmark')
        projected = decode_bookmarks(self.a, json.dumps(root).encode())
        assert projected is not None
        self.assertEqual(encode_bookmarks(projected), self.raw)

    def test_canonical_reconnect_and_distinct_servers_with_same_metadata_namespace(self):
        self.assertEqual(self.a.namespace, self.b.namespace)
        self.assertNotEqual(self.a.server_key, self.b.server_key)
        alias = DevelopmentConfig.from_address(' HTTP://192.0.2.10:8089/ ')
        self.assertEqual(BookmarkScope.from_config(alias), self.a)
        self.assertEqual(BookmarkScope.from_config(replace(alias, expected_version='test-build')), self.a)
        self.assertNotEqual(BookmarkScope.from_config(replace(alias, namespace='different')), self.a)
        self.assertIsNone(decode_bookmarks(self.b, self.raw))
        saved_b = HubBookmarks(self.b, last_view='DVR')
        raw_b = encode_bookmarks(saved_b)
        self.assertEqual(decode_bookmarks(self.b, raw_b), saved_b)
        self.assertIsNone(decode_bookmarks(self.a, raw_b))
        self.assertEqual(decode_bookmarks(BookmarkScope.from_config(alias), self.raw), self.saved)

    def test_missing_corrupt_and_unsupported_envelopes_are_not_snapshots(self):
        root = json.loads(self.raw)
        invalid = [None, b'', self.raw[:-1], b'\xff', b'[]', b'{}',
                   b'[' * 2000 + b']' * 2000, b' ' * (MAX_BOOKMARK_BYTES + 1),
                   self.raw.replace(b'"version":1', b'"version":1,"version":1'),
                   self.raw.replace(b'"version":1', b'"version":NaN')]
        for version in (0, 2, True, '1', 1.0):
            invalid.append(json.dumps(dict(root, version=version)).encode())
        for raw in invalid:
            with self.subTest(size=len(raw) if raw else 0):
                self.assertIsNone(decode_bookmarks(self.a, raw))

    def test_invalid_fields_drop_without_rebinding_selection_or_truncating_identity(self):
        root = json.loads(self.raw)
        root.update(guide_group={}, guide_channel=['antenna', '001' + 'x' * 128],
                    recordings_category='unknown', recordings_filter='cable',
                    recordings_sort=['title'], dvr_tab='https:example.invalid',
                    recent_terms=[{}, 'https://example.invalid/', 'one\u0000two', 'data:text/plain,x'], last_view='Search')
        self.assertEqual(decode_bookmarks(self.a, json.dumps(root).encode()), HubBookmarks(self.a))
        self.assertIsNone(replace(self.saved, recordings_filter='import').recording_selection)
        self.assertIsNone(replace(self.saved, recordings_category='Search').recording_selection)
        self.assertIsNone(replace(self.saved, recording_selection=('other', 'dvr', '001')).recording_selection)
        self.assertIsNone(replace(self.saved, guide_channel=ChannelKey('other', 'antenna', '001')).guide_channel)

    def test_recent_terms_are_bounded_cleaned_and_deduplicated_newest_first(self):
        candidate_terms = ('  Café  ', 'ＣＡＦÉ', '', 'x' * 129, 'file:///private',
                           *(str(i) for i in range(100)))
        model = replace(self.saved, recent_terms=candidate_terms)
        self.assertEqual(model.recent_terms, ('Café', *(str(i) for i in range(MAX_RECENT_TERMS - 1))))
        self.assertEqual(decode_bookmarks(self.a, encode_bookmarks(model)), model)
        largest = replace(self.saved, recent_terms=tuple('😀' * 127 + str(i) for i in range(MAX_RECENT_TERMS)))
        raw = encode_bookmarks(largest)
        self.assertLessEqual(len(raw), MAX_BOOKMARK_BYTES)
        self.assertEqual(decode_bookmarks(self.a, raw), largest)

    def test_guide_restore_uses_full_channel_key_and_current_group_membership(self):
        other = ChannelKey(self.a.namespace, 'cable', '001')
        groups = {'All': (other, self.channel), 'Favorites': (other, self.channel)}
        self.assertEqual(self.saved.guide_target(self.a, groups), ('Favorites', self.channel))
        self.assertEqual(self.saved.guide_target(self.a, {'All': (other,)}), ('All', other))
        self.assertEqual(self.saved.guide_target(self.a, {'Favorites': (other,)}), ('Favorites', other))
        self.assertEqual(self.saved.guide_target(self.b, groups), ('All', other))
        self.assertEqual(self.saved.guide_target(self.a, {}), (None, None))

    def test_dvr_restore_uses_available_stable_tab_keys(self):
        self.assertEqual(self.saved.dvr_target(self.a, ('scheduled', 'passes')), 'passes')
        self.assertEqual(self.saved.dvr_target(self.a, ('scheduled',)), 'scheduled')
        self.assertEqual(self.saved.dvr_target(self.b, ('scheduled', 'passes')), 'scheduled')
        self.assertIsNone(self.saved.dvr_target(self.a, ()))

    def test_recording_restore_matches_only_current_unambiguous_qualified_leaf(self):
        recording = Recording('001', 'Example', None, None, None, False, 100, 1800,
                              namespace=self.a.namespace, provenance='dvr')
        browser = LibraryBrowser((replace(recording, provenance='import'), recording))
        browser.set_mode('All')
        browser.set_provenance_filter('dvr')
        row_ids = tuple(row.id for row in browser.rows())
        self.assertEqual(self.saved.matching_recording_row(self.a, row_ids), repr(self.saved.recording_selection))
        self.assertIsNone(self.saved.matching_recording_row(self.b, row_ids))
        self.assertIsNone(self.saved.matching_recording_row(self.a, row_ids * 2))
        browser.set_provenance_filter('import')
        self.assertIsNone(self.saved.matching_recording_row(self.a, (row.id for row in browser.rows())))

    def test_startup_defaults_and_last_use_only_current_admitted_server(self):
        choose = partial(startup_destination, admission=StartupAdmission.ADMITTED, intent_current=True)
        for view in ('Dashboard', 'Guide', 'Recordings', 'DVR'):
            self.assertEqual(choose(view), view)
        for invalid in (None, '', 3, True, 'Scheduled', 'Search', 'Settings', 'guide', []):
            self.assertEqual(choose(invalid), 'Dashboard')
        self.assertEqual(choose(), 'Dashboard')
        self.assertEqual(choose('Last', source=self.a, bookmarks=self.saved), 'Recordings')
        self.assertEqual(choose('Last', source=self.b, bookmarks=self.saved), 'Dashboard')
        self.assertEqual(choose('Last', source=self.a), 'Dashboard')
        for preference in ('Guide', 'Last'):
            self.assertEqual(startup_destination(preference, admission=StartupAdmission.UNAVAILABLE, intent_current=True,
                                                 source=self.a, bookmarks=self.saved), 'Dashboard')

    def test_pending_and_superseded_launch_intent_do_not_select_automatic_route(self):
        self.assertIsNone(startup_destination('Guide', admission=StartupAdmission.PENDING, intent_current=True))
        for admission in StartupAdmission:
            self.assertIsNone(startup_destination('Guide', admission=admission, intent_current=False))
            self.assertIsNone(startup_destination('Guide', admission=admission, intent_current=True, launch_override=True))


if __name__ == '__main__':
    unittest.main()
