"""Closed pure manager verifier and monotonic successors. No I/O or callbacks."""
from copy import deepcopy
import hashlib
import json
import time

from .series_models import RuleSnapshot, SparseMutationReply, canonical
from . import series_contracts as contracts
from .series_manager_models import (need, ManagerError, rule_rows, associated_inactive,
                                    MAX_HISTORY, digest)
from .series_manager_store import decode, encode, wire_payload, candidate_key


def _fingerprint(value):
    return hashlib.sha256(encode(value)).hexdigest()


def successor(raw, binding, actual, reads):
    """Return exact validated successor plus a CLOSED verifier disposition.

    blocked is monotonic. The input wire/intents are never rewritten. An absent
    returned create ID is never found by name, SeriesID, similarity or list delta.
    """
    root = decode(raw)
    need(root['binding'] == binding, 'source')
    need(type(reads) is dict and set(reads) == {'rules', 'jobs', 'public', 'details'})
    rows = rule_rows(reads['rules'])
    mapping = {r.key: r for r in rows}
    need(type(reads['details']) is dict)
    # Validate complete arrays/pairs/links even when no rule is being controlled.
    associated_inactive(reads['jobs'], reads['public'], '__manager_no_rule__', time.time())
    need(len(root['observations']) < MAX_HISTORY, 'capacity')
    pending = root['pending']
    observation = dict(index=pending if pending is not None else -1, actual=actual,
        rules=_fingerprint(reads['rules']), jobs=_fingerprint(reads['jobs']), public=_fingerprint(reads['public']),
        outcome='observed')
    result = deepcopy(root)
    outcome = 'observed'
    if root['blocked']:
        outcome = 'blocked'
    else:
        try:
            for record in root['records']:
                row = mapping.get(record['id'])
                if record['removed']:
                    need(row is None)
                    need(not associated_inactive(reads['jobs'], reads['public'], record['id'], time.time()), 'activity')
                else:
                    # Drift stays visible, NEVER overwrites a consent snapshot.
                    # Pending Remove alone may account for this ID's absence.
                    removing = pending is not None and root['history'][pending]['operation'] == 'remove' and root['history'][pending]['id'] == record['id']
                    need(row is not None or removing, 'stale')
                    if row is not None:
                        RuleSnapshot.parse(row.payload())
                        associated_inactive(reads['jobs'], reads['public'], record['id'], time.time(), check_activity=False)
            if pending is not None:
                entry = root['history'][pending]
                need(actual == entry['actual'], 'source')
                key = candidate_key(entry)
                if entry['operation'] == 'create' and entry['dispatched'] and key is None:
                    # Honest unknown, no proof of failure or permission to create again.
                    result['observations'].append(observation)
                    return encode(result), 'pending'
                if key is not None:
                    need(set(reads['details']) == {key})
                    detail = reads['details'][key]
                    row = mapping.get(key)
                    need((detail is None and row is None) or (row is not None and canonical(detail) == row.raw), 'stale')
                else:
                    detail = None
                    need(not reads['details'])
                before = RuleSnapshot.parse(entry['before']) if entry['before'] is not None else None
                after = RuleSnapshot.parse(detail) if detail is not None else None
                if not entry['dispatched'] and entry['operation'] != 'adopt':
                    # Durable false burn proves this HTTP operation was never sent.
                    # Consume the intent without manufacturing a created/changed rule.
                    pass
                elif entry['operation'] == 'adopt':
                    need(after is not None and before is not None and before._json == after._json, 'stale')
                    _accept(result, entry, after, 'adopted')
                elif entry['operation'] == 'create':
                    need(after is not None and key not in entry['baseline_ids'])
                    need(entry['wire'] is not None)
                    if entry['wire']['complete'] is not True:
                        result['observations'].append(observation)
                        return encode(result), 'pending'  # Prefix is not a complete create receipt.
                    reply = wire_payload(entry['wire'])
                    contracts.validate_create(entry['body'], key, reply, after, binding, receipt_id=entry['token'])
                    associated_inactive(reads['jobs'], reads['public'], key, time.time(), check_activity=False)
                    # Monotonic late decoded-ID annotation from original wire,
                    # not a guessed identity from the current DVR list.
                    result['history'][pending]['returned_id'] = key
                    _accept(result, entry, after, 'created')
                elif entry['operation'] == 'remove':
                    need(before is not None and after is None)
                    need(not associated_inactive(reads['jobs'], reads['public'], key, time.time()), 'activity')
                    if entry['wire'] is not None and entry['wire']['complete']:
                        contracts.validate_remove_reply(wire_payload(entry['wire']), before)
                    record = next(r for r in result['records'] if r['id'] == key)
                    record['removed'] = True
                else:
                    need(before is not None and after is not None)
                    planned = entry['body']['Name'] if entry['operation'] == 'rename' else None
                    contracts.validate_readback(before, after, entry['operation'], planned_name=planned)
                    if entry['wire'] is not None and entry['wire']['complete']:
                        contracts.validate_mutation(before, SparseMutationReply.parse(wire_payload(entry['wire'])),
                                                    after, entry['operation'], planned_name=planned)
                    _accept(result, entry, after, None)
                result['history'][pending]['state'] = 'complete'
                result['pending'] = None
                outcome = 'reconciled'
        except ManagerError as error:
            if error.code in {'capacity', 'activity'}:
                raise  # Inactivity/capacity is not an immutable contract mismatch.
            result = deepcopy(root)
            result['blocked'], outcome = True, 'blocked'
        except Exception:
            result = deepcopy(root)
            result['blocked'], outcome = True, 'blocked'
    observation['outcome'] = outcome
    result['observations'].append(observation)
    return encode(result), 'blocked' if result['blocked'] else 'clear'


def _accept(root, entry, after, provenance):
    need(type(after) is RuleSnapshot)
    row = after.payload()
    existing = next((r for r in root['records'] if r['id'] == row['ID']), None)
    if existing is None:
        need(provenance in {'created', 'adopted'})
        root['records'].append(dict(id=row['ID'], provenance=provenance, receipt=entry['token'],
            scope_token=entry['token'], initial=row, last=row, removed=False))
    else:
        need(provenance != 'created' and not existing['removed'])
        existing['last'] = row
