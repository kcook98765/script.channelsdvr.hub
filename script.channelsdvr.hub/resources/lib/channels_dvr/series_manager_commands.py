"""Worker-confined general Series lifecycle. No SDK, implicit mutations or cleanup."""
from dataclasses import dataclass, field
import secrets
import time

from . import series_contracts as contracts
from .series_models import RuleSnapshot, RuleRef, SparseMutationReply, SeriesContractError, canonical, text
from .series_manager_models import (GeneralSeriesPlan, ManagerView, ManagerError, TTL,
    rule_rows, associated_inactive, digest, need, EpisodePreview)
from .pass_preview import selected_series
from .models import Event
from .series_manager_store import encode


@dataclass(frozen=True, repr=False)
class Review:
    token: str = field(repr=False)
    at: float
    actual: str
    operation: str
    snapshot: RuleSnapshot | None = field(default=None, repr=False)
    plan: GeneralSeriesPlan | None = field(default=None, repr=False)
    event: Event | None = field(default=None, repr=False)
    name: str | None = field(default=None, repr=False)
    drift: bool = False


class SeriesManagerCommands:
    def __init__(self, config, store, client_factory, *, clock=time.time):
        self.config, self.store, self._factory, self._clock = config, store, client_factory, clock
        self._review: Review | None = None

    def _client(self):
        client = self._factory(self.config)
        need(client.binding == self.store.binding, 'source')
        actual = self._status(client)
        return client, actual

    def _status(self, client):
        from .series_http import SourceStatusError
        request = self.store.status_challenge(self.config)
        try:
            actual = client.verify_status()
        except SourceStatusError as error:
            self.store.quarantine_status(request, error.refusal)
            raise
        return actual

    def _mutable(self):
        need(self.store.root is not None and not self.store.failed, 'storage')
        need(self.store.root['pending'] is None, 'pending')
        need(not self.store.root['blocked'] and not self.store.store.pending_reconciliation, 'pending')

    def status(self):
        self._review = None
        disposition = self.store.reconcile(self.config, self._factory)
        return ManagerView('status', error=None if disposition == 'clear' else 'pending')

    def _fresh(self, client, key):
        rows = rule_rows(client.rules())
        selected = next((r for r in rows if r.key == key), None)
        need(selected is not None, 'stale')
        assert selected is not None
        named = client.rule_detail(key)
        need(named is not None and canonical(named) == selected.raw, 'stale')
        return selected

    def list(self):
        self._review = None
        client, actual = self._client()
        rows = rule_rows(client.rules())
        need(self._status(client) == actual, 'source')
        return ManagerView('list', rows=rows, provenances=tuple((r['id'], r['provenance']) for r in self.store.root['records']))

    def details(self, key):
        self._review = None
        client, actual = self._client()
        row = self._fresh(client, key)
        need(self._status(client) == actual, 'source')
        record = self.store.record(key)
        actions: tuple[str, ...] = ()
        token = None
        if (row.supported and self.store.root['pending'] is None and not self.store.root['blocked']
                and not self.store.store.pending_reconciliation):
            snap = RuleSnapshot.parse(row.payload())
            token = secrets.token_hex(32)
            drift = record is not None and canonical(record['last']) != row.raw
            self._review = Review(token, self._clock(), actual, 'details', snapshot=snap, drift=drift)
            if record is None:
                actions = ('adopt',)
            elif not record['removed']:
                actions = ('refresh_consent',) if drift else ('rename', 'resume' if row.payload()['Paused'] else 'pause', 'remove')
        return ManagerView('details', row=row, token=token, actions=actions,
                           provenance=record['provenance'] if record else None)

    def prepare(self, event):
        self._review = None
        self._mutable()
        self.store.reserve('create')
        client, actual = self._client()
        series = selected_series(client.public_channels(), client.guide_window(event.start), self.config.namespace, event)
        plan = GeneralSeriesPlan(series, event.title)
        preview = client.preview(plan)
        # Full bounded provider rows, no time/channel/cardinality filters. Zero is valid.
        need(type(preview) is list and len(preview) <= 10000)
        for row in preview:
            need(type(row) is dict)
            canonical(row)
            need('SeriesID' not in row or row['SeriesID'] == series)
        preview_raw = encode(preview)
        need(len(preview_raw) <= 4194304)
        preview_json = preview_raw.decode('ascii')
        need(self._status(client) == actual, 'source')
        token = secrets.token_hex(32)
        self._review = Review(token, self._clock(), actual, 'create', plan=plan, event=event)
        return ManagerView('prepare', token=token, plan=plan, preview=preview_json, actions=('create',),
                           matches=tuple(EpisodePreview(i, canonical(row)) for i, row in enumerate(preview)), observed_at=int(self._clock()))

    def abort(self):
        self._review = None
        return ManagerView('abort')

    def execute(self, operation, *, token=None, confirmed=False, name=None):
        try:
            return self._execute(operation, token=token, confirmed=confirmed, name=name)
        except SeriesContractError:
            self.store.quarantine()
            raise ManagerError('schema') from None
        except ManagerError as error:
            if error.code in {'schema', 'stale', 'source'} and self.store.root.get('pending') is not None:
                self.store.quarantine()
            raise

    def _execute(self, operation, *, token=None, confirmed=False, name=None):
        review, self._review = self._review, None  # Consume once, including No/stale/failure.
        need(confirmed is True, 'consent')
        need(review is not None and token == review.token and type(token) is str, 'stale')
        assert review is not None
        now = self._clock()
        need(0 <= now - review.at <= TTL, 'expired')
        self._mutable()
        client, actual = self._client()
        need(actual == review.actual, 'source')
        before = None
        body = None
        if operation == 'create':
            need(review.operation == 'create' and type(review.plan) is GeneralSeriesPlan, 'stale')
            assert review.plan is not None
            event = review.event
            need(event is not None, 'stale')
            assert event is not None
            series = selected_series(client.public_channels(), client.guide_window(event.start), self.config.namespace, event)
            need(GeneralSeriesPlan(series, event.title) == review.plan, 'stale')
            body = review.plan.body()
            # No name/SeriesID duplicate ownership or immutable all-job assumption.
            self._before_ids = {r.key for r in rule_rows(client.rules())}
        else:
            need(review.operation == 'details' and review.snapshot is not None, 'stale')
            assert review.snapshot is not None
            row = self._fresh(client, review.snapshot.payload()['ID'])
            need(row.supported, 'unsupported')
            before = RuleSnapshot.parse(row.payload())
            contracts.assert_fresh(RuleRef.from_snapshot(review.snapshot, client.binding), before, client.binding)
            record = self.store.record(row.key)
            if operation == 'adopt':
                need(record is None, 'consent')
            elif operation == 'refresh_consent':
                need(record is not None and not record['removed'] and review.drift, 'consent')
            else:
                need(record is not None and not record['removed'] and not review.drift, 'consent')
                need(canonical(record['last']) == before._json, 'stale')
                need(operation in {'rename', 'pause', 'resume', 'remove'}, 'unsupported')
                associated_inactive(client.jobs(), client.public_jobs(), row.key, self._clock())
                if operation == 'rename':
                    text(name, 4096)
                    body = contracts.rename_payload(RuleRef.from_snapshot(before, client.binding), before, client.binding, name)
                elif operation in {'pause', 'resume'}:
                    need(before.payload()['Paused'] is (operation == 'resume'), 'stale')
        need(self._status(client) == actual, 'source')
        need(0 <= self._clock() - review.at <= TTL, 'expired')
        if operation in {'adopt', 'refresh_consent'}:
            # Local consent is not Created and sends no mutation to the server.
            self.store.intent('adopt', token, before, None, actual)
            self.store.finish(before, 'adopted' if operation == 'adopt' else None)
            return ManagerView(operation)
        self.store.intent(operation, token, before, body, actual,
                          baseline_ids=sorted(self._before_ids) if operation == 'create' else None)
        # Recheck after storage latency. Failure leaves intent pending, never retryable.
        need(0 <= self._clock() - review.at <= TTL, 'expired')
        if before is not None:
            fresh = self._fresh(client, before.payload()['ID'])
            need(fresh.raw == before._json, 'stale')
            associated_inactive(client.jobs(), client.public_jobs(), fresh.key, self._clock())
        else:
            assert review.plan is not None and review.event is not None
            series = selected_series(client.public_channels(), client.guide_window(review.event.start), self.config.namespace, review.event)
            need(series == review.plan.series_id, 'stale')
        need(self._status(client) == actual, 'source')
        need(0 <= self._clock() - review.at <= TTL, 'expired')
        proof = self.store.dispatch()
        need(0 <= self._clock() - review.at <= TTL, 'expired')
        reply = client.mutate(proof, capture=self.store.capture)
        if operation == 'create':
            need(type(reply) is dict, 'schema')
            key = reply.get('ID')
            need(key not in self._before_ids, 'stale')
            after = RuleSnapshot.parse(self._fresh(client, key).payload())
            contracts.validate_create(body, key, reply, after, client.binding, receipt_id=digest(body))
            need(self._status(client) == actual, 'source')
            self.store.finish(after, 'created')
        elif operation == 'remove':
            assert before is not None
            contracts.validate_remove_reply(reply, before)
            key = before.payload()['ID']
            need(all(r.key != key for r in rule_rows(client.rules())), 'stale')
            need(client.rule_detail(key) is None, 'stale')
            need(not associated_inactive(client.jobs(), client.public_jobs(), key, self._clock()), 'activity')
            need(self._status(client) == actual, 'source')
            self.store.finish()
        else:
            assert before is not None
            after = RuleSnapshot.parse(self._fresh(client, before.payload()['ID']).payload())
            contracts.validate_mutation(before, SparseMutationReply.parse(reply), after, operation,
                                        planned_name=name if operation == 'rename' else None)
            need(self._status(client) == actual, 'source')
            self.store.finish(after)
        return ManagerView(operation)
