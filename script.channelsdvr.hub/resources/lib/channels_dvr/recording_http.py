"""Closed recording HTTP transport, isolated from the metadata five-GET contract.

No redirects, ambient proxy, cookie storage, retries or caller-selected routes.
Deadlines are checked between bounded read1 operations; DNS or one socket I/O
can overrun wall time (the finite socket idle timeout remains in force).
"""

import json
import re
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from .metadata_http import (
    ChannelsMetadataClient, MetadataError, _NoRedirect, _finite_float,
    _http_code, _origin, _reject_constant, _unique_object,
)
from .recording_commands import WRITE_MARGIN, RecordingError, RecordingPlan, ensure_cancel_future, text
from .newbuild_compatibility import profile_binding, version_admitted


class ChannelsRecordingClient:
    def __init__(self, origin: str, *, namespace: str, expected_version: str,
                 timeout: float = 5, max_bytes: int = 4194304,
                 request_budget: int = 24, opener=None) -> None:
        # Reuse strict configuration validation without broadening its API.
        ChannelsMetadataClient(origin, namespace=namespace, expected_version=expected_version,
                               timeout=timeout, max_bytes=max_bytes,
                               request_budget=request_budget, opener=opener)
        self._origin = _origin(origin)
        self._expected_version = expected_version
        self._server_binding = profile_binding(self._origin, namespace, expected_version)
        self._timeout, self._max_bytes, self._remaining = timeout, max_bytes, request_budget
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
        self._verified = False
        self._receipts: list[dict[str, str | int]] = []

    @property
    def server_binding_sha256(self) -> str:
        """Opaque origin/namespace/historical build association; not verification."""
        return self._server_binding

    @property
    def receipts(self) -> tuple[dict, ...]:
        return tuple(dict(row) for row in self._receipts)

    def verify_status(self) -> object:
        self._verified = False
        result = self._request("verify_status")
        if type(result) is not dict or not version_admitted(self._expected_version, result.get('version')):
            self._receipts[-1]["outcome"] = "identity_mismatch"
            raise MetadataError("identity_mismatch") from None
        self._verified = True
        return {"identity_verified": True, "version": result['version']}

    def jobs(self) -> object:
        return self._request("internal_jobs")

    def public_jobs(self) -> object:
        return self._request("public_jobs")

    def job_detail(self, job_id: str) -> object:
        return self._request("job_detail", job_id=job_id)

    def public_job_detail(self, job_id: str) -> object:
        return self._request("public_job_detail", job_id=job_id)

    def create_job(self, plan: RecordingPlan, *, confirmed: bool = False) -> object:
        if confirmed is not True:
            raise RecordingError("confirmation_required") from None
        if type(plan) is not RecordingPlan:
            raise RecordingError("invalid_argument") from None
        plan.ensure_future(time.time(), margin=self._timeout + 30)
        return self._request("create_job", body=plan.body())

    def cancel_job(self, job_id: str, *, expected_start: float, confirmed: bool = False) -> object:
        if confirmed is not True:
            raise RecordingError("confirmation_required") from None
        ensure_cancel_future(expected_start, time.time(), margin=self._timeout + WRITE_MARGIN)
        return self._request("cancel_owned_job", job_id=job_id)

    def _request(self, operation: str, **params) -> object:
        routes = {"verify_status": ("GET", "/status"),
                  "internal_jobs": ("GET", "/dvr/jobs"),
                  "public_jobs": ("GET", "/api/v1/jobs"),
                  "job_detail": ("GET", "/dvr/jobs/"),
                  "public_job_detail": ("GET", "/api/v1/jobs/"),
                  "create_job": ("POST", "/dvr/jobs/new"),
                  "cancel_owned_job": ("DELETE", "/dvr/jobs/")}
        if operation not in routes:
            raise MetadataError("unknown_operation") from None
        if operation != "verify_status" and not self._verified:
            raise MetadataError("identity_not_verified") from None
        method, path = routes[operation]
        data = None
        expected = ({"job_id"} if operation in {"job_detail", "public_job_detail", "cancel_owned_job"}
                    else {"body"} if operation == "create_job" else set())
        if set(params) != expected:
            raise MetadataError("invalid_argument") from None
        if "job_id" in params:
            job_id = text(params["job_id"])
            # Dot segments and reserved route names are not addressable job IDs.
            if job_id in {".", "..", "new"}:
                raise RecordingError("invalid_argument") from None
            path += quote(job_id, safe="", encoding="utf-8", errors="strict")
        if "body" in params:
            data = json.dumps(params["body"], ensure_ascii=True, allow_nan=False).encode("utf-8")
            if len(data) > 65536:
                raise MetadataError("invalid_argument") from None
        if self._remaining == 0:
            raise MetadataError("request_budget") from None
        headers = {"Accept": "application/json", "Accept-Encoding": "identity",
                   "User-Agent": "ChannelsRecordingClient/1"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self._origin + path, data=data, method=method, headers=headers)
        self._remaining -= 1
        started = time.monotonic()
        deadline = started + self._timeout
        status, size, outcome, response = None, 0, "network_error", None
        try:
            response = self._opener.open(request, timeout=self._timeout)
            status = response.status
            if time.monotonic() >= deadline:
                raise MetadataError("response_timeout", status)
            if status not in ({200, 201} if method == "POST" else {200, 204} if method == "DELETE" else {200}):
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
            body = b"".join(chunks)
            if method == "DELETE" and not body:
                result = None
            else:
                types = response.headers.get_all("Content-Type", [])
                media = types[0].split(";", 1)[0].strip().lower() if len(types) == 1 else ""
                if media != "application/json" and not re.fullmatch(r"application/[a-z0-9!#$&^_.+-]+\+json", media):
                    raise MetadataError("content_type", status)
                try:
                    result = json.loads(body.decode("utf-8"), parse_constant=_reject_constant,
                                        parse_float=_finite_float, object_pairs_hook=_unique_object)
                except (ValueError, UnicodeError, RecursionError):
                    raise MetadataError("malformed_json", status) from None
            if time.monotonic() >= deadline:
                raise MetadataError("response_timeout", status)
            outcome = "ok"
            return result
        except HTTPError as error:
            status, outcome = error.code, _http_code(error.code)
            try:
                error.close()
            except Exception:
                pass
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
                    # Closing is not a write acknowledgement or a retry trigger.
                    pass
            self._receipts.append({"operation": operation, "method": method,
                                   "status": status or 0, "bytes": size, "outcome": outcome,
                                   "elapsed_ms": max(0, int((time.monotonic() - started) * 1000))})
