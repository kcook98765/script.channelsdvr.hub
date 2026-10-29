"""Explicit fixed-path legacy decoding; no SQL, SDK, UI or automatic migration."""
from dataclasses import dataclass, field
import json
import os
import stat

from .operation_storage import LEGACY_PATHS, Scope, StorageError
from .metadata_http import _unique_object, _reject_constant, _finite_float


@dataclass(frozen=True)
class LegacyRoot:
    scope: Scope
    original: bytes = field(repr=False)
    canonical: bytes = field(repr=False)
    binding: str | None = field(default=None, repr=False)


def decode_root(scope, raw):
    """Same domain validators and encoders; original bytes stay separate forever."""
    if type(raw) is not bytes or not 0 < len(raw) <= scope.max_bytes:
        raise StorageError('checkpoint_failed')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                       parse_constant=_reject_constant, parse_float=_finite_float)
    if type(value) is not dict:
        raise StorageError('checkpoint_failed')
    if scope is Scope.RECORDING:
        from .recording_journal import journal_state
        journal_state(value)
        canonical = json.dumps(value, ensure_ascii=True, allow_nan=False).encode('utf-8')
    elif scope is Scope.PROGRAMME:
        from .programme_pass_journal import journal_state
        from .programme_pass_commands import canonical as encode
        journal_state(value)
        canonical = encode(value).encode('utf-8')
    else:
        raise StorageError('checkpoint_failed')
    if len(canonical) > scope.max_bytes:
        raise StorageError('checkpoint_failed')
    return value, canonical


def inventory(profile):
    found = []
    for index, parts in enumerate(LEGACY_PATHS):
        try:
            os.lstat(os.path.join(profile, *parts))
        except FileNotFoundError:
            continue
        found.append(index)
    return tuple(found)


def root_binding(scope, value):
    # Caller has already used the unchanged domain decoder. No origin/config read.
    if scope is Scope.RECORDING:
        return value['command']['server_binding_sha256'] if value['version'] == 1 else value['server_binding_sha256']
    if scope is Scope.PROGRAMME:
        return value['server_binding']
    raise StorageError('checkpoint_failed')


def read_legacy(profile):
    """Called only for explicit import or verification of committed provenance.

    Profile trust/inherited permissions apply. Portable checks reject nonregular
    entries but cannot promise a descriptor-relative no-follow race guarantee.
    """
    found = inventory(profile)
    if not found or any(index in found for index in (1, 3)):
        raise StorageError('checkpoint_failed')
    roots, bindings = [], []
    for index, scope in ((0, Scope.RECORDING), (2, Scope.PROGRAMME)):
        if index not in found:
            continue
        path = os.path.join(profile, *LEGACY_PATHS[index])
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise StorageError('checkpoint_failed')
        with open(path, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise StorageError('checkpoint_failed')
            raw = stream.read(scope.max_bytes + 1)
        value, canonical = decode_root(scope, raw)
        binding = root_binding(scope,value)
        if binding is not None:
            bindings.append(binding)
        roots.append(LegacyRoot(scope, raw, canonical, binding))
    if len(set(bindings)) > 1 or inventory(profile) != found:
        raise StorageError('checkpoint_failed')
    return tuple(roots)


def summaries(inspection):
    """Sanitized, closed state/count DTOs; never identifiers, paths or payloads."""
    rows = []
    for root in inspection.roots:
        if root.scope is Scope.SERIES:
            continue  # No qualified Series DTO or operator recovery contract yet.
        state, count = 'none', 0
        if root.payload is not None:
            value, _ = decode_root(root.scope, root.payload)
            if root.scope is Scope.RECORDING:
                from .recording_journal import journal_state
                active, history, _ = journal_state(value)
            else:
                active, history = value['active'], value['history']
            state = active['command']['state'] if active else 'none'
            count = len(history)
        rows.append((root.scope, state, count, root.owner is not None, root.barrier is not None))
    return tuple(rows)
