"""Bounded native hit controls over the Hub's existing drawn content.

The target never changes underneath a control ID. Replace moved/rebound hits
instead of recycling a visible slot; delayed callbacks for removed IDs are inert.
"""
from dataclasses import dataclass
from .guide import GuideController


@dataclass(frozen=True)
class Hit:
    rect: tuple[int, int, int, int]
    key: tuple


class ContentHits:
    def __init__(self, window):
        self.window = window
        self.controls = {}
        self.scope = None
        self.plan = ()
        self.last_id = 0
        self.disabled = False
        self._retired = []

    def clear(self):
        controls, self.controls = self.controls, {}
        retired, self._retired = self._retired, []
        self.plan, self.scope = (), None
        for control in retired:
            self._remove_control(control)
        for _, control in controls.values():
            self._remove_control(control)

    def _remove_control(self, control):
        try:
            self.window.removeControl(control)
        except Exception:
            self.disabled = True  # a broken removal must not grow the overlay pool
            self._retired.append(control)  # retry on reset/shutdown, not each idle paint
            for method in ('setEnabled', 'setVisible'):
                try:
                    getattr(control, method)(False)
                except Exception:
                    pass  # Unpublished controls can never dispatch.

    def sync(self, gui, scope, plan):
        if self.disabled:
            return ()
        plan = plan[:GuideController.max_cells]
        if scope != self.scope:
            self.clear()
        if self.disabled:
            return ()
        if scope != self.scope or plan != self.plan:
            self.scope, self.plan = scope, plan
            wanted = set(plan)
            for hit in tuple(self.controls):
                if hit not in wanted:
                    _, control = self.controls.pop(hit)
                    self._remove_control(control)
            for hit in plan:
                if self.disabled:
                    break
                if hit in self.controls:
                    continue
                # Non-empty missing textures prevent skin-provided button art
                # from covering the separately rendered guide cells/labels.
                button = None
                try:
                    button = gui.ControlButton(*hit.rect, '', focusTexture='-', noFocusTexture='-')
                    self.window.addControl(button)
                    ident = button.getId()
                    if type(ident) is not int or ident <= self.last_id or ident < 1000:
                        # Never accept a recycled Python control ID as a new hit.
                        self._remove_control(button)
                        continue
                    self.last_id = ident
                    button.setNavigation(button, button, button, button)
                    self.controls[hit] = ident, button
                except Exception:
                    if button is not None:
                        self._remove_control(button)
        if self.disabled:
            self.clear()
            return ()
        return tuple((ident, hit) for hit, (ident, _) in self.controls.items())
