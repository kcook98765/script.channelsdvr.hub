"""Dashboard contracts with explicit controls, clocks and transport; never Kodi/DVR."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from threading import Event, get_ident
import io
import json
import sys
import xml.etree.ElementTree as ET
import unittest

from channels_dvr.dashboard_http import DashboardResult, DashboardStatusReadClient
from channels_dvr.dashboard_models import DashboardState, ResourceSummary, parse_resource
from channels_dvr.dashboard_ui import DashboardUI, format_timestamp, refresh_seconds
from channels_dvr.dashboard_worker import DashboardWorker
from channels_dvr.build_status_ui import current as bound_status
from channels_dvr.live_loader import DevelopmentConfig, LoadResult, MetadataSubmission, StatusObservation
from channels_dvr.metadata_http import ChannelsMetadataClient, MetadataError, status_sample
from channels_dvr.metadata_models import MetadataBatch
from channels_dvr.metadata_repository import DevelopmentSnapshot
from channels_dvr.live_session import LiveDataSession
from channels_dvr.normalization import DataResult, DataState
from channels_dvr.ui_chrome import WindowChrome
from channels_dvr.ui_dispatch import UiDispatcher
from channels_dvr.ui_navigation import execute_command
from channels_dvr.ui_routes import Route, back
from channels_dvr.ui_text import FALLBACK, IDS, capture, text
from channels_dvr.guide_render import RenderMemo
from channels_dvr.metrics import GuiMetrics
from channels_dvr.session import Presentation


VERSION = '2026.08.07.0346'
CONFIG = DevelopmentConfig('http://192.0.2.10:8089')


def result(at=1000, files=2, **changes):
    resources = (
        ('server', (('version', VERSION), ('os', 'Linux'), ('arch', 'x86_64'), ('running', 60))),
        ('dvr', tuple(dict(files=files, total=2, jobs=0, rules=0, groups=0,
                          storage=0, enabled=False, busy=False).items())),
        ('sources', tuple(dict(sources=0, tuners=0, reporting=0, unknown=0).items())),
        ('passes', tuple(dict(total=0, paused=0, active=0, unknown=0).items())),
        ('activity', tuple(dict(streams=0, recordings=0, workers=0, other=0).items())),
    )
    return DashboardResult(**dict(dict(version=VERSION, fetched_at=at, resources=resources,
                                       error=False, coherent=True), **changes))


class Worker:
    def __init__(self):
        self.requests = []
        self.result = None
        self.cancels = 0
        self.closed = False

    def request(self, config):
        token = object()
        self.requests.append((token, config))
        return token

    def poll(self):
        item, self.result = self.result, None
        return item

    def cancel(self):
        self.cancels += 1
        self.result = None

    def close(self):
        self.closed = True


class Control:
    def __init__(self):
        self.calls = []

    def __getattr__(self, method):
        if not method.startswith('set'):
            raise AttributeError(method)
        return lambda *args: self.calls.append((method, args))


class Window:
    def __init__(self):
        self.view = 'Dashboard'
        self._closed = False
        self._development_blocked = None
        self._development_config = CONFIG
        self._guide_runtime = SimpleNamespace(_mode=None, incarnation=object(), latest_status=None)
        self._live_session = SimpleNamespace(_generation=1, actual_version=VERSION,
            _version_blocked=False, summaries={}, incarnation=object(),
            summary_health=Mock(return_value=(False, False)))
        self.settings = {'development_metadata_enabled': True,
                         'dashboard_refresh_minutes': '5'}
        self.setting_reads = []
        self.saved = CONFIG
        self.controls = {i: Control() for i in (*range(200, 206), *range(300, 311), *range(400, 406))}
        self.labels = {}
        self.focus = 201
        self.focus_calls = []
        self.selection = object()
        self._dispatcher = UiDispatcher()
        self._playback_ui = SimpleNamespace(cancel=Mock(), selection=Mock(return_value=None))
        self._guide_runtime.invalidate_view = Mock()
        self._chrome = WindowChrome(self)
        self._dashboard_ui = DashboardUI(self, Worker())

    @property
    def view(self):
        return self._route.view

    @view.setter
    def view(self, value):
        self._route = Route(value)

    def get_setting(self, key):
        self.setting_reads.append(key)
        return self.settings.get(key, '')

    def developer_enabled(self):
        return False

    def _saved_config(self):
        return self.saved

    def t(self, key, **values):
        return text(FALLBACK, key, **values)

    def _ensure_pool(self, count):
        pass

    def _label(self, ident, value):
        self.labels[ident] = value

    def getControl(self, ident):
        return self.controls[ident]

    def getFocusId(self):
        return self.focus

    def setFocusId(self, ident):
        self.focus_calls.append(ident)
        self.focus = ident

    def render(self):
        self._dashboard_ui.render()
        self._chrome.render()


class DashboardFixture(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.wall = 1000.0
        self.mono_patch = patch('channels_dvr.dashboard_ui.monotonic', lambda: self.now)
        self.mono_patch.start()
        self.addCleanup(self.mono_patch.stop)
        self.w = Window()
        self.ui = self.w._dashboard_ui
        self.ui.poll()
        self.ui.worker.result = (self.ui.token, result())
        self.ui.poll()
        self.w.render()

    def complete(self, item=None):
        self.ui.worker.result = (self.ui.token, item or result(at=self.wall))
        return self.ui.poll()


class DashboardRenderTests(DashboardFixture):
    def test_idle_second_does_not_force_repaint(self):
        self.now += 2
        self.wall += 2
        self.assertFalse(self.ui.poll(), 'Idle second must not invalidate Dashboard')

    def test_twenty_idle_polls_do_not_format_translate_or_build_cards(self):
        with patch.object(self.w, 't', wraps=self.w.t) as translate, \
                patch.object(self.ui, 'timestamp', wraps=self.ui.timestamp) as timestamp, \
                patch.object(self.ui, 'cards', wraps=self.ui.cards) as cards:
            for _ in range(20):
                self.now += 0.05
                self.assertFalse(self.ui.poll())
            translate.assert_not_called()
            timestamp.assert_not_called()
            cards.assert_not_called()

    def test_changed_values_update_in_place_without_focus_or_selection_change(self):
        for control in self.w.controls.values():
            control.calls.clear()
        selection = self.w.selection
        activation = self.w._chrome.capture(201)
        self.ui.refresh()
        self.ui.poll()
        self.assertTrue(self.complete(result(at=1001, files=3)))
        self.w.render()
        calls = self.w.controls[401].calls
        self.assertTrue(any(method == 'setText' for method, _ in calls))
        self.assertFalse(any(method == 'setVisible' for method, _ in calls), calls)
        self.assertEqual(self.w.focus_calls, [])
        self.assertIs(self.w.selection, selection)
        self.assertTrue(activation.matches(self.w._chrome.capture(201)))

    def test_retrieval_retains_all_cards_and_last_success(self):
        cards = self.ui.cards()
        line = self.ui.status_line()
        self.ui.refresh()
        self.assertFalse(self.ui.poll())
        self.assertEqual(self.ui.cards(), cards)
        self.assertEqual(self.ui.state.last_success, 1000)
        self.assertEqual(line, self.ui.status_line())
        self.assertEqual('', self.ui.warning_line())

    def test_partial_cycle_and_failure_do_not_advance_last_success(self):
        old = self.ui.state.values['dvr']
        partial = result(at=1100, resources=tuple((n, None if n == 'dvr' else v)
                                                for n, v in result().resources), error=True)
        self.ui.refresh()
        self.ui.poll()
        self.assertTrue(self.complete(partial))
        self.assertIn('partial, failed or stale', self.ui.warning_line())
        self.assertEqual(self.ui.state.last_success, 1000)
        self.assertEqual(self.ui.state.values['dvr'].fetched_at, old.fetched_at)
        self.assertNotIn('retained or unavailable', self.ui.cards()[1])
        self.ui.refresh()
        self.ui.poll()
        self.complete(result(at=1200, coherent=False, error=True))
        self.assertIn('partial, failed or stale', self.ui.warning_line())
        self.assertEqual(self.ui.state.last_success, 1000)
        self.ui.refresh()
        self.ui.poll()
        self.complete(result(at=1300))
        self.assertEqual(self.ui.state.last_success, 1300)
        self.assertEqual('', self.ui.warning_line())

    def test_unknown_is_not_zero_or_false_and_fresh_missing_fields_are_not_failures(self):
        self.assertIn('Jobs: 0', self.ui.cards()[1])
        self.assertIn('Enabled: No', self.ui.cards()[1])
        fields = tuple((k, None if k in ('files', 'busy') else v)
                       for k, v in dict(result().resources)['dvr'])
        self.ui.state = DashboardState()
        self.ui.state.merge(result(resources=tuple((n, fields if n == 'dvr' else v)
                                                  for n, v in result().resources)))
        self.assertIn('Files: Unknown', self.ui.cards()[1])
        self.assertIn('Busy: Unknown', self.ui.cards()[1])
        self.assertEqual('', self.ui.warning_line())
        self.assertEqual(self.ui.state.last_success, 1000)
        self.assertFalse(self.ui.state.values['dvr'].unavailable)

    def test_listing_publication_is_prompt_independent_and_has_no_age_labels(self):
        summary = ResourceSummary(CONFIG.namespace, VERSION, (1000, 2000), 1000, 0, ())
        self.w._live_session.summaries['recordings'] = summary
        self.assertTrue(self.ui.poll())
        self.assertIn('Independently loaded listings (cache)', self.ui.cards()[4])
        self.assertIn('Recordings (complete scan): 0', self.ui.cards()[4])
        self.ui.state.merge(result(coherent=False, error=True))
        self.assertIn('Recordings (complete scan): 0', self.ui.cards()[4])
        self.assertNotIn('stale listing cache', self.ui.cards()[4])
        self.w._live_session.summary_health.return_value = (True, True)
        self.ui.poll()
        self.assertNotIn('stale listing cache', self.ui.cards()[4])
        self.assertNotIn('read failed', self.ui.cards()[4])
        self.assertIn('partial, failed or stale', self.ui.warning_line())
        self.assertNotIn('ago', '\n'.join(self.ui.cards()))
        self.assertNotIn('Last refreshed', '\n'.join(self.ui.cards()))
        self.assertEqual(self.ui.state.last_success, 1000)

    def test_listing_scope_rejects_wrong_namespace_version_and_blocked_session(self):
        good = ResourceSummary(CONFIG.namespace, VERSION, (1000, 2000), 1000, 123, ())
        for summary in (replace(good, namespace='other'), replace(good, version='2026.08.08.0000')):
            self.w._live_session.summaries['channels'] = summary
            self.assertNotIn('123', self.ui.cards()[4])
        self.w._live_session.summaries['channels'] = good
        self.assertIn('123', self.ui.cards()[4])
        self.w._live_session._version_blocked = True
        self.assertNotIn('123', self.ui.cards()[4])

    def test_absolute_local_timestamp_uses_localized_line(self):
        local = datetime(2026, 11, 25, 17, 23, 45, tzinfo=timezone(timedelta(hours=-7)))
        with patch('channels_dvr.dashboard_ui.datetime') as dt:
            dt.fromtimestamp.return_value.astimezone.return_value = local
            line = self.ui.status_line()
            self.assertIn(local.strftime('%Y-%m-%d %H:%M:%S %z'), line)
            dt.fromtimestamp.assert_called_once_with(1000)
            dt.fromtimestamp.return_value.astimezone.assert_called_once_with()
        translated = capture(lambda ident: 'Local success: {timestamp}'
                             if ident == IDS['dashboard_updated'] else '')
        with patch.object(self.w, 't', lambda key, **values: text(translated, key, **values)):
            self.assertIn('Local success:', self.ui.status_line())

    def test_stale_transition_invalidates_once_even_with_auto_disabled(self):
        self.w.settings['dashboard_refresh_minutes'] = '0'
        self.ui.poll()
        self.now += 300
        self.assertTrue(self.ui.poll())
        self.assertIn('partial, failed or stale', self.ui.warning_line())
        self.assertFalse(self.ui.poll())
        self.assertEqual(len(self.ui.worker.requests), 1)

    def test_wall_clock_corrections_do_not_move_monotonic_stale_threshold(self):
        self.w.settings['dashboard_refresh_minutes'] = '0'
        self.ui.poll()
        self.ui.state.last_success = 9999999999
        self.now += 299
        self.assertFalse(self.ui.poll())
        self.now += 1
        self.assertTrue(self.ui.poll())
        self.assertIn('partial, failed or stale', self.ui.warning_line())

    def test_direct_render_revokes_source_timestamp_before_next_poll(self):
        old_line = self.ui.status_line()
        self.assertNotIn('Never', old_line)
        self.w._live_session = SimpleNamespace(_generation=1, actual_version=VERSION,
            _version_blocked=False, summaries={}, incarnation=object())
        self.w.render()
        self.assertIn('Never', self.w.labels[116])
        self.assertEqual(self.w.labels[112], self.w.t('connection_prompt'))
        self.assertIn('Files: Unknown', self.ui.cards()[1])

    def test_unsupported_and_failed_status_is_not_duplicated_in_112(self):
        for item in (result(unsupported=True, coherent=False, error=True,
                            version='2020.01.01.0000'),
                     result(coherent=False, error=True)):
            self.ui.state.merge(item)
            self.w.render()
            self.assertEqual(self.w.labels[112], '')
            self.assertNotIn('Complete', self.w.labels[116])

    def test_text_failure_is_fail_closed_and_retried_without_stale_text(self):
        control = self.w.controls[401]
        self.ui.state.merge(result(files=3))
        with patch.object(control, 'setText', side_effect=RuntimeError('fake SDK')):
            self.w.render()
            self.assertNotIn(401, self.ui.ready_cards)
            self.assertIn(('setVisible', (False,)), control.calls)
        self.w.render()
        self.assertIn(401, self.ui.ready_cards)
        self.assertEqual(control.calls[-1], ('setVisible', (True,)))

    def test_chrome_reinitialization_rebinds_controls(self):
        self.w.controls[401] = Control()
        self.w._chrome.reset()
        self.w.render()
        self.assertTrue(any(m == 'setText' for m, _ in self.w.controls[401].calls))
        self.assertEqual(self.w.focus_calls, [])


class DashboardScheduleTests(DashboardFixture):
    def test_default_and_unchanged_setting_reads_do_not_renew_deadline(self):
        for elapsed in (1, 30, 150, 299):
            self.now = 10 + elapsed
            self.ui.poll()
            self.assertEqual(len(self.ui.worker.requests), 1)
            self.assertEqual(self.ui.completed, 10)
        self.now = 310
        self.assertFalse(self.ui.poll(), 'On-time automatic dispatch is not a display change')
        self.assertEqual(len(self.ui.worker.requests), 2)

    def test_every_allowed_interval_and_invalid_fallback(self):
        for setting, seconds in [('1', 60), ('2', 120), ('5', 300), ('10', 600),
                                 ('15', 900), ('30', 1800), ('bad', 300), ('', 300)]:
            with self.subTest(setting=setting):
                w = Window()
                ui = w._dashboard_ui
                w.settings['dashboard_refresh_minutes'] = setting
                self.now = 10
                ui.poll()
                ui.worker.result = (ui.token, result())
                ui.poll()
                self.now += seconds - 0.001
                ui.poll()
                self.assertEqual(len(ui.worker.requests), 1)
                self.now += 0.001
                ui.poll()
                self.assertEqual(len(ui.worker.requests), 2)

    def test_disabled_allows_initial_and_manual_but_no_periodic_read(self):
        w = Window()
        w.settings['dashboard_refresh_minutes'] = '0'
        ui = w._dashboard_ui
        ui.poll()
        self.assertIsNotNone(ui.token)
        ui.worker.result = (ui.token, result())
        ui.poll()
        self.now += 10000
        ui.poll()
        self.assertEqual(len(ui.worker.requests), 1)
        ui.refresh()
        ui.poll()
        self.assertEqual(len(ui.worker.requests), 2)

    def test_manual_clicks_coalesce_without_cancellation_or_overlap(self):
        self.ui.refresh()
        self.ui.poll()
        token = self.ui.token
        cancels = self.ui.worker.cancels
        for _ in range(20):
            self.ui.refresh()
            self.now += 100
            self.ui.poll()
        self.assertIs(self.ui.token, token)
        self.assertEqual(self.ui.worker.cancels, cancels)
        self.assertEqual(len(self.ui.worker.requests), 2)
        self.complete()
        self.assertIsNone(self.ui.token)
        self.assertEqual(self.ui.completed, self.now)

    def test_failure_retries_after_interval_not_every_poll(self):
        self.ui.refresh()
        self.ui.poll()
        self.complete(result(coherent=False, error=True))
        self.now += 299
        self.ui.poll()
        self.assertEqual(len(self.ui.worker.requests), 2)
        self.now += 1
        self.ui.poll()
        self.assertEqual(len(self.ui.worker.requests), 3)

    def test_interval_change_reuses_completion_and_disabled_enable_is_due(self):
        self.now += 120
        self.w.settings['dashboard_refresh_minutes'] = '0'
        self.ui.poll()
        self.assertEqual(self.ui.completed, 10)
        self.w.settings['dashboard_refresh_minutes'] = '2'
        self.ui.poll()
        self.assertEqual(len(self.ui.worker.requests), 2)

    def test_setting_read_error_uses_same_default_as_invalid_data(self):
        read = self.w.get_setting
        def broken(key):
            if key == 'dashboard_refresh_minutes':
                raise RuntimeError('unavailable')
            return read(key)
        with patch.object(self.w, 'get_setting', broken):
            self.now = 309
            self.ui.poll()
            self.assertEqual(len(self.ui.worker.requests), 1)
            self.now = 310
            self.ui.poll()
            self.assertEqual(len(self.ui.worker.requests), 2)

    def test_off_dashboard_no_setting_reads_and_back_respects_elapsed_time(self):
        for return_via_nav in (False, True):
            with self.subTest(return_via_nav=return_via_nav):
                self.now = 20
                self.w.view = 'Guide'
                self.w.setting_reads.clear()
                self.ui.poll()
                self.assertEqual(self.w.setting_reads, [])
                with patch.object(self.w, 'render'):
                    if return_via_nav:
                        self.w.focus = 201
                        self.w._chrome.action(92)
                    else:
                        back(self.w)
                self.assertEqual(self.w.view, 'Dashboard')
                self.ui.poll()
                self.assertEqual(len(self.ui.worker.requests), 1)
        self.w.view = 'Guide'
        self.now = 310
        self.ui.poll()
        with patch.object(self.w, 'render'):
            back(self.w)
        self.ui.poll()
        self.assertEqual(len(self.ui.worker.requests), 2)

    def test_selecting_already_active_dashboard_keeps_request_and_cadence(self):
        with patch.object(self.w, 'render'):
            execute_command(self.w, None, 'view.dashboard')
        self.assertEqual(self.ui.completed, 10)
        cancels = self.ui.worker.cancels
        self.ui.refresh()
        self.ui.poll()
        token = self.ui.token
        with patch.object(self.w, 'render'):
            execute_command(self.w, None, 'view.dashboard')
        self.assertIs(self.ui.token, token)
        self.assertEqual(self.ui.worker.cancels, cancels)
        self.w._guide_runtime.invalidate_view.assert_not_called()

    def test_other_views_preserve_same_view_invalidation(self):
        self.w.view = 'Guide'
        with patch.object(self.w, 'render'):
            execute_command(self.w, None, 'view.guide')
        self.w._guide_runtime.invalidate_view.assert_called_once_with()

    def test_leaving_cancels_token_and_late_result_cannot_publish(self):
        self.ui.refresh()
        self.ui.poll()
        token = self.ui.token
        self.w.view = 'Guide'
        self.ui.poll()
        self.assertIsNone(self.ui.token)
        self.ui.worker.result = (token, result(files=999))
        self.w.view = 'Dashboard'
        self.ui.poll()
        self.assertNotIn('999', self.ui.cards()[1])

    def test_cancelled_initial_read_is_eligible_on_return_even_disabled(self):
        w = Window()
        w.settings['dashboard_refresh_minutes'] = '0'
        ui = w._dashboard_ui
        ui.poll()
        w.view = 'Guide'
        ui.poll()
        w.view = 'Dashboard'
        ui.poll()
        self.assertEqual(len(ui.worker.requests), 2)

    def test_source_and_opt_in_changes_revoke_values_and_old_tokens(self):
        for mutate in (
            lambda: setattr(self.w, '_live_session', SimpleNamespace(_generation=1,
                actual_version=VERSION, _version_blocked=False, summaries={}, incarnation=object())),
            lambda: setattr(self.w, 'saved', DevelopmentConfig('http://192.0.2.11:8089')),
            lambda: self.w.settings.update(development_metadata_enabled=False),
            lambda: setattr(self.w._guide_runtime, '_mode', ('no_connection', None)),
        ):
            with self.subTest(mutate=mutate):
                self.w = Window()
                self.ui = self.w._dashboard_ui
                self.ui.poll()
                self.complete()
                self.ui.refresh()
                self.ui.poll()
                token = self.ui.token
                mutate()
                self.ui.worker.result = (token, result(files=999))
                self.assertTrue(self.ui.poll())
                self.assertEqual(self.ui.state.values, {})
                self.assertIsNone(self.ui.state.last_success)
                self.assertNotIn('999', self.ui.cards()[1])

    def test_close_never_accepts_late_result_or_reads_settings(self):
        self.ui.refresh()
        self.ui.poll()
        self.ui.worker.result = (self.ui.token, result(files=999))
        self.w._closed = True
        self.ui.close()
        self.w.setting_reads.clear()
        self.assertFalse(self.ui.poll())
        self.assertEqual(self.w.setting_reads, [])
        self.assertTrue(self.ui.worker.closed)

    def test_disconnected_idle_is_not_reformatted_or_repainted(self):
        self.w.settings['development_metadata_enabled'] = False
        self.assertTrue(self.ui.poll())
        with patch.object(self.w, 't', wraps=self.w.t) as translate:
            for _ in range(20):
                self.assertFalse(self.ui.poll())
            translate.assert_not_called()


class DashboardStartupTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        for target in ('channels_dvr.dashboard_ui.monotonic', 'channels_dvr.build_status_ui.monotonic'):
            clock = patch(target, lambda: self.now)
            clock.start()
            self.addCleanup(clock.stop)

    def window(self, minutes='1'):
        w = Window()
        w.settings['dashboard_refresh_minutes'] = minutes
        w._live_session.actual_version = None
        return w, w._dashboard_ui

    def metadata(self, w, transition):
        if transition == 'corroboration':
            w._live_session.actual_version = VERSION
        elif transition == 'block':
            w._live_session._version_blocked = True
        elif transition == 'generation':
            w._live_session._generation += 1
        else:
            self.fail('Unknown fake metadata transition')

    def complete(self, ui, item=None):
        ui.worker.result = (ui.token, item or result())
        return ui.poll()

    def status(self, w, version=VERSION, *, observed_at=None):
        # Mint real sealed HTTP status evidence through an inert opener. Raw
        # session actual_version, a lookalike DTO or old receipt is not proof.
        client = ChannelsMetadataClient(CONFIG.origin, namespace=CONFIG.namespace,
            expected_version=VERSION, opener=SimpleNamespace(open=lambda *a, **k: Response({'version': version})))
        with patch('channels_dvr.metadata_http.time.monotonic',
                   lambda: self.now if observed_at is None else observed_at):
            try:
                client.verify_status()
            except MetadataError:
                pass  # A strict HTTP200 status sample can prove an unsupported build.
        sample = status_sample(client)
        self.assertIsNotNone(sample)
        w.settings['ShowDeveloperTools'] = False
        w.transient_demo_mode = False
        w._live_session.loading = False
        receipt = MetadataSubmission(1, w._live_session._generation, w._live_session.incarnation, object(), 0)
        w._current_status_receipt = receipt
        w._metadata_loader = SimpleNamespace(current_status_receipt=lambda r:
            r is w._current_status_receipt and r.owner is w._live_session.incarnation
            and r.metadata_generation == w._live_session._generation)
        runtime = w._guide_runtime
        runtime._request = None
        context = SimpleNamespace(saved_connect=True, saved_developer=False,
            session=w._live_session, config=w._development_config, view=w.view)
        runtime._same_context = lambda c: (c is context and c.session is w._live_session
            and c.config is w._development_config and c.view == w.view)
        observation = StatusObservation(CONFIG, receipt, 1000, 4, sample)
        runtime.latest_status = (observation, context)
        return observation, context

    def test_metadata_after_first_cycle_does_not_rearm_initial(self):
        for minutes, gap in (('1', 5.131), ('0', 0.734)):
            for transition in ('corroboration', 'block', 'generation'):
                with self.subTest(minutes=minutes, transition=transition):
                    self.now = 10.0
                    w, ui = self.window(minutes)
                    self.assertTrue(ui.poll())
                    self.complete(ui)
                    w.render()
                    state, completed, accepted = ui.state, ui.completed, ui.accepted_at
                    cancels = ui.worker.cancels
                    self.now += gap
                    self.metadata(w, transition)
                    ui.poll()
                    self.assertEqual(len(ui.worker.requests), 1, 'Metadata-only startup change caused duplicate cycle')
                    self.assertIs(ui.state, state)
                    self.assertEqual((ui.completed, ui.accepted_at), (completed, accepted))
                    self.assertEqual(ui.worker.cancels, cancels)
                    self.assertEqual(ui.state.last_success, 1000)
                    self.now = 69.999
                    ui.poll()
                    self.assertEqual(len(ui.worker.requests), 1)
                    self.now = 70.0 if minutes == '1' else 1000.0
                    ui.poll()
                    self.assertEqual(len(ui.worker.requests), 2 if minutes == '1' else 1)

    def test_metadata_before_initial_read_still_allows_immediate_stats(self):
        for minutes in ('1', '0'):
            for transition in ('corroboration', 'block', 'generation'):
                with self.subTest(minutes=minutes, transition=transition):
                    w, ui = self.window(minutes)
                    self.metadata(w, transition)
                    self.status(w)
                    ui.poll()
                    self.assertEqual(len(ui.worker.requests), 1)
                    self.complete(ui)
                    self.assertEqual(ui.state.last_success, 1000)
                    self.assertEqual(len(ui.worker.requests), 1)

    def test_actual_metadata_apply_corroboration_or_block_does_not_restart_stats(self):
        for minutes in ('1', '0'):
            for blocked in (False, True):
                with self.subTest(minutes=minutes, blocked=blocked):
                    self.now = 10
                    w, ui = self.window(minutes)
                    session = LiveDataSession(CONFIG.namespace, VERSION, clock=lambda: 1000)
                    session.begin(1, 1000, guide_windows=1)
                    w._live_session = session
                    ui.poll()
                    self.complete(ui)
                    state = ui.state
                    batch = MetadataBatch((), DataState.OUTAGE if blocked else DataState.EMPTY)
                    empty = MetadataBatch((), DataState.EMPTY)
                    snapshot = DevelopmentSnapshot(CONFIG.namespace, VERSION, 1000, (1000, 2800),
                                                   batch, batch, empty, empty)
                    self.now += 0.734 if minutes == '0' else 5.131
                    self.assertTrue(session.apply(LoadResult(1, 1000, 1, snapshot, None)))
                    self.assertEqual(session._version_blocked, blocked)
                    self.assertEqual(session.actual_version, None if blocked else VERSION)
                    ui.poll()
                    self.assertIs(ui.state, state)
                    self.assertEqual(ui.state.last_success, 1000)
                    self.assertEqual(ui.completed, 10)
                    self.assertEqual(len(ui.worker.requests), 1)

    def test_metadata_during_first_read_preserves_pending_or_ready_token(self):
        for minutes in ('1', '0'):
            for transition in ('corroboration', 'block', 'generation'):
                for ready in (False, True):
                    with self.subTest(minutes=minutes, transition=transition, ready=ready):
                        self.now = 10
                        w, ui = self.window(minutes)
                        ui.poll()
                        token, cancels = ui.token, ui.worker.cancels
                        self.now = 11
                        self.metadata(w, transition)
                        self.status(w)
                        if ready:
                            ui.worker.result = (token, result())
                        ui.poll()
                        self.assertEqual(len(ui.worker.requests), 1)
                        self.assertEqual(ui.worker.cancels, cancels)
                        if not ready:
                            self.assertIs(ui.token, token)
                            self.complete(ui)
                        self.assertEqual(ui.state.last_success, 1000)
                        self.assertEqual(ui.completed, 11)

    def test_failed_and_partial_initial_attempts_keep_cadence_through_listing_block(self):
        for minutes in ('1', '0'):
            for item in (result(coherent=False, error=True),
                         result(resources=(('server', dict(result().resources)['server']), ('dvr', None)), error=True)):
                with self.subTest(minutes=minutes, coherent=item.coherent):
                    self.now = 10
                    w, ui = self.window(minutes)
                    ui.poll()
                    self.complete(ui, item)
                    state = ui.state
                    self.now = 15
                    self.metadata(w, 'block')
                    self.status(w)
                    ui.poll()
                    self.assertIs(ui.state, state)
                    self.assertEqual(ui.completed, 10)
                    self.assertIsNone(ui.state.last_success)
                    self.assertEqual(len(ui.worker.requests), 1)

    def test_manual_during_and_after_initial_read_does_not_create_extra_requests(self):
        for minutes in ('1', '0'):
            with self.subTest(minutes=minutes):
                w, ui = self.window(minutes)
                ui.refresh()  # manual intent before the first owner poll
                ui.poll()
                token = ui.token
                self.metadata(w, 'block')
                self.status(w)
                for _ in range(3):
                    ui.refresh()
                    ui.poll()
                    self.assertIs(ui.token, token)
                self.complete(ui)
                ui.refresh()
                ui.poll()
                self.assertEqual(len(ui.worker.requests), 2)
                token = ui.token
                self.metadata(w, 'corroboration')
                ui.refresh()
                ui.poll()
                self.assertIs(ui.token, token)
                self.complete(ui, result(at=1001))
                self.assertEqual(len(ui.worker.requests), 2)
                self.assertEqual(ui.state.last_success, 1001)

    def test_listing_generation_block_and_cached_version_are_not_stats_authority(self):
        w, ui = self.window('0')
        ui.poll()
        self.complete(ui)
        state = ui.state
        for version, blocked in ((None, True), (VERSION, False), ('2026.08.08.0000', True)):
            w._live_session._generation += 1
            w._live_session.actual_version = version
            w._live_session._version_blocked = blocked
            ui.poll()
            self.assertIs(ui.state, state)
            self.assertEqual(len(ui.worker.requests), 1)
        self.assertEqual(ui.state.accepted_version, VERSION)

    def test_fresh_different_status_revokes_values_without_stale_listing_version_loop(self):
        w, ui = self.window('0')
        ui.poll()
        self.complete(ui)
        w._live_session.actual_version = VERSION
        self.now += 5
        newer = '2026.08.08.0000'
        self.status(w, newer)
        self.assertTrue(ui.poll())
        self.assertEqual(ui.state.values, {})
        self.assertIsNone(ui.state.last_success)
        self.assertEqual(len(ui.worker.requests), 2)
        self.complete(ui, result(version=newer, at=1100))
        self.assertEqual(ui.state.accepted_version, newer)
        for _ in range(20):
            self.assertFalse(ui.poll())
        self.assertEqual(len(ui.worker.requests), 2)
        self.assertEqual(w._live_session.actual_version, VERSION)

    def test_positive_status_fences_ready_old_result_before_and_during_initialization(self):
        newer = '2026.08.08.0000'
        for already_accepted in (False, True):
            with self.subTest(already_accepted=already_accepted):
                self.now = 10
                w, ui = self.window('0')
                ui.poll()
                if already_accepted:
                    self.complete(ui)
                    ui.refresh()
                    ui.poll()
                token = ui.token
                self.now = 12
                self.status(w, newer)
                ui.worker.result = (token, result(files=999))
                ui.poll()
                self.assertIsNot(ui.token, token)
                self.assertEqual(ui.state.values, {})
                self.assertIsNone(ui.state.last_success)
                current = ui.token
                ui.worker.result = (token, result(files=999))  # late/cancelled replay
                ui.poll()
                self.assertIs(ui.token, current)
                self.assertEqual(ui.state.values, {})
                self.complete(ui, result(version=newer, at=1100, files=3))
                self.assertEqual(ui.state.accepted_version, newer)

    def test_different_status_cancels_known_old_scope_before_new_verification(self):
        w, ui = self.window('0')
        ui.poll()
        self.complete(ui)
        ui.refresh()
        ui.poll()
        token, count = ui.token, len(ui.worker.requests)
        self.now += 1
        newer = '2026.08.08.0000'
        self.status(w, newer)
        ui.poll()
        self.assertEqual(ui.state.values, {})
        self.assertIsNot(ui.token, token)
        self.assertEqual(len(ui.worker.requests), count + 1)
        current = ui.token
        ui.worker.result = (token, result(version=newer, at=1100))
        ui.poll()
        self.assertIs(ui.token, current)
        self.assertIsNone(ui.state.last_success)
        self.complete(ui, result(version=newer, at=1100))
        self.assertEqual(len(ui.worker.requests), count + 1)
        self.assertEqual(ui.state.last_success, 1100)

    def test_contradicted_pending_failure_cannot_suppress_verification_even_disabled(self):
        for minutes in ('1', '0'):
            with self.subTest(minutes=minutes):
                self.now = 10
                w, ui = self.window(minutes)
                ui.poll()
                self.complete(ui)
                ui.refresh()
                ui.poll()
                old = ui.token
                self.now = 15
                newer = '2026.08.08.0000'
                self.status(w, newer)
                ui.worker.result = (old, result(coherent=False, error=True))
                ui.poll()
                self.assertEqual(len(ui.worker.requests), 3)
                self.assertIsNot(ui.token, old)
                self.assertIsNone(ui.completed)
                self.assertIsNone(ui.state.last_success)
                token = ui.token
                ui.worker.result = (old, result(coherent=False, error=True))
                ui.poll()
                self.assertIs(ui.token, token)
                self.assertIsNone(ui.completed)
                self.complete(ui, result(version=newer, at=1100))
                self.assertEqual(ui.state.last_success, 1100)

    def test_processed_and_expired_publication_skip_full_trust_checks_on_idle(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                self.now = 100
                w, ui = self.window('0')
                ui.poll()
                self.complete(ui)
                self.status(w, observed_at=0 if expired else 100)
                with patch('channels_dvr.dashboard_ui.current_status',
                           wraps=bound_status) as current:
                    ui.poll()
                    calls = current.call_count
                    for _ in range(20):
                        self.now += 0.05
                        ui.poll()
                    self.assertEqual(current.call_count, calls)
                    self.assertEqual(calls, 0 if expired else 1)
                self.assertEqual(len(ui.worker.requests), 1)

    def test_untrusted_fresh_publication_can_become_eligible_on_later_poll(self):
        w, ui = self.window('0')
        ui.poll()
        self.complete(ui)
        self.now += 1
        self.status(w, '2026.08.08.0000')
        w._live_session.loading = True
        ui.poll()
        self.assertEqual(len(ui.worker.requests), 1)
        self.assertEqual(ui.state.accepted_version, VERSION)
        w._live_session.loading = False
        ui.poll()
        self.assertEqual(len(ui.worker.requests), 2)
        self.assertEqual(ui.state.values, {})

    def test_unsupported_status_and_own_unsupported_result_remain_fail_closed(self):
        w, ui = self.window('0')
        ui.poll()
        self.complete(ui)
        self.now += 1
        unsupported = '2020.01.01.0000'
        self.status(w, unsupported)
        ui.poll()
        self.assertFalse(ui.state.values)
        self.assertIsNone(ui.state.last_success)
        self.complete(ui, result(version=unsupported, coherent=False, unsupported=True, error=True))
        self.assertEqual(ui.state.unsupported_version, unsupported)
        self.assertEqual(len(ui.worker.requests), 2)
        self.now += 1000
        ui.poll()
        self.assertEqual(len(ui.worker.requests), 2)
        ui.refresh()
        ui.poll()
        self.complete(ui, result(at=1200))
        self.assertEqual(ui.state.last_success, 1200)

    def test_unknown_or_incoherent_stats_result_cannot_advance_success(self):
        w, ui = self.window('0')
        ui.poll()
        token = ui.token
        self.now += 1
        self.metadata(w, 'block')
        self.status(w)
        ui.worker.result = (token, result(version=None, coherent=False, error=True))
        ui.poll()
        self.assertIsNone(ui.state.last_success)
        self.assertIsNone(ui.state.accepted_version)
        self.assertIn('partial, failed or stale', ui.warning_line())
        self.assertEqual(len(ui.worker.requests), 1)

    def test_unsealed_expired_wrong_config_and_cancelled_status_proofs_are_ignored(self):
        for rejected in ('unsealed', 'expired', 'old-generation', 'wrong-config', 'cancelled'):
            with self.subTest(rejected=rejected):
                self.now = 100
                w, ui = self.window('0')
                ui.poll()
                self.complete(ui)
                state = ui.state
                observation, context = self.status(w, '2020.01.01.0000',
                    observed_at=0 if rejected == 'expired' else 100)
                if rejected == 'unsealed':
                    observation = replace(observation, sample=replace(observation.sample, _seal=object()))
                elif rejected == 'old-generation':
                    w._live_session._generation += 1
                elif rejected == 'wrong-config':
                    observation = replace(observation, config=DevelopmentConfig('http://192.0.2.11:8089'))
                elif rejected == 'cancelled':
                    w._current_status_receipt = None
                w._guide_runtime.latest_status = (observation, context)
                ui.poll()
                self.assertIs(ui.state, state)
                self.assertEqual(len(ui.worker.requests), 1)

    def test_delayed_status_older_than_subsequent_stats_read_is_not_a_capability(self):
        self.now = 100
        w, ui = self.window('0')
        ui.poll()
        self.complete(ui)
        state = ui.state
        self.now = 101
        self.status(w, '2020.01.01.0000', observed_at=99)  # still fresh, but superseded
        ui.poll()
        self.assertIs(ui.state, state)
        self.assertEqual(len(ui.worker.requests), 1)

    def test_source_or_config_change_cancels_old_result_even_with_metadata_proof(self):
        for change in ('config', 'session'):
            with self.subTest(change=change):
                w, ui = self.window('0')
                ui.poll()
                token = ui.token
                self.status(w)
                if change == 'config':
                    w._development_config = w.saved = DevelopmentConfig('http://192.0.2.11:8089')
                else:
                    w._live_session = SimpleNamespace(_generation=1, actual_version=VERSION,
                        _version_blocked=False, summaries={}, incarnation=object(), loading=False)
                ui.worker.result = (token, result(files=999))
                ui.poll()
                self.assertIsNot(ui.token, token)
                self.assertFalse(ui.state.values)
                self.assertIsNone(ui.state.last_success)

    def test_navigation_metadata_changes_preserve_due_time_and_disabled_state(self):
        for minutes in ('1', '0'):
            with self.subTest(minutes=minutes):
                self.now = 10
                w, ui = self.window(minutes)
                ui.poll()
                self.complete(ui)
                w.render()
                state = ui.state
                w.view = 'Guide'
                self.now = 80
                self.metadata(w, 'generation')
                self.metadata(w, 'block')
                w.setting_reads.clear()
                ui.poll()
                self.assertEqual(w.setting_reads, [])
                with patch.object(w, 'render'):
                    back(w)
                ui.poll()
                self.assertIs(ui.state, state)
                self.assertEqual(ui.state.last_success, 1000)
                self.assertEqual(len(ui.worker.requests), 2 if minutes == '1' else 1)

    def test_listing_provenance_still_rejects_blocked_and_wrong_version_summaries(self):
        w, ui = self.window('0')
        ui.poll()
        self.complete(ui)
        summary = ResourceSummary(CONFIG.namespace, VERSION, (1000, 2000), 1000, 123, ())
        w._live_session.summaries['channels'] = summary
        self.assertTrue(ui.poll())
        self.assertIn('123', ui.cards()[4])
        self.metadata(w, 'block')
        self.assertTrue(ui.poll())
        self.assertNotIn('123', ui.cards()[4])
        self.assertEqual(ui.state.last_success, 1000)
        w._live_session._version_blocked = False
        w._live_session.summaries['channels'] = replace(summary, version='2026.08.08.0000')
        ui.poll()
        self.assertNotIn('123', ui.cards()[4])
        self.assertEqual(len(ui.worker.requests), 1)


class DashboardStateTests(unittest.TestCase):
    def test_partial_missing_scalar_retains_private_time_and_zero_false_replace(self):
        state = DashboardState()
        state.merge(result())
        fields = tuple((k, None if k == 'files' else v) for k, v in dict(result(files=0).resources)['dvr'])
        state.merge(result(at=1100, resources=tuple((n, fields if n == 'dvr' else v)
                                                  for n, v in result().resources)))
        self.assertEqual(dict(state.values['dvr'].values)['files'], 2)
        self.assertTrue(state.values['dvr'].unavailable)
        self.assertEqual(state.values['dvr'].fetched_at, 1000)
        self.assertEqual(state.last_success, 1000)
        state.merge(result(at=1200, files=0))
        self.assertEqual(dict(state.values['dvr'].values)['files'], 0)
        self.assertIs(dict(state.values['dvr'].values)['enabled'], False)
        self.assertEqual(state.last_success, 1200)

    def test_version_drift_never_retags_values_and_new_version_partial_has_no_fallback(self):
        state = DashboardState()
        state.merge(result())
        newer = '2026.08.08.0000'
        state.merge(result(version=newer, coherent=False, error=True))
        self.assertEqual(state.accepted_version, VERSION)
        self.assertEqual(state.last_success, 1000)
        state.merge(result(version=newer, at=1100, resources=(('server', None), ('dvr', None)), error=True))
        self.assertEqual(state.accepted_version, newer)
        self.assertEqual(state.values, {})
        self.assertIsNone(state.last_success)

    def test_unsupported_observation_clears_values_and_success(self):
        state = DashboardState()
        state.merge(result())
        state.merge(result(version='2020.01.01.0000', unsupported=True, coherent=False, error=True))
        self.assertFalse(state.values)
        self.assertEqual(state.unsupported_version, '2020.01.01.0000')
        self.assertIsNone(state.last_success)

    def test_parser_missing_values_remain_none_and_scalars_are_strict(self):
        parsed = parse_resource('dvr', {'stats': {'files': 0, 'jobs': False}, 'enabled': False}, 1000)
        self.assertEqual(parsed['files'], 0)
        self.assertIsNone(parsed['jobs'])
        self.assertIsNone(parsed['busy'])
        self.assertIs(parsed['enabled'], False)

    def test_listing_health_observes_optional_failure_without_renewing_cache(self):
        session = LiveDataSession(CONFIG.namespace, VERSION, clock=lambda: 1000)
        session._recordings.update(session._recordings_query,
            DataResult((), DataState.EMPTY), now=1000, ttl=300)
        self.assertEqual(session.summary_health('recordings'), (False, False))
        session._recordings.update(session._recordings_query,
            DataResult((), DataState.OUTAGE), now=1010, ttl=300)
        self.assertEqual(session.summary_health('recordings'), (True, True))
        view = session._recordings.get(session._recordings_query, now=1010)
        self.assertEqual(view.updated_at, 1000)
        self.assertIsNone(session.last_error, 'resource failure is not necessarily a session-wide failure')
        session._recordings.update(session._recordings_query,
            DataResult((), DataState.EMPTY), now=1000, ttl=0)
        self.assertEqual(session.summary_health('recordings'), (True, False))
        self.assertEqual(session.summary_health('events'), (True, False))


class DashboardSettingsTests(unittest.TestCase):
    def test_region_timestamp_and_deterministic_local_fallback(self):
        local = datetime(2026, 11, 25, 17, 23, 45, tzinfo=timezone(timedelta(hours=-7)))
        with patch('channels_dvr.dashboard_ui.datetime') as dt:
            dt.fromtimestamp.return_value.astimezone.return_value = local
            region = Mock(side_effect=lambda key: {'dateshort': '%d.%m.%Y', 'time': '%I:%M %p'}[key])
            self.assertEqual(format_timestamp(1000, region), '25.11.2026 05:23 PM -0700')
            self.assertEqual([call.args for call in region.call_args_list], [('dateshort',), ('time',)])
            unpadded = local.replace(month=1, day=5)
            dt.fromtimestamp.return_value.astimezone.return_value = unpadded
            region = lambda key: {'dateshort': '%-d.%-m.%Y', 'time': '%H:%M:%S'}[key]
            self.assertEqual(format_timestamp(1000, region), '5.1.2026 17:23:45 -0700')
            # Windows-native %# is accepted, preserving order on that runtime;
            # strftime's actual modifier behavior is platform-specific.
            region = lambda key: {'dateshort': '%#d.%#m.%Y', 'time': '%H:%M:%S'}[key]
            self.assertEqual(format_timestamp(1000, region), unpadded.strftime('%#d.%#m.%Y %H:%M:%S %z'))
            dt.fromtimestamp.return_value.astimezone.return_value = local
            for region in (None, Mock(return_value=''), Mock(return_value='%q'),
                           Mock(side_effect=RuntimeError('missing region'))):
                self.assertEqual(format_timestamp(1000, region), '2026-11-25 17:23:45 -0700')

    def test_persisted_values_are_closed_not_indexes_or_coerced_numbers(self):
        for value in (None, '', '3', '4', ' 5', '5 ', '05', '300', 'disabled', 0, 1, True, [], {}):
            with self.subTest(value=value):
                self.assertEqual(refresh_seconds(value), 300)
        for minutes in (0, 1, 2, 5, 10, 15, 30):
            self.assertEqual(refresh_seconds(str(minutes)), minutes * 60)

    def test_xml_default_choices_labels_and_nonoverlapping_status_position(self):
        root = Path(__file__).resolve().parents[1] / 'script.channelsdvr.hub'
        settings = ET.parse(root / 'resources/settings.xml').getroot()
        setting = settings.find(".//setting[@id='dashboard_refresh_minutes']")
        self.assertEqual(setting.get('type'), 'string')
        self.assertEqual(setting.findtext('default'), '5')
        self.assertEqual([n.text for n in setting.findall('constraints/options/option')],
                         ['0', '1', '2', '5', '10', '15', '30'])
        self.assertEqual(setting.find('control').attrib, {'type': 'spinner', 'format': 'string'})
        for ident in [setting.get('label'), setting.get('help')] + [n.get('label') for n in setting.findall('constraints/options/option')]:
            self.assertIn(int(ident), IDS.values())
        skin = ET.parse(root / 'resources/skins/Default/1080i/ChannelsDvr.xml').getroot()
        line = skin.find(".//control[@id='116']")
        bottom = int(line.findtext('top')) + int(line.findtext('height'))
        self.assertLessEqual(bottom, 235)
        self.assertGreaterEqual(int(line.findtext('top')), 215)
        warning = skin.find(".//control[@id='117']")
        self.assertEqual(warning.findtext('font'), 'font10')
        self.assertLessEqual(int(warning.findtext('top')) + int(warning.findtext('height')),
                             int(line.findtext('top')))
        self.assertFalse(skin.findall('.//autoscroll'))


class Response(io.BytesIO):
    def __init__(self, value, content_type='application/json'):
        super().__init__(json.dumps(value).encode())
        self.status = 200
        self.headers = Message()
        self.headers['Content-Type'] = content_type


class DashboardTransportTests(unittest.TestCase):
    def client(self, responses):
        calls = []
        def open_response(request, timeout):
            calls.append((request.full_url, request.method, timeout))
            value = responses[len(calls) - 1]
            if isinstance(value, Exception):
                raise value
            return value
        client = DashboardStatusReadClient(CONFIG, opener=SimpleNamespace(open=open_response),
                                           clock=lambda: 10, wall=lambda: 1000)
        return client, calls

    def responses(self):
        status = {'version': VERSION, 'os': 'Linux', 'arch': 'x86_64',
                  'start_time': '1970-01-01T00:00:00Z'}
        return [Response(status), Response({'stats': {}}), Response({}),
                Response([]), Response([]), Response(status)]

    def test_six_closed_gets_with_status_bookends_and_immutable_result(self):
        client, calls = self.client(self.responses())
        item = client.read()
        self.assertTrue(item.coherent)
        self.assertEqual([url.removeprefix(CONFIG.origin) for url, _, _ in calls],
                         ['/status', '/dvr', '/dvr/activity', '/devices', '/dvr/rules', '/status'])
        self.assertTrue(all(method == 'GET' and timeout <= 5 for _, method, timeout in calls))
        self.assertEqual(client.remaining_reads, 0)
        with self.assertRaises(FrozenInstanceError):
            item.error = False

    def test_optional_failure_is_partial_but_status_drift_is_incoherent(self):
        responses = self.responses()
        responses[1] = ValueError('unavailable')
        client, _ = self.client(responses)
        item = client.read()
        self.assertTrue(item.coherent)
        self.assertTrue(item.error)
        self.assertIsNone(dict(item.resources)['dvr'])
        responses = self.responses()
        responses[-1] = Response({'version': '2026.08.08.0000'})
        client, _ = self.client(responses)
        item = client.read()
        self.assertFalse(item.coherent)
        self.assertTrue(all(values is None for _, values in item.resources))

    def test_six_accepted_gets_with_unreported_other_is_success_not_partial(self):
        responses = self.responses()
        responses[2] = Response(dict(streams=[], recordings=[], workers=[]))
        client, calls = self.client(responses)
        item = client.read()
        self.assertEqual(len(calls), 6)
        self.assertTrue(item.coherent)
        self.assertFalse(item.error)
        w = Window()
        ui = w._dashboard_ui
        ui.poll()
        ui.worker.result = (ui.token, item)
        ui.poll()
        self.assertEqual(ui.state.last_success, 1000)
        self.assertFalse(ui.state.partial)
        self.assertFalse(ui.state.values['activity'].unavailable)
        self.assertIn('Other: Unknown', ui.cards()[5])
        self.assertIn('Streams: 0', ui.cards()[5])
        self.assertEqual('', ui.warning_line())
        self.assertNotIn('Never', ui.status_line())

    def test_cancellation_deadline_budgets_route_and_type_safeguards(self):
        for mutate in (lambda c: setattr(c, 'cancelled', lambda: True),
                       lambda c: setattr(c, 'clock', lambda: 35),
                       lambda c: setattr(c, 'remaining_reads', 0),
                       lambda c: setattr(c, 'remaining_bytes', 0)):
            client, calls = self.client(self.responses())
            mutate(client)
            self.assertFalse(client.read().coherent)
            self.assertLessEqual(len(calls), 1)
        client, calls = self.client(self.responses())
        with self.assertRaises(ValueError):
            client._get('/not-allowed')
        self.assertEqual(calls, [])
        client, _ = self.client([Response({'version': VERSION}, content_type='text/html')])
        self.assertFalse(client.read().coherent)

    def test_worker_serializes_replacements_and_cannot_publish_cancelled_results(self):
        entered, release, second = Event(), Event(), Event()
        threads = []
        cancelled_observations = []
        def factory(config, cancelled):
            def read():
                threads.append(get_ident())
                if len(threads) == 1:
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError('test release timeout')
                    cancelled_observations.append(cancelled())
                else:
                    second.set()
                return result()
            return SimpleNamespace(read=read)
        worker = DashboardWorker(factory)
        self.addCleanup(worker.close)
        self.addCleanup(release.set)
        worker.request(CONFIG)
        self.assertTrue(entered.wait(2))
        worker.cancel()
        token = worker.request(CONFIG)
        self.assertFalse(second.is_set())
        release.set()
        self.assertTrue(second.wait(2))
        with worker.condition:
            self.assertTrue(worker.condition.wait_for(lambda: worker.result is not None, 2))
        published = worker.poll()
        self.assertIs(published[0], token)
        self.assertEqual(len(set(threads)), 1)
        self.assertEqual(cancelled_observations, [True])
        self.assertNotEqual(threads[0], get_ident())
        worker.close()
        worker.thread.join(2)
        self.assertFalse(worker.thread.is_alive())
        self.assertIsNone(worker.poll())
        with self.assertRaises(ValueError):
            worker.request(CONFIG)

    def test_worker_exception_is_sanitized_and_close_discards_inflight_result(self):
        def broken(config, cancelled):
            raise ValueError('private transport detail must not escape')
        worker = DashboardWorker(broken)
        token = worker.request(CONFIG)
        with worker.condition:
            self.assertTrue(worker.condition.wait_for(lambda: worker.result is not None, 2))
        published = worker.poll()
        self.assertIs(published[0], token)
        self.assertTrue(published[1].error)
        self.assertTrue(all(v is None for _, v in published[1].resources))
        worker.close()
        worker.thread.join(2)
        entered, release = Event(), Event()
        def factory(config, cancelled):
            def read():
                entered.set()
                release.wait(2)
                return result()
            return SimpleNamespace(read=read)
        worker = DashboardWorker(factory)
        self.addCleanup(worker.close)
        self.addCleanup(release.set)
        worker.request(CONFIG)
        self.assertTrue(entered.wait(2))
        worker.close()
        release.set()
        worker.thread.join(2)
        self.assertIsNone(worker.poll())


class DashboardOwnerIntegrationTests(DashboardFixture):
    def adapter(self):
        # Import the actual adapter only with explicit inert SDK fakes. No stubs.
        modules = {'xbmc': SimpleNamespace(),
                   'xbmcgui': SimpleNamespace(WindowXML=object, ControlLabel=Control),
                   'xbmcvfs': SimpleNamespace()}
        context = patch.dict(sys.modules, modules)
        context.start()
        self.addCleanup(context.stop)
        from channels_dvr.kodi_ui import ChannelsWindow
        return ChannelsWindow

    def whole_window(self, strings=FALLBACK):
        """Real parent render/memo/chrome and owner loop, inert SDK controls."""
        from types import MethodType
        adapter = self.adapter()
        w = self.w
        w.controls.update({i: Control() for i in (100, 110, 112, 113, 114, 115, 116, 117,
                                                 *range(120, 126), *range(140, 144))})
        w.strings = strings
        w.t = lambda key, **values: text(w.strings, key, **values)
        w._render_memo = RenderMemo()
        w.pool = [(Control(), Control())]  # previously displayed listing row
        w._guide_line = None
        w.path = ''
        w.guide = SimpleNamespace(prepared_ready=False, selection=object(), group='All')
        w.repository = SimpleNamespace(channels=())
        w.metrics = GuiMetrics()
        w._paint_pending = False
        w._status_tick = None
        w._revoke_demo = Mock()
        w.demo_allowed = lambda: False
        w.selected_indexed_event = lambda: None
        w._guide_runtime.ready = False
        w._guide_runtime.revision = 0
        w._live_session._catalog_epoch = 0
        w._live_session.loading = False
        w._live_session.guide = lambda: Presentation((), DataState.OUTAGE, False, True,
            None, None, 'Updated 37s ago | listing failure', '', False)
        w._guide_runtime.presentation = lambda data: data
        w._guide_runtime.poll = Mock(return_value=False)
        w._artwork_ui = SimpleNamespace(poll=Mock(return_value=False), update=Mock(), draw=Mock())
        w._recording_ui = SimpleNamespace(poll=Mock(), _current_config=lambda: None)
        w._storage_ui = SimpleNamespace(poll=Mock())
        w._playback_ui.poll = Mock()
        w._series_ui = None
        w._large_exercise = None
        w._dispatcher = UiDispatcher()
        w.initialized = True
        for name in ('render', '_render', '_label', '_ensure_pool', '_current_data', 'process_pending'):
            setattr(w, name, MethodType(getattr(adapter, name), w))
        return w

    def displayed(self):
        result = {}
        for ident, control in self.w.controls.items():
            visible, body = True, ''
            for method, args in control.calls:
                if method == 'setVisible':
                    visible = args[0]
                elif method in ('setText', 'setLabel'):
                    body = args[0]
            if visible and body:
                result[ident] = body
        return result

    def test_whole_dashboard_has_no_elapsed_or_routine_status(self):
        w = self.whole_window()
        w.render()
        shown = '\n'.join(self.displayed().values())
        self.assertNotIn('Server running time', shown)
        self.assertNotIn('0d 0h 1m 0s', shown)
        self.assertNotIn('Complete', shown)
        self.assertIn('Last updated:', shown)
        before = self.displayed()
        self.ui.refresh()
        w.process_pending()
        self.assertEqual(self.displayed(), before)
        with patch.object(w, 'render', wraps=w.render) as render:
            for _ in range(20):
                self.now += 1
                w.process_pending()
            render.assert_not_called()

    def test_whole_dashboard_clears_listing_banner_on_navigation_and_back(self):
        w = self.whole_window()
        for return_method in ('navigation', 'back', 'nav-back'):
            execute_command(w, None, 'view.guide')
            w.process_pending()
            self.assertIn('Updated 37s ago', self.displayed()[114])
            self.assertNotIn(116, self.displayed())
            self.assertFalse(set(range(400, 406)) & self.displayed().keys())
            if return_method == 'navigation':
                execute_command(w, None, 'view.dashboard')
            elif return_method == 'back':
                back(w)
            else:
                w.focus = 201
                w._chrome.action(92)
            w.process_pending()
            self.assertEqual(w.view, 'Dashboard')
            self.assertNotIn('ago', '\n'.join(self.displayed().values()))
            self.assertNotIn(114, self.displayed())
            self.assertTrue(set(range(400, 406)) <= self.displayed().keys())

    def test_whole_dashboard_localized_timestamp_and_single_warning_lifecycle(self):
        legacy = {'dashboard_server', 'dashboard_dvr', 'dashboard_sources',
                  'dashboard_passes', 'dashboard_activity', 'dashboard_status',
                  'dashboard_duration', 'dashboard_listing_count'}
        by_id = {IDS[key]: ('OLD AGE ' if key in legacy else 'L: ') + value
                 for key, value in FALLBACK.items()}
        w = self.whole_window(capture(by_id.get))
        self.ui.timestamp = lambda stamp: f'ABSOLUTE {stamp}'
        summary = ResourceSummary(CONFIG.namespace, VERSION, (1000, 2000), 1000, 9, ())
        w._live_session.summaries['recordings'] = summary
        self.ui.poll()
        w.render()
        line = self.displayed()[116]
        self.assertEqual(line, 'L: Last updated: ABSOLUTE 1000')
        self.assertNotIn(117, self.displayed())

        def check_warning(expected=True):
            shown = self.displayed()
            self.assertEqual(shown[116], line)
            self.assertEqual(117 in shown, expected)
            body = '\n'.join(shown.values())
            self.assertNotIn('OLD AGE', body)
            self.assertNotIn('ago', body)
            self.assertNotIn('Refreshing', body)
            self.assertNotIn('Complete', body)
            if expected:
                self.assertEqual(body.count('L: Statistics are partial, failed or stale'), 1)
            self.assertNotIn('stale', '\n'.join(shown[i] for i in range(400, 406)))

        # Independent listing expiry/failure never dates statistics, shows a
        # statistics warning, or invalidates the parent Dashboard display.
        w._live_session.summary_health.return_value = (True, True)
        with patch.object(w, 'render', wraps=w.render) as render:
            w.process_pending()
            render.assert_not_called()
        check_warning(False)
        self.assertIn('Recordings (complete scan): 9', self.displayed()[404])
        w._live_session.summary_health.return_value = (False, False)
        with patch.object(w, 'render', wraps=w.render) as render:
            w.process_pending()
            render.assert_not_called()
        check_warning(False)
        # Both partial resource failure and total failure retain the last success.
        for item in (result(at=1100, resources=tuple((n, None if n == 'dvr' else v)
                                                   for n, v in result().resources), error=True),
                     result(at=1200, coherent=False, error=True)):
            self.ui.refresh()
            w.process_pending()
            self.ui.worker.result = (self.ui.token, item)
            w.process_pending()
            check_warning()
            self.ui.refresh()
            w.process_pending()
            check_warning()  # a retry must not hide the existing warning
        # A complete cycle alone advances the timestamp and clears the warning.
        self.ui.worker.result = (self.ui.token, result(at=1300))
        w.process_pending()
        line = 'L: Last updated: ABSOLUTE 1300'
        check_warning(False)
        w.settings['dashboard_refresh_minutes'] = '0'
        w.process_pending()
        self.now += 300
        w.process_pending()
        check_warning()
        with patch.object(w, 'render', wraps=w.render) as render:
            self.now += 1000
            w.process_pending()
            render.assert_not_called()  # stable stale state is not a timer repaint

    def test_changed_templates_reject_old_placeholder_translations(self):
        translated = capture(lambda ident: 'OLD {timestamp} {status}'
                             if ident == IDS['dashboard_updated'] else '')
        w = self.whole_window(translated)
        w.render()
        self.assertTrue(self.displayed()[116].startswith('Last updated:'))

    def test_whole_dashboard_on_time_automatic_read_does_not_flash_stale(self):
        w = self.whole_window()
        self.ui.timestamp = lambda stamp: f'ABSOLUTE {stamp}'
        w.render()
        before = self.displayed()
        self.now = 310.05  # normal owner-loop jitter at the five-minute deadline
        with patch.object(w, 'render', wraps=w.render) as render:
            w.process_pending()
            self.assertIsNotNone(self.ui.token)
            self.assertEqual(len(self.ui.worker.requests), 2)
            self.assertTrue(self.ui._stale(self.ui.state.values['server']))
            self.assertEqual(self.displayed(), before)
            self.now += 0.25
            w.process_pending()
            self.assertEqual(self.displayed(), before)
            render.assert_not_called()
            self.ui.worker.result = (self.ui.token, result(at=2000))
            w.process_pending()
            self.assertEqual(render.call_count, 1)
        after = self.displayed()
        self.assertEqual(after.pop(116), 'Last updated: ABSOLUTE 2000')
        before.pop(116)
        self.assertEqual(after, before)
        self.assertNotIn(117, self.displayed())

    def test_whole_dashboard_delayed_automatic_read_and_late_poll_warn(self):
        w = self.whole_window()
        w.render()
        self.now = 310
        w.process_pending()
        self.assertNotIn(117, self.displayed())
        self.now += 25
        w.process_pending()
        self.assertIn(117, self.displayed())
        self.assertEqual(self.ui.state.last_success, 1000)
        self.ui.worker.result = (self.ui.token, result(at=2000))
        w.process_pending()
        self.assertNotIn(117, self.displayed())
        self.now += 400  # resume polling well past the next deadline
        w.process_pending()
        self.assertIsNotNone(self.ui.token)
        self.assertIn(117, self.displayed())

    def test_automatic_retry_keeps_partial_or_failed_warning(self):
        w = self.whole_window()
        for item in (result(error=True), result(error=True, coherent=False)):
            self.ui.state.merge(item)
            self.now += 300
            w.process_pending()
            self.assertIsNotNone(self.ui.token)
            self.assertIn(117, self.displayed())
            self.ui.worker.result = (self.ui.token, item)
            w.process_pending()

    def test_owned_setting_read_paths_preserve_legacy_and_boolean_settings(self):
        adapter = self.adapter()
        addon = SimpleNamespace(getSettingString=Mock(side_effect=lambda key: 'legacy' if key == 'server_expected_version' else '5'),
                                getSettingBool=Mock(return_value=True))
        w = SimpleNamespace(_addon=addon)
        for key, expected in [('server_origin', '5'), ('server_expected_version', 'legacy'),
                              ('dashboard_refresh_minutes', '5'), ('ShowDeveloperTools', True),
                              ('development_metadata_enabled', True)]:
            self.assertEqual(adapter.get_setting(w, key), expected)
        with self.assertRaises(ValueError):
            adapter.get_setting(w, 'unowned')
        w._addon = None
        self.assertEqual(refresh_seconds(adapter.get_setting(w, 'dashboard_refresh_minutes')), 300)
        self.assertIs(adapter.get_setting(w, 'development_metadata_enabled'), False)

    def test_adapter_routes_kodi_region_provider_to_pure_formatter(self):
        adapter = self.adapter()
        provider = Mock(return_value='%Y')
        with patch('channels_dvr.kodi_ui.xbmc.getRegion', provider, create=True), \
                patch('channels_dvr.kodi_ui.format_timestamp', return_value='region date') as formatter:
            self.assertEqual(adapter._dashboard_timestamp(1000), 'region date')
            formatter.assert_called_once_with(1000, provider)

    def test_actual_owner_loop_publishes_without_focus_change_and_polls_other_work(self):
        adapter = self.adapter()
        w = self.w
        w._dispatcher = UiDispatcher()
        w._revoke_demo = Mock()
        w._guide_runtime.poll = Mock(return_value=False)
        w._artwork_ui = SimpleNamespace(poll=Mock(return_value=False))
        w._recording_ui = SimpleNamespace(poll=Mock())
        w._storage_ui = SimpleNamespace(poll=Mock())
        w._playback_ui.poll = Mock()
        w._series_ui = SimpleNamespace(poll=Mock())
        w._large_exercise = None
        w.initialized = True
        w._paint_pending = False
        selection = w.selection
        activation = w._chrome.capture(201)
        self.ui.refresh()
        self.ui.poll()
        self.ui.worker.result = (self.ui.token, result(at=1001, files=9))
        with patch.object(w, 'render', wraps=w.render) as render:
            adapter.process_pending(w)
            self.assertEqual(render.call_count, 1)
            self.now += 2
            self.wall += 2
            adapter.process_pending(w)
            self.assertEqual(render.call_count, 1)
            w._paint_pending = True
            adapter.process_pending(w)
            self.assertEqual(render.call_count, 2, 'non-timer invalidation must still render')
        self.assertIn('Files: 9', self.ui.cards()[1])
        self.assertEqual(w.focus_calls, [])
        self.assertIs(w.selection, selection)
        self.assertTrue(activation.matches(w._chrome.capture(201)))
        for coordinator in (w._guide_runtime, w._artwork_ui, w._recording_ui, w._storage_ui,
                            w._playback_ui, w._series_ui):
            self.assertEqual(coordinator.poll.call_count, 3)


if __name__ == '__main__':
    unittest.main()
