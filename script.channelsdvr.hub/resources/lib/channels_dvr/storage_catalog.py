"""Owner-captured operator-storage copy; fixed new IDs, no private data."""
ROWS = (
    ('storage_title', 30600, 'Operation storage'),
    ('storage_view', 30601, 'View stored status'),
    ('storage_import', 30602, 'Import old saved operation history'),
    ('storage_recover_recording', 30603, 'Recover recording storage claim'),
    ('storage_recover_programme', 30604, 'Recover programme storage claim'),
    ('storage_exclusive', 30605, 'Have you closed all other add-on instances and old-version writers on every device? Continue only with exclusive access. This window will stop its recording/pass controls.'),
    ('storage_reconcile_warning', 30606, 'Clear only the abandoned storage claim? Pending actions and history stay saved. Run normal Recording or Programme status next. Changes stay blocked until qualified read-only reconciliation succeeds.'),
    ('storage_import_warning', 30607, 'Import this profile\'s old history? Originals stay untouched. No DVR action runs. Old versions must remain closed; rollback or mixing versions can lose newer history. Fresh status is required before changes.'),
    ('storage_done', 30608, 'Storage action saved. Run normal Recording or Programme status before making changes. Stored status alone is not fresh ownership proof.'),
    ('storage_failed', 30609, 'Storage action unavailable or outcome uncertain. No automatic retry. Refresh stored status before deciding what to do next. Keep all original history; do not reset or delete it.'),
    ('storage_busy', 30610, 'Storage work is pending. Close recording/pass controls and try a fresh stored-status inspection.'),
    ('storage_status_line', 30611, '{scope}: {state}\nArchived receipts: {count}; claim present: {claim}; reconciliation required: {pending}'),
    ('storage_local_warning', 30612, 'Stored observations only, not current DVR proof. Recovery/import never retries a DVR action. Profile privacy and durable local storage are required.'),
    ('storage_legacy', 30613, 'Old saved history found. Explicit exclusive import is required; interrupted or invalid history stays blocked.'),
    ('storage_imported', 30614, 'Import recorded. Original files are retained; any later change or removal blocks operation controls.'),
    ('storage_refresh', 30615, 'Refresh stored status'),
)
