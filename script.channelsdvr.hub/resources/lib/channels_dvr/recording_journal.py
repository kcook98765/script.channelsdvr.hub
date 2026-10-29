"""Bounded recording envelopes over worker-only portable operation storage."""

import json
import re

from .recording_commands import RecordingError, RecordingPlan, seconds, text
from .operation_storage import OperationStore, Scope


MAX_HISTORY = 32
MAX_BYTES = 131072


def _hash(value) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def journal_state(value: dict | None) -> tuple[dict | None, list, str | None]:
    """Strict read-only v1/v2 decoding; never migrate on status/reopen.

    Active keeps the complete private airing. History contains only bounded
    terminal receipts, not an alternative source of cancellable ownership.
    """
    if value is None:
        return None, [], None
    if type(value) is not dict or type(value.get("version")) is not int:
        raise RecordingError("checkpoint_failed")
    if value["version"] == 1 and set(value) == {"version", "command", "airing"}:
        active, history = {"command": value["command"], "airing": value["airing"]}, []
        binding = value["command"].get("server_binding_sha256") if type(value["command"]) is dict else None
    elif value["version"] == 2 and set(value) == {"version", "active", "history", "server_binding_sha256"}:
        active, history, binding = value["active"], value["history"], value["server_binding_sha256"]
    else:
        raise RecordingError("checkpoint_failed")
    if not _hash(binding) or type(history) is not list or len(history) > MAX_HISTORY:
        raise RecordingError("checkpoint_failed")
    # Old v2 has implicit positional lifecycle identity; do not rewrite on read.
    # Numbered envelopes are all-or-nothing and strictly contiguous, since no
    # receipt is ever evicted. Backend job IDs are NOT lifecycle identities.
    numbered = ((type(active) is dict and "lifecycle_seq" in active)
                or any(type(row) is dict and "lifecycle_seq" in row for row in history))
    receipt_keys = {"state", "returned_id", "server_binding_sha256", "binding_sha256", "expected_start",
                    "terminal_at", "terminal_time_source", "archived_at", "proof"}
    if numbered:
        receipt_keys.add("lifecycle_seq")
    for position, receipt in enumerate(history, 1):
        if (type(receipt) is not dict or set(receipt) != receipt_keys
                or receipt["state"] != "cancelled" or receipt["server_binding_sha256"] != binding
                or not _hash(receipt["binding_sha256"])
                or receipt["terminal_time_source"] not in {"checkpoint", "legacy_observed"}):
            raise RecordingError("checkpoint_failed")
        text(receipt["returned_id"])
        if numbered and (type(receipt["lifecycle_seq"]) is not int or receipt["lifecycle_seq"] != position):
            raise RecordingError("checkpoint_failed")
        for key in ("expected_start", "terminal_at", "archived_at"):
            seconds(receipt[key], 946684800, 4102444800)
        proof = receipt["proof"]
        if (type(proof) is not dict or set(proof) != {
                "checked_at", "status_verified", "internal_absent", "public_absent"}
                or any(proof[key] is not True for key in
                       ("status_verified", "internal_absent", "public_absent"))
                or proof["checked_at"] != receipt["archived_at"]
                or receipt["terminal_at"] > receipt["archived_at"]):
            raise RecordingError("checkpoint_failed")
    if active is not None:
        if (type(active) is not dict or (set(active) - ({"lifecycle_seq"} if numbered else set())) not in
                ({"command", "airing"}, {"command", "airing", "terminal_at"})):
            raise RecordingError("checkpoint_failed")
        if numbered and (type(active.get("lifecycle_seq")) is not int
                         or active["lifecycle_seq"] != len(history) + 1):
            raise RecordingError("checkpoint_failed")
        command = active["command"]
        if (type(command) is not dict or set(command) != {
                "state", "returned_id", "server_binding_sha256", "baseline_ids", "binding_sha256",
                "expected_start", "closed"} or command["server_binding_sha256"] != binding
                or command["state"] not in {"ready", "create_unknown", "owned", "cancel_unknown", "cancelled"}
                or type(command["closed"]) is not bool or type(command["baseline_ids"]) is not list
                or len(command["baseline_ids"]) > 1000):
            raise RecordingError("checkpoint_failed")
        start = seconds(command["expected_start"], 946684800, 4102444800)
        plan = RecordingPlan.from_airing(active["airing"], now=start - 1800)
        baseline = [text(identity) for identity in command["baseline_ids"]]
        identity = command["returned_id"]
        if identity is not None:
            text(identity)
        if (len(set(baseline)) != len(baseline) or identity in baseline
                or (command["state"] in {"owned", "cancel_unknown", "cancelled"} and identity is None)
                or command["binding_sha256"] != plan.binding_sha256 or start != plan.body()["Time"]):
            raise RecordingError("ownership_unverified")
        if "terminal_at" in active:
            seconds(active["terminal_at"], 946684800, 4102444800)
            if command["state"] != "cancelled":
                raise RecordingError("checkpoint_failed")
    return active, history, binding


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecordingError("checkpoint_failed")
        result[key] = value
    return result


class PrivateJournal:
    def __init__(self, profile: str) -> None:
        try:
            self._store = OperationStore(profile, Scope.RECORDING)
        except Exception:
            raise RecordingError("checkpoint_failed") from None

    def acquire(self) -> None:
        try:
            self._store.acquire()
        except Exception:
            raise RecordingError("checkpoint_failed") from None

    def load(self) -> dict | None:
        try:
            data = self._store.load()
            if data is None:
                return None
            value = json.loads(data, object_pairs_hook=_unique_object)
            if type(value) is not dict:
                raise RecordingError("checkpoint_failed")
            journal_state(value)
            return value
        except Exception:
            self._store.poison()
            raise RecordingError("checkpoint_failed") from None

    def save(self, value: dict) -> None:
        try:
            if type(value) is not dict:
                raise RecordingError("checkpoint_failed")
            journal_state(value)
            data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")
            self._store.save(data)
        except Exception:
            self._store.poison()
            raise RecordingError("checkpoint_failed") from None

    def release(self) -> None:
        self._store.release()

    @property
    def pending_reconciliation(self):
        return self._store.pending_reconciliation

    def storage_guard(self, operation):
        try:
            self._store.reconciliation_guard(operation)
        except Exception:
            raise RecordingError('checkpoint_failed') from None

    def reconciled(self):
        try:
            self._store.finish_reconciliation()
        except Exception:
            raise RecordingError('checkpoint_failed') from None
