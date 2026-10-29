"""Pure, bounded complete rules observation; no query evaluation or ownership."""
from dataclasses import dataclass, field
import json
import math

from .pass_browser import display_text, utc
from .pass_http import series_text
from .pass_preview import PreviewError
from .metadata_http import MetadataError
from .ui_text import FALLBACK, text as ui_text

MAPS = ("EQ", "NE", "GT", "LT", "IN", "NI")
SCOPE = FALLBACK['saved_scope']
STALE_SECONDS = 60


def _bad():
    raise PreviewError("schema_unqualified")


def _bounded(value, depth=0):
    """Validate every value before freezing; reject rather than truncate conditions."""
    if depth > 6:
        _bad()
    if value is None or type(value) is bool:
        return
    if type(value) is int and -(2**63) <= value < 2**63:
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is str and len(value) <= 4096:
        return
    if type(value) is list and len(value) <= 64:
        for item in value:
            _bounded(item, depth + 1)
        return
    if type(value) is dict and len(value) <= 64:
        for key, item in value.items():
            if type(key) is not str or not key or len(key) > 256:
                _bad()
            _bounded(item, depth + 1)
        return
    _bad()


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _validate(row):
    if type(row) is not dict:
        _bad()
    _bounded(row)
    try:
        series_text(row.get("ID"))
    except MetadataError:
        _bad()
    if type(row.get("Name")) is not str:
        _bad()
    for key in MAPS:
        if key not in row or (row[key] is not None and type(row[key]) is not dict):
            _bad()
        if len(_encoded(row[key])) > 8192:
            _bad()
    eq = row["EQ"]
    if eq is not None and "SeriesID" in eq:
        try:
            series_text(eq["SeriesID"])
        except MetadataError:
            _bad()
    for key in ("Paused", "Duplicates", "Rerecord"):
        if row.get(key) is not None and type(row[key]) is not bool:
            _bad()
    for key in ("NumJobs", "PaddingStart", "PaddingEnd", "KeepNum", "Limit", "Priority", "UpdatedAt"):
        if row.get(key) is not None and type(row[key]) is not int:
            _bad()
    if row.get("NumJobs") is not None and row["NumJobs"] < 0:
        _bad()
    for key in ("KeepOnly", "Image"):
        if row.get(key) is not None and type(row[key]) is not str:
            _bad()
    raw = _encoded(row)
    if len(raw) > 32768:
        _bad()
    return raw


def _safe(value, strings=FALLBACK):
    text = value if type(value) is str else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return display_text(text, len(text)) or ui_text(strings, 'unavailable')


def _value(row, key, strings=FALLBACK):
    value = row.get(key)
    if value is None:
        return ui_text(strings, 'unknown')
    if type(value) is str:
        # Preserve empty/whitespace/escaped values distinctly. ASCII JSON keeps
        # controls, bidi and surrogates inert; escape Kodi brackets as JSON too.
        text = _encoded(value).translate({ord("["): "\\u005b", ord("]"): "\\u005d"})
        return text + (ui_text(strings, 'empty_string') if value == '' else '')
    return _safe(value, strings)


@dataclass(frozen=True)
class SavedRule:
    identity: str = field(repr=False)
    raw_json: str = field(repr=False)


@dataclass(frozen=True)
class SavedSnapshot:
    observed_at: int
    series_id: str = field(repr=False)
    rows_json: tuple[str, ...] = field(repr=False)
    rules: tuple[SavedRule, ...] = field(repr=False)
    labels: tuple[str, ...] = field(repr=False)
    details: tuple[str, ...] = field(repr=False)

    def __post_init__(self):
        for key in ("rows_json", "rules", "labels", "details"):
            object.__setattr__(self, key, tuple(getattr(self, key)))
        if not len(self.rules) == len(self.labels) == len(self.details) <= len(self.rows_json) <= 1000:
            _bad()


def saved_snapshot(payload, series_id: str, *, observed_at: int, strings=FALLBACK) -> SavedSnapshot:
    """Worker-only preparation: validate full array and all IDs BEFORE filtering."""
    try:
        series_text(series_id)
    except MetadataError:
        _bad()
    if (type(payload) is not list or len(payload) > 1000 or type(observed_at) is not int
            or not 0 <= observed_at <= 4102444800):
        _bad()
    frozen, ids, size = [], set(), 0
    for row in payload:
        raw = _validate(row)
        if row["ID"] in ids:
            _bad()
        ids.add(row["ID"])
        size += len(raw)
        if size > 4194304:
            _bad()
        frozen.append(raw)
    rules, labels, details = [], [], []
    for raw in frozen:
        row = json.loads(raw)
        if row["EQ"] is None or row["EQ"].get("SeriesID") != series_id:
            continue
        rules.append(SavedRule(row["ID"], raw))
        name = _safe(row['Name'], strings)
        values = dict(name=name, paused=_value(row, 'Paused', strings), jobs=_value(row, 'NumJobs', strings))
        labels.append(ui_text(strings, 'saved_row', **values))
        lines = [ui_text(strings, 'saved_fields', **values, series=_safe(series_id, strings))]
        fields = (('PaddingStart', 'option_padding_start'), ('PaddingEnd', 'option_padding_end'),
                  ('KeepNum', 'option_keep_num'), ('KeepOnly', 'option_keep_only'), ('Limit', 'option_limit'),
                  ('Duplicates', 'option_duplicates'), ('Rerecord', 'option_rerecord'),
                  ('Priority', 'option_priority'), ('UpdatedAt', 'option_updated_at'))
        lines.extend(ui_text(strings, 'field_value', label=ui_text(strings, caption), value=_value(row, key, strings))
                     for key, caption in fields)
        lines.append(ui_text(strings, 'saved_conditions'))
        lines.extend(ui_text(strings, 'field_value', label=key, value=_safe(row[key], strings)) for key in MAPS)
        lines.extend((ui_text(strings, 'response_read', time=utc(observed_at)), ui_text(strings, 'saved_scope')))
        details.append("\n".join(lines))
    return SavedSnapshot(observed_at, series_id, tuple(frozen), tuple(rules), tuple(labels), tuple(details))


def snapshot_age(snapshot: SavedSnapshot, now: int, strings=FALLBACK) -> str:
    age = now - snapshot.observed_at
    if age < 0:
        state = ui_text(strings, 'age_unknown')
    else:
        state = ui_text(strings, 'age_seconds', count=age) + (' | ' + ui_text(strings, 'outdated') if age >= STALE_SECONDS else '')
    return ui_text(strings, 'snapshot_age', status=state, seconds=STALE_SECONDS, time=utc(snapshot.observed_at))


def saved_list_heading(snapshot: SavedSnapshot, page: int, now: int, strings=FALLBACK) -> str:
    """Compact at-opening status before the range; no private row text or timer."""
    count = len(snapshot.rules)
    if type(page) is not int or page < 0 or page * 50 >= count:
        raise ValueError("invalid_saved_pass_page")
    age = now - snapshot.observed_at
    if age < 0:
        status = ui_text(strings, 'age_unknown')
    else:
        # Explicit lower bound for extreme clocks, never silent digit truncation.
        age_text = str(age) if age <= 9999999999 else '>=9999999999'
        status = ui_text(strings, 'age_stale_compact' if age >= STALE_SECONDS else 'age_compact', count=age_text)
    start, stop = page * 50 + 1, min((page + 1) * 50, count)
    return ui_text(strings, 'saved_heading', status=status, start=start, stop=stop, count=count)


def page_entries(snapshot: SavedSnapshot, page: int, strings=FALLBACK) -> tuple[tuple[str, ...], tuple[int | str, ...]]:
    if type(page) is not int or page < 0 or page * 50 >= len(snapshot.rules):
        raise ValueError("invalid_saved_pass_page")
    start, stop = page * 50, min((page + 1) * 50, len(snapshot.rules))
    labels = snapshot.labels[start:stop]
    indices: tuple[int | str, ...] = tuple(range(start, stop))
    if page:
        labels, indices = labels + (ui_text(strings, 'previous_page'),), indices + ('previous',)
    if stop < len(snapshot.rules):
        labels, indices = labels + (ui_text(strings, 'next_page'),), indices + ('next',)
    return labels, indices
