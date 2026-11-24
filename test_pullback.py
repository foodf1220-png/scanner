# -*- coding: utf-8 -*-
"""pullback.py 검증 사례 (python test_pullback.py)
요청서 9번 '반드시 통과해야 하는 검증 사례' + 경계 사례. 네트워크·텔레그램 없이 가짜 5분봉으로 확인"""
import copy
import json
import unittest

import pandas as pd

import pullback as pb

ET = "America/New_York"
DAY = "2026-11-19"
PRIOR = ["2026-11-16", "2026-11-17", "2026-11-18"]          # 224 EMA 용 이전 거래일 (78봉 x 3)

# 오늘 5분봉 (시, 고, 저, 종) 09:30 부터
BARS = [
    (2.70, 2.72, 2.68, 2.71),   # 0  급등 시작 저점 2.68
    (2.71, 2.80, 2.70, 2.79),
    (2.79, 3.00, 2.78, 2.99),
    (2.99, 3.40, 2.98, 3.38),
    (3.38, 3.80, 3.37, 3.78),
    (3.78, 4.00, 3.77, 3.95),   # 5  급등 고점 $4.00 (+49%)
    (3.95, 3.96, 3.66, 3.68),
    (3.68, 3.70, 3.52, 3.55),
    (3.55, 3.57, 3.30, 3.32),
    (3.32, 3.34, 3.08, 3.10),   # 9  고점 대비 -23% -> 눌림 후보
    (3.10, 3.12, 2.92, 2.95),
    (2.95, 2.97, 2.80, 2.86),   # 11 눌림 저점 $2.80 (-30%)
    (2.86, 2.89, 2.83, 2.85),
    (2.85, 2.93, 2.85, 2.91),   # 13 양봉이지만 직전 2봉 고가(2.97) 못 넘음
    (2.91, 2.93, 2.90, 2.91),
    (2.91, 3.01, 2.90, 2.98),   # 15 C: 양봉, 2.98 > 2.93·2.93, EMA 이상 ~ +5% 이내 / 고가 3.01 = H1
    (2.98, 3.00, 2.95, 2.96),   # 16 종가 하락 -> H1 $3.01 확정
    (2.96, 2.97, 2.85, 2.87),   # 17 중간 저점 $2.85 (H1 대비 -5.3%)
    (2.87, 2.93, 2.86, 2.92),
    (2.92, 3.01, 2.91, 2.99),   # 19 고가 3.01 = H1 ±1% -> H2 후보
    (2.99, 3.00, 2.94, 2.95),   # 20 종가 하락 -> H2 $3.01 확정, S = $2.85
    (2.95, 2.96, 2.90, 2.91),
    (2.91, 2.92, 2.85, 2.86),   # 22 저가 2.85 = S ±1% -> 재접근
    (2.86, 2.90, 2.85, 2.89),   # 23 B: 양봉 2.89 > 직전 종가 2.86 > S
]


def make_raw(bars, prior=True, prior_px=2.85):
    idx, rows = [], []
    if prior:
        for d in PRIOR:
            for t in pd.date_range(f"{d} 09:30", periods=78, freq="5min", tz=ET):
                idx.append(t)
                rows.append((prior_px,) * 4 + (1000,))
    t0 = pd.Timestamp(f"{DAY} 09:30", tz=ET)
    for k, b in enumerate(bars):
        idx.append(t0 + pd.Timedelta(minutes=5 * k))
        rows.append(tuple(b) + (1000,))
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx), columns=["Open", "High", "Low", "Close", "Volume"])


def cfg():
    """요청서 원안 (C 알림만 -> B 매수, ±7%, 60분) 으로 검증. C 진입·±4%·30분은 C진입 테스트에서 따로"""
    return dict(copy.deepcopy(pb.DEFAULTS), entry_signal="B", tp_pct=7.0, sl_pct=7.0, max_hold_min=60)


def cfg_c():
    return copy.deepcopy(pb.DEFAULTS)          # 지금 설정: C 봉에서 바로 매수, +4% / -7%, 30분, $0.5 이상


def today5(raw, c):
    d5 = pb.prepare_5m(raw, c)
    return d5[d5.index.date == pd.Timestamp(DAY).date()]


def new_core(c=None, alerts=None, cash=10000.0):
    alerts = [] if alerts is None else alerts
    return pb.Core(c or cfg(), dict(day=DAY, start_eq=cash, cash=cash), alert=alerts.append, mode="live"), alerts


def feed(core, tk, d5, upto=None):
    """실시간처럼 봉이 하나씩 마감될 때마다 처리"""
    out = []
    for k in range(1, (upto or len(d5)) + 1):
        out += core.process_5m(tk, d5.iloc[:k], None)
    return out


def fake_b(tk, t_end="2026-11-19T10:00:00-05:00", h1=1.2, close=1.0):
    return dict(type="B", tk=tk, pid=f"{tk}@x", t="2026-11-19T09:55:00-05:00", t_end=t_end, fresh=True,
                h1=h1, h2=h1, S=0.95, re_low=0.95, close=close)


T_FILL = pd.Timestamp("2026-11-19 10:01", tz=ET)


def fill_all(core, tks, px=1.0):
    core.on_minute(T_FILL, {tk: (px, px, px, px) for tk in tks}, "close")


class 검증사례(unittest.TestCase):
    def test_1_C알림_1회_매수_0회(self):
        c = cfg()
        core, alerts = new_core(c)
        d5 = today5(make_raw(BARS), c)
        b = feed(core, "TEST", d5, upto=23)                 # B 직전 봉까지
        rows = core.st["rows"]
        cs = [r for r in rows if r["type"] == "C"]
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0]["peak"], 4.00)
        self.assertEqual(cs[0]["pb_low"], 2.80)
        self.assertAlmostEqual(cs[0]["pb_pct"], 30.0, places=6)
        self.assertEqual(pd.Timestamp(cs[0]["t"]).strftime("%H:%M"), "10:45")     # 15번째 봉
        self.assertTrue(cs[0]["ema"] <= cs[0]["close"] <= cs[0]["ema"] * 1.05)
        self.assertEqual(b, [])
        self.assertEqual(len(core.st["orders"]), 0)
        self.assertEqual(sum(a.startswith("🟡 C 알림") for a in alerts), 1)
        self.assertEqual(len(alerts), 1)

    def test_2_B신호_1회_주문_1회(self):
        c = cfg()
        core, alerts = new_core(c)
        d5 = today5(make_raw(BARS), c)
        b = feed(core, "TEST", d5)
        self.assertEqual(len(b), 1)
        e = b[0]
        self.assertEqual((e["h1"], e["h2"], e["S"], e["re_low"], e["close"]), (3.01, 3.01, 2.85, 2.85, 2.89))
        oid = core.submit_b(e, pd.Timestamp(e["t_end"]), e["close"], pd.Timestamp(e["t_end"]))
        self.assertIsNotNone(oid)
        self.assertEqual(len(core.st["orders"]), 1)
        self.assertEqual(sum(a.startswith("🔔 B 신호") for a in alerts), 1)
        # 신호봉 마감 이후 시세로 체결 (신호봉 종가 2.89 가 아니라 다음 시세 2.90)
        core.on_minute(pd.Timestamp(e["t_end"]), {"TEST": (2.89, 2.91, 2.88, 2.90)}, "close")
        p = core.st["pos"]["TEST"]
        self.assertAlmostEqual(p["avg"], 2.90 * (1 + c["slippage_pct"] / 100))
        self.assertAlmostEqual(p["tp"], p["avg"] * 1.07)
        self.assertAlmostEqual(p["sl"], p["avg"] * 0.93)
        self.assertLessEqual(p["cost"], 10000 * 0.25 + 1e-6)

    def test_3_H2확정후_S아래_종가면_매수_0회(self):
        c = cfg()
        bars = list(BARS)
        bars[22] = (2.91, 2.92, 2.83, 2.84)                 # H2 확정 후 2.84 종가 (S 2.85 아래)
        bars[23] = (2.84, 2.90, 2.84, 2.89)                 # 이후 2.89 반등해도
        core, alerts = new_core(c)
        b = feed(core, "TEST", today5(make_raw(bars), c))
        self.assertEqual(b, [])
        self.assertTrue(any(r["type"] == "CANCEL" and "S" in r["why"] for r in core.st["rows"]))
        self.assertEqual(len(core.st["orders"]), 0)

    def test_4_보유미체결_4종목이면_5번째_B는_기록만(self):
        core, alerts = new_core()
        for tk in ("AAA", "BBB", "CCC", "DDD"):
            self.assertIsNotNone(core.submit_b(fake_b(tk), T_FILL, 1.0, T_FILL))
        fill_all(core, ["AAA", "BBB"])                      # 2종목 보유 + 2종목 미체결 = 4자리
        self.assertEqual(len(core.st["pos"]), 2)
        self.assertEqual(core.slots_used(), 4)
        n_orders = len(core.st["orders"])
        self.assertIsNone(core.submit_b(fake_b("EEE"), T_FILL, 1.0, T_FILL))
        self.assertEqual(len(core.st["orders"]), n_orders)
        self.assertIn("보유 한도 초과", core.st["rows"][-1]["result"])
        self.assertEqual(core.st["missed"]["slots"], 1)
        self.assertIn("보유 한도 초과", alerts[-1])

    def _lock_case(self, px, sign):
        core, alerts = new_core()
        tks = ["AAA", "BBB", "CCC", "DDD"]
        for tk in tks:
            core.submit_b(fake_b(tk), T_FILL, 1.0, T_FILL)
        fill_all(core, tks)
        self.assertEqual(len(core.st["pos"]), 4)
        core.on_minute(T_FILL + pd.Timedelta(minutes=1), {tk: (px, px, px, px) for tk in tks}, "close")
        lk = core.st["locked"]
        self.assertIsNotNone(lk)
        self.assertTrue(lk["why"].startswith(f"계좌 {sign}3%"))
        self.assertEqual(core.st["pos"], {})               # 보유 전량 청산, 최종 수량 0
        self.assertEqual(lk["left"], [])
        self.assertEqual(len(core.st["trades"]), 4)
        n_orders = len(core.st["orders"])
        self.assertIsNone(core.submit_b(fake_b("EEE", t_end="2026-11-19T10:05:00-05:00"), T_FILL, 1.0, T_FILL))
        self.assertEqual(len(core.st["orders"]), n_orders)  # 신규 주문 0
        self.assertIn("거래 잠금", core.st["rows"][-1]["result"])
        core.on_minute(T_FILL + pd.Timedelta(minutes=2), {tk: (1.0,) * 4 for tk in tks}, "close")
        self.assertIsNotNone(core.st["locked"])            # ±3% 안으로 돌아와도 잠금 유지
        self.assertEqual(sum(a.startswith("⏹") for a in alerts), 1)

    def test_5a_계좌_플러스3_도달_전량청산_신규0(self):
        self._lock_case(1.045, "+")                         # 종목 +4.5% (익절 +7% 전), 계좌 +3% 넘김

    def test_5b_계좌_마이너스3_도달_전량청산_신규0(self):
        self._lock_case(0.955, "-")                         # 종목 -4.5% (손절 -7% 전), 계좌 -3% 넘김

    def test_5c_잠금때_미체결_취소(self):
        core, alerts = new_core()
        for tk in ("AAA", "BBB", "CCC"):
            core.submit_b(fake_b(tk), T_FILL, 1.0, T_FILL)
        fill_all(core, ["AAA", "BBB", "CCC"])
        core.submit_b(fake_b("DDD"), T_FILL, 1.0, T_FILL + pd.Timedelta(minutes=5))   # 아직 미체결
        core.on_minute(T_FILL + pd.Timedelta(minutes=1), {tk: (0.94,) * 4 for tk in ("AAA", "BBB", "CCC")}, "close")
        self.assertIsNotNone(core.st["locked"])
        self.assertEqual(core.st["locked"]["cancelled"], ["DDD"])
        self.assertEqual(core.open_buys(), [])
        self.assertEqual(core.st["pos"], {})

    def test_6_같은_이벤트_재수신_재시작_중복0(self):
        c = cfg()
        core, alerts = new_core(c)
        d5 = today5(make_raw(BARS), c)
        b = feed(core, "TEST", d5)
        e = b[0]
        core.submit_b(e, pd.Timestamp(e["t_end"]), e["close"], pd.Timestamp(e["t_end"]))
        t1 = pd.Timestamp(e["t_end"])
        core.on_minute(t1, {"TEST": (2.89, 2.91, 2.88, 2.90)}, "close")
        n_alert, n_orders, n_rows = len(alerts), len(core.st["orders"]), len(core.st["rows"])
        # 같은 봉·같은 체결 시세 재수신
        self.assertEqual(core.process_5m("TEST", d5, None), [])
        core.on_minute(t1, {"TEST": (2.89, 2.91, 2.88, 2.90)}, "close")
        self.assertEqual(len(core.st["pos"]), 1)
        # 재시작: 상태를 JSON 으로 저장했다가 새로 만든 Core 로 복구
        st2 = json.loads(json.dumps(core.snapshot(), default=str))
        core2 = pb.Core(c, st2, alert=alerts.append, mode="live")
        self.assertEqual(core2.process_5m("TEST", d5, None), [])
        self.assertIsNone(core2.submit_b(dict(e), t1, e["close"], t1))          # 같은 패턴 B 재수신
        core2.on_minute(t1, {"TEST": (2.89, 2.91, 2.88, 2.90)}, "close")
        self.assertEqual(len(alerts), n_alert)
        self.assertEqual(len(core2.st["orders"]), n_orders)
        self.assertEqual(core2.st["pos"]["TEST"]["qty"], core.st["pos"]["TEST"]["qty"])
        # 익절 체결도 한 번만
        tp = core2.st["pos"]["TEST"]["tp"]
        t2 = t1 + pd.Timedelta(minutes=3)
        core2.on_minute(t2, {"TEST": (2.95, tp + 0.01, 2.94, tp)}, "close")
        core2.on_minute(t2, {"TEST": (2.95, tp + 0.01, 2.94, tp)}, "close")
        self.assertEqual(len(core2.st["trades"]), 1)
        self.assertEqual(core2.st["trades"][0]["why"], "익절")
        self.assertEqual(sum(a.startswith("✅ 매도") for a in alerts), 1)


class C진입(unittest.TestCase):
    """사용자 변경: C 알림 봉에서 바로 매수, 익절 +4% / 손절 -7%, 진입 30분 뒤 청산"""

    def test_C봉에서_매수신호_1회_주문_1회_B추적_안함(self):
        c = cfg_c()
        self.assertEqual((c["entry_signal"], c["tp_pct"], c["sl_pct"], c["max_hold_min"]), ("C", 4.0, 7.0, 30))
        core, alerts = new_core(c)
        d5 = today5(make_raw(BARS), c)
        ents = feed(core, "TEST", d5)
        self.assertEqual([e["type"] for e in ents], ["C"])
        e = ents[0]
        self.assertEqual(pd.Timestamp(e["t"]).strftime("%H:%M"), "10:45")
        self.assertEqual((e["peak"], e["pb_low"], e["close"]), (4.00, 2.80, 2.98))
        self.assertIsNotNone(core.submit_b(e, pd.Timestamp(e["t_end"]), e["close"], pd.Timestamp(e["t_end"])))
        self.assertEqual(sum(a.startswith("🟡 C 매수 신호") for a in alerts), 1)
        self.assertFalse(any(r["type"] in ("H1", "H2", "RE", "B") for r in core.st["rows"]))   # B자리 추적 안 함
        self.assertEqual(sum(r["type"] == "C" for r in core.st["rows"]), 1)                  # 기록도 1줄
        core.on_minute(pd.Timestamp(e["t_end"]), {"TEST": (2.98, 2.99, 2.97, 2.99)}, "close")
        p = core.st["pos"]["TEST"]
        self.assertAlmostEqual(p["tp"], p["avg"] * 1.04)
        self.assertAlmostEqual(p["sl"], p["avg"] * 0.93)
        self.assertIsNone(p["to_h1"])

    def test_C진입_30분_지나면_청산_29분은_보유(self):
        c = cfg_c()
        core = pb.Core(c, dict(day=DAY, start_eq=10000.0, cash=10000.0), mode="cons")
        core.submit_b(dict(fake_b("AAA"), type="C", h1=None, peak=1.3, pb_low=0.98, pb_pct=24.6, ema=0.97), T_FILL, 1.0, T_FILL)
        core.on_minute(T_FILL, {"AAA": (1.0,) * 4}, "open")
        core.on_minute(T_FILL + pd.Timedelta(minutes=29), {"AAA": (1.01, 1.03, 0.95, 1.02)}, "open")   # +4% · -6% 안
        self.assertIn("AAA", core.st["pos"])
        core.on_minute(T_FILL + pd.Timedelta(minutes=30), {"AAA": (1.02, 1.03, 1.01, 1.02)}, "open")
        self.assertEqual(core.st["trades"][0]["why"], "보유 30분 청산")

    def test_C진입_익절4_손절7(self):
        for row, why in (((1.0, 1.05, 0.99, 1.04), "익절"), ((1.0, 1.01, 0.95, 0.96), "보유"), ((1.0, 1.01, 0.93, 0.95), "손절")):
            core = pb.Core(cfg_c(), dict(day=DAY, start_eq=10000.0, cash=10000.0), mode="cons")
            core.submit_b(dict(fake_b("AAA"), type="C", h1=None, peak=1.3, pb_low=0.98, pb_pct=24.6, ema=0.97), T_FILL, 1.0, T_FILL)
            core.on_minute(T_FILL, {"AAA": (1.0,) * 4}, "open")
            core.on_minute(T_FILL + pd.Timedelta(minutes=5), {"AAA": row}, "open")
            if why == "보유":                                   # -5% 는 손절(-7%) 전이라 계속 보유
                self.assertIn("AAA", core.st["pos"])
                self.assertEqual(core.st["trades"], [])
                continue
            t = core.st["trades"][0]
            self.assertEqual(t["why"], why)
            self.assertAlmostEqual(t["target"], t["avg"] * (1.04 if why == "익절" else 0.93), places=6)

    def test_주가_0점5달러_미만이면_C_매수신호_없음(self):
        c = cfg_c()
        self.assertEqual(c["min_price"], 0.5)
        small = [tuple(round(x * 0.1, 4) for x in b) for b in BARS]          # 같은 모양, 가격만 1/10 (C봉 종가 $0.298)
        core, alerts = new_core(c)
        self.assertEqual(feed(core, "TEST", today5(make_raw(small, prior_px=0.285), c)), [])
        core, alerts = new_core(c)                                           # 원래 가격($2.98)이면 그대로 나옴
        self.assertEqual(len(feed(core, "TEST", today5(make_raw(BARS), c))), 1)

    def test_C진입_재시작_중복0(self):
        c = cfg_c()
        core, alerts = new_core(c)
        d5 = today5(make_raw(BARS), c)
        e = feed(core, "TEST", d5)[0]
        core.submit_b(e, pd.Timestamp(e["t_end"]), e["close"], pd.Timestamp(e["t_end"]))
        n = len(alerts)
        core2 = pb.Core(c, json.loads(json.dumps(core.snapshot(), default=str)), alert=alerts.append, mode="live")
        self.assertEqual(core2.process_5m("TEST", d5, None), [])
        self.assertIsNone(core2.submit_b(dict(e), pd.Timestamp(e["t_end"]), e["close"], pd.Timestamp(e["t_end"])))
        self.assertEqual(len(alerts), n)
        self.assertEqual(len(core2.st["orders"]), 1)


class 경계사례(unittest.TestCase):
    def test_EMA_부족하면_건너뜀(self):
        c = cfg()
        core, alerts = new_core(c)
        feed(core, "TEST", today5(make_raw(BARS, prior=False), c))
        self.assertTrue(any(r["type"] == "SKIP" and "EMA" in r["why"] for r in core.st["rows"]))
        self.assertFalse(any(r["type"] == "C" for r in core.st["rows"]))

    def test_눌림_40퍼_초과면_후보_아님(self):
        c = cfg()
        bars = list(BARS)
        bars[9] = (3.32, 3.34, 2.30, 2.40)                  # 한 봉에 고점 대비 -42.5%
        core, _ = new_core(c)
        feed(core, "TEST", today5(make_raw(bars), c))
        self.assertTrue(any(r["type"] == "NOCAND" for r in core.st["rows"]))
        self.assertFalse(any(r["type"] == "C" for r in core.st["rows"]))

    def test_반등전_40퍼_초과_하락하면_후보_취소(self):
        c = cfg()
        bars = list(BARS)
        bars[12] = (2.86, 2.89, 2.35, 2.40)                 # 후보 등록 뒤, C 전에 -41%
        core, _ = new_core(c)
        feed(core, "TEST", today5(make_raw(bars), c))
        self.assertTrue(any(r["type"] == "CANCEL" and "40%" in r["why"] for r in core.st["rows"]))

    def test_H2가_H1_1퍼_초과면_취소(self):
        c = cfg()
        bars = list(BARS)
        bars[19] = (2.92, 3.06, 2.91, 2.99)                 # 3.06 > 3.01 x 1.01
        core, _ = new_core(c)
        b = feed(core, "TEST", today5(make_raw(bars), c))
        self.assertEqual(b, [])
        self.assertTrue(any(r["type"] == "CANCEL" and "+1%" in r["why"] for r in core.st["rows"]))

    def test_백테스트_경로에서도_B가_기록됨(self):
        """백테스트·한 번 검색은 주문 단계 없이 process_5m 반환값으로 B 를 모음 -> 기록에 B 가 있어야 함"""
        c = cfg()
        core = pb.Core(c, dict(day=DAY), mode="cons")
        bs = core.process_5m("TEST", today5(make_raw(BARS), c), None)
        core.st["rows"] += [dict(b, result="백테스트") for b in bs]
        core.end_day(pd.Timestamp(f"{DAY} 16:00", tz=ET))
        types = [r["type"] for r in core.st["rows"]]
        self.assertEqual(types.count("B"), 1)
        for k in ("CAND", "C", "H1", "H2", "RE"):
            self.assertIn(k, types)

    def test_거래일_끝나면_진행중_패턴_만료(self):
        c = cfg()
        core, _ = new_core(c)
        feed(core, "TEST", today5(make_raw(BARS), c), upto=18)       # H2 대기 중
        core.end_day(pd.Timestamp(f"{DAY} 16:00", tz=ET))
        self.assertEqual(core.trk["TEST"].ph, "IDLE")
        self.assertEqual(core.st["rows"][-1]["type"], "EXPIRE")
        self.assertIn("거래일 끝", core.st["rows"][-1]["why"])

    def test_진입_60분_지나면_청산(self):
        for mode, field, px_expect in (("cons", "open", 1.02), ("live", "close", 1.03)):
            c = cfg()
            core = pb.Core(c, dict(day=DAY, start_eq=10000.0, cash=10000.0), mode=mode)
            core.submit_b(fake_b("AAA"), T_FILL, 1.0, T_FILL)
            core.on_minute(T_FILL, {"AAA": (1.0,) * 4}, field)
            core.on_minute(T_FILL + pd.Timedelta(minutes=59), {"AAA": (1.01, 1.02, 1.0, 1.01)}, field)
            self.assertIn("AAA", core.st["pos"])                     # 59분: 아직 보유
            core.on_minute(T_FILL + pd.Timedelta(minutes=60), {"AAA": (1.02, 1.04, 1.01, 1.03)}, field)
            self.assertEqual(core.st["pos"], {})
            t = core.st["trades"][0]
            self.assertEqual(t["why"], "보유 60분 청산")
            self.assertAlmostEqual(t["trigger"], px_expect)        # 백테스트 = 그 분 시가, 실시간 = 현재가

    def test_60분_청산은_시세_없는_종목도(self):
        core, _ = new_core()
        core.submit_b(fake_b("AAA"), T_FILL, 1.0, T_FILL)
        fill_all(core, ["AAA"])
        core.on_minute(T_FILL + pd.Timedelta(minutes=61), {"ZZZ": (5.0,) * 4}, "close")   # AAA 시세 없음
        self.assertEqual(core.st["trades"][0]["why"], "보유 60분 청산")

    def test_한국_0230_전량청산_시각(self):
        c = cfg()
        _, _, flat = pb.session_times(pd.Timestamp("2026-11-24").date(), c)      # 표준시
        self.assertEqual(flat.strftime("%H:%M"), "12:30")
        self.assertEqual(flat.tz_convert("Asia/Seoul").strftime("%m-%d %H:%M"), "11-25 02:30")
        _, _, flat = pb.session_times(pd.Timestamp("2026-07-15").date(), c)      # 서머타임
        self.assertEqual(flat.strftime("%H:%M"), "13:30")
        self.assertEqual(flat.tz_convert("Asia/Seoul").strftime("%H:%M"), "02:30")

    def test_실시간_신호모드는_칸_제한_없음(self):
        core = pb.Core(pb.signal_cfg(cfg()), dict(day=DAY, start_eq=1e9, cash=1e9), mode="live")
        for k in range(8):
            self.assertIsNotNone(core.submit_b(fake_b(f"T{k}"), T_FILL, 1.0, T_FILL))
        self.assertIn("매수 신호", core.st["rows"][-1]["result"])

    def test_동시_신호_처리순서_고정(self):
        core, _ = new_core()
        for tk in ("AAA", "BBB", "CCC"):
            core.submit_b(fake_b(tk), T_FILL, 1.0, T_FILL)
        sigs = [fake_b("ZZZ"), fake_b("MMM")]
        for e in pb.signal_order(sigs):                     # 같은 시각이면 종목 코드 순 -> MMM 이 마지막 자리
            core.submit_b(e, T_FILL, 1.0, T_FILL)
        self.assertEqual(sorted(o["tk"] for o in core.open_buys()), ["AAA", "BBB", "CCC", "MMM"])

    def test_같은_분봉_익절손절_둘다면_보수는_손절_낙관은_익절(self):
        for mode, why in (("cons", "손절"), ("opt", "익절")):
            c = cfg()
            core = pb.Core(c, dict(day=DAY, start_eq=10000.0, cash=10000.0), mode=mode)
            core.submit_b(fake_b("AAA"), T_FILL, 1.0, T_FILL)
            core.on_minute(T_FILL, {"AAA": (1.0, 1.0, 1.0, 1.0)}, "open")
            core.on_minute(T_FILL + pd.Timedelta(minutes=1), {"AAA": (1.0, 1.2, 0.8, 1.0)}, "open")
            self.assertEqual(core.st["trades"][0]["why"], why)
            self.assertTrue(core.st["trades"][0]["amb"])
            self.assertEqual(core.st["unknown"]["tpsl"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
