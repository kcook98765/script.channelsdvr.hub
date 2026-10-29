"""General manager lifecycle/transport/storage/owner tests; no SDK or backend."""
import base64
from copy import deepcopy
from email.message import Message
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from channels_dvr.live_loader import DevelopmentConfig
from channels_dvr.newbuild_compatibility import profile_binding
from channels_dvr.operation_storage import OperationStore, Scope, StorageError
from channels_dvr.series_manager_models import (GeneralSeriesPlan, ManagerError, ManagerView,
    rule_rows, associated_inactive)
from channels_dvr.series_manager_store import SeriesManagerStore, decode, encode, MAX_BYTES
from channels_dvr.series_manager_commands import SeriesManagerCommands
from channels_dvr.series_manager_worker import SeriesManagerWorker
from channels_dvr.series_manager_ui import SeriesManagerUI
from channels_dvr.series_http import SeriesHTTP
from channels_dvr.series_models import RuleSnapshot
from channels_dvr.ui_text import FALLBACK, IDS, capture
from channels_dvr.ui_menu import main_entries

CONFIG = DevelopmentConfig('http://192.0.2.20:8089')
BINDING = profile_binding(CONFIG.origin, CONFIG.namespace, CONFIG.expected_version)
NOW = 1793000000


def full(key='rule-A', **updates):
    return dict(ID=key, **GeneralSeriesPlan('SH000001', 'Provider title').body(),
                Paused=False, NumJobs=0, Priority=0, UpdatedAt=1,
                **updates)


def sparse(value):
    return {k: deepcopy(v) for k, v in value.items()
            if k not in {'NE', 'GT', 'LT', 'IN', 'NI', 'NumJobs', 'Priority'}}


def visible(job):
    return dict(id=job['ID'], start_time=job['Time'], end_time=job['Time'] + job['Duration'])


class FakeBackend:
    def __init__(self):
        self.rows = [full()]
        self.internal = []
        self.public = []
        self.preview = []
        self.writes = []
        self.store = None
        self.on_write = None
        self.fail_reply = False
        self.actual = '2026.10.24.0621'

    def client(self, config):
        backend = self
        class Client:
            binding = BINDING
            namespace = config.namespace
            def verify_status(self): return backend.actual
            def rules(self): return deepcopy(backend.rows)
            def jobs(self): return deepcopy(backend.internal)
            def public_jobs(self): return deepcopy(backend.public)
            def rule_detail(self, key): return deepcopy(next((r for r in backend.rows if r['ID'] == key), None))
            def public_channels(self): return ['qualified-catalog-double']
            def guide_window(self, start): return ['qualified-guide-double', start]
            def preview(self, plan): return deepcopy(backend.preview)
            def mutate(self, proof, *, capture):
                root = OperationStore.inspect(backend.store.store._profile, Scope.SERIES)
                pending = decode(root.payload)
                entry = pending['history'][pending['pending']]
                assert entry['dispatched'] is True and entry['state'] == 'unknown'
                assert backend.store.store._db.in_transaction is False
                backend.writes.append(proof.operation)
                if proof.operation == 'create':
                    row = dict(ID='created-ID', **json.loads(proof.body), Paused=False,
                               NumJobs=len(backend.internal), Priority=0, UpdatedAt=2)
                    backend.rows.append(row)
                    reply = deepcopy(row)
                else:
                    row = next(r for r in backend.rows if r['ID'] == proof.identity)
                    if proof.operation == 'remove':
                        reply = sparse(row)
                        backend.rows.remove(row)
                        backend.internal = [j for j in backend.internal if j['RuleID'] != proof.identity]
                        backend.public = [visible(j) for j in backend.internal]
                    else:
                        if proof.operation == 'rename':
                            new = json.loads(proof.body)
                            assert set(new) == set(row)
                            assert all(new[k] == row[k] for k in row if k != 'Name')
                            row['Name'] = new['Name']
                        else:
                            row['Paused'] = proof.operation == 'pause'
                        row['UpdatedAt'] += 1
                        reply = sparse(row)
                raw = json.dumps(reply).encode()
                wire = dict(raw_base64=base64.b64encode(raw).decode(), status=200,
                            complete=True, media='json', encoding='identity')
                capture(wire)
                capture(wire, reply['ID'])
                if backend.on_write:
                    backend.on_write()
                if backend.fail_reply:
                    raise ManagerError('network')
                return reply
        return Client()


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='series-manager-test-')
        self.store = SeriesManagerStore(self.temp.name, BINDING)
        self.store.open()
        self.backend = FakeBackend()
        self.backend.store = self.store
        self.now = NOW
        self.commands = SeriesManagerCommands(CONFIG, self.store, self.backend.client, clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def adopt(self):
        view = self.commands.details('rule-A')
        self.assertEqual(view.actions, ('adopt',))
        self.commands.execute('adopt', token=view.token, confirmed=True)

    def test_global_list_without_selected_event_or_optional_metadata(self):
        self.backend.rows += [dict(ID='advanced-ID', Name='Title', EQ={'Title': 'Title'}),
                              dict(ID='other-Series', EQ={'SeriesID': 'other'})]
        result = self.commands.list()
        self.assertEqual(len(result.rows), 3)
        self.assertEqual([r.supported for r in result.rows], [True, False, False])
        self.assertEqual(self.store.root['records'], [])
        self.assertEqual(self.backend.writes, [])

    def test_adoption_is_explicit_and_distinct_not_created(self):
        self.adopt()
        record = self.store.record('rule-A')
        self.assertEqual(record['provenance'], 'adopted')
        self.assertEqual(record['initial'], self.backend.rows[0])
        self.assertEqual(self.backend.writes, [])
        self.assertIsNone(self.store.root['pending'])

    def test_no_confirmation_consumes_plan(self):
        view = self.commands.details('rule-A')
        with self.assertRaises(ManagerError):
            self.commands.execute('adopt', token=view.token, confirmed=False)
        with self.assertRaises(ManagerError):
            self.commands.execute('adopt', token=view.token, confirmed=True)
        self.assertEqual(self.store.root['history'], [])

    def test_wrong_token_cannot_adopt_by_name_or_series(self):
        view = self.commands.details('rule-A')
        with self.assertRaises(ManagerError):
            self.commands.execute('adopt', token='wrong', confirmed=True)
        self.assertIsNone(self.store.record('rule-A'))

    def test_expired_and_clock_reversed_reviews_rejected(self):
        for age in (61, -1):
            view = self.commands.details('rule-A')
            self.now += age
            with self.assertRaises(ManagerError):
                self.commands.execute('adopt', token=view.token, confirmed=True)
        self.assertEqual(self.backend.writes, [])

    def test_full_unknown_preserving_name_pause_resume_remove_lifecycle(self):
        self.backend.rows[0]['FutureMetadata'] = {'significant': [None, False, 0, 'value']}
        self.adopt()
        for operation, values in [('rename', dict(name='New provider name')), ('pause', {}), ('resume', {}), ('remove', {})]:
            view = self.commands.details('rule-A')
            self.assertIn(operation, view.actions)
            self.commands.execute(operation, token=view.token, confirmed=True, **values)
        self.assertEqual(self.backend.writes, ['rename', 'pause', 'resume', 'remove'])
        self.assertTrue(self.store.record('rule-A')['removed'])
        self.assertEqual(self.store.record('rule-A')['initial']['Name'], 'Provider title')
        self.assertEqual(self.store.record('rule-A')['last']['FutureMetadata'], {'significant': [None, False, 0, 'value']})
        self.assertEqual(len(self.store.root['history']), 5)

    def test_unmanaged_update_blocked(self):
        view = self.commands.details('rule-A')
        with self.assertRaises(ManagerError):
            self.commands.execute('pause', token=view.token, confirmed=True)
        self.assertEqual(self.backend.writes, [])

    def test_external_edit_requires_fresh_details_and_explicit_reconsent(self):
        self.adopt()
        view = self.commands.details('rule-A')
        self.backend.rows[0]['Name'] = 'External edit'
        with self.assertRaises(Exception):
            self.commands.execute('pause', token=view.token, confirmed=True)
        view = self.commands.details('rule-A')
        self.assertEqual(view.actions, ('refresh_consent',))
        self.commands.execute('refresh_consent', token=view.token, confirmed=True)
        self.assertEqual(self.commands.details('rule-A').actions, ('rename', 'pause', 'remove'))
        self.assertEqual(self.store.record('rule-A')['initial']['Name'], 'Provider title')

    def test_source_actual_drift_blocks_before_intent(self):
        self.adopt()
        view = self.commands.details('rule-A')
        self.backend.actual = '2026.10.25.0621'
        with self.assertRaises(ManagerError):
            self.commands.execute('pause', token=view.token, confirmed=True)
        self.assertEqual(self.backend.writes, [])

    def test_missing_paused_readonly(self):
        self.backend.rows[0].pop('Paused')
        self.assertEqual(self.commands.details('rule-A').actions, ())

    def test_advanced_nonseries_conditions_readonly(self):
        for changed in ({'EQ': {'Title': 'same name'}}, {'GT': {'Time': NOW}},
                        {'EQ': {'SeriesID': 'SH000001', 'ProgramID': 'specific'}}):
            row = full(); row.update(changed)
            self.backend.rows = [row]
            self.assertEqual(self.commands.details('rule-A').actions, ())

    def test_active_and_unknown_linked_jobs_block_all_updates(self):
        self.adopt()
        for job in (dict(ID='j', RuleID='rule-A', Time=NOW-1),
                    dict(ID='j', RuleID='rule-A', Time=NOW+1000, Recording=True),
                    dict(ID='j', Time=NOW+1000),
                    dict(ID='j', RuleID='rule-A', Time=NOW+1000, Active=None)):
            self.backend.internal = [dict(Duration=900, **job)]
            self.backend.public = [visible(j) for j in self.backend.internal]
            for operation in ('pause', 'resume', 'remove', 'rename'):
                view = self.commands.details('rule-A')
                with self.assertRaises(Exception):
                    self.commands.execute(operation, token=view.token, confirmed=True, name='Name')
        self.assertEqual(self.backend.writes, [])

    def test_fanout_future_jobs_can_change_without_frozen_baseline(self):
        self.adopt()
        for count, operation in ((2, 'pause'), (50, 'resume')):
            self.backend.internal = [dict(ID='j'+str(i), RuleID='rule-A', Time=NOW+1000+i, Duration=900,
                                          Recording=False) for i in range(count)]
            self.backend.public = [visible(j) for j in self.backend.internal]
            view = self.commands.details('rule-A')
            self.commands.execute(operation, token=view.token, confirmed=True)
        self.assertEqual(self.backend.writes, ['pause', 'resume'])
        self.assertEqual(len(self.backend.internal), 50)

    def test_public_pair_conflict_or_missing_job_blocks(self):
        for public in ([], [dict(ID='j', rule_id='foreign', Time=NOW+1000)]):
            with self.assertRaises(Exception):
                associated_inactive([dict(ID='j', RuleID='rule-A', Time=NOW+1000)], public, 'rule-A', NOW)

    def test_actual_public_lowercase_pair_qualifies_future_inactivity(self):
        internal = dict(ID='j', RuleID='rule-A', Time=NOW+1000, Duration=900)
        self.assertEqual(associated_inactive([internal], [visible(internal)], 'rule-A', NOW), ('j',))

    def test_response_preserved_and_pending_survives_transport_uncertainty(self):
        self.adopt()
        self.backend.fail_reply = True
        view = self.commands.details('rule-A')
        with self.assertRaises(ManagerError):
            self.commands.execute('pause', token=view.token, confirmed=True)
        root = decode(OperationStore.inspect(self.temp.name, Scope.SERIES).payload)
        entry = root['history'][root['pending']]
        self.assertEqual(entry['returned_id'], 'rule-A')
        self.assertTrue(json.loads(base64.b64decode(entry['wire']['raw_base64']))['Paused'])
        self.assertEqual(self.commands.details('rule-A').actions, ())

    def test_capacity_failure_before_http_and_no_eviction(self):
        self.adopt()
        record = self.store.record('rule-A')
        # More than the pre-dispatch reserve can safely retain, still below 8MiB.
        for i in range(45):
            entry = deepcopy(self.store.root['history'][0])
            entry['token'] = format(i+1, '064x')
            entry['before']['ProviderMetadata'] = 'x'*60000
            self.store.root['history'].append(entry)
        self.store.save()
        before = deepcopy(self.store.root)
        view = self.commands.details('rule-A')
        with self.assertRaises(ManagerError):
            self.commands.execute('pause', token=view.token, confirmed=True)
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(self.store.root, before)
        self.assertEqual(record['id'], 'rule-A')

    def test_record_capacity_64_and_history_limit(self):
        self.adopt()
        original = deepcopy(self.store.root['records'][0])
        for i in range(1, 64):
            record = deepcopy(original)
            record['id'] = 'copy-'+str(i)
            for key in ('initial', 'last'): record[key]['ID'] = record['id']
            record['scope_token'] = format(i, '064x')
            entry = deepcopy(self.store.root['history'][0])
            entry['token'] = record['scope_token']
            entry['id'] = record['id']
            entry['before'] = deepcopy(record['initial'])
            self.store.root['history'].append(entry)
            self.store.root['records'].append(record)
        self.store.save()
        with self.assertRaises(ManagerError): self.store.reserve('adopt')
        original_history = deepcopy(self.store.root['history'][0])
        for i in range(len(self.store.root['history']), 256):
            entry = deepcopy(original_history)
            entry['token'] = format(i, '064x')
            self.store.root['history'].append(entry)
        self.store.save()
        with self.assertRaises(ManagerError): self.store.reserve('pause')
        self.assertEqual(len(self.store.root['records']), 64)

    def test_intent_save_failure_prevents_http_and_poison_claim(self):
        self.adopt()
        view = self.commands.details('rule-A')
        with patch.object(self.store.store, 'save', side_effect=StorageError('checkpoint_failed')):
            with self.assertRaises(ManagerError):
                self.commands.execute('pause', token=view.token, confirmed=True)
        self.assertEqual(self.backend.writes, [])
        self.assertTrue(self.store.failed)

    def test_other_claim_cannot_execute(self):
        other = SeriesManagerStore(self.temp.name, BINDING)
        with self.assertRaises(ManagerError): other.open()
        other.close()

    def test_unrecognized_root_no_reinterpretation(self):
        with self.assertRaises(Exception): decode(encode({'protocol': 'old-manager'}))

    def test_create_zero_multi_long_future_and_multichannel_preview(self):
        event = SimpleNamespace(start=NOW-3600, title='Same title')
        for count in (0, 2, 50):
            self.backend.preview = [dict(ProgramID='EP'+str(i), Time=NOW+2000000+i,
                SeriesID='SH-source-2', Channels=['station-A', 'station-B']) for i in range(count)]
            self.backend.rows = []
            with patch('channels_dvr.series_manager_commands.selected_series', return_value='SH-source-2') as join:
                prepared = self.commands.prepare(event)
                self.assertEqual(len(json.loads(prepared.preview)), count)
                self.assertEqual(prepared.plan.body()['EQ'], {'SeriesID': 'SH-source-2'})
                self.commands.execute('create', token=prepared.token, confirmed=True)
                self.assertEqual(join.call_count, 3)  # Prepare, confirmed create and post-storage dispatch gate.
            self.assertEqual(self.store.record('created-ID')['provenance'], 'created')
            # New TEMP store for independent cases; no history reset in runtime.
            if count != 50:
                self.store.close()
                self.temp.cleanup()
                self.setUp()

    def test_source_rejoin_changes_id_despite_same_title_blocks_create(self):
        event = SimpleNamespace(start=NOW, title='Same title')
        with patch('channels_dvr.series_manager_commands.selected_series', side_effect=['source-1', 'source-2']):
            view = self.commands.prepare(event)
            with self.assertRaises(ManagerError):
                self.commands.execute('create', token=view.token, confirmed=True)
        self.assertEqual(self.backend.writes, [])

    def test_current_event_creation_uses_real_source_qualified_join_despite_title_collision(self):
        from test_metadata_models import channel, airing, guide_row, NOW as GUIDE_NOW
        from channels_dvr.models import ChannelKey, Event
        selected = Event('program', ChannelKey(CONFIG.namespace, 'source-b', '001'),
                         GUIDE_NOW, GUIDE_NOW+900, 'Synthetic show', 'Long')
        channels = [channel(), channel(source_id='source-b')]
        guide = [guide_row([airing(SeriesID='SH-source-a')]),
                 guide_row([airing(SeriesID='SH-source-b')], DeviceID='source-b')]
        def factory(config):
            client = self.backend.client(config)
            client.public_channels = lambda: deepcopy(channels)
            client.guide_window = lambda start: deepcopy(guide)
            return client
        self.commands._factory = factory
        self.now = GUIDE_NOW + 1
        view = self.commands.prepare(selected)
        self.assertEqual(view.plan.conditions()['EQ'], {'SeriesID': 'SH-source-b'})
        self.commands.execute('create', token=view.token, confirmed=True)
        self.assertEqual(self.store.record('created-ID')['initial']['EQ'], {'SeriesID': 'SH-source-b'})


class Response:
    def __init__(self, value, status=200, media='application/json', raw=None, encoding=None):
        self.raw = json.dumps(value).encode() if raw is None else raw
        self.status = status
        self.headers = Message()
        self.headers['Content-Type'] = media
        if encoding: self.headers['Content-Encoding'] = encoding
    def read1(self, amount):
        chunk, self.raw = self.raw[:amount], self.raw[amount:]
        return chunk
    def close(self): pass


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
    def open(self, request, timeout):
        self.requests.append(request)
        return self.responses.pop(0)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='series-manager-test-')
        self.store = SeriesManagerStore(self.temp.name, BINDING)
        self.store.open()
    def tearDown(self):
        self.store.close(); self.temp.cleanup()
    def proof(self, operation='create'):
        self.store.intent(operation, 'a'*64, None if operation == 'create' else RuleSnapshot.parse(full()),
                          GeneralSeriesPlan('SH', 'Name').body() if operation == 'create' else None,
                          '2026.10.24.0621')
        return self.store.dispatch()
    def client(self, response):
        opener = Opener([Response({'version': '2026.10.24.0621'}), response])
        client = SeriesHTTP(CONFIG, opener=opener)
        client.verify_status()
        return client, opener
    def test_exact_routes_and_empty_pause_body(self):
        client, opener = self.client(Response(sparse(full())))
        client.mutate(self.proof('pause'), capture=self.store.capture)
        request = opener.requests[-1]
        self.assertTrue(request.full_url.endswith('/dvr/rules/rule-A/pause'))
        self.assertEqual(request.method, 'PUT')
        self.assertEqual(request.data, b'')
    def test_raw_and_id_before_bad_media_refusal(self):
        client, opener = self.client(Response(full(), media='text/plain'))
        with self.assertRaises(ManagerError): client.mutate(self.proof(), capture=self.store.capture)
        entry = self.store.root['history'][self.store.root['pending']]
        self.assertEqual(entry['returned_id'], 'rule-A')
        self.assertEqual(json.loads(base64.b64decode(entry['wire']['raw_base64']))['ID'], 'rule-A')

    def test_raw_and_id_before_bad_http_status_refusal(self):
        client, opener = self.client(Response(full(), status=500))
        with self.assertRaises(ManagerError): client.mutate(self.proof(), capture=self.store.capture)
        entry = self.store.root['history'][self.store.root['pending']]
        self.assertEqual(entry['returned_id'], 'rule-A')
        self.assertEqual(entry['wire']['status'], 500)
    def test_oversize_keeps_full_bounded_prefix_in_pending(self):
        client, opener = self.client(Response(None, raw=b'x'*4194305))
        with self.assertRaises(ManagerError): client.mutate(self.proof(), capture=self.store.capture)
        entry = self.store.root['history'][self.store.root['pending']]
        self.assertEqual(len(base64.b64decode(entry['wire']['raw_base64'])), 4194304)
        self.assertFalse(entry['wire']['complete'])
    def test_named404_only_literal_null_not_missing_shape(self):
        client, _ = self.client(Response(None, status=404, raw=b'null'))
        self.assertIsNone(client.rule_detail('rule-A'))
        client, _ = self.client(Response({}, status=404))
        with self.assertRaises(ManagerError): client.rule_detail('rule-A')
    def test_bare_id_cannot_write_and_endpoint_alias_rejected(self):
        client, opener = self.client(Response(full()))
        with self.assertRaises(ManagerError): client.mutate('rule-A', capture=self.store.capture)
        with self.assertRaises(Exception): client.rule_detail('new')
        self.assertEqual(len(opener.requests), 1)
    def test_no_retry_on_same_transport(self):
        client, opener = self.client(Response(full()))
        proof = self.proof()
        client.mutate(proof, capture=self.store.capture)
        with self.assertRaises(ManagerError): client.mutate(proof, capture=self.store.capture)
        self.assertEqual(len(opener.requests), 2)

    def test_internal_dispatch_cannot_bypass_durable_proof_or_change_target(self):
        client, opener = self.client(Response(full()))
        for operation in ('create', 'rename', 'pause', 'resume', 'remove'):
            with self.assertRaises(ManagerError):
                client._request(operation, suffix='rule-A')
        proof = self.proof('pause')
        with self.assertRaises(ManagerError):
            client._request('pause', suffix='foreign/pause', proof=proof, capture=self.store.capture)
        self.assertEqual(len(opener.requests), 1)

    def test_proof_cannot_be_replayed_through_another_client(self):
        client, opener = self.client(Response(full()))
        proof = self.proof()
        client.mutate(proof, capture=self.store.capture)
        second, second_opener = self.client(Response(full()))
        with self.assertRaises(ManagerError): second.mutate(proof, capture=self.store.capture)
        self.assertEqual(len(second_opener.requests), 1)

    def test_late_reply_id_checkpoint_precedes_deadline_refusal(self):
        now = [0.0]
        response = Response(full())
        original_read = response.read1
        def read(amount):
            chunk = original_read(amount)
            if not chunk: now[0] = 10.0
            return chunk
        response.read1 = read
        opener = Opener([Response({'version': '2026.10.24.0621'}), response])
        client = SeriesHTTP(CONFIG, opener=opener, clock=lambda: now[0])
        client.verify_status()
        with self.assertRaises(ManagerError): client.mutate(self.proof(), capture=self.store.capture)
        entry = self.store.root['history'][-1]
        self.assertEqual(entry['returned_id'], 'rule-A')
        self.assertTrue(entry['wire']['complete'])

    def test_decodable_prefix_identity_survives_read_exception_unqualified(self):
        response = Response(full())
        original_read = response.read1
        def read(amount):
            if not response.raw: raise OSError('private endpoint must not escape')
            return original_read(amount)
        response.read1 = read
        client, _ = self.client(response)
        with self.assertRaises(ManagerError) as error: client.mutate(self.proof(), capture=self.store.capture)
        self.assertNotIn('private', str(error.exception))
        entry = self.store.root['history'][-1]
        self.assertEqual(entry['returned_id'], 'rule-A')
        self.assertFalse(entry['wire']['complete'])


class UITests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.results = []
        self.answers = []
        self.shown = []
        self.owner = threading.get_ident()
        self.config = CONFIG
        self.scope = object()
        self.event = SimpleNamespace(start=NOW, title='Title')
        parent = self
        class Dialog:
            def select(self, *args, **kwargs): return parent.answers.pop(0) if parent.answers else -1
            def yesno(self, *args, **kwargs):
                parent.assertEqual(kwargs.get('defaultbutton'), 10)
                return False
            def ok(self, *args, **kwargs): pass
            def input(self, *args, **kwargs): return 'Edited Name'
        class Worker:
            config = CONFIG
            def submit(self, operation, **values): parent.calls.append((operation, values)); return True
            def poll(self): return parent.results.pop(0) if parent.results else None
            def close(self): parent.calls.append(('close', {}))
        def owner(): parent.assertEqual(threading.get_ident(), parent.owner)
        self.ui = SeriesManagerUI(assert_owner=owner, dialog=Dialog, profile=lambda: '/unit-own-profile',
            config=lambda: self.config, selection=lambda: self.event,
            viewer=lambda heading, body: self.shown.append(body) or True, strings=capture(), default_no=10,
            source_scope=lambda: self.scope,
            worker_factory=lambda *a: Worker())
    def test_global_list_independent_of_guide(self):
        self.event = None
        self.answers = [0]
        self.ui.request()
        self.assertEqual(self.calls, [('list', {})])
    def test_preview_dismiss_and_no_confirmation_never_mutates(self):
        self.answers = [1]
        self.ui.request()
        self.results = [ManagerView('prepare', token='a'*64, plan=GeneralSeriesPlan('SH', 'Title'), preview='[]')]
        self.ui.poll()
        self.assertEqual([c[0] for c in self.calls], ['prepare', 'abort'])
        self.assertIn('no end date', self.shown[0])
    def test_adoption_no_never_mutates(self):
        self.answers = [0]
        self.ui.request()
        row = rule_rows([full()])[0]
        self.results = [ManagerView('details', row=row, token='a'*64, actions=('adopt',))]
        self.answers = [0]
        self.ui.poll()
        self.assertEqual([c[0] for c in self.calls], ['list', 'abort'])
    def test_reordered_localized_action_dispatch_uses_stable_ids(self):
        self.answers = [0]
        self.ui.request()
        self.ui.strings = dict(FALLBACK, series_remove='renamed-display', series_pause='other-display')
        self.results = [ManagerView('details', row=rule_rows([full()])[0], token='a'*64, actions=('remove', 'pause'))]
        self.answers = [1]
        self.ui.poll()
        self.assertEqual(self.calls[-1][0], 'abort')
    def test_disconnect_before_result_revokes_ui(self):
        self.answers = [0]
        self.ui.request()
        self.config = None
        self.ui.poll()
        self.assertIsNone(self.ui.worker)

    def test_source_generation_revokes_even_with_equal_event_and_config(self):
        self.answers = [1]
        self.ui.request()
        self.scope = object()
        self.results = [ManagerView('prepare', token='a'*64, plan=GeneralSeriesPlan('SH', 'Title'), preview='[]')]
        self.ui.poll()
        self.assertIsNone(self.ui.worker)
        self.assertNotIn('create', [c[0] for c in self.calls])

    def test_pending_request_does_not_replace_selected_review(self):
        self.answers = [1]
        self.ui.request()
        selected = self.ui._event
        self.ui.request()
        self.assertIs(self.ui._event, selected)
        self.assertEqual(len(self.calls), 1)

    def test_all_supported_action_no_confirmations_send_only_abort(self):
        self.answers = [0]
        self.ui.request()
        for action in ('rename', 'pause', 'resume', 'remove', 'refresh_consent'):
            self.results = [ManagerView('details', row=rule_rows([full()])[0], token='a'*64, actions=(action,))]
            self.answers = [0]
            self.ui.poll()
            self.assertEqual(self.calls[-1][0], 'abort')
        self.assertFalse(any(k in {'rename', 'pause', 'resume', 'remove', 'refresh_consent'} for k, _ in self.calls))

    def test_pagination_uses_captured_keys_and_preserves_all_rows(self):
        self.answers = [0]
        self.ui.request()
        rows = rule_rows([full('r-'+str(i)) for i in range(101)])
        self.results = [ManagerView('list', rows=rows)]
        self.answers = [50, 0]
        self.ui.poll()
        self.assertEqual(self.calls[-1], ('details', {'key': 'r-50'}))
    def test_normal_menu_17_unique_stable_ids(self):
        entries = main_entries(False)
        self.assertEqual(len(entries), 18)
        self.assertEqual(len({k for k, _ in entries}), 18)
        self.assertIn(('series.manager', 'series_manager'), entries)
        self.assertTrue(all(k in IDS for _, k in entries))
    def test_all_series_strings_present_in_real_po_and_under_tool_limit(self):
        from channels_dvr.series_manager_catalog import ROWS
        path = Path(__file__).absolute().parents[1]/'script.channelsdvr.hub/resources/language/resource.language.en_gb/strings.po'
        raw = path.read_bytes()
        self.assertLess(len(raw), 49152)
        for key, number, english in ROWS:
            self.assertIn(('msgctxt "#'+str(number)+'"').encode(), raw)
            self.assertIn(json.dumps(english, ensure_ascii=False).encode(), raw)


class WorkerTests(unittest.TestCase):
    def test_close_during_write_persists_identity_and_no_owner_io_or_join(self):
        owner = threading.get_ident()
        with tempfile.TemporaryDirectory(prefix='series-manager-test-') as profile:
            backend = FakeBackend()
            ready, release = threading.Event(), threading.Event()
            def factory(path, binding):
                self.assertNotEqual(owner, threading.get_ident())
                store = SeriesManagerStore(path, binding)
                backend.store = store
                return store
            worker = SeriesManagerWorker(CONFIG, profile, client_factory=backend.client, store_factory=factory)
            def cleanup():
                worker.close()
                release.set()
                if worker._thread is not None:
                    worker._thread.join(5)
                    self.assertFalse(worker._thread.is_alive(), 'test worker teardown incomplete')
            def poll():
                deadline = time.monotonic()+5
                while time.monotonic() < deadline:
                    result = worker.poll()
                    if result is not None: return result
                    time.sleep(.002)
                self.fail('worker result timeout')
            try:
                self.assertTrue(worker.submit('details', key='rule-A'))
                view = poll()
                self.assertTrue(worker.submit('adopt', token=view.token, confirmed=True))
                poll()
                self.assertTrue(worker.submit('details', key='rule-A'))
                view = poll()
                def on_write(): ready.set(); release.wait(5)
                backend.on_write = on_write
                self.assertTrue(worker.submit('pause', token=view.token, confirmed=True))
                self.assertFalse(worker.submit('remove', token=view.token, confirmed=True))
                self.assertTrue(ready.wait(5))
            finally:
                cleanup()  # Always finish worker BEFORE TEMP deletion or fake restoration.
            root = decode(OperationStore.inspect(profile, Scope.SERIES).payload)
            self.assertEqual(root['history'][-1]['returned_id'], 'rule-A')
            self.assertEqual(root['history'][-1]['state'], 'complete')
            self.assertTrue(root['records'][0]['last']['Paused'])
