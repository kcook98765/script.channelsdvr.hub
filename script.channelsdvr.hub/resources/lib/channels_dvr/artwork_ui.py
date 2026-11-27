"""Owner-only URL assignment to fitted Kodi images; no Python artwork I/O/worker.

An assigned reference is not a delivered/decoded image. Native asynchronous failure
cannot reliably trigger candidate fallback or text reflow through this SDK API.
"""
from .artwork import ArtKey, channel_key, event_key
from .artwork_refs import first_reference
from .guide_render import ROW_PITCH, ROW_TOP, RenderMemo
from .ui_text import text


class ArtworkUI:
    def __init__(self, window):
        self.window = window
        self._binding = None
        self._refs = {}
        self._selected = None
        self._logos: tuple[ArtKey, ...] = ()
        self._images = []
        self._preview = None
        self._memo = RenderMemo()
        self._assigned = {}
        self._disabled = False
        self._missing = 0

    def reset(self):
        self._memo.reset()
        self._assigned.clear()

    def _source(self):
        w = self.window
        session, config = w._live_session, w._development_config
        if (self._disabled or w._closed or session is None or config is None or session.loading
                or w._development_blocked or w._guide_runtime._mode is not None
                or (w.view == 'Guide' and not w._guide_runtime.ready)):
            return None
        # Command/ownership generation is deliberately NOT an artwork revision.
        return session, session.artwork, config, w.view

    def _selection(self, inventory):
        w = self.window
        if w.view == 'Guide' and w._guide_runtime.ready:
            event = w.guide.selected_event()
            return event_key(event) if event is not None else None
        if w.view == 'Search':
            event = w._selected_search_event()
            return event_key(event) if event is not None else None
        if w.view in ('Recordings', 'DVR'):
            rows = w._rows()
            position = w.library.position if w.view == 'Recordings' else w.positions.get(w._position_key(), 0)
            row = rows[min(position, len(rows)-1)] if rows else None
            return inventory.rows.get(('Scheduled' if w.view == 'DVR' else w.view, row.id)) if row is not None and not row.children else None
        return None

    def _try_apply(self, control, method, *args):
        try:
            self._memo.apply(control, method, *args)
            return True
        except Exception:
            return False

    def _hide(self):
        for image in [*self._images, self._preview]:
            if image is not None:
                hidden = self._try_apply(image, 'setVisible', False)
                cleared = self._try_apply(image, 'setImage', '')
                if not hidden or not cleared:
                    self._disabled = True
        self._assigned.clear()
        self._refs = {}

    def _disable(self):
        self._disabled = True
        self._hide()
        self._restore_geometry()

    def _control(self, ident):
        try:
            if ident not in self._memo.controls:
                self._memo.controls[ident] = self.window.getControl(ident)
            return self._memo.controls[ident]
        except Exception:
            return None

    def _station_geometry(self, index, fitted):
        for first, offset in ((120, 9), (126, 43)):
            control = self._control(first + index)
            if control is not None:
                self._try_apply(control, 'setPosition', 155 if fitted else 88, ROW_TOP + index*ROW_PITCH + offset)
                self._try_apply(control, 'setWidth', 232 if fitted else 296)

    def _restore_geometry(self):
        for index in range(6):
            self._station_geometry(index, False)
        self._try_apply(self._control(113), 'setWidth', 1760)

    def update(self):
        binding = self._source()
        if binding != self._binding:
            self._hide()  # source/view/snapshot revocation BEFORE any new assignment
            self._binding = binding
        self._selected, self._logos, self._missing = None, (), 0
        if binding is None or self._disabled:
            return
        session, inventory, config, view = binding
        self._selected = self._selection(inventory)
        w = self.window
        if view == 'Guide' and w._guide_runtime.ready:
            self._logos = tuple(channel_key(c.key) for c in w.guide.channels[w.guide.top:w.guide.top+6])
        refs = {}
        # Coalesce repeated identities in one frame. Never fetch or inspect bytes.
        for key in dict.fromkeys(k for k in (self._selected, *self._logos) if k is not None):
            candidates = inventory.logos.get(key, ())
            if not candidates:
                primary = inventory.references.get(key)
                candidates = (primary,) if primary else ()
            ref = first_reference(config.origin, candidates)
            if ref is None:
                self._missing += 1
            else:
                refs[key] = ref
        self._refs = refs

    def poll(self) -> bool:
        # Existing owner-poll integration only revokes stale refs, never waits.
        if self._source() != self._binding:
            had_refs = bool(self._refs)
            self._hide()
            self._binding = None
            self._restore_geometry()
            return had_refs
        return False

    def ready(self, key=None):
        """Resolved reference only (historical adapter name), NOT decoder readiness."""
        if self._source() != self._binding or self._binding is None:
            return None
        if self._selection(self._binding[1]) != self._selected:
            return None
        return self._refs.get(self._selected if key is None else key)

    def diagnostic_text(self):
        valid = self._binding is not None and self._source() == self._binding
        return text(self.window.strings, 'art_native_status', available=len(self._refs) if valid else 0,
            assigned=len(self._assigned) if valid else 0, unavailable=self._missing if valid else 0)

    def _assign(self, image, key, ref):
        identity = (key, ref) if ref else None
        if self._assigned.get(image) == identity:
            return True
        # Clear old source/selection before replacing; never infer success of decode.
        if not self._try_apply(image, 'setVisible', False) or not self._try_apply(image, 'setImage', ''):
            return False
        self._assigned.pop(image, None)
        if ref:
            if not self._try_apply(image, 'setImage', ref) or not self._try_apply(image, 'setVisible', True):
                return False
            self._assigned[image] = identity
        return True

    def draw(self, sdk):
        if self._source() != self._binding or self._disabled:
            self._hide()
            self._restore_geometry()
            return
        for index in range(6):
            key = self._logos[index] if index < len(self._logos) else None
            ref = self.ready(key) if key is not None else None
            if ref and not self._images:
                try:
                    for n in range(6):
                        image = sdk.ControlImage(80, ROW_TOP + 7 + n*ROW_PITCH, 64, 64, '', aspectRatio=2)
                        self.window.addControls([image])
                        self._images.append(image)
                except Exception:
                    self._disable()
                    return
            self._station_geometry(index, bool(ref))
            if index < len(self._images) and not self._assign(self._images[index], key, ref):
                self._disable()
                return
        ref = self.ready()
        guide = self.window.view == 'Guide'
        rect = (1520, 125, 300, 132) if guide else (1600, 870, 240, 80)
        if ref and self._preview is None:
            try:
                self._preview = sdk.ControlImage(*rect, '', aspectRatio=2)
                self.window.addControls([self._preview])
            except Exception:
                self._disable()
                return
        if self._preview is not None:
            x, y, width, height = rect
            placed = not ref or all((self._try_apply(self._preview, 'setPosition', x, y),
                                     self._try_apply(self._preview, 'setWidth', width),
                                     self._try_apply(self._preview, 'setHeight', height)))
            if not placed:
                # Hide only the misplaced preview. The next draw retries the
                # uncommitted setter; admitted references and logos survive.
                hidden = self._try_apply(self._preview, 'setVisible', False)
                cleared = self._try_apply(self._preview, 'setImage', '')
                self._assigned.pop(self._preview, None)
                if not hidden or not cleared:
                    self._disable()  # Unable to prove revocation, not just a layout failure.
                ref = None
            elif not self._assign(self._preview, self._selected, ref):
                self._disable()
                return
        self._try_apply(self._control(113), 'setWidth', 1500 if ref and not guide else 1760)

    def close(self):
        self._hide()
        self._binding = None

    def detached(self):
        self._refs = {}
        self._assigned.clear()


def show_media_details(window, row):
    """Completed text modal only; image delivery is delegated to Kodi."""
    try:
        if not row.detail or len(row.detail.encode('utf-8')) > 32768:
            return False
        from .programme_scope_ui import show_scope
        return show_scope(window.path, row.label, row.detail, strings=window.strings,
                          art_path=window._artwork_ui.ready(), modal_owner=window) is True
    except Exception:
        return False
