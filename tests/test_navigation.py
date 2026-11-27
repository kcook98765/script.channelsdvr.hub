"""Phase 1 owner/route/pointer regressions using inert WindowXML controls only."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from channels_dvr.guide import GuideController
from channels_dvr.dvr_ui import TABS
from channels_dvr.library import LibraryBrowser
from channels_dvr.normalization import DataState
from channels_dvr.repository import MockRepository
from channels_dvr.search import airing_key
from channels_dvr.series_manager_models import GeneralSeriesPlan, ManagerView, rule_rows
from channels_dvr.session import OfflineDataSession
from channels_dvr.ui_chrome import ACTIONS, CONTENT, CONTEXT, DOWN, INFO, NAV, NAV_BACK, PAGE_DOWN, RIGHT, UP
from channels_dvr.ui_navigation import command_label, execute_command
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
        ids = (100, 110, *range(112, 118), *range(120, 126), *range(140, 144), *NAV, *TABS, *ACTIONS, *range(400, 406))
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

    def window(self, start='Dashboard'):
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
            for arguments, view in (([], 'Dashboard'), (['Scheduled'], 'DVR'), (['Guide'], 'Guide'),
                                    (['Recordings'], 'Recordings'), (['Search'], 'Search'), (['Guide', 'bad'], 'Dashboard')):
                with patch.object(sys, 'argv', ['default.py', *arguments]):
                    runpy.run_path(str(ADDON / 'default.py'), run_name='__main__')
                launch.assert_called_with(str(ADDON), addon, start_view=view)

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
        # Successful publication can replace the controller even without a
        # source switch. The bookmark must not depend on keeping it alive.
        w.guide = GuideController(repo, strings=w.strings)
        self.assertTrue(w.guide.focus_event(result))
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
        self.assertEqual(w._dvr.tab, 'dvr.scheduled')
        self.action(RIGHT)
        self.assertEqual((w.getFocusId(), w._dvr.tab), (231, 'dvr.scheduled'))
        self.click(231)
        self.click(303)
        w._series_manager.assert_not_called()
        self.assertEqual(w._dvr.tab, 'dvr.passes')
        self.assertFalse(w._current_data().can_browse)
        self.enter('Settings')
        w._addon.openSettings.assert_not_called()
        self.click(self.hits('row')[('connection.settings',)])
        w._addon.openSettings.assert_called_once()
        w._addon.setSettingBool.assert_not_called()

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
        self.click(303)
        subject, current, finish = manager.request_dvr.call_args.args
        self.assertIs(subject, rows[0])
        self.assertTrue(current())
        finish(ManagerView('rename', error='pending'))
        w.process_pending()
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


if __name__ == '__main__':
    unittest.main()
