"""Address admission and actual adapter methods with explicit, socket-free fakes.

Extract only the owned settings methods from the adapter AST, so no Kodi SDK or
stub is imported or executed outside Kodi. Connection dispatch is used directly.
"""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from channels_dvr.live_loader import DevelopmentConfig
from channels_dvr.metadata_http import MetadataError
from channels_dvr.newbuild_compatibility import BASE_MINIMUM_DVR_BUILD
from channels_dvr.ui_navigation import execute_connection, settings


ORIGIN = "http://192.0.2.10:8089"
VARIANTS = (
    "192.0.2.10", "192.0.2.10:8089", "http://192.0.2.10",
    ORIGIN, ORIGIN + "/", " HTTP://192.0.2.10/ ",
)
INVALID = (
    "", "dvr.example.test", "http://dvr.example.test:8089",
    "https://192.0.2.10:8089", "http://192.0.2.10:80",
    "192.0.2.10:8088", "http://192.0.2.10:57000",
    "http://user:synthetic-secret@192.0.2.10:8089",
    ORIGIN + "/status", ORIGIN + "?key=synthetic-secret", ORIGIN + "#",
    ORIGIN + "\\", ORIGIN + "\n", "0.0.0.0", "224.0.0.1",
    "255.255.255.255", "::", "ff02::1", "fe80::1%eth0",
    "http://2001:db8::1", "http://[2001:db8::1]:80",
    "http://[192.0.2.10]", "[192.0.2.10]", None, True, 123,
)


class FakeAddon:
    def __init__(self, address, enabled=False):
        self.values = {"server_origin": address,
                       "development_metadata_enabled": enabled,
                       "server_expected_version": "2026.08.08.0000"}
        self.writes = []
        self.fail_key = None

    def getSettingString(self, key):
        return self.values.get(key, "")

    def getSettingBool(self, key):
        return self.values.get(key, False)

    def _save(self, key, value):
        self.writes.append((key, value))
        if key == self.fail_key:
            return False
        self.values[key] = value
        return True

    setSettingString = _save
    setSettingBool = _save

    def openSettings(self):
        pass


def adapter(dialog):
    source = (Path(__file__).resolve().parents[1] / "script.channelsdvr.hub"
              / "resources/lib/channels_dvr/kodi_ui.py")
    tree = ast.parse(source.read_text())
    window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                  and n.name == "ChannelsWindow")
    names = {"get_setting", "_saved_config", "_config_host",
             "_configure_development", "_save_development"}
    methods = [n for n in window.body if isinstance(n, ast.FunctionDef)
               and n.name in names]
    assert {n.name for n in methods} == names
    body = ast.ClassDef(name="Adapter", bases=[], keywords=[], body=methods,
                        decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[body], type_ignores=[]))
    namespace = {"DevelopmentConfig": DevelopmentConfig,
                 "MetadataError": MetadataError,
                 "BASE_MINIMUM_DVR_BUILD": BASE_MINIMUM_DVR_BUILD,
                 "xbmcgui": SimpleNamespace(Dialog=lambda: dialog)}
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["Adapter"]()


class ConnectionConfigTests(unittest.TestCase):
    def setUp(self):
        for name in ("socket.socket", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(name, side_effect=AssertionError("No network"))
            guard.start()
            self.addCleanup(guard.stop)
        self.messages = []
        self.inputs = []
        self.answer = "192.0.2.10"
        self.dialog = SimpleNamespace(ok=lambda *args: self.messages.append(args),
                                      input=self.input)
        self.gui = SimpleNamespace(Dialog=lambda: self.dialog)
        self.window = adapter(self.dialog)
        w = self.window
        w._config_save_failed = False
        w._live_session = None
        w._development_blocked = None
        w._addon = FakeAddon("")
        w.strings = {}
        w.t = lambda key: key
        w.activated = []
        w.requested = []
        w.suspended = []
        w._activate = w.activated.append
        w._request_live = w.requested.append
        w._suspend_development = lambda **kwargs: w.suspended.append(kwargs)
        w.utc_now = lambda: 123

    def input(self, *args, **kwargs):
        self.inputs.append((args, kwargs))
        return self.answer

    def test_address_variants_normalize_without_network(self):
        for address in VARIANTS:
            with self.subTest(address=address):
                self.assertEqual(DevelopmentConfig.from_address(address).origin, ORIGIN)

    def test_ipv6_literals_and_bracketed_http_addresses(self):
        for address in ("2001:db8::1", "[2001:db8::1]",
                        "[2001:db8::1]:8089", "http://[2001:db8::1]",
                        "http://[2001:db8::1]:8089/"):
            with self.subTest(address=address):
                self.assertEqual(DevelopmentConfig.from_address(address).origin,
                                 "http://[2001:db8::1]:8089")

    def test_invalid_addresses_return_only_closed_errors(self):
        for address in INVALID:
            with self.subTest(address=address), self.assertRaises(MetadataError) as caught:
                DevelopmentConfig.from_address(address)
            self.assertEqual(str(caught.exception), "invalid_origin")

    def test_saved_settings_variants_connect_and_preserve_saved_address(self):
        w = self.window
        for address in VARIANTS:
            with self.subTest(address=address):
                w.activated.clear()
                w.requested.clear()
                w._addon = FakeAddon(address)
                execute_connection(w, self.gui, "connection.connect")
                self.assertEqual(w.activated, [DevelopmentConfig(ORIGIN)])
                self.assertEqual(w.requested, [123])
                self.assertTrue(w._addon.values["development_metadata_enabled"])
                self.assertEqual(w._addon.values["server_origin"], address)
                self.assertEqual(w._addon.writes, [("development_metadata_enabled", True)])
                self.assertEqual(w._saved_config().expected_version, BASE_MINIMUM_DVR_BUILD)
        self.assertEqual(self.messages, [])

    def test_configure_uses_the_same_parser_as_settings_without_enabling(self):
        w = self.window
        for address in VARIANTS:
            with self.subTest(address=address):
                w._addon = FakeAddon("")
                self.answer = address
                w._configure_development()
                self.assertEqual(w._addon.values["server_origin"], ORIGIN)
                self.assertFalse(w._addon.values["development_metadata_enabled"])
                self.assertEqual(w._saved_config(), DevelopmentConfig.from_address(address))
        self.assertEqual(w.activated, [])
        self.assertEqual(w.requested, [])

    def test_configure_prefills_the_saved_host_and_cancel_preserves_everything(self):
        w = self.window
        w._addon = FakeAddon("http://192.0.2.10", True)
        self.answer = ""
        w._configure_development()
        self.assertEqual(self.inputs[-1][1]["defaultt"], "192.0.2.10")
        self.assertEqual(w._addon.writes, [])

    def test_invalid_saved_settings_do_not_enable_or_request(self):
        w = self.window
        for address in INVALID:
            with self.subTest(address=address):
                w._addon = FakeAddon(address)
                execute_connection(w, self.gui, "connection.connect")
                self.assertEqual(w._addon.writes, [])
                self.assertEqual(self.messages[-1], ("connection", "configure_first"))
        self.assertEqual(w.activated, [])
        self.assertEqual(w.requested, [])

    def test_invalid_configure_preserves_existing_settings(self):
        w = self.window
        w._addon = FakeAddon(ORIGIN, True)
        self.answer = "http://user:synthetic-secret@192.0.2.10:8089"
        w._configure_development()
        self.assertEqual(w._addon.writes, [])
        self.assertEqual(self.messages, [("connection", "address_invalid")])

    def test_enable_save_failure_blocks_connection(self):
        w = self.window
        w._addon = FakeAddon(ORIGIN)
        w._addon.fail_key = "development_metadata_enabled"
        execute_connection(w, self.gui, "connection.connect")
        self.assertFalse(w._addon.values["development_metadata_enabled"])
        self.assertEqual(w.activated, [])
        self.assertEqual(w.requested, [])
        self.assertEqual(self.messages, [("connection", "connection_save_failed")])

    def test_address_save_failure_keeps_the_existing_failure_fence(self):
        w = self.window
        w._addon = FakeAddon(ORIGIN)
        w._addon.fail_key = "server_origin"
        w._configure_development()
        self.assertTrue(w._config_save_failed)
        self.assertIsNone(w._saved_config())
        execute_connection(w, self.gui, "connection.connect")
        self.assertEqual(w.activated, [])
        self.assertEqual(w.requested, [])

    def test_settings_reactivation_uses_normalized_address(self):
        w = self.window
        w._addon = FakeAddon("192.0.2.10", True)
        w._live_session = object()
        w._guide_runtime = SimpleNamespace(latest_status=object())
        w._revoke_demo = lambda: None
        settings(w)
        self.assertEqual(w.activated, [DevelopmentConfig(ORIGIN)])
        self.assertEqual(w._addon.writes, [])

    def test_disabling_in_settings_still_suspends_connection(self):
        w = self.window
        w._addon = FakeAddon(ORIGIN, False)
        w._live_session = object()
        w._guide_runtime = SimpleNamespace(latest_status=object())
        w._revoke_demo = lambda: None
        settings(w)
        self.assertEqual(w.suspended, [{"disabled": True}])
        self.assertEqual(w.activated, [])
