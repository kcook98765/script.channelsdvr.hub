"""Owner-only memo for properties this window owns; SDK supplied by the caller.

No focus, external properties or SDK getters are memoized. A new XML lifecycle
must reset the memo, even if the logical view is unchanged. Only successful writes
enter the memo; a failing setter discards its previous proof and is retried.
Labels are coalesced per frame so clear/rebind produces only the final value.
"""


class RenderMemo:
    def __init__(self):
        self.reset()

    def reset(self):
        self.context = None
        self.controls = {}
        self.values = {}
        self.labels: dict[int, str] | None = None

    def begin(self, context):
        if self.context != context:
            self.reset()
            self.context = context
        self.labels = {}

    def apply(self, control, method, *args):
        key = (control, method)
        if key in self.values and self.values[key] == args:
            return
        self.values.pop(key, None)
        getattr(control, method)(*args)
        self.values[key] = args

    def best_effort(self, control, method, *args):
        """Optional presentation only. apply already revokes a failed proof."""
        try:
            self.apply(control, method, *args)
            return True
        except Exception:
            return False

    def label(self, window, sdk, control_id, text):
        if self.labels is not None:
            self.labels[control_id] = text
            return
        control = self.controls.get(control_id)
        if control is None:
            control = window.getControl(control_id)
            self.controls[control_id] = control
        if isinstance(control, sdk.ControlLabel):
            self.apply(control, "setLabel", text)

    def finish(self, window, sdk):
        labels, self.labels = self.labels, None
        if labels is None:
            return
        for control_id, text in labels.items():
            self.label(window, sdk, control_id, text)


def draw(window, index, x, y, width, height, text, selected, gap=False):
    image, label = window.pool[index]
    apply = window._render_memo.apply
    apply(image, "setPosition", x, y)
    apply(image, "setWidth", max(1, width - 3))
    apply(image, "setHeight", height - 3)
    texture = "selected.png" if selected else "gap.png" if gap else "cell.png"
    apply(image, "setImage", window._texture(texture))
    apply(image, "setVisible", True)
    apply(label, "setPosition", x + 8, y + 3)
    apply(label, "setWidth", max(1, width - 16))
    apply(label, "setHeight", height - 6)
    apply(label, "setLabel", text)
    apply(label, "setVisible", width >= 50)
