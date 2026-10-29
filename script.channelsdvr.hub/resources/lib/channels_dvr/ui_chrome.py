"""Visible native buttons; owner resolves focus, callbacks post plain input only."""
from .guide_render import RenderMemo
from .ui_navigation import execute_command
from .ui_activation import ButtonActivation
from types import MappingProxyType
from collections.abc import Mapping

NAV = {200: ('view.dashboard', 'dashboard'), 201: ('view.guide', 'guide'),
       202: ('view.recordings', 'recordings'), 203: ('view.scheduled', 'scheduled'),
       204: ('series.manager', 'passes'), 205: ('view.search', 'search'),
       206: ('connection.menu', 'connection')}
ACTIONS = {300: ('selection.details', 'details'), 301: ('playback.play', 'play_short'),
           302: ('recording.schedule', 'schedule_short'), 303: ('series.manager', 'passes'),
           304: ('window.actions', 'actions'), 305: ('window.back', 'back'),
           306: ('search.programmes', 'search'), 307: ('search.clear', 'search_clear'),
           308: ('connection.configure', 'dashboard_configure'), 309: ('connection.connect', 'connect'),
           310: ('dashboard.refresh', 'refresh')}


class WindowChrome:
    def __init__(self, window):
        self.w = window
        self.memo = RenderMemo()
        self.presented: Mapping[int, ButtonActivation] = MappingProxyType({})
        self.layout = object()

    def reset(self):
        self.memo.reset()
        self.presented = MappingProxyType({})
        self.layout = object()

    def capture(self, ident):
        # Callback reads one immutable owner-published map, never model/SDK state.
        return self.presented.get(ident)

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

    def _activation(self, ident: int, entry: tuple[str, str]):
        w = self.w
        session = w._live_session
        scope: tuple = (self.layout, w._development_config,
                 session.incarnation if session is not None else w._guide_runtime.incarnation,
                 w._development_blocked, w._guide_runtime._mode)
        if ident in NAV:
            # Stable navigation is independent of data/stats/clock repaint.
            return ButtonActivation(ident, entry[0], entry[1], scope)
        scope += (w.view,)
        if w.view == 'Recordings':
            library = w.library
            scope += (library._context(), library._paths.get(library._context(), ()),)
        if ident not in (300, 301, 302):
            return ButtonActivation(ident, entry[0], entry[1], scope)
        data = w._current_data()
        selection: tuple
        if w.view == 'Recordings':
            row = w.library.selected
            selection = (row.id if row else None, w.library.selected_recording())
        elif w.view == 'Guide':
            selection = (w.guide.selection, w.guide.group)
        else:
            selection = (w.positions.get(w._position_key(), 0), w.search_selection, w.search_query, w.guide.group)
        product = w.guide.current_product
        snapshot = (id(data.items), data.updated_at, w._guide_runtime.revision,
                    product.source.ticket if product is not None else None,
                    session._generation if session is not None else None,
                    session._catalog_epoch if session is not None else w._guide_runtime.synthetic_owner)
        return ButtonActivation(ident, entry[0], entry[1], scope + (selection,), snapshot, id(w.repository.channels))

    def focus(self):
        """Known parent control only; SDK uncertainty is never content100."""
        self.w._dispatcher.assert_owner()
        try:
            ident = self.w.getFocusId()
        except Exception:
            return None
        return ident if type(ident) is int and ident in (100, *NAV, *ACTIONS) else None

    def _set_focus(self, target, *, initial=False):
        """One bounded explicit focus attempt, never recursive repair/input."""
        self.w._dispatcher.assert_owner()
        if (self.w._closed or type(target) is not int
                or (target != 100 and target not in self.presented
                    and not (initial and target == 201))):
            return False
        try:
            self.w.setFocusId(target)
            return True
        except Exception:
            return False

    def initialize_focus(self):
        # onInit is an explicit owned lifecycle, not passive paint recovery.
        self._set_focus(201 if self.w.view == 'Dashboard' else 100, initial=True)

    def available(self):
        w = self.w
        buttons = dict(NAV)
        buttons.update({304: ACTIONS[304], 305: ACTIONS[305]})
        if w.view == 'Dashboard':
            buttons.update({i: ACTIONS[i] for i in (308, 309, 310)})
            return buttons
        if w.view == 'Search':
            buttons.update({i: ACTIONS[i] for i in (306, 307)})
        data = w._current_data()
        if w.view == 'Guide':
            if data.can_browse or (w._guide_runtime.ready and w.guide.channel(w.guide.selection.channel)):
                buttons[300] = ACTIONS[300]
            event = w.selected_indexed_event()
            if event is not None and event.start > w.utc_now() and data.can_browse and not data.loading:
                buttons[302] = ACTIONS[302]
            buttons[303] = ACTIONS[303]
            if data.can_browse and not data.loading and w._guide_runtime.ready:
                buttons[301] = ('playback.play', 'watch_short')
        elif w.view == 'Recordings':
            row = w.library.selected
            if row is not None and data.can_browse:
                buttons[300] = ('selection.details', 'open' if row.children else 'details')
                if not row.children and w.library.selected_recording() is not None:
                    buttons[301] = ACTIONS[301]
        elif w.view in ('Scheduled', 'Search') and data.can_browse and w._rows():
            buttons[300] = ACTIONS[300]
            if w.view == 'Search':
                buttons[302] = ('search.guide', 'show_in_guide')
        return buttons

    def restore(self, origin):
        if self.w._closed or type(origin) is not int or origin not in (100, *NAV, *ACTIONS):
            return
        current = self.focus()
        if current is None:
            # No proof of parent focus: defer rather than steal child/modal focus.
            return
        if origin == 100 and self.w.view != 'Dashboard':
            if current != 100:
                self._set_focus(100)
            return
        valid = self.presented
        target = origin if origin in valid else next(iter(valid), None)
        if target is not None and current != target:
            self._set_focus(target)

    def render(self):
        w = self.w
        available = self.available()
        for ident in (*NAV, *ACTIONS):
            control = self.control(ident)
            if control is None:
                self._withdraw(ident)
                continue
            shown = ident in available
            required: list[tuple[str, tuple]] = [('setVisible', (shown,)), ('setEnabled', (shown,))]
            if shown:
                if ident in ACTIONS:
                    position = [i for i in ACTIONS if i in available].index(ident)
                    required.append(('setPosition', (80+position*290, 1000)))
                key = available[ident][1]
                required.append(('setLabel', (w.t(key),)))
            if not shown or any(self.memo.values.get((control, method)) != args for method, args in required):
                self._withdraw(ident)  # BEFORE potentially reentrant SDK setters
            committed = True
            for method, args in required:
                committed = self.memo.best_effort(control, method, *args) and committed
            if shown and committed:
                current = dict(self.presented)
                current[ident] = self._activation(ident, available[ident])
                self.presented = MappingProxyType(current)
                active = available[ident][0] == 'view.'+w.view.lower()
                self.memo.best_effort(control, 'setTextColor', 'FFDEB560' if active else 'FFFFFFFF')
            else:
                self._withdraw(ident)
                if shown:
                    self.memo.best_effort(control, 'setEnabled', False)
                    self.memo.best_effort(control, 'setVisible', False)
        for ident in range(400, 406):
            control = self.control(ident)
            if control is not None:
                self.memo.best_effort(control, 'setVisible', w.view == 'Dashboard' and ident in w._dashboard_ui.ready_cards)
        # Only repair revoked controls; passive metadata/art/status never steals focus.
        focus = self.focus()
        if focus in (*NAV, *ACTIONS) and focus not in self.presented:
            self.restore(304)

    def click(self, activation, gui):
        if type(activation) is not ButtonActivation:
            return False
        ident = activation.control_id
        entry = self.available().get(ident)
        if (entry is None or not activation.matches(self.presented.get(ident))
                or not activation.matches(self._activation(ident, entry))):
            return False
        command = activation.command
        if command == 'window.back':
            self.back(content=False)
        elif command == 'selection.details':
            self.w._details()
            self.restore(ident)
        else:
            execute_command(self.w, gui, command, origin=ident)
        return True

    def back(self, *, content):
        w = self.w
        if w.view == 'Dashboard':
            w.close()
        elif content:
            target = next((i for i in sorted(self.presented) if i >= 300), None)
            if target is not None:
                self._set_focus(target)
        elif w.view == 'Recordings' and w.library.back():
            w.render()
            self.restore(305)
        elif w.view == 'Recordings' and w.library.mode == 'Search':
            w._set_library_mode(w.library_return_mode)
            w.render()
            self.restore(305)
        else:
            w.view = 'Dashboard'
            w._guide_runtime.invalidate_view()
            w._playback_ui.cancel()
            w.render()
            self.restore(201)

    def action(self, action):
        focus = self.focus()
        if focus is None:
            # Consume/defer focus-dependent input. In particular, do not let
            # directions fall through to the Guide behind an unknown overlay.
            return True
        if action in (10, 92):
            if focus in NAV and self.w.view != 'Dashboard':
                self.w.view = 'Dashboard'
                self.w._guide_runtime.invalidate_view()
                self.w.render()
                self.restore(201)
            else:
                self.back(content=focus == 100)
            return True
        if action not in (1, 2, 3, 4) or focus == 100:
            return False
        available = self.presented
        row = sorted(i for i in available if (i in NAV) == (focus in NAV))
        if focus not in row:
            return True
        if action in (1, 2):
            target = row[(row.index(focus)+(-1 if action == 1 else 1)) % len(row)]
        elif focus in NAV:
            target = next((i for i in sorted(available) if i >= 300), focus) if action == 3 or self.w.view == 'Dashboard' else 100
        else:
            target = 100 if action == 3 and self.w.view != 'Dashboard' else next((i for i in sorted(available) if i in NAV), focus)
        self.w._playback_ui.cancel()
        self._set_focus(target)
        return True
