"""Pure worker-side projection of validated v1 receipts; no ownership authority.

Only bounded, sanitized display strings cross to the owner. Never return private
plans, identities, replies, bindings, nonce names or filesystem paths.
"""
from dataclasses import dataclass

from .pass_browser import display_text, utc
from .programme_pass_commands import ProgrammePlan, require
from .programme_pass_journal import journal_state
from .ui_text import FALLBACK, text

WARNINGS = FALLBACK['history_warning']
LOCAL_READ = FALLBACK['history_local']


@dataclass(frozen=True)
class ProgrammeHistoryRow:
    label: str
    details: str


@dataclass(frozen=True)
class ProgrammeHistorySnapshot:
    rows: tuple[ProgrammeHistoryRow, ...]
    notice: str


@dataclass(frozen=True)
class ProgrammeHistoryResult:
    snapshot: ProgrammeHistorySnapshot | None = None
    error: str | None = None


def _time(value, strings):
    # Keep display conversion bounded too, including an airing's end time.
    if type(value) not in (int, float) or not 946684800 <= value <= 4102488000:
        return text(strings, 'time_unavailable')
    return utc(value)


def history_snapshot(saved, binding, strings=FALLBACK):
    """Worker only; validate the ENTIRE root before projecting any row."""
    rows = []
    if saved is not None:
        saved = journal_state(saved)
        if saved['active'] is not None:
            require(saved['server_binding'] == binding, 'binding_mismatch')
        receipts = [(r, text(strings, 'archived')) for r in saved['history']]
        if saved['active'] is not None:
            receipts.append((saved['active'], text(strings, 'current_record')))
        for receipt, location in receipts:
            command = receipt['command']
            airing = ProgrammePlan(**command['plan']).airing()
            title = display_text(airing['Title'], 512, strings) or text(strings, 'unavailable')
            state = text(strings, 'state_' + command['state'])
            sequence = receipt['sequence']
            proof = command['absence_proof']
            proof_text = (_time(proof['checked_at'], strings) if proof is not None else text(strings, 'not_recorded'))
            label = text(strings, 'history_row', sequence=sequence, location=location,
                         state=state, title=display_text(title, 80, strings))
            details = text(strings, 'history_details', sequence=sequence, location=location,
                state=state, title=title, start=_time(airing['Time'], strings),
                end=_time(airing['Time'] + airing['Duration'], strings),
                returned=text(strings, 'yes' if command['returned_id'] is not None else 'no'),
                proof=proof_text, local=text(strings, 'history_local'), warning=text(strings, 'history_warning'))
            rows.append(ProgrammeHistoryRow(label, details))
    notice = ((text(strings, 'history_empty') + '\n' if not rows else '')
              + text(strings, 'history_local') + '\n\n' + text(strings, 'history_warning'))
    return ProgrammeHistorySnapshot(tuple(rows), notice)
