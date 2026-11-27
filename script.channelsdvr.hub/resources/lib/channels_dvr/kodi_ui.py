"""Kodi-only WindowXML adapter. One owner for the Hub and full-screen Guide.

Callbacks only enqueue plain commands; the script loop owns all GUI work.
The pool grows to the high-water visible cell count, never the guide horizon.
"""

import os
from collections.abc import Sequence
from time import monotonic, perf_counter, time
from typing import Protocol

import xbmc
import xbmcgui

from .browser import Row, scheduled_rows
from .dvr_ui import DvrUI, PassRows
from .event_details_ui import show_event_details
from .guide import GuideController, Selection, time_label
from .guide_runtime import GuideRuntime, display_repository
from .guide_fixture_data import FixtureMode as FixtureMode
from .guide_gap import show_station_gap
from .guide_render import RenderMemo, draw
from .guide_screen import hide_guide, paint_clock, paint_guide, station_rows
from .guide_fixtures import cancel_large, install_fixture, tick_large
from .hub_session_ui import HubSessionUI
from .library import LibraryBrowser, PAGE_ROWS
from .metrics import GuiMetrics
from .live_loader import DevelopmentConfig, MetadataLoader
from .live_session import DevelopmentRepository, LiveDataSession
from .metadata_http import MetadataError
from .newbuild_compatibility import BASE_MINIMUM_DVR_BUILD
from .models import Channel, ChannelKey, Event, Job, Recording
from .normalization import DataState
from .repository import MockRepository as MockRepository
from .recording_ui import RecordingUI
from .storage_ui import StorageUI
from .search import ProgrammeSearchIndex, SearchPage, airing_key, normalize_query
from .session import OfflineDataSession, Presentation
from .series_manager_models import RuleRow
from .ui_dispatch import UiDispatcher
from .ui_text import capture, text
from .ui_menu import SETTINGS, group_label, select
from .ui_navigation import execute_command
from .ui_routes import Route, SearchReturn, VIEW_KEYS, enter, launch_view, presentation_scope
from .ui_hits import Hit
from .ui_modal import ModalGui, dialog_for, modal_input
from .ui_browse_navigation import search_menu
from .ui_recordings import rows_status
from .ui_status import STATUS_KEYS
from .artwork_ui import ArtworkUI, show_media_details
from .playback_ui import PlaybackUI
from .dashboard_ui import DashboardUI, format_timestamp
from .ui_chrome import BACK, CONTEXT, DOWN, LEFT, NAV_BACK, RIGHT, UP, WindowChrome



class SettingsProtocol(Protocol):
    def getAddonInfo(self, key: str, /) -> str: ...
    def getSettingString(self, key: str, /) -> str: ...
    def getSettingBool(self, key: str, /) -> bool: ...
    def setSettingString(self, key: str, value: str, /) -> bool: ...
    def setSettingBool(self, key: str, value: bool, /) -> bool: ...
    def openSettings(self) -> None: ...


class ChannelsWindow(xbmcgui.WindowXML):
    def __init__(self, *args, **kwargs):
        requested_view = kwargs.pop('start_view', None)
        start_view = launch_view(requested_view or 'Dashboard')
        super().__init__(*args, **kwargs)
        self._dispatcher = UiDispatcher()
        self._closed = False
        self._paint_pending = False
        self._addon: SettingsProtocol | None = None
        self.strings = capture()
        self._strings_bound = False
        self.transient_demo_mode = False
        self._metadata_loader = MetadataLoader()
        self._live_session: LiveDataSession | None = None
        self._development_config: DevelopmentConfig | None = None
        self._development_blocked: str | None = None
        self._config_save_failed = False
        self._intended_anchor: int | None = None
        self._status_tick: float | None = None
        self.repository: MockRepository = DevelopmentRepository(0, (0, 7200))
        self.guide = GuideController(display_repository(self.repository), deferred=True)
        self._route = Route(start_view)
        self._automatic_launch = requested_view is None
        self._hub_session = HubSessionUI(self)
        self._guide_return: tuple[str, Selection, int, int] | None = None
        self._search_return: SearchReturn | None = None
        self._native_window: int | None = None
        self.positions: dict[str, int] = {}
        self._dvr_selected: str | None = None
        self._dvr_rows_key: tuple | None = None
        self._dvr_rows: tuple[Row, ...] = ()
        self._hit_plan: list[Hit] = []
        self.pool: list[tuple[xbmcgui.ControlImage, xbmcgui.ControlLabel]] = []
        self._guide_line: xbmcgui.ControlImage | None = None
        self.initialized = False
        self.path = ""
        self.metrics = GuiMetrics()
        self._visible_count = 0
        self._render_memo = RenderMemo()
        self.stress_fixture = False
        self._large_fixture = False
        self._large_exercise = None
        self.exercise_summary = ''
        self.data_session = OfflineDataSession(self.repository)
        self.library = LibraryBrowser(self.repository.recordings)
        self.search_query = ""
        self.search_selection: tuple[ChannelKey, str, int] | None = None
        self._search_index: ProgrammeSearchIndex | None = None
        self._search_snapshot: tuple[Event, ...] | None = None
        self._search_channels: tuple[Channel, ...] | None = None
        self._search_page_key: object = None
        self._search_page_value: SearchPage | None = None
        self._search_rows_value: tuple[Row, ...] = ()
        self._recording_ui = RecordingUI(self)
        self._storage_ui = StorageUI(self)
        self._series_ui = None
        self._dvr = DvrUI(self)
        self._guide_runtime = GuideRuntime(self)
        self._artwork_ui = ArtworkUI(self)
        self._playback_ui = PlaybackUI(self)
        self._dashboard_ui = DashboardUI(self, timestamp=self._dashboard_timestamp)
        self._chrome = WindowChrome(self)

    def onInit(self):
        self._dispatcher.post(("initialize", None))

    def onAction(self, action):
        self._dispatcher.post(("action", self._chrome.capture_action(action.getId())))

    def onClick(self, control_id):
        self._dispatcher.post(('chrome', self._chrome.capture(control_id)))

    @property
    def view(self) -> str:
        return self._route.view

    def _owns_input(self):
        if not self.initialized or self._dispatcher.modal_active:
            return False
        return self._native_window is None or xbmcgui.getCurrentWindowId() == self._native_window

    def process_pending(self) -> None:
        self._dispatcher.assert_owner()
        if self._closed or self._dispatcher.modal_active:
            return
        self._revoke_demo()
        for kind, value in self._dispatcher.dispatch():
            if self._closed:
                break
            if kind == "initialize":
                with modal_input(self):
                    self._initialize()
            elif not self._owns_input():
                continue
            else:
                previous = self._hub_session.input_state()
                if kind == "action":
                    self._handle_action(value)
                elif kind == 'chrome':
                    cancel_large(self, 'click')
                    self._chrome.click(value, xbmcgui)
                self._hub_session.note_input(previous)
        if not self._closed:
            changed = self._guide_runtime.poll()
            if self.initialized:
                changed = self._hub_session.poll() or changed
            changed = self._dvr.poll() or changed
            changed = self._artwork_ui.poll() or changed
            changed = self._dashboard_ui.poll() or changed
            if not self._closed and (changed or self._paint_pending):
                self.render()
        if not self._closed and self.initialized and self._live_session is not None and self.view not in ('Dashboard', 'Settings'):
            now = monotonic()
            if self._status_tick is None or now - self._status_tick >= 1:
                self._label(114, self._current_data().banner)
                if self.view == 'Guide':
                    paint_clock(self)
                self._status_tick = now
        if not self._closed:
            self._recording_ui.poll()
            self._storage_ui.poll()
            self._playback_ui.poll(ModalGui(self, xbmcgui))
            if self._series_ui is not None:
                self._series_ui.poll()
            tick_large(self)

    def shutdown(self) -> None:
        self._dispatcher.assert_owner()
        if self._closed:
            return
        self._hub_session.close()
        self._closed = True
        self._dashboard_ui.close()
        self._playback_ui.close()
        self._artwork_ui.close()
        cancel_large(self, "window closed")
        self._recording_ui.close()
        self._storage_ui.close()
        self._dvr.refresh()
        if self._series_ui is not None:
            self._series_ui.close()
        self._guide_runtime.close()
        self._dispatcher.close()
        self._chrome.reset()
        super().close()
        self._artwork_ui.detached()

    def close(self):
        self.shutdown()

    def _series_manager(self, gui):
        from .series_manager_ui import SeriesManagerUI
        from .pass_ui import _programme_scope
        self._dispatcher.assert_owner()
        if self._series_ui is None:
            self._series_ui = SeriesManagerUI(assert_owner=self._dispatcher.assert_owner,
                dialog=ModalGui(self, gui).Dialog, profile=self._recording_ui._programme_profile,
                config=self._recording_ui._current_config,
                selection=self._series_selection,
                viewer=lambda heading, body: _programme_scope(self.path, heading, body, self.strings, modal_owner=self),
                source_scope=lambda: self._recording_ui.preview._control_context()[0],
                strings=self.strings, default_no=gui.DLG_YESNO_NO_BTN,
                presentation_scope=lambda: presentation_scope(self))
        return self._series_ui

    def _series_manager_request(self, gui):
        self._series_manager(gui).request()

    def _series_selection(self):
        self._dispatcher.assert_owner()
        if (self.view != 'Guide' or self._live_session is None or self._live_session.loading
                or not self._current_data().can_browse):
            return None
        return self.selected_indexed_event()

    def _initialize(self) -> None:
        self._dispatcher.fence_input()
        self._native_window = xbmcgui.getCurrentWindowId()
        if not self._strings_bound:
            self.strings = capture(getattr(self._addon, 'getLocalizedString', None))
            self._strings_bound = self._addon is not None
            self.guide.strings = self.strings
            self.guide.projection.strings = self.strings
            self.guide.projection.snapshot = None
            self.guide.projection.update(self.guide.repository.channels)
            self.data_session.strings = self.strings
            self.library.bind_strings(self.strings)
            if self._live_session is not None:
                self._live_session.strings = self.strings
                self._live_session.channel_projection.strings = self.strings
                self._live_session.channel_projection.snapshot = None
        cancel_large(self, "window initialized")
        self._render_memo.reset()
        self._chrome.reset()
        self._artwork_ui.reset()
        self.metrics.initialized()
        if not self.initialized:
            self.initialized = True
            if self.get_setting("development_metadata_enabled") is True:
                config = self._saved_config()
                if config is not None:
                    self._activate(config)
                    if self._automatic_launch:
                        self._hub_session.arm_startup(config)
                    self._request_live(int(time()))
                else:
                    self._suspend_development()
        self.render()
        self._chrome.initialize_focus()

    def get_setting(self, key: str) -> str | bool:
        if key not in ("server_origin", "server_expected_version", "development_metadata_enabled",
                       "ShowDeveloperTools", "dashboard_refresh_minutes", "hub_startup_view", "hub_guide_ok"):
            raise ValueError("Unknown owned setting")
        if self._addon is None:
            return False if key in ("development_metadata_enabled", "ShowDeveloperTools") else ""
        if key in ("development_metadata_enabled", "ShowDeveloperTools"):
            return self._addon.getSettingBool(key)
        return self._addon.getSettingString(key)

    def t(self, key: str, **values: object) -> str:
        return text(self.strings, key, **values)

    @staticmethod
    def utc_now() -> int:
        return int(time())

    @staticmethod
    def _dashboard_timestamp(stamp):
        return format_timestamp(stamp, getattr(xbmc, 'getRegion', None))

    def developer_enabled(self) -> bool:
        try:
            return self.get_setting('ShowDeveloperTools') is True
        except (RuntimeError, AttributeError, TypeError, ValueError):
            return False

    def demo_allowed(self) -> bool:
        return (self.transient_demo_mode is True and self.developer_enabled()
                and not self._closed and self._live_session is None)

    def _open_demo(self) -> None:
        self._dispatcher.assert_owner()
        if not self.developer_enabled() or self._closed:
            return
        self.transient_demo_mode = True
        self._development_blocked = None
        cancel_large(self, 'demo opened')
        self._guide_runtime.mode('normal')
        enter(self, 'Guide')

    def _close_demo(self) -> None:
        self._dispatcher.assert_owner()
        self._hub_session.source_changed()
        self.transient_demo_mode = False
        self._development_blocked = 'metadata disabled'
        cancel_large(self, 'demo closed')
        self._guide_runtime.mode('no_connection')

    def _revoke_demo(self) -> None:
        if self.transient_demo_mode and not self.developer_enabled():
            self._close_demo()

    def _saved_config(self) -> DevelopmentConfig | None:
        try:
            if self._config_save_failed:
                return None
            origin = self.get_setting("server_origin")
            if type(origin) is not str:
                return None
            # Both entry points accept the same forms. Normalize in memory only;
            # legacy persisted per-build pins remain ignored, never rewritten.
            return DevelopmentConfig.from_address(origin)
        except (MetadataError, ValueError, RuntimeError, AttributeError, TypeError):
            return None

    @staticmethod
    def _config_host(config: DevelopmentConfig) -> str:
        authority = config.origin.removeprefix("http://").removesuffix(":8089")
        return authority[1:-1] if authority.startswith("[") and authority.endswith("]") else authority

    def _configure_development(self) -> None:
        config = self._saved_config()
        try:
            host = dialog_for(self, xbmcgui).input(self.t('address_ip'),
                                         defaultt=self._config_host(config) if config else "")
        except (RuntimeError, AttributeError, TypeError, ValueError):
            dialog_for(self, xbmcgui).ok(self.t('connection'), self.t('address_dialog_failed'))
            return
        if not host or self._addon is None:
            return
        try:
            address = DevelopmentConfig.from_address(host)
            config = DevelopmentConfig(address.origin, config.namespace if config else address.namespace,
                                       BASE_MINIMUM_DVR_BUILD)
        except MetadataError:
            dialog_for(self, xbmcgui).ok(self.t('connection'), self.t('address_invalid'))
            return
        if not self._save_development(config=config):
            return
        if (self._live_session is not None and self._development_blocked is None
                and config != self._development_config):
            self._activate(config)

    def _save_development(self, config: DevelopmentConfig | None = None,
                          enabled: bool | None = None) -> bool:
        try:
            if self._addon is None:
                raise ValueError
            if config is not None:
                if self._addon.setSettingString("server_origin", config.origin) is not True:
                    raise ValueError
                self._config_save_failed = False
            if enabled is not None:
                if self._addon.setSettingBool("development_metadata_enabled", enabled) is not True:
                    raise ValueError
            return True
        except (RuntimeError, AttributeError, TypeError, ValueError):
            if config is not None:
                self._config_save_failed = True
            if self._live_session is not None:
                self._suspend_development()
            dialog_for(self, xbmcgui).ok(self.t('connection'), self.t('connection_save_failed'))
            return False

    def _activate(self, config: DevelopmentConfig) -> None:
        self._dispatcher.assert_owner()
        self.transient_demo_mode = False
        if (self._live_session is not None and config == self._development_config
                and self._development_blocked is None):
            return
        self._replace_live(config)
        self._development_blocked = None

    def _suspend_development(self, disabled: bool = False) -> None:
        self._replace_live(None)
        self._development_blocked = "metadata disabled" if disabled else "configuration required"

    def _replace_live(self, config: DevelopmentConfig | None) -> None:
        self._dispatcher.assert_owner()
        self._hub_session.source_changed()
        self._playback_ui.cancel()
        cancel_large(self, "mode changed")
        self._guide_runtime.mode("live", config)

    def _reset_search(self) -> None:
        self._search_return = None
        self.positions.pop('Search', None)
        self.search_query = ""
        self.search_selection = None
        self._clear_search_cache()

    def _reset_dvr(self) -> None:
        self.positions.pop('DVR', None)
        self._dvr_selected = self._dvr_rows_key = None
        self._dvr_rows = ()
        self._dvr.reset()

    def _clear_search_cache(self) -> None:
        # Runtime holds immutable source/records on worker until commit. Normal
        # metadata refresh preserves query, airing bookmark and cursor.
        self._search_index = None
        self._search_snapshot = None
        self._search_channels = None
        self._search_page_key = None
        self._search_page_value = None
        self._search_rows_value = ()

    def _request_live(self, anchor: int) -> None:
        self._dispatcher.assert_owner()
        pending_mode = self._guide_runtime._mode
        if pending_mode is not None and pending_mode[0] == 'live' and pending_mode[1] is not None:
            self._guide_runtime.request(anchor)
            return
        if (self._live_session is None or self._development_config is None
                or self._development_blocked is not None):
            dialog_for(self, xbmcgui).ok(self.t('connection'), self.t('configure_first'))
            return
        self._guide_runtime.request(anchor)

    def _refresh_live(self) -> None:
        if self._development_blocked is not None:
            return
        anchor = self._intended_anchor if self._intended_anchor is not None else self.guide.selection.anchor
        start = self.repository.horizon[0]
        self._request_live(start)
        self._intended_anchor = anchor

    def _jump_time(self, anchor: int) -> None:
        if self._development_blocked is not None:
            return
        if self._route.parent == 'Search':
            # A drill-in browses the shared accepted horizon without replacing
            # the product behind Search or the primary Guide bookmark.
            if self._guide_runtime.ready:
                self.guide.jump(anchor)
            return
        start, end = self.repository.horizon
        if self._live_session is not None and (self._live_session.loading or not start <= anchor < end):
            if not self._guide_runtime.reenter(anchor):
                self._request_live(anchor)
        else:
            if self._live_session is not None and not self._guide_runtime.ready:
                # Metadata already names this horizon, but adoption still owns
                # the final cursor. Replace the previous request's anchor.
                self._intended_anchor = anchor
            self.guide.jump(anchor)

    def _source_label(self, key: ChannelKey) -> str:
        catalog = self.guide.repository.channels if self.view == "Guide" else self.repository.channels
        return self.guide.projection.update(catalog).source_label(key)

    def _label(self, control_id: int, label: str) -> None:
        self._render_memo.label(self, xbmcgui, control_id, label)

    def _texture(self, name: str) -> str:
        return os.path.join(self.path, "resources", "skins", "Default", "media", name)

    def _ensure_guide_line(self) -> "xbmcgui.ControlImage":
        if self._guide_line is None:
            line = xbmcgui.ControlImage(0, 0, 1, 1, self._texture("selected.png"))
            self.addControls([line])
            self._guide_line = line
        return self._guide_line

    def _ensure_pool(self, count: int) -> None:
        self._visible_count = count
        added: list[xbmcgui.Control] = []
        while len(self.pool) < count:
            # Reuse the old top image as a cell on growth. The next line is
            # appended after the new cells, with only one extra image total.
            image, self._guide_line = self._guide_line, None
            if image is None:
                image = xbmcgui.ControlImage(0, 0, 1, 1, self._texture("cell.png"))
                added.append(image)
            label = xbmcgui.ControlLabel(0, 0, 1, 1, "", font="font13",
                                        textColor="FFFFFFFF")
            self.pool.append((image, label))
            added.append(label)
        if added:
            self.addControls(added)
        for image, label in self.pool[count:]:
            self._render_memo.apply(image, "setVisible", False)
            self._render_memo.apply(label, "setVisible", False)

    def _draw(self, index: int, x: int, y: int, width: int, height: int,
              text: str, selected: bool, gap: bool = False) -> None:
        draw(self, index, x, y, width, height, text, selected, gap)

    def _rows(self) -> Sequence[Row]:
        if self.view in ('Dashboard', 'Guide'):
            return ()
        if self.view == 'Settings':
            return tuple(Row(code, self.t(label), '') for code, label in SETTINGS)
        if self._dvr.passes:
            return self._dvr.rows()
        if self._development_blocked is not None or (self._live_session is None and not self.demo_allowed()):
            return ()
        if self.view == "Search":
            self._search_page()
            return self._search_rows_value
        if self.view == 'DVR':
            demo = self.demo_allowed()
            r = self.repository
            session = self._live_session
            key = (id(r.jobs), id(r.events), id(r.channels), self._guide_runtime.revision,
                   session._catalog_epoch if session is not None else None, demo, id(self.strings))
            if key != self._dvr_rows_key:
                self._dvr_rows = scheduled_rows(r, self.strings, demo=demo)
                self._dvr_rows_key = key
                if self._dvr_rows:
                    self._select_list(self._dvr_rows, next((i for i, row in enumerate(self._dvr_rows)
                        if row.id == self._dvr_selected), self.positions.get('DVR', 0)))
            return self._dvr_rows
        return self.library.rows()

    def _detail_text(self, text: str) -> str:
        # Compatibility helper: provider text is never rewritten by English markers.
        return text

    def _position_key(self) -> str:
        return self.view

    def _selected_position(self, rows):
        if isinstance(rows, PassRows):
            return self._dvr.position
        return self.library.position if self.view == 'Recordings' else min(
            self.positions.get(self._position_key(), 0), max(0, len(rows) - 1))

    def _select_list(self, rows, index):
        index = max(0, min(index, len(rows) - 1))
        if self.view == 'Recordings':
            self.library.move(index - self.library.position)
        elif isinstance(rows, PassRows):
            self._dvr.select(rows.items, index)
        else:
            self.positions[self._position_key()] = index
            if rows and self.view == 'Search':
                self.search_selection = airing_key(self._search_page().matches[index])
            elif rows and self.view == 'DVR':
                self._dvr_selected = rows[index].id

    def _select_hit(self, target):
        kind, *key = target
        if kind == 'cell':
            channel, event_id, start = key
            return self.view == 'Guide' and self._guide_runtime.ready and self.guide.focus_cell(channel, event_id, start)
        if kind != 'row' or self.view not in ('Recordings', 'DVR', 'Search', 'Settings'):
            return False
        if self._dvr.passes:
            data = self._dvr.data()
            return data.can_browse and self._dvr.hit(data.items, key[0])
        if self.view != 'Settings' and not self._current_data().can_browse:
            return False
        rows = self._rows()
        matches = [i for i, row in enumerate(rows) if row.id == key[0]]
        if len(matches) != 1:
            return False
        self._select_list(rows, matches[0])
        return True

    def _current_data(self) -> Presentation[Event] | Presentation[Recording] | Presentation[Job] | Presentation[RuleRow]:
        if self.view in ('Dashboard', 'Settings'):
            return Presentation((), DataState.EMPTY, False, False, None, None, '', '', False)
        if self.view == 'DVR' and self._dvr.passes:
            return self._dvr.data()
        if self._development_blocked is not None or (self._live_session is None and not self.demo_allowed()):
            return Presentation((), DataState.OUTAGE, False, False, None, None,
                                self.t('not_connected'), self.t('connection_prompt'),
                                False)
        if self._live_session is not None:
            if self.view in ("Guide", "Search"):
                return self._guide_runtime.presentation(self._live_session.guide())
            if self.view == "Recordings":
                return self._live_session.recordings()
            return self._live_session.jobs()
        if self.view in ("Guide", "Search"):
            guide = self.data_session.guide()
            if guide.updated_at is not None:
                self.repository.events = guide.items
            return self._guide_runtime.presentation(guide)
        if self.view == "Recordings":
            recordings = self.data_session.recordings()
            if recordings.can_browse or (recordings.state == DataState.EMPTY and recordings.updated_at is not None):
                self.repository.recordings = recordings.items
                self.library.update(recordings.items)
            return recordings
        jobs = self.data_session.jobs()
        if jobs.can_browse:
            self.repository.jobs = jobs.items
        return jobs

    def render(self) -> None:
        self._dispatcher.assert_owner()
        if self._closed:
            return
        self._revoke_demo()
        started = perf_counter()
        self._render_memo.begin((self.view, self.guide, self._live_session,
                                 self._development_blocked, self.path))
        try:
            self._hit_plan = []
            self._render()
            self._chrome.render(xbmcgui, tuple(self._hit_plan))
            self._artwork_ui.update()
            self._artwork_ui.draw(xbmcgui)
            self._render_memo.finish(self, xbmcgui)
            self._paint_pending = False
        finally:
            self._render_memo.labels = None
            self.metrics.rendered((perf_counter() - started) * 1000,
                                  len(self.pool), self._visible_count)

    def _render(self) -> None:
        if self.view != 'Guide':
            hide_guide(self)
        if self.view == 'Dashboard':
            self._dashboard_ui.render()
            if self._hub_session.startup_failed:
                self._label(112, self.t('refresh_hint'))
            return
        self._label(116, '')
        self._label(117, '')
        data = self._current_data()
        view_label = self.t(VIEW_KEYS[self.view])
        if self.demo_allowed():
            view_label = self.t('demo_guide') if self.view == 'Guide' else self.t('demo_view', view=view_label)
        self._label(110, self.t('header', view=view_label))
        for tick in range(4):
            self._label(140 + tick, "")
        for row_index in range(6):
            self._label(120 + row_index, "")
        self._label(112, "")
        self._label(113, "")
        self._label(114, data.banner)
        self._status_tick = monotonic() if self._live_session is not None else None
        self._label(115, self.t('demo_footer') if self.demo_allowed() else self.t('listings_footer'))
        if self.view == 'Settings':
            self._label(112, self.t('connection'))
            session = self._live_session
            if isinstance(session, LiveDataSession) and self._development_blocked is None:
                connection_presentation = session.guide()
                self._label(114, connection_presentation.banner)
                self._label(113, connection_presentation.message)
            else:
                self._label(114, self.t('not_connected'))
                self._label(113, self.t('connection_prompt'))
            self._label(115, '')
            self._render_rows(self._rows())
            return
        if self.view == 'DVR':
            self._dvr.render(data)
            return
        retained = ((self._live_session is not None or self.demo_allowed())
                    and self.view == "Guide" and self.guide.prepared_ready
                    and not self._guide_runtime.ready
                    and (data.updated_at is not None or (self._live_session is not None
                                                         and self._live_session.loading))
                    and not self._development_blocked)
        if not data.can_browse and not (self.view == 'Guide' and station_rows(self, data)) and not retained:
            if self.view == 'Guide':
                hide_guide(self)
            self._ensure_pool(0)
            if self._development_blocked is not None or (self._live_session is None and not self.demo_allowed()):
                state = 'not_connected'
            elif data.loading:
                state = 'live_loading'
            elif data.state not in (None, DataState.READY, DataState.EMPTY):
                state = STATUS_KEYS[data.state.value]
            elif data.updated_at is None:
                state = 'no_listings'
            elif data.state == DataState.EMPTY:
                state = 'empty_update'
            elif self.view in ('Guide', 'Search') and not self._guide_runtime.ready:
                state = 'guide_unavailable' if self._guide_runtime.error else 'preparing_guide'
            else:
                state = 'no_listings'
            # Deferred source retirement can leave the old breadcrumb in memory.
            accepted_empty = (self.view == 'Recordings' and state == 'empty_update'
                              and self._guide_runtime._mode is None)
            self._label(112, self.library.breadcrumb if accepted_empty else
                        self.t('empty_view', view=view_label, status=self.t(state)))
            self._label(113, self.t('refresh_hint') if self._live_session is not None else
                        self.t('demo_hint') if self.demo_allowed() else self.t('connection_prompt'))
            return
        if self.view == 'Guide':
            paint_guide(self, retained)
        else:
            rows = self._rows()
            selected = self._render_rows(rows)
            if self.view == "Search":
                page = self._search_page()
                self._label(112, self.t('search_heading', query=page.query[:80] or self.t('search_query_prompt'),
                                       group=group_label(self.strings, self.guide.group)))
                status = self.t('search_no_matches' if page.query else 'search_choose')
                if rows:
                    status = self.t('search_rows_status', position=selected + 1, count=len(rows))
                if page.truncated:
                    status += self.t('results_limited')
                self._label(113, status)
            elif self.view == "Recordings":
                self._label(112, self.library.breadcrumb)
                self._label(113, rows_status(self, rows, selected))

    def _render_rows(self, rows):
        selected = self._selected_position(rows)
        recordings = self.view == 'Recordings'
        first = self.library.top if recordings else selected // PAGE_ROWS * PAGE_ROWS
        y, stride, height = (311, 82, 72) if recordings else (250, 95, 85)
        visible = rows[first:first + PAGE_ROWS]
        self._ensure_pool(len(visible))
        for index, row in enumerate(visible):
            rect = (80, y + index * stride, 1760, height)
            self._draw(index, *rect, row.label, first + index == selected)
            self._hit_plan.append(Hit(rect, ('row', row.id)))
        return selected

    def _details(self, allow_search_action: bool = True, *, origin=100) -> None:
        if self.view == 'Dashboard':
            return
        if self.view == 'Settings':
            rows = self._rows()
            execute_command(self, xbmcgui, rows[self._selected_position(rows)].id)
            return
        if self._dvr.passes:
            self._dvr.show(dialog_for(self, xbmcgui))
            return
        data = self._current_data()
        if (self._live_session is not None and self._development_blocked is None
                and self.view == "Guide" and self._guide_runtime.ready
                and self.guide.selected_event() is None):
            channel = self.guide.channel(self.guide.selection.channel)
            if show_station_gap(self, channel, dialog_for(self, xbmcgui)):
                return
        if not data.can_browse:
            dialog_for(self, xbmcgui).ok(self.t('demo_status') if self.demo_allowed() else self.t('connection'), data.message)
            return
        if self.view == "Search":
            event = self._selected_search_event()
            if event is None:
                dialog_for(self, xbmcgui).ok(self.t('search_programmes'), self.t('search_select_result'))
                return
            if allow_search_action:
                entries = (('result.details', 'event_details'), ('result.guide', 'show_in_guide'))
                choice = select(dialog_for(self, xbmcgui), self.strings, 'search_result', entries)
                if choice is None:
                    return
                if choice == 'result.guide':
                    self._show_search_in_guide(event, origin=origin)
                    return
            if self._selected_search_event() is event:
                self._show_event_details(event)
        elif self.view == "Guide":
            event = self.selected_indexed_event()
            if event is None:
                dialog_for(self, xbmcgui).ok(self.t('no_listing'), self.t('temporal_gap'))
                return
            self._show_event_details(event)
        elif self.view == "Recordings":
            row = self.library.selected
            if row is None:
                dialog_for(self, xbmcgui).ok(self.t('recordings'), self.t('recording_select_item'))
                return
            if not self._artwork_ui.ready() or show_media_details(self, row) is not True:
                dialog_for(self, xbmcgui).textviewer(row.label, self._detail_text(row.detail))
        else:
            rows = self._rows()
            if not rows:
                return
            index = min(self.positions.get(self._position_key(), 0), len(rows) - 1)
            row = rows[index]
            if not self._artwork_ui.ready() or show_media_details(self, row) is not True:
                dialog_for(self, xbmcgui).textviewer(row.label, row.detail)

    def _show_search_in_guide(self, event: Event | None = None, *, origin=100):
        if self._closed or self.view != 'Search':
            return
        selected = self._selected_search_event()
        bookmark = self.guide.bookmark()
        if selected is None or (event is not None and event is not selected) or not self.guide.focus_event(selected):
            dialog_for(self, xbmcgui).ok(self.t('search_programmes'), self.t('search_airing_gone'))
            return
        enter(self, 'Guide', guide_return=bookmark,
              search_return=SearchReturn(airing_key(selected), self.positions.get('Search', 0), origin))

    def selected_indexed_event(self, event: Event | None = None) -> Event | None:
        return self._guide_runtime.selected(event)

    def _show_event_details(self, event: Event) -> None:
        show_event_details(self, event, dialog_for(self, xbmcgui))

    def _set_search_query(self, text: str) -> None:
        normalized = normalize_query(text)
        if normalized != normalize_query(self.search_query):
            self.search_selection = None
            self.positions["Search"] = 0
        # Keep original input: Unicode normalization may expand 128 characters.
        self.search_query = text

    def _search_page(self) -> SearchPage:
        if self._development_blocked is not None or (self._live_session is None and not self.demo_allowed()):
            self._search_rows_value = ()
            return SearchPage(self.search_query, (), 0, False, False)
        events, channels = self.repository.events, self.repository.channels
        if (self._search_index is None or self._search_snapshot is not events
                or self._search_channels is not channels):
            self._search_index = ProgrammeSearchIndex(events, channels)
            self._search_snapshot, self._search_channels = events, channels
            self._search_page_key = None
        allowed = tuple(channel.key for channel in self.guide.channels)
        key = (id(events), id(channels), normalize_query(self.search_query), allowed,
               self.repository.horizon)
        if key != self._search_page_key:
            page = self._search_index.search(self.search_query, allowed_channels=allowed,
                                             horizon=self.repository.horizon)
            position = next((i for i, event in enumerate(page.matches)
                             if airing_key(event) == self.search_selection),
                            min(self.positions.get("Search", 0), max(0, len(page.matches) - 1)))
            self.positions["Search"] = position
            self.search_selection = airing_key(page.matches[position]) if page.matches else None
            inventory = {channel.key: channel for channel in channels}
            self._search_rows_value = tuple(
                Row(repr(airing_key(event)),
                     self.t('search_row', title=event.title, number=inventory[event.channel].number,
                            source=self._source_label(event.channel), time=time_label(event.start)), self.t('search_result'))
                for event in page.matches)
            self._search_page_value, self._search_page_key = page, key
        assert self._search_page_value is not None
        return self._search_page_value

    def _selected_search_event(self) -> Event | None:
        if not self._guide_runtime.ready:
            return None
        page = self._search_page()
        position = min(self.positions.get("Search", 0), max(0, len(page.matches) - 1))
        return self.selected_indexed_event(page.matches[position]) if page.matches else None

    def _search_menu(self) -> None:
        search_menu(self, xbmcgui)

    def _diagnostics(self) -> None:
        if not self.developer_enabled():
            return
        dialog_for(self, xbmcgui).textviewer(self.t('timing'),
            self.t('diagnostic_summary', metrics=self.metrics.compact_report(self.strings),
                   exercise=self.exercise_summary or self.t('exercise_none'), stress=self.stress_fixture))

    def _toggle_stress_fixture(self) -> None:
        if not self.demo_allowed():
            return
        install_fixture(self, "normal" if self.stress_fixture else "stress")

    def _content_edge(self, action_id):
        if self.view == 'Dashboard':
            return True
        if self.view == 'Guide':
            # Down at the last channel reaches the action rail. Page moves a
            # viewport at a time without stealing ordinary Guide directions.
            last = self.guide.channels[-1].key if self.guide.channels else None
            data = self._current_data()
            return action_id == DOWN and (self.guide.selection.channel == last or
                                          not (data.can_browse or station_rows(self, data)))
        rows = self._rows()
        index = self._selected_position(rows)
        return index == 0 if action_id == UP else index >= len(rows) - 1

    def _navigate(self, action_id: int, *, page=False) -> None:
        self._playback_ui.cancel()
        started = perf_counter()
        try:
            data = self._current_data()
            if self.view == 'Settings':
                if action_id in (UP, DOWN):
                    rows = self._rows()
                    self._select_list(rows, self._selected_position(rows) + (PAGE_ROWS if page else 1) * (-1 if action_id == UP else 1))
                self.render()
                return
            if self._development_blocked is not None:
                self.render()
                return
            live_guide = (self._live_session is not None and self.view == 'Guide'
                          and self._route.parent is None and data.can_browse)
            if not data.can_browse and not (self.view == 'Guide' and station_rows(self, data)):
                self.render()
                return
            if self.view == "Guide":
                if action_id in (1, 2):
                    if live_guide and self._live_session is not None and self._live_session.loading:
                        anchor = (self._intended_anchor if self._intended_anchor is not None
                                  else self.guide.selection.anchor)
                        self._request_live(anchor + (-1800 if action_id == 1 else 1800))
                    else:
                        before = self.guide.selection
                        self.guide.horizontal(-1 if action_id == 1 else 1)
                        after = self.guide.selection
                        if live_guide and before == after:
                            start, end = self.repository.horizon
                            target = start - 1800 if action_id == 1 else end
                            self._request_live(max(0, target))
                elif page:
                    self.guide.page(-1 if action_id == UP else 1)
                else:
                    self.guide.vertical(-1 if action_id == UP else 1)
            elif action_id in (UP, DOWN):
                rows = self._rows()
                self._select_list(rows, self._selected_position(rows) + (PAGE_ROWS if page else 1) * (-1 if action_id == UP else 1))
            self.render()
        finally:
            self.metrics.navigated((perf_counter() - started) * 1000)

    def _exercise(self, max_steps: int = 60) -> None:
        # Explicit fixed local synthetic exercise, not an RPC/action executor.
        # Same owner-context navigation/render path as ordinary directions.
        if type(max_steps) is not int or max_steps not in (60, 600):
            raise ValueError("Only fixed 60/600-step synthetic exercises are supported")
        if not self.demo_allowed():
            return
        enter(self, 'Guide')
        started = perf_counter()
        completed = 0
        for step in range(max_steps):
            action_id = (4, 2, 3, 1)[step % 4]
            self._navigate(action_id)
            completed += 1
            if perf_counter() - started >= 2.0:
                break
        elapsed = (perf_counter() - started) * 1000
        self.exercise_summary = self.t('exercise_run', completed=completed, steps=max_steps, milliseconds=f'{elapsed:.3f}')
        self._diagnostics()

    def _handle_action(self, activation):
        action_id = activation if type(activation) is int else activation.action
        if action_id in (BACK, NAV_BACK) and self._guide_runtime.cancel_fixture():
            self._dispatcher.fence_input()
        if self._large_exercise is not None:
            cancel_large(self, "input")
            if action_id == CONTEXT:
                self._dispatcher.fence_input()
                return
        if self._chrome.action(activation, xbmcgui):
            return
        if action_id in (LEFT, RIGHT, UP, DOWN):
            self._navigate(action_id)


def open_window(addon_path: str, addon: SettingsProtocol | None = None, *, start_view: str | None = None) -> None:
    window = ChannelsWindow("ChannelsDvr.xml", addon_path, "Default", "1080i", start_view=start_view)
    window.path = addon_path
    window._addon = addon
    monitor = xbmc.Monitor()
    try:
        window.show()
        while not window._closed and not monitor.abortRequested():
            window.process_pending()
            if window._closed or monitor.waitForAbort(0.05):
                break
    finally:
        window.shutdown()
