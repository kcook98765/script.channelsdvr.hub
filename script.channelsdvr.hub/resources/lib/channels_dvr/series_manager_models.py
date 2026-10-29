"""General Series plans and private frozen projections. No I/O or Kodi imports."""
from dataclasses import dataclass, field
import hashlib
import json

from .series_models import RuleSnapshot, MAPS, Provenance, canonical, identity, text
from .pass_http import conditions, series_text
from .programme_pass_commands import FLAGS, inactive, owner_reference
from .recording_commands import internal_jobs, public_jobs

PROTOCOL = 'series-manager-v1'
MAX_RECORDS = 64
MAX_HISTORY = 256
TTL = 60


class ManagerError(Exception):
    def __init__(self, code='schema'):
        self.code = code if code in {'schema', 'source', 'stale', 'consent', 'capacity',
            'activity', 'storage', 'pending', 'unsupported', 'network', 'closed', 'expired'} else 'schema'
        super().__init__(self.code)


def need(value, code='schema'):
    if not value:
        raise ManagerError(code)


def digest(value):
    return hashlib.sha256(canonical(value).encode('ascii')).hexdigest()


@dataclass(frozen=True, repr=False)
class GeneralSeriesPlan:
    series_id: str = field(repr=False)
    title: str = field(repr=False)

    def __post_init__(self):
        series_text(self.series_id)
        text(self.title, 4096)
        canonical(self.body())

    def conditions(self):
        return conditions(self.series_id)

    def body(self):
        return dict(Name=self.title, **self.conditions(), PaddingStart=0, PaddingEnd=0,
                    KeepOnly='', KeepNum=0, Rerecord=False, Limit=0)

    def __repr__(self):
        return 'GeneralSeriesPlan(private=True)'


@dataclass(frozen=True, repr=False)
class RuleRow:
    key: str = field(repr=False)
    raw: str = field(repr=False)
    supported: bool

    def payload(self):
        return json.loads(self.raw)

    def __repr__(self):
        return 'RuleRow(supported=' + str(self.supported) + ', private=True)'


@dataclass(frozen=True, repr=False)
class SeriesRecord:
    """Typed private durable provenance; a GET/hash alone cannot construct consent."""
    key: str = field(repr=False)
    binding: str = field(repr=False)
    provenance: Provenance
    receipt: str = field(repr=False)
    initial: RuleSnapshot = field(repr=False)
    last: RuleSnapshot = field(repr=False)
    scope_token: str = field(repr=False)
    removed: bool

    @classmethod
    def from_payload(cls, row, binding):
        need(type(row) is dict and set(row) == {'id', 'provenance', 'receipt', 'initial', 'last', 'removed', 'scope_token'})
        identity(row['id'])
        need(type(row['removed']) is bool)
        need(all(type(s) is str and len(s) == 64 and all(c in '0123456789abcdef' for c in s)
                 for s in (binding, row['receipt'], row['scope_token'])))
        initial, last = RuleSnapshot.parse(row['initial']), RuleSnapshot.parse(row['last'])
        need(initial.payload()['ID'] == last.payload()['ID'] == row['id'])
        return cls(row['id'], binding, Provenance(row['provenance']), row['receipt'],
                   initial, last, row['scope_token'], row['removed'])

    def __repr__(self):
        return 'SeriesRecord(provenance=' + self.provenance.value + ', private=True)'


@dataclass(frozen=True, repr=False)
class EpisodePreview:
    index: int
    raw: str = field(repr=False)

    def payload(self):
        return json.loads(self.raw)

    def __repr__(self):
        return 'EpisodePreview(private=True)'


def rule_rows(payload):
    """Parse the WHOLE list first. Advanced rows remain visible, never owned by inference."""
    need(type(payload) is list and len(payload) <= 10000)
    result = []
    keys = set()
    for row in payload:
        need(type(row) is dict)
        key = identity(row.get('ID'))
        need(key not in keys)
        keys.add(key)
        raw = canonical(row)
        supported = False
        try:
            RuleSnapshot.parse(row)
            supported = (set(row['EQ']) == {'SeriesID'}
                         and all(row[k] is None for k in MAPS if k != 'EQ'))
        except Exception:
            pass  # Optional/advanced schema affects this row, never empties the DVR.
        result.append(RuleRow(key, raw, supported))
    return tuple(result)


def job_rows(payload):
    need(type(payload) is list and len(payload) <= 10000)
    result = {}
    for row in payload:
        need(type(row) is dict)
        raw = canonical(row)
        key = row.get('ID')
        need(type(key) is str and bool(key) and key not in result)
        result[key] = json.loads(raw)
    return result


def associated_inactive(jobs_payload, public_payload, rule_id, now, *, check_activity=True):
    """Exact full-list pair/link proof; no frozen all-jobs baseline or cleanup guess.

    Unstarted future times are positive evidence only after every present activity
    flag is literal false and all qualified file/state guards pass. Missing internal
    RuleID links cannot exclude an association and block the action.
    """
    try:
        jobs, public = internal_jobs(jobs_payload), public_jobs(public_payload)
        for row in (*jobs.values(), *public.values()):
            canonical(row)  # Per-job 64KiB bound, retaining all unknown fields.
    except Exception:
        raise ManagerError('activity') from None
    try:
        internal_owners = {key: owner_reference(row) for key, row in jobs.items()}
        public_owners = {key: owner_reference(row) for key, row in public.items()}
    except Exception:
        raise ManagerError('activity') from None
    # Complete foreign rosters are validated, but only positive exact links can
    # exclude a row from this target. Orphan public rows with no known foreign
    # owner remain possible target associations, never silently filtered out.
    for key in public:
        if key not in jobs:
            need(public_owners[key] is not None and public_owners[key] != rule_id, 'activity')
    associated = []
    for key, internal in jobs.items():
        need('RuleID' in internal and internal_owners[key] is not None, 'activity')
        left = internal_owners[key]
        paired = public.get(key)
        if paired is not None and ('RuleID' in paired or 'rule_id' in paired):
            need(left == public_owners[key], 'activity')
        if left != rule_id:
            continue
        need(paired is not None, 'activity')
        assert paired is not None
        airing = internal.get('Airing')
        item = paired.get('item')
        # Supplied target metadata must agree. Absence of optional public source
        # data is not a source pin; a supplied contradictory/unverifiable source
        # or programme reference cannot be ignored merely because job IDs match.
        for aliases in (('DeviceID', 'SourceID', 'source_id'), ('ProgramID', 'program_id'), ('SeriesID', 'series_id')):
            left_values = [row[k] for row in (internal, airing) if type(row) is dict for k in aliases if k in row]
            right_values = [row[k] for row in (paired, item) if type(row) is dict for k in aliases if k in row]
            if right_values:
                need(bool(left_values) and type(left_values[0]) is str and bool(left_values[0]), 'activity')
                need(all(canonical(v) == canonical(left_values[0]) for v in (*left_values, *right_values)), 'activity')
        for row in (internal, paired):
            if not check_activity:
                continue  # Read-only reconciliation still checks exact owner/ID/time pairs below.
            try:
                inactive(row)
            except Exception:
                raise ManagerError('activity') from None
            for flag in (*FLAGS, *(k.lower() for k in FLAGS)):
                need(flag not in row or row[flag] is False, 'activity')
            start = row.get('Time') if row is internal else row.get('start_time')
            need(type(start) is int and now + 10 < start <= 4102444800, 'activity')
        if check_activity and type(item) is dict:
            try:
                inactive(item)
            except Exception:
                raise ManagerError('activity') from None
            for flag in (*FLAGS, *(k.lower() for k in FLAGS)):
                need(flag not in item or item[flag] is False, 'activity')
        need(internal['Time'] == paired['start_time']
             and internal['Time'] + internal['Duration'] == paired['end_time'], 'activity')
        associated.append(key)
    return tuple(associated)


@dataclass(frozen=True, repr=False)
class ManagerView:
    operation: str
    rows: tuple[RuleRow, ...] = field(default=(), repr=False)
    row: RuleRow | None = field(default=None, repr=False)
    token: str | None = field(default=None, repr=False)
    actions: tuple[str, ...] = ()
    preview: str | None = field(default=None, repr=False)
    plan: GeneralSeriesPlan | None = field(default=None, repr=False)
    provenance: str | None = None
    error: str | None = None
    matches: tuple[EpisodePreview, ...] = field(default=(), repr=False)
    observed_at: int = 0
    provenances: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __repr__(self):
        return 'ManagerView(operation=' + self.operation + ', private=True)'
