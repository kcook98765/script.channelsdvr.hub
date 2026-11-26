"""Fence parent callbacks at blocking native dialog boundaries, not on idle polls."""
from contextlib import nullcontext


def modal_input(window):
    # The operation presenters are also used with standalone fake owners.
    dispatcher = getattr(window, '_dispatcher', None)
    scope = getattr(dispatcher, 'modal_input', None)
    return scope() if scope is not None else nullcontext()


def modal_call(window, method, *args, **kwargs):
    chrome = getattr(window, '_chrome', None)
    layout = chrome.layout if chrome is not None else None
    origin = chrome.focus() if chrome is not None else None
    with modal_input(window):
        result = method(*args, **kwargs)
    if (chrome is not None and origin is not None
            and chrome.layout is layout and window._owns_input()):
        chrome.restore(origin)
    return result


def dialog_for(window, gui):
    return BoundDialog(window, gui.Dialog())


class ModalGui:
    """The injected playback/Series adapters need only Dialog and default-No."""
    def __init__(self, window, gui):
        self.window, self.gui = window, gui

    @property
    def DLG_YESNO_NO_BTN(self):
        return self.gui.DLG_YESNO_NO_BTN

    def Dialog(self):
        return dialog_for(self.window, self.gui)


class BoundDialog:
    def __init__(self, window, dialog):
        self.window, self.dialog = window, dialog

    def select(self, *args, **kwargs):
        return modal_call(self.window, self.dialog.select, *args, **kwargs)

    def ok(self, *args, **kwargs):
        return modal_call(self.window, self.dialog.ok, *args, **kwargs)

    def yesno(self, *args, **kwargs):
        return modal_call(self.window, self.dialog.yesno, *args, **kwargs)

    def input(self, *args, **kwargs):
        return modal_call(self.window, self.dialog.input, *args, **kwargs)

    def textviewer(self, *args, **kwargs):
        return modal_call(self.window, self.dialog.textviewer, *args, **kwargs)
