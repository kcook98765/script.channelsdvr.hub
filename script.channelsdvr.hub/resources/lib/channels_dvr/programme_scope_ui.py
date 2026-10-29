"""Kodi-only scope display, with no worker/token/profile or Save authority.

Own XML IDs only. Omega Kodistubs21 documents WindowXMLDialog/onInit and
ControlTextBox.reset/setText/scroll(position), Window.setFocusId. Manual scrolling
belongs to the XML-linked native scrollbar, never a Python position estimate.
Callback errors latch because Kodi need not propagate them through doModal.
"""
import xbmcgui
from channels_dvr.ui_text import FALLBACK, text as ui_text


class ProgrammeScopeDialog(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, heading, text, strings=FALLBACK, art_path=None):
        super().__init__(*args)
        self._heading, self._text = heading, text
        self._footer = ui_text(strings, 'scope_footer', close=ui_text(strings, 'close'))
        self._initialized = self._dismissed = self._failed = self._disposed = False
        self._art_path = art_path
        self._art_image = None

    def _live(self):
        return not (self._disposed or self._dismissed or self._failed)

    def _failure(self):
        self._failed = True
        self.close()

    def onInit(self):
        if not self._live():
            return
        self._initialized = False
        try:
            label = self.getControl(200)
            box = self.getControl(201)
            button = self.getControl(100)
            if not isinstance(label, xbmcgui.ControlLabel) or not isinstance(box, xbmcgui.ControlTextBox):
                raise RuntimeError('scope controls unavailable')
            if not isinstance(button, xbmcgui.ControlButton):
                raise RuntimeError('scope close control unavailable')
            label.setLabel(self._heading)
            button.setLabel(self._footer)
            if self._art_path:
                # Resolved URL, not a ready local file. Kodi owns asynchronous
                # load/decode/cache; a later decode miss cannot reflow this box.
                try:
                    if self._art_image is None:
                        image = xbmcgui.ControlImage(1380, 140, 390, 580, self._art_path, aspectRatio=2)
                        self.addControls([image])
                        self._art_image = image
                    box.setWidth(1240)
                except Exception:
                    if self._art_image is not None:
                        try:
                            self._art_image.setVisible(False)
                        except Exception:
                            pass
                    try:
                        box.setWidth(1690)
                    except Exception:
                        pass  # optional art geometry must not suppress full text
            box.reset()
            box.setText(self._text)
            box.scroll(0)  # attached in onInit; repeat on EVERY initialization
            self.setFocusId(202)  # own XML native pagecontrol, including on re-init
            self._initialized = True
        except Exception:
            self._failure()

    def onAction(self, action):
        if not self._live():
            return
        try:
            ident = action.getId()
            if ident in (xbmcgui.ACTION_NAV_BACK, xbmcgui.ACTION_PREVIOUS_MENU):
                self.close()
            # Other actions belong to native focused XML controls. No requested
            # offset to drift beyond the rendered bottom or unwind on reversal.
        except Exception:
            self._failure()

    def onClick(self, controlId):
        if self._live() and controlId == 100:
            self.close()

    def close(self):
        if self._disposed or self._dismissed:
            return
        self._dismissed = True
        try:
            super().close()
        except Exception:
            self._failed = True


def show_scope(addon_path, heading, text, strings=FALLBACK, *, art_path=None):
    """True means successful display/dismissal ONLY; caller separately asks Save."""
    if type(text) is not str or not text or len(text.encode('utf-8')) > 32768:
        raise ValueError('scope text unavailable')
    dialog = ProgrammeScopeDialog('ProgrammePassScope.xml', addon_path, 'Default', '1080i',
                                  heading=heading, text=text, strings=strings, art_path=art_path)
    try:
        dialog.doModal()
        if dialog._failed or not dialog._initialized or not dialog._dismissed:
            raise RuntimeError('scope display failed')
        return True
    finally:
        dialog.close()
        dialog._disposed = True
