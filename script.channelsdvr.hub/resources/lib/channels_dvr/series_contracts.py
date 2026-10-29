"""Pure Series contracts shared by future production commands and v3 fixture.

Full GET records never lose null/default/unknown fields. Sparse mutation replies
have a deliberately different contract. No network, store or GUI is implemented.
"""
from .series_models import (RuleSnapshot, SparseMutationReply, RuleRef, RuleAuthority,
    Provenance, SeriesContractError, OPTIONAL_NULL, OPTIONAL_ZERO, MAPS, OPTIONS,
    canonical, body_fields, require, text, identity)


def assert_fresh(ref, snapshot, binding):
    require(type(ref) is RuleRef and type(snapshot) is RuleSnapshot, 'stale_reference')
    require(ref == RuleRef.from_snapshot(snapshot, binding), 'stale_reference')


def adopt(snapshot, binding, *, confirmed, receipt_id):
    require(confirmed is True, 'consent_required')
    return RuleAuthority(RuleRef.from_snapshot(snapshot, binding), Provenance.ADOPTED, receipt_id)


def validate_create(body, returned_id, reply, after, binding, *, receipt_id):
    require(type(body) is dict and set(body) == {'Name', *MAPS, *OPTIONS}, 'shape')
    canonical(body); body_fields(body); identity(returned_id)
    complete_reply = RuleSnapshot.parse(reply)
    require(type(after) is RuleSnapshot, 'shape')
    a, r = after.payload(), complete_reply.payload()
    require(a['ID'] == r['ID'] == returned_id)
    require(all(k in a and canonical(a[k]) == canonical(v) for k, v in body.items()))
    require(a['Paused'] is False and a['Priority'] == 0)
    # NumJobs can be positive for actual future Series. Fixture containment is a
    # separate zero-match admission rule, not a product single-airing restriction.
    require(complete_reply._json == after._json)
    return RuleAuthority(RuleRef.from_snapshot(after, binding), Provenance.CREATED, receipt_id)


def validate_sparse_reply(reply, after, owned_id):
    require(type(reply) is SparseMutationReply and type(after) is RuleSnapshot, 'shape')
    r, a = reply.payload(), after.payload()
    require(r['ID'] == a['ID'] == identity(owned_id))
    require(set(r) <= set(a))
    for name, value in a.items():
        if name in r:
            require(canonical(r[name]) == canonical(value))
        elif name in OPTIONAL_NULL:
            require(value is None)
        elif name in OPTIONAL_ZERO:
            require(type(value) is int and value == 0)
        else:
            require(False)  # Unknown fields may NOT be silently omitted.


def validate_readback(before, after, operation, *, planned_name=None):
    require(type(before) is RuleSnapshot and type(after) is RuleSnapshot, 'shape')
    require(operation in ('pause', 'resume', 'rename'), 'shape')
    b, a = before.payload(), after.payload()
    require(set(a) == set(b))
    require(a['UpdatedAt'] >= b['UpdatedAt'])
    changed = 'Name' if operation == 'rename' else 'Paused'
    if operation == 'rename':
        text(planned_name, 4096); require(a['Name'] == planned_name)
    else:
        require(planned_name is None and a['Paused'] is (operation == 'pause'))
    require(all(canonical(a[k]) == canonical(b[k]) for k in b if k not in (changed, 'UpdatedAt')))


def validate_mutation(before, reply, after, operation, *, planned_name=None):
    validate_readback(before, after, operation, planned_name=planned_name)
    validate_sparse_reply(reply, after, before.payload()['ID'])


def rename_payload(ref, before, binding, planned_name):
    assert_fresh(ref, before, binding); text(planned_name, 4096)
    result = before.payload()
    result['Name'] = planned_name  # Preserve even server/unknown fields in the PUT.
    return result


def validate_remove_reply(reply, before=None):
    """Validate a deleted-rule receipt, never ownership or backend absence.

    Object replies require the complete, independently owned before snapshot.
    Unlike an update readback, even UpdatedAt must equal that snapshot exactly.
    None preserves the legacy caller-qualified empty/JSON-null transport case;
    this does not infer an empty reply from an arbitrary/missing response.
    """
    if reply is None:
        require(before is None or type(before) is RuleSnapshot, 'shape')
        return
    require(type(before) is RuleSnapshot, 'shape')
    sparse = reply if type(reply) is SparseMutationReply else SparseMutationReply.parse(reply)
    validate_sparse_reply(sparse, before, before.payload()['ID'])
