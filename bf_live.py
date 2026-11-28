# -*- coding: utf-8 -*-
"""
bf_live.py - Bull Flag V2 실시간 신호 (서버 bf-live.service, 평일 뉴욕 09:20 시작 ~ 장 마감 10분 전 종료)

[조건] V2 + 진입가 entry_price_ranges + 익절 tp_pct / 손절 sl_pct (bf_config.json, 실제 주문 % 는 서버 bot.env)
  검색기 (2분마다 전 종목): 장중 고가 >= 전일 종가 +10%, $0.5~20, 유통주식수 2,000만 주 미만, RVOL 3배(14거래일 같은 시각)
  1분 완성봉 상태 기계 (bf_core.detect): 깃대 5봉 +5% -> 눌림 1~4봉(되돌림 50%·VWAP 위·눌림 거래량 <= 깃대 60%) -> READY
  돌파 (매수 대기 종목 있으면 2초마다 토스 실시간 현재가, 토스 안 되면 5초마다 야후 1분봉 종가):
                현재가 > 매수 기준가 (직전 조정 음봉 고가), 기준가 +0.5% 이내, 현재가 > 9EMA > VWAP,
                진입가 entry_price_ranges (예 $1~3, $10~20), 신규 매수 시간 안, 깃대마다 첫 돌파 1번
  -> state/fl_signal_queue.jsonl 에 strategy=BF 로 기록 -> 토스 자동매수 봇(bot.py)이 자동 승인
  -> monitor.py 가 즉시 시장가 매수, 손절·익절 조건주문, 60분 지나면·전량 청산 시각에 청산
[모의] paper_price_ranges (예 $10~20) 돌파는 strategy=BF_PAPER 로만 기록 (bot.py 가 받지 않음 = 주문 없음).
       장 끝나면 그날 모의 신호를 백테스트와 같은 방식(bf_core.sim_signal)으로 채점 -> state/bf_paper_results.jsonl,
       요약은 strategy=BF_PAPER_RESULT 로 신호 파일에 남겨 bot.py 가 텔레그램으로 보냄
[연구] research.enabled 면 bf_research.Research 가 가상 매매 (주문 없음): 실전 신호(BF) + 실전 범위 밖이지만
       research.price_ranges 안의 돌파(strategy=BF_RESEARCH, bot.py 가 받지 않음)
       -> 🧪 모의매매 텔레그램(알림설정.json)으로 가상 매수·청산·장 마감 요약
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
from bf_research import Research
from fl_core import KST_S, MIN, clk, day_windows, iso, now_et, regular_1m, round_near
from toss_quote import TossQuote

QUEUE = os.path.join(FC.STATE_DIR, "fl_signal_queue.jsonl")
PAPER_FILE = os.path.join(FC.STATE_DIR, "bf_paper_results.jsonl")
FLOAT_FILE = os.path.join(FC.HERE, "cache", "bf_float.json")
SCAN_SEC, HOT_SEC, TOSS_HOT_SEC = 120, 5, 2       # 돌파 확인: 야후 5초 / 토스 실시간 2초


def paper_outcome(bars, s, cfg, W):
    """모의 신호 하나를 백테스트와 같은 규칙으로 채점 (신호 봉 종가 -> 다음 봉부터 고가·저가, 60분·전량 청산 시각, 비용 반영)"""
    if bars is None or bars.empty:
        return None
    t = pd.Timestamp(s["t"]).tz_convert(bars.index.tz)
    j = int(bars.index.searchsorted(t))
    if j >= len(bars) or bars.index[j] != t:
        return None
    rec = dict(tk=s["tk"], day=str(W["open"].date()), t=s["t"], j=j, entry_raw=float(s["px"]), trigger=float(s["trigger"]))
    r = BC.sim_signal(bars, rec, cfg, W, cfg["tp_pct"], cfg["sl_pct"], max_hold=cfg["max_hold_min"])
    return dict(day=rec["day"], tk=s["tk"], t=s["t"], px=float(s["px"]), why=r["why"], net_pct=round(r["net_pct"], 2),
                mfe_pct=round(r["mfe_pct"], 2), hold_min=round(r["hold_min"]), exit_t=r["exit_t"])


def _pb():
    import pullback
    return pullback


class BFLive:
    def __init__(self, cfg, check=False):
        self.c, self.check = cfg, check
        self.ranges = [(float(a), float(b)) for a, b in (cfg.get("entry_price_ranges")
                                                          or [[cfg["entry_min_price"], cfg["entry_max_price"]]])]   # 실전 진입가
        self.paper = [(float(a), float(b)) for a, b in (cfg.get("paper_price_ranges") or [])]   # 모의만 (주문 없음)
        self.toss = TossQuote()
        self.toss_err_t = 0.0
        self.cands, self.ready, self.fired, self.uni = {}, {}, set(), {}
        self.floats = json.load(open(FLOAT_FILE, encoding="utf-8")) if os.path.exists(FLOAT_FILE) else {}
        self.logf = None
        self.last_min = None
        self.next_scan = None
        self.research = Research(cfg, self.log, dry=check) if (cfg.get("research") or {}).get("enabled") else None
        self.rsch = self.research.ranges if self.research else []     # 연구판 모의만 보는 진입가 범위 (실전 밖 포함)
        self.loop_n = 0

    @staticmethod
    def rng_text(rs):
        return ", ".join(f"${a:g}~{b:g}" for a, b in rs)

    def in_live(self, p):
        return any(a <= p < b for a, b in self.ranges)

    def quote(self, tks, W):
        """현재가 {종목: (가격, 출처)} - 토스 실시간 (서버), 안 되는 종목만 야후 1분봉 마지막 종가"""
        out = {}
        if self.toss.available():
            for tk in tks:
                try:
                    out[tk] = (self.toss.price(tk), "토스")
                except Exception as e:
                    if time.time() - self.toss_err_t > 60:             # 오류 로그는 1분에 한 번만
                        self.toss_err_t = time.time()
                        self.log(f"{tk} 토스 현재가 실패 → 야후로 확인: {type(e).__name__}: {e}")
                    if not self.toss.available():
                        break
        rest = [tk for tk in tks if tk not in out]
        if rest:
            data = _pb().download(rest, period="1d", interval="1m", prepost=False)
            for tk in rest:
                bars = regular_1m(data.get(tk), W)
                if not bars.empty:
                    out[tk] = (float(bars["Close"].iat[-1]), "야후")
        return out

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
            trig = P["trigger"]
            if not any(a * 0.99 <= trig < b for a, b in self.ranges + self.paper + self.rsch):
                continue
            ready[tk] = dict(P, key=key, ema=float(F["ema"].iat[-1]), vwap=float(F["vwap"].iat[-1]),
                             chg=float(F["chg"].iat[-1]), rvol=float(F["rvol"].iat[-1]) if F["rvol"].iat[-1] == F["rvol"].iat[-1] else None,
                             t_start=iso(F.index[P["si"]]))
        if set(ready) != set(self.ready):
            self.log(f"매수 대기(READY) {len(ready)}: " + ", ".join(f"{tk} 기준가 ${r['trigger']:.4g}" for tk, r in ready.items()))
        self.ready = ready

    def hot(self, now, W):
        """2초(토스)·5초(야후)마다 READY 종목 현재가 -> 기준가 돌파면 신호 (깃대마다 1번)"""
        if not self.ready or not (W["buy_start"] <= now <= W["buy_end"]):
            return
        self.loop_n += 1
        near_live = lambda R: any(a * 0.99 <= R["trigger"] < b for a, b in self.ranges + self.paper)
        main = [tk for tk, R in self.ready.items() if near_live(R)]
        extra = [tk for tk in self.ready if tk not in main]          # 연구판 모의만: 토스 호출을 아끼려고 한 번 걸러 확인
        px = self.quote(main + (extra if self.loop_n % 2 == 0 else []), W)
        cfg = self.c
        for tk, R in list(self.ready.items()):
            if tk not in px:
                continue
            p, src = px[tk]
            T = R["trigger"]
            if p <= T:
                continue
            self.fired.add(R["key"])
            self.ready.pop(tk)
            why = None
            paper = not self.in_live(p) and any(a <= p < b for a, b in self.paper)
            rs = not self.in_live(p) and not paper and any(a <= p < b for a, b in self.rsch)
            if p > T * (1 + cfg["max_chase_pct"] / 100):
                why = f"기준가 +{cfg['max_chase_pct']:g}% 초과 ({src} ${p:.4g} / 기준가 ${T:.4g}) - 추격 안 함"
            elif not self.in_live(p) and not paper and not rs:
                why = f"진입가 ${p:.4g} 가 {self.rng_text(self.ranges)} 밖"
            elif not (p > R["ema"] > R["vwap"]):
                why = f"현재가 > 9EMA > VWAP 아님 (현재가 {p:.4g}, EMA {R['ema']:.4g}, VWAP {R['vwap']:.4g})"
            if why:
                self.log(f"{tk} 돌파 탈락: {why}")
                continue
            self.emit(tk, R, p, now, paper=paper, src=src, research=rs)

    def emit(self, tk, R, p, now, paper=False, src="", research=False):
        strat = "BF_PAPER" if paper else "BF_RESEARCH" if research else "BF"     # BF 만 실전 주문 (bot.py ACCEPT_STRATEGY)
        rec = dict(strategy=strat, tk=tk, px_src=src, name=(self.cands.get(tk) or {}).get("name", ""), mode="BF",
                   t=iso(now.floor("min")), t_sig=iso(now), t_detect=iso(now), px=round(p, 6), L=float(R["pb_low"]),
                   SL=round_near(p * (1 - self.c["sl_pct"] / 100)), TP=round_near(p * (1 + self.c["tp_pct"] / 100)),
                   sl_pct=self.c["sl_pct"], tp_pct=self.c["tp_pct"], chg=round(R["chg"], 2),
                   rvol=round(R["rvol"], 2) if R.get("rvol") else None, score=None, spread_pct=None, block=None,
                   trigger=float(R["trigger"]), pole_start=float(R["start"]), pole_high=float(R["high"]),
                   pole_pct=round((R["high"] / R["start"] - 1) * 100, 2), retr=round(float(R["retr"]), 1),
                   pb_bars=len(R["pb"]))
        tag = '📝 [모의 · 주문 없음] ' if paper else '🧪 [연구 모의 · 주문 없음] ' if research else '🚩 '
        self.log(f"{tag}{tk} Bull Flag 돌파 신호 {src} ${p:.4g} (기준가 ${R['trigger']:.4g}, "
                 f"깃대 +{rec['pole_pct']:.1f}%, 되돌림 {rec['retr']:.0f}%, 눌림 {rec['pb_bars']}봉)")
        if self.check:
            return
        os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
        with open(QUEUE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if self.research and not paper:                            # 연구판 가상 매매 (실전 신호 기록 뒤, 실패해도 실전과 무관)
            try:
                self.research.on_signal(rec, now)
            except Exception as e:
                self.log(f"[연구] 신호 처리 오류: {type(e).__name__}: {e}")

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
        if self.research:
            self.research.start_day(d)
        if now >= W["flat"]:
            self.log("오늘 장이 끝났습니다 - 종료")
            if self.research and self.research.open:              # 청산 전에 재시작돼 남은 가상 포지션 정리·요약
                self.research.day_end(now, W, self.quote)
            return
        self.uni = load_universe()
        tq = "?"
        if self.toss.enabled:
            try:
                tq = f"OK (SPY ${self.toss.price('SPY'):.2f})"
            except Exception as e:
                tq = f"실패 → 야후 사용 ({type(e).__name__}: {e})"
        self.log(f"Bull Flag V2 실시간 신호 시작 | 진입 {self.rng_text(self.ranges)}"
                 + (f" (모의 {self.rng_text(self.paper)})" if self.paper else "")
                 + f" · 익절 +{cfg['tp_pct']:g}% / 손절 -{cfg['sl_pct']:g}% | 신규 {clk(W['buy_start'])}~{clk(W['buy_end'])} | "
                 f"돌파 확인 " + (f"토스 실시간 {TOSS_HOT_SEC}초 - {tq}" if self.toss.enabled else f"야후 {HOT_SEC}초 (토스 키 없음)")
                 + f" | 유통주식 캐시 {len(self.floats)}종목"
                 + (" | 연구판 모의 " + self.research.describe() if self.research else ""))
        start = W["open"] + MIN + pd.Timedelta(seconds=5)
        while now_et() < start:
            time.sleep(min(30, max(1, (start - now_et()).total_seconds())))
        while True:
            now = now_et()
            if now >= W["flat"]:
                self.log(f"전량 청산 시각 {clk(W['flat'])} - 신호 종료 (청산은 monitor.py)")
                if self.research:
                    try:
                        self.research.day_end(now, W, self.quote)
                    except Exception as e:
                        self.log(f"[연구] 장 마감 정리 오류: {type(e).__name__}: {e}")
                        traceback.print_exc()
                if self.paper:
                    time.sleep(75)                                 # 청산 시각 1분봉까지 받은 뒤 채점
                    try:
                        self.paper_review(W)
                    except Exception as e:
                        self.log(f"모의 채점 오류: {type(e).__name__}: {e}")
                        traceback.print_exc()
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
            if self.research and self.research.open:
                steps.append(("연구 모의", lambda: self.research.tick(now, W, self.quote)))
            for name, fn in steps:
                try:
                    fn()
                except Exception as e:
                    self.log(f"{name} 오류 (다음 주기에 다시): {type(e).__name__}: {e}")
                    traceback.print_exc()
            busy = self.ready or (self.research and self.research.open)
            gap = TOSS_HOT_SEC if (busy and self.toss.available()) else HOT_SEC
            time.sleep(max(1.0, gap - (now_et() - now).total_seconds()))

    def paper_review(self, W):
        """오늘 모의(BF_PAPER) 신호 채점 -> state/bf_paper_results.jsonl + 텔레그램 요약(BF_PAPER_RESULT, bot.py 가 보냄)"""
        d = str(W["open"].date())
        sigs = []
        if os.path.exists(QUEUE):
            for line in open(QUEUE, encoding="utf-8"):
                try:
                    s = json.loads(line)
                except Exception:
                    continue
                if s.get("strategy") == "BF_PAPER" and str(s.get("t", ""))[:10] == d:
                    sigs.append(s)
        rows = []
        if sigs:
            data = _pb().download(sorted({s["tk"] for s in sigs}), period="1d", interval="1m", prepost=False)
            for s in sigs:
                r = paper_outcome(regular_1m(data.get(s["tk"]), W), s, self.c, W)
                if r:
                    rows.append(r)
                else:
                    self.log(f"모의 채점 불가 (1분봉 없음): {s['tk']} {s['t']}")
        hist = {}
        if os.path.exists(PAPER_FILE):
            for line in open(PAPER_FILE, encoding="utf-8"):
                try:
                    x = json.loads(line)
                    hist[(x["day"], x["tk"], x["t"])] = x
                except Exception:
                    continue
        new = [r for r in rows if (r["day"], r["tk"], r["t"]) not in hist]
        if new:
            os.makedirs(os.path.dirname(PAPER_FILE), exist_ok=True)
            with open(PAPER_FILE, "a", encoding="utf-8") as f:
                for r in new:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        for r in rows:
            hist[(r["day"], r["tk"], r["t"])] = r
        rng = ", ".join(f"${a:g}~{b:g}" for a, b in self.paper)
        k = lambda xs: (f"승률 {sum(x['net_pct'] > 0 for x in xs) / len(xs) * 100:.0f}% · "
                        f"평균 {sum(x['net_pct'] for x in xs) / len(xs):+.2f}%") if xs else "-"
        lines = [f"📝 <b>{rng} 모의 결과</b> (실제 주문 없음) · {d}",
                 f"오늘 {len(rows)}건" + (f": {k(rows)}" if rows else " - 신호 없음")]
        for r in rows:
            lines.append(f"· {pd.Timestamp(r['t']).tz_convert(FC.ET_S):%H:%M} {r['tk']} ${r['px']:.2f} → {r['why']} "
                         f"{r['net_pct']:+.1f}% ({r['hold_min']}분, 최대 +{r['mfe_pct']:.1f}%)")
        allr = list(hist.values())
        lines.append(f"누적 {len(allr)}건 ({len({x['day'] for x in allr})}일): {k(allr)} (비용 반영, 백테스트와 같은 채점)")
        text = "\n".join(lines)
        self.log(text.replace("<b>", "").replace("</b>", ""))
        now = now_et()
        with open(QUEUE, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(strategy="BF_PAPER_RESULT", tk="-", day=d, t=iso(now), t_detect=iso(now), text=text),
                               ensure_ascii=False) + "\n")

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
