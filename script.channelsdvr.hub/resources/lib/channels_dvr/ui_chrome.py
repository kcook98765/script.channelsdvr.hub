"""Native navigation regions and immutable input publication for one owner."""
from collections.abc import Mapping
from types import MappingProxyType

from .dvr_ui import TABS as DVR_TABS
from .guide_render import RenderMemo
from .ui_activation import ButtonActivation, ShortcutActivation
from .ui_hits import ContentHits
from .ui_navigation import DESTINATIONS, SELECTION_COMMANDS, command_label, execute_command
from .ui_recordings import ACTION_SLOTS as RECORDINGS_ACTIONS, SOURCE, TABS as RECORDINGS_TABS, TOOLS
from .ui_recordings import active_tab as recordings_active_tab
from .ui_search import TABS as SEARCH_TABS

CONTENT = 100
NAV = dict(zip(range(200, 206), DESTINATIONS))
TABS = {**RECORDINGS_TABS, **{ident: code for ident, (code, _) in DVR_TABS.items()}}
ACTIONS = {
    300: ('selection.open',), 301: ('playback.play',),
    302: ('recording.schedule', 'search.reveal'), 303: ('series.manager',),
    304: ('window.actions',), 305: ('window.back',),
    306: ('search.query',), 307: ('search.clear',),
    308: ('connection.configure',), 309: ('connection.connect',), 310: ('dashboard.refresh',),
    **TOOLS,
}
VIEW_ACTIONS = {('Guide', 306): ('guide.collection',), ('Guide', 307): ('guide.time',),
                ('Guide', 308): ('guide.now',), ('DVR', 303): ('dvr.manage',),
                ('DVR', 306): ('dvr.refresh',), ('Search', 304): ('search.coverage',)}
# Kodi Omega ActionIDs; Select is delivered by native onClick (including mouse).
LEFT, RIGHT, UP, DOWN, PAGE_UP, PAGE_DOWN = 1, 2, 3, 4, 5, 6
BACK, INFO, NAV_BACK, CONTEXT = 10, 11, 92, 117
WHEEL_UP, WHEEL_DOWN = 104, 105
SHORTCUTS = {INFO: 'selection.info', CONTEXT: 'window.actions',
             PAGE_UP: 'selection.page-up', PAGE_DOWN: 'selection.page-down'}


class WindowChrome:
    def __init__(self, window):
        self.w = window
        self.memo = RenderMemo()
        self.presented: Mapping[int, ButtonActivation] = MappingProxyType({})
        self.layout = object()
        self.hits = ContentHits(window)
        self.selection: ButtonActivation | None = None

    def reset(self):
        self.memo.reset()
        self.presented = MappingProxyType({})
        self.selection = None
        self.layout = object()
        self.hits.clear()

    def capture(self, ident):
        # Callbacks read already-published immutable values, never model/SDK state.
        return self.presented.get(ident)

    def capture_action(self, action):
        return ShortcutActivation(action, self.selection) if action in (INFO, CONTEXT) else action

    def _withdraw(self, ident):
        if ident in self.presented:
            current = dict(self.presented)
            current.pop(ident)
            self.presented = MappingProxyType(current)

    def control(self, ident):
        try:
            control = self.memo.controls.get(ident)
            if control is None:
                control = self.w.getControl(ident)
                if control is not None:
                    self.memo.controls[ident] = control
            return control
        except Exception:
            return None

    def _scope(self):
        w = self.w
        session = w._live_session
        scope: tuple = (self.layout, w._route, w._development_config,
                session.incarnation if session is not None else w._guide_runtime.incarnation,
                w._development_blocked, w._guide_runtime._mode)
        if w.view == 'Recordings':
            scope += (w.library.navigation_scope,)
        elif w.view == 'Search':
            scope += (w._search_ui.category, w.search_query, w._guide_runtime.revision,
                      getattr(session, '_generation', None), getattr(w.data_session, '_revision', None))
        return scope

    def _activation(self, ident, code, label, target=None):
        w = self.w
        scope = self._hit_scope() if target is not None else self._scope()
        session = w._live_session
        if w.view in ('Dashboard', 'Settings') or (code not in SELECTION_COMMANDS and target is None
                                                   and code != 'selection.anchor'):
            if w.view == 'Settings' and (code in SELECTION_COMMANDS or code == 'selection.anchor'):
                scope += (w.positions.get('Settings', 0),)
            return ButtonActivation(ident, code, label, scope, target=target)
        if w.view == 'Search':
            w._search_ui.page()
            if target is None:
                scope += (w.search_selection,)
            return ButtonActivation(ident, code, label, scope, (w._search_ui.publication,), target=target)
        data = w._current_data()
        if w.view == 'DVR' and w._dvr.passes:
            if target is None:
                scope += w._dvr.selection_identity(data)
            return ButtonActivation(ident, code, label, scope, w._dvr.snapshot(data), target=target)
        if target is None:
            if w.view == 'Recordings':
                row = w.library.selected
                scope += (row.id if row else None,)
            elif w.view == 'Guide':
                scope += (w.guide.selection, w.guide.group)
            else:
                scope += (w._dvr_selected,)
        product = getattr(w.guide, 'current_product', None) if w.view == 'Guide' else None
        snapshot = (id(data.items), data.updated_at, data.can_browse, data.loading, w._guide_runtime.revision,
                    product.source.ticket if product is not None else None,
                    session._generation if session is not None else None,
                    session._catalog_epoch if session is not None else w._guide_runtime.synthetic_owner)
        return ButtonActivation(ident, code, label, scope, snapshot, id(w.repository.channels), target)

    def _hit_scope(self):
        w = self.w
        scope = self._scope()
        if w.view == 'Guide':
            scope += (w.guide.group, w.guide.top, w.guide.viewport)
        return scope

    def command_activation(self, code, label):
        return self._activation(0, code, label)

    def command_current(self, activation):
        label = command_label(self.w, activation.command)
        return label is not None and activation.matches(self.command_activation(activation.command, label))

    def focus(self):
        self.w._dispatcher.assert_owner()
        try:
            ident = self.w.getFocusId()
        except Exception:
            return None
        if type(ident) is not int:
            return None
        return ident if ident in (CONTENT, *NAV, *TABS, *ACTIONS) else CONTENT if ident in self.presented else None

    def _set_focus(self, target):
        self.w._dispatcher.assert_owner()
        if self.w._closed or (target != CONTENT and target not in self.presented):
            return False
        try:
            current = self.w.getFocusId()
        except Exception:
            current = None
        try:
            if current != target:
                self.w.setFocusId(target)
                return True
        except Exception:
            pass  # Unknown native/child focus is not permission for retries.
        return False

    def _travel_focus(self, target):
        if target is not None and self._set_focus(target):
            hub = getattr(self.w, '_hub_session', None)
            if hub is not None:
                hub.cancel_startup()

    def entry_target(self, preferred=None):
        if preferred in self.presented:
            return preferred
        if CONTENT in self.presented:
            return CONTENT
        if self.w.view == 'DVR' and (tab := self._active_tab()) in self.presented:
            return tab
        if self.w.view == 'Guide' and 304 in self.presented:
            return 304  # Search/Settings remain reachable without a channel.
        active = next((i for i in NAV if i in self.presented and DESTINATIONS[NAV[i]] == self.w.view), None)
        return active if active is not None else next(iter(self.presented), None)

    def _active_tab(self):
        if self.w.view == 'DVR':
            return next((ident for ident, (code, _) in DVR_TABS.items() if code == self.w._dvr.tab), None)
        if self.w.view == 'Recordings':
            return recordings_active_tab(self.w)
        if self.w.view == 'Search':
            return self.w._search_ui.active_tab()
        return None

    def initialize_focus(self, preferred=None):
        target = self.entry_target(preferred)
        if target is not None:
            self._set_focus(target)

    def restore(self, origin):
        if not self.w._closed and self.focus() is not None:
            self.initialize_focus(origin)

    def available(self):
        w = self.w
        visible = {ident: (code, label) for ident, code in NAV.items() if w.view != 'Guide'
                   if (label := command_label(w, code)) is not None}
        tabs = SEARCH_TABS if w.view == 'Search' else TABS
        visible.update({ident: (code, label) for ident, code in tabs.items()
                        if (label := command_label(w, code)) is not None})
        slots = {
            'Dashboard': (304, 305, 308, 309, 310),
            'Guide': (306, 307, 308, 300, 304, 305),
            'Recordings': RECORDINGS_ACTIONS,
            'DVR': (300, 303, 306, 304, 305),
            'Search': (306, 307, 300, 302, 301, 304, 305),
            'Settings': (300, 304, 305),
        }[w.view]
        for ident in slots:
            for code in VIEW_ACTIONS.get((w.view, ident), ACTIONS[ident]):
                label = command_label(w, code)
                if label is not None:
                    visible[ident] = code, label
                    break
        return visible

    def render(self, gui=None, plan=()):
        w = self.w
        available = self.available()
        rail = RECORDINGS_ACTIONS if w.view == 'Recordings' else tuple(ident for ident in available if ident in ACTIONS)
        spacing, width = (290, 280) if len(rail) <= 6 else (252, 242)
        active_tab = self._active_tab()
        for ident in (*NAV, *TABS, *ACTIONS):
            control = self.control(ident)
            if control is None:
                self._withdraw(ident)
                available.pop(ident, None)
                continue
            shown = ident in available
            required: list[tuple[str, tuple]] = [('setVisible', (shown,)), ('setEnabled', (shown,))]
            if shown:
                if ident in ACTIONS:
                    required.extend((('setPosition', (80 + rail.index(ident)*spacing, 1000)), ('setWidth', (width,))))
                label = w.t(available[ident][1])
                if ident == active_tab:
                    label = w.t('recordings_active_tab', tab=label)  # Shared ASCII active-tab marker.
                required.append(('setLabel', (label,)))
            if not shown or any(self.memo.values.get((control, method)) != args for method, args in required):
                self._withdraw(ident)
            committed = True
            for method, args in required:
                committed = self.memo.best_effort(control, method, *args) and committed
            if shown and committed:
                active = NAV.get(ident) == 'view.' + w.view.lower() or ident == active_tab
                self.memo.best_effort(control, 'setTextColor', 'FFDEB560' if active else 'FFFFFFFF')
            else:
                available.pop(ident, None)
                if shown:
                    self.memo.best_effort(control, 'setEnabled', False)
                    self.memo.best_effort(control, 'setVisible', False)
        for ident in range(400, 406):
            control = self.control(ident)
            if control is not None:
                self.memo.best_effort(control, 'setVisible', w.view == 'Dashboard' and ident in w._dashboard_ui.ready_cards)
        state = {ident: self._activation(ident, *entry) for ident, entry in available.items()}
        if (label := command_label(w, 'selection.activate')) is not None:
            state[CONTENT] = self._activation(CONTENT, 'selection.activate', label)
        elif w.view == 'Search' and w._rows():
            # Stale rows remain focusable and pageable. This command is not
            # registered for dispatch, even if the row later becomes current.
            state[CONTENT] = self._activation(CONTENT, 'selection.focus', '')
        anchor = self._activation(0, 'selection.anchor', '')
        # Withdraw before SDK removals; do not let an old ID resolve to a new row.
        self.presented = MappingProxyType(dict(state))
        if gui is not None:
            scope = self._hit_scope() if plan else self._scope()
            for ident, hit in self.hits.sync(gui, (*scope, anchor.snapshot, anchor.channels), plan):
                state[ident] = ButtonActivation(ident, 'selection.hit', '', scope, anchor.snapshot, anchor.channels, hit.key)
        self.presented = MappingProxyType(state)
        self.selection = anchor
        # Passive paint never sets focus (even if a control just disappeared).

    def click(self, activation, gui):
        if (self.w._closed or self.focus() is None or type(activation) is not ButtonActivation
                or not activation.matches(self.presented.get(activation.control_id))):
            return False
        ident, code = activation.control_id, activation.command
        if code == 'selection.hit':
            if (activation.target is None or
                    not activation.matches(self._activation(ident, code, '', activation.target))
                    or not self.w._select_hit(activation.target)):
                return False
            if self.w.view == 'Guide':
                self.w.render()  # Rebind preview to the clicked airing before opening details.
            self._set_focus(CONTENT)
            # This is intentionally a new selection. Only normal read/open is
            # dispatched, never the former selection's toolbar intent.
            return execute_command(self.w, gui, 'selection.open')
        label = command_label(self.w, code)
        if label is None or not activation.matches(self._activation(ident, code, label)):
            return False
        return execute_command(self.w, gui, code, origin=ident)

    def action(self, activation, gui=None):
        action = activation.action if type(activation) is ShortcutActivation else activation
        if action in (BACK, NAV_BACK):
            return execute_command(self.w, gui, 'window.back')
        focus = self.focus()
        if action in (WHEEL_UP, WHEEL_DOWN) and self.w.view in ('Guide', 'Recordings', 'Search'):
            # Scroll content, not the focus rail. Continued wheel travel can
            # outlive the hovered hit control when a viewport is repainted.
            if CONTENT in self.presented and focus in (CONTENT, None):
                self.w._navigate(UP if action == WHEEL_UP else DOWN)
            return True
        if focus is None:
            # Explicit travel can recover native focus after a hovered hit was
            # retired; a passive repaint and a stale Select cannot.
            if action in (LEFT, RIGHT, UP, DOWN):
                self._travel_focus(self.entry_target())
            return True
        if action in SHORTCUTS:
            if action in (INFO, CONTEXT):
                anchor = activation.selection if type(activation) is ShortcutActivation else None
                if anchor is None or not anchor.matches(self._activation(0, 'selection.anchor', '')):
                    return True
            if action not in (PAGE_UP, PAGE_DOWN) or focus == CONTENT:
                execute_command(self.w, gui, SHORTCUTS[action], origin=focus)
            return True
        if action not in (LEFT, RIGHT, UP, DOWN):
            return False
        if focus not in self.presented:
            self._travel_focus(self.entry_target())
            return True
        nav = tuple(i for i in self.presented if i in NAV)
        tabs = tuple(i for i in self.presented if i in TABS)
        rail = tuple(i for i in self.presented if i in ACTIONS)
        active = next((i for i in nav if DESTINATIONS[NAV[i]] == self.w.view), CONTENT)
        category = self._active_tab() if tabs else active
        if category not in tabs:
            category = tabs[0] if tabs else active
        if focus in nav or focus in tabs or focus in rail:
            siblings = nav if focus in nav else tabs if focus in tabs else rail
            if action in (LEFT, RIGHT):
                target = siblings[(siblings.index(focus) + (-1 if action == LEFT else 1)) % len(siblings)]
            else:
                if focus in nav and tabs and action == DOWN:
                    target = category
                elif focus in nav:
                    target = CONTENT if CONTENT in self.presented else rail[0] if rail and action == DOWN else focus
                elif focus in tabs:
                    target = active if action == UP else CONTENT if CONTENT in self.presented else rail[0] if rail else focus
                elif self.w.view == 'DVR' and action == DOWN:
                    target = active
                else:
                    target = CONTENT if action == UP and CONTENT in self.presented else category if nav else focus
        elif tabs and action in (LEFT, RIGHT):
            # Reach tabs and local selectors from any page without traversing
            # a large catalogue or changing the content cursor behind focus.
            target = SOURCE if action == RIGHT and SOURCE in rail else category
        elif action == UP and nav and self.w._content_edge(UP):
            target = category
        elif action == DOWN and rail and self.w._content_edge(DOWN):
            target = rail[0]
        else:
            return False
        self.w._playback_ui.cancel()
        self._travel_focus(target)
        return True
