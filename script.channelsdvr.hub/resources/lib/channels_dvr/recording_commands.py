"""Future-only, single-attempt recording commands; no Kodi or implicit cleanup.

Channels are opaque backend selectors, NOT proven source-aware ChannelKeys.
Ownership requires a new returned ID plus internal/public reads and bound detail.
A lost/malformed POST response never authorizes adopting a job by resemblance.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
import math
import re
import threading
import time
from typing import Callable, Protocol, cast


_CODES = frozenset({
    "invalid_airing", "invalid_argument", "near_start", "schema_unqualified",
    "baseline_conflict", "confirmation_required", "duplicate_blocked", "closed",
    "ownership_unverified", "foreign_job", "active_job", "checkpoint_failed",
    "operation_in_progress",
})
MIN_LEAD = 1800
MAX_LEAD = 48 * 3600
CANCEL_MIN_LEAD = 60
WRITE_MARGIN = 30


class RecordingError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code if code in _CODES else "schema_unqualified"
        super().__init__(self.code)


def text(value: object, limit: int = 256) -> str:
    if (type(value) is not str or not value.strip() or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise RecordingError("invalid_argument") from None
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise RecordingError("invalid_argument") from None
    return value


def seconds(value: object, low: int, high: int) -> int | float:
    if type(value) not in (int, float):
        raise RecordingError("invalid_argument") from None
    numeric = cast(int | float, value)
    if not low <= numeric <= high or not math.isfinite(numeric):
        raise RecordingError("invalid_argument") from None
    return numeric


def ensure_cancel_future(expected_start: float, now: float, *, margin: float = WRITE_MARGIN) -> None:
    """Cancellation has its own strict future-start guard, never creation's lead/window.

    Recheck after ownership reads and persistence; the transport adds its finite
    timeout to the margin. No permission to cancel an active or unknown job.
    """
    seconds(expected_start, 946684800, 4102444800)
    seconds(now, 946684800, 4102444800)
    seconds(margin, 0, 60)
    if expected_start - now <= CANCEL_MIN_LEAD + margin:
        raise RecordingError("near_start") from None


def _bounded_json(value: object, depth: int = 0) -> None:
    if depth > 12:
        raise RecordingError("invalid_airing") from None
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        if len(value) > 16384 or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise RecordingError("invalid_airing") from None
    elif type(value) in (int, float):
        seconds(value, -10**12, 10**12)
    elif type(value) is list and len(value) <= 256:
        for item in value:
            _bounded_json(item, depth + 1)
    elif type(value) is dict and len(value) <= 128:
        for key, item in value.items():
            text(key, 128)
            _bounded_json(item, depth + 1)
    else:
        raise RecordingError("invalid_airing") from None


def _channels(airing: dict) -> list[str]:
    values = airing.get("Channels")
    if values is None:
        values = [airing.get("Channel")]
    if type(values) is not list or not 1 <= len(values) <= 16:
        raise RecordingError("invalid_airing") from None
    result = [text(item, 128) for item in values]
    if len(set(result)) != len(result):
        raise RecordingError("invalid_airing") from None
    if airing.get("Channel") is not None and text(airing["Channel"], 128) not in result:
        raise RecordingError("invalid_airing") from None
    return result


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     allow_nan=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True, repr=False, init=False)
class RecordingPlan:
    """Private defensive snapshot of the complete source airing, zero padding default."""

    _body: dict = field(repr=False)

    def __repr__(self) -> str:
        return "RecordingPlan(private_airing=True, source_pinning=False)"

    @classmethod
    def from_airing(cls, airing: object, *, now: float,
                    padding_before: int = 0, padding_after: int = 0) -> "RecordingPlan":
        if type(airing) is not dict:
            raise RecordingError("invalid_airing") from None
        _bounded_json(airing)
        text(airing.get("ProgramID"))
        title = text(airing.get("Title"), 512)
        start = seconds(airing.get("Time"), 946684800, 4102444800)
        duration = seconds(airing.get("Duration"), 1, 12 * 3600)
        for value in (padding_before, padding_after):
            if type(value) is not int or not 0 <= value <= 3600:
                raise RecordingError("invalid_argument") from None
        body = {"Name": title, "Time": start - padding_before,
                "Duration": duration + padding_before + padding_after,
                "Channels": _channels(airing), "Airing": deepcopy(airing)}
        if len(json.dumps(body, ensure_ascii=True).encode("utf-8")) > 65536:
            raise RecordingError("invalid_airing") from None
        plan = object.__new__(cls)
        object.__setattr__(plan, "_body", body)
        plan.ensure_future(now)
        return plan

    def body(self) -> dict:
        return deepcopy(self._body)

    @property
    def binding_sha256(self) -> str:
        return _digest(self._body)

    def ensure_future(self, now: float, margin: float = 0) -> None:
        seconds(now, 946684800, 4102444800)
        lead = self._body["Time"] - now
        if lead < MIN_LEAD + margin or lead > MAX_LEAD:
            raise RecordingError("near_start") from None

    def matches(self, job: object) -> bool:
        # Live build 2026.08.07.0346 list/detail omit transient SearchScore.
        # Only that omission is qualified; changed scores/other omissions fail.
        if type(job) is not dict:
            return False
        body = self._body
        airing = job.get("Airing")
        channels = job.get("Channels")
        if channels is None:
            channels = [job.get("Channel")]
        return (job.get("Name") == body["Name"] and job.get("Time") == body["Time"]
                and job.get("Duration") == body["Duration"] and channels == body["Channels"]
                and type(airing) is dict
                and all((key == "SearchScore" and key not in airing)
                        or (key in airing and _digest(airing[key]) == _digest(value))
                        for key, value in body["Airing"].items()))


def internal_jobs(payload: object) -> dict[str, dict]:
    if type(payload) is not list or len(payload) > 1000:
        raise RecordingError("schema_unqualified") from None
    result = {}
    for row in payload:
        if type(row) is not dict:
            raise RecordingError("schema_unqualified") from None
        job_id = text(row.get("ID"))
        seconds(row.get("Time"), 946684800, 4102444800)
        seconds(row.get("Duration"), 1, 24 * 3600)
        if job_id in result:
            raise RecordingError("schema_unqualified") from None
        result[job_id] = row
    return result


def public_jobs(payload: object) -> dict[str, dict]:
    if type(payload) is not list or len(payload) > 1000:
        raise RecordingError("schema_unqualified") from None
    result = {}
    for row in payload:
        if type(row) is not dict:
            raise RecordingError("schema_unqualified") from None
        job_id = text(row.get("id"))
        start = seconds(row.get("start_time"), 946684800, 4102444800)
        end = seconds(row.get("end_time"), 946684800, 4102531200)
        if end <= start or job_id in result:
            raise RecordingError("schema_unqualified") from None
        result[job_id] = row
    return result


class RecordingClient(Protocol):
    @property
    def server_binding_sha256(self) -> str: ...

    def verify_status(self) -> object: ...
    def jobs(self) -> object: ...
    def public_jobs(self) -> object: ...
    def job_detail(self, job_id: str) -> object: ...
    def create_job(self, plan: RecordingPlan, *, confirmed: bool) -> object: ...
    def cancel_job(self, job_id: str, *, expected_start: float, confirmed: bool) -> object: ...


def require_server_binding(client: RecordingClient, saved: dict | None = None) -> str:
    """Compare client-derived opaque identity locally, before any server I/O."""
    binding = getattr(client, "server_binding_sha256", None)
    if (type(binding) is not str or not re.fullmatch(r"[0-9a-f]{64}", binding)
            or (saved is not None and (type(saved) is not dict
                                       or saved.get("server_binding_sha256") != binding))):
        raise RecordingError("ownership_unverified") from None
    return binding


def _serialized(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        # The complete read/decision/write/persist operation is serialized, not
        # just the state assignment. Late reads cannot roll back uncertainty.
        with self._dispatch_lock:
            return method(self, *args, **kwargs)
    return invoke


class RecordingCommand:
    """Serialized synchronous lifecycle, including transport I/O and checkpoints.

    close() BLOCKS behind an in-flight operation and its checkpoint writes. Once
    it returns, no new write can dispatch. An operation already holding the gate
    may finish first, including read reconciliation. Use a worker-side close in
    a future GUI adapter; this is not a nonblocking GUI shutdown API. Transport
    timeouts/deadlines are finite but not a preemptive DNS/socket wall-time bound.

    A checkpoint callback must durably store private snapshots before dispatch.
    Its failure blocks dispatch. Neither close nor any failure retries a write.
    There is no ID-accepting cancellation API: only this command's verified ID.
    """

    def __init__(self, client: RecordingClient, plan: RecordingPlan, *,
                 clock: Callable[[], float] | None = None,
                 checkpoint: Callable[[dict], None] = lambda value: None) -> None:
        self._client, self._plan = client, plan
        self._clock, self._checkpoint = clock if clock is not None else time.time, checkpoint
        self._server_binding = require_server_binding(client)
        self.state = "ready"
        self._closed = False
        self._returned_id: str | None = None
        self._baseline: set[str] = set()
        self.last_error: str | None = None
        self._dispatch_lock = threading.RLock()

    def __repr__(self) -> str:
        return f"RecordingCommand(state={self.state!r}, closed={self._closed!r})"

    @_serialized
    def private_checkpoint(self) -> dict:
        return {"state": self.state, "returned_id": self._returned_id,
                "server_binding_sha256": self._server_binding,
                "baseline_ids": sorted(self._baseline),
                "binding_sha256": _digest(self._plan.body()),
                "expected_start": self._plan.body()["Time"], "closed": self._closed}

    @classmethod
    def resume_for_reconciliation(cls, client: RecordingClient, plan: RecordingPlan,
                                  saved: dict, **options) -> "RecordingCommand":
        """Restore only a write-attempted lifecycle, never a new schedule intent."""
        require_server_binding(client, saved)
        if (type(saved) is not dict or saved.get("state") not in
                {"create_unknown", "owned", "cancel_unknown", "cancelled"}
                or saved.get("binding_sha256") != plan.binding_sha256
                or saved.get("expected_start") != plan.body()["Time"]
                or type(saved.get("baseline_ids")) is not list
                or len(saved["baseline_ids"]) > 1000 or type(saved.get("closed")) is not bool):
            raise RecordingError("ownership_unverified") from None
        command = cls(client, plan, **options)
        command.state = saved["state"]
        command._baseline = {text(value) for value in saved["baseline_ids"]}
        command._returned_id = (text(saved["returned_id"]) if saved.get("returned_id") is not None else None)
        if command._returned_id in command._baseline:
            raise RecordingError("foreign_job") from None
        command._closed = saved["closed"]
        return command

    @classmethod
    def reconcile_cancel_checkpoint(cls, client: RecordingClient, saved: dict, *,
                                    checkpoint: Callable[[dict], None]) -> str:
        """Read-only crash recovery; no guide, airing reconstruction, or DELETE.

        The caller must serialize access to the persisted lifecycle (the harness
        holds its process lock). A cancelled receipt is terminal, not permission
        for a new create. Missing identity/binding never authorizes recovery.
        """
        require_server_binding(client, saved)
        if (saved.get("state") not in {"cancel_unknown", "cancelled"}
                or type(saved.get("baseline_ids")) is not list
                or len(saved["baseline_ids"]) > 1000
                or type(saved.get("closed")) is not bool):
            raise RecordingError("ownership_unverified") from None
        job_id = text(saved.get("returned_id"))
        baseline = {text(value) for value in saved["baseline_ids"]}
        if job_id in baseline:
            raise RecordingError("foreign_job") from None
        if saved["state"] == "cancelled":
            return "cancelled"  # recognition of a durable receipt, no fresh claim
        client.verify_status()
        internal = internal_jobs(client.jobs())
        public = public_jobs(client.public_jobs())
        if set(internal) != set(public):
            raise RecordingError("schema_unqualified") from None
        if job_id in internal or job_id in public:
            return "cancel_unknown"  # never replay the ambiguous DELETE
        completed = deepcopy(saved)
        completed["state"] = "cancelled"
        try:
            checkpoint(completed)
        except Exception:
            raise RecordingError("checkpoint_failed") from None
        return "cancelled"

    @_serialized
    def _save(self) -> None:
        try:
            self._checkpoint(self.private_checkpoint())
        except Exception:
            raise RecordingError("checkpoint_failed") from None

    @_serialized
    def close(self) -> None:
        with self._dispatch_lock:
            self._closed = True
        self._save()

    def _write_guard(self, confirmed: bool, *, cancelling: bool = False) -> None:
        if self._closed:
            raise RecordingError("closed") from None
        if confirmed is not True:
            raise RecordingError("confirmation_required") from None
        if cancelling:
            ensure_cancel_future(self._plan.body()["Time"], self._clock())
        else:
            self._plan.ensure_future(self._clock(), margin=WRITE_MARGIN)

    def _lists(self) -> tuple[dict, dict]:
        internal = internal_jobs(self._client.jobs())
        public = public_jobs(self._client.public_jobs())
        if set(internal) != set(public):
            raise RecordingError("schema_unqualified") from None
        return internal, public

    @_serialized
    def schedule(self, *, confirmed: bool = False) -> str:
        self._write_guard(confirmed)
        if self.state != "ready":
            raise RecordingError("duplicate_blocked") from None
        self._client.verify_status()
        internal, public = self._lists()
        body = self._plan.body()
        # Public source semantics are unqualified: conservatively refuse ANY
        # overlapping baseline job, rather than pretending number/source joins.
        if any(row["start_time"] < body["Time"] + body["Duration"]
               and row["end_time"] > body["Time"] for row in public.values()):
            raise RecordingError("baseline_conflict") from None
        self._baseline = set(internal)
        with self._dispatch_lock:
            self._write_guard(confirmed)
            if self.state != "ready":
                raise RecordingError("duplicate_blocked") from None
            self.state = "create_unknown"
        self._save()  # write-ahead uncertainty survives process loss
        self._write_guard(confirmed)  # close/clock can change during persistence
        try:
            result = self._client.create_job(self._plan, confirmed=True)
            if type(result) is dict:
                candidate = text(result.get("ID"))
                if candidate in self._baseline:
                    raise RecordingError("foreign_job") from None
                self._returned_id = candidate
                self._save()
                if not self._plan.matches(result):
                    raise RecordingError("ownership_unverified") from None
        except Exception:
            # No exception string/body escapes; keep ID if unequivocally returned.
            self.last_error = "create_response_unqualified"
        return self.reconcile()

    @_serialized
    def reconcile(self) -> str:
        if self.state not in {"create_unknown", "owned", "cancel_unknown"}:
            return self.state
        internal, public = self._lists()
        job_id = self._returned_id
        if job_id is None or job_id in self._baseline:
            self._save()
            return self.state  # never adopt a similar new job after a lost ID
        if self.state == "cancel_unknown":
            # Both independent list reads must confirm disappearance. A detail
            # 404 alone is not evidence; no ambiguous DELETE is replayed.
            if job_id not in internal and job_id not in public:
                self.state = "cancelled"
        elif job_id in internal and job_id in public:
            detail = self._client.job_detail(job_id)
            row = public[job_id]
            body = self._plan.body()
            if (type(detail) is dict and detail.get("ID") == job_id
                    and self._plan.matches(internal[job_id]) and self._plan.matches(detail)
                    and row["start_time"] == body["Time"]
                    and row["end_time"] == body["Time"] + body["Duration"]):
                self.state = "owned"
            else:
                self.state = "create_unknown"
        else:
            self.state = "create_unknown"
        self._save()
        return self.state

    @_serialized
    def cancel_owned(self, *, confirmed: bool = False) -> str:
        self._write_guard(confirmed, cancelling=True)
        if self.state != "owned" or self._returned_id is None:
            raise RecordingError("ownership_unverified") from None
        self._client.verify_status()
        if self.reconcile() != "owned":
            raise RecordingError("ownership_unverified") from None
        detail = self._client.job_detail(self._returned_id)
        if type(detail) is not dict or not self._plan.matches(detail):
            raise RecordingError("ownership_unverified") from None
        if (detail.get("ID") != self._returned_id or detail.get("RuleID")
                or any(detail.get(key) not in (None, False, "", 0)
                       for key in ("FileID", "Recording", "Active", "Running", "Skipped", "Failed"))):
            raise RecordingError("active_job") from None
        with self._dispatch_lock:
            self._write_guard(confirmed, cancelling=True)  # clock/close may change during reads
            if self.state != "owned":
                raise RecordingError("ownership_unverified") from None
            self.state = "cancel_unknown"
        self._save()
        self._write_guard(confirmed, cancelling=True)
        try:
            self._client.cancel_job(self._returned_id,
                                    expected_start=self._plan.body()["Time"], confirmed=True)
        except Exception:
            self.last_error = "cancel_response_unqualified"
        return self.reconcile()
