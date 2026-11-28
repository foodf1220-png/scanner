# -*- coding: utf-8 -*-
"""연구판 모의(bf_research) 시험 (python test_research.py) - 가짜 시세·가짜 시각, 텔레그램 대신 목록에 저장
설정: bf_config.json research = 승률형 하나 (+3/-4), $0.5~20 전 구간, 재진입 포함"""
import json
import os
import shutil
import tempfile
import unittest

import pandas as pd

import bf_core as BC
import fl_core as FC
from bf_research import Research

CFG = BC.load_config()
D = "2026-11-30"
W = FC.day_windows(D, CFG)


def at(hhmm):
    return FC.at(D, hhmm)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sent = []
        self.px = {}
        self.R = self.make()
        self.R.start_day(D)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self):
        return Research(CFG, log=lambda m: None, state_file=os.path.join(self.tmp, "st.json"),
                        trades_file=os.path.join(self.tmp, "tr.jsonl"), sender=self.sent.append)

    def quote(self, tks, W):
        return {tk: (self.px[tk], "토스") for tk in tks if tk in self.px}

    def sig(self, tk, px, hhmm, strategy="BF"):
        self.R.on_signal(dict(tk=tk, px=px, t=FC.iso(at(hhmm)), px_src="토스", strategy=strategy), at(hhmm))

    def tick(self, hhmm):
        self.R.tick(at(hhmm), W, self.quote, force=True)

    def trades(self):
        p = os.path.join(self.tmp, "tr.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(x) for x in f]


class Setup(unittest.TestCase):
    def test_config_is_single_winrate_variant_all_prices(self):
        r = CFG["research"]
        self.assertEqual([(v["name"], v["tp_pct"], v["sl_pct"]) for v in r["variants"]], [("승률형", 3.0, 4.0)])
        self.assertEqual(r["price_ranges"], [[0.5, 20.0]])


class Flow(Base):
    def test_signal_alert_format(self):
        self.sig("KYNB", 2.00, "10:00")
        m = self.sent[-1]
        self.assertTrue(m.startswith("🧪 승률형 가상 매수 · KYNB $2.00 (토스)"))
        self.assertIn("(+3%)", m)
        self.assertIn("(-4%)", m)
        self.assertIn("오늘 1번째 신호 · 실전 범위 · 실전 봇에도 같은 신호", m)
        self.assertIn("실제 주문 없음", m)
        self.assertTrue(m.splitlines()[-1].startswith("📊 전체 누적: 아직 끝난 거래 없음"))
        self.assertNotIn("①", m)

    def test_research_only_signal_marked(self):
        self.sig("MIDP", 5.00, "10:00", strategy="BF_RESEARCH")
        self.assertIn("실전 범위 밖 · 모의만", self.sent[-1])
        self.assertEqual(len(self.R.open), 1)

    def test_entry_price_includes_slippage_and_levels(self):
        self.sig("KYNB", 2.00, "10:00")
        p = self.R.open[0]
        self.assertAlmostEqual(p["E"], 2.00 * 1.002)                  # $5 미만 슬리피지 0.2%
        self.assertAlmostEqual(p["TP"], FC.round_near(2.004 * 1.03))
        self.assertAlmostEqual(p["SL"], FC.round_near(2.004 * 0.96))

    def test_tp_hit_and_fee_math(self):
        self.sig("KYNB", 2.00, "10:00")
        self.px["KYNB"] = 2.07
        self.tick("10:05")
        t = self.trades()
        self.assertEqual([x["why"] for x in t], ["tp"])
        X = 2.06 * (1 - 0.002)
        self.assertAlmostEqual(t[0]["ret"], round(((X * 0.999) / (2.004 * 1.001) - 1) * 100, 3), places=3)
        self.assertIn("🟢 승률형 익절 · KYNB", self.sent[-1])

    def test_reentry_after_exit_but_not_while_holding(self):
        self.sig("KYNB", 2.00, "10:00")
        n = len(self.sent)
        self.sig("KYNB", 2.02, "10:03")                                # 아직 보유 중 -> 안 삼, 알림 없음
        self.assertEqual(len(self.sent), n)
        self.assertEqual(len(self.R.open), 1)
        self.px["KYNB"] = 2.07
        self.tick("10:05")                                             # 익절
        self.sig("KYNB", 2.10, "10:20")                                # 재진입
        m = self.sent[-1]
        self.assertIn("오늘 3번째 신호 (재진입)", m)
        self.assertEqual(len(self.R.open), 1)
        r = self.trades()[0]["ret"]
        self.assertEqual(m.splitlines()[-1], f"수익률 {r:+.2f}% · 승률 100% (1승 0패)")
        self.assertIn("📊 전체 누적 (11/30부터", m)

    def test_stop_and_time_exit(self):
        self.sig("AAA", 3.00, "10:00")
        self.px["AAA"] = 2.87
        self.tick("10:10")
        self.assertEqual({x["why"] for x in self.trades()}, {"sl"})
        self.sig("BBB", 12.0, "11:00")
        self.px["BBB"] = 12.05
        self.tick("11:30")
        self.assertEqual(len(self.R.open), 1)
        self.tick("12:00")                                             # 60분
        self.assertEqual([x["why"] for x in self.trades()], ["sl", "time"])
        self.assertEqual(self.R.open, [])

    def test_day_end_summary_with_price_buckets(self):
        self.sig("KYNB", 2.00, "10:00")
        self.sig("CHEAP", 0.80, "10:01", strategy="BF_RESEARCH")
        self.sig("MID", 6.00, "10:02", strategy="BF_RESEARCH")
        self.px.update(KYNB=2.07, CHEAP=0.70, MID=6.01)
        self.tick("10:05")
        self.R.day_end(W["flat"], W, self.quote)
        s = self.sent[-1]
        self.assertTrue(s.startswith("🧪 승률형 모의 결과 · 2026-11-30"))
        self.assertIn("신호 3건 (실전 범위 1 · 범위 밖 2)", s)
        self.assertIn("오늘 3건 1승 2패", s)
        self.assertIn("가격대별 누적", s)
        self.assertIn("$1 미만  1건 승률 0%", s)
        self.assertIn("$1~3  1건 승률 100%", s)
        self.assertIn("$3~10  1건", s)

    def test_restart_restores_open_positions(self):
        self.sig("KYNB", 2.00, "10:00")
        R2 = self.make()
        R2.start_day(D)
        self.assertEqual(len(R2.open), 1)
        self.assertEqual(R2.nth, {"KYNB": 1})
        R3 = self.make()
        R3.start_day("2026-12-01")
        self.assertEqual(R3.open, [])

    def test_missing_price_keeps_position(self):
        self.sig("KYNB", 2.00, "10:00")
        self.tick("10:05")
        self.assertEqual(len(self.R.open), 1)


class FakeToss:
    def __init__(self, prices):
        self.prices, self.enabled, self.calls = prices, True, []

    def available(self):
        return True

    def price(self, tk):
        self.calls.append(tk)
        return self.prices[tk]


class LiveHook(unittest.TestCase):
    def setUp(self):
        import bf_live as BL
        self.BL = BL
        self.tmp = tempfile.mkdtemp()
        BL.QUEUE = os.path.join(self.tmp, "q.jsonl")
        self.bot = BL.BFLive(CFG)
        self.sent = []
        self.bot.research = Research(CFG, log=lambda m: None, state_file=os.path.join(self.tmp, "st.json"),
                                     trades_file=os.path.join(self.tmp, "tr.jsonl"), sender=self.sent.append)
        self.bot.research.start_day(D)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def queue(self):
        with open(self.BL.QUEUE, encoding="utf-8") as f:
            return {x["tk"]: x for x in map(json.loads, f)}

    @staticmethod
    def R(T, k):
        return dict(trigger=T, ema=T * 0.99, vwap=T * 0.97, key=k, pb_low=T * 0.95, start=T * 0.9, high=T * 1.02,
                    retr=30.0, pb=[1, 2], chg=20.0, rvol=5.0)

    def test_hot_routes_live_and_research_only(self):
        bot = self.bot
        bot.toss = FakeToss({"LIVE": 2.005, "OUT": 5.01, "TEN": 12.03, "CHASE": 0.81})
        bot.ready = {"LIVE": self.R(2.0, "a"), "OUT": self.R(5.0, "b"), "TEN": self.R(12.0, "c"), "CHASE": self.R(0.80, "d")}
        bot.loop_n = 1                                                 # 다음 확인이 짝수 -> 연구 전용 종목도 확인
        bot.hot(at("10:00:30"), W)
        q = self.queue()
        self.assertEqual(q["LIVE"]["strategy"], "BF")                  # 실전 주문 신호
        self.assertEqual(q["TEN"]["strategy"], "BF")
        self.assertEqual(q["OUT"]["strategy"], "BF_RESEARCH")          # 실전 범위 밖 -> 연구판만 (bot.py 는 안 받음)
        self.assertNotIn("CHASE", q)                                   # +1.25% 추격 -> 탈락 (실전과 같은 규칙)
        self.assertEqual(sorted(p["tk"] for p in bot.research.open), ["LIVE", "OUT", "TEN"])

    def test_research_only_quoted_every_other_loop(self):
        bot = self.bot
        bot.toss = FakeToss({"LIVE": 1.99, "OUT": 4.99})
        bot.ready = {"LIVE": self.R(2.0, "a"), "OUT": self.R(5.0, "b")}
        bot.loop_n = 0
        bot.hot(at("10:00:30"), W)                                     # 홀수 -> 실전 범위만
        self.assertEqual(bot.toss.calls, ["LIVE"])
        bot.hot(at("10:00:32"), W)                                     # 짝수 -> 둘 다
        self.assertEqual(bot.toss.calls, ["LIVE", "LIVE", "OUT"])

    def test_paper_signal_not_researched(self):
        self.bot.emit("PPR", self.R(12.0, "p"), 12.0, at("10:01"), paper=True, src="토스")
        self.assertEqual(self.bot.research.open, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
