"""Read-only Series text. Provider values are data, never keys or identities."""
import json

from .pass_browser import utc
from .ui_text import text

TEXT_PAGE_BYTES = 30000
LIST_PAGE_BYTES = 24000
MAX_READER_BYTES = 4194304 + 65536


def display_text(body):
    """UTF-8-safe display only; never normalize authoritative provider/JSON data.

    Valid Unicode is returned unchanged. Well-formed explicit UTF-16 pairs are
    displayed as their supplementary scalar; isolated surrogate units retain
    their exact identity as visible ASCII \\uXXXX escapes, not dropped/replaced.
    Canonical raw JSON is already ASCII-escaped and therefore stays byte-exact.
    """
    try:
        body.encode('utf-8', errors='strict')
        return body
    except UnicodeEncodeError:
        result = []
        index = 0
        while index < len(body):
            unit = ord(body[index])
            if 0xd800 <= unit <= 0xdbff and index+1 < len(body):
                low = ord(body[index+1])
                if 0xdc00 <= low <= 0xdfff:
                    result.append(chr(0x10000+((unit-0xd800) << 10)+(low-0xdc00)))
                    index += 2
                    continue
            result.append(f'\\u{unit:04x}' if 0xd800 <= unit <= 0xdfff else body[index])
            index += 1
        return ''.join(result)


def text_pages(body):
    """Exact valid-text/safe escaped-display reassembly; approval cap is unchanged."""
    raw = display_text(body).encode('utf-8', errors='strict')
    if len(raw) > MAX_READER_BYTES:
        raise ValueError('reader_limit')
    pages = []
    offset = 0
    while offset < len(raw):
        end = min(len(raw), offset+TEXT_PAGE_BYTES)
        while end < len(raw) and raw[end] & 0xc0 == 0x80:
            end -= 1
        pages.append(raw[offset:end].decode('utf-8', errors='strict'))
        offset = end
    return tuple(pages) or ('',)


class RowPages:
    """Lazy frozen page plans. Only visited <=50-row pages are decoded, once."""
    def __init__(self, rows, label, fallback):
        self.rows, self.label, self.fallback = rows, label, fallback
        self.cache = []

    def page(self, index):
        while index >= len(self.cache):
            start = self.cache[-1][1] if self.cache else 0
            stop, size = start, 0
            labels: list[str] = []
            while stop < len(self.rows) and stop-start < 50:
                label = display_text(self.label(self.rows[stop]))
                amount = len(label.encode('utf-8'))
                if amount > LIST_PAGE_BYTES:
                    # Compact list reference only. The complete provider value
                    # remains in this row's read-only details/raw reader.
                    label = display_text(self.fallback)+' '+str(stop+1)
                    amount = len(label.encode('utf-8'))
                if labels and size+amount > LIST_PAGE_BYTES:
                    break
                labels.append(label); size += amount; stop += 1
            self.cache.append((start, stop, tuple(labels)))
        return self.cache[index]


def value(item, strings):
    if item is None:
        return display_text(text(strings, 'unknown'))
    if type(item) is str:
        return display_text(item)  # No keyword filtering, ellipsis or provider mutation.
    return display_text(json.dumps(item, ensure_ascii=False, sort_keys=True, indent=2))


def stamp(item, strings):
    return utc(item) if type(item) is int and 946684800 <= item <= 4102444800 else display_text(text(strings, 'unknown'))


def provenance(kind, strings):
    return display_text(text(strings, {'created': 'series_created', 'adopted': 'series_adopted'}.get(kind, 'series_unmanaged')))


def rule_label(row, kind, strings):
    body = row.payload()
    paused = text(strings, 'yes' if body.get('Paused') is True else 'no') if type(body.get('Paused')) is bool else text(strings, 'unknown')
    return display_text(text(strings, 'saved_row', name=value(body.get('Name'), strings), paused=paused,
                jobs=value(body.get('NumJobs'), strings)) + ' | ' + provenance(kind, strings))


def rule_details(row, kind, actions, strings):
    body = row.payload()
    eq = body.get('EQ')
    paused = text(strings, 'yes' if body.get('Paused') else 'no') if type(body.get('Paused')) is bool else text(strings, 'unknown')
    result = provenance(kind, strings) + '\n' + text(strings, 'saved_fields',
        name=value(body.get('Name'), strings), series=value(eq.get('SeriesID') if type(eq) is dict else None, strings),
        paused=paused, jobs=value(body.get('NumJobs'), strings))
    for key, label in (('PaddingStart', 'option_padding_start'), ('PaddingEnd', 'option_padding_end'),
        ('KeepOnly', 'option_keep_only'), ('KeepNum', 'option_keep_num'), ('Rerecord', 'option_rerecord'),
        ('Limit', 'option_limit'), ('Priority', 'option_priority'), ('UpdatedAt', 'option_updated_at')):
        result += '\n' + text(strings, 'field_value', label=text(strings, label), value=value(body.get(key), strings))
    if not actions:
        result += '\n\n' + text(strings, 'series_readonly')
    return display_text(result)


def episode_label(row, strings):
    body = row.payload()
    return display_text(text(strings, 'preview_row', start=stamp(body.get('Time'), strings),
                title=value(body.get('Title'), strings), index=row.index+1))


def episode_details(row, observed_at, strings):
    body = row.payload()
    start, duration = body.get('Time'), body.get('Duration')
    known = type(start) is int and type(duration) is int and duration > 0
    end = start+duration if known else None
    phase = ('preview_past' if end <= observed_at else 'preview_current' if start <= observed_at else 'preview_future') if known else 'unknown'
    summary = body.get('FullSummary', body.get('Summary'))
    return display_text(text(strings, 'preview_fields', title=value(body.get('Title'), strings),
        episode_title=value(body.get('EpisodeTitle'), strings), season=value(body.get('SeasonNumber'), strings),
        episode=value(body.get('EpisodeNumber'), strings), start=stamp(start, strings), end=stamp(end, strings),
        duration=text(strings, 'seconds', count=duration) if type(duration) is int and duration > 0 else text(strings, 'unknown'),
        phase=text(strings, phase), channels=value(body.get('Channels'), strings), channel=value(body.get('Channel'), strings),
        summary=value(summary, strings), time=stamp(observed_at, strings)))
