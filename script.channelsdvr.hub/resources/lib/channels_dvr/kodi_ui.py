"""Kodi-only WindowXML adapter. One focus sink; all drawing is non-focusable.

Callbacks only enqueue plain commands; the script loop owns all GUI work.
The pool grows to the high-water visible cell count, never the guide horizon.
"""

import os
from time import monotonic, perf_counter, time
from typing import Protocol

import xbmc
import xbmcgui

from .browser import Row, scheduled_rows
from .event_details_ui import show_event_details
from .guide import GuideController, time_label
from .guide_runtime import GuideRuntime, display_repository
from .guide_fixture_data import FixtureMode as FixtureMode
from .guide_gap import show_station_gap, station_label
from .guide_render import RenderMemo, draw
from .guide_fixtures import cancel_large, install_fixture, tick_large
from .library import LibraryBrowser
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
from .ui_dispatch import UiDispatcher
from .ui_text import capture, text
from .ui_menu import VIEW_KEYS, group_label, select
from .ui_navigation import connection_menu, main_menu
from .ui_browse_navigation import recording_tools, search_menu, time_navigation
from .ui_status import STATUS_KEYS
from .artwork_ui import ArtworkUI, show_media_details
from .playback_ui import PlaybackUI
from .dashboard_ui import DashboardUI
from .ui_chrome import WindowChrome


class SettingsProtocol(Protocol):
    def getAddonInfo(self, key: str, /) -> str: ...
    def getSettingString(self, key: str, /) -> str: ...
    def getSettingBool(self, key: str, /) -> bool: ...
    def setSettingString(self, key: str, value: str, /) -> bool: ...
    def setSettingBool(self, key: str, value: bool, /) -> bool: ...
    def openSettings(self) -> None: ...


class ChannelsWindow(xbmcgui.WindowXML):
    def __init__(self, *args, **kwargs):
        start_view = kwargs.pop('start_view', 'Dashboard')
        if start_view not in ('Dashboard', 'Guide', 'Recordings', 'Scheduled', 'Search'):
            raise ValueError('Unknown initial view')
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
        self.view = start_view
        self.positions: dict[str, int] = {}
        self.pool: list[tuple[xbmcgui.ControlImage, xbmcgui.ControlLabel]] = []
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
        self.library_return_mode = "Shows"
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
        self._guide_runtime = GuideRuntime(self)
        self._artwork_ui = ArtworkUI(self)
        self._playback_ui = PlaybackUI(self)
        self._dashboard_ui = DashboardUI(self)
        self._chrome = WindowChrome(self)

    def onInit(self):
        self._dispatcher.post(("initialize", None))

    def onAction(self, action):
        self._dispatcher.post(("action", action.getId()))

    def onClick(self, control_id):
        if control_id == 100:
            self._dispatcher.post(('click', control_id))
        else:
            self._dispatcher.post(('chrome', self._chrome.capture(control_id)))

    def process_pending(self) -> None:
        self._dispatcher.assert_owner()
        if self._closed:
            return
        self._revoke_demo()
        for kind, value in self._dispatcher.dispatch():
            if self._closed:
                break
            if kind == "initialize":
                self._initialize()
            elif kind == "action":
                self._handle_action(value)
            elif kind == "click":
                self._handle_click(value)
            elif kind == 'chrome':
                self._chrome.click(value, xbmcgui)
        if not self._closed:
            changed = self._guide_runtime.poll()
            changed = self._artwork_ui.poll() or changed
            changed = self._dashboard_ui.poll() or changed
            if not self._closed and (changed or self._paint_pending):
                self.render()
        if not self._closed and self.initialized and self._live_session is not None and self.view != 'Dashboard':
            now = monotonic()
            if self._status_tick is None or now - self._status_tick >= 1:
                self._label(114, self._current_data().banner)
                self._status_tick = now
        if not self._closed:
            self._recording_ui.poll()
            self._storage_ui.poll()
            self._playback_ui.poll(xbmcgui)
            if self._series_ui is not None:
                self._series_ui.poll()
            tick_large(self)

    def shutdown(self) -> None:
        self._dispatcher.assert_owner()
        if self._closed:
            return
        self._closed = True
        self._dashboard_ui.close()
        self._playback_ui.close()
        self._artwork_ui.close()
        cancel_large(self, "window closed")
        self._recording_ui.close()
        self._storage_ui.close()
        if self._series_ui is not None:
            self._series_ui.close()
        self._guide_runtime.close()
        self._dispatcher.close()
        super().close()
        self._artwork_ui.detached()

    def close(self):
        self.shutdown()

    def _series_manager_request(self, gui):
        from .series_manager_ui import SeriesManagerUI
        from .pass_ui import _programme_scope
        self._dispatcher.assert_owner()
        if self._series_ui is None:
            self._series_ui = SeriesManagerUI(assert_owner=self._dispatcher.assert_owner,
                dialog=gui.Dialog, profile=self._recording_ui._programme_profile,
                config=self._recording_ui._current_config,
                selection=self._series_selection,
                viewer=lambda heading, body: _programme_scope(self.path, heading, body, self.strings),
                source_scope=lambda: self._recording_ui.preview._control_context()[0],
                strings=self.strings, default_no=gui.DLG_YESNO_NO_BTN)
        self._series_ui.request()

    def _series_selection(self):
        self._dispatcher.assert_owner()
        if (self.view != 'Guide' or self._live_session is None or self._live_session.loading
                or not self._current_data().can_browse):
            return None
        return self.selected_indexed_event()

    def _initialize(self) -> None:
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
                    self._request_live(int(time()))
                else:
                    self._suspend_development()
        self._chrome.initialize_focus()
        self.render()

    def get_setting(self, key: str) -> str | bool:
        if key not in ("server_origin", "server_expected_version", "development_metadata_enabled", "ShowDeveloperTools"):
            raise ValueError("Unknown owned setting")
        if self._addon is None:
            return False if key in ("development_metadata_enabled", "ShowDeveloperTools") else ""
        if key in ("development_metadata_enabled", "ShowDeveloperTools"):
            return self._addon.getSettingBool(key)
        if key == "server_origin":
            return self._addon.getSettingString("server_origin")
        return self._addon.getSettingString("server_expected_version")

    def t(self, key: str, **values: object) -> str:
        return text(self.strings, key, **values)

    @staticmethod
    def utc_now() -> int:
        return int(time())

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

    def _close_demo(self) -> None:
        self._dispatcher.assert_owner()
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
            # Legacy persisted per-build pins are ignored, never rewritten.
            config = DevelopmentConfig(origin, expected_version=BASE_MINIMUM_DVR_BUILD)
            # Only origins produced by the numeric-IP configuration UI are used.
            host = self._config_host(config)
            if DevelopmentConfig.from_host(host).origin != config.origin:
                return None
            return config
        except (MetadataError, ValueError, RuntimeError, AttributeError, TypeError):
            return None

    @staticmethod
    def _config_host(config: DevelopmentConfig) -> str:
        authority = config.origin.removeprefix("http://").removesuffix(":8089")
        return authority[1:-1] if authority.startswith("[") and authority.endswith("]") else authority

    def _configure_development(self) -> None:
        config = self._saved_config()
        try:
            host = xbmcgui.Dialog().numeric(3, self.t('address_ip'),
                                           defaultt=self._config_host(config) if config else "0.0.0.0")
        except (RuntimeError, AttributeError, TypeError, ValueError):
            xbmcgui.Dialog().ok(self.t('connection'), self.t('address_dialog_failed'))
            return
        if not host or self._addon is None:
            return
        try:
            address = DevelopmentConfig.from_host(host)
            config = DevelopmentConfig(address.origin, config.namespace if config else address.namespace,
                                       BASE_MINIMUM_DVR_BUILD)
        except MetadataError:
            xbmcgui.Dialog().ok(self.t('connection'), self.t('address_invalid'))
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
            xbmcgui.Dialog().ok(self.t('connection'), self.t('connection_save_failed'))
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
        self._playback_ui.cancel()
        cancel_large(self, "mode changed")
        self._guide_runtime.mode("live", config)

    def _reset_search(self) -> None:
        self.search_query = ""
        self.search_selection = None
        self._clear_search_cache()

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
            xbmcgui.Dialog().ok(self.t('connection'), self.t('configure_first'))
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
        start, end = self.repository.horizon
        if self._live_session is not None and (self._live_session.loading or not start <= anchor < end):
            if not self._guide_runtime.reenter(anchor):
                self._request_live(anchor)
        else:
            self.guide.jump(anchor)
        self.view = "Guide"

    def _return_synthetic(self) -> None:
        self._open_demo()

    def _development_menu(self) -> None:
        connection_menu(self, xbmcgui)

    def _source_label(self, key: ChannelKey) -> str:
        catalog = self.guide.repository.channels if self.view == "Guide" else self.repository.channels
        return self.guide.projection.update(catalog).source_label(key)

    def _label(self, control_id: int, label: str) -> None:
        self._render_memo.label(self, xbmcgui, control_id, label)

    def _texture(self, name: str) -> str:
        return os.path.join(self.path, "resources", "skins", "Default", "media", name)

    def _ensure_pool(self, count: int) -> None:
        self._visible_count = count
        added: list[xbmcgui.Control] = []
        while len(self.pool) < count:
            image = xbmcgui.ControlImage(0, 0, 1, 1, self._texture("cell.png"))
            label = xbmcgui.ControlLabel(0, 0, 1, 1, "", font="font13",
                                        textColor="FFFFFFFF")
            self.pool.append((image, label))
            added.extend((image, label))
        if added:
            self.addControls(added)
        for image, label in self.pool[count:]:
            self._render_memo.apply(image, "setVisible", False)
            self._render_memo.apply(label, "setVisible", False)

    def _draw(self, index: int, x: int, y: int, width: int, height: int,
              text: str, selected: bool, gap: bool = False) -> None:
        draw(self, index, x, y, width, height, text, selected, gap)

    def _rows(self) -> tuple[Row, ...]:
        if self.view == 'Dashboard':
            return ()
        if self._development_blocked is not None or (self._live_session is None and not self.demo_allowed()):
            return ()
        if self.view == "Search":
            self._search_page()
            return self._search_rows_value
        if self.view == "Scheduled":
            return scheduled_rows(self.repository, self.strings, demo=self.demo_allowed())
        return self.library.rows()

    def _detail_text(self, text: str) -> str:
        # Compatibility helper: provider text is never rewritten by English markers.
        return text

    def _position_key(self) -> str:
        if self.view == "Search":
            return "Search"
        return self.view

    def _current_data(self) -> Presentation[Event] | Presentation[Recording] | Presentation[Job]:
        if self.view == 'Dashboard':
            return Presentation((), DataState.EMPTY, False, False, None, None, '', '', False)
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
            if recordings.can_browse:
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
            self._render()
            self._chrome.render()
            self._artwork_ui.update()
            self._artwork_ui.draw(xbmcgui)
            self._render_memo.finish(self, xbmcgui)
            self._paint_pending = False
        finally:
            self._render_memo.labels = None
            self.metrics.rendered((perf_counter() - started) * 1000,
                                  len(self.pool), self._visible_count)

    def _render(self) -> None:
        if self.view == 'Dashboard':
            self._dashboard_ui.render()
            return
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
        station_rows = (self._live_session is not None and self.view == "Guide" and self.guide.prepared_ready
                        and self._guide_runtime.ready and bool(self.guide.channels)
                        and data.updated_at is not None)
        retained = ((self._live_session is not None or self.demo_allowed())
                    and self.view == "Guide" and self.guide.prepared_ready
                    and not self._guide_runtime.ready
                    and (data.updated_at is not None or (self._live_session is not None
                                                         and self._live_session.loading))
                    and not self._development_blocked)
        if not data.can_browse and not station_rows and not retained:
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
            self._label(112, self.t('empty_view', view=view_label, status=self.t(state)))
            self._label(113, self.t('refresh_hint') if self._live_session is not None else
                        self.t('demo_hint') if self.demo_allowed() else self.t('connection_prompt'))
            return
        if self.view != "Guide":
            rows = self._rows()
            selected = (self.library.position if self.view == "Recordings" else
                        min(self.positions.get(self._position_key(), 0), max(0, len(rows) - 1)))
            first = max(0, selected - 5)
            visible = rows[first:first + 6]
            self._ensure_pool(len(visible))
            for index, row in enumerate(visible):
                self._draw(index, 80, 250 + index * 95, 1760, 85, row.label,
                           first + index == selected)
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
                status = self.t('recording_rows_status', position=selected + 1 if rows else 0, count=len(rows))
                if not rows:
                    status = self.t('recording_no_matches')
                if self.library.mode == "Search" and not normalize_query(self.library.query):
                    status = self.t('recording_search_hint')
                if self.library.truncated or self.library.coverage_limited:
                    status += self.t('results_limited')
                self._label(113, status)
            else:
                self._label(112, self.t('demo_scheduled_readonly' if self.demo_allowed() else 'scheduled_readonly'))
                self._label(113, self.t('details_rows_status', position=selected + 1 if rows else 0, count=len(rows)))
            return
        guide = self.guide
        cells = [cell for cell in guide.cells() if 0 <= cell.row < guide.rows]
        self._ensure_pool(len(cells))
        self._label(112, self.t('guide_range', group=group_label(self.strings, guide.group),
                               start=time_label(guide.viewport), end=time_label(guide.viewport + guide.span)))
        for tick in range(4):
            self._label(140 + tick, time_label(guide.viewport + tick * 1800).split(" ")[-2])
        for index, channel in enumerate(guide.channels[guide.top:guide.top + guide.rows]):
            self._label(120 + index, self.t('channel_row', number=channel.number, name=channel.name,
                                          source=self._source_label(channel.key)))
        for index, cell in enumerate(cells):
            # Alternating overlap lanes expose overlapping metadata without
            # interpreting it as a recording or silently merging the sources.
            text = ("< " if cell.continues_left else "") + cell.title
            if self._live_session is not None and cell.event_id is None:
                cell_channel = guide.channel(cell.channel)
                if cell_channel is not None:
                    text = station_label(cell_channel, text, self.strings)
            text += " >" if cell.continues_right else ""
            self._draw(index, 390 + round(cell.x * 1450),
                       250 + cell.row * 95 + cell.lane * 39,
                       max(1, round(cell.width * 1450)), 43, text,
                       cell.selected, cell.event_id is None)
        event = guide.selected_event()
        if retained:
            self._label(113, self.t('guide_retained_status', status=self.t('guide_unavailable' if self._guide_runtime.error else 'preparing_guide')))
            return
        self._label(113, self.t('guide_selection', title=(event.title if event else
                          (station_label(guide.channel(guide.selection.channel), strings=self.strings) if self._live_session is not None else "")
                          or self.t('no_listing_selected')), time=time_label(guide.selection.anchor))
                     + (self.t('some_listings_hidden') if guide.truncated else ''))

    def _details(self, allow_search_action: bool = True) -> None:
        if self.view == 'Dashboard':
            return
        data = self._current_data()
        if (self._live_session is not None and self._development_blocked is None
                and self.view == "Guide" and self._guide_runtime.ready
                and self.guide.selected_event() is None):
            channel = self.guide.channel(self.guide.selection.channel)
            if show_station_gap(self, channel, xbmcgui.Dialog()):
                return
        if not data.can_browse:
            xbmcgui.Dialog().ok(self.t('demo_status') if self.demo_allowed() else self.t('connection'), data.message)
            return
        if self.view == "Search":
            event = self._selected_search_event()
            if event is None:
                xbmcgui.Dialog().ok(self.t('search_programmes'), self.t('search_select_result'))
                return
            if allow_search_action:
                entries = (('result.details', 'event_details'), ('result.guide', 'show_in_guide'))
                choice = select(xbmcgui.Dialog(), self.strings, 'search_result', entries)
                if choice is None:
                    return
                if choice == 'result.guide':
                    if self.selected_indexed_event(event) is not None and self.guide.focus_event(event):
                        self.view = "Guide"
                    else:
                        xbmcgui.Dialog().ok(self.t('search_programmes'), self.t('search_airing_gone'))
                    self.render()
                    return
            if self.selected_indexed_event(event) is not None:
                self._show_event_details(event)
        elif self.view == "Guide":
            event = self.selected_indexed_event()
            if event is None:
                xbmcgui.Dialog().ok(self.t('no_listing'), self.t('temporal_gap'))
                return
            self._show_event_details(event)
        elif self.view == "Recordings":
            row = self.library.selected
            if row is None:
                xbmcgui.Dialog().ok(self.t('recordings'), self.t('recording_select_item'))
                return
            if not (allow_search_action and self.library.open_selected()):
                if not self._artwork_ui.ready() or show_media_details(self, row) is not True:
                    xbmcgui.Dialog().textviewer(row.label, self._detail_text(row.detail))
        else:
            rows = self._rows()
            if not rows:
                return
            index = min(self.positions.get(self._position_key(), 0), len(rows) - 1)
            row = rows[index]
            if not self._artwork_ui.ready() or show_media_details(self, row) is not True:
                xbmcgui.Dialog().textviewer(row.label, row.detail)
        self.render()

    def selected_indexed_event(self, event: Event | None = None) -> Event | None:
        return self._guide_runtime.selected(event)

    def _show_event_details(self, event: Event) -> None:
        show_event_details(self, event, xbmcgui.Dialog())

    def _set_search_query(self, text: str) -> None:
        normalized = normalize_query(text)
        if normalized != normalize_query(self.search_query):
            self.search_selection = None
            self.positions["Search"] = 0
        # Keep original input: Unicode normalization may expand 128 characters.
        self.search_query = text
        self.view = "Search"

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

    def _set_library_mode(self, mode: str) -> None:
        if mode == "Search" and self.library.mode != "Search":
            self.library_return_mode = self.library.mode
        self.library.set_mode(mode)

    def _recording_search_menu(self) -> None:
        search_menu(self, xbmcgui, recordings=True)

    def _recording_tools(self) -> None:
        recording_tools(self, xbmcgui)

    def _menu(self) -> None:
        self._playback_ui.cancel()
        main_menu(self, xbmcgui)

    def _diagnostics(self) -> None:
        if not self.developer_enabled():
            return
        xbmcgui.Dialog().textviewer(self.t('timing'),
            self.t('diagnostic_summary', metrics=self.metrics.compact_report(self.strings),
                   exercise=self.exercise_summary or self.t('exercise_none'), stress=self.stress_fixture))

    def _time_navigation(self) -> None:
        time_navigation(self, xbmcgui)

    def _toggle_stress_fixture(self) -> None:
        if not self.demo_allowed():
            return
        install_fixture(self, "normal" if self.stress_fixture else "stress")

    def _navigate(self, action_id: int) -> None:
        self._playback_ui.cancel()
        started = perf_counter()
        try:
            data = self._current_data()
            if self._development_blocked is not None:
                self.render()
                return
            live_guide = self._live_session is not None and self.view == "Guide"
            if not data.can_browse:
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
                else:
                    self.guide.vertical(-1 if action_id == 3 else 1)
            elif self.view == "Recordings":
                if action_id in (3, 4):
                    self.library.move(-1 if action_id == 3 else 1)
                else:
                    modes = ("Shows", "Recent", "Movies", "Search")
                    self._set_library_mode(modes[(modes.index(self.library.mode)
                                                  + (-1 if action_id == 1 else 1)) % 4])
            elif action_id in (3, 4):
                key = self._position_key()
                self.positions[key] = max(0, min(len(self._rows()) - 1,
                    self.positions.get(key, 0) + (-1 if action_id == 3 else 1)))
                if self.view == "Search":
                    selected = self._selected_search_event()
                    self.search_selection = airing_key(selected) if selected else None
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
        self.view = "Guide"
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

    def _handle_action(self, action_id):
        if action_id in (10, 92) and self._guide_runtime.cancel_fixture():
            self.render()
            return
        if self._large_exercise is not None:
            cancel_large(self, "input")
            if action_id in (10, 92, xbmcgui.ACTION_CONTEXT_MENU):
                return
        if self._chrome.action(action_id):
            return
        if action_id == xbmcgui.ACTION_CONTEXT_MENU:
            self._menu()
        elif action_id == 11:
            origin = self._chrome.focus()
            self._details(allow_search_action=False)
            self._chrome.restore(origin)
        elif action_id in (1, 2, 3, 4):
            self._navigate(action_id)

    def _handle_click(self, control_id):
        if control_id == 100:
            cancel_large(self, "click")
            if self.view != 'Dashboard':
                origin = self._chrome.focus()
                self._details()
                self._chrome.restore(origin)


def open_window(addon_path: str, addon: SettingsProtocol | None = None, *, start_view='Dashboard') -> None:
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
