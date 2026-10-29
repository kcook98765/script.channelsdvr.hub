"""Pure, bounded GUI measurement contracts."""

import unittest

from channels_dvr.metrics import GuiMetrics


class MetricsTests(unittest.TestCase):
    def test_bounds_preserve_lifetime_counts_and_maxima(self):
        m = GuiMetrics()
        m.rendered(999, 0, 0)
        m.navigated(888)
        for n in range(300):
            m.rendered(n, 0, 0)
            m.navigated(n)
        self.assertEqual((len(m.render_ms), len(m.direction_ms)), (256, 256))
        self.assertEqual((m.render_ms[0], m.direction_ms[-1]), (44, 299))
        self.assertEqual((m.render_count, m.direction_count), (301, 301))
        self.assertEqual((m.render_max_ms, m.direction_max_ms), (999, 888))
        self.assertIn("p50=171.000 p95=287.000", m.report())

    def test_nearest_rank_percentiles_and_latest(self):
        m = GuiMetrics()
        for n in range(20, 0, -1):
            m.rendered(n, 1, 1)
            m.navigated(n / 2)
        self.assertIn("latest=1.000 max=20.000 p50=10.000 p95=19.000", m.report())
        self.assertIn("latest=0.500 max=10.000 p50=5.000 p95=9.500", m.report())

    def test_invalid_timings_do_not_mutate(self):
        m = GuiMetrics()
        before = m.report()
        for bad in (-1, float("nan"), float("inf"), -float("inf"), True, "1", None, 10**400):
            for call in (lambda: m.rendered(bad, 1, 1), lambda: m.navigated(bad)):
                with self.assertRaises(ValueError):
                    call()
                self.assertEqual(m.report(), before)

    def test_invalid_counts_do_not_mutate(self):
        m = GuiMetrics()
        for bad in (-1, 1.5, float("nan"), float("inf"), True, "1", None):
            for counts in ((bad, 0), (0, bad)):
                with self.assertRaises(ValueError):
                    m.rendered(1, *counts)
                self.assertEqual(m.report(), GuiMetrics().report())

    def test_pool_highwater_and_initializations(self):
        m = GuiMetrics()
        m.initialized()
        m.initialized()
        m.rendered(0, 12, 9)
        m.rendered(0, 3, 10)
        self.assertIn("onInit count=2", m.report())
        self.assertIn("Pooled pairs latest=3 highwater=12", m.report())
        self.assertIn("Materialized visible cells latest=10 highwater=10", m.report())

    def test_empty_report_is_plain_text_and_explicit(self):
        report = GuiMetrics().report()
        self.assertEqual(report.count("count=0 samples=0/256"), 2)
        self.assertEqual(report.count("latest=0.000 max=0.000 p50=0.000 p95=0.000"), 2)
        self.assertTrue(report.endswith(
            "Times exclude MCP/model/menu/dialog wait; render is API update time, "
            "not frame presentation. Process/native memory unmeasured"))

    def test_empty_compact_report(self):
        self.assertEqual(GuiMetrics().compact_report().splitlines(), [
            "Init 0 Render 0/0 (count/samples)",
            "Render ms p95(win)=0.000 max(life)=0.000",
            "Dir 0/0 ms p95(win)=0.000 max(life)=0.000",
            "Pairs cur/hi=0/0 Cells cur/hi=0/0",
            "API updates, not frames/MCP/menu wait.",
            "Process/native memory unmeasured.",
        ])

    def test_compact_retained_percentiles_lifetime_max_and_no_mutation(self):
        m = GuiMetrics()
        m.initialized()
        for n in range(600):
            m.rendered(999 if n == 0 else n, 12 if n == 0 else 3, 10 if n == 0 else 9)
            m.navigated(888 if n == 0 else n / 2)
        before = m.report()
        render_samples, direction_samples = tuple(m.render_ms), tuple(m.direction_ms)
        self.assertEqual(m.compact_report().splitlines(), [
            "Init 1 Render 600/256 (count/samples)",
            "Render ms p95(win)=587.000 max(life)=999.000",
            "Dir 600/256 ms p95(win)=293.500 max(life)=888.000",
            "Pairs cur/hi=3/12 Cells cur/hi=9/10",
            "API updates, not frames/MCP/menu wait.",
            "Process/native memory unmeasured.",
        ])
        self.assertEqual(m.report(), before)
        self.assertEqual(tuple(m.render_ms), render_samples)
        self.assertEqual(tuple(m.direction_ms), direction_samples)
        self.assertEqual((m.render_count, m.direction_count, m.init_count), (600, 600, 1))
        self.assertIn("p50=471.000 p95=587.000", before)
        self.assertIn("p50=235.500 p95=293.500", before)

    def test_compact_length_bound_representative_values(self):
        for elapsed, observations, pairs, cells in (
            (0.001, 1, 1, 0), (123.456, 600, 120, 240),
            (12345.678, 1000, 4096, 8192), (999999.9999, 1, 999999, 999999),
        ):
            with self.subTest(elapsed=elapsed, observations=observations):
                m = GuiMetrics()
                for _ in range(observations):
                    m.initialized()
                    m.rendered(elapsed, pairs, cells)
                    m.navigated(elapsed)
                lines = m.compact_report().splitlines()
                self.assertEqual(len(lines), 6)
                self.assertTrue(all(len(line) <= 64 for line in lines), lines)
                self.assertIn(f"p95(win)={elapsed:.3f}", lines[1])

    def test_compact_huge_numbers_remain_bounded(self):
        m = GuiMetrics()
        m.init_count = m.render_count = m.direction_count = 10**5000
        m.rendered(1e308, 10**5000, 10**5000)
        m.navigated(1e308)
        lines = m.compact_report().splitlines()
        self.assertEqual(len(lines), 6)
        self.assertTrue(all(len(line) <= 64 for line in lines), lines)
        self.assertIn("Render >999999/1", lines[0])
        self.assertIn("p95(win)=1.000e+308 max(life)=1.000e+308", lines[1])

    def test_compact_rejected_observations_leave_populated_metrics_unchanged(self):
        m = GuiMetrics()
        m.rendered(1.234, 12, 9)
        m.navigated(0.123)
        before = m.report(), m.compact_report()
        for bad in (-1, float("nan"), float("inf"), True, "1", None, 10**400):
            for call in (lambda: m.rendered(bad, 99, 99), lambda: m.navigated(bad)):
                with self.assertRaises(ValueError):
                    call()
                self.assertEqual((m.report(), m.compact_report()), before)
        with self.assertRaises(ValueError):
            m.rendered(999, 99, -1)
        self.assertEqual((m.report(), m.compact_report()), before)
