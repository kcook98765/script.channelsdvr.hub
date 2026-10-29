"""Pure event-gated worker tests; all addresses and metadata are synthetic."""

from dataclasses import FrozenInstanceError, fields
from threading import Event, Thread, get_ident
import unittest
from unittest.mock import patch

from channels_dvr.live_loader import DevelopmentConfig, LoadResult, MetadataLoader
from channels_dvr.metadata_http import MetadataError
from channels_dvr.metadata_repository import MetadataClient
from channels_dvr.normalization import DataState

NOW = 1_800_000_000
CONFIG = DevelopmentConfig("http://metadata.invalid:8089")
OPERATIONS = ("status", "channels", "guide", "files", "jobs")
WAIT = 5


class FakeClient:
    namespace = "synthetic"

    def __init__(self, blocked: str | None = None) -> None:
        self.blocked = blocked
        self.release = Event()
        self.entered = {name: Event() for name in OPERATIONS}
        self.calls: list[tuple[str, int, int]] = []
        self.threads: list[int] = []
        self.failure: Exception | None = None
        self.fail_at = "status"

    @property
    def receipts(self) -> tuple[object, ...]:
        return tuple({"not_retained": "synthetic receipt"} for _ in self.calls)

    def _call(self, name: str, a: int = 0, b: int = 0) -> None:
        self.calls.append((name, a, b))
        self.threads.append(get_ident())
        self.entered[name].set()
        if name == self.blocked and not self.release.wait(WAIT):
            raise RuntimeError("test_gate_timeout")
        if name == self.fail_at and self.failure is not None:
            raise self.failure

    def verify_status(self) -> object:
        self._call("status")
        return {"identity_verified": True, "version": CONFIG.expected_version}

    def public_channels(self) -> object:
        self._call("channels")
        return [dict(id="one", source_id="synthetic", number="1", name="Synthetic",
                     station_id="station")]

    def guide_window(self, time_s: int, duration: int = 1800) -> object:
        self._call("guide", time_s, duration)
        return []

    def files_page(self, limit: int = 100, offset: int = 0) -> object:
        self._call("files", limit, offset)
        return {"items": [], "total": 0}

    def scheduled_jobs(self) -> object:
        self._call("jobs")
        return []


class LiveLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        for name in ("socket.socket", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(name, side_effect=AssertionError("No network"))
            guard.start()
            self.addCleanup(guard.stop)

    def loader(self, *clients: FakeClient) -> MetadataLoader:
        remaining = iter(clients)

        def factory(config: DevelopmentConfig) -> MetadataClient:
            self.assertIsNot(config, CONFIG)
            return next(remaining)

        loader = MetadataLoader(factory)

        def cleanup() -> None:
            loader.close()
            for client in clients:
                client.release.set()
            if loader._thread is not None:
                loader._thread.join(WAIT)
                self.assertFalse(loader._thread.is_alive())

        self.addCleanup(cleanup)
        return loader

    def idle(self, loader: MetadataLoader) -> None:
        with loader._condition:
            self.assertTrue(loader._condition.wait_for(
                lambda: not loader.busy and not loader.pending, timeout=WAIT))

    def result(self, loader: MetadataLoader) -> LoadResult:
        self.idle(loader)
        result = loader.poll()
        self.assertIsNotNone(result)
        assert result is not None
        return result

    def test_config_is_canonical_frozen_and_origin_not_in_repr(self) -> None:
        config = DevelopmentConfig("HTTP://METADATA.invalid:80/")
        self.assertEqual(config.origin, "http://metadata.invalid")
        self.assertEqual(config.namespace, "channels-dvr-development")
        self.assertEqual(config.expected_version, "2026.08.07.0346")
        self.assertNotIn("metadata.invalid", repr(config))
        with self.assertRaises(FrozenInstanceError):
            setattr(config, "origin", "other")

    def test_config_validation_is_safe_and_socket_free(self) -> None:
        invalid: tuple[tuple[object, object, object], ...] = (
            (None, "safe", "v"), ("http://metadata.invalid:57000", "safe", "v"),
            ("http://user:secret@metadata.invalid", "safe", "v"),
            ("http://metadata.invalid/path", "safe", "v"),
            ("http://metadata.invalid?secret", "safe", "v"),
            (CONFIG.origin, "bad namespace", "v"), (CONFIG.origin, "", "v"),
            (CONFIG.origin, "n" * 129, "v"), (CONFIG.origin, None, "v"),
            (CONFIG.origin, "safe", ""), (CONFIG.origin, "safe", "bad\nversion"),
            (CONFIG.origin, "safe", "https://secret/path"),
            (CONFIG.origin, "safe", "v" * 65), (CONFIG.origin, "safe", None))
        for origin, namespace, version in invalid:
            with self.subTest(case=len(str(namespace))), self.assertRaises(MetadataError) as caught:
                DevelopmentConfig(origin, namespace, version)  # type: ignore[arg-type]
            self.assertIn(str(caught.exception), ("invalid_origin", "invalid_argument"))

    def test_from_host_pins_literal_ipv4_and_ipv6(self) -> None:
        for host, origin in (("192.0.2.7", "http://192.0.2.7:8089"),
                             ("2001:0db8:0:0:0:0:0:7", "http://[2001:db8::7]:8089")):
            self.assertEqual(DevelopmentConfig.from_host(host).origin, origin)

    def test_from_host_rejects_everything_except_unscoped_literals(self) -> None:
        for host in ("", "metadata.invalid", "http://192.0.2.7", "192.0.2.7:8089",
                     "192.0.2.7/", "192.0.2.7?x", "user@192.0.2.7", "192.0.2.007",
                     " 192.0.2.7", "192.0.2.7\n", "[2001:db8::7]", "fe80::7%eth0",
                     "3221225991", "0xc0000207", "192.0.2", None, 123):
            with self.assertRaises(MetadataError) as caught:
                DevelopmentConfig.from_host(host)  # type: ignore[arg-type]
            self.assertEqual(str(caught.exception), "invalid_origin")

    def test_result_exact_fields_defaults_and_frozen(self) -> None:
        result = LoadResult(1, NOW, 4, None, "worker_error")
        self.assertEqual([f.name for f in fields(result)], ["generation", "requested_start",
                         "guide_windows", "snapshot", "error_code", "request_count"])
        self.assertEqual(result.request_count, 0)
        with self.assertRaises(FrozenInstanceError):
            setattr(result, "generation", 2)

    def test_from_host_rejects_non_targets_with_safe_errors(self) -> None:
        for host in ("0.0.0.0", "255.255.255.255", "224.0.0.0", "239.255.255.255",
                     "::", "0:0:0:0:0:0:0:0", "ff00::", "ff02::1", "ffff::1",
                     "::ffff:0.0.0.0", "::ffff:255.255.255.255", "::ffff:224.0.0.1"):
            with self.subTest(host=host), self.assertRaises(MetadataError) as caught:
                DevelopmentConfig.from_host(host)
            self.assertEqual(caught.exception.code, "invalid_origin")
            self.assertEqual(str(caught.exception), "invalid_origin")
            self.assertEqual(repr(caught.exception), "MetadataError('invalid_origin')")
            self.assertNotIn(host, repr(caught.exception))

    def test_from_host_retains_loopback_private_and_documentation_addresses(self) -> None:
        for host in ("127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1",
                     "192.0.2.7", "198.51.100.7", "203.0.113.7", "::1",
                     "fd00::7", "2001:db8::7", "::ffff:127.0.0.1", "::ffff:192.0.2.7"):
            with self.subTest(host=host):
                config = DevelopmentConfig.from_host(host)
                self.assertTrue(config.origin.startswith("http://"))
                self.assertTrue(config.origin.endswith(":8089"))
                self.assertNotIn(host, repr(config))
                self.assertNotIn(config.origin, repr(config))

    def test_generic_origin_constructor_keeps_existing_validation(self) -> None:
        for origin in ("http://0.0.0.0:8089", "http://255.255.255.255:8089",
                       "http://224.0.0.1:8089", "http://[::]:8089", "http://[ff00::]:8089"):
            with self.subTest(origin=origin):
                config = DevelopmentConfig(origin)
                self.assertEqual(config.origin, origin)
                self.assertNotIn(origin, repr(config))

    def test_lazy_single_daemon_and_factory_on_worker(self) -> None:
        client = FakeClient("status")
        loader = self.loader(client)
        self.assertIsNone(loader._thread)
        self.assertEqual((loader.busy, loader.pending, loader.stale_results), (False, False, 0))
        self.assertIsNone(loader.poll())
        loader.request(CONFIG, NOW)
        self.assertTrue(client.entered["status"].wait(WAIT))
        assert loader._thread is not None
        self.assertTrue(loader._thread.daemon)
        self.assertNotEqual(client.threads[0], get_ident())
        self.assertTrue(loader.busy)
        self.assertFalse(loader.pending)
        client.release.set()
        self.result(loader)

    def test_default_factory_runs_on_worker_with_explicit_finite_bounds(self) -> None:
        client = FakeClient()
        caller = get_ident()
        threads: list[int] = []

        def construct(origin: str, **kwargs: object) -> MetadataClient:
            threads.append(get_ident())
            self.assertEqual(origin, CONFIG.origin)
            self.assertEqual(kwargs, dict(namespace=CONFIG.namespace,
                             expected_version=CONFIG.expected_version,
                             timeout=5, max_bytes=4194304, request_budget=24))
            return client

        loader = MetadataLoader()
        self.addCleanup(loader.close)
        with patch("channels_dvr.live_loader.ChannelsMetadataClient", side_effect=construct):
            self.assertEqual(threads, [])
            loader.request(CONFIG, NOW)
            self.result(loader)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], caller)
        loader.close()
        assert loader._thread is not None
        loader._thread.join(WAIT)
        self.assertFalse(loader._thread.is_alive())

    def test_four_windows_are_sequential_and_result_is_single_consume(self) -> None:
        client = FakeClient()
        loader = self.loader(client)
        generation = loader.request(CONFIG, NOW)
        result = self.result(loader)
        self.assertEqual((result.generation, result.requested_start, result.guide_windows,
                          result.error_code, result.request_count), (generation, NOW, 4, None, 8))
        self.assertEqual(client.calls, [("status", 0, 0), ("channels", 0, 0)]
                         + [("guide", NOW + i * 1800, 1800) for i in range(4)]
                         + [("files", 100, 0), ("jobs", 0, 0)])
        self.assertEqual(len(set(client.threads)), 1)
        assert result.snapshot is not None
        self.assertEqual(result.snapshot.horizon, (NOW, NOW + 7200))
        self.assertIsNone(loader.poll())
        self.assertEqual((loader.busy, loader.pending), (False, False))

    def test_latest_pending_coalesces_without_extra_factories_or_threads(self) -> None:
        initial, latest = FakeClient("status"), FakeClient()
        loader = self.loader(initial, latest)
        first = loader.request(CONFIG, NOW)
        self.assertTrue(initial.entered["status"].wait(WAIT))
        thread = loader._thread
        for i in range(1, 101):
            generation = loader.request(CONFIG, NOW + i, 1)
            self.assertEqual((loader.busy, loader.pending), (True, True))
            self.assertIs(loader._thread, thread)
        self.assertEqual(generation, first + 100)
        initial.release.set()
        result = self.result(loader)
        self.assertEqual((result.generation, result.requested_start), (generation, NOW + 100))
        self.assertEqual(initial.calls, [("status", 0, 0)])
        self.assertEqual(loader.stale_results, 1)
        self.assertEqual(set(initial.threads + latest.threads), {thread.ident})  # type: ignore[union-attr]

    def test_completed_old_mailbox_is_cleared_by_new_request(self) -> None:
        old, new = FakeClient(), FakeClient("status")
        loader = self.loader(old, new)
        loader.request(CONFIG, NOW)
        self.idle(loader)
        generation = loader.request(CONFIG, NOW + 1)
        self.assertTrue(new.entered["status"].wait(WAIT))
        self.assertIsNone(loader.poll())
        new.release.set()
        self.assertEqual(self.result(loader).generation, generation)

    def test_old_final_response_is_discarded_even_without_another_operation(self) -> None:
        old, new = FakeClient("jobs"), FakeClient()
        loader = self.loader(old, new)
        loader.request(CONFIG, NOW)
        self.assertTrue(old.entered["jobs"].wait(WAIT))
        generation = loader.request(CONFIG, NOW + 2)
        old.release.set()
        self.assertEqual(self.result(loader).generation, generation)
        self.assertEqual(loader.stale_results, 1)

    def test_cancel_between_status_and_channels_stops_all_other_reads(self) -> None:
        client = FakeClient("status")
        loader = self.loader(client)
        loader.request(CONFIG, NOW)
        self.assertTrue(client.entered["status"].wait(WAIT))
        loader.cancel()
        self.assertEqual((loader.busy, loader.pending), (True, False))
        client.release.set()
        self.idle(loader)
        self.assertEqual(client.calls, [("status", 0, 0)])
        self.assertIsNone(loader.poll())
        self.assertEqual(loader.stale_results, 1)

    def test_each_named_operation_is_guarded_and_cancel_escapes_metadata_errors(self) -> None:
        for operation in OPERATIONS:
            with self.subTest(operation=operation):
                client = FakeClient(operation)
                client.fail_at = operation
                client.failure = MetadataError("network_error")
                loader = self.loader(client)
                loader.request(CONFIG, NOW)
                self.assertTrue(client.entered[operation].wait(WAIT))
                before = list(client.calls)
                loader.cancel()
                client.release.set()
                self.idle(loader)
                self.assertEqual(client.calls, before)
                self.assertIsNone(loader.poll())
                self.assertEqual(loader.stale_results, 1)

    def test_cancel_drops_pending_and_completed_results_but_worker_is_reusable(self) -> None:
        old, new = FakeClient("status"), FakeClient()
        loader = self.loader(old, new)
        first = loader.request(CONFIG, NOW)
        self.assertTrue(old.entered["status"].wait(WAIT))
        loader.request(CONFIG, NOW + 1)
        thread = loader._thread
        loader.cancel()
        self.assertFalse(loader.pending)
        old.release.set()
        self.idle(loader)
        self.assertIsNone(loader.poll())
        generation = loader.request(CONFIG, NOW + 2)
        self.assertGreater(generation, first + 1)
        self.idle(loader)
        self.assertIs(loader._thread, thread)
        loader.cancel()
        self.assertIsNone(loader.poll())
        self.assertEqual(len(new.calls), 8)

    def test_close_returns_during_blocked_io_and_never_starts_pending(self) -> None:
        client, pending = FakeClient("status"), FakeClient()
        loader = self.loader(client, pending)
        loader.request(CONFIG, NOW)
        self.assertTrue(client.entered["status"].wait(WAIT))
        loader.request(CONFIG, NOW + 1)
        returned = Event()

        def close() -> None:
            loader.close()
            returned.set()

        closer = Thread(target=close, daemon=True)
        closer.start()
        try:
            self.assertTrue(returned.wait(WAIT), "close blocked on in-flight I/O")
            self.assertFalse(client.release.is_set())
            self.assertEqual((loader.busy, loader.pending), (True, False))
            self.assertIsNone(loader.poll())
        finally:
            client.release.set()
            closer.join(WAIT)
        self.idle(loader)
        assert loader._thread is not None
        loader._thread.join(WAIT)
        self.assertFalse(loader._thread.is_alive())
        self.assertEqual(pending.calls, [])
        self.assertEqual(client.calls, [("status", 0, 0)])
        self.assertEqual(loader.stale_results, 1)

    def test_close_is_terminal_idempotent_and_clears_completed_result(self) -> None:
        client = FakeClient()
        loader = self.loader(client)
        loader.request(CONFIG, NOW)
        self.idle(loader)
        loader.close()
        loader.close()
        loader.cancel()
        self.assertIsNone(loader.poll())
        with self.assertRaisesRegex(ValueError, "^loader_closed$"):
            loader.request(CONFIG, NOW)
        self.assertEqual((loader.busy, loader.pending), (False, False))
        unused = self.loader()
        unused.close()
        self.assertIsNone(unused._thread)

    def test_errors_are_closed_codes_and_never_retain_raw_exception_text(self) -> None:
        errors: list[Exception] = [MetadataError("identity_mismatch"),
                                   RuntimeError("synthetic-secret-url/body")]
        poisoned = MetadataError("network_error")
        poisoned.code = "synthetic-secret-url/body"
        errors.append(poisoned)
        for error, code in zip(errors, ("identity_mismatch", "worker_error", "worker_error")):
            error.args = ("synthetic-secret-url/body",)
            client = FakeClient()
            client.failure = error
            loader = self.loader(client)
            loader.request(CONFIG, NOW)
            result = self.result(loader)
            self.assertEqual((result.snapshot, result.error_code, result.request_count),
                             (None, code, 1))
            self.assertNotIn("synthetic-secret", repr(result))

    def test_factory_failure_is_redacted_and_next_request_recovers(self) -> None:
        client = FakeClient()
        calls: list[int] = []

        def factory(config: DevelopmentConfig) -> MetadataClient:
            calls.append(get_ident())
            if len(calls) == 1:
                raise RuntimeError("synthetic-secret-url/body")
            return client

        loader = self.loader(client)
        loader._client_factory = factory
        loader.request(CONFIG, NOW)
        result = self.result(loader)
        self.assertEqual((result.error_code, result.request_count), ("worker_error", 0))
        self.assertNotIn("synthetic-secret", repr(result))
        loader.request(CONFIG, NOW)
        self.assertIsNone(self.result(loader).error_code)
        self.assertEqual(len(set(calls)), 1)
        self.assertNotEqual(calls[0], get_ident())

    def test_receipt_property_absent_or_raising_safely_counts_zero(self) -> None:
        class NoReceipts(FakeClient):
            @property
            def receipts(self) -> tuple[object, ...]:
                raise AttributeError("synthetic-secret")

        class BrokenReceipts(FakeClient):
            @property
            def receipts(self) -> tuple[object, ...]:
                raise RuntimeError("synthetic-secret")

        for client in (NoReceipts(), BrokenReceipts()):
            loader = self.loader(client)
            loader.request(CONFIG, NOW)
            result = self.result(loader)
            self.assertEqual((result.error_code, result.request_count), (None, 0))

    def test_invalid_requests_do_not_mutate_pending_generation_or_mailbox(self) -> None:
        client, next_client = FakeClient("status"), FakeClient()
        loader = self.loader(client, next_client)
        first = loader.request(CONFIG, NOW)
        self.assertTrue(client.entered["status"].wait(WAIT))
        pending = loader.request(CONFIG, NOW + 1)
        invalid: tuple[tuple[object, object, object], ...] = (
            (None, NOW, 1), (CONFIG, True, 1), (CONFIG, -1, 1), (CONFIG, 1.0, 1),
            (CONFIG, "1", 1), (CONFIG, NOW, True), (CONFIG, NOW, 0),
            (CONFIG, NOW, 5), (CONFIG, NOW, 1.0))
        for config, start, windows in invalid:
            with self.assertRaises(MetadataError):
                loader.request(config, start, windows)  # type: ignore[arg-type]
            self.assertEqual(loader._generation, pending)
            self.assertEqual((loader.busy, loader.pending), (True, True))
        self.assertEqual(pending, first + 1)
        client.release.set()
        self.idle(loader)
        with self.assertRaises(MetadataError):
            loader.request(CONFIG, -1)
        self.assertEqual(loader.poll().generation, pending)  # type: ignore[union-attr]
        self.assertEqual(loader.stale_results, 1)

    def test_idle_transition_accepts_new_intents_without_lost_wakeups(self) -> None:
        clients = [FakeClient() for _ in range(25)]
        loader = self.loader(*clients)
        thread: Thread | None = None
        for i in range(len(clients)):
            generation = loader.request(CONFIG, NOW + i, 1)
            result = self.result(loader)
            self.assertEqual(result.generation, generation)
            self.assertEqual((loader.busy, loader.pending), (False, False))
            if thread is None:
                thread = loader._thread
            self.assertIs(loader._thread, thread)
        self.assertEqual(loader.stale_results, 0)

    def test_partial_resource_errors_are_snapshot_states_without_retries(self) -> None:
        client = FakeClient()
        client.fail_at = "channels"
        client.failure = MetadataError("unsupported")
        loader = self.loader(client)
        loader.request(CONFIG, 0, 1)
        result = self.result(loader)
        self.assertIsNone(result.error_code)
        assert result.snapshot is not None
        self.assertEqual(result.snapshot.channels.state, DataState.UNSUPPORTED)
        self.assertEqual([call[0] for call in client.calls], ["status", "channels", "files", "jobs"])
        self.assertEqual(result.request_count, 4)


if __name__ == "__main__":
    unittest.main()
