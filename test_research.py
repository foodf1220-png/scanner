# -*- coding: utf-8 -*-
"""연구판 모의(bf_research) 시험 (python test_research.py) - 가짜 시세·가짜 시각, 텔레그램 대신 목록에 저장"""
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

    def sig(self, tk, px, hhmm):
        self.R.on_signal(dict(tk=tk, px=px, t=FC.iso(at(hhmm)), px_src="토스"), at(hhmm))

    def tick(self, hhmm):
        self.R.tick(at(hhmm), W, self.quote, force=True)

    def trades(self):
        p = os.path.join(self.tmp, "tr.jsonl")
        return [json.loads(x) for x in open(p, encoding="utf-8")] if os.path.exists(p) else []


class Flow(Base):
    def test_first_signal_skips_reentry_variant(self):
        self.sig("KYNB", 2.00, "10:00")
        self.assertEqual(sorted(p["v"] for p in self.R.open), ["①", "②"])
        self.assertIn("오늘 첫 신호라 안 삼", self.sent[-1])
        self.assertIn("실제 주문 없음", self.sent[-1])

    def test_entry_price_includes_slippage_and_levels(self):
        self.sig("KYNB", 2.00, "10:00")
        p = next(p for p in self.R.open if p["v"] == "②")
        self.assertAlmostEqual(p["E"], 2.00 * 1.002)                  # $5 미만 슬리피지 0.2%
        self.assertAlmostEqual(p["TP"], FC.round_near(2.004 * 1.03))
        self.assertAlmostEqual(p["SL"], FC.round_near(2.004 * 0.96))

    def test_tp_hit_closes_only_that_variant(self):
        self.sig("KYNB", 2.00, "10:00")
        self.px["KYNB"] = 2.07                                         # ② 익절가 2.06 위, ① 익절가 2.10 아래
        self.tick("10:05")
        t = self.trades()
        self.assertEqual([(x["v"], x["why"]) for x in t], [("②", "tp")])
        X = 2.06 * (1 - 0.002)
        self.assertAlmostEqual(t[0]["ret"], round(((X * 0.999) / (2.004 * 1.001) - 1) * 100, 3), places=3)
        self.assertEqual([p["v"] for p in self.R.open], ["①"])
        self.assertIn("🟢", self.sent[-1])

    def test_reentry_signal_opens_variant3_and_respects_holding(self):
        self.sig("KYNB", 2.00, "10:00")
        self.px["KYNB"] = 2.07
        self.tick("10:05")                                             # ② 청산, ① 보유
        self.sig("KYNB", 2.10, "10:20")
        msg = self.sent[-1]
        self.assertIn("오늘 2번째 신호", msg)
        self.assertIn("① 기준선  - 이미 보유 중이라 안 삼", msg)
        self.assertEqual(sorted(p["v"] for p in self.R.open), ["①", "②", "③"])

    def test_signal_shows_cumulative_return(self):
        self.sig("KYNB", 2.00, "10:00")
        self.assertIn("📊 전체 누적: 아직 끝난 거래 없음", self.sent[-1])
        self.px["KYNB"] = 2.07
        self.tick("10:05")                                             # ② 익절
        r2 = self.trades()[0]["ret"]
        self.sig("ATOS", 2.50, "10:30")
        msg = self.sent[-1]
        self.assertTrue(msg.splitlines()[-1].startswith("③ 재진입"))    # 누적이 메시지 맨 끝
        self.assertIn("📊 전체 누적 (11/30부터", msg)
        self.assertIn(f"② 승률형  수익률 {r2:+.2f}% · 승률 100% (1승 0패)", msg)
        self.assertIn("① 기준선  거래 없음", msg)

    def test_stop_and_time_exit(self):
        self.sig("AAA", 3.00, "10:00")
        self.px["AAA"] = 2.87                                          # -4% 아래
        self.tick("10:10")
        self.assertEqual({x["why"] for x in self.trades()}, {"sl"})
        self.sig("BBB", 12.0, "11:00")
        self.px["BBB"] = 12.05
        self.tick("11:30")
        self.assertEqual(len(self.R.open), 2)
        self.tick("12:00")                                             # 60분
        self.assertEqual(sum(x["why"] == "time" for x in self.trades()), 2)
        self.assertEqual(self.R.open, [])

    def test_day_end_flattens_and_summarizes(self):
        self.sig("KYNB", 2.00, "10:00")
        self.px["KYNB"] = 2.01
        self.R.day_end(W["flat"], W, self.quote)
        self.assertEqual({x["why"] for x in self.trades()}, {"flat"})
        s = self.sent[-1]
        self.assertIn("연구판 모의 결과", s)
        self.assertIn("실전 신호 1건 (KYNB)", s)
        self.assertIn("③ 재진입", s)
        self.assertIn("누적 1일", s)

    def test_restart_restores_open_positions(self):
        self.sig("KYNB", 2.00, "10:00")
        R2 = self.make()
        R2.start_day(D)
        self.assertEqual(len(R2.open), 2)
        self.assertEqual(R2.nth, {"KYNB": 1})
        R3 = self.make()
        R3.start_day("2026-12-01")                                     # 다음 날: 지난 포지션 정리
        self.assertEqual(R3.open, [])

    def test_missing_price_keeps_position(self):
        self.sig("KYNB", 2.00, "10:00")
        self.tick("10:05")                                             # 시세 없음
        self.assertEqual(len(self.R.open), 2)


class LiveHook(unittest.TestCase):
    def test_bf_live_emit_feeds_research(self):
        import bf_live as BL
        tmp = tempfile.mkdtemp()
        try:
            BL.QUEUE = os.path.join(tmp, "q.jsonl")
            bot = BL.BFLive(CFG)
            sent = []
            bot.research = Research(CFG, log=lambda m: None, state_file=os.path.join(tmp, "st.json"),
                                    trades_file=os.path.join(tmp, "tr.jsonl"), sender=sent.append)
            bot.research.start_day(D)
            R = dict(trigger=2.0, ema=1.98, vwap=1.95, key="k", pb_low=1.9, start=1.8, high=2.05, retr=30.0, pb=[1, 2],
                     chg=20.0, rvol=5.0)
            bot.emit("KYNB", R, 2.005, at("10:00:30"), src="토스")
            q = [json.loads(x) for x in open(BL.QUEUE, encoding="utf-8")]
            self.assertEqual(q[0]["strategy"], "BF")                   # 실전 신호는 그대로
            self.assertEqual(len(bot.research.open), 2)
            self.assertIn("KYNB", sent[-1])
            bot.emit("PPR", R, 12.0, at("10:01"), paper=True, src="토스")   # 모의 범위 신호는 연구판에 안 들어감
            self.assertEqual(len(bot.research.open), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
