"""Private immutable Series DTOs. No Kodi, network, filesystem or storage APIs."""
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import re

MAX_JSON_BYTES = 65536
MAX_INT = 9223372036854775806
MAPS = ('EQ', 'NE', 'GT', 'LT', 'IN', 'NI')
OPTIONS = ('PaddingStart', 'PaddingEnd', 'KeepOnly', 'KeepNum', 'Rerecord', 'Limit')
FULL_FIELDS = frozenset(('ID', 'Name', 'Paused', 'NumJobs', 'Priority', 'UpdatedAt', *MAPS, *OPTIONS))
OPTIONAL_NULL = frozenset(('GT', 'IN', 'LT', 'NE', 'NI'))
OPTIONAL_ZERO = frozenset(('NumJobs', 'Priority'))


class SeriesContractError(Exception):
    """Closed diagnostics; no private input is included in exception text."""
    def __init__(self, code='contract_difference'):
        self.code = code if code in ('shape', 'contract_difference', 'stale_reference', 'consent_required') else 'contract_difference'
        super().__init__(self.code)


def require(value, code='contract_difference'):
    if not value:
        raise SeriesContractError(code)


def canonical(value):
    """Bounded JSON only, exact null/bool/int/float distinctions, no projection."""
    remaining = [20000]
    def visit(item, depth):
        remaining[0] -= 1
        require(remaining[0] >= 0 and depth <= 16, 'shape')
        kind = type(item)
        if item is None or kind is bool:
            return
        if kind is int:
            require(-MAX_INT <= item <= MAX_INT, 'shape')
        elif kind is float:
            require(math.isfinite(item), 'shape')
        elif kind is str:
            require(len(item) <= MAX_JSON_BYTES and '\x00' not in item, 'shape')
        elif kind is dict:
            require(all(type(k) is str and 0 < len(k) <= 256 for k in item), 'shape')
            for k, v in item.items():
                visit(k, depth + 1); visit(v, depth + 1)
        elif kind is list:
            for v in item: visit(v, depth + 1)
        else:
            require(False, 'shape')
    try:
        visit(value, 0)
        text = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
        require(len(text.encode('ascii')) <= MAX_JSON_BYTES, 'shape')
        return text
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise SeriesContractError('shape') from None


def text(value, limit):
    require(type(value) is str and 0 < len(value) <= limit and '\x00' not in value, 'shape')


def identity(value):
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9_~-][A-Za-z0-9._~-]{0,255}', value)
            and value not in ('.', '..'), 'shape')
    return value


def integer(value, maximum=1000000):
    require(type(value) is int and 0 <= value <= maximum, 'shape')


def body_fields(value):
    require(type(value) is dict, 'shape')
    text(value.get('Name'), 4096)
    for key in MAPS:
        require(key in value and (value[key] is None or type(value[key]) is dict), 'shape')
    eq = value['EQ']
    require(type(eq) is dict and 'SeriesID' in eq, 'shape')
    text(eq['SeriesID'], 512)
    if 'Title' in eq: text(eq['Title'], 4096)
    for key in ('PaddingStart', 'PaddingEnd'): integer(value.get(key), 86400)
    for key in ('KeepNum', 'Limit'): integer(value.get(key))
    require(type(value.get('KeepOnly')) is str and len(value['KeepOnly']) <= 256, 'shape')
    require(type(value.get('Rerecord')) is bool, 'shape')


@dataclass(frozen=True, repr=False)
class RuleSnapshot:
    """Canonical private JSON is deeply immutable; payload() returns a fresh copy."""
    _json: str = field(repr=False)

    def __post_init__(self):
        require(type(self._json) is str and 0 < len(self._json) <= MAX_JSON_BYTES, 'shape')
        try:
            value = json.loads(self._json)
            require(type(value) is dict and FULL_FIELDS <= set(value), 'shape')
            body_fields(value); identity(value['ID'])
            require(type(value['Paused']) is bool, 'shape')
            integer(value['NumJobs']); integer(value['Priority']); integer(value['UpdatedAt'], MAX_INT)
            require(canonical(value) == self._json, 'shape')
        except (TypeError, ValueError, RecursionError):
            raise SeriesContractError('shape') from None

    @classmethod
    def parse(cls, value):
        return cls(canonical(value))

    def payload(self):
        return json.loads(self._json)

    def __repr__(self):
        return 'RuleSnapshot(private=True)'


@dataclass(frozen=True, repr=False)
class SparseMutationReply:
    _json: str = field(repr=False)

    def __post_init__(self):
        require(type(self._json) is str and 0 < len(self._json) <= MAX_JSON_BYTES, 'shape')
        try:
            value = json.loads(self._json)
            require(type(value) is dict and bool(value), 'shape')
            require(FULL_FIELDS - OPTIONAL_NULL - OPTIONAL_ZERO <= set(value), 'shape')
            identity(value['ID'])
            require(canonical(value) == self._json, 'shape')
        except (TypeError, ValueError, RecursionError):
            raise SeriesContractError('shape') from None

    @classmethod
    def parse(cls, value):
        return cls(canonical(value))

    def payload(self):
        return json.loads(self._json)

    def __repr__(self):
        return 'SparseMutationReply(private=True)'


@dataclass(frozen=True, repr=False)
class RuleRef:
    binding: str = field(repr=False)
    identity: str = field(repr=False)
    revision: int = field(repr=False)
    digest: str = field(repr=False)

    def __post_init__(self):
        identity(self.identity); integer(self.revision, MAX_INT)
        require(all(type(x) is str and re.fullmatch('[0-9a-f]{64}', x) for x in (self.binding, self.digest)), 'shape')

    @classmethod
    def from_snapshot(cls, snapshot, binding):
        require(type(snapshot) is RuleSnapshot, 'shape')
        row = snapshot.payload()
        return cls(binding, row['ID'], row['UpdatedAt'], hashlib.sha256(snapshot._json.encode('ascii')).hexdigest())

    def __repr__(self):
        return 'RuleRef(private=True)'


class Provenance(Enum):
    CREATED = 'created'
    ADOPTED = 'adopted'


@dataclass(frozen=True, repr=False)
class RuleAuthority:
    """Explicit local receipt metadata, not standalone network/storage authority."""
    ref: RuleRef = field(repr=False)
    provenance: Provenance
    receipt_id: str = field(repr=False)

    def __post_init__(self):
        require(type(self.ref) is RuleRef and type(self.provenance) is Provenance, 'shape')
        require(type(self.receipt_id) is str and re.fullmatch('[0-9a-f]{64}', self.receipt_id), 'shape')

    def __repr__(self):
        return 'RuleAuthority(provenance=' + self.provenance.value + ', private=True)'
