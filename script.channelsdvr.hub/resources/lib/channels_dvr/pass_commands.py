"""Pure, serialized, one-create/owned-remove qualification. No Kodi or file I/O.

The required checkpoint callback is a durability contract, not optional telemetry.
Unknown writes are never retried. Lost IDs never authorize resemblance adoption.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
import re
import threading
from typing import Callable, NoReturn, Protocol


_CODES = frozenset({"invalid_plan", "schema_unqualified", "binding_mismatch",
                    "checkpoint_failed", "confirmation_required", "duplicate_blocked",
                    "ownership_unverified", "baseline_changed", "closed"})
MAPS = ("EQ", "NE", "GT", "LT", "IN", "NI")
STATES = frozenset({"ready", "create_unknown", "owned", "delete_unknown", "deleted"})


class PassError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code if code in _CODES else "schema_unqualified"
        super().__init__(self.code)


def text(value: object, limit: int = 256) -> str:
    if (type(value) is not str or not value.strip() or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise PassError("schema_unqualified") from None
    return value


def canonical(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":"))
    except (ValueError, TypeError, RecursionError):
        raise PassError("schema_unqualified") from None


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()


@dataclass(frozen=True, repr=False)
class PassPlan:
    """Only a fresh synthetic conjunction; never a general pass editor."""

    nonce: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.nonce) is not str or not re.fullmatch(r"[0-9a-f]{32}", self.nonce):
            raise PassError("invalid_plan") from None

    def __repr__(self) -> str:
        return "PassPlan(private_synthetic=True)"

    def conditions(self) -> dict:
        return {"EQ": {"SeriesID": "dev-pass-series-" + self.nonce,
                       "Title": "dev-pass-title-" + self.nonce},
                **{key: None for key in MAPS if key != "EQ"}}

    def body(self) -> dict:
        # Y2 serializer: omit undefined Image/ID, preserve null maps and defaults.
        # Paused is NOT in Y2. This is not an atomic paused-create protocol.
        return {"Name": "DEV disposable pass " + self.nonce, **self.conditions(),
                "PaddingStart": 0, "PaddingEnd": 0, "KeepOnly": "", "KeepNum": 0,
                "Rerecord": False, "Limit": 0}

    def matches(self, row: object, identity: str) -> bool:
        return (type(row) is dict and row.get("ID") == identity
                and all(key in row and canonical(row[key]) == canonical(value)
                        for key, value in self.body().items())
                and row.get("Paused", False) is False
                and ("NumJobs" not in row or (type(row["NumJobs"]) is int and row["NumJobs"] == 0)))


def rows(payload: object, *, jobs: bool = False) -> dict[str, dict]:
    """Accept only entire bounded arrays, validate every row, reject duplicate IDs.

    Full rows are retained privately: baseline comparison detects field changes,
    not just insertions. No pagination/envelope/partial-result interpretation.
    """
    if type(payload) is not list or len(payload) > 1000:
        raise PassError("schema_unqualified") from None
    result = {}
    for row in payload:
        if type(row) is not dict or len(row) > 128 or len(canonical(row)) > 131072:
            raise PassError("schema_unqualified") from None
        identity = text(row.get("ID"))
        if identity in result:
            raise PassError("schema_unqualified") from None
        if jobs:
            for key, low, high in (("Time", 946684800, 4102444800), ("Duration", 1, 86400)):
                if type(row.get(key)) is not int or not low <= row[key] <= high:
                    raise PassError("schema_unqualified") from None
            if row.get("RuleID") not in (None, ""):
                text(row["RuleID"])
        else:
            text(row.get("Name"), 512)
            for key in MAPS:
                if key not in row or (row[key] is not None and type(row[key]) is not dict):
                    raise PassError("schema_unqualified") from None
            for key in ("PaddingStart", "PaddingEnd", "KeepNum", "Limit"):
                if type(row.get(key)) is not int or not 0 <= row[key] <= 10**9:
                    raise PassError("schema_unqualified") from None
            if type(row.get("KeepOnly")) is not str or type(row.get("Rerecord")) is not bool:
                raise PassError("schema_unqualified") from None
        result[identity] = deepcopy(row)
    return result


@dataclass(frozen=True, repr=False)
class RuleDetailAbsent:
    """Only the named detail GET's validated not-found response, privately bound."""

    identity: str
    server_binding: str

    def __repr__(self) -> str:
        return "RuleDetailAbsent(qualified_named_get=True)"

    def matches(self, identity: str, server_binding: str) -> bool:
        return self.identity == identity and self.server_binding == server_binding


@dataclass(frozen=True, repr=False)
class _OwnedRemoval:
    identity: str
    plan: PassPlan
    server_binding: str
    baseline_ids: tuple[str, ...]


class PassClient(Protocol):
    @property
    def server_binding_sha256(self) -> str: ...
    def verify_status(self) -> object: ...
    def rules(self) -> object: ...
    def jobs(self) -> object: ...
    def rule_detail(self, identity: str) -> object: ...
    def preview(self, plan: PassPlan) -> object: ...
    def create(self, plan: PassPlan, *, confirmed: bool) -> object: ...
    def remove_owned(self, proof: _OwnedRemoval, *, confirmed: bool) -> object: ...


def binding(client: PassClient, saved: dict | None = None) -> str:
    value = client.server_binding_sha256
    if (type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value)
            or (saved is not None and saved.get("server_binding") != value)):
        raise PassError("binding_mismatch") from None
    return value


def serialized(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        with self._gate:
            return method(self, *args, **kwargs)
    return invoke


class PassCommand:
    """Complete operations/checkpoints/close share one gate.

    close blocks behind in-flight I/O/persistence, then bars further writes on
    this instance. Restart is read-only unless remove_owned is explicitly called;
    the new instance may perform its FIRST delete, never repeat an unknown delete.
    Persistence failure permanently bars writes on this instance.
    """

    def __init__(self, client: PassClient, plan: PassPlan, *, checkpoint: Callable[[dict], None]) -> None:
        if not callable(checkpoint) or type(plan) is not PassPlan:
            raise PassError("checkpoint_failed") from None
        self._client, self._plan, self._checkpoint = client, plan, checkpoint
        self._binding = binding(client)
        self._gate = threading.RLock()
        self._state = "ready"
        self._id: str | None = None
        self._rules: dict[str, dict] = {}
        self._jobs: dict[str, dict] = {}
        self._blocked = self._closed = self._storage_failed = False

    @property
    def state(self) -> str:
        return self._state

    def __repr__(self) -> str:
        return f"PassCommand(state={self._state!r}, blocked={self._blocked!r})"

    @serialized
    def private_checkpoint(self) -> dict:
        return {"version": 1, "state": self._state, "returned_id": self._id,
                "server_binding": self._binding, "plan": self._plan.body(),
                "nonce": self._plan.nonce, "baseline_rules": deepcopy(list(self._rules.values())),
                "baseline_jobs": deepcopy(list(self._jobs.values())), "blocked": self._blocked}

    def _save(self) -> None:
        try:
            self._checkpoint(self.private_checkpoint())
        except Exception:
            self._storage_failed = True
            raise PassError("checkpoint_failed") from None

    @classmethod
    def resume(cls, client: PassClient, saved: dict, *, checkpoint: Callable[[dict], None]) -> "PassCommand":
        if type(saved) is not dict:
            raise PassError("schema_unqualified") from None
        binding(client, saved)  # before ANY I/O, even terminal receipt recognition
        if (set(saved) != {"version", "state", "returned_id", "server_binding", "plan", "nonce",
                           "baseline_rules", "baseline_jobs", "blocked"}
                or type(saved["version"]) is not int or saved["version"] != 1
                or saved["state"] not in STATES or type(saved["blocked"]) is not bool):
            raise PassError("schema_unqualified") from None
        plan = PassPlan(saved["nonce"])
        if canonical(plan.body()) != canonical(saved["plan"]):
            raise PassError("ownership_unverified") from None
        command = cls(client, plan, checkpoint=checkpoint)
        command._rules = rows(saved["baseline_rules"])
        command._jobs = rows(saved["baseline_jobs"], jobs=True)
        identity = saved["returned_id"]
        if identity is not None:
            text(identity)
        if (identity in command._rules
                or (saved["state"] in {"owned", "delete_unknown", "deleted"} and identity is None)
                or (saved["state"] == "ready" and identity is not None)):
            raise PassError("ownership_unverified") from None
        command._state, command._id, command._blocked = saved["state"], identity, saved["blocked"]
        # A persisted ready intent is never a second create authorization.
        if command._state == "ready":
            command._blocked = True
        return command

    def _write_guard(self, confirmed: bool) -> None:
        binding(self._client, {"server_binding": self._binding})
        if self._closed:
            raise PassError("closed") from None
        if self._storage_failed:
            raise PassError("checkpoint_failed") from None
        if confirmed is not True:
            raise PassError("confirmation_required") from None
        if self._blocked:
            raise PassError("ownership_unverified") from None

    def _read(self) -> tuple[dict, dict]:
        binding(self._client, {"server_binding": self._binding})
        self._client.verify_status()
        return rows(self._client.rules()), rows(self._client.jobs(), jobs=True)

    def _stop(self) -> NoReturn:
        self._blocked = True
        if self._state == "owned":
            self._state = "create_unknown"
        self._save()
        raise PassError("baseline_changed") from None

    @serialized
    def create(self, *, confirmed: bool = False) -> str:
        self._write_guard(confirmed)
        if self._state != "ready":
            raise PassError("duplicate_blocked") from None
        self._rules, self._jobs = self._read()
        if any(row["Name"] == self._plan.body()["Name"] for row in self._rules.values()):
            self._stop()
        # Empty means EXACT complete empty JSON array, not null/error/partial.
        preview = self._client.preview(self._plan)
        if type(preview) is not list or preview:
            self._stop()
        fresh_rules, fresh_jobs = self._read()
        if canonical(fresh_rules) != canonical(self._rules) or canonical(fresh_jobs) != canonical(self._jobs):
            self._stop()
        self._state = "create_unknown"
        self._save()  # must durably succeed BEFORE create callback/HTTP
        # Persistence can be slow. Recheck complete baselines after fsync, too.
        self.reconcile()
        self._write_guard(confirmed)
        try:
            result = self._client.create(self._plan, confirmed=True)
        except Exception:
            return self._state  # lost ID: never scan/adopt even an exact name
        if type(result) is not dict:
            return self._state
        try:
            identity = text(result.get("ID"))
        except PassError:
            return self._state
        if identity in self._rules:
            self._stop()
        self._id = identity
        self._save()  # ID retained even if returned fields/readback fail
        if not self._plan.matches(result, identity):
            self._stop()
        return self.reconcile()

    @serialized
    def reconcile(self) -> str:
        binding(self._client, {"server_binding": self._binding})
        if self._state in {"ready", "deleted"}:
            return self._state  # terminal recognition, not a fresh absence claim
        rules, jobs = self._read()
        foreign = {key: value for key, value in rules.items() if key != self._id}
        if (canonical(jobs) != canonical(self._jobs)
                or canonical(foreign) != canonical(self._rules)
                or (self._id is not None and any(row.get("RuleID") == self._id for row in jobs.values()))):
            self._stop()
        if self._id is None:
            return self._state
        if self._state == "delete_unknown":
            if self._id not in rules and not self._blocked:
                try:
                    detail = self._client.rule_detail(self._id)
                except Exception:
                    return self._state  # timeout/malformed/unsupported are NOT absence
                if type(detail) is RuleDetailAbsent and detail.matches(self._id, self._binding):
                    self._state = "deleted"
                    self._save()
            return self._state  # presence NEVER authorizes another DELETE
        if self._id not in rules:
            self._stop()
        self._verify_rule(rules)
        if not self._blocked:
            self._state = "owned"
            self._save()
        return self._state

    def _verify_rule(self, rules: dict) -> None:
        if self._id is None or self._id not in rules:
            self._stop()
        detail = self._client.rule_detail(self._id)
        if not self._plan.matches(rules[self._id], self._id) or not self._plan.matches(detail, self._id):
            self._stop()

    @serialized
    def remove_owned(self, *, confirmed: bool = False) -> str:
        """No ID parameter: only this lifecycle's newly returned, verified rule."""
        self._write_guard(confirmed)
        if self._state != "owned" or self._id is None:
            raise PassError("ownership_unverified") from None
        if self.reconcile() != "owned":
            raise PassError("ownership_unverified") from None
        self._state = "delete_unknown"
        self._save()
        # Recheck after durable write-ahead, without turning this uncertainty
        # back into owned or permitting any future replay of the DELETE.
        rules, jobs = self._read()
        foreign = {key: value for key, value in rules.items() if key != self._id}
        if (canonical(jobs) != canonical(self._jobs)
                or canonical(foreign) != canonical(self._rules)
                or any(row.get("RuleID") == self._id for row in jobs.values())):
            self._stop()
        self._verify_rule(rules)
        self._write_guard(confirmed)
        proof = _OwnedRemoval(self._id, self._plan, self._binding, tuple(self._rules))
        try:
            self._client.remove_owned(proof, confirmed=True)
        except Exception:
            pass
        return self.reconcile()

    @serialized
    def close(self) -> None:
        self._closed = True
        self._save()
