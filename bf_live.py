# -*- coding: utf-8 -*-
"""
bf_live.py - Bull Flag V2 실시간 신호 (서버 bf-live.service, 평일 뉴욕 09:20 시작 ~ 장 마감 10분 전 종료)

[조건] V2 + 진입가 $2 이상 + 익절 +5% / 손절 -3% (사용자 지정, bf_config.json)
  검색기 (2분마다 전 종목): 장중 고가 >= 전일 종가 +10%, $0.5~20, 유통주식수 2,000만 주 미만, RVOL 3배(14거래일 같은 시각)
  1분 완성봉 상태 기계 (bf_core.detect): 깃대 5봉 +5% -> 눌림 1~4봉(되돌림 50%·VWAP 위·눌림 거래량 <= 깃대 60%) -> READY
  돌파 (5초마다): 현재가 > 매수 기준가 (직전 조정 음봉 고가), 기준가 +0.5% 이내, 현재가 > 9EMA > VWAP,
                진입가 $1~2, 뉴욕 09:31~15:30, 깃대마다 첫 돌파 1번
  -> state/fl_signal_queue.jsonl 에 strategy=BF 로 기록 -> 토스 자동매수 봇(bot.py)이 자동 승인(3종목·30%·현금 10%)
  -> monitor.py 가 즉시 시장가 매수, 손절 -4% / 익절 +4% 조건주문, 장 마감 10분 전 청산
[실행] python bf_live.py          실시간 (서비스)
       python bf_live.py --check  최근 장으로 한 번 검색만 (신호 안 씀)
"""
import json
import logging
import os
import sys
import time
import traceback

import numpy as np
import pandas as pd

import bf_core as BC
import fl_core as FC
from fl_core import KST_S, MIN, clk, day_windows, iso, now_et, regular_1m, round_near

QUEUE = os.path.join(FC.STATE_DIR, "fl_signal_queue.jsonl")
FLOAT_FILE = os.path.join(FC.HERE, "cache", "bf_float.json")
SCAN_SEC, HOT_SEC = 120, 5


def _pb():
    import pullback
    return pullback


class BFLive:
    def __init__(self, cfg, check=False):
        self.c, self.check = cfg, check
        self.lo, self.hi = float(cfg["entry_min_price"]), float(cfg["entry_max_price"])   # 진입가 범위 (bf_config.json)
        self.cands, self.ready, self.fired, self.uni = {}, {}, set(), {}
        self.floats = json.load(open(FLOAT_FILE, encoding="utf-8")) if os.path.exists(FLOAT_FILE) else {}
        self.logf = None
        self.last_min = None
        self.next_scan = None

    def log(self, msg):
        n = now_et()
        line = f"[{n:%H:%M:%S}ET {n.tz_convert(KST_S):%H:%M}KST] {msg}"
        print(line, flush=True)
        if self.logf:
            self.logf.write(line + "\n")
            self.logf.flush()

    def float_of(self, tk):
        f = self.floats.get(tk) or {}
        return f.get("float") or f.get("shares")

    def fetch_floats(self, tks, limit=40):
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        n = 0
        for tk in tks:
            if tk in self.floats or n >= limit:
                continue
            n += 1
            try:
                i = yf.Ticker(tk).info
                self.floats[tk] = dict(float=i.get("floatShares"), shares=i.get("sharesOutstanding"))
            except Exception:
                pass
            time.sleep(0.3)
        if n:
            os.makedirs(os.path.dirname(FLOAT_FILE), exist_ok=True)
            json.dump(self.floats, open(FLOAT_FILE, "w"))

    def scan(self, now, W):
        cfg, pb = self.c, _pb()
        try:
            quotes, _ = pb.screen_all()
        except Exception as e:
            self.log(f"전 종목 검색 실패 ({type(e).__name__}) - 기존 후보로 계속")
            return
        d = W["open"].date()
        found = []
        for x in quotes:
            s = x.get("symbol")
            if s not in self.uni or pb.epoch_date(x.get("regularMarketTime")) != d:
                continue
            hi, lo, pc = x.get("regularMarketDayHigh"), x.get("regularMarketDayLow"), x.get("regularMarketPreviousClose")
            if hi and lo and pc and hi >= pc * (1 + cfg["min_change_pct"] / 100) and hi >= cfg["min_price"] and lo <= cfg["max_price"]:
                found.append((s, pc))
        new = [(s, pc) for s, pc in found if s not in self.cands]
        if not new:
            return
        self.fetch_floats([s for s, _ in new])
        keep = [(s, pc) for s, pc in new if (self.float_of(s) or 9e18) < cfg["max_float"]]
        for s, _ in new:
            if (self.float_of(s) or 9e18) >= cfg["max_float"]:
                self.cands[s] = None                       # 유통주식 초과·정보 없음: 다시 보지 않음 (정보 생기면 다음 날)
        if not keep:
            return
        tks = [s for s, _ in keep]
        h5 = pb.download(tks, period="1mo", interval="5m", prepost=False)
        daily = pb.download(tks, period="2mo", interval="1d")
        for s, pc in keep:
            dctx = FC.daily_context(daily.get(s), d, cfg)
            self.cands[s] = dict(pc=pc, h5=h5.get(s), prof=FC.rvol_profile(h5.get(s), d, cfg),
                                 adv=(dctx or {}).get("adv"), name=self.uni.get(s, ""))
        self.log(f"감시 추가 {len(keep)}종목 (유통 {cfg['max_float'] / 1e6:g}백만 미만): {', '.join(tks[:12])}"
                 + (" …" if len(tks) > 12 else "") + f" | 감시 {sum(1 for v in self.cands.values() if v)}종목")

    def minute(self, now, W, period="1d"):
        """1분봉 마감마다: 후보 전체 상태 기계 -> READY 목록 (매수 기준가)"""
        cfg = self.c
        live = [s for s, v in self.cands.items() if v]
        if not live:
            self.ready = {}
            return
        data = _pb().download(live, period=period, interval="1m", prepost=False)
        cut = now - pd.Timedelta(seconds=5)
        ready = {}
        for tk in live:
            v = self.cands[tk]
            try:                                                   # 한 종목 오류가 전체를 멈추지 않게
                bars = regular_1m(data.get(tk), W)
                if bars.empty:                                     # 1분봉을 못 받은 종목 (빈 표는 시각 계산 불가)
                    continue
                done = bars[bars.index + MIN <= cut]
                if len(done) < 3:
                    continue
                F = FC.build_frame(done, W, v["pc"], v["prof"], v["adv"], cfg)
                m5 = BC.m5_context(F, v["h5"], W["open"].date())
                _, _, (state, P) = BC.detect(F, cfg, W, tk, self.float_of(tk), m5, return_state=True)
            except Exception as e:
                self.log(f"{tk} 처리 오류 (건너뜀): {type(e).__name__}: {e}")
                continue
            if state != "READY":
                continue
            key = f"{tk}|{iso(F.index[P['si']])}"
            if key in self.fired:
                continue
            if not P["pb_vol"] <= cfg["pb_vol_ratio"] * P["pole_vol"]:     # V2 눌림 거래량
                continue
            if not (self.lo * 0.99 <= P["trigger"] < self.hi):
                continue
            ready[tk] = dict(P, key=key, ema=float(F["ema"].iat[-1]), vwap=float(F["vwap"].iat[-1]),
                             chg=float(F["chg"].iat[-1]), rvol=float(F["rvol"].iat[-1]) if F["rvol"].iat[-1] == F["rvol"].iat[-1] else None,
                             t_start=iso(F.index[P["si"]]))
        if set(ready) != set(self.ready):
            self.log(f"매수 대기(READY) {len(ready)}: " + ", ".join(f"{tk} 기준가 ${r['trigger']:.4g}" for tk, r in ready.items()))
        self.ready = ready

    def hot(self, now, W):
        """5초마다 READY 종목 현재가 -> 기준가 돌파면 신호 (깃대마다 1번)"""
        if not self.ready or not (W["buy_start"] <= now <= W["buy_end"]):
            return
        data = _pb().download(list(self.ready), period="1d", interval="1m", prepost=False)
        cfg = self.c
        for tk, R in list(self.ready.items()):
            bars = regular_1m(data.get(tk), W)
            if bars.empty:
                continue
            p = float(bars["Close"].iat[-1])
            T = R["trigger"]
            if p <= T:
                continue
            self.fired.add(R["key"])
            self.ready.pop(tk)
            why = None
            if p > T * (1 + cfg["max_chase_pct"] / 100):
                why = f"기준가 +{cfg['max_chase_pct']:g}% 초과 (${p:.4g} / 기준가 ${T:.4g}) - 추격 안 함"
            elif not (self.lo <= p < self.hi):
                why = f"진입가 ${p:.4g} 가 ${self.lo:g}~{self.hi:g} 밖"
            elif not (p > R["ema"] > R["vwap"]):
                why = f"현재가 > 9EMA > VWAP 아님 (현재가 {p:.4g}, EMA {R['ema']:.4g}, VWAP {R['vwap']:.4g})"
            if why:
                self.log(f"{tk} 돌파 탈락: {why}")
                continue
            self.emit(tk, R, p, now)

    def emit(self, tk, R, p, now):
        rec = dict(strategy="BF", tk=tk, name=(self.cands.get(tk) or {}).get("name", ""), mode="BF",
                   t=iso(now.floor("min")), t_sig=iso(now), t_detect=iso(now), px=round(p, 6), L=float(R["pb_low"]),
                   SL=round_near(p * (1 - self.c["sl_pct"] / 100)), TP=round_near(p * (1 + self.c["tp_pct"] / 100)),
                   sl_pct=self.c["sl_pct"], tp_pct=self.c["tp_pct"], chg=round(R["chg"], 2),
                   rvol=round(R["rvol"], 2) if R.get("rvol") else None, score=None, spread_pct=None, block=None,
                   trigger=float(R["trigger"]), pole_start=float(R["start"]), pole_high=float(R["high"]),
                   pole_pct=round((R["high"] / R["start"] - 1) * 100, 2), retr=round(float(R["retr"]), 1),
                   pb_bars=len(R["pb"]))
        self.log(f"🚩 {tk} Bull Flag 돌파 신호 ${p:.4g} (기준가 ${R['trigger']:.4g}, 깃대 +{rec['pole_pct']:.1f}%, "
                 f"되돌림 {rec['retr']:.0f}%, 눌림 {rec['pb_bars']}봉)")
        if self.check:
            return
        os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
        with open(QUEUE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def run(self):
        from scanner import load_universe
        cfg = self.c
        now = now_et()
        d = now.date()
        os.makedirs(FC.LOGS, exist_ok=True)
        self.logf = open(os.path.join(FC.LOGS, f"bf_live_{d:%Y%m%d}.log"), "a", encoding="utf-8")
        W = day_windows(d, cfg)
        if W is None:
            self.log(f"{d} 미국 휴장일 - 종료")
            return
        if now >= W["flat"]:
            self.log("오늘 장이 끝났습니다 - 종료")
            return
        self.uni = load_universe()
        self.log(f"Bull Flag V2 실시간 신호 시작 | 진입 ${self.lo:g}~{self.hi:g} · 익절 +{cfg['tp_pct']:g}% / 손절 -{cfg['sl_pct']:g}% | 신규 {clk(W['buy_start'])}~{clk(W['buy_end'])} | "
                 f"유통주식 캐시 {len(self.floats)}종목")
        start = W["open"] + MIN + pd.Timedelta(seconds=5)
        while now_et() < start:
            time.sleep(min(30, max(1, (start - now_et()).total_seconds())))
        while True:
            now = now_et()
            if now >= W["flat"]:
                self.log(f"전량 청산 시각 {clk(W['flat'])} - 신호 종료 (청산은 monitor.py)")
                break
            bar = (now - pd.Timedelta(seconds=5)).floor("min")
            steps = []                                             # 검색·1분 갱신·돌파 확인을 따로: 하나가 실패해도 나머지는 돎
            if now <= W["buy_end"] and (self.next_scan is None or now >= self.next_scan):
                self.next_scan = now + pd.Timedelta(seconds=SCAN_SEC)
                steps.append(("검색", lambda: self.scan(now, W)))
            if bar != self.last_min and now <= W["buy_end"]:
                self.last_min = bar
                steps.append(("1분 갱신", lambda: self.minute(now, W)))
            steps.append(("돌파 확인", lambda: self.hot(now, W)))
            for name, fn in steps:
                try:
                    fn()
                except Exception as e:
                    self.log(f"{name} 오류 (다음 주기에 다시): {type(e).__name__}: {e}")
                    traceback.print_exc()
            time.sleep(max(1.0, HOT_SEC - (now_et() - now).total_seconds()))

    def check_once(self):
        """최근 장(장 전이면 어제 장)으로 검색·상태 기계 한 번 - 신호 파일에는 안 씀"""
        from scanner import load_universe
        pb = _pb()
        spy = pb.download(["SPY"], period="5d", interval="1m", prepost=False).get("SPY")
        d = spy.dropna(subset=["Close"]).index[-1].tz_convert("America/New_York").date()
        W = day_windows(d, self.c)
        self.uni = load_universe()
        now = min(now_et(), W["flat"])
        self.scan(now, W)
        live = [s for s, v in self.cands.items() if v]
        print(f"[점검] {d} 장 | 감시 후보 {len(live)}종목 (유통주식 초과·정보 없음 {sum(1 for v in self.cands.values() if not v)})")
        self.minute(now, W, period="5d")
        print(f"[점검] 마지막 시점 매수 대기(READY): {len(self.ready)}종목 {list(self.ready)[:10]}")


def main():
    cfg = BC.load_config()
    if "--check" in sys.argv:
        BFLive(cfg, check=True).check_once()
    else:
        BFLive(cfg).run()


if __name__ == "__main__":
    main()
