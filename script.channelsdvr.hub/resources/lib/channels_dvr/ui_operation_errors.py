"""Closed display-only error/state maps; never expose arbitrary backend text."""
from .ui_text import FALLBACK, text

_COMMON = {
    'checkpoint_failed': 'write_storage', 'binding_mismatch': 'error_binding',
    'confirmation_required': 'error_confirmation', 'expired': 'error_expired',
    'closed': 'error_closed', 'operation_in_progress': 'pending_operation',
    'worker_error': 'error_worker', 'invalid_argument': 'error_schema',
    'schema_unqualified': 'error_schema', 'selection_changed': 'error_selection',
    'series_unsupported': 'error_series', 'identity_mismatch': 'error_binding',
    'invalid_origin': 'error_invalid_origin', 'unsupported': 'error_unsupported',
    'unknown_operation': 'error_unsupported', 'redirect_refused': 'error_redirect',
    'http_error': 'error_http', 'network_error': 'error_network',
    'content_type': 'error_format', 'content_encoding': 'error_format',
    'malformed_json': 'error_json', 'response_too_large': 'error_size',
    'request_budget': 'error_budget', 'identity_not_verified': 'error_identity',
    'response_timeout': 'error_timeout',
}
_PROGRAMME = {
    'programme_past': 'error_past', 'programme_current': 'error_current',
    'near_start': 'error_near_start', 'outside_window': 'error_outside_window',
    'preview_not_one': 'error_preview_one', 'duplicate_blocked': 'error_duplicate',
    'history_limit': 'error_history_full', 'ownership_unverified': 'error_ownership',
    'ineligible': 'error_ineligible',
}
_RECORDING = {
    'invalid_airing': 'error_selection', 'near_start': 'error_recording_near',
    'baseline_conflict': 'error_recording_conflict', 'duplicate_blocked': 'error_recording_duplicate',
    'history_limit': 'error_recording_duplicate', 'ownership_unverified': 'error_recording_ownership',
    'foreign_job': 'error_recording_ownership', 'active_job': 'error_recording_active',
}
_STATES = {'prepared', 'none', 'ready', 'owned', 'cancelled', 'removed', 'blocked',
           'create_unknown', 'cancel_unknown', 'remove_unknown'}


def error_message(code, strings=FALLBACK, kind='programme'):
    code = code if type(code) is str else ''
    specific = _RECORDING if kind == 'recording' else _PROGRAMME
    return text(strings, specific.get(code, _COMMON.get(code, 'operation_error')))


def state_label(state, strings=FALLBACK):
    if state == 'cancelled':
        return text(strings, 'cancelled_saved')
    return text(strings, 'state_' + state if type(state) is str and state in _STATES else 'state_unavailable')


def uncertainty(state, strings=FALLBACK, kind='programme'):
    if state == 'create_unknown':
        return text(strings, 'recording_unknown_create' if kind == 'recording' else 'programme_unknown_create')
    if state == 'cancel_unknown':
        return text(strings, 'recording_unknown_cancel')
    if state == 'remove_unknown':
        return text(strings, 'programme_unknown_remove')
    return text(strings, 'operation_error')
