"""DVR tab copy, scoped to 30960–30979."""

ROWS = (
    ('dvr_passes', 30960, 'Passes'),
    ('dvr_manage', 30961, 'Series actions'),
    ('dvr_series_status', 30962, 'Check Series status'),
    ('dvr_pass_state', 30963, 'Paused: {paused} · Jobs reported: {jobs}'),
    ('dvr_pass_observed', 30964, 'Saved-pass snapshot. Listing a pass does not grant control.'),
    ('dvr_pass_stale', 30965, 'Older pass snapshot. Refresh before requesting Series actions.'),
    ('dvr_empty_passes', 30966, 'No saved passes in the last successful read.'),
    ('dvr_pass_readonly', 30967, 'Read-only rule'),
    ('dvr_series_review', 30968, 'Series rule; management requires a fresh check.'),
    ('dvr_series_pending', 30969, 'Series outcome unknown. Check Series status. Do not retry.'),
    ('dvr_series_verified', 30970, 'Series result verified.'),
    ('dvr_pass_changed', 30971, 'The pass changed since it was listed. Review the refreshed list before requesting actions again.'),
)
