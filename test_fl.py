# -*- coding: utf-8 -*-
"""test_fl.py - Fashionably Late V4.0 검증 테스트 (python test_fl.py)"""
import copy
import unittest

import numpy as np
import pandas as pd

import fl
from fl_backtest import sim_day, solo_cfg
from fl_core import (DEFAULTS, MIN, Account, bar_exit, build_frame, conditions, day_windows, early_exit, find_signals,
                     levels, market_hours, round_tick, rvol_profile, structure, ts, with_cfg)

D = "2026-11-24"                     # 표준시 (뉴욕 10:00 = 한국 00:00)
CROSS = 64                           # 합성 패턴에서 9EMA 가 VWAP 를 상향 돌파하는 봉 (10:34)


def cfg0(**kw):
    c = copy.deepcopy(DEFAULTS)
    return with_cfg(c, **kw) if kw else c


def make(tail="up", spike=True, deep=False, d=D):
    """합성 1분봉: 횡보 → 1차 급등(2.00→3.00, 큰 거래량) → 눌림(→2.55, 거래량 감소, 44%) → 저점 안정 → 회복 → 돌파"""
    W = day_windows(d, cfg0())
    closes, vols = [], []
    closes += [2.00 + 0.002 * (k % 2) for k in range(10)]
    vols += [5000] * 10
    closes += list(np.linspace(2.05, 3.00, 20))
    vols += list(np.linspace(40000, 150000, 20))
    closes += list(np.linspace(2.96, 2.25 if deep else 2.55, 20))
    vols += [15000] * 20
    base = 2.25 if deep else 2.55
    closes += [base + x for x in (0.01, 0.02, 0.015, 0.025, 0.02, 0.03, 0.025, 0.035, 0.03, 0.04)]
    vols += [15000] * 10
    closes += list(np.linspace(base + 0.05, base + 0.25, 16))
    vols += [22000] * 16
    closes += list(np.linspace(2.82, 3.00, 15)) if tail == "up" else list(np.linspace(2.60, 2.40, 15))
    vols += [30000] * 15
    v = np.array(vols, float)
    if spike:
        v[CROSS] = 40000
    c = np.array(closes)
    if tail == "down":                                   # 진입 직후 다시 밀림 (익절가 전에)
        k0 = CROSS + 2
        c[k0 + 1:] = c[k0] - 0.012 * np.arange(1, len(c) - k0)
    o = np.concatenate([[1.98], c[:-1]])
    idx = pd.date_range(W["open"], periods=len(c), freq="1min")
    df = pd.DataFrame(dict(Open=o, High=np.maximum(o, c) + 0.005, Low=np.minimum(o, c) - 0.005, Close=c, Volume=v),
                      index=idx)
    return W, df


def frame(df, W, cfg=None, partial=None):
    F = build_frame(df, W, 1.70, None, 100000.0, cfg or cfg0(), partial)
    return F, structure(F)


def signals(df, W, cfg=None, **kw):
    cfg = cfg or cfg0()
    F, S = frame(df, W, cfg)
    return find_signals(F, S, cfg, W, "TEST", **kw)[0], F, S


class Levels(unittest.TestCase):
    def test_example1(self):            # 저점 4.40, 체결 5.00 -> 손절 4.80 (-4%), 익절 5.60 (+12%)
        lv = levels(5.00, 4.40, cfg0())
        self.assertAlmostEqual(lv["SL"], 4.80)
        self.assertAlmostEqual(lv["TP"], 5.60)
        self.assertAlmostEqual(lv["sl_pct"], 4.0, places=6)
        self.assertAlmostEqual(lv["tp_pct"], 12.0, places=6)
        self.assertAlmostEqual(lv["rr"], 3.0, places=6)

    def test_example2(self):            # 저점 0.80, 체결 0.90 -> 손절 약 0.8667 (-3.70%), 익절 1.00 (+11.11%)
        lv = levels(0.90, 0.80, cfg0())
        self.assertAlmostEqual(lv["SL"], 0.8667)
        self.assertAlmostEqual(lv["TP"], 1.00)
        self.assertAlmostEqual(lv["sl_pct"], 3.70, places=2)
        self.assertAlmostEqual(lv["tp_pct"], 11.11, places=2)

    def test_tick(self):
        self.assertEqual(round_tick(0.866666, True), 0.8667)
        self.assertEqual(round_tick(4.8049, True), 4.81)
        self.assertEqual(round_tick(5.6049, False), 5.60)
        self.assertIsNone(levels(2.0, 2.1, cfg0()))          # 체결가가 저점 아래 -> 계산 불가

    def test_fixed(self):
        lv = levels(2.0, 1.5, cfg0(exit_style="fixed"))
        self.assertAlmostEqual(lv["SL"], 1.86)
        self.assertAlmostEqual(lv["TP"], 2.14)


class Calendar(unittest.TestCase):
    def test_buy_window_kst(self):
        """신규 매수 한국 23:30~02:30 (사용자 지정): 표준시 = 뉴욕 09:30~12:30, 서머타임 = 뉴욕 10:30~13:30"""
        kst = lambda t: f"{t.tz_convert('Asia/Seoul'):%H:%M}"
        w = day_windows(D, cfg0())
        self.assertEqual((kst(w["buy_start"]), kst(w["buy_end"])), ("23:30", "02:30"))
        self.assertEqual((f"{w['buy_start']:%H:%M}", f"{w['buy_end']:%H:%M}"), ("09:30", "12:30"))
        self.assertEqual(f"{w['flat']:%H:%M}", "15:50")     # 보유 종목은 정규장 종료 10분 전까지 관리
        w = day_windows("2026-07-15", cfg0())
        self.assertEqual((kst(w["buy_start"]), kst(w["buy_end"])), ("23:30", "02:30"))
        self.assertEqual(f"{w['buy_start']:%H:%M}", "10:30")
        w = day_windows(D, cfg0(buy_start_kst="22:00"))     # 개장 전 시각이면 개장부터
        self.assertEqual(f"{w['buy_start']:%H:%M}", "09:30")

    def test_dst_and_standard_et(self):
        """요청서 원안 (뉴욕 10:00~13:30): buy_*_kst 를 끄면"""
        c = cfg0(buy_start_kst=None, buy_end_kst=None)
        w = day_windows("2026-07-15", c)                    # 서머타임: 뉴욕 10:00 = 한국 23:00
        self.assertEqual(f"{w['buy_start'].tz_convert('Asia/Seoul'):%H:%M}", "23:00")
        self.assertEqual(f"{w['buy_end'].tz_convert('Asia/Seoul'):%H:%M}", "02:30")
        w = day_windows(D, c)                               # 표준시: 뉴욕 10:00 = 한국 00:00
        self.assertEqual(f"{w['buy_start'].tz_convert('Asia/Seoul'):%H:%M}", "00:00")
        self.assertEqual(f"{w['buy_end'].tz_convert('Asia/Seoul'):%H:%M}", "03:30")

    def test_holiday_early_close(self):
        self.assertIsNone(market_hours("2026-11-26"))       # 추수감사절
        self.assertIsNone(market_hours("2026-11-28"))       # 토요일
        w = day_windows("2026-11-27", cfg0())               # 조기 폐장 13:00
        self.assertEqual(f"{w['close']:%H:%M}", "13:00")
        self.assertEqual(f"{w['flat']:%H:%M}", "12:50")
        self.assertEqual(f"{w['buy_end']:%H:%M}", "12:30")


class Signal(unittest.TestCase):
    def test_signal_at_cross(self):
        W, df = make()
        sigs, F, S = signals(df, W)
        self.assertEqual([s["i"] for s in sigs], [CROSS])
        s = sigs[0]
        self.assertEqual(f"{ts(s['t']):%H:%M}", "10:34")
        self.assertAlmostEqual(s["L"], 2.545)                # 눌림 저점 = 급등 고점 이후 최저가
        self.assertTrue(20 <= s["retr"] <= 60)
        self.assertGreater(s["ema"], s["vwap"])

    def test_no_lookahead(self):
        """하루치 전체로 계산한 신호 = 봉이 하나씩 들어올 때마다 그때까지의 데이터로 계산한 신호"""
        W, df = make()
        full = [s["i"] for s in signals(df, W)[0]]
        inc = []
        for i in range(30, len(df)):
            s = signals(df.iloc[:i + 1], W, only_last=True)[0]
            inc += [x["i"] for x in s]
        self.assertEqual(full, inc)
        F1, S1 = frame(df, W)
        F2, S2 = frame(df.iloc[:CROSS + 1], W)
        for k in ("ema", "vwap", "va", "rvol"):
            np.testing.assert_allclose(F1[k].to_numpy()[:CROSS + 1], F2[k].to_numpy(), equal_nan=True)
        for k in ("H", "S", "L", "Li"):
            np.testing.assert_allclose(S1[k][:CROSS + 1], S2[k])

    def test_volume_required(self):
        W, df = make(spike=False)
        self.assertEqual(signals(df, W)[0], [])
        self.assertEqual(len(signals(df, W, cfg0(**{"conditions.volume": False}))[0]), 1)

    def test_deep_pullback_rejected(self):
        """눌림이 상승폭의 60% 를 넘으면 매수 안 함 (2.00→3.00 후 2.25 까지 = 74%)"""
        W, df = make(deep=True)
        sigs, F, S = signals(df, W, cfg0(**{"conditions.volume": False}))
        self.assertEqual(sigs, [])
        K, X = conditions(F, S, cfg0(), "pullback")
        self.assertGreater(np.nanmax(X["retr"][60:]), 60)

    def test_flat_ema_rejected(self):
        W, df = make()
        self.assertEqual(signals(df, W, cfg0(ema_flat_pct=5.0))[0], [])     # 기울기 기준을 높이면 '평평' 처리

    def test_chase_and_stop_width(self):
        W, df = make()
        self.assertEqual(signals(df, W, cfg0(max_vwap_ext_pct=0.5))[0], [])  # VWAP 이격 제한
        self.assertEqual(signals(df, W, cfg0(min_stop_pct=2.0))[0], [])      # 손절폭 1.36% < 2%

    def test_time_window(self):
        W, df = make()
        c = cfg0(buy_start_kst="00:40")                     # 표준시 뉴욕 10:40 부터 -> 10:35 신호는 시간 밖
        self.assertEqual(signals(df, day_windows(D, c), c)[0], [])

    def test_lod_mode(self):
        W, df = make()
        sigs = signals(df, W, cfg0(min_stop_pct=0.0, max_stop_pct=100.0), mode="lod")[0]
        self.assertTrue(sigs)
        self.assertAlmostEqual(sigs[0]["L"], df["Low"].iloc[:CROSS + 1].min())   # 원본: 당일 저점

    def test_partial_bar_a_mode(self):
        """A 모드: 진행 중인 봉(30초 경과, 거래량 2만 -> 1분 환산 4만)으로 교차를 바로 감지"""
        W, df = make(spike=False)
        part = df.iloc[:CROSS + 1].copy()
        part.iloc[-1, part.columns.get_loc("Volume")] = 20000
        F, S = frame(part, W, partial=30)
        sigs = find_signals(F, S, cfg0(), W, "TEST", only_last=True, entry_mode="A")[0]
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["mode"], "A")
        F2, S2 = frame(part, W)                               # 같은 봉을 완성봉으로 보면 거래량 2만 < 1.5배 -> 신호 없음
        self.assertEqual(find_signals(F2, S2, cfg0(), W, "TEST", only_last=True)[0], [])


def acct(cash=10000.0, **kw):
    return Account(cfg0(**kw), dict(day=D, start_eq=cash, cash=cash))


def sig(tk, px=2.0, L=1.85, score=50.0):
    return dict(tk=tk, day=D, mode="B", struct="pullback", t=f"{D}T10:30:00-05:00", px=px, L=L, score=score)


T0 = pd.Timestamp(f"{D} 10:31").tz_localize("America/New_York")


class AccountRules(unittest.TestCase):
    def test_sizing_30pct_and_slots(self):
        a = acct(**{"risk_limit_pct": 100.0})
        for k, tk in enumerate("ABC"):
            o = a.submit(sig(tk), T0, 2.0)
            self.assertIsNotNone(o)
            self.assertLessEqual(o["qty"] * 2.0 * 1.002 * 1.0025, 3000.0 + 1e-6)   # 종목당 30% 이하 (슬리피지·수수료 포함)
        self.assertIsNone(a.submit(sig("D"), T0, 2.0))                           # 미체결도 보유 수에 포함
        self.assertIn("보유 한도 초과", a.st["rows"][-1]["result"])
        for o in a.open_buys():
            a.fill_buy(o, 2.0, T0)
        self.assertLessEqual(a.exposure() / a.base() * 100, 90.0 + 1e-6)
        self.assertGreaterEqual(a.st["cash"], 1000.0 - 1e-6)                    # 예비 현금 10%

    def test_risk_limit_reduces_third(self):
        """3종목 x 30% x 손절 5% = 4.5% > 4% -> 세 번째 종목 수량 축소"""
        a = acct()
        for tk in "AB":
            a.fill_buy(a.submit(sig(tk, 2.0, 1.70), T0, 2.0), 2.0, T0)          # 손절 1.90 (-5%)
        o = a.submit(sig("C", 2.0, 1.70), T0, 2.0)
        self.assertIsNotNone(o)
        self.assertIn("위험 한도로 수량 축소", a.st["rows"][-1]["result"])
        a.fill_buy(o, 2.0, T0)
        self.assertLessEqual(a.expected_loss_pct(), 4.0 + 1e-6)

    def test_duplicate_and_reentry(self):
        a = acct()
        o = a.submit(sig("A"), T0, 2.0)
        self.assertIsNone(a.submit(sig("A"), T0, 2.0))
        self.assertIn("중복 주문", a.st["rows"][-1]["result"])
        a.fill_buy(o, 2.0, T0)
        a.sell("A", 2.1, T0 + MIN, "익절", "limit")
        self.assertIsNone(a.submit(sig("A"), T0 + 2 * MIN, 2.0))
        self.assertIn("재진입 금지", a.st["rows"][-1]["result"])

    def test_partial_fill_recomputes_levels(self):
        a = acct()
        o = a.submit(sig("A", 2.0, 1.85), T0, 2.0)
        a.fill_buy(o, 2.0, T0, cap=100)
        p = a.st["pos"]["A"]
        self.assertEqual(p["qty"], 100)
        self.assertEqual(o["status"], "open")
        sl1 = p["SL"]
        a.fill_buy(o, 2.05, T0 + MIN)                                            # 나머지 체결 -> 평균가 상승
        self.assertEqual(o["status"], "filled")
        self.assertGreater(p["avg"], 2.0)
        lv = levels(p["avg"], 1.85, cfg0())
        self.assertAlmostEqual(p["SL"], lv["SL"])                                # 실제 평균 체결가로 다시 계산
        self.assertNotEqual(p["SL"], sl1)

    def test_pnl_identity_and_flows(self):
        a = acct()
        for tk, px in (("A", 2.0), ("B", 3.0)):
            a.fill_buy(a.submit(sig(tk, px, px * 0.93), T0, px), px, T0)
        a.sell("A", 2.1, T0 + MIN, "익절", "limit", cap=200)                    # 부분 매도
        a.mark("A", 2.05)
        a.mark("B", 2.9)
        self.assertAlmostEqual(a.equity() - a.base(), a.realized() + a.unrealized(), places=6)
        r = a.day_ret()
        a.add_flow(5000.0, "입금")
        self.assertAlmostEqual(a.equity() - a.base(), a.realized() + a.unrealized(), places=6)
        self.assertNotAlmostEqual(a.day_ret(), r + 50, places=1)                # 입금은 수익률이 아님

    def test_account_lock_and_restart(self):
        a = acct()
        a.fill_buy(a.submit(sig("A", 2.0, 1.86), T0, 2.0), 2.0, T0)
        a.submit(sig("B", 2.0, 1.86), T0, 2.0)                                  # 미체결
        a.mark("A", 2.40)                                                        # +20% x 30% ≈ 계좌 +5.8%
        self.assertTrue(a.check_account(T0 + MIN))
        self.assertIn("+4%", a.st["locked"]["why"])
        self.assertEqual(a.open_buys(), [])                                      # 미체결 취소
        self.assertTrue(a.st["pos"]["A"]["exit_flag"])                           # 전량 청산 표시
        a.sell("A", 2.40, T0 + 2 * MIN, a.st["pos"]["A"]["exit_flag"])
        a.lock_done(T0 + 2 * MIN)
        self.assertIsNotNone(a.st["locked"]["done"])
        st = fl.json.loads(fl.json.dumps(a.st, default=str))                    # 저장 -> 재시작
        b = Account(cfg0(), st)
        self.assertIsNone(b.submit(sig("C"), T0 + 3 * MIN, 2.0))
        self.assertIn("계좌 잠금", b.st["rows"][-1]["result"])

    def test_account_stop(self):
        a = acct()
        for tk in "AB":
            a.fill_buy(a.submit(sig(tk, 2.0, 1.86), T0, 2.0), 2.0, T0)
        for tk in "AB":
            a.mark(tk, 1.66)                                                     # -17% (손절 아래로 급락한 경우)
        self.assertIsNone(a.submit(sig("C", 2.0, 1.86), T0, 2.0))              # 손실 중: 위험 여유 없음
        self.assertTrue(a.check_account(T0 + MIN))
        self.assertIn("-4%", a.st["locked"]["why"])


class Exits(unittest.TestCase):
    def test_bar_exit(self):
        p = dict(SL=1.90, TP=2.30)
        self.assertEqual(bar_exit(p, 2.0, 2.31, 1.95, True)[1], "익절")
        self.assertEqual(bar_exit(p, 2.0, 2.10, 1.89, True)[1], "손절")
        self.assertTrue(bar_exit(p, 2.0, 2.31, 1.89, True, "cons")[2])           # 모호 -> 보수적 손절
        self.assertEqual(bar_exit(p, 2.0, 2.31, 1.89, True, "cons")[0], 1.90)
        self.assertEqual(bar_exit(p, 2.0, 2.31, 1.89, True, "opt")[0], 2.30)
        self.assertEqual(bar_exit(p, 1.80, 1.85, 1.75, False)[0], 1.80)          # 갭 하락 -> 시가 체결
        self.assertIsNone(bar_exit(p, 2.0, 2.1, 1.95, True))

    def test_early_exits_each(self):
        W, df = make(tail="down")
        F, S = frame(df, W)
        base = dict(ej=CROSS + 1, R=0.05, avg=2.66, mfe=2.70)
        c = cfg0()
        found = {}
        for j in range(CROSS + 1, len(F)):
            for k in c["early_exits"]:
                only = cfg0(early_exits={x: x == k for x in c["early_exits"]})
                r = early_exit(F, j, base, only, (j - CROSS) * 1.0)
                if r and k not in found:
                    found[k] = (j, r)
        for k in ("vwap_2close", "ema_turn_below", "ema_cross_down", "no_progress"):
            self.assertIn(k, found)
        self.assertIn("VWAP 아래 2봉", found["vwap_2close"][1])
        off = cfg0(early_exits={x: False for x in c["early_exits"]})
        self.assertIsNone(early_exit(F, len(F) - 1, base, off, 60))

    def test_flat_sideways(self):
        W, df = make()
        F, S = frame(df, W)
        F = F.copy()
        F.iloc[-6:, F.columns.get_loc("ema")] = 2.9
        F.iloc[-6:, F.columns.get_loc("High")] = 2.91
        F.iloc[-6:, F.columns.get_loc("Low")] = 2.90
        F.iloc[-6:, F.columns.get_loc("Close")] = 2.905
        p = dict(ej=len(F) - 6, R=0.05, avg=2.8, mfe=3.0)
        r = early_exit(F, len(F) - 1, p, cfg0(early_exits={"flat_sideways": True}), 5)
        self.assertIn("평평", r)


class Simulation(unittest.TestCase):
    def test_trade_tp(self):
        """신호 봉 마감 -> 다음 봉 시가 + 슬리피지 매수 -> 익절 지정가 체결"""
        W, df = make()
        c = cfg0()
        sigs, F, S = signals(df, W, c)
        a = sim_day(solo_cfg(c), W["open"].date(), W, {"TEST": F}, sigs, 100000.0)
        t = a.st["trades"][0]
        self.assertEqual(f"{ts(t['entry_t']):%H:%M}", "10:35")
        self.assertAlmostEqual(t["avg"], df["Open"].iloc[CROSS + 1] * 1.002, places=6)   # $1~5 슬리피지 0.2%
        self.assertEqual(t["why"], "익절")
        self.assertAlmostEqual(t["exit_px"], levels(t["avg"], sigs[0]["L"], c)["TP"])

    def test_fixed_exit_same_entry(self):
        """비교 D: 고정 ±7% 도 같은 신호·같은 진입 (진입 필터는 눌림 저점 기준 손절폭)"""
        W, df = make()
        c = cfg0()
        sigs, F, S = signals(df, W, c)
        a = sim_day(solo_cfg(cfg0(exit_style="fixed")), W["open"].date(), W, {"TEST": F}, sigs, 100000.0)
        b = sim_day(solo_cfg(c), W["open"].date(), W, {"TEST": F}, sigs, 100000.0)
        self.assertEqual(a.st["trades"][0]["entry_t"], b.st["trades"][0]["entry_t"])
        self.assertAlmostEqual(a.st["trades"][0]["SL"], round(a.st["trades"][0]["avg"] * 0.93, 2))   # 고정 %: 가까운 호가

    def test_trade_early_exit(self):
        W, df = make(tail="down")
        c = cfg0()
        sigs, F, S = signals(df, W, c)
        a = sim_day(solo_cfg(c), W["open"].date(), W, {"TEST": F}, sigs, 100000.0)
        t = a.st["trades"][0]
        self.assertTrue(t["why"].startswith("조기") or t["why"].startswith("손절"))
        self.assertLess(t["net_pct"], 0)

    def test_participation_partial(self):
        W, df = make()
        c = cfg0(participation_pct=1.0)                                          # 1분 거래량 1% 만 체결
        sigs, F, S = signals(df, W, c)
        a = sim_day(c, W["open"].date(), W, {"TEST": F}, sigs, 10000.0)
        o = a.st["orders"][0]
        self.assertGreater(o["filled"], 0)
        self.assertLess(o["filled"], o["qty"])                                   # 2분 안에 다 못 삼 -> 부분 체결
        self.assertEqual(o["status"], "partial")


class TelegramButtons(unittest.TestCase):
    """체결 알림의 [즉시 청산] [절반 청산] 버튼 처리 (네트워크 없이)"""

    def setUp(self):
        import scanner
        self.calls = []
        self._api, self._conf = fl.tg_api, scanner.telegram_conf
        fl.tg_api = lambda m, p, timeout=15: self.calls.append((m, p)) or {"ok": True}
        scanner.telegram_conf = lambda: ("TOKEN", "123")
        self.bot = fl.LiveFL(cfg0())
        self.bot.acct = acct()
        self.alerts = []
        self.bot.acct.alert = lambda text, buttons=None: self.alerts.append((text, buttons))
        o = self.bot.acct.submit(dict(sig("ABC"), name="Abc Corp", chg=30.0, rvol=8.0), T0, 2.0)
        self.bot.acct.fill_buy(o, 2.0, T0)

    def tearDown(self):
        import scanner
        fl.tg_api, scanner.telegram_conf = self._api, self._conf

    def press(self, data, chat=123):
        self.bot.handle_update({"update_id": 1, "callback_query": {"id": "q", "data": data,
                                "message": {"message_id": 9, "chat": {"id": chat}}}}, T0 + MIN)

    def test_fill_alert_has_buttons(self):
        text, buttons = self.alerts[0]
        self.assertIn("매수 신호", text)
        self.assertIn("Abc Corp", text)
        self.assertEqual([d for _, d in buttons[0]], [f"exit|{D}|ABC", f"half|{D}|ABC"])

    def test_exit_button(self):
        self.press(f"exit|{D}|ABC")
        self.assertEqual(self.bot.acct.st["pos"]["ABC"]["exit_flag"], "수동 청산 (텔레그램 버튼)")
        self.assertIn("answerCallbackQuery", [m for m, _ in self.calls])

    def test_half_button(self):
        q = self.bot.acct.st["pos"]["ABC"]["qty"]
        self.press(f"half|{D}|ABC")
        p = self.bot.acct.st["pos"]["ABC"]
        self.assertEqual(p["manual_sell"], q // 2)
        self.assertIsNone(p["exit_flag"])

    def test_button_cancels_rest_of_buy(self):
        a = self.bot.acct
        o = a.submit(dict(sig("XYZ"), name=""), T0, 2.0)
        a.fill_buy(o, 2.0, T0, cap=100)                                          # 부분 체결 중
        self.press(f"half|{D}|XYZ")
        self.assertEqual(o["status"], "partial")
        self.assertEqual(a.open_buys(), [])

    def test_other_chat_and_closed(self):
        self.press(f"exit|{D}|ABC", chat=999)                                     # 다른 채팅방 -> 무시
        self.assertIsNone(self.bot.acct.st["pos"]["ABC"]["exit_flag"])
        self.assertEqual(self.calls, [])
        self.press(f"exit|2026-11-23|ABC")                                        # 지난날 버튼 -> 처리 안 함
        self.assertIsNone(self.bot.acct.st["pos"]["ABC"]["exit_flag"])
        self.assertIn("이미 청산", self.calls[0][1]["text"])


class Data(unittest.TestCase):
    def test_rvol_profile(self):
        """과거 10일 같은 시각 누적 거래량 평균 (5분 1천 주 -> 개장 30분 = 6천 주)"""
        idx = []
        for d in pd.bdate_range("2026-11-09", "2026-11-23"):
            if market_hours(d):
                idx += list(pd.date_range(pd.Timestamp(f"{d.date()} 09:30").tz_localize("America/New_York"),
                                          periods=78, freq="5min"))
        h5 = pd.DataFrame(dict(Close=1.0, Volume=1000.0), index=pd.DatetimeIndex(idx))
        prof = rvol_profile(h5, pd.Timestamp(D).date(), cfg0())
        self.assertAlmostEqual(prof[30], 6000.0)
        self.assertAlmostEqual(prof[32], 6400.0)
        self.assertIsNone(rvol_profile(h5.iloc[:78 * 3], pd.Timestamp(D).date(), cfg0()))   # 3일뿐 -> 부족

    def test_halt_parse(self):
        xml = ("<rss><item><ndaq:HaltDate>11/24/2026</ndaq:HaltDate><ndaq:HaltTime>10:05:00.000</ndaq:HaltTime>"
               "<ndaq:IssueSymbol>ABC</ndaq:IssueSymbol><ndaq:ReasonCode>LUDP</ndaq:ReasonCode>"
               "<ndaq:ResumptionDate>11/24/2026</ndaq:ResumptionDate><ndaq:ResumptionTradeTime>10:10:00</ndaq:ResumptionTradeTime>"
               "</item><item><ndaq:HaltDate>11/24/2026</ndaq:HaltDate><ndaq:HaltTime>09:00:00.000</ndaq:HaltTime>"
               "<ndaq:IssueSymbol>XYZ</ndaq:IssueSymbol><ndaq:ReasonCode>T1</ndaq:ReasonCode>"
               "<ndaq:ResumptionDate></ndaq:ResumptionDate><ndaq:ResumptionTradeTime></ndaq:ResumptionTradeTime></item></rss>")
        now = pd.Timestamp(f"{D} 10:07").tz_localize("America/New_York")
        h = fl.parse_halts(xml, now)
        self.assertEqual(set(h), {"ABC", "XYZ"})
        self.assertIn("LUDP", h["ABC"])
        self.assertEqual(set(fl.parse_halts(xml, now + 10 * MIN)), {"XYZ"})     # ABC 는 10:10 재개


if __name__ == "__main__":
    unittest.main(verbosity=2)
