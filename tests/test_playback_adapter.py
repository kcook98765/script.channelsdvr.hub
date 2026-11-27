"""Omega playback adapter under inert SDK doubles; no Kodi or stream I/O."""
from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from channels_dvr.models import ChannelKey
from channels_dvr.playback_descriptors import LibraryRecording, PlaybackDescriptor, PlaybackSelection
from channels_dvr.playback_ui import PlaybackUI
from channels_dvr.ui_dispatch import UiDispatcher
from channels_dvr.ui_text import FALLBACK


ADAPTER = (Path(__file__).resolve().parents[1]
           / 'script.channelsdvr.hub/resources/lib/channels_dvr/playback_native.py')
LOCATOR = 'https://recording.invalid/Clip%20One?opaque=fixture'
RECORDING = PlaybackSelection('recording', LibraryRecording('fixture', 'dvr', 'file'), 1, 'Private title')


class PlaybackAdapterTests(unittest.TestCase):
    def setUp(self):
        self.item = Mock()
        self.list_item = Mock(return_value=self.item)
        self.play = Mock()
        self.observers = []
        fixture = self

        class Player:
            def __init__(self):
                self.play = fixture.play
                self.isPlaying = Mock(return_value=True)
                self.isPlayingVideo = Mock(return_value=True)
                self.isPlayingAudio = Mock(return_value=False)
                self.isExternalPlayer = Mock(return_value=False)
                self.getPlayingFile = Mock(return_value=LOCATOR)
                fixture.observers.append(self)

        spec = spec_from_file_location('channels_dvr._fake_playback_adapter', ADAPTER)
        adapter = module_from_spec(spec)
        with patch.dict(sys.modules, {'xbmc': SimpleNamespace(Player=Player),
                                      'xbmcgui': SimpleNamespace(ListItem=self.list_item)}):
            spec.loader.exec_module(adapter)
        self.Reading = adapter.RecordingReadback
        self.owner = Mock()
        self.native = adapter.NativePlayer(self.owner)
        self.descriptor = PlaybackDescriptor(RECORDING, LOCATOR, 'video/mp2t', 'fixture-attempt', 0.0,
                                             art=(('thumb', 'fixture-art'),))

    def start(self):
        self.assertTrue(self.native.handoff(self.descriptor, admit=lambda: True))
        return self.observers[-1]

    def test_fullscreen_requested_once_only_after_final_admission(self):
        def admit():
            self.play.assert_not_called()
            self.item.setPath.assert_called_with(LOCATOR)
            self.item.setContentLookup.assert_called_with(False)
            self.item.setProperty.assert_called_with('StartOffset', '0')
            self.item.setArt.assert_called_with({'thumb': 'fixture-art'})
            self.assertEqual(len(self.observers), 1)
            return True

        self.assertTrue(self.native.handoff(self.descriptor, admit=admit))
        self.assertFalse(self.native.handoff(self.descriptor, admit=admit))
        self.play.assert_called_once_with(LOCATOR, self.item, windowed=False)

    def test_denied_or_failed_send_never_creates_recording_readback(self):
        self.assertIsNone(self.native.recording_readback())
        self.assertFalse(self.native.handoff(self.descriptor, admit=lambda: False))
        self.play.assert_not_called()
        self.assertIsNone(self.native.recording_readback())
        self.play.side_effect = RuntimeError('ambiguous dispatch')
        with self.assertRaises(RuntimeError):
            self.native.handoff(self.descriptor, admit=lambda: True)
        self.assertFalse(self.native.handoff(self.descriptor, admit=lambda: True))
        self.assertIsNone(self.native.recording_readback())
        self.play.assert_called_once()
        for observer in self.observers:
            observer.isPlaying.assert_not_called()

    def test_readback_sample_does_not_depend_on_callbacks_or_normalize_aliases(self):
        player = self.start()
        self.assertIs(self.native.recording_readback(), self.Reading.EXACT_VIDEO)
        player.getPlayingFile.assert_called()  # no AV callback was needed
        for different in (LOCATOR.split('?')[0], LOCATOR.replace('%20', ' '),
                          LOCATOR.replace('Clip', 'clip'), 'https://other.invalid/Clip%20One?opaque=fixture'):
            with self.subTest(different=different):
                player.getPlayingFile.return_value = different
                self.assertIs(self.native.recording_readback(), self.Reading.OTHER_ACTIVE)
        player.getPlayingFile.return_value = LOCATOR
        player.isPlayingVideo.return_value = False
        player.isPlayingAudio.return_value = True
        self.assertIs(self.native.recording_readback(), self.Reading.OTHER_ACTIVE)

    def test_positive_idle_requires_consistent_activity_and_does_not_read_locator(self):
        player = self.start()
        player.isPlaying.return_value = player.isPlayingVideo.return_value = False
        self.assertIs(self.native.recording_readback(), self.Reading.IDLE)
        player.getPlayingFile.assert_not_called()
        player.isPlaying.side_effect = [False, True]
        self.assertIs(self.native.recording_readback(), self.Reading.UNKNOWN)
        player.isPlaying.side_effect = None
        player.isPlayingAudio.return_value = True
        self.assertIs(self.native.recording_readback(), self.Reading.UNKNOWN)
        player.getPlayingFile.assert_not_called()

    def test_unavailable_or_changing_readback_reports_unknown(self):
        player = self.start()
        player.isExternalPlayer.return_value = True
        self.assertIs(self.native.recording_readback(), self.Reading.UNKNOWN)
        player.getPlayingFile.assert_not_called()
        player.isExternalPlayer.return_value = False
        player.getPlayingFile.return_value = ''
        self.assertIs(self.native.recording_readback(), self.Reading.UNKNOWN)
        player.getPlayingFile.side_effect = [LOCATOR, LOCATOR + '-changed']
        self.assertIs(self.native.recording_readback(), self.Reading.UNKNOWN)
        player.getPlayingFile.side_effect = [LOCATOR, None]
        self.assertIs(self.native.recording_readback(), self.Reading.UNKNOWN)
        player.getPlayingFile.side_effect = RuntimeError(LOCATOR)
        self.assertIs(self.native.recording_readback(), self.Reading.UNKNOWN)

    def test_recording_callbacks_remain_advisory_and_sdk_reads_are_owner_only(self):
        player = self.start()
        self.owner.side_effect = RuntimeError('not owner')
        player.onAVStarted()
        player.onAVStarted()
        player.onPlayBackStopped()
        with self.assertRaisesRegex(RuntimeError, 'not owner'):
            self.native.recording_readback()
        player.isPlaying.assert_not_called()

        self.owner.side_effect = None
        self.assertEqual(self.native.poll(), ('av_started_observed', 'stopped_observed'))
        self.assertEqual(player.getPlayingFile.call_count, 2)  # one bracketed sample per poll
        self.native.close()
        player.onAVStarted()
        self.assertIsNone(self.native.recording_readback())
        self.assertEqual(self.native.poll(), ())

    def test_unrelated_av_callback_does_not_publish_recording_start(self):
        player = self.start()
        player.getPlayingFile.return_value = LOCATOR + '-other'
        player.onAVStarted()
        self.assertEqual(self.native.poll(), ())
        self.assertIs(self.native.recording_readback(), self.Reading.OTHER_ACTIVE)

    def test_diagnostics_only_emit_closed_observation_not_locator_or_title(self):
        player = self.start()
        ui = PlaybackUI(SimpleNamespace(_dispatcher=UiDispatcher(), strings=FALLBACK))
        ui.state, ui.player = 'handoff', self.native
        diagnostic = ui.diagnostic_text()
        self.assertIn('handoff; recording_exact_video', diagnostic)
        self.assertNotIn(LOCATOR, diagnostic)
        self.assertNotIn(RECORDING.title, diagnostic)
        player.getPlayingFile.side_effect = RuntimeError(LOCATOR)
        diagnostic = ui.diagnostic_text()
        self.assertIn('recording_readback_unknown', diagnostic)
        self.assertNotIn(LOCATOR, diagnostic)
        self.assertEqual(ui.state, 'handoff')  # the read-only checkpoint does not change state

    def test_live_does_not_produce_a_recording_observation(self):
        live = PlaybackSelection('live', ChannelKey('fixture', 'source', 'station'), 1, 'Listing')
        descriptor = replace(self.descriptor, selection=live, resolved_label='Station')
        self.assertTrue(self.native.handoff(descriptor, admit=lambda: True))
        self.list_item.assert_called_once_with(label='Station', offscreen=True)
        self.play.assert_called_once_with(LOCATOR, self.item, windowed=False)
        self.item.setArt.assert_not_called()
        self.assertIsNone(self.native.recording_readback())
        self.observers[-1].isPlaying.assert_not_called()
