"""Native reference contract: no image bytes, downloader, cache, SDK or network."""
import ast
from dataclasses import asdict, replace
from pathlib import Path
import unittest
from unittest.mock import patch

from channels_dvr.artwork import ArtBuilder, channel_key, event_key, recording_key, job_key
from channels_dvr.artwork_refs import resolve_reference, first_reference
from channels_dvr import metadata_models as dto
from channels_dvr.models import ChannelKey, Event
from channels_dvr.normalization import DataState

ORIGIN = 'http://192.0.2.10:8089/prefix'
PUBLIC = 'https://any-image.example.test/image?synthetic-token=one%2Ftwo'
KEY = ChannelKey('synthetic', 'source1', 'channel1')
EVENT = Event('program', KEY, 1000, 2800, 'Title')
ROOT = Path(__file__).resolve().parents[1]


class ReferenceTests(unittest.TestCase):
    def test_invalid_ascii_hostname_syntax_rejected(self):
        for host in ('example^test', 'example{test', 'example}test', 'example<test',
                     'example`test', 'example"test', 'example_test', '-example.test',
                     'example-.test', '.example.test', 'example..test'):
            with self.subTest(host=host):
                self.assertIsNone(resolve_reference(ORIGIN, 'https://' + host + '/logo'))

    def test_raw_nonascii_authority_rejected_without_rewriting(self):
        for authority in ('münich.example', 'example。test', 'ｅxample.test', 'example.test:８０'):
            with self.subTest(authority=authority):
                self.assertIsNone(resolve_reference(ORIGIN, 'https://' + authority + '/logo'))

    def test_malformed_primary_and_unicode_primary_use_valid_alternate(self):
        for primary in ('https://example^test/logo', 'https://münich.example/logo'):
            with self.subTest(primary=primary):
                self.assertEqual(first_reference(ORIGIN, (primary, PUBLIC)), PUBLIC)

    def test_localhost_ipv4_private_lan_and_configured_origin_remain_supported(self):
        for origin in ('http://localhost:8089', 'http://dvr-lan:8089/prefix',
                       'http://192.168.1.20:8089', 'http://127.0.0.1:8089', 'http://10.0.0.5'):
            with self.subTest(origin=origin):
                self.assertEqual(resolve_reference(origin, 'logo?token=a%2Bb'), origin+'/logo?token=a%2Bb')

    def test_literal_ipv6_valid_with_optional_port_and_relative_origin(self):
        for host in ('[::1]', '[2001:db8::1]', '[fd00::1234]', '[::ffff:192.168.1.20]'):
            for suffix in ('', ':8089'):
                origin = 'http://' + host + suffix
                with self.subTest(origin=origin):
                    self.assertEqual(resolve_reference(ORIGIN, origin+'/logo?token=a%2Fb'), origin+'/logo?token=a%2Fb')
                    self.assertEqual(resolve_reference(origin, 'logo'), origin+'/logo')

    def test_malformed_brackets_literal_and_ports_rejected(self):
        for authority in ('[::1', '::1]', '::1', '[::1]]', '[[::1]', '[::1]bad', '[::1]bad:80',
                          '[::1]:', '[::1]:0', '[::1]:65536', '[::1]:bad', '[:::1]',
                          '[127.0.0.1]', '[v1.example]', '[fe80::1%25eth0]', 'example.test:80:90'):
            with self.subTest(authority=authority):
                self.assertIsNone(resolve_reference(ORIGIN, 'http://'+authority+'/logo'))

    def test_ascii_domain_limits_trailing_dot_punycode_and_query_unchanged(self):
        for host in ('localhost', 'DVR-LAN', 'example.test.', 'xn--mnich-kva.example', 'a'*63+'.test'):
            ref = 'https://'+host+':443/logo?token=a%2Fb%2Bc&next=%3Fq%3Dx'
            self.assertEqual(resolve_reference(ORIGIN, ref), ref)
        for host in ('a'*64+'.test', '.'.join(['a'*63]*4)):
            self.assertIsNone(resolve_reference(ORIGIN, 'https://'+host+'/logo'))

    def test_absolute_http_https_external_hosts_and_ports_are_not_allowlisted(self):
        for ref in (PUBLIC, 'http://other.test:8088/a.webp', 'https://[::1]:444/x',
                    'http://127.0.0.1/image', 'HTTPS://example.test/a.svg'):
            with self.subTest(ref=ref):
                self.assertEqual(resolve_reference(ORIGIN, ref), ref)

    def test_relative_prefix_uses_origin_directory_semantics(self):
        self.assertEqual(resolve_reference(ORIGIN, 'logo.png?x=1'), ORIGIN + '/logo.png?x=1')
        self.assertEqual(resolve_reference(ORIGIN+'/', 'logo.png'), ORIGIN + '/logo.png')
        self.assertEqual(resolve_reference(ORIGIN, '../logo'), 'http://192.0.2.10:8089/logo')

    def test_root_relative_is_not_prefixed_or_guessed(self):
        self.assertEqual(resolve_reference(ORIGIN, '/provided/image'), 'http://192.0.2.10:8089/provided/image')

    def test_query_is_preserved_exactly(self):
        self.assertEqual(resolve_reference(ORIGIN, PUBLIC), PUBLIC)
        self.assertEqual(resolve_reference(ORIGIN, '?token=a%2Bb'), ORIGIN + '/?token=a%2Bb')

    def test_local_and_non_http_schemes_rejected(self):
        for ref in ('file:///x', 'special://x', 'plugin://x', 'data:x', 'javascript:x', 'C:/x', '//other.test/x'):
            with self.subTest(ref=ref):
                self.assertIsNone(resolve_reference(ORIGIN, ref))

    def test_controls_userinfo_fragments_and_header_injection_rejected(self):
        for ref in ('https://u:p@x/x', 'https://u@x/x', '/x#f', '/x#', '/x|Header=y',
                    '/x\\y', '/x\0', '/x\r\n', '/x\x7f', '/x%00', '/x%0a', '/x%7c',
                    '/x%5c', '/x%23', '/x y', '/x%20', 'https://x/x\t', '/x\x85', '/x%C2%85'):
            with self.subTest(ref=ref):
                self.assertIsNone(resolve_reference(ORIGIN, ref))

    def test_malformed_url_port_and_percent_never_raise(self):
        for ref in ('http://', 'http:///x', 'https://[bad/x', 'https://x:0/x', 'https://x:65536/x',
                    'https://x:/x', 'https://x:bad/x', 'https://x%40y/x', '/x%zz', '/x%', 'https://[::1]bad/x'):
            with self.subTest(ref=ref):
                self.assertIsNone(resolve_reference(ORIGIN, ref))

    def test_missing_nonstring_and_unencodable_refs(self):
        for ref in (None, False, 3, [], {}, '', 'x'*2049, '\ud800'):
            with self.subTest(ref=repr(ref)):
                self.assertIsNone(resolve_reference(ORIGIN, ref))

    def test_relative_requires_configured_http_origin_without_credentials_query(self):
        for origin in ('', 'file:///x', 'http://u:p@x', 'http://x/?secret=x', 'http://x/#f'):
            self.assertIsNone(resolve_reference(origin, 'image'))

    def test_first_usable_only_no_decode_fallback(self):
        self.assertEqual(first_reference(ORIGIN, ('file:///x', '', PUBLIC, '/second')), PUBLIC)
        self.assertEqual(first_reference(ORIGIN, ('/not-known-to-decode', PUBLIC)),
                         'http://192.0.2.10:8089/not-known-to-decode')
        self.assertIsNone(first_reference(ORIGIN, ()))

    def test_no_python_network_file_or_dns_io_for_resolution(self):
        with patch('builtins.open', side_effect=AssertionError('file I/O')), \
             patch('socket.getaddrinfo', side_effect=AssertionError('DNS')), \
             patch('socket.socket', side_effect=AssertionError('network')):
            self.assertEqual(first_reference(ORIGIN, ('data:x', PUBLIC)), PUBLIC)


class MetadataIdentityTests(unittest.TestCase):
    def test_optional_bad_art_does_not_change_channel_dto(self):
        row = dict(source_id='source1', id='channel1', number='1', name='Station')
        baseline = dto.public_channels([row], 'synthetic')
        for value in (None, False, 3, [], {}, '', 'x'*2049, '\ud800'):
            builder = ArtBuilder()
            self.assertEqual(dto.public_channels([{**row, 'logo_url': value}], 'synthetic', art=builder), baseline)
            self.assertFalse(builder.freeze().references)

    def test_source_keys_do_not_collide(self):
        for other in (replace(EVENT, channel=replace(KEY, namespace='other')),
                      replace(EVENT, channel=replace(KEY, source_id='source2')),
                      replace(EVENT, channel=replace(KEY, channel_id='other')),
                      replace(EVENT, start=1001), replace(EVENT, id='other')):
            self.assertNotEqual(event_key(EVENT), event_key(other))

    def test_rotation_changes_only_sidecar_not_event_equality_hash_asdict(self):
        channels = dto.public_channels([dict(source_id='source1', id='channel1', number='1')], 'synthetic').items
        inventories, batches = [], []
        for url in (PUBLIC, PUBLIC+'2'):
            art = ArtBuilder()
            raw = [dict(Channel=dict(DeviceID='source1', ChannelID='channel1'), Airings=[
                dict(ProgramID='program', Title='Title', Time=1000, Duration=1800, Image=url)])]
            batches.append(dto.guide_window(raw, channels, 'synthetic', 1000, art=art))
            inventories.append(art.freeze())
        self.assertEqual(batches[0], batches[1])
        self.assertEqual(hash(batches[0].items[0]), hash(batches[1].items[0]))
        self.assertEqual(asdict(batches[0].items[0]), asdict(batches[1].items[0]))
        self.assertNotEqual(inventories[0].references, inventories[1].references)

    def test_inventory_immutable_bounded_and_hides_private_urls_and_ids_repr(self):
        art = ArtBuilder()
        for n in range(9000):
            art.add(('synthetic-private-id', str(n)), PUBLIC)
        inventory = art.freeze()
        self.assertEqual(len(inventory.references), 8192)
        with self.assertRaises(TypeError):
            inventory.references[('x',)] = 'bad'
        self.assertNotIn(PUBLIC, repr(inventory))
        self.assertNotIn('synthetic-private-id', repr(inventory))

    def test_station_public_then_guide_priority_same_full_channel_only(self):
        art = ArtBuilder()
        channels = dto.public_channels([dict(source_id='source1', id='channel1', number='1', logo_url='/logo')],
                                       'synthetic', art=art).items
        dto.guide_window([dict(Channel=dict(DeviceID='source1',ChannelID='channel1',Image='/guide'), Airings=[]),
                          dict(Channel=dict(DeviceID='other',ChannelID='channel1',Image='/foreign'), Airings=[])],
                         channels, 'synthetic', 1000, art=art)
        inventory = art.freeze()
        self.assertEqual(inventory.logos[channel_key(KEY)], ('/logo', '/guide'))
        self.assertNotIn('/foreign', inventory.references.values())
        self.assertEqual(inventory.references[channel_key(KEY)], '/logo')

    def test_station_candidate_dedup_bound_and_invalid_primary_fallback(self):
        art = ArtBuilder(); key = channel_key(KEY)
        for ref in ('special://bad', 'special://bad', PUBLIC, '/third'):
            art.add(key, ref)
        self.assertEqual(art.freeze().logos[key], ('special://bad', PUBLIC))
        self.assertEqual(first_reference(ORIGIN, art.freeze().logos[key]), PUBLIC)

    def test_recording_job_refs_qualified_no_clearart_fanart_guess(self):
        art = ArtBuilder()
        record = dto.files_page(dict(total=1, items=[dict(ID='r',CreatedAt=1000,
            Airing=dict(Title='Film',Image='/film',ClearArt='/clear',FanArt='/fan'))]), 'synthetic', art=art).items[0]
        job = dto.scheduled_jobs([dict(id='j',start_time=1000,end_time=2800,
            item=dict(title='Scheduled',image_url='/scheduled'))], 'synthetic', art=art).items[0]
        self.assertEqual(art.freeze().references[recording_key(record)], '/film')
        self.assertEqual(art.freeze().references[job_key('synthetic', job)], '/scheduled')
        self.assertNotEqual(recording_key(record), recording_key(replace(record, provenance='dvr')))
        self.assertNotEqual(job_key('synthetic', job), job_key('other', job))
        self.assertEqual(len(art.freeze().references), 2)

    def test_missing_invalid_recording_job_art_not_api_failure(self):
        for value in (None, {}, 4, False, []):
            art = ArtBuilder()
            files = dto.files_page(dict(total=1,items=[dict(ID='r',CreatedAt=1000,
                Airing=dict(Title='Film',Image=value))]), 'synthetic', art=art)
            jobs = dto.scheduled_jobs([dict(id='j',start_time=1000,end_time=2000,
                name='Job',item=dict(image_url=value))], 'synthetic', art=art)
            self.assertEqual((files.state,jobs.state),(DataState.READY,DataState.READY))
            self.assertFalse(art.freeze().references)

    def test_invalid_row_art_not_accepted(self):
        art = ArtBuilder()
        dto.public_channels([dict(source_id='s', id='c', logo_url=PUBLIC)], 'synthetic', art=art)
        self.assertFalse(art.freeze().references)


class ArchitectureTests(unittest.TestCase):
    def test_deleted_pipeline_absent_and_no_remaining_imports(self):
        lib = ROOT / 'script.channelsdvr.hub/resources/lib/channels_dvr'
        retired = {'artwork_image', 'artwork_http', 'artwork_cache', 'artwork_worker', 'artwork_status'}
        for name in retired:
            self.assertFalse((lib / (name+'.py')).exists())
        for path in lib.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or '').split('.')[-1], retired)

    def test_artwork_ast_has_no_runtime_io_platform_decoder_or_worker_imports(self):
        lib = ROOT / 'script.channelsdvr.hub/resources/lib/channels_dvr'
        allowed = {'dataclasses', 'types', 'typing', 'models', 're', 'urllib.parse',
                   'artwork', 'artwork_refs', 'guide_render', 'ui_text', 'programme_scope_ui'}
        for name in ('artwork.py', 'artwork_refs.py', 'artwork_ui.py'):
            tree = ast.parse((lib / name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertIn(node.module, allowed)
                if isinstance(node, ast.Import):
                    self.assertTrue(all(a.name in allowed for a in node.names))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, ('open', 'eval', 'exec', '__import__'))

    def test_manifest_runtime_dependencies_unchanged(self):
        import xml.etree.ElementTree as ET
        root = ET.parse(ROOT / 'script.channelsdvr.hub/addon.xml').getroot()
        self.assertEqual([(n.get('addon'), n.get('version')) for n in root.findall('requires/import')],
                         [('xbmc.python', '3.0.1')])
