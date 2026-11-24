# -*- coding: utf-8 -*-
"""
fl_backtest.py - Fashionably Late V4.0 백테스트 (python fl.py --backtest)

[데이터] 야후(yfinance): 1분봉은 최근 30일만 받을 수 있음 -> 기간은 최근 약 20거래일
  1) 전체 보통주 일봉 -> 그날 고가가 전일 종가 +15% 이상 & 거래량 50만 주 이상 & 고가 $0.5 이상인 (종목, 날짜)
     = 장중 어느 시점에 검색기 조건을 만족하기 위한 필요조건 (빠뜨리는 종목 없음)
  2) 후보의 1분봉(정규장) + 과거 5분봉(상대거래량 RVOL: 과거 10거래일 같은 시각 누적 거래량 평균)
  3) 실시간과 같은 fl_core 코드로 지표·신호 계산 (그 봉까지의 데이터만 사용)
[체결 가정] B 모드: 신호 봉 마감 -> 다음 1분봉 시가 + 슬리피지에 시장가 매수 (신호 봉 종가 체결 가정 안 함)
  익절 = 지정가 (슬리피지 없음), 손절·조기청산·강제청산 = 시장가 (슬리피지)
  같은 1분봉에서 익절·손절 둘 다 닿으면 순서를 알 수 없음 -> 보수적(손절 먼저)·낙관적(익절 먼저) 둘 다 보고
  계좌 단위: 1분 거래량의 10% 까지만 체결 (부분 체결), 호가 스프레드는 과거 데이터가 없어 검사 못 함
  A 모드(실시간 교차): 틱 데이터가 없어 1분봉으로 체결을 추정하지 않음 -> 실시간 모의에서 감지 가격을 기록해 비교
"""
import collections
import os
import time
from datetime import timedelta

import numpy as np
import pandas as pd

from fl_core import (COND_KO, COND_ORDER, FILTER_KO, FILTER_ORDER, HERE, LOGS, MIN, NOT_IN_LOD, Account, at,
                     bar_exit, build_frame, conditions, daily_context, day_windows, early_exit, find_signals,
                     market_hours, money, now_et, regular_1m, required_keys, rvol_profile, structure, ts, with_cfg)

CACHE = os.path.join(HERE, "cache")


def _dl():
    from pullback import _yf, download        # 기존 다운로드 코드 재사용 (재시도·묶음 요청)
    return _yf, download


# ═════════════════════ 데이터 ═════════════════════
def trading_days(cfg, ndays=None):
    _yf, _ = _dl()
    spy = _yf().download("SPY", period="2mo", interval="1d", progress=False, auto_adjust=True)
    today = now_et().date()
    lim = today - timedelta(days=29)                        # 야후 1분봉 한도
    Wt = day_windows(today, cfg)
    days = [pd.Timestamp(x).date() for x in spy.index]
    days = [x for x in days if x >= lim and market_hours(x, cfg) and (x < today or (Wt and now_et() >= Wt["close"]))]
    return days[-ndays:] if ndays else days


def _reuse_daily(days):
    """같은 기간을 받은 눌림목 백테스트 캐시가 있으면 그 일봉을 씀 (전체 4천여 종목 다시 받지 않게)"""
    if not os.path.isdir(CACHE):
        return None
    for f in sorted(os.listdir(CACHE), reverse=True):
        if f.startswith("pullback_bt_") and f.endswith(f"_{days[-1]:%Y%m%d}.pkl"):
            try:
                d = pd.read_pickle(os.path.join(CACHE, f)).get("daily")
                if d:
                    return d
            except Exception:
                pass
    return None


def last_close_on(h5, day):
    if h5 is None or len(h5) == 0 or day is None:
        return None
    idx = h5.index.tz_convert("America/New_York")
    m = np.asarray(idx.hour * 60 + idx.minute)
    sel = (np.array(idx.date) == pd.Timestamp(day).date()) & (m >= 570) & (m < 960)
    x = h5["Close"].to_numpy()[sel]
    x = x[~np.isnan(x)]
    return float(x[-1]) if len(x) else None


def load_data(cfg, days, use_cache=True, P=print):
    from scanner import load_universe
    _, download = _dl()
    uni = load_universe()
    os.makedirs(CACHE, exist_ok=True)
    cpath = os.path.join(CACHE, f"fl_bt_{days[0]:%Y%m%d}_{days[-1]:%Y%m%d}.pkl")
    cache = pd.read_pickle(cpath) if use_cache and os.path.exists(cpath) else {}
    daily = cache.get("daily") or (_reuse_daily(days) if use_cache else None)
    print(f"[1/4] 전체 보통주 {len(uni):,}개 일봉" + (" (캐시)" if daily else ""), flush=True)
    if not daily:
        daily = download(sorted(uni), period="3mo", interval="1d", quiet=False)
    dayset = set(days)
    cand = collections.defaultdict(list)
    for tk, df in daily.items():
        if tk not in uni or df is None or df.empty:
            continue
        df = df.dropna(subset=["Close"])
        pc = df["Close"].shift(1)
        ok = ((df["High"] >= pc * (1 + cfg["min_change_pct"] / 100)) & (df["Volume"] >= cfg["min_cum_volume"])
              & (df["High"] >= cfg["min_price"])).to_numpy()
        for x, k in zip(df.index, ok):
            x = pd.Timestamp(x).date()
            if k and x in dayset:
                cand[tk].append(x)
    n_pairs = sum(len(v) for v in cand.values())
    P(f"[검색 범위] 보통주 {len(uni):,}개 중 일봉 확보 {sum(1 for t in uni if t in daily):,}개 → 일봉 필요조건"
      f"(고가 ≥ 전일 종가 +{cfg['min_change_pct']:g}%, 거래량 ≥ {cfg['min_cum_volume']:,}, 고가 ≥ ${cfg['min_price']:g}) "
      f"{n_pairs}건 (종목·일), 고유 {len(cand)}종목")
    # 1분봉: 7일 창 단위 (야후 한 번에 8일까지)
    m1 = cache.get("m1", {})
    got = cache.get("m1_win", set())
    print(f"[2/4] 후보 1분봉", flush=True)
    w = days[0]
    while w <= days[-1]:
        we = w + timedelta(days=7)
        tks = sorted(tk for tk, ds in cand.items() if any(w <= x < we for x in ds) and (tk, str(w)) not in got)
        if tks:
            res = download(tks, chunk=50, start=str(w), end=str(we), interval="1m", prepost=False, quiet=False)
            for tk, df in res.items():
                m1[tk] = pd.concat([m1[tk], df]) if tk in m1 else df
            got |= {(tk, str(w)) for tk in tks}
        w = we
    m1 = {tk: df[~df.index.duplicated(keep="last")].sort_index() for tk, df in m1.items()}
    # 과거 5분봉 (RVOL 같은 시각 비교 + 분할 확인)
    h5 = cache.get("h5", {})
    miss = sorted(tk for tk in cand if tk not in h5)
    print(f"[3/4] RVOL 용 과거 5분봉 {len(miss)}종목", flush=True)
    if miss:
        s5 = max(days[0] - timedelta(days=21), now_et().date() - timedelta(days=58))
        h5.update(download(miss, chunk=50, start=str(s5), end=str(days[-1] + timedelta(days=1)), interval="5m",
                           prepost=False, quiet=False))
        for tk in miss:
            h5.setdefault(tk, None)
    if use_cache:
        pd.to_pickle(dict(daily=daily, m1=m1, m1_win=got, h5=h5), cpath)
    return uni, daily, cand, m1, h5


def build_day(tk, d, cfg, m1, h5, daily):
    W = day_windows(d, cfg)
    bars = regular_1m(m1.get(tk), W)
    if bars.empty:
        return None, "그날 1분봉 없음"
    dctx = daily_context(daily.get(tk), d, cfg)
    if not dctx:
        return None, "전일 종가 없음"
    note = None
    c5 = last_close_on(h5.get(tk), dctx["prev_date"])
    if c5 and abs(c5 / dctx["prev_close"] - 1) * 100 > 10:      # 일봉과 분봉의 분할 반영 차이 -> 분봉 기준으로 맞춤
        k = c5 / dctx["prev_close"]
        for x in ("prev_close", "sma5", "sma10", "resistance"):
            dctx[x] = dctx[x] * k
        note = f"분할 의심: 일봉 전일 종가를 분봉 기준({k:.3g}배)으로 맞춤"
    prof = rvol_profile(h5.get(tk), d, cfg)
    F = build_frame(bars, W, dctx["prev_close"], prof, dctx["adv"], cfg)
    return dict(tk=tk, d=d, W=W, F=F, S=structure(F), dctx=dctx, note=note, prof=prof is not None), None


# ═════════════════════ 시뮬레이션 ═════════════════════
def solo_cfg(cfg):
    """공통 표본: 계좌 한도 없이 신호 하나만 (전량 체결)"""
    return with_cfg(cfg, position_pct=100.0, max_total_pct=100.0, min_cash_pct=0.0, risk_limit_pct=1e9,
                    account_lock=False, participation_pct=None, max_positions=1)


def sim_day(cfg, d, W, frames, sigs, start_eq, amb="cons", order="score"):
    """하루 계좌 시뮬레이션 (실시간과 같은 순서)
    매 1분봉: ① 강제청산 시각이면 전량 시가 매도 ② 잠금·조기청산 표시된 종목 시가 매도 ③ 미체결 매수 시가 체결
              ④ 봉 안 익절·손절 ⑤ 봉 마감: 시세 갱신 -> 계좌 ±4% 확인(우선) -> 조기청산 판단 -> 새 신호 주문"""
    st = dict(day=str(d), start_eq=start_eq, cash=start_eq)
    acct = Account(cfg, st)
    A = {tk: (F["Open"].to_numpy(), F["High"].to_numpy(), F["Low"].to_numpy(), F["Close"].to_numpy(),
              F["Volume"].to_numpy()) for tk, F in frames.items()}
    pos_of = {tk: {t: j for j, t in enumerate(F.index)} for tk, F in frames.items()}
    times = sorted(set().union(*[set(F.index) for F in frames.values()])) if frames else []
    times = [t for t in times if t <= W["flat"]]
    by_t = collections.defaultdict(list)
    for s in sigs:
        by_t[ts(s["t"])].append(s)
    key = {"score": lambda s: (-s["score"], s["tk"]), "reverse": lambda s: (s["score"], s["tk"]),
           "time": lambda s: s["tk"]}[order]
    cap = acct.cap_qty
    for t in times:
        bars = {tk: pos_of[tk][t] for tk in frames if t in pos_of[tk]}
        P = st["pos"]
        if t >= W["flat"]:
            acct.cancel_buys(t, "강제 청산 시각")
            for tk in list(P):
                if tk in bars:
                    j = bars[tk]
                    acct.sell(tk, A[tk][0][j], t, f"장 마감 {cfg['flatten_before_close_min']:g}분 전 강제 청산",
                              "market", cap(A[tk][4][j]))
            continue
        if st["locked"]:
            acct.cancel_buys(t, st["locked"]["why"])
        for tk in list(P):
            p = P[tk]
            if p["exit_flag"] and tk in bars:
                j = bars[tk]
                acct.sell(tk, A[tk][0][j], t, p["exit_flag"], "market", cap(A[tk][4][j]))
        acct.expire_orders(t)
        for o in acct.open_buys():
            if o["tk"] in bars and t >= ts(o["created"]):
                j = bars[o["tk"]]
                acct.fill_buy(o, A[o["tk"]][0][j], t, cap(A[o["tk"]][4][j]), j=j)
        for tk in list(P):
            p = P.get(tk)
            if not p or tk not in bars:
                continue
            j = bars[tk]
            o, h, l = A[tk][0][j], A[tk][1][j], A[tk][2][j]
            r = bar_exit(p, o, h, l, ts(p["entry_min"]) == t, amb)
            if r:
                px, why, a, kind = r
                p["amb"] = p["amb"] or a
                acct.sell(tk, px, t, why, kind, cap(A[tk][4][j]))
        for tk, j in bars.items():
            acct.mark(tk, A[tk][3][j], A[tk][1][j] if tk in P else None, A[tk][2][j] if tk in P else None)
        tc = t + MIN
        acct.check_account(tc)
        acct.lock_done(tc)
        if not st["locked"]:
            for tk in list(P):
                p = P[tk]
                if tk in bars and not p["exit_flag"] and p.get("ej") is not None:
                    held = (tc - ts(p["entry_t"])).total_seconds() / 60
                    r = early_exit(frames[tk], bars[tk], p, cfg, held)
                    if r:
                        p["exit_flag"] = r
        st["max_conc"] = max(st["max_conc"], len(P))
        for s in sorted(by_t.get(t, []), key=key):
            acct.submit(s, tc, ref=s["px"])
    for tk in list(st["pos"]):                                    # 시세가 끊겨 못 판 종목: 마지막 시세로 정리
        acct.sell(tk, acct.px(tk), W["flat"], "강제 청산 (마지막 시세)", "market")
    acct.lock_done(W["flat"])
    return acct


def trade_level(cfg, items, sigs, amb):
    """공통 표본: (종목, 날짜) 별 첫 신호 하나씩, 계좌 한도 없이"""
    sc = solo_cfg(cfg)
    out = []
    first = {}
    for s in sigs:
        k = (s["tk"], s["day"])
        if k not in first:
            first[k] = s
    for (tk, day), s in sorted(first.items(), key=lambda x: (x[1]["day"], x[1]["t"])):
        it = items[(tk, day)]
        acct = sim_day(sc, it["d"], it["W"], {tk: it["F"]}, [s], 100000.0, amb)
        out += acct.st["trades"]
        if not acct.st["trades"]:
            out.append(dict(tk=tk, day=day, skipped=acct.st["rows"][-1]["result"] if acct.st["rows"] else "체결 없음"))
    return [t for t in out if "net_pct" in t], [t for t in out if "skipped" in t]


def account_level(cfg, days, items, sigs, amb="cons", order="score"):
    eq = float(cfg["capital"])
    rows, trades, misses = [], [], collections.Counter()
    by_day = collections.defaultdict(list)
    for s in sigs:
        by_day[s["day"]].append(s)
    for d in days:
        ds = str(d)
        W = day_windows(d, cfg)
        frames = {it["tk"]: it["F"] for k, it in items.items() if k[1] == ds}
        acct = sim_day(cfg, d, W, frames, by_day.get(ds, []), eq, amb, order)
        st = acct.st
        end = acct.equity()
        lk = st["locked"]
        rows.append(dict(day=ds, start=eq, end=end, ret=(end / eq - 1) * 100, trades=len(st["trades"]),
                         lock=lk["why"] if lk else "", lock_ret=lk["ret"] if lk else None,
                         final_ret=lk.get("final_ret") if lk else None, max_conc=st["max_conc"],
                         sigs=len(by_day.get(ds, []))))
        trades += st["trades"]
        misses.update(st["missed"])
        eq = end
    return rows, trades, misses


# ═════════════════════ 통계 ═════════════════════
def tstats(T):
    T = sorted(T, key=lambda t: t["entry_t"])
    n = len(T)
    if not n:
        return dict(n=0)
    r = np.array([t["net_pct"] for t in T])
    g = np.array([t["gross_pct"] for t in T])
    w, l = r[r > 0], r[r <= 0]
    streak = best = 0
    for x in r:
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    mfe = [t["mfe_R"] for t in T if t.get("mfe_R") is not None]
    mae = [t["mae_R"] for t in T if t.get("mae_R") is not None]
    return dict(n=n, win=len(w) / n * 100, avg=r.mean(), gross=g.mean(), avg_win=w.mean() if len(w) else 0.0,
                avg_loss=l.mean() if len(l) else 0.0,
                rr=(w.mean() / abs(l.mean())) if len(w) and len(l) and l.mean() != 0 else float("nan"),
                pf=(w.sum() / abs(l.sum())) if len(l) and l.sum() < 0 else float("inf"), tot=r.sum(),
                consec=best, hold=float(np.median([t["hold_min"] for t in T])),
                mfe_R=float(np.mean(mfe)) if mfe else float("nan"), mae_R=float(np.mean(mae)) if mae else float("nan"),
                mfe=float(np.mean([t["mfe_pct"] for t in T])), mae=float(np.mean([t["mae_pct"] for t in T])),
                amb=sum(1 for t in T if t.get("amb")), early=sum(1 for t in T if str(t["why"]).startswith("조기")))


def ft(s):
    if not s.get("n"):
        return "거래 0건"
    rr = f"{s['rr']:.2f}" if s["rr"] == s["rr"] else "-"
    pf = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "∞"
    return (f"{s['n']}건 | 승률 {s['win']:.0f}% | 평균 {s['avg']:+.2f}% (비용 전 {s['gross']:+.2f}%) | "
            f"이익 평균 {s['avg_win']:+.2f}% / 손실 평균 {s['avg_loss']:+.2f}% → 실제 손익비 {rr} | PF {pf} | "
            f"합계 {s['tot']:+.1f}% | 최대 연속 손실 {s['consec']} | 보유 중앙 {s['hold']:.0f}분")


def astats(rows):
    if not rows:
        return dict(days=0)
    r = np.array([x["ret"] for x in rows])
    eq = np.cumprod(1 + r / 100)
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))
    dd = (np.concatenate([[1.0], eq]) / peak - 1).min() * 100
    return dict(days=len(rows), cum=(eq[-1] - 1) * 100, avg=r.mean(), mdd=dd, trades=sum(x["trades"] for x in rows),
                up=sum(1 for x in rows if str(x["lock"]).startswith("계좌 +")),
                dn=sum(1 for x in rows if str(x["lock"]).startswith("계좌 -")),
                trade_days=sum(1 for x in rows if x["trades"]), conc=max(x["max_conc"] for x in rows),
                best=r.max(), worst=r.min())


def fa(a):
    if not a.get("days"):
        return "-"
    return (f"{a['days']}일 · 거래 {a['trades']}건(거래한 날 {a['trade_days']}일) | 누적 {a['cum']:+.2f}% | 일평균 {a['avg']:+.3f}% | "
            f"최대 낙폭 {a['mdd']:.2f}% | 최고 {a['best']:+.2f}% / 최저 {a['worst']:+.2f}% | +4% 도달 {a['up']}일 · "
            f"-4% 도달 {a['dn']}일 | 최대 동시 보유 {a['conc']}")


def bucket_price(p):
    for lo, hi, name in ((0.5, 1, "$0.5~1"), (1, 5, "$1~5"), (5, 10, "$5~10"), (10, 30, "$10~30")):
        if p < hi:
            return name
    return "$30 이상"


def breakdown(T, keyf, order=None):
    g = collections.defaultdict(list)
    for t in T:
        g[keyf(t)].append(t)
    keys = order or sorted(g)
    out = []
    for k in keys:
        if k in g:
            s = tstats(g[k])
            out.append(f"{k}: {s['n']}건 승률 {s['win']:.0f}% 평균 {s['avg']:+.2f}%")
    return " / ".join(out) or "-"


# ═════════════════════ 백테스트 본체 ═════════════════════
def signals_for(cfg, items, mode=None):
    out = []
    for it in items.values():
        s, _ = find_signals(it["F"], it["S"], cfg, it["W"], it["tk"], mode or cfg["structure"], it["dctx"])
        out += s
    return sorted(out, key=lambda s: (s["day"], s["t"], s["tk"]))


def first_per_day(sigs):
    seen, out = set(), []
    for s in sigs:
        k = (s["tk"], s["day"])
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out


def backtest(cfg, ndays=None, use_cache=True):
    t0 = time.time()
    lines = []

    def P(s=""):
        print(s, flush=True)
        lines.append(s)

    days = trading_days(cfg, ndays)
    P(f"Fashionably Late V4.0 백테스트 | {days[0]} ~ {days[-1]} ({len(days)}거래일) | 1분봉 정규장 | "
      f"매수 {cfg['buy_start_et']}~{cfg['buy_end_et']}ET · 장 마감 {cfg['flatten_before_close_min']:g}분 전 청산")
    uni, daily, cand, m1, h5 = load_data(cfg, days, use_cache, P)
    print("[4/4] 지표·신호·매매 시뮬레이션", flush=True)
    items, skip, notes = {}, collections.Counter(), 0
    for tk in sorted(cand):
        for d in cand[tk]:
            it, why = build_day(tk, d, cfg, m1, h5, daily)
            if not it:
                skip[why] += 1
                continue
            items[(tk, str(d))] = it
            notes += bool(it["note"])
            if not it["prof"]:
                skip["RVOL 과거 5분봉 부족 → 10일 평균 일거래량 x 경과시간으로 계산"] += 1
    P(f"[데이터] 1분봉 확보 {len(items)}건 (종목·일) | " + ", ".join(f"{k} {v}" for k, v in skip.items())
      + (f" | 분할 의심 보정 {notes}" if notes else ""))
    # ── 조건 깔때기 (종목·일 기준: 그날 한 번이라도 1~k 번째 조건을 동시에 만족한 봉이 있는지) ──
    keys = required_keys(cfg, cfg["structure"])
    funnel = collections.Counter()
    for it in items.values():
        K, _ = conditions(it["F"], it["S"], cfg, cfg["structure"], it["dctx"])
        tend = it["F"].index + MIN
        ok = np.asarray((tend >= it["W"]["buy_start"]) & (tend <= it["W"]["buy_end"]))
        funnel["매수 시간 10:00~13:30"] += bool(ok.any())
        for k in keys:
            ok = ok & K[k]
            funnel[k] += bool(ok.any())
    P("[조건 깔때기] 종목·일 수 (앞 조건들과 동시에 만족한 봉이 하루에 한 번이라도 있었던 경우)")
    P("  " + " → ".join([f"후보 {len(items)}", f"매수 시간 {funnel['매수 시간 10:00~13:30']}"] +
                        [f"{COND_KO.get(k, FILTER_KO.get(k, k))} {funnel[k]}" for k in keys]))
    base = signals_for(cfg, items)
    fb = first_per_day(base)
    P(f"[신호] 매수 신호 {len(base)}건 (종목·일 첫 신호 {len(fb)}건)")
    if not base:
        P("매수 신호가 없어 승률을 계산할 수 없습니다. 조건별 영향은 위 깔때기를 보세요.")
    # ── ① 공통 표본 ──
    P("")
    P("━━ ① 기본 전략 (급등주 변형: 눌림 저점 기준 3:1, B 모드 봉 마감 확인) - 공통 표본: 종목·일 첫 신호, 계좌 한도 없이 ━━")
    res = {}
    for amb in ("cons", "opt"):
        T, sk = trade_level(cfg, items, base, amb)
        res[amb] = T
        P(f"  [{'보수적' if amb == 'cons' else '낙관적'}] {ft(tstats(T))}" + (f" | 진입 안 됨 {len(sk)}" if sk else ""))
    T = res["cons"]
    if T:
        s = tstats(T)
        P(f"  MFE(진입 후 최대 상승) 평균 {s['mfe']:+.2f}% ({s['mfe_R']:.2f}R) | MAE(최대 하락) 평균 {s['mae']:+.2f}% "
          f"({s['mae_R']:.2f}R) | 익절·손절 같은 봉 모호 {s['amb']}건 | 조기 청산 {s['early']}건")
        P("  청산 사유: " + " / ".join(f"{k} {v}" for k, v in collections.Counter(
            str(t["why"]).split(" (")[0] for t in T).most_common()))
        P("  시간대(뉴욕, 진입 기준): " + breakdown(T, lambda t: f"{ts(t['entry_t']).tz_convert('America/New_York'):%H}시"))
        P("  주가 구간: " + breakdown(T, lambda t: bucket_price(t["avg"]),
                                     ["$0.5~1", "$1~5", "$5~10", "$10~30", "$30 이상"]))
        P("  보유 시간: " + breakdown(T, lambda t: "5분 이하" if t["hold_min"] <= 5 else "5~15분" if t["hold_min"] <= 15
                                     else "15~60분" if t["hold_min"] <= 60 else "60분 초과",
                                     ["5분 이하", "5~15분", "15~60분", "60분 초과"]))
        P("  손절폭(신호 때): " + breakdown(T, lambda t: "1~2%" if t["sl_pct"] < 2 else "2~3%" if t["sl_pct"] < 3
                                       else "3~5%", ["1~2%", "2~3%", "3~5%"]))
        P(f"  비용: 수수료 매수·매도 각 {cfg['fee_pct']:g}% + 슬리피지(가격대별 {cfg['slippage_tiers']}) → 거래당 평균 "
          f"{s['gross'] - s['avg']:.2f}%p 차감")
    # ── ② 계좌 단위 ──
    P("")
    P(f"━━ ② 계좌 단위: 종목당 {cfg['position_pct']:g}% · 최대 {cfg['max_positions']}종목 · 총 {cfg['max_total_pct']:g}% · "
      f"예상 손실 한도 -{cfg['risk_limit_pct']:g}% · 계좌 ±{cfg['account_target_pct']:g}% 잠금 · 1분 거래량 "
      f"{cfg['participation_pct']:g}% 까지 체결 ━━")
    acc = {}
    for amb in ("cons", "opt"):
        rows, AT, miss = account_level(cfg, days, items, base, amb)
        acc[amb] = (rows, AT, miss)
        P(f"  [{'보수적' if amb == 'cons' else '낙관적'}] {fa(astats(rows))}")
        P(f"     거래: {ft(tstats(AT))}")
    rows, AT, miss = acc["cons"]
    if miss:
        P("  신호가 있었지만 못 산 이유: " + " / ".join(f"{k} {v}" for k, v in miss.most_common()))
    P("  [거래가 있었던 날] " + (" / ".join(f"{r['day']} {r['ret']:+.2f}% ({r['trades']}건"
                                         + (f", {r['lock']}" if r["lock"] else "") + ")" for r in rows if r["trades"]) or "없음"))
    if T:
        _compare(cfg, days, items, base, P)
    _save(lines, base, res.get("cons", []), acc["cons"][1], acc["cons"][0])
    P("")
    P(f"(소요 {time.time() - t0:.0f}초)")


def _cmp_line(name, cfg, items, sigs):
    T, _ = trade_level(cfg, items, sigs, "cons")
    To, _ = trade_level(cfg, items, sigs, "opt")
    s, so = tstats(T), tstats(To)
    tail = f" | 낙관적 승률 {so['win']:.0f}% 평균 {so['avg']:+.2f}%" if so.get("n") else ""
    return f"  {name}: {ft(s)}{tail}", T


def _compare(cfg, days, items, base, P):
    fb = first_per_day(base)
    P("")
    P("━━ 비교 (공통 표본, 보수적) ━━")
    # A. 원본 (당일 저점 기준)
    lod = signals_for(cfg, items, "lod")
    P(f"[A·B] 원본 Fashionably Late(당일 저점 L) vs 급등주 변형(1차 급등 후 눌림 저점 L)")
    P(_cmp_line(f"A 원본 (신호 {len(first_per_day(lod))})", with_cfg(cfg, structure="lod"), items, lod)[0])
    P(_cmp_line(f"B 변형 (신호 {len(fb)})", cfg, items, base)[0])
    P("[C] 실시간 교차(A 모드) vs 봉 마감 확인(B 모드): 1분봉만으로는 봉 안에서 언제 교차했는지·그때 체결가를 알 수 없어 "
      "백테스트하지 않음 (요청서 원칙). 실시간 모의가 A 모드 감지 시각·가격을 기록 → python fl.py --compare-ab 로 비교")
    P("[D] 손익절 방식 (같은 신호·같은 진입)")
    P(_cmp_line("동적 3:1 (기본)", cfg, items, base)[0])
    P(_cmp_line("고정 +7% / -7% (조기 청산 켬)", with_cfg(cfg, exit_style="fixed"), items, base)[0])
    off = {k: False for k in cfg["early_exits"]}
    P(_cmp_line("고정 +7% / -7% (조기 청산 끔)", with_cfg(cfg, exit_style="fixed", early_exits=off), items, base)[0])
    P("[E] 거래량 회복 조건(⑨)")
    nv = with_cfg(cfg, **{"conditions.volume": False})
    sv = signals_for(nv, items)
    P(_cmp_line(f"⑨ 끔 (신호 {len(first_per_day(sv))})", nv, items, sv)[0])
    P(_cmp_line(f"⑨ 켬 (기본, 신호 {len(fb)})", cfg, items, base)[0])
    P("[F] 일봉 필터 (선택)")
    for k in ("daily_uptrend", "daily_sma_touch", "daily_volume_up", "daily_resistance"):
        c2 = with_cfg(cfg, **{f"filters.{k}": True})
        s2 = signals_for(c2, items)
        P(_cmp_line(f"{FILTER_KO[k]} (신호 {len(first_per_day(s2))})", c2, items, s2)[0])
    c2 = with_cfg(cfg, **{f"filters.{k}": True for k in ("daily_uptrend", "daily_sma_touch")})
    s2 = signals_for(c2, items)
    P(_cmp_line(f"상승 추세 + 5·10 SMA 눌림 (신호 {len(first_per_day(s2))})", c2, items, s2)[0])
    P("[그 밖의 선택 필터]")
    for k in ("higher_low", "steady_rise"):
        c2 = with_cfg(cfg, **{f"filters.{k}": True})
        s2 = signals_for(c2, items)
        P(_cmp_line(f"{FILTER_KO[k]} (신호 {len(first_per_day(s2))})", c2, items, s2)[0])
    P("[G·H] 계좌 단위 (보수적)")
    for n in (1, 2, 3):
        rows, AT, _ = account_level(with_cfg(cfg, max_positions=n), days, items, base)
        P(f"  최대 {n}종목: {fa(astats(rows))} | 승률 {tstats(AT).get('win', 0):.0f}%")
    rows, AT, _ = account_level(with_cfg(cfg, account_lock=False), days, items, base)
    P(f"  계좌 ±4% 잠금 끔: {fa(astats(rows))}")
    for o, name in (("time", "점수 무시(종목명 순)"), ("reverse", "점수 낮은 순")):
        rows, AT, _ = account_level(cfg, days, items, base, order=o)
        P(f"  우선순위 {name}: {fa(astats(rows))}")
    P("[조기 청산 조건 하나씩 끄기] (지나치게 빠른 청산 확인)")
    for k in cfg["early_exits"]:
        P(_cmp_line(f"{k} 끔", with_cfg(cfg, **{f"early_exits.{k}": False}), items, base)[0])
    P(_cmp_line("조기 청산 전부 끔 (익절·손절·장 마감만)", with_cfg(cfg, early_exits=off), items, base)[0])
    P("[필수 조건 하나씩 끄기] (어떤 조건이 신호를 얼마나 줄이는지)")
    for k in required_keys(cfg, cfg["structure"]):
        if k in cfg["filters"]:
            continue
        c2 = with_cfg(cfg, **{f"conditions.{k}": False})
        s2 = signals_for(c2, items)
        P(_cmp_line(f"{COND_KO[k]} 끔 (신호 {len(first_per_day(s2))})", c2, items, s2)[0])
    T, _ = trade_level(cfg, items, base, "cons")
    if len(T) >= 6:
        P("[우선순위 점수 검증] 점수 3등분 (공통 표본)")
        T = sorted(T, key=lambda t: t["score"] or 0)
        k = len(T) // 3
        for name, part in (("하위", T[:k]), ("중위", T[k:2 * k]), ("상위", T[2 * k:])):
            s = tstats(part)
            P(f"  {name} (점수 {part[0]['score']:.0f}~{part[-1]['score']:.0f}): {s['n']}건 승률 {s['win']:.0f}% 평균 {s['avg']:+.2f}%")
    half = len(days) // 2
    if half:
        P("[기간 나눠 보기] (설정을 이 데이터로 최적화하지 않았음 → 두 기간 모두 표본 밖 성격)")
        for name, ds in (("앞 절반", days[:half]), ("뒤 절반", days[half:])):
            dset = {str(x) for x in ds}
            sub = [s for s in base if s["day"] in dset]
            P(_cmp_line(f"{name} {ds[0]}~{ds[-1]}", cfg, items, sub)[0])


def _save(lines, sigs, common, acct_trades, acct_rows):
    os.makedirs(LOGS, exist_ok=True)
    day = f"{now_et():%Y%m%d}"
    with open(os.path.join(LOGS, f"fl_backtest_{day}.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    for name, data in (("signals", sigs), ("common", common), ("trades", acct_trades), ("days", acct_rows)):
        if data:
            pd.DataFrame(data).to_csv(os.path.join(LOGS, f"fl_backtest_{name}_{day}.csv"), index=False, encoding="utf-8-sig")
    print(f"저장: logs/fl_backtest_{day}.txt 와 CSV")
