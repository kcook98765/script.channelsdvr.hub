"""Bounded, in-memory GUI measurements; callers supply elapsed milliseconds."""

from collections import deque
from math import ceil, isfinite
from .ui_text import FALLBACK, text


class GuiMetrics:
    """Single-owner metrics; percentiles use nearest rank over retained samples."""

    def __init__(self):
        self.render_ms: deque[float] = deque(maxlen=256)
        self.direction_ms: deque[float] = deque(maxlen=256)
        self.render_count = self.direction_count = self.init_count = 0
        self.render_max_ms = self.direction_max_ms = 0.0
        self.pool_pairs = self.pool_pairs_highwater = 0
        self.visible_cells = self.visible_cells_highwater = 0

    def initialized(self) -> None:
        self.init_count += 1

    @staticmethod
    def _elapsed(value):
        try:
            if type(value) not in (int, float):
                raise ValueError
            value = float(value)
            if not isfinite(value) or value < 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise ValueError("elapsed milliseconds must be finite and nonnegative") from None
        return value

    def rendered(self, elapsed_ms, pool_pairs, visible_cells) -> None:
        elapsed_ms = self._elapsed(elapsed_ms)
        if any(type(n) is not int or n < 0 for n in (pool_pairs, visible_cells)):
            raise ValueError("pool and visible counts must be nonnegative integers")
        self.render_ms.append(elapsed_ms)
        self.render_count += 1
        self.render_max_ms = max(self.render_max_ms, elapsed_ms)
        self.pool_pairs, self.visible_cells = pool_pairs, visible_cells
        self.pool_pairs_highwater = max(self.pool_pairs_highwater, pool_pairs)
        self.visible_cells_highwater = max(self.visible_cells_highwater, visible_cells)

    def navigated(self, elapsed_ms) -> None:
        elapsed_ms = self._elapsed(elapsed_ms)
        self.direction_ms.append(elapsed_ms)
        self.direction_count += 1
        self.direction_max_ms = max(self.direction_max_ms, elapsed_ms)

    def report(self) -> str:
        lines = [f"onInit count={self.init_count}"]
        for label, samples, count, maximum in (
            ("Render", self.render_ms, self.render_count, self.render_max_ms),
            ("Direction callback", self.direction_ms, self.direction_count, self.direction_max_ms),
        ):
            ordered = sorted(samples)
            p50, p95 = (ordered[ceil(len(ordered) * p) - 1] if ordered else 0.0
                        for p in (0.50, 0.95))
            latest = samples[-1] if samples else 0.0
            lines.append(f"{label} count={count} samples={len(samples)}/256 ms: "
                         f"latest={latest:.3f} max={maximum:.3f} p50={p50:.3f} p95={p95:.3f}")
        lines.extend((f"Pooled pairs latest={self.pool_pairs} highwater={self.pool_pairs_highwater}",
                      f"Materialized visible cells latest={self.visible_cells} "
                      f"highwater={self.visible_cells_highwater}",
                      "Times exclude MCP/model/menu/dialog wait; render is API update time, "
                      "not frame presentation. Process/native memory unmeasured"))
        return "\n".join(lines)

    def compact_report(self, strings=FALLBACK) -> str:
        """Six metadata-only lines; count/samples applies to Render and Dir.

        p95(win) uses retained samples; max(life) includes evicted samples.
        Very large counts use an explicit lower bound, and large times use
        scientific notation, keeping the native dialog's lines short.
        """
        def count(value):
            return str(value) if value <= 999999 else ">999999"

        def milliseconds(value):
            return f"{value:.3f}" if value < 1000000 else f"{value:.3e}"

        def timings(samples, maximum):
            ordered = sorted(samples)
            p95 = ordered[ceil(len(ordered) * 0.95) - 1] if ordered else 0.0
            return f"ms p95(win)={milliseconds(p95)} max(life)={milliseconds(maximum)}"

        return text(strings, 'metrics_compact', init=count(self.init_count), render=count(self.render_count),
            render_samples=len(self.render_ms), render_times=timings(self.render_ms, self.render_max_ms),
            directions=count(self.direction_count), direction_samples=len(self.direction_ms),
            direction_times=timings(self.direction_ms, self.direction_max_ms), pairs=count(self.pool_pairs),
            pairs_high=count(self.pool_pairs_highwater), cells=count(self.visible_cells),
            cells_high=count(self.visible_cells_highwater))
