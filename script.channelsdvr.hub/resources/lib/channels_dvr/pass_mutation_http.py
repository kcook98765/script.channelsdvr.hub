"""Closed qualification transport, independent of the preview-only client.

No retries/proxies/redirects. Finite budget, bytes and cooperative read deadlines;
a single DNS/socket call can still overrun wall time. No update/pause/job routes.
"""

import json
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener
from .newbuild_compatibility import profile_binding, version_admitted

from .metadata_http import (ChannelsMetadataClient, MetadataError, _NoRedirect,
                            _finite_float, _http_code, _origin, _reject_constant, _unique_object)
from .pass_commands import PassError, PassPlan, RuleDetailAbsent, _OwnedRemoval, serialized, text


def opaque_segment(identity: object) -> str:
    value = text(identity)
    # Dot segments and endpoint aliases must not resolve to collection commands.
    if value.lower() in {".", "..", "new", "test", "priority", "pause", "resume", "batch"}:
        raise PassError("schema_unqualified") from None
    return quote(value, safe="", encoding="utf-8", errors="strict")


class PassMutationClient:
    def __init__(self, origin: str, *, namespace: str, expected_version: str,
                 timeout: float = 5, max_bytes: int = 4194304,
                 request_budget: int = 48, opener=None) -> None:
        ChannelsMetadataClient(origin, namespace=namespace, expected_version=expected_version,
                               timeout=timeout, max_bytes=max_bytes, request_budget=request_budget,
                               opener=opener)
        self._origin, self._version = _origin(origin), expected_version
        self._timeout, self._max_bytes, self._remaining = timeout, max_bytes, request_budget
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
        self._binding = profile_binding(self._origin, namespace, expected_version)
        self._verified = False
        self._sent: set[str] = set()
        self._preview_body: dict | None = None
        self._receipts: list[dict] = []
        self._gate = threading.RLock()

    @property
    def server_binding_sha256(self) -> str:
        return self._binding

    @property
    def receipts(self) -> tuple[dict, ...]:
        return tuple(dict(row) for row in self._receipts)

    def verify_status(self) -> object:
        return self._request("status")

    def rules(self) -> object:
        return self._request("rules")

    def jobs(self) -> object:
        return self._request("jobs")

    def rule_detail(self, identity: str) -> object:
        return self._request("detail", identity=identity)

    def preview(self, plan: PassPlan) -> object:
        return self._request("preview", plan=plan)

    def create(self, plan: PassPlan, *, confirmed: bool = False) -> object:
        return self._request("create", plan=plan, confirmed=confirmed)

    def remove_owned(self, proof: _OwnedRemoval, *, confirmed: bool = False) -> object:
        return self._request("remove", proof=proof, confirmed=confirmed)

    def _request(self, operation: str, *, identity: str | None = None, plan: PassPlan | None = None,
                 confirmed: bool = False, proof: _OwnedRemoval | None = None) -> object:
        with self._gate:
            return self._send(operation, identity=identity, plan=plan, confirmed=confirmed, proof=proof)

    @serialized
    def _send(self, operation: str, *, identity: str | None = None, plan: PassPlan | None = None,
              confirmed: bool = False, proof: _OwnedRemoval | None = None) -> object:
        routes = {"status": "/status", "rules": "/dvr/rules", "jobs": "/dvr/jobs",
                  "detail": "/dvr/rules/", "preview": "/dvr/rules/test",
                  "create": "/dvr/rules/new", "remove": "/dvr/rules/"}
        if operation not in routes:
            raise MetadataError("unknown_operation") from None
        # Every entry into dispatch (including direct internal calls) enforces
        # the same confirmation, typed ownership and closed-parameter guards.
        if operation == "create" and confirmed is not True:
            raise PassError("confirmation_required") from None
        if operation == "remove":
            if (confirmed is not True or identity is not None or plan is not None
                    or type(proof) is not _OwnedRemoval or type(proof.plan) is not PassPlan
                    or proof.server_binding != self._binding or type(proof.baseline_ids) is not tuple
                    or len(proof.baseline_ids) > 1000):
                raise PassError("ownership_unverified") from None
            baseline = [text(value) for value in proof.baseline_ids]
            if len(set(baseline)) != len(baseline) or text(proof.identity) in baseline:
                raise PassError("ownership_unverified") from None
            identity = proof.identity  # DELETE identity ONLY derives from validated proof
        elif proof is not None or (operation != "create" and confirmed is not False):
            raise MetadataError("invalid_argument") from None
        if operation == "status":
            self._verified = False
        elif not self._verified:
            raise MetadataError("identity_not_verified") from None
        path, data, method = routes[operation], None, "GET"
        if operation in {"detail", "remove"}:
            path += opaque_segment(identity)
        elif identity is not None:
            raise MetadataError("invalid_argument") from None
        if operation in {"preview", "create"}:
            if type(plan) is not PassPlan:
                raise PassError("invalid_plan") from None
            if operation == "create" and self._preview_body != plan.conditions():
                raise PassError("ownership_unverified") from None
            data = json.dumps(plan.conditions() if operation == "preview" else plan.body(),
                              ensure_ascii=True, allow_nan=False).encode("ascii")
            method = "POST"
        elif plan is not None:
            raise MetadataError("invalid_argument") from None
        if operation == "remove":
            method = "DELETE"
        if self._remaining == 0 or operation in self._sent:
            raise MetadataError("request_budget") from None
        headers = {"Accept": "application/json", "Accept-Encoding": "identity",
                   "User-Agent": "ChannelsPassOwnership/1"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self._origin + path, data=data, method=method, headers=headers)
        self._remaining -= 1
        if operation in {"preview", "create", "remove"}:
            self._sent.add(operation)  # burn admission before dispatch, regardless of outcome
        started = time.monotonic()
        deadline = started + self._timeout
        status, size, outcome, response = None, 0, "network_error", None
        try:
            try:
                response = self._opener.open(request, timeout=self._timeout)
            except HTTPError as error:
                response = error  # bounded read/parse/close also applies to detail 404
            status = response.status
            if type(status) is not int or not 100 <= status <= 599:
                status = None
                raise MetadataError("network_error")
            if (status != 200 and not (operation == "remove" and status == 204)
                    and not (operation == "detail" and status == 404)):
                raise MetadataError(_http_code(status), status)
            if any(v.strip().lower() != "identity" for v in response.headers.get_all("Content-Encoding", [])):
                raise MetadataError("content_encoding", status)
            chunks = []
            while True:
                if time.monotonic() >= deadline:
                    raise MetadataError("response_timeout", status)
                amount = min(65536, self._max_bytes + 1 - size)
                chunk = response.read1(amount)
                if type(chunk) is not bytes or len(chunk) > amount:
                    raise MetadataError("network_error", status)
                size += len(chunk)
                if time.monotonic() >= deadline:
                    raise MetadataError("response_timeout", status)
                if size > self._max_bytes:
                    raise MetadataError("response_too_large", status)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            media = ""
            if operation == "remove" and not raw:
                result = None
            else:
                types = response.headers.get_all("Content-Type", [])
                media = types[0].split(";", 1)[0].strip().lower() if len(types) == 1 else ""
                if media != "application/json" and not re.fullmatch(r"application/[a-z0-9!#$&^_.+-]+\+json", media):
                    raise MetadataError("content_type", status)
                try:
                    result = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                                        parse_float=_finite_float, object_pairs_hook=_unique_object)
                except (ValueError, UnicodeError, RecursionError):
                    raise MetadataError("malformed_json", status) from None
            if time.monotonic() >= deadline:
                raise MetadataError("response_timeout", status)
            if operation == "detail" and status == 404:
                if result is not None or media != "application/json":
                    raise MetadataError("unsupported", status)
                # Observed DEV contract: named detail GET404, application/json,
                # parsed JSON null. Never generic None, 200null or an unqualified
                # error object. All strict parsing/bounds above still apply.
                outcome = "rule_detail_absent"
                return RuleDetailAbsent(text(identity), self._binding)
            if operation == "status":
                if type(result) is not dict or not version_admitted(self._version, result.get('version')):
                    raise MetadataError("identity_mismatch", status)
                self._verified = True
                result = {"identity_verified": True, "version": result['version']}
            if operation == "preview" and type(result) is list and not result and plan is not None:
                self._preview_body = plan.conditions()
            outcome = "ok"
            return result
        except HTTPError as error:
            status, outcome = error.code, _http_code(error.code)
            error.close()
            raise MetadataError(outcome, status) from None
        except MetadataError as error:
            outcome = error.code
            raise MetadataError(outcome, error.http_status) from None
        except Exception:
            raise MetadataError("network_error", status) from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            self._receipts.append({"operation": operation, "method": method, "status": status or 0,
                                   "bytes": size, "outcome": outcome,
                                   "elapsed_ms": max(0, int((time.monotonic() - started) * 1000))})
