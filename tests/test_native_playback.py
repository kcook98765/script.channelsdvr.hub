"""New playback contracts under fake metadata only; no old fixture/guard rebases."""
from dataclasses import replace
from email.message import Message
import io
import json
from threading import Event, get_ident
import time
import unittest
from urllib.error import HTTPError

from channels_dvr.live_loader import DevelopmentConfig
from channels_dvr.models import ChannelKey, Recording
from channels_dvr.library import LibraryBrowser
from channels_dvr.playback_descriptors import (PlaybackError, PlaybackSelection,
    PlaybackDescriptor, LibraryRecording, component, media_url, recording_url, live_url,
    m3u, admit_activity)
from channels_dvr.playback_http import PlaybackMetadataClient
from channels_dvr.playback_worker import PlaybackWorker

ORIGIN = 'http://192.0.2.10:8089'
CONFIG = DevelopmentConfig(ORIGIN)
NS = CONFIG.namespace
KEY = ChannelKey(NS, 'device', '28.1')
PUBLIC = [dict(id='28.1', source_id='device', number='28.1')]
DEVICES = [dict(DeviceID='device', TunerCount=4, Channels=[dict(GuideNumber='28.1', GuideName='PBS')])]
MEDIA = ORIGIN + '/devices/ANY/channels/28.1/stream.mpg?codec=copy&format=ts'
PLAYLIST = ('#EXTM3U\n#EXTINF:-1 channel-id="28.1" tvg-chno="28.1" tvg-id="external-guide-id",PBS\n' + MEDIA + '\n').encode()
FILE = dict(ID='file-A', Completed=True, Processed=True, Duration=148.271466, Airing=dict(ProgramID='p'))
EPISODES = [dict(id='file-A', program_id='p')]
ACTIVITY = dict(streams=[], recordings=[], workers=[], other=[], queues={'processor': 0})


def record(**changes):
    return replace(Recording('file-A', 'Episode', 'Show', 1, 1, False, 100, None,
        namespace=NS, provenance='unknown'), **changes)


def selection(kind='recording'):
    return PlaybackSelection(kind, LibraryRecording.selected(record()) if kind == 'recording' else KEY, 1, 'Title')


class Descriptors(unittest.TestCase):
    def test_recording_fractional_duration_absent_source_and_optional_flags(self):
        self.assertEqual(recording_url(ORIGIN, selection().identity, FILE, EPISODES),
                         ORIGIN + '/dvr/files/file-A/hls')
        self.assertEqual(recording_url(ORIGIN, selection().identity, dict(FILE, Path='C:/not/media'), EPISODES),
                         ORIGIN + '/dvr/files/file-A/hls')

    def test_recording_rejects_each_contraindication_without_inventing_false(self):
        for name, bad in [('Completed', False), ('Processed', False), ('Completed', 1),
            ('Recording', True), ('Corrupt', True), ('Failed', True), ('Deleted', True),
            ('Corrupt', None), ('Duration', 0), ('Duration', -1), ('Duration', True),
            ('Duration', float('nan')), ('Duration', float('inf')), ('ID', 'other'), ('ID', 1)]:
            with self.subTest(field=name, value=bad), self.assertRaises(PlaybackError):
                recording_url(ORIGIN, selection().identity, dict(FILE, **{name: bad}), EPISODES)

    def test_public_recordings_context_exact_id_source_and_program_consistency(self):
        for public in ([], EPISODES * 2, [dict(id='other')], [dict(id='file-A', source='imports')],
                       [dict(id='file-A', program_id='other')], [dict(id=1)], {}):
            with self.subTest(public=public), self.assertRaises(PlaybackError):
                recording_url(ORIGIN, selection().identity, FILE, public)
        recording_url(ORIGIN, selection().identity, FILE, [dict(id='file-A', source='recordings')])

    def test_opaque_ids_no_numeric_conversion_strip_or_dot_segments(self):
        self.assertEqual(component('0001'), '0001')
        self.assertEqual(component('épisode A'), '%C3%A9pisode%20A')
        for value in (1, '', ' ', '.', '..', '../one', 'x%2Fy', 'x?y', 'a#b', 'a|Header=x', 'x\\y', 'x\x7f', '\ud800'):
            with self.subTest(value=repr(value)), self.assertRaises(PlaybackError):
                component(value)

    def test_basic_same_origin_and_path_policy(self):
        self.assertEqual(media_url(ORIGIN, '/dvr/files/a/hls'), ORIGIN+'/dvr/files/a/hls')
        for value in ('file:///one', 'http://other/a', '//other/a', ORIGIN+'/a/../b',
            ORIGIN+'/a/%2e%2e/b', ORIGIN+'/a/%252e/b', ORIGIN+'/a%2Fb', ORIGIN+'/a#x',
            ORIGIN+'/a|User-Agent=x', ORIGIN+'/a?q=%0a', ORIGIN+'/a?q=%7c', 'http://u:p@192.0.2.10:8089/a'):
            with self.subTest(value=value), self.assertRaises(PlaybackError):
                media_url(ORIGIN, value)

    def test_live_documented_guidenumber_fallback_and_external_guide_id(self):
        self.assertEqual(live_url(ORIGIN, KEY, PUBLIC, DEVICES, PLAYLIST), (MEDIA, False))
        explicit = [dict(DeviceID='device', Channels=[dict(ID='28.1')])]
        pinned = PLAYLIST.replace(b'/devices/ANY/', b'/devices/device/')
        self.assertTrue(live_url(ORIGIN, KEY, PUBLIC, explicit, pinned)[1])

    def test_live_fallback_refuses_ambiguity_and_explicit_id_contradiction(self):
        bads = [([dict(PUBLIC[0], id='different')], DEVICES),
                (PUBLIC, [dict(DeviceID='device', Channels=DEVICES[0]['Channels']*2)]),
                (PUBLIC, [dict(DeviceID='device', Channels=[dict(ID='wrong', GuideNumber='28.1')])]),
                (PUBLIC, [dict(DeviceID='other', Channels=DEVICES[0]['Channels'])]),
                (PUBLIC+[dict(PUBLIC[0], source_id='other')], DEVICES)]
        for channels, devices in bads:
            with self.subTest(channels=channels, devices=devices), self.assertRaises(PlaybackError):
                live_url(ORIGIN, KEY, channels, devices, PLAYLIST)

    def test_any_requires_full_catalog_unique_backend_id_even_with_explicit_source_id(self):
        channels = PUBLIC + [dict(id='28.1', source_id='other', number='99')]
        devices = [dict(DeviceID='device', Channels=[dict(ID='28.1')])]
        with self.assertRaises(PlaybackError):
            live_url(ORIGIN, KEY, channels, devices, PLAYLIST)
        self.assertTrue(live_url(ORIGIN, KEY, channels, devices, PLAYLIST.replace(b'/ANY/', b'/device/'))[1])

    def test_live_requires_actual_uri_identity_not_just_attributes(self):
        for wire in (PLAYLIST.replace(b'channels/28.1/', b'channels/99/'),
            PLAYLIST.replace(b'channel-id="28.1"', b'channel-id="99"'),
            PLAYLIST.replace(b'tvg-chno="28.1"', b'tvg-chno="99"'),
            PLAYLIST + PLAYLIST.split(b'\n', 1)[1],
            PLAYLIST.replace(b'192.0.2.10', b'192.0.2.11'),
            PLAYLIST.replace(b'/ANY/', b'/other/')):
            with self.subTest(wire=wire), self.assertRaises(PlaybackError):
                live_url(ORIGIN, KEY, PUBLIC, DEVICES, wire)

    def test_playlist_bounds_bad_attributes_and_player_directives(self):
        for wire in (b'not m3u', b'#EXTM3U\n#EXTINF:-1,Missing\n',
            PLAYLIST.replace(b'channel-id="28.1"', b'channel-id="28.1" channel-id="28.1"'),
            PLAYLIST.replace(b'#EXTINF:', b'#KODIPROP:inputstream=x\n#EXTINF:'),
            b'x'*(2*1024*1024+1)):
            with self.subTest(length=len(wire)), self.assertRaises(PlaybackError):
                m3u(wire)

    def test_busy_activity_never_infers_capacity_from_tunercount(self):
        admit_activity(ACTIVITY)
        for key in ('streams', 'recordings', 'workers', 'other', 'queues'):
            bad = dict(ACTIVITY, **{key: {'processor': 1} if key == 'queues' else [{}]})
            with self.assertRaises(PlaybackError):
                admit_activity(bad)
        with self.assertRaises(PlaybackError):
            admit_activity(dict(ACTIVITY, queues={'processor': False}))

    def test_library_typed_leaf_lookup_never_parses_row_id(self):
        item = record()
        browser = LibraryBrowser((item, record(id='other', provenance='import', created=99)))
        self.assertIsNone(browser.selected_recording())
        browser.set_mode('Recent')
        self.assertIs(browser.selected_recording(), item)
        browser.set_query('Episode')
        self.assertIs(browser.selected_recording(), item)
        browser.update(())
        self.assertIsNone(browser.selected_recording())
        with self.assertRaises(PlaybackError):
            LibraryRecording.selected(record(provenance='import'))


class Response:
    def __init__(self, body, mime='application/json', status=200):
        self.body = io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = Message()
        self.headers['Content-Type'] = mime
        self.status = status
        self.closed = False

    def read1(self, amount):
        return self.body.read(amount)

    def close(self):
        self.closed = True


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request.full_url, request.get_method(), timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def status():
    return Response(dict(version='2026.10.24.0621'))


class MetadataTransport(unittest.TestCase):
    def test_completed_prepare_only_four_metadata_gets_never_hls_head_or_segments(self):
        responses = [status(), Response(FILE), Response(EPISODES), status()]
        opener = Opener(responses)
        descriptor = PlaybackMetadataClient(CONFIG, opener=opener).prepare(selection(), 'attempt')
        self.assertEqual([r[0].removeprefix(ORIGIN) for r in opener.requests],
            ['/status', '/dvr/files/file-A?raw=false', '/api/v1/episodes?source=recordings', '/status'])
        self.assertTrue(all(r[1] == 'GET' and 0 < r[2] <= 5 for r in opener.requests))
        self.assertTrue(all(r.closed for r in responses))
        self.assertEqual(descriptor.url, ORIGIN+'/dvr/files/file-A/hls')
        self.assertNotIn(ORIGIN, repr(descriptor))
        self.assertFalse(descriptor.resume_sync)
        self.assertEqual(descriptor.recording, LibraryRecording(NS, 'dvr', 'file-A'))
        self.assertEqual(descriptor.selection.identity.provenance, 'unknown')

    def test_live_prepare_six_metadata_gets_native_url_unopened(self):
        opener = Opener([status(), Response(PUBLIC), Response(DEVICES),
            Response(PLAYLIST, 'audio/x-mpegurl'), Response(ACTIVITY), status()])
        descriptor = PlaybackMetadataClient(CONFIG, opener=opener).prepare(selection('live'), 'live-attempt')
        self.assertEqual(descriptor.url, MEDIA)
        self.assertFalse(descriptor.source_pinned)
        self.assertEqual(descriptor.availability, 'UNKNOWN')
        self.assertEqual(len(opener.requests), 6)
        self.assertFalse(any('/stream.mpg' in r[0] for r in opener.requests))

    def test_named_404_missing_no_public_or_media_follow(self):
        opener = Opener([status(), HTTPError(ORIGIN, 404, 'secret', Message(), io.BytesIO())])
        with self.assertRaises(PlaybackError) as caught:
            PlaybackMetadataClient(CONFIG, opener=opener).prepare(selection(), 'a')
        self.assertEqual(caught.exception.code, 'file_missing')
        self.assertEqual(len(opener.requests), 2)

    def test_metadata_redirect_html_duplicate_json_encoding_and_size_refused(self):
        bads = [Response(b'', status=302), Response(b'<html>', 'text/html'),
                Response(b'{"version":"a","version":"b"}'), Response(b'x'*(4*1024*1024+1))]
        encoded = status()
        encoded.headers['Content-Encoding'] = 'gzip'
        bads.append(encoded)
        for bad in bads:
            opener = Opener([bad])
            with self.subTest(bad=bad), self.assertRaises(PlaybackError):
                PlaybackMetadataClient(CONFIG, opener=opener).prepare(selection(), 'a')
            self.assertTrue(bad.closed)
            self.assertEqual(len(opener.requests), 1)

    def test_status_floor_source_namespace_change_and_cancellation(self):
        for version in ('2026.08.07.0345', '2026.02.30.1200', 'bad'):
            with self.assertRaises(PlaybackError):
                PlaybackMetadataClient(CONFIG, opener=Opener([Response(dict(version=version))])).prepare(selection(), 'a')
        with self.assertRaises(PlaybackError):
            PlaybackMetadataClient(CONFIG, cancelled=lambda: True, opener=Opener([])).prepare(selection(), 'a')
        with self.assertRaises(PlaybackError):
            PlaybackMetadataClient(replace(CONFIG, namespace='other'), opener=Opener([])).prepare(selection(), 'a')
        opener = Opener([status(), Response(FILE), Response(EPISODES), Response(dict(version='2026.10.25.0100'))])
        with self.assertRaises(PlaybackError):
            PlaybackMetadataClient(CONFIG, opener=opener).prepare(selection(), 'a')

    def test_total_and_per_request_deadline_checked_before_acceptance(self):
        clock = [0.0]
        response = status()
        original = response.read1
        def slow(amount):
            clock[0] += 6
            return original(amount)
        response.read1 = slow
        with self.assertRaises(PlaybackError):
            PlaybackMetadataClient(CONFIG, opener=Opener([response]), clock=lambda: clock[0]).prepare(selection(), 'a')
        self.assertTrue(response.closed)


class Worker(unittest.TestCase):
    def test_coalesces_pending_reads_discards_old_result_and_close_does_not_join(self):
        entered, release, done = Event(), Event(), Event()
        calls, threads = [], []
        class Client:
            def __init__(self, config, cancelled):
                self.cancelled = cancelled
            def prepare(self, selected, attempt):
                calls.append(attempt)
                threads.append(get_ident())
                if attempt == 'a':
                    entered.set()
                    release.wait(2)
                if attempt == 'c':
                    done.set()
                return PlaybackDescriptor(selected, 'private', 'video/mp2t', attempt, 0)
        worker = PlaybackWorker(CONFIG, client_factory=Client)
        self.addCleanup(worker.close)
        worker.submit(selection(), 'a')
        self.assertTrue(entered.wait(2))
        worker.submit(selection(), 'b')
        worker.submit(selection(), 'c')
        release.set()
        self.assertTrue(done.wait(2))
        deadline, result = time.monotonic()+2, None
        while result is None and time.monotonic() < deadline:
            result = worker.poll()
            time.sleep(.001)
        self.assertEqual(result.attempt, 'c')
        self.assertEqual(calls, ['a', 'c'])
        self.assertTrue(all(t != get_ident() for t in threads))
        worker.close()
        self.assertFalse(worker.submit(selection(), 'd'))

    def test_close_inflight_suppresses_publication_without_owner_wait(self):
        entered, release = Event(), Event()
        class Client:
            def __init__(self, *args):
                pass
            def prepare(self, selected, attempt):
                entered.set()
                release.wait(2)
                return PlaybackDescriptor(selected, 'private', 'video/mp2t', attempt, 0)
        worker = PlaybackWorker(CONFIG, client_factory=Client)
        worker.submit(selection(), 'a')
        self.assertTrue(entered.wait(2))
        worker.close()
        self.assertIsNone(worker.poll())
        release.set()
