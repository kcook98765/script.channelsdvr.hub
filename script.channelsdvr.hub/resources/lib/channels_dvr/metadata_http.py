"""Bounded, explicitly identity-gated Channels metadata reads (stdlib only)."""

from email.message import Message
from dataclasses import dataclass, field
import ipaddress
import json
import math
import re
import time
from typing import Protocol, cast
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from .newbuild_compatibility import parse_build, version_admitted


_CODES = frozenset({
    "invalid_origin", "invalid_argument", "unknown_operation", "unsupported",
    "redirect_refused", "http_error", "network_error", "content_type",
    "content_encoding", "malformed_json", "response_too_large", "request_budget",
    "identity_not_verified", "identity_mismatch", "response_timeout",
})
_PATHS = {
    "verify_status": "/status",
    "public_channels": "/api/v1/channels",
    "guide_window": "/devices/ANY/guide",
    "files_page": "/dvr/files",
    "scheduled_jobs": "/api/v1/jobs",
}

_STATUS_SEAL = object()


@dataclass(frozen=True, eq=False, slots=True)
class StatusSample:
    """Local receipt for a strictly validated HTTP200 /status, not verified identity."""
    reported_version: str
    observed_at: float
    _client: object = field(repr=False)
    _seal: object = field(repr=False)


def status_sample(client: object) -> StatusSample | None:
    # Pure/fixture metadata clients cannot mint a read-status proof by returning
    # a string/property. Exact class and locally issued sample identity required.
    if type(client) is not ChannelsMetadataClient:
        return None
    sample = client._status_sample
    return sample if (type(sample) is StatusSample and valid_status_sample(sample)
                      and sample._client is client._status_identity) else None


def valid_status_sample(sample: object) -> bool:
    return type(sample) is StatusSample and sample._seal is _STATUS_SEAL


class MetadataError(Exception):
    """Only a closed error code and optional HTTP status escape the adapter."""

    def __init__(self, code: str, http_status: int | None = None) -> None:
        self.code = code if code in _CODES else "network_error"
        self.http_status = (http_status if type(http_status) is int
                            and 100 <= http_status <= 599 else None)
        super().__init__(self.code)


class _Response(Protocol):
    status: int
    headers: Message

    def read1(self, amt: int) -> bytes: ...
    def close(self) -> None: ...


class _Opener(Protocol):
    def open(self, fullurl: Request, *, timeout: float) -> _Response: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: object, code: int, msg: str,
                         headers: Message, newurl: str) -> None:
        close = getattr(fp, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        raise MetadataError("redirect_refused", code) from None


def _origin(value: str) -> str:
    try:
        if (not isinstance(value, str) or not value
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
                or "\\" in value or "?" in value or "#" in value):
            raise ValueError
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme not in {"http", "https"} or not host
                or parts.path not in {"", "/"} or "@" in parts.netloc
                or "%" in host or parts.netloc.endswith(":")):
            raise ValueError
        port = parts.port
        if port is not None and (not 1 <= port <= 65535 or port == 57000):
            raise ValueError
        if ":" in host:
            if not re.fullmatch(r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?", parts.netloc):
                raise ValueError
            host = "[" + ipaddress.IPv6Address(host).compressed + "]"
        else:
            host = host.encode("idna").decode("ascii").lower().removesuffix(".")
            if not host or len(host) > 253:
                raise ValueError
            for label in host.split("."):
                if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label):
                    raise ValueError
            if host.rsplit(".", 1)[-1].isdigit():
                host = str(ipaddress.IPv4Address(host))
        suffix = "" if port is None or port == {"http": 80, "https": 443}[parts.scheme] else f":{port}"
        return f"{parts.scheme}://{host}{suffix}"
    except (ValueError, UnicodeError, AttributeError):
        raise MetadataError("invalid_origin") from None


def _integer(value: int, low: int, high: int | None = None) -> bool:
    return type(value) is int and value >= low and (high is None or value <= high)


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError
    return result


def _reject_constant(value: str) -> object:
    raise ValueError


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _http_code(status: int) -> str:
    if status in {404, 405, 501}:
        return "unsupported"
    return "redirect_refused" if 300 <= status < 400 else "http_error"


class ChannelsMetadataClient:
    """Five named GET operations; no ambient proxies, retries, or implicit status reads.

    An injected opener is a trusted transport seam for offline tests. Production
    callers should use the default isolated opener, which refuses all redirects.
    The timeout also sets a total read deadline starting before open, checked
    between bounded HTTPResponse.read1 I/O chunks and before accepting JSON.
    These checks are not a preemptive DNS/socket wall-time guarantee: one I/O
    can overrun the deadline. The connection/socket idle timeout remains active.
    """

    def __init__(self, origin: str, *, namespace: str, expected_version: str,
                 timeout: float = 5, max_bytes: int = 4194304,
                 request_budget: int = 24, opener: _Opener | None = None) -> None:
        self._origin = _origin(origin)
        if (not isinstance(namespace, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", namespace)
                or not isinstance(expected_version, str) or not expected_version
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in expected_version)
                or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0.1 <= timeout <= 30 or not math.isfinite(timeout)
                or not _integer(max_bytes, 1, 8 * 1024 * 1024)
                or not _integer(request_budget, 1, 256)):
            raise MetadataError("invalid_argument") from None
        self.namespace = namespace
        self._expected_version = expected_version
        self._timeout = float(timeout)
        self._max_bytes = max_bytes
        self._remaining = request_budget
        self._verified = False
        self._status_identity = object()
        self._status_sample: StatusSample | None = None
        self._opener = opener if opener is not None else build_opener(
            ProxyHandler({}), _NoRedirect())
        self._receipts: list[dict[str, str | int]] = []

    @property
    def receipts(self) -> tuple[dict[str, str | int], ...]:
        """Defensive snapshots: callers cannot mutate retained receipt evidence."""
        return tuple(dict(receipt) for receipt in self._receipts)

    def verify_status(self) -> dict[str, str | bool]:
        return cast("dict[str, str | bool]", self._request("verify_status"))

    @property
    def reported_version(self) -> str | None:
        return self._status_sample.reported_version if self._status_sample else None

    def public_channels(self) -> object:
        return self._request("public_channels")

    def guide_window(self, time_s: int, duration: int = 1800) -> object:
        return self._request("guide_window", time_s=time_s, duration=duration)

    def files_page(self, limit: int = 100, offset: int = 0) -> object:
        return self._request("files_page", limit=limit, offset=offset)

    def scheduled_jobs(self) -> object:
        return self._request("scheduled_jobs")

    def _request(self, operation: str, **params: int) -> object:
        if operation == 'verify_status':
            self._verified = False
            self._status_sample = None  # before budget/argument failures and I/O
        if operation not in _PATHS:
            raise MetadataError("unknown_operation") from None
        if operation != "verify_status" and not self._verified:
            raise MetadataError("identity_not_verified") from None
        query: dict[str, str | int] = {}
        if operation == "guide_window":
            if (set(params) != {"time_s", "duration"}
                    or not _integer(params["time_s"], 0)
                    or type(params["duration"]) is not int or params["duration"] != 1800):
                raise MetadataError("invalid_argument") from None
            query = {"time": params["time_s"], "duration": 1800, "raw": "false"}
        elif operation == "files_page":
            if (set(params) != {"limit", "offset"}
                    or not _integer(params["limit"], 1, 100)
                    or not _integer(params["offset"], 0, 1000000)):
                raise MetadataError("invalid_argument") from None
            query = {"raw": "false", "sort": "date_added", "order": "desc",
                     "limit": params["limit"], "offset": params["offset"]}
        elif params:
            raise MetadataError("invalid_argument") from None
        if self._remaining == 0:
            raise MetadataError("request_budget") from None
        path = _PATHS[operation] + ("?" + urlencode(query) if query else "")
        request = Request(self._origin + path, method="GET", headers={
            "Accept": "application/json", "Accept-Encoding": "identity",
            "User-Agent": "ChannelsMetadataClient/1",
        })
        self._remaining -= 1
        if operation == "verify_status":
            self._verified = False
        started = time.monotonic()
        deadline = started + self._timeout
        status: int | None = None
        size = 0
        outcome = "network_error"
        response: _Response | None = None
        try:
            response = self._opener.open(request, timeout=self._timeout)
            status = response.status
            if time.monotonic() >= deadline:
                raise MetadataError("response_timeout", status)
            if status != 200:
                raise MetadataError(_http_code(status), status)
            encodings = response.headers.get_all("Content-Encoding", [])
            if any(value.strip().lower() != "identity" for value in encodings):
                raise MetadataError("content_encoding", status)
            types = response.headers.get_all("Content-Type", [])
            media_type = types[0].split(";", 1)[0].strip().lower() if len(types) == 1 else ""
            if (media_type != "application/json"
                    and not re.fullmatch(r"application/[a-z0-9!#$&^_.+-]+\+json", media_type)):
                raise MetadataError("content_type", status)
            # Never fall back to read(n): that may wait to fill n bytes while a
            # slow peer continually resets the socket's idle timeout.
            read1 = response.read1
            chunks: list[bytes] = []
            while True:
                if time.monotonic() >= deadline:
                    raise MetadataError("response_timeout", status)
                amount = min(65536, self._max_bytes + 1 - size)
                chunk = read1(amount)
                if not isinstance(chunk, bytes) or len(chunk) > amount:
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
            try:
                result = json.loads(body.decode("utf-8", errors="strict"),
                                    parse_constant=_reject_constant, parse_float=_finite_float,
                                    object_pairs_hook=_unique_object)
            except (ValueError, UnicodeError, RecursionError):
                raise MetadataError("malformed_json", status) from None
            if time.monotonic() >= deadline:
                raise MetadataError("response_timeout", status)
            if operation == "verify_status":
                value = result.get('version') if type(result) is dict else None
                if type(value) is str and parse_build(value) is not None:
                    self._status_sample = StatusSample(value, time.monotonic(),
                                                      self._status_identity, _STATUS_SEAL)
                if (type(result) is not dict
                        or not version_admitted(self._expected_version, result.get('version'))):
                    raise MetadataError("identity_mismatch", status)
                result = {"version": result['version'], "identity_verified": True}
                self._verified = True
            outcome = "ok"
            return result
        except HTTPError as error:
            status = error.code
            outcome = _http_code(status)
            try:
                error.close()
            except Exception:
                pass
            raise MetadataError(outcome, status) from None
        except MetadataError as error:
            outcome = error.code
            status = error.http_status if error.http_status is not None else status
            raise MetadataError(outcome, status) from None
        except Exception:
            raise MetadataError("network_error", status) from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            self._receipts.append({
                "operation": operation, "method": "GET", "status": status or 0,
                "bytes": size, "outcome": outcome,
                "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
            })
