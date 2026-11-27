"""Static package/offline boundary checks. Never import an entry point or Kodi."""

import ast
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


ADDON = Path(__file__).resolve().parents[1] / "script.channelsdvr.hub"
LIB = ADDON / "resources/lib/channels_dvr"
SKIN = ADDON / "resources/skins/Default"
BOOKMARK_STORAGE = LIB / "bookmark_storage.py"
BOOKMARK_WRITES = {
    "os.mkdir": "os.mkdir(self._directory, mode=448)",  # 0o700
    "tempfile.NamedTemporaryFile": ("tempfile.NamedTemporaryFile(mode='wb', dir=self._directory, "
                                    "prefix='.bookmark-', suffix='.tmp', delete=False)"),
    "stream.write": "stream.write(payload)",
    "stream.flush": "stream.flush()",
    "os.fsync": "os.fsync(stream.fileno())",
    "os.replace": "os.replace(temp_path, self._path)",
    "os.unlink": "os.unlink(temp_path)",
}


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return ""


class PackageTests(unittest.TestCase):
    def assert_exact_file(self, relative):
        """Check each path component's spelling even on case-insensitive mounts."""
        current = ADDON
        for component in Path(relative).parts:
            self.assertNotIn(component, ("..", "."), relative)
            self.assertIn(component, [p.name for p in current.iterdir()],
                          f"Missing or incorrectly cased package path: {relative}")
            current = current / component
        self.assertTrue(current.is_file(), f"Not a package file: {relative}")

    def python_sources(self):
        for path in sorted(ADDON.rglob("*.py")):
            yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_manifest_identity_python_dependency_and_entry_paths(self):
        root = ET.parse(ADDON / "addon.xml").getroot()
        self.assertEqual(root.tag, "addon")
        self.assertEqual(root.get("id"), "script.channelsdvr.hub")
        self.assertEqual(root.get("version"), "0.1.4")
        self.assertEqual([(n.get("addon"), n.get("version")) for n in root.findall("requires/import")],
                         [("xbmc.python", "3.0.1")])
        entries = {n.get("point"): n for n in root.findall("extension")}
        self.assertEqual(set(entries), {"xbmc.python.script", "xbmc.addon.metadata"})
        for point, filename in (("xbmc.python.script", "default.py"),):
            self.assertEqual(entries[point].get("library"), filename)
            self.assert_exact_file(filename)
        self.assertIsNone(entries['xbmc.python.script'].find('provides'))

    def test_manifest_asset_paths_exist_with_exact_case(self):
        root = ET.parse(ADDON / "addon.xml").getroot()
        assets = root.findall("extension/assets/*")
        self.assertTrue(assets)
        for node in assets:
            with self.subTest(asset=node.text):
                self.assert_exact_file(node.text)

    def test_window_xml_path_and_texture_references_exist_with_exact_case(self):
        self.assert_exact_file("resources/skins/Default/1080i/ChannelsDvr.xml")
        root = ET.parse(SKIN / "1080i/ChannelsDvr.xml").getroot()
        textures = {n.text for tag in ("texture", "texturefocus", "texturenofocus")
                    for n in root.iter(tag) if n.text}
        # Dynamic texture names are read from the adapter's actual AST too.
        ui = ast.parse((LIB / "kodi_ui.py").read_text(encoding="utf-8"))
        for node in ast.walk(ui):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.endswith(".png"):
                    textures.add(node.value)
        self.assertTrue(textures)
        for texture in sorted(textures):
            with self.subTest(texture=texture):
                self.assert_exact_file(f"resources/skins/Default/media/{texture}")

    def test_xml_control_ids_unique_and_adapter_labels_exist(self):
        root = ET.parse(SKIN / "1080i/ChannelsDvr.xml").getroot()
        controls = root.findall(".//control[@id]")
        ids = [int(n.get("id")) for n in controls]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {100, 110, 112, 113, 114, 115, 116, 117, *range(120, 132), *range(140, 144),
                                    *range(150, 156), *range(200, 206), *range(220, 225), 230, 231,
                                    *range(300, 311), *range(320, 323), *range(400, 406)})
        self.assertTrue(all(n.get('type') == 'button' for n in controls if 200 <= int(n.get('id')) < 400))
        self.assertTrue(all(n.get('type') == 'textbox' for n in controls if int(n.get('id')) >= 400))

    def test_dvr_tab_visibility_is_owner_managed_and_geometry_scales(self):
        root = ET.parse(SKIN / '1080i/ChannelsDvr.xml').getroot()
        def bounds(ident):
            node = root.find(f'.//control[@id="{ident}"]')
            return node, tuple(int(node.findtext(key)) / 3 for key in ('left', 'top', 'width', 'height'))

        nav, (_, nav_y, _, nav_h) = bounds(203)
        previous_right = 0
        for ident in (230, 231):
            node, (x, y, width, height) = bounds(ident)
            self.assertIsNone(node.find('visible'))
            self.assertIsNone(nav.find('visible'))
            self.assertTrue(all(node.findtext(key) == str(ident) for key in ('onleft', 'onright', 'onup', 'ondown')))
            self.assertLess(previous_right, x)
            self.assertLessEqual(nav_y + nav_h, y)
            self.assertLess(y + height, 250/3)  # row hits start at y=250 in the source grid
            self.assertLess(x + width, 640)
            previous_right = x + width

    def test_guide_skin_positions_share_the_presenter_coordinates(self):
        from channels_dvr.guide_render import GRID_LEFT, GRID_WIDTH, ROW_PITCH, ROW_TOP
        root = ET.parse(SKIN / '1080i/ChannelsDvr.xml').getroot()
        controls = {int(n.get('id')): n for n in root.findall('.//control[@id]')}
        self.assertEqual(controls[152].get('type'), 'textbox')
        self.assertEqual(controls[154].get('type'), 'image')
        self.assertEqual(controls[155].get('type'), 'image')
        # Static cap ends exactly where the foreground owner-drawn line begins.
        self.assertEqual(int(controls[154].findtext('top')) + int(controls[154].findtext('height')), ROW_TOP)
        for tick in range(4):
            self.assertEqual(int(controls[140 + tick].findtext('left')), GRID_LEFT + tick * GRID_WIDTH // 4)
        for row in range(6):
            self.assertEqual(int(controls[120 + row].findtext('top')), ROW_TOP + row * ROW_PITCH + 9)
            self.assertEqual(int(controls[126 + row].findtext('top')), ROW_TOP + row * ROW_PITCH + 43)

    def test_server_setting_defaults_blank(self):
        root = ET.parse(ADDON / "resources/settings.xml").getroot()
        settings = root.findall(".//setting[@id='server_origin']")
        self.assertEqual(len(settings), 1)
        self.assertEqual(settings[0].get("type"), "string")
        default = settings[0].find("default")
        self.assertIsNotNone(default)
        self.assertEqual((default.text or "").strip(), "")
        # Kodi21 CSettingString rejects an empty default unless explicitly
        # permitted; an unregistered setting then raises Invalid setting type.
        self.assertEqual(settings[0].findtext("constraints/allowempty"), "true")

    def test_hub_choices_are_closed_and_safe_by_default(self):
        from channels_dvr.ui_text import IDS
        root = ET.parse(ADDON / 'resources/settings.xml').getroot()
        for key, default, values in (
            ('hub_startup_view', 'Dashboard', (('dashboard', 'Dashboard'), ('guide', 'Guide'),
                ('recordings', 'Recordings'), ('dvr_view', 'DVR'), ('startup_last', 'Last'))),
            ('hub_guide_ok', 'details', (('details', 'details'), ('watch_short', 'live'))),
        ):
            with self.subTest(setting=key):
                node = root.find(f".//category[@id='hub']//setting[@id='{key}']")
                self.assertIsNotNone(node)
                self.assertEqual(node.get('type'), 'string')
                self.assertEqual(node.findtext('default'), default)
                self.assertEqual([(n.get('label'), n.text) for n in node.findall('constraints/options/option')],
                                 [(str(IDS[label]), value) for label, value in values])

    def test_development_metadata_optin_is_false_and_build_pin_is_explicit(self):
        root = ET.parse(ADDON / "resources/settings.xml").getroot()
        enabled = root.find(".//setting[@id='development_metadata_enabled']")
        self.assertIsNotNone(enabled)
        self.assertEqual(enabled.get("type"), "boolean")
        self.assertEqual(enabled.findtext("default"), "false")
        version = root.find(".//setting[@id='server_expected_version']")
        self.assertEqual(version.findtext("default"), "2026.08.07.0346")
        self.assertEqual(root.findtext(".//setting[@id='development_metadata_enabled']/default"), "false")

    def test_combined_english_catalog_matches_all_active_localization_ids(self):
        from channels_dvr.ui_text import FALLBACK, IDS
        entries = {}
        context, message, field = '', '', None
        path = ADDON / 'resources/language/resource.language.en_gb/strings.po'
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.startswith('msgctxt '):
                context = ast.literal_eval(line.split(' ', 1)[1])
                message, field = '', 'context'
            elif line.startswith('msgid '):
                message = ast.literal_eval(line.split(' ', 1)[1])
                field = 'message'
            elif line.startswith('msgstr '):
                if context:
                    self.assertNotIn(context, entries, 'Duplicate localization ID')
                    entries[context] = message
                context, field = '', None
            elif line.startswith('"'):
                if field == 'context':
                    context += ast.literal_eval(line)
                elif field == 'message':
                    message += ast.literal_eval(line)
        self.assertEqual(len(IDS), len(set(IDS.values())), 'Duplicate active catalog ID')
        for key, ident in IDS.items():
            with self.subTest(key=key, ident=ident):
                self.assertEqual(entries.get('#' + str(ident)), FALLBACK[key])

    def test_ast_network_imports_confined_to_named_metadata_transport(self):
        forbidden = {"requests", "http", "httpx", "aiohttp", "urllib", "urllib3",
                     "socket", "ssl", "ftplib", "websocket", "websockets"}
        for path, tree in self.python_sources():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    imports = [node.module or ""]
                else:
                    continue
                for module in imports:
                    with self.subTest(file=str(path.relative_to(ADDON)), line=node.lineno, module=module):
                        if module.split(".")[0] in forbidden:
                            if path == LIB / 'artwork_refs.py':
                                # Parsing only; no artwork network transport remains.
                                self.assertIsInstance(node, ast.ImportFrom)
                                self.assertEqual(module, 'urllib.parse')
                                self.assertEqual({a.name for a in node.names}, {'unquote', 'urljoin', 'urlsplit'})
                                continue
                            if path == LIB / 'playback_descriptors.py':
                                self.assertIsInstance(node, ast.ImportFrom)
                                self.assertEqual(module, 'urllib.parse')
                                self.assertEqual({a.name for a in node.names}, {'quote','unquote','urljoin','urlsplit'})
                                continue
                            self.assertIsInstance(node, ast.ImportFrom)
                            if path == LIB / "metadata_http.py":
                                permitted = {
                                    "urllib.error": {"HTTPError"},
                                    "urllib.parse": {"urlencode", "urlsplit"},
                                    "urllib.request": {"HTTPRedirectHandler", "ProxyHandler", "Request", "build_opener"},
                                }
                                self.assertIn(module, permitted)
                                self.assertLessEqual({a.name for a in node.names}, permitted[module])
                            elif path == LIB / 'playback_http.py':
                                permitted = {'urllib.error': {'HTTPError'},
                                             'urllib.request': {'ProxyHandler','Request','build_opener'}}
                                self.assertIn(module, permitted)
                                self.assertEqual({a.name for a in node.names}, permitted[module])
                            elif path == LIB / "programme_pass_http.py":
                                permitted = {
                                    "urllib.error": {"HTTPError"},
                                    "urllib.parse": {"quote", "urlencode"},
                                    "urllib.request": {"ProxyHandler", "Request", "build_opener"},
                                }
                                self.assertIn(module, permitted)
                                self.assertLessEqual({a.name for a in node.names}, permitted[module])
                            elif path in {LIB / "recording_http.py", LIB / "pass_http.py", LIB / "pass_mutation_http.py", LIB / "series_http.py", LIB / "dashboard_http.py"}:
                                permitted = {
                                    "urllib.error": {"HTTPError"},
                                    "urllib.parse": {"urlencode"} if path.name in {"pass_http.py", "series_http.py"} else {"quote"},
                                    "urllib.request": {"ProxyHandler", "Request", "build_opener"},
                                }
                                self.assertIn(module, permitted)
                                self.assertLessEqual({a.name for a in node.names}, permitted[module])
                            else:
                                self.assertEqual(path, ADDON / "plugin.py")
                                self.assertEqual(module, "urllib.parse")
                                self.assertLessEqual({a.name for a in node.names}, {"parse_qs", "urlencode"})

    def test_ast_kodi_imports_confined_to_adapter_and_entry_points(self):
        allowed = {ADDON / "default.py", ADDON / "plugin.py", LIB / "kodi_ui.py"}
        for path, tree in self.python_sources():
            for node in ast.walk(tree):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                for name in names:
                    if name.split(".")[0] in {"xbmc", "xbmcgui", "xbmcplugin", "xbmcaddon", "xbmcvfs"}:
                        with self.subTest(file=str(path.relative_to(ADDON)), line=node.lineno):
                            if path == LIB / 'playback_native.py':
                                self.assertIsInstance(node, ast.Import)
                                self.assertIn(name, {'xbmc','xbmcgui'})
                                continue
                            if path in {LIB / "recording_ui.py", LIB / "storage_ui.py", LIB / "pass_ui.py", LIB / "programme_scope_ui.py"}:
                                self.assertIsInstance(node, ast.Import)
                                self.assertIn(name, {"xbmcgui", "xbmcvfs"} if path.name in {"recording_ui.py","storage_ui.py"} else {"xbmcgui"})
                                continue
                            self.assertIn(path, allowed)
                            self.assertNotEqual(name, "xbmcvfs")

    def test_pass_transport_routes_are_closed_and_preview_helpers_cannot_import_recording_writes(self):
        tree = ast.parse((LIB / "pass_http.py").read_text())
        request = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_request")
        routes = next(n.value for n in request.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "routes" for t in n.targets))
        self.assertEqual(ast.literal_eval(routes), {
            "verify_status": "/status", "public_channels": "/api/v1/channels",
            "guide_window": "/devices/ANY/guide", "rules": "/dvr/rules",
            "jobs": "/dvr/jobs", "preview": "/dvr/rules/test"})
        for filename in ("pass_http.py", "pass_preview.py", "pass_ui.py"):
            tree = ast.parse((LIB / filename).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, {"recording_http", "recording_commands",
                                                   "recording_journal", "recording_worker"})

    def test_gui_settings_protocol_and_callbacks_are_closed_boundaries(self):
        tree = ast.parse((LIB / "kodi_ui.py").read_text(encoding="utf-8"))
        protocol = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                        and node.name == "SettingsProtocol")
        self.assertEqual({node.name for node in protocol.body if isinstance(node, ast.FunctionDef)},
                          {"getSettingString", "getSettingBool", "setSettingString",
                           "setSettingBool", "openSettings", "getAddonInfo"})
        window = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                      and node.name == "ChannelsWindow")
        for method in window.body:
            if isinstance(method, ast.FunctionDef) and method.name in ("onInit", "onAction", "onClick"):
                self.assertEqual(len(method.body), 1)
                calls = {dotted(node.func) for node in ast.walk(method) if isinstance(node, ast.Call)}
                self.assertLessEqual(calls, {"self._dispatcher.post", "action.getId", "self._chrome.capture",
                                             "self._chrome.capture_action"})
                self.assertIn("self._dispatcher.post", calls)
        self.assertLess((LIB / "kodi_ui.py").stat().st_size, 48 * 1024)
        for file in ADDON.rglob('*'):
            if file.is_file():
                # Localized Dashboard settings/status expand this one catalog;
                # retain the existing 48-KiB source/asset bound everywhere else.
                limit = 52 * 1024 if file == ADDON / 'resources/language/resource.language.en_gb/strings.po' else 49152
                self.assertLess(file.stat().st_size, limit, str(file.relative_to(ADDON)))

    def test_ast_no_playback_resolver_external_execution_or_persistent_writes(self):
        forbidden_calls = {
            "Player", "play", "playselected", "setResolvedUrl", "resolve", "executeJSONRPC",
            "executebuiltin", "executescript", "exec", "eval", "__import__", "import_module",
            "urlopen", "urlretrieve", "connect", "create_connection", "system", "popen",
            "Popen", "run", "call", "check_call", "check_output", "write", "writelines",
            "write_text", "write_bytes", "mkdir", "makedirs", "unlink", "remove", "rmdir",
            "rename", "replace", "copy", "copyfile", "copytree", "rmtree", "setSetting",
            "setSettingString", "setSettingBool", "setSettingInt", "setSettingNumber",
            "NamedTemporaryFile", "TemporaryFile", "TemporaryDirectory", "mkstemp", "mkdtemp",
            "fsync", "flush",
        }
        # Path.resolve and dataclasses.replace are pure operations, not I/O writes.
        for path, tree in self.python_sources():
            aliases = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    aliases.update({a.asname or a.name: a.name for a in node.names})
                elif isinstance(node, ast.ImportFrom):
                    aliases.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names})
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = dotted(node.func)
                root, _, suffix = name.partition(".")
                qualified = aliases.get(root, root) + ("." + suffix if suffix else "")
                leaf = qualified.rsplit(".", 1)[-1]
                with self.subTest(file=str(path.relative_to(ADDON)), line=node.lineno, call=qualified):
                    owned_write = (path == LIB / "kodi_ui.py" and qualified in
                                   {"self._addon.setSettingString", "self._addon.setSettingBool"})
                    if owned_write:
                        self.assertTrue(node.args)
                        self.assertIsInstance(node.args[0], ast.Constant)
                        allowed = ({"server_origin", "server_expected_version"}
                                   if leaf == "setSettingString" else {"development_metadata_enabled"})
                        self.assertIn(node.args[0].value, allowed)
                    elif path == LIB / 'playback_native.py' and qualified == 'player.play':
                        # Exactly the reviewed direct script adapter, not a global
                        # exemption for Player/play/resolvers/builtins elsewhere.
                        self.assertEqual(ast.unparse(node),'player.play(descriptor.url, item)')
                        self.assertEqual(sum(isinstance(n,ast.Call) and dotted(n.func) == 'player.play'
                                             for n in ast.walk(tree)),1)
                    elif path == LIB / "operation_storage.py" and qualified in {"os.makedirs", "sqlite3.connect"}:
                        if qualified == "os.makedirs":
                            self.assertEqual(ast.unparse(node), "os.makedirs(self._profile, exist_ok=True)")
                        else:
                            self.assertIn(ast.unparse(node.args[0]), {"os.path.join(self._profile, DB_NAME)", "uri"})
                            self.assertEqual(next(ast.literal_eval(k.value) for k in node.keywords
                                                  if k.arg == 'isolation_level'), None)
                    elif path == BOOKMARK_STORAGE and qualified in BOOKMARK_WRITES:
                        self.assertEqual(ast.unparse(node), BOOKMARK_WRITES[qualified])
                    elif qualified != "dataclasses.replace":
                        self.assertNotIn(leaf, forbidden_calls)
                    if leaf in {"open", "File"}:
                        modes = [k.value for k in node.keywords if k.arg == "mode"]
                        if len(node.args) > 1:
                            modes.append(node.args[1])
                        for mode in modes:
                            self.assertIsInstance(mode, ast.Constant)
                            self.assertIsInstance(mode.value, str)
                            self.assertFalse(set(mode.value) & set("wax+"))
                    if leaf == "setProperty" and node.args:
                        if isinstance(node.args[0], ast.Constant) and node.args[0].value == "IsPlayable":
                            self.assertIsInstance(node.args[1], ast.Constant)
                            self.assertEqual(node.args[1].value, "false")

    def test_bookmark_storage_paths_and_file_calls_are_closed(self):
        tree = ast.parse(BOOKMARK_STORAGE.read_text(encoding='utf-8'))
        expected_bindings = [
            ("BOOKMARK_DIRECTORY", "'hub-bookmarks'"),
            ("self._profile", "normalize_profile(profile)"),
            ("self._directory", "os.path.join(self._profile, BOOKMARK_DIRECTORY)"),
            ("self._path", "os.path.join(self._directory, source.server_key + '.json')"),
            ("payload", "encode_bookmarks(bookmarks)"),
            ("temp_path", "stream.name"),
            ("temp_path", "None"),  # cleared after replace, never redirected
            ("temp_path", "None"),  # initial sentinel
        ]
        names = {name for name, _ in expected_bindings}
        bindings = [(dotted(target), ast.unparse(n.value)) for n in ast.walk(tree)
                    if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None
                    for target in (n.targets if isinstance(n, ast.Assign) else [n.target])
                    if dotted(target) in names]
        self.assertCountEqual(bindings, expected_bindings)
        calls = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and (dotted(n.func) == 'open' or dotted(n.func).startswith(('os.', 'tempfile.', 'stream.')))]
        self.assertCountEqual(calls, [
            *BOOKMARK_WRITES.values(),
            "os.path.join(self._profile, BOOKMARK_DIRECTORY)",
            "os.path.join(self._directory, source.server_key + '.json')",
            "os.lstat(path)", "os.lstat(self._path)", "os.lstat(self._path)",
            "open(self._path, 'rb')", "os.fstat(stream.fileno())",
            "stream.read(MAX_BOOKMARK_BYTES + 1)", "stream.fileno()", "stream.fileno()",
        ])

    def test_private_journal_paths_and_profile_access_are_narrow(self):
        tree = ast.parse((LIB / "operation_storage.py").read_text())
        filenames = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                     and isinstance(n.value, str) and n.value.startswith("recording-owned.")}
        self.assertEqual(filenames, {"recording-owned.json", "recording-owned.tmp"})
        opens = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and dotted(n.func) == "os.open"]
        self.assertEqual(len(opens), 0)
        self.assertIn("DB_NAME = 'operation-history.sqlite3'", (LIB / 'operation_storage.py').read_text())
        # SQLite access stays confined to the one owned adapter, not UI/domain code.
        for path, source in self.python_sources():
            for node in ast.walk(source):
                if isinstance(node, ast.Import) and any(a.name == 'sqlite3' for a in node.names):
                    self.assertEqual(path, LIB / 'operation_storage.py')
        for path, tree in self.python_sources():
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and dotted(n.func).endswith("getAddonInfo"):
                    if path in {LIB / "recording_ui.py", LIB / "storage_ui.py"}:
                        self.assertEqual(ast.literal_eval(n.args[0]), "profile")
                    else:
                        self.assertIn(path, {ADDON / "default.py", ADDON / "plugin.py"})

    def test_retired_plugin_file_is_documentation_only(self):
        tree = ast.parse((ADDON / "plugin.py").read_text(encoding="utf-8"))
        self.assertEqual(len(tree.body), 1)
        self.assertIsInstance(tree.body[0], ast.Expr)
        self.assertIsInstance(tree.body[0].value, ast.Constant)


if __name__ == "__main__":
    unittest.main()
