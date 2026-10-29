"""Dedicated serial recording worker; no Kodi, GUI callbacks or latest-wins writes.

One admitted operation/result at a time. close() only signals; an admitted write
may finish reconciliation/persistence, but no queued or later write is dispatched.
The blocking command close and journal release happen on this worker exclusively.
"""

from dataclasses import dataclass, field
from threading import Condition, Thread
import time
from uuid import uuid4

from . import metadata_models as dto
from .live_loader import DevelopmentConfig, _default_client
from .models import Event
from .normalization import DataState
from .recording_commands import (
    RecordingCommand, RecordingError, RecordingPlan, ensure_cancel_future,
    internal_jobs, public_jobs, require_server_binding,
)
from .recording_http import ChannelsRecordingClient
from .recording_journal import MAX_HISTORY, PrivateJournal, journal_state
from .control_lifetime import ControlWorkerTicket


def selected_plan(channels_payload, guide_payload, namespace: str, selected: Event,
                  now: float) -> RecordingPlan:
    """Exact full-key fresh join, station qualification, no raw-airing truncation."""
    if selected.channel.namespace != namespace:
        raise RecordingError("invalid_airing")
    all_channels = dto.public_channels(channels_payload, namespace, include_hidden=True)
    channels = dto.public_channels(channels_payload, namespace)
    events = dto.guide_window(guide_payload, all_channels.items, namespace, selected.start,
                              require_station=True)
    if (all_channels.state not in (DataState.READY, DataState.EMPTY)
            or channels.state not in (DataState.READY, DataState.EMPTY)
            or events.state not in (DataState.READY, DataState.EMPTY)):
        raise RecordingError("schema_unqualified")
    # As in metadata_repository._guide: validate all known, then select visible.
    visible_keys = {c.key for c in channels.items}
    matches = [e for e in events.items if e.channel in visible_keys and
               (e.channel, e.id, e.start) == (selected.channel, selected.id, selected.start)]
    if len(matches) != 1 or matches[0] != selected:
        raise RecordingError("invalid_airing")  # stale title/end/description is a conflict too
    candidates = []
    for row in guide_payload:
        channel = row["Channel"]
        if (channel.get("DeviceID"), channel.get("ChannelID")) != (
                selected.channel.source_id, selected.channel.channel_id):
            continue
        for airing in row["Airings"]:
            if (airing.get("ProgramID"), airing.get("Time")) == (selected.id, selected.start):
                candidates.append(RecordingPlan.from_airing(airing, now=now))
    # Even identical duplicate rows are ambiguous for writes; never first-wins.
    if len(candidates) != 1 or len(candidates[0].body()["Channels"]) != 1:
        raise RecordingError("schema_unqualified")
    return candidates[0]


def _recording_client(config):
    return ChannelsRecordingClient(config.origin, namespace=config.namespace,
                                   expected_version=config.expected_version,
                                   timeout=5, request_budget=24)


@dataclass(frozen=True)
class RecordingResult:
    operation: str
    state: str
    token: int
    event: Event | None = field(default=None, repr=False)
    can_cancel: bool = False
    error: str | None = None
    history_count: int = 0
    cancel_ineligible_reason: str | None = None


def _numbered_history(history):
    """Preserve every receipt; persist positional legacy identity only on a write intent."""
    return [dict(receipt, lifecycle_seq=position) for position, receipt in enumerate(history, 1)]


class _SessionClient:
    """Fresh bounded transport per operation; immutable server binding and write gate."""
    def __init__(self, worker, client):
        self.worker, self.client = worker, client
        self.server_binding_sha256 = require_server_binding(client)

    def verify_status(self):
        return self.client.verify_status()

    def jobs(self):
        return self.client.jobs()

    def public_jobs(self):
        return self.client.public_jobs()

    def job_detail(self, job_id):
        return self.client.job_detail(job_id)

    def create_job(self, plan, *, confirmed):
        self.worker._check_open()  # admission boundary, no condition held over I/O
        return self.client.create_job(plan, confirmed=confirmed)

    def cancel_job(self, job_id, *, expected_start, confirmed):
        self.worker._check_open()
        active = self.worker._active
        # Only the current command's durable write-ahead cancellation can dispatch.
        # A history receipt can never supply cancellation authority, even though
        # its backend ID may legitimately equal the current command's returned ID.
        if (active is None or self.worker._command is None
                or active["command"] != self.worker._command.private_checkpoint()
                or active["command"]["state"] != "cancel_unknown"
                or active["command"]["returned_id"] != job_id
                or active["command"]["expected_start"] != expected_start
                or job_id in active["command"]["baseline_ids"]):
            raise RecordingError("ownership_unverified")
        return self.client.cancel_job(job_id, expected_start=expected_start, confirmed=confirmed)


class RecordingWorker:
    def __init__(self, config: DevelopmentConfig, profile: str, *,
                 metadata_factory=_default_client, recording_factory=_recording_client,
                 journal_factory=PrivateJournal, clock=time.time) -> None:
        self.config, self._profile = config, profile
        self._metadata_factory, self._recording_factory = metadata_factory, recording_factory
        self._journal_factory, self._clock = journal_factory, clock
        self._condition = Condition()
        self._closed = False
        self._intent: tuple[str, Event | None, int, bool] | None = None
        self._result: RecordingResult | None = None
        self._busy = False
        self._thread: Thread | None = None
        self._token = uuid4().int  # stale confirmations cannot alias after a window/worker reopen
        self._journal: PrivateJournal | None = None
        self._saved: dict | None = None
        self._client: _SessionClient | None = None
        self._command: RecordingCommand | None = None
        self._plan: RecordingPlan | None = None
        self._event: Event | None = None
        self._active: dict | None = None
        self._history: list = []
        self._binding: str | None = None
        self._storage_failed = False
        self._rollover_token: int | None = None
        self._epoch = 0
        self._lifetime = ControlWorkerTicket()

    def submit(self, operation: str, *, event: Event | None = None, token: int = 0,
               confirmed: bool = False) -> bool:
        if operation not in {"prepare", "abort", "schedule", "status", "cancel", "rollover"}:
            raise ValueError("unknown_recording_operation")
        with self._condition:
            if self._closed or self._busy or self._intent is not None or self._result is not None:
                return False
            self._intent = (operation, event, token, confirmed)
            self._busy = True
            if self._thread is None:
                self._thread = Thread(target=self._run, name="channels-recording", daemon=True)
                self._thread.start()
            self._condition.notify()
            return True

    def poll(self) -> RecordingResult | None:
        with self._condition:
            result, self._result = self._result, None
            return result

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._intent = None
            self._result = None
            self._condition.notify()
            if self._thread is None:
                self._lifetime.finish()

    def _check_open(self) -> None:
        with self._condition:
            if self._closed:
                raise RecordingError("closed")
            if self._storage_failed:
                raise RecordingError("checkpoint_failed")

    def _poison_storage(self) -> None:
        if not self._storage_failed:
            self._storage_failed = True
            self._token += 1
            self._epoch += 1
            self._rollover_token = None

    def _storage(self) -> None:
        if self._storage_failed:
            raise RecordingError("checkpoint_failed")
        if self._journal is None:
            journal = self._journal_factory(self._profile)
            try:
                journal.acquire()
                saved = journal.load()
                active, history, binding = journal_state(saved)
            except Exception:
                self._poison_storage()
                journal.release()
                raise RecordingError("checkpoint_failed") from None
            self._journal, self._saved = journal, saved
            self._active, self._history, self._binding = active, history, binding

    def _transport(self) -> None:
        client = self._recording_factory(self.config)
        if self._binding is not None:
            require_server_binding(client, {"server_binding_sha256": self._binding})
        if self._client is None:
            self._client = _SessionClient(self, client)
        else:
            require_server_binding(client, {"server_binding_sha256": self._client.server_binding_sha256})
            self._client.client = client

    def _save(self, snapshot: dict) -> None:
        assert self._journal is not None and self._plan is not None
        active = {"command": snapshot, "airing": self._plan.body()["Airing"]}
        if self._saved is not None and self._saved["version"] == 1:
            value = {"version": 1, **active}  # legacy status never migrates
        else:
            history = self._history
            if self._active is None or "lifecycle_seq" in self._active:
                # New reservation (including legacy idle) or already numbered active.
                # Legacy active status/cancel/close retain their original envelope.
                active["lifecycle_seq"] = len(history) + 1
                history = _numbered_history(history)
            if snapshot["state"] == "cancelled":
                active["terminal_at"] = (self._active or {}).get("terminal_at", self._clock())
            value = {"version": 2, "active": active, "history": history,
                     "server_binding_sha256": snapshot["server_binding_sha256"]}
        self._persist(value)

    def _checkpoint(self):
        epoch = self._epoch

        def save(snapshot):
            if epoch != self._epoch:
                raise RecordingError("closed")  # retired command cannot checkpoint a later cycle
            self._save(snapshot)

        return save

    def _persist(self, value: dict) -> None:
        if self._storage_failed:
            raise RecordingError("checkpoint_failed")
        assert self._journal is not None
        try:
            active, history, binding = journal_state(value)
            self._journal.save(value)
        except Exception:
            self._poison_storage()  # no retry or close-checkpoint after uncertain replace/fsync
            raise RecordingError("checkpoint_failed") from None
        self._saved, self._active = value, active
        self._history, self._binding = history, binding

    def _restore(self) -> None:
        if self._command is not None or self._active is None:
            return
        assert self._client is not None
        saved = self._active["command"]
        self._plan = RecordingPlan.from_airing(self._active["airing"], now=saved["expected_start"] - 1800)
        # closed is a session dispatch guard, not revocation of durable ownership.
        # A new explicit session can reconcile, then confirm a first owned cancel.
        restored = dict(saved, closed=False)
        self._command = RecordingCommand.resume_for_reconciliation(
            self._client, self._plan, restored, clock=self._clock, checkpoint=self._checkpoint())

    def _execute(self, operation, event, token, confirmed) -> RecordingResult:
        self._check_open()
        if operation == "rollover" and (confirmed is not True or self._rollover_token is None
                                        or token != self._rollover_token):
            raise RecordingError("confirmation_required")  # no disk/network I/O
        self._storage()
        guard = getattr(self._journal, 'storage_guard', None)
        if guard is not None:
            guard(operation)  # Recovered/imported roots permit read-only status, not writes.
        self._transport()  # binding checked locally before ANY server I/O
        assert self._client is not None
        if operation == "rollover":
            return self._rollover()
        if operation == "prepare":
            if self._active is not None or self._command is not None:
                raise RecordingError("duplicate_blocked")
            self._plan = self._event = None
            self._token += 1  # invalidate any earlier preparation even if this one fails
            if not isinstance(event, Event):
                raise RecordingError("invalid_airing")
            metadata = self._metadata_factory(self.config)
            metadata.verify_status()
            self._check_open()
            channels = metadata.public_channels()
            self._check_open()
            guide = metadata.guide_window(event.start, duration=1800)
            self._check_open()
            self._plan = selected_plan(channels, guide, self.config.namespace, event, self._clock())
            self._event = event
            return RecordingResult(operation, "prepared", self._token, event)
        if operation == "abort":
            if self._active is None:
                self._plan = self._event = None
                self._token += 1
            return RecordingResult(operation, "aborted", self._token)
        if operation == "schedule":
            if confirmed is not True or token != self._token or self._plan is None:
                raise RecordingError("confirmation_required")
            if self._active is not None or self._command is not None:
                raise RecordingError("duplicate_blocked")
            self._command = RecordingCommand(self._client, self._plan, clock=self._clock,
                                             checkpoint=self._checkpoint())
            # Durable reservation before even baseline reads; retained on all failures.
            self._save(self._command.private_checkpoint())
            self._check_open()
            state = self._command.schedule(confirmed=True)
        else:
            self._restore()
            if self._command is None:
                if operation == 'status' and getattr(self._journal, 'pending_reconciliation', False):
                    self._client.verify_status()
                    if set(internal_jobs(self._client.jobs())) != set(public_jobs(self._client.public_jobs())):
                        raise RecordingError('schema_unqualified')
                    self._storage_reconciled()
                return RecordingResult(operation, "none", self._token, history_count=len(self._history))
            self._client.verify_status()
            if operation == "cancel":
                if confirmed is not True or token != self._token:
                    raise RecordingError("confirmation_required")
                state = self._command.cancel_owned(confirmed=True)
            else:
                state = self._command.reconcile()
        assert self._command is not None and self._plan is not None
        can_cancel = state == "owned"
        reason = None
        if can_cancel:
            try:
                ensure_cancel_future(self._plan.body()["Time"], self._clock(), margin=35)
                job_id = self._command.private_checkpoint()["returned_id"]
                detail = self._client.job_detail(job_id)
                if type(detail) is not dict or detail.get("ID") != job_id or not self._plan.matches(detail):
                    reason = "detail_unqualified"
                elif detail.get("RuleID"):
                    reason = "rule_job"
                elif any(detail.get(key) not in (None, False, "", 0)
                         for key in ("FileID", "Recording", "Active", "Running", "Skipped", "Failed")):
                    reason = "active_job"
                if reason is None:
                    ensure_cancel_future(self._plan.body()["Time"], self._clock(), margin=35)
            except RecordingError as error:
                reason = error.code
            except Exception:
                reason = "detail_unavailable"  # never expose an exception body/path/ID
            can_cancel = reason is None
        if state == "cancelled":
            self._token += 1
            self._rollover_token = self._token
        if operation == 'status' and getattr(self._journal, 'pending_reconciliation', False):
            qualified = state == 'owned' and can_cancel
            if state == 'cancelled':
                # Same complete-list absence predicates as the existing rollover
                # guard; no receipt recognition alone clears a storage barrier.
                self._client.verify_status()
                internal = internal_jobs(self._client.jobs())
                public = public_jobs(self._client.public_jobs())
                identity = self._command.private_checkpoint()['returned_id']
                qualified = set(internal) == set(public) and identity not in internal and identity not in public
            if qualified:
                self._storage_reconciled()
            else:
                can_cancel = False
        return RecordingResult(operation, state, self._token, self._event, can_cancel,
                               history_count=len(self._history), cancel_ineligible_reason=reason)

    def _storage_reconciled(self):
        assert self._journal is not None
        try:
            self._journal.reconciled()
        except Exception:
            self._poison_storage()
            raise RecordingError('checkpoint_failed') from None

    def _rollover(self) -> RecordingResult:
        """Archive only durable cancelled ownership, under the existing lifetime flock."""
        self._rollover_token = None
        active = self._active
        if active is None or active["command"]["state"] != "cancelled":
            raise RecordingError("ownership_unverified")
        if len(self._history) >= MAX_HISTORY:
            raise RecordingError("duplicate_blocked")  # retain ALL; never evict
        assert self._client is not None
        receipt = active["command"]
        identity = receipt["returned_id"]
        self._client.verify_status()
        internal = internal_jobs(self._client.jobs())
        public = public_jobs(self._client.public_jobs())
        if set(internal) != set(public):
            raise RecordingError("schema_unqualified")
        if identity in internal or identity in public:
            raise RecordingError("ownership_unverified")
        self._check_open()
        now = self._clock()
        archived = {key: receipt[key] for key in
                    ("returned_id", "server_binding_sha256", "binding_sha256", "expected_start")}
        archived.update(state="cancelled", lifecycle_seq=len(self._history) + 1,
                        terminal_at=active.get("terminal_at", now),
                        terminal_time_source="checkpoint" if "terminal_at" in active else "legacy_observed",
                        archived_at=now, proof={"checked_at": now, "status_verified": True,
                                               "internal_absent": True, "public_absent": True})
        # Detach BEFORE the atomic write, including its uncertain failure path.
        # The retired command's close callback must never overwrite the new envelope.
        self._command = self._plan = self._event = None
        self._epoch += 1
        self._token += 1
        self._persist({"version": 2, "active": None, "history": [*_numbered_history(self._history), archived],
                       "server_binding_sha256": self._binding})
        return RecordingResult("rollover", "none", self._token, history_count=len(self._history))

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or self._intent is not None)
                    if self._closed:
                        return
                    intent, self._intent = self._intent, None
                assert intent is not None
                try:
                    result = self._execute(*intent)
                except Exception as error:
                    code = error.code if isinstance(error, RecordingError) else "worker_error"
                    state = self._command.state if self._command else "blocked"
                    if self._storage_failed:
                        state, code = "blocked", "checkpoint_failed"
                    result = RecordingResult(intent[0], state, self._token, error=code,
                                             history_count=len(self._history))
                with self._condition:
                    if not self._closed:
                        self._result = result
                    self._busy = False
        finally:
            try:
                if self._command is not None and self._saved is not None and not self._storage_failed:
                    try:
                        self._command.close()  # BLOCKING: only here, never on owner
                    except Exception:
                        pass  # retained write-ahead uncertainty; no cleanup/retry
            finally:
                if self._journal is not None:
                    try:
                        self._journal.release()
                    except Exception:
                        pass  # no raw filesystem paths in Kodi's exception log
                self._lifetime.finish()  # terminal close checkpoints and release are finished
