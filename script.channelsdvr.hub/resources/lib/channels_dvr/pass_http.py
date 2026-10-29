"""Closed preview-only transport: named GETs and one fixed-shape preview POST.

No redirects, proxies, retries, media or rule/job mutations. Read deadlines are
checked between read1 calls; a single DNS/socket operation can overrun wall time.
"""

import hashlib
import json
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from .newbuild_compatibility import profile_binding, version_admitted

from .metadata_http import (
    ChannelsMetadataClient, MetadataError, _NoRedirect, _finite_float,
    _http_code, _origin, _reject_constant, _unique_object,
)


def series_text(value: object) -> str:
    if (type(value) is not str or not value.strip() or len(value) > 256
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise MetadataError("unsupported") from None
    return value  # preserve the exact raw ID, including significant whitespace


def conditions(series_id: str) -> dict:
    return {"EQ": {"SeriesID": series_text(series_id)}, "NE": None, "GT": None,
            "LT": None, "IN": None, "NI": None}


def condition_digest(series_id: str) -> str:
    return hashlib.sha256(json.dumps(conditions(series_id), sort_keys=True,
                                     ensure_ascii=True).encode("ascii")).hexdigest()


class ChannelsPassClient:
    def __init__(self, origin: str, *, namespace: str, expected_version: str,
                 timeout: float = 5, max_bytes: int = 4194304,
                 request_budget: int = 12, opener=None) -> None:
        ChannelsMetadataClient(origin, namespace=namespace, expected_version=expected_version,
                               timeout=timeout, max_bytes=max_bytes,
                               request_budget=request_budget, opener=opener)
        self.namespace = namespace
        self._origin, self._expected_version = _origin(origin), expected_version
        self._timeout, self._max_bytes, self._remaining = timeout, max_bytes, request_budget
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
        self._verified = False
        self._preview_sent = False
        self._receipts: list[dict] = []
        self._binding = profile_binding(self._origin, namespace, expected_version)

    @property
    def server_binding_sha256(self) -> str:
        return self._binding

    @property
    def receipts(self) -> tuple[dict, ...]:
        return tuple(dict(row) for row in self._receipts)

    def verify_status(self) -> object:
        self._verified = False
        return self._request("verify_status")

    def public_channels(self) -> object:
        return self._request("public_channels")

    def guide_window(self, time_s: int) -> object:
        return self._request("guide_window", time_s=time_s)

    def rules(self) -> object:
        return self._request("rules")

    def jobs(self) -> object:
        return self._request("jobs")

    def preview(self, series_id: str) -> object:
        return self._request("preview", series_id=series_id)

    def _request(self, operation: str, **params) -> object:
        routes = {"verify_status": "/status", "public_channels": "/api/v1/channels",
                  "guide_window": "/devices/ANY/guide", "rules": "/dvr/rules",
                  "jobs": "/dvr/jobs", "preview": "/dvr/rules/test"}
        if operation not in routes:
            raise MetadataError("unknown_operation") from None
        if operation != "verify_status" and not self._verified:
            raise MetadataError("identity_not_verified") from None
        expected = {"time_s"} if operation == "guide_window" else {"series_id"} if operation == "preview" else set()
        if set(params) != expected:
            raise MetadataError("invalid_argument") from None
        path, data, method = routes[operation], None, "GET"
        if operation == "guide_window":
            start = params["time_s"]
            if type(start) is not int or not 0 <= start <= 4102444800:
                raise MetadataError("invalid_argument") from None
            path += "?" + urlencode({"time": start, "duration": 1800, "raw": "false"})
        if operation == "preview":
            data = json.dumps(conditions(params["series_id"]), ensure_ascii=True).encode("ascii")
            method = "POST"
            if self._preview_sent:
                raise MetadataError("request_budget") from None
        if self._remaining == 0:
            raise MetadataError("request_budget") from None
        headers = {"Accept": "application/json", "Accept-Encoding": "identity",
                   "User-Agent": "ChannelsPassPreview/1"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self._origin + path, data=data, method=method, headers=headers)
        self._remaining -= 1
        if operation == "preview":
            self._preview_sent = True  # no replay even after ambiguous transport failure
        started = time.monotonic()
        deadline = started + self._timeout
        status, size, outcome, response = None, 0, "network_error", None
        try:
            response = self._opener.open(request, timeout=self._timeout)
            status = response.status
            if status != 200:
                raise MetadataError(_http_code(status), status)
            if any(v.strip().lower() != "identity" for v in response.headers.get_all("Content-Encoding", [])):
                raise MetadataError("content_encoding", status)
            types = response.headers.get_all("Content-Type", [])
            media = types[0].split(";", 1)[0].strip().lower() if len(types) == 1 else ""
            if media != "application/json" and not re.fullmatch(r"application/[a-z0-9!#$&^_.+-]+\+json", media):
                raise MetadataError("content_type", status)
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
            try:
                result = json.loads(b"".join(chunks).decode("utf-8"), parse_constant=_reject_constant,
                                    parse_float=_finite_float, object_pairs_hook=_unique_object)
            except (ValueError, UnicodeError, RecursionError):
                raise MetadataError("malformed_json", status) from None
            if time.monotonic() >= deadline:
                raise MetadataError("response_timeout", status)
            if operation == "verify_status":
                if type(result) is not dict or not version_admitted(self._expected_version, result.get('version')):
                    raise MetadataError("identity_mismatch", status)
                self._verified = True
                result = {"identity_verified": True, "version": result['version']}
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
