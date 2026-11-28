# -*- coding: utf-8 -*-
"""scalp_core 핵심 규칙 시험 (python test_scalp.py)"""
import unittest

import numpy as np
import pandas as pd

import scalp_core as SC


def frame(o, h, l, c, v=None, start="2026-11-16 10:00"):
    n = len(c)
    t = pd.date_range(pd.Timestamp(start).tz_localize("America/New_York"), periods=n, freq="min")
    return dict(o=np.array(o, float), h=np.array(h, float), l=np.array(l, float), c=np.array(c, float),
                v=np.array(v if v is not None else [1000] * n, float), t=t, n=n, flat_k=n)


CFG = SC.load_config()


class Fractal(unittest.TestCase):
    def test_confirm_needs_right_bars(self):
        low = np.array([5, 4, 3, 2, 1, 2, 3, 4, 5], float)
        fr = SC.fractal_low(low, 3)
        self.assertTrue(fr[4])
        self.assertEqual(fr.sum(), 1)
        self.assertFalse(SC.fractal_low(low[:7], 3)[4])          # 오른쪽 3봉 중 2봉만 있으면 미확정

    def test_equal_low_not_fractal(self):
        low = np.array([5, 4, 3, 1, 1, 3, 4, 5], float)
        self.assertEqual(SC.fractal_low(low, 2).sum(), 0)


class Exits(unittest.TestCase):
    def entry(self, X, k=0):
        return dict(k=k, E_raw=float(X["o"][k]), rt=False)

    def test_same_bar_both_is_stop(self):
        X = frame([10, 10], [10.5, 10.1], [9.8, 10], [10, 10])
        r = SC.sim_exit(X, self.entry(X), 2.0, 1.5, CFG, scen=0.0, fee=0.0)
        self.assertEqual(r["why"], "sl")
        self.assertTrue(r["amb"])

    def test_gap_below_stop_fills_at_open(self):
        X = frame([10, 9.5], [10.05, 9.6], [9.95, 9.4], [10, 9.5])
        r = SC.sim_exit(X, self.entry(X), 2.0, 1.5, CFG, scen=0.0, fee=0.0)
        self.assertEqual(r["why"], "sl")
        self.assertAlmostEqual(r["X"], 9.5)

    def test_tp_sells_at_trigger_minus_slip(self):
        X = frame([10, 10.1], [10.05, 10.25], [9.95, 10.05], [10, 10.2])
        r = SC.sim_exit(X, self.entry(X), 2.0, 1.5, CFG, scen=0.1, fee=0.0)
        self.assertEqual(r["why"], "tp")
        self.assertAlmostEqual(r["X"], r["TP"] * 0.999, places=6)
        self.assertAlmostEqual(r["E"], 10 * 1.001, places=6)

    def test_time_stop_ret(self):
        X = frame([10, 10, 10], [10.02, 10.02, 10.02], [9.99, 9.99, 9.99], [10.0, 10.01, 10.0])
        r = SC.sim_exit(X, self.entry(X), 2.0, 1.5, CFG, scen=0.0, fee=0.0, tstop=(1, "ret"))
        self.assertEqual(r["why"], "time")
        self.assertEqual(r["ex_k"], 0)

    def test_flat_exit_at_flat_bar_open(self):
        X = frame([10, 10, 10.05], [10.02] * 3, [9.99] * 3, [10, 10, 10])
        X["flat_k"] = 2
        r = SC.sim_exit(X, self.entry(X), 2.0, 1.5, CFG, scen=0.0, fee=0.0)
        self.assertEqual(r["why"], "flat")
        self.assertAlmostEqual(r["X"], 10.05)

    def test_fee_applied_both_sides(self):
        X = frame([10, 10.3], [10.05, 10.4], [9.99, 10.2], [10, 10.3])
        r = SC.sim_exit(X, self.entry(X), 2.0, 1.5, CFG, scen=0.0, fee=0.001)
        self.assertAlmostEqual(r["ret"], ((10.3 * 0.999) / (10 * 1.001) - 1) * 100, places=6)


class Indicators(unittest.TestCase):
    def test_rsi_bounds(self):
        c = np.cumsum(np.random.default_rng(1).normal(0, 1, 300)) + 100
        r = SC.rsi(c, 10)
        self.assertTrue(np.nanmin(r) >= 0 and np.nanmax(r) <= 100)
        self.assertTrue(np.isnan(r[:10]).all())

    def test_pivot_high(self):
        h = np.array([1, 2, 5, 3, 2, 6, 1], float)
        p = SC.pivot_high(h, 2, 2)
        self.assertTrue(p[2])
        self.assertFalse(p[5])                                   # 오른쪽 2봉이 없음 -> 미확정


if __name__ == "__main__":
    unittest.main(verbosity=1)
