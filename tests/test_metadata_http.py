"""Synthetic transport contracts only: explicit fakes, never sockets or Kodi."""

from email.message import Message
import json
import socket
from typing import Callable, IO, cast
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from channels_dvr.metadata_http import (
    ChannelsMetadataClient, MetadataError, _NoRedirect,
)


ORIGIN = "https://metadata.example"
VERSION = "synthetic-build"


class FakeResponse:
    def __init__(self, body: bytes = b"[]", *, status: int = 200,
                 content_type: str | None = "application/json",
                 encoding: str | None = None, fail_read: bool = False) -> None:
        self.body = body
        self.status = status
        self.headers = Message()
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        if encoding is not None:
            self.headers["Content-Encoding"] = encoding
        self.read_sizes: list[int] = []
        self.closed = False
        self.fail_read = fail_read
        self.position = 0

    def read(self, amt: int) -> bytes:
        raise AssertionError("Full read must never be used")

    def read1(self, amt: int) -> bytes:
        self.read_sizes.append(amt)
        if self.fail_read:
            raise OSError("secret body at https://hidden.example/private")
        chunk = self.body[self.position:self.position + amt]
        self.position += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[Request, float]] = []
        self.on_open: Callable[[], None] | None = None

    def open(self, fullurl: Request, *, timeout: float) -> FakeResponse:
        self.calls.append((fullurl, timeout))
        if self.on_open is not None:
            self.on_open()
        if not self.responses:
            raise AssertionError("Unexpected transport call")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlowResponse(FakeResponse):
    def __init__(self, body: bytes, clock: FakeClock, *, chunk_size: int = 1,
                 delay: float = 2) -> None:
        super().__init__(body)
        self.clock = clock
        self.chunk_size = chunk_size
        self.delay = delay

    def read1(self, amt: int) -> bytes:
        result = super().read1(min(amt, self.chunk_size))
        self.clock.advance(self.delay)
        return result


def status_response(version: object = VERSION, **fields: object) -> FakeResponse:
    return FakeResponse(json.dumps({"version": version, **fields}).encode("utf-8"))


class MetadataHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        # Defence in depth alongside the project's global socket guard.
        self.socket_guard = patch.object(socket, "socket", side_effect=AssertionError("No sockets"))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)

    def client(self, *responses: FakeResponse | Exception,
               verified: bool = True, **options: object) -> tuple[ChannelsMetadataClient, FakeOpener]:
        opener = FakeOpener(*((status_response(),) if verified else ()), *responses)
        args: dict[str, object] = {"namespace": "synthetic", "expected_version": VERSION,
                                   "opener": opener, **options}
        client = ChannelsMetadataClient(ORIGIN, **args)  # type: ignore[arg-type]
        if verified:
            client.verify_status()
        return client, opener

    def error(self, code: str, call: object) -> MetadataError:
        with self.assertRaises(MetadataError) as caught:
            call()  # type: ignore[operator]
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertTrue(caught.exception.__suppress_context__)
        return caught.exception

    def test_origin_rejects_unsafe_authorities(self) -> None:
        origins = ["", " ", "http://", "ftp://metadata.example", "https://user:secret@metadata.example",
                   "https://metadata.example/path", "https://metadata.example?", "https://metadata.example#",
                   "https://metadata.example\\other", "https://meta data.example", "https://metadata.example\n",
                   "https://metadata.example\x00", "https://%65xample.test", "https://[2001:db8::1%25eth0]",
                   "https://metadata.example:0", "https://metadata.example:65536",
                   "https://metadata.example:bad", "https://metadata.example:",
                   "https://metadata.example:57000", "http://[2001:db8::1]evil", "http://-bad.example",
                   "http://bad_.example", "http://example..", "http://192.0.2", "http://3221225985"]
        for origin in origins:
            with self.subTest(origin=origin):
                opener = FakeOpener()
                self.error("invalid_origin", lambda: ChannelsMetadataClient(
                    origin, namespace="synthetic", expected_version=VERSION, opener=opener))
                self.assertEqual(opener.calls, [])

    def test_origin_normalization(self) -> None:
        for origin, normalized in [
            ("HTTPS://METAdata.example:443/", "https://metadata.example/status"),
            ("http://metadata.example.:80", "http://metadata.example/status"),
            ("http://metadata.example:8080/", "http://metadata.example:8080/status"),
            ("https://[2001:0db8:0:0:0:0:0:1]:443/", "https://[2001:db8::1]/status"),
            ("https://b\u00fccher.example", "https://xn--bcher-kva.example/status"),
        ]:
            with self.subTest(origin=origin):
                opener = FakeOpener(status_response())
                client = ChannelsMetadataClient(origin, namespace="synthetic", expected_version=VERSION,
                                                opener=opener)
                client.verify_status()
                self.assertEqual(opener.calls[0][0].full_url, normalized)

    def test_constructor_limits(self) -> None:
        invalid: dict[str, list[object]] = {
            "namespace": ["", " ", "https://metadata.example", "a/b", "a?x", "a\n"],
            "expected_version": ["", " ", "v\n", True],
            "timeout": [True, 0, .09, 30.1, 10 ** 1000, float("inf"), float("nan"), "5"],
            "max_bytes": [True, 0, -1, 8388609, 1.5],
            "request_budget": [True, 0, -1, 1.5, 257],
        }
        for name, values in invalid.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    self.error("invalid_argument", lambda: self.client(verified=False, **{name: value}))
        for timeout in (.1, 30):
            self.client(verified=False, timeout=timeout, max_bytes=8388608, request_budget=1)

    def test_identity_guard_all_operations_without_network(self) -> None:
        client, opener = self.client(verified=False)
        for call in (client.public_channels, lambda: client.guide_window(0), client.files_page,
                     client.scheduled_jobs, lambda: client._request("scheduled_jobs")):
            self.error("identity_not_verified", call)
        self.assertEqual(opener.calls, [])
        self.assertEqual(client.receipts, ())

    def test_status_minimal_and_no_retained_account_body(self) -> None:
        client, opener = self.client(status_response(account="secret-account", token="secret-token"),
                                     verified=False)
        result = client.verify_status()
        self.assertEqual(result, {"version": VERSION, "identity_verified": True})
        self.assertEqual(len(opener.calls), 1)
        self.assertNotIn("secret-account", repr(vars(client)))
        self.assertNotIn("secret-token", repr(client.receipts))

    def test_status_mismatch_missing_and_wrong_type(self) -> None:
        for response in (status_response("other"), status_response(1), FakeResponse(b"{}"),
                         FakeResponse(b"[]"), FakeResponse(b"null")):
            client, opener = self.client(response, verified=False)
            self.error("identity_mismatch", client.verify_status)
            self.error("identity_not_verified", client.public_channels)
            self.assertEqual(len(opener.calls), 1)
            self.assertEqual(client.receipts[0]["outcome"], "identity_mismatch")

    def test_explicit_reverification_failure_revokes_identity(self) -> None:
        client, opener = self.client(status_response("changed"))
        self.error("identity_mismatch", client.verify_status)
        self.error("identity_not_verified", client.scheduled_jobs)
        self.assertEqual(len(opener.calls), 2)

    def test_all_five_closed_gets_exact_headers_and_queries(self) -> None:
        client, opener = self.client(*(FakeResponse(b'{"items":[]}') for _ in range(4)))
        self.assertEqual(client.public_channels(), {"items": []})
        client.guide_window(1760000000)
        client.files_page()
        client.scheduled_jobs()
        self.assertEqual(len(opener.calls), 5)
        expected = [
            ("/status", ""), ("/api/v1/channels", ""),
            ("/devices/ANY/guide", "time=1760000000&duration=1800&raw=false"),
            ("/dvr/files", "raw=false&sort=date_added&order=desc&limit=100&offset=0"),
            ("/api/v1/jobs", ""),
        ]
        for (request, timeout), (path, query) in zip(opener.calls, expected):
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(timeout, 5.0)
            self.assertEqual(urlsplit(request.full_url).path, path)
            self.assertEqual(urlsplit(request.full_url).query, query)
            self.assertEqual(dict(request.header_items()), {
                "Accept": "application/json", "Accept-encoding": "identity",
                "User-agent": "ChannelsMetadataClient/1"})

    def test_guide_limits_time_not_start(self) -> None:
        client, opener = self.client(FakeResponse())
        for time_s in (-1, True, 1.5, "10"):
            self.error("invalid_argument", lambda: client.guide_window(time_s))  # type: ignore[arg-type]
        for duration in (True, 0, 1799, 1801, 1800.0):
            self.error("invalid_argument", lambda: client.guide_window(0, duration))  # type: ignore[arg-type]
        client.guide_window(0)
        self.assertEqual(parse_qs(urlsplit(opener.calls[-1][0].full_url).query),
                         {"time": ["0"], "duration": ["1800"], "raw": ["false"]})
        self.assertEqual(len(opener.calls), 2)

    def test_files_page_limits_and_encoding(self) -> None:
        client, opener = self.client(FakeResponse())
        for limit, offset in ((0, 0), (101, 0), (True, 0), (1, -1), (1, True),
                              (1, 1000001), (1.0, 0), (1, "0")):
            self.error("invalid_argument", lambda: client.files_page(limit, offset))  # type: ignore[arg-type]
        client.files_page(1, 1000000)
        self.assertEqual(urlsplit(opener.calls[-1][0].full_url).query,
                         "raw=false&sort=date_added&order=desc&limit=1&offset=1000000")
        self.assertEqual(len(opener.calls), 2)

    def test_private_dispatch_rejects_unknown_and_path_injection(self) -> None:
        client, opener = self.client()
        for operation in ("/status", "https://other.example", "devices", "HEAD", "POST", "logs", "settings"):
            self.error("unknown_operation", lambda: client._request(operation))
        self.error("invalid_argument", lambda: client._request("public_channels", path=1))
        self.error("invalid_argument", lambda: client._request("guide_window", time_s=0, duration=1800, start=0))
        self.assertEqual(len(opener.calls), 1)

    def test_default_transport_no_environment_proxy_or_cookie_handlers(self) -> None:
        with patch("channels_dvr.metadata_http.build_opener") as build:
            ChannelsMetadataClient(ORIGIN, namespace="synthetic", expected_version=VERSION)
        proxy, redirect = build.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsInstance(redirect, _NoRedirect)
        self.assertEqual(len(build.call_args.args), 2)

    def test_redirect_handler_never_follows_any_destination(self) -> None:
        handler = _NoRedirect()
        for code in (301, 302, 303, 307, 308):
            for destination in ("/status", "https://other.example/secret", "file:///secret"):
                response = FakeResponse()
                error = self.error("redirect_refused", lambda: handler.redirect_request(
                    Request(ORIGIN), response, code, "secret", Message(), destination))
                self.assertEqual(error.http_status, code)
                self.assertTrue(response.closed)
                self.assertEqual(response.read_sizes, [])

    def test_redirect_responses_no_read_or_retry(self) -> None:
        for code in (300, 301, 302, 303, 304, 307, 308, 399):
            response = FakeResponse(b"secret", status=code)
            client, opener = self.client(response)
            self.assertEqual(self.error("redirect_refused", client.public_channels).http_status, code)
            self.assertEqual(response.read_sizes, [])
            self.assertTrue(response.closed)
            self.assertEqual(len(opener.calls), 2)

    def test_unsupported_and_other_http_statuses(self) -> None:
        for status in (201, 204, 400, 401, 403, 404, 405, 429, 500, 501, 503):
            client, opener = self.client(FakeResponse(status=status))
            code = "unsupported" if status in (404, 405, 501) else "http_error"
            self.assertEqual(self.error(code, client.files_page).http_status, status)
            self.assertEqual(len(opener.calls), 2)

    def test_http_error_exception_sanitized_and_closed(self) -> None:
        for status, code in ((302, "redirect_refused"), (404, "unsupported"), (500, "http_error")):
            body = FakeResponse(b"secret-body")
            failure = HTTPError("https://hidden.example/secret", status, "secret", Message(),
                                cast(IO[bytes], body))
            client, opener = self.client(failure)
            error = self.error(code, client.scheduled_jobs)
            self.assertEqual(error.http_status, status)
            self.assertTrue(body.closed)
            self.assertEqual(body.read_sizes, [])
            self.assertNotIn("secret", repr(client.receipts))
            self.assertEqual(len(opener.calls), 2)

    def test_network_exceptions_private_no_retry(self) -> None:
        for failure in (TimeoutError("secret"), URLError("https://hidden.example/secret"),
                        OSError("secret"), RuntimeError("secret")):
            client, opener = self.client(failure)
            error = self.error("network_error", client.public_channels)
            self.assertIsNone(error.http_status)
            self.assertEqual(client.receipts[-1]["outcome"], "network_error")
            self.assertEqual(len(opener.calls), 2)

    def test_content_types_accept_json_and_structured_suffix(self) -> None:
        for content_type in ("application/json", "Application/JSON; charset=utf-8",
                             "application/vnd.synthetic+json", "application/problem+json"):
            client, _ = self.client(FakeResponse(b'{"raw":true}', content_type=content_type))
            self.assertEqual(client.public_channels(), {"raw": True})

    def test_wrong_missing_duplicate_content_types(self) -> None:
        for content_type in (None, "text/json", "text/html", "application/jsonp", "application/+json"):
            response = FakeResponse(content_type=content_type)
            client, _ = self.client(response)
            self.error("content_type", client.public_channels)
            self.assertEqual(response.read_sizes, [])
        response = FakeResponse()
        response.headers["Content-Type"] = "application/json"
        client, _ = self.client(response)
        self.error("content_type", client.public_channels)

    def test_encoding_identity_only_no_decompression(self) -> None:
        for encoding in ("gzip", "br", "deflate", "identity,gzip", ""):
            response = FakeResponse(encoding=encoding)
            client, _ = self.client(response)
            self.error("content_encoding", client.public_channels)
            self.assertEqual(response.read_sizes, [])
        client, _ = self.client(FakeResponse(encoding=" Identity "))
        self.assertEqual(client.public_channels(), [])

    def test_max_plus_one_read_exact_and_overflow(self) -> None:
        # Budget includes status, whose minimal body requires 29 bytes.
        response = FakeResponse(b" " * 31 + b"0")
        client, _ = self.client(response, max_bytes=32)
        self.assertEqual(client.public_channels(), 0)
        self.assertEqual(response.read_sizes, [33, 1])
        response = FakeResponse(b"x" * 100)
        client, _ = self.client(response, max_bytes=32)
        self.error("response_too_large", client.public_channels)
        self.assertEqual(response.read_sizes, [33])
        self.assertEqual(client.receipts[-1]["bytes"], 33)

    def test_strict_utf8_and_json_no_body_in_errors(self) -> None:
        for body in (b"", b"secret-body", b"{} trailing-secret", b"\xff", b"NaN", b"Infinity",
                     b"-Infinity", b"1e999", b'{"secret":NaN}', b'"raw\x01"', b"\xef\xbb\xbf{}"):
            client, _ = self.client(FakeResponse(body))
            error = self.error("malformed_json", client.public_channels)
            self.assertNotIn("secret", repr(error))
            self.assertNotIn("secret", repr(client.receipts))

    def test_raw_json_returned_without_filtering(self) -> None:
        for body, expected in ((b"null", None), (b"true", True), (b"12", 12),
                               (b'"synthetic"', "synthetic"), (b"[]", []),
                               (b'{"unrecognized":[1,false]}', {"unrecognized": [1, False]})):
            client, _ = self.client(FakeResponse(body))
            self.assertEqual(client.public_channels(), expected)

    def test_budget_counts_status_success_and_failures_before_io(self) -> None:
        client, opener = self.client(TimeoutError("secret"), FakeResponse(), request_budget=3)
        self.error("network_error", client.public_channels)
        client.scheduled_jobs()
        self.error("request_budget", client.files_page)
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(len(client.receipts), 3)
        self.assertEqual([r["outcome"] for r in client.receipts], ["ok", "network_error", "ok"])

    def test_failed_verification_counts_budget_without_hidden_status_requests(self) -> None:
        client, opener = self.client(status_response("wrong"), verified=False, request_budget=1)
        self.error("identity_mismatch", client.verify_status)
        self.error("identity_not_verified", client.public_channels)
        self.error("request_budget", client.verify_status)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(len(client.receipts), 1)

    def test_receipts_safe_defensive_readonly_snapshots(self) -> None:
        client, _ = self.client(FakeResponse())
        client.public_channels()
        receipts = client.receipts
        self.assertIsInstance(receipts, tuple)
        for receipt in receipts:
            self.assertEqual(set(receipt), {"operation", "method", "status", "bytes", "outcome", "elapsed_ms"})
            self.assertEqual(receipt["status"], 200)
            self.assertGreaterEqual(cast(int, receipt["elapsed_ms"]), 0)
            self.assertNotIn(ORIGIN, repr(receipt))
        receipts[0]["outcome"] = "tampered"
        self.assertEqual(client.receipts[0]["outcome"], "ok")
        with self.assertRaises(AttributeError):
            client.receipts = ()  # type: ignore[misc]

    def test_read_failure_closes_response_and_records_attempt(self) -> None:
        response = FakeResponse(fail_read=True)
        client, opener = self.client(response, request_budget=2)
        self.error("network_error", client.public_channels)
        self.assertTrue(response.closed)
        self.assertEqual(client.receipts[-1]["bytes"], 0)
        self.error("request_budget", client.public_channels)
        self.assertEqual(len(opener.calls), 2)

    def test_slow_chunks_hit_deadline_before_eof_without_parsing_prefix(self) -> None:
        clock = FakeClock()
        response = SlowResponse(b"0" + b" " * 100, clock)
        client, opener = self.client(response)
        with patch("channels_dvr.metadata_http.time.monotonic", side_effect=clock.monotonic), \
                patch("channels_dvr.metadata_http.json.loads", wraps=json.loads) as loads:
            error = self.error("response_timeout", client.public_channels)
        loads.assert_not_called()
        self.assertEqual(error.http_status, 200)
        self.assertEqual(response.position, 3)
        self.assertLess(response.position, len(response.body))
        self.assertTrue(response.closed)
        self.assertEqual(client.receipts[-1]["bytes"], 3)
        self.assertEqual(client.receipts[-1]["outcome"], "response_timeout")
        self.assertEqual(client.receipts[-1]["elapsed_ms"], 6000)
        self.assertEqual(len(opener.calls), 2)
        self.assertNotIn(ORIGIN, repr(error))

    def test_final_status_chunk_at_deadline_leaves_identity_unverified(self) -> None:
        for verified in (False, True):
            with self.subTest(verified=verified):
                clock = FakeClock()
                response = SlowResponse(status_response().body, clock, chunk_size=65536, delay=5)
                client, opener = self.client(response, verified=verified)
                with patch("channels_dvr.metadata_http.time.monotonic", side_effect=clock.monotonic):
                    self.error("response_timeout", client.verify_status)
                self.error("identity_not_verified", client.scheduled_jobs)
                self.assertEqual(response.position, len(response.body))
                self.assertTrue(response.closed)
                self.assertEqual(client.receipts[-1]["outcome"], "response_timeout")
                self.assertEqual(len(opener.calls), 2 if verified else 1)

    def test_eof_after_deadline_cannot_return_valid_json(self) -> None:
        clock = FakeClock()
        response = SlowResponse(b"0", clock)
        client, opener = self.client(response, timeout=3)
        with patch("channels_dvr.metadata_http.time.monotonic", side_effect=clock.monotonic):
            self.error("response_timeout", client.public_channels)
        self.assertEqual(len(response.read_sizes), 2)
        self.assertEqual(response.position, 1)
        self.assertTrue(response.closed)
        self.assertEqual(len(opener.calls), 2)

    def test_open_time_consumes_deadline_and_keeps_socket_timeout(self) -> None:
        clock = FakeClock()
        response = FakeResponse()
        client, opener = self.client(response, timeout=5)
        opener.on_open = lambda: clock.advance(5)
        with patch("channels_dvr.metadata_http.time.monotonic", side_effect=clock.monotonic):
            self.error("response_timeout", client.public_channels)
        self.assertEqual(response.read_sizes, [])
        self.assertTrue(response.closed)
        self.assertEqual(opener.calls[-1][1], 5.0)
        self.assertEqual(client.receipts[-1]["elapsed_ms"], 5000)
        self.assertEqual(len(opener.calls), 2)

    def test_read1_chunks_never_exceed_64k_and_complete_json_is_joined(self) -> None:
        text = "x" * 140000
        response = FakeResponse(json.dumps(text).encode("utf-8"))
        client, opener = self.client(response, max_bytes=len(response.body))
        self.assertEqual(client.public_channels(), text)
        self.assertEqual(response.read_sizes, [65536, 65536, 8931, 1])
        self.assertTrue(all(0 < amount <= 65536 for amount in response.read_sizes))
        self.assertEqual(client.receipts[-1]["bytes"], len(response.body))
        self.assertTrue(response.closed)
        self.assertEqual(len(opener.calls), 2)

    def test_multichunk_oversize_stops_at_cap_plus_one(self) -> None:
        response = FakeResponse(b" " * 200000)
        client, opener = self.client(response, max_bytes=65540)
        self.error("response_too_large", client.public_channels)
        self.assertEqual(response.read_sizes, [65536, 5])
        self.assertEqual(response.position, 65541)
        self.assertEqual(client.receipts[-1]["bytes"], 65541)
        self.assertTrue(response.closed)
        self.assertEqual(len(opener.calls), 2)

    def test_missing_read1_is_sanitized_without_fallback_read(self) -> None:
        response = FakeResponse()
        # An explicitly injected, trusted but incompatible transport.
        response.read1 = None  # type: ignore[assignment,method-assign]
        client, opener = self.client(response)
        self.error("network_error", client.public_channels)
        self.assertEqual(response.read_sizes, [])
        self.assertTrue(response.closed)
        self.assertEqual(client.receipts[-1]["outcome"], "network_error")
        self.assertEqual(len(opener.calls), 2)

    def test_duplicate_json_keys_including_nested_and_escaped_are_rejected(self) -> None:
        for body in (b'{"secret":1,"secret":2}', b'{"nested":{"x":0,"x":1}}',
                     b'[{"x":0,"\\u0078":1}]'):
            response = FakeResponse(body)
            client, opener = self.client(response)
            error = self.error("malformed_json", client.public_channels)
            self.assertNotIn("secret", repr(error))
            self.assertTrue(response.closed)
            self.assertEqual(len(opener.calls), 2)
        client, _ = self.client(FakeResponse(b'[{"x":0},{"x":1}]'))
        self.assertEqual(client.public_channels(), [{"x": 0}, {"x": 1}])

    def test_duplicate_status_version_never_verifies_identity(self) -> None:
        body = json.dumps(VERSION).encode("utf-8")
        response = FakeResponse(b'{"version":' + body + b',"version":' + body + b'}')
        client, opener = self.client(response, verified=False)
        self.error("malformed_json", client.verify_status)
        self.error("identity_not_verified", client.public_channels)
        self.assertTrue(response.closed)
        self.assertEqual(len(opener.calls), 1)

    def test_json_finishing_after_deadline_cannot_verify_identity(self) -> None:
        clock = FakeClock()
        response = status_response()
        client, opener = self.client(response, verified=False)

        def delayed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            clock.advance(5)
            return dict(pairs)

        with patch("channels_dvr.metadata_http.time.monotonic", side_effect=clock.monotonic), \
                patch("channels_dvr.metadata_http._unique_object", side_effect=delayed_object):
            self.error("response_timeout", client.verify_status)
        self.error("identity_not_verified", client.public_channels)
        self.assertTrue(response.closed)
        self.assertEqual(len(opener.calls), 1)

    def test_budget_256_bounds_receipts_and_rejects_257(self) -> None:
        client, opener = self.client(*(FakeResponse() for _ in range(255)), request_budget=256)
        for _ in range(255):
            client.public_channels()
        self.error("request_budget", client.public_channels)
        self.assertEqual(len(client.receipts), 256)
        self.assertEqual(len(opener.calls), 256)
        self.error("invalid_argument", lambda: self.client(verified=False, request_budget=257))

    def test_error_code_and_status_whitelist(self) -> None:
        error = MetadataError("https://hidden.example/secret", 999)
        self.assertEqual(error.code, "network_error")
        self.assertEqual(str(error), "network_error")
        self.assertIsNone(error.http_status)
        self.assertIsNone(MetadataError("http_error", True).http_status)


if __name__ == "__main__":
    unittest.main()
