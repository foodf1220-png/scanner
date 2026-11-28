# -*- coding: utf-8 -*-
"""
scalp_core.py - 스캘핑 3전략 공통 로직 (지표, 종목 검색, 전략 신호, 청산 시뮬레이션, 계좌, 성과 지표)

규칙: 모든 판단은 그 봉 마감까지의 정보만 사용 (프랙탈·고점은 오른쪽 봉이 마감된 뒤에만 확정)
체결: 토스 방식 - 시장가 매수 (신호 봉 다음 봉 시가 + 슬리피지), 손절·익절 = 조건주문 (닿으면 시장가 매도 - 슬리피지)
"""
import json
import math
import os

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

import fl_core as FC

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "scalp_config.json")
TIME_BUCKETS = [("09:30", "10:00", "장 초반"), ("10:00", "11:00", "오전"), ("11:00", "12:00", "오전 후반"),
                ("12:00", "14:00", "점심"), ("14:00", "15:30", "오후"), ("15:30", "16:00", "마감")]


def load_config(path=CONFIG_FILE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ═════════════════════ 지표 ═════════════════════
def ema(c, n):
    return pd.Series(c).ewm(span=n, adjust=False).mean().to_numpy()


def sma(c, n):
    return pd.Series(c).rolling(n).mean().to_numpy()


def rsi(c, n):
    """와일더 RSI (트레이딩뷰 ta.rsi 와 같은 RMA)"""
    d = np.diff(c, prepend=c[0])
    up = pd.Series(np.clip(d, 0, None)).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    dn = pd.Series(np.clip(-d, 0, None)).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(dn > 0, 100 - 100 / (1 + up / np.where(dn > 0, dn, 1)), np.where(up > 0, 100.0, 50.0))
    r[:n] = np.nan
    return r


def boll(c, n, k):
    s = pd.Series(c)
    mid = s.rolling(n).mean().to_numpy()
    sd = s.rolling(n).std(ddof=0).to_numpy()
    return mid, mid + k * sd, mid - k * sd


def side_min_max(x, n, fn):
    """left[j] = fn(x[j-n:j]), right[j] = fn(x[j+1:j+n+1]) (없으면 nan)"""
    N = len(x)
    left, right = np.full(N, np.nan), np.full(N, np.nan)
    if N > n:
        w = fn(sliding_window_view(x, n), axis=1)        # w[i] = fn(x[i:i+n])
        left[n:] = w[:N - n]
        right[:N - n] = w[1:]
    return left, right


def fractal_low(low, n):
    """좌우 각 n봉보다 낮은 저점 (j 봉). j+n 봉 마감 때 확정"""
    lm, rm = side_min_max(low, n, np.min)
    with np.errstate(invalid="ignore"):
        return (low < lm) & (low < rm)


def pivot_high(high, left, right):
    lm, _ = side_min_max(high, left, np.max)
    _, rm = side_min_max(high, right, np.max)
    with np.errstate(invalid="ignore"):
        return (high > lm) & (high > rm)


# ═════════════════════ 하루 데이터 표 ═════════════════════
def build_day(tk, d, prev_bars, bars, dctx, prof, cfg, W):
    """전날 정규장(지표 예열) + 오늘 정규장 1분봉 -> 배열 묶음. 오늘 봉은 s0 부터"""
    B = pd.concat([prev_bars, bars]) if len(prev_bars) else bars
    s0, n = len(prev_bars), len(B)
    X = dict(tk=tk, d=str(d), s0=s0, n=n, W=W, t=B.index, prev_close=dctx["prev_close"], adv=dctx["adv"])
    for k, col in (("o", "Open"), ("h", "High"), ("l", "Low"), ("c", "Close"), ("v", "Volume")):
        X[k] = B[col].to_numpy(float)
    c, v = X["c"], X["v"]
    e = cfg["ema_lens"]
    X["e20"], X["e50"], X["e100"] = ema(c, e[0]), ema(c, e[1]), ema(c, e[2])
    X["s10"], X["s34"] = sma(c, cfg["sma_lens"][0]), sma(c, cfg["sma_lens"][1])
    X["rsi"] = rsi(c, cfg["rsi_len"])
    X["bbm"], X["bbu"], X["bbl"] = boll(c, cfg["bb_len"], cfg["bb_std"])
    nb = int(cfg["vol_avg_bars"])
    X["va"] = pd.Series(v).shift(1).rolling(nb, min_periods=nb // 2).mean().to_numpy()
    # 오늘 봉만의 값 (누적 거래량·등락률·RVOL·등록)
    cv = np.zeros(n)
    cv[s0:] = np.cumsum(v[s0:])
    X["cumv"] = cv
    dv = np.zeros(n)
    dv[s0:] = np.cumsum(c[s0:] * v[s0:])
    X["dollar"] = dv
    mins = np.full(n, -1)
    mins[s0:] = ((B.index[s0:] - W["open"]).total_seconds() // 60).astype(int)
    X["mins"] = mins
    X["chg"] = (c / dctx["prev_close"] - 1) * 100
    end_m = (mins + 1).astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        if prof is not None:
            den = np.interp(end_m, np.arange(len(prof)), prof)
            X["rvol_src"] = "tod"
        else:                                                 # 과거 5분봉 부족 -> 일 거래량 비례로 대신 (건수 따로 셈)
            den = dctx["adv"] * end_m / 390 if dctx["adv"] == dctx["adv"] else np.full(n, np.nan)
            X["rvol_src"] = "daily_linear"
        X["rvol"] = np.where(den > 0, cv / np.where(den > 0, den, 1), np.nan)
        X["rvol_daily"] = cv / dctx["adv"] if dctx["adv"] and dctx["adv"] == dctx["adv"] else np.full(n, np.nan)
    rv = X["rvol"] if cfg["rvol_method"] == "tod" else X["rvol_daily"]
    ok = np.zeros(n, bool)
    with np.errstate(invalid="ignore"):
        ok[s0:] = (X["chg"][s0:] >= cfg["min_change_pct"]) & (cv[s0:] >= cfg["min_cum_volume"]) & (rv[s0:] >= cfg["min_rvol"])
    X["reg"] = np.maximum.accumulate(ok)                      # 한 번 조건을 넘으면 그날 감시 목록에 남음
    X["reg_t"] = int(np.argmax(ok)) if ok.any() else None
    dh = np.full(n, np.nan)
    dh[s0:] = np.maximum.accumulate(X["h"][s0:])
    X["dayhigh"] = dh
    t = B.index
    X["flat_k"] = int(np.searchsorted(t, W["flat"]))          # 강제 청산 봉 (이 봉 시가에 매도)
    X["buy_end"] = W["buy_end"]
    return X


# ═════════════════════ 전략 1: EMA 20/50/100 + Williams Fractal ═════════════════════
def strat1(X, cfg, n=None, allow100=False):
    n = n or cfg["fractal_n"]
    c, l = X["c"], X["l"]
    e20, e50, e100 = X["e20"], X["e50"], X["e100"]
    fr = fractal_low(l, n)
    late, maxb = cfg["fractal_late_bars"], cfg["episode_max_bars"]
    out, miss, ep = [], dict(fractal_late=0, expired=0, below100=0), None
    lv_of = {"E20": e20, "E50": e50, "E100": e100}
    for t in range(max(X["s0"], 1), X["n"]):
        if ep is None:
            if c[t] < e20[t] and c[t - 1] >= e20[t - 1] and e20[t - 1] > e50[t - 1] > e100[t - 1]:
                ep = dict(start=t, deep=False, b100=False, fbar=None, fconf=None, cross=None, typ=None)
            continue
        if c[t] < e100[t] and not ep["b100"]:
            if not allow100:
                miss["below100"] += 1
                ep = None
                continue
            ep["b100"] = True
        if c[t] < e50[t]:
            ep["deep"] = True
        j = t - n
        if ep["fconf"] is None and j >= ep["start"] and fr[j]:
            ep["fbar"], ep["fconf"] = j, t
        typ = ep["typ"] or ("E100" if ep["b100"] else "E50" if ep["deep"] else "E20")
        lv = lv_of[typ]
        if ep["cross"] is None:
            if c[t] > lv[t] and c[t - 1] <= lv[t - 1]:
                ep["cross"], ep["typ"] = t, typ
        elif c[t] < lv[t]:                                    # 돌파 후 다시 밑으로 -> 눌림 계속
            ep["cross"], ep["typ"] = None, None
        if ep["cross"] is not None and ep["fconf"] is not None and c[t] > lv[t] and e20[t] > e50[t] > e100[t]:
            out.append(dict(i=t, strat="S1", typ=ep["typ"], fbar=ep["fbar"], fconf=ep["fconf"],
                            lag_conf=t - ep["fconf"], lag_low=t - ep["fbar"], cross_lag=t - ep["cross"],
                            struct=float(l[ep["fbar"]])))
            ep = None
            continue
        if ep["cross"] is not None and t - ep["cross"] >= late:
            miss["fractal_late"] += 1
            ep = None
        elif t - ep["start"] > maxb:
            miss["expired"] += 1
            ep = None
    return out, miss


# ═════════════════════ 전략 2: SMA 10/34 + RSI 10 ═════════════════════
def is_pin(o, h, l, c, j):
    rng = h[j] - l[j]
    if rng <= 0:
        return False
    body = abs(c[j] - o[j])
    lower = min(o[j], c[j]) - l[j]
    return lower >= 2 * body and lower >= 0.5 * rng and c[j] >= l[j] + rng * 2 / 3


def is_engulf(o, c, j):
    return c[j - 1] < o[j - 1] and c[j] > o[j] and o[j] <= c[j - 1] and c[j] >= o[j - 1]


def is_tri(h, l, c, j, nb):
    if j - nb < 0:
        return False
    x = np.arange(nb)
    sh = np.polyfit(x, h[j - nb:j], 1)[0]
    sl = np.polyfit(x, l[j - nb:j], 1)[0]
    return sh < 0 < sl and c[j] > h[j - 5:j].max()


def strat2(X, cfg):
    o, h, l, c, v = X["o"], X["h"], X["l"], X["c"], X["v"]
    r, s10, s34, va = X["rsi"], X["s10"], X["s34"], X["va"]
    L, lo_b, hi_b, fb, pb = cfg["rsi_level"], *cfg["rsi_flat_band"], cfg["rsi_flat_bars"], cfg["rsi_prior_bars"]
    sb = cfg["sma_slope_bars"]
    out, miss = [], dict(flat=0)
    for t in range(max(X["s0"], pb + 1, cfg["sma_lens"][1] + sb), X["n"]):
        if not (r[t - 1] < L <= r[t]):
            continue
        if not (s10[t] > s34[t] and s34[t] > s34[t - sb]):
            continue
        prior = r[t - pb:t - 1]
        if not (np.nanmax(prior) >= L if np.isfinite(prior).any() else False):
            continue
        w = r[t - fb:t]
        if np.all((w >= lo_b) & (w <= hi_b)):
            miss["flat"] += 1
            continue
        pin = is_pin(o, h, l, c, t) or is_pin(o, h, l, c, t - 1)
        eng = is_engulf(o, c, t)
        tri = is_tri(h, l, c, t, cfg["pattern_tri_bars"])
        out.append(dict(i=t, strat="S2", vol_ok=bool(v[t] >= cfg["vol_mult"] * va[t]) if va[t] == va[t] else False,
                        pin=pin, eng=eng, tri=tri, pattern=bool(pin or eng or tri), struct=float(l[t]),
                        support=float(l[t - 10:t].min())))
    return out, miss


# ═════════════════════ 전략 3: 볼린저 + 하락 추세선 돌파 ═════════════════════
def strat3(X, cfg, mode="close"):
    """mode close = 봉 종가가 추세선 위 (다음 봉 시가 매수) / rt = 봉 안에서 추세선을 넘는 순간 (추세선 가격 매수)"""
    o, h, l, c, v, va = X["o"], X["h"], X["l"], X["c"], X["v"], X["va"]
    mid, up, lo = X["bbm"], X["bbu"], X["bbl"]
    pl, pr = cfg["pivot_left"], cfg["pivot_right"]
    ph = pivot_high(h, pl, pr)
    lb, rmin, ms = cfg["bb_ride_lookback"], cfg["bb_ride_min"], cfg["bb_mid_slope_bars"]
    tol, exp_b = cfg["p2_mid_tol_pct"] / 100, cfg["pattern_expire_bars"]
    with np.errstate(invalid="ignore"):
        ride = (h >= up).astype(int)
    rcum = np.concatenate([[0], np.cumsum(ride)])
    out, miss = [], dict(new_high=0, no_p2=0, broke_low=0, no_break=0)
    st = None
    start = max(X["s0"], lb + pr + 1, cfg["bb_len"] + ms + pr)
    for t in range(start, X["n"]):
        j = t - pr
        newp = bool(ph[j])
        if st is None or st["corr"] is None:
            if newp and h[j] >= up[j] and rcum[j + 1] - rcum[j + 1 - lb] >= rmin and mid[j] > mid[j - ms]:
                if st is None or h[j] > st["h1"]:
                    st = dict(p1=j, h1=float(h[j]), corr=None)
            if st is None:
                continue
            if h[t] > st["h1"] and not newp:
                st = None
                continue
            if t > st["p1"] + pr - 1 and c[t] < mid[t] and l[t] <= mid[t] - 0.5 * (mid[t] - lo[t]):
                seg = l[st["p1"] + 1:t + 1]
                st.update(corr=t, pull=float(seg.min()), pullbar=st["p1"] + 1 + int(np.argmin(seg)), p2=None)
            continue
        if st["p2"] is None:
            if h[t] > st["h1"]:
                miss["new_high"] += 1
                st = None
                continue
            if l[t] < st["pull"]:
                st["pull"], st["pullbar"] = float(l[t]), t
            if newp and j > st["pullbar"] and h[j] < st["h1"] and abs(h[j] / mid[j] - 1) <= tol:
                st.update(p2=j, h2=float(h[j]), c2=t, slope=(h[j] - st["h1"]) / (j - st["p1"]))
            elif t - st["corr"] > exp_b:
                miss["no_p2"] += 1
                st = None
            continue
        line = st["h2"] + st["slope"] * (t - st["p2"])
        if l[t] < st["pull"]:
            miss["broke_low"] += 1
            st = None
            continue
        hit = (c[t] > line) if mode == "close" else (t > st["c2"] and h[t] > line)
        if hit:
            px = None if mode == "close" else float(max(o[t], line))
            bstop = max(st["pull"], float(lo[t])) if lo[t] == lo[t] else st["pull"]
            reg = c[t] / c[t - cfg["regime_bars"]] - 1 if t >= cfg["regime_bars"] else np.nan
            out.append(dict(i=t, strat="S3", mode=mode, rt_px=px, vol_ok=bool(v[t] >= cfg["vol_mult"] * va[t]) if va[t] == va[t] else False,
                            struct=float(bstop), pull=st["pull"], line=float(line), h1=st["h1"], h2=st["h2"],
                            regime=("강한 상승" if reg >= 0.03 else "횡보" if abs(reg) < 0.01 else "보통") if reg == reg else "?"))
            st = None
        elif t - st["c2"] > exp_b:
            miss["no_break"] += 1
            st = None
    return out, miss


# ═════════════════════ 신호 -> 진입 정보 ═════════════════════
def time_bucket(t):
    hm = f"{pd.Timestamp(t):%H:%M}"
    for a, b, name in TIME_BUCKETS:
        if a <= hm < b:
            return name
    return "기타"


def score_parts(X, i):
    c, v, va = X["c"], X["v"], X["va"]
    pers = float(np.mean(v[i - 9:i + 1] >= va[i - 9:i + 1])) if i >= 10 else 0.0
    dh = X["dayhigh"][i]
    retr = (dh - c[i]) / dh * 100 if dh and dh == dh else 0.0
    hl = bool(X["l"][i - 4:i + 1].min() > X["l"][i - 9:i - 4].min()) if i >= 10 else False
    ma = sum(c[i] > X[k][i] for k in ("e20", "e50", "e100"))
    rv = X["rvol"][i]
    return dict(chg=min(max(X["chg"][i], 0) / 30, 1), rvol=min(math.log(max(rv, 1)) / math.log(30), 1) if rv == rv else 0,
                dollar_vol=min(max(math.log10(max(X["dollar"][i], 1)) - 5, 0) / 3, 1), vol_persist=pers,
                retrace=1 - min(retr / 20, 1), higher_low=float(hl), above_ma=ma / 3)


def entries(X, sigs, cfg):
    """신호 -> 진입 (봉 마감 신호는 다음 봉 시가, rt 는 그 봉 안 추세선 가격). 등록·예열·시간 조건 확인"""
    out, drop = [], dict(not_registered=0, warmup=0, late=0, no_next=0)
    s0, n, t = X["s0"], X["n"], X["t"]
    w = cfg["score_weights"]
    for s in sigs:
        i = s["i"]
        rt = s.get("rt_px") is not None
        k = i if rt else i + 1
        reg_i = i - 1 if rt else i
        if reg_i < s0 or not X["reg"][reg_i]:
            drop["not_registered"] += 1
            continue
        if i < cfg["warmup_bars"]:
            drop["warmup"] += 1
            continue
        if k >= n:
            drop["no_next"] += 1
            continue
        if t[k] > X["buy_end"] or k >= X["flat_k"]:
            drop["late"] += 1
            continue
        E = s["rt_px"] if rt else X["o"][k]
        sp = score_parts(X, i)
        out.append(dict(s, tk=X["tk"], day=X["d"], k=k, t_sig=t[i], t_in=t[k], E_raw=float(E), rt=rt,
                        price=float(E), chg=float(X["chg"][i]), rvol=float(X["rvol"][i]), rvol_daily=float(X["rvol_daily"][i]),
                        rvol_src=X["rvol_src"], dollar=float(X["dollar"][i]), bucket=time_bucket(t[k]),
                        score=sum(w[k2] * sp[k2] for k2 in w) / sum(w.values())))
    return out, drop


# ═════════════════════ 청산 시뮬레이션 ═════════════════════
def slip_of(price, cfg, scen):
    return FC.slip_pct(price, cfg) / 100 if scen == "tier" else float(scen) / 100


def sim_exit(X, e, tp, sl, cfg, scen="tier", fee=None, tstop=None, use_struct=False):
    """한 신호의 청산. tstop = (분, 정의) 또는 None. 반환: 수익률(%) 등"""
    o, h, l, c, v, t = X["o"], X["h"], X["l"], X["c"], X["v"], X["t"]
    fee = cfg["fee_pct"] / 100 if fee is None else fee
    k, n, end = e["k"], X["n"], X["flat_k"]
    sb = slip_of(e["E_raw"], cfg, scen)
    E = e["E_raw"] * (1 + sb)
    SL, TP = FC.round_near(E * (1 - sl / 100)), FC.round_near(E * (1 + tp / 100))
    if use_struct and e.get("struct") and e["struct"] < E:
        SL = max(SL, FC.round_near(e["struct"]))
    hs = l[k:end] <= SL
    ht = h[k:end] >= TP
    if e["rt"]:                                           # 돌파 봉 안에서 산 경우: 그 봉은 종가·고가로만 판정
        hs[0] = c[k] <= SL
    hit = hs | ht
    j = k + int(np.argmax(hit)) if hit.any() else None
    q, why, amb = None, None, False
    if tstop:
        m, kind = tstop
        q = k + m - 1
        if q < end and (j is None or j > q):
            ts = cfg["time_stop"]
            if kind == "ret":
                cut = c[q] < E * (1 + ts["ret_min_pct"] / 100)
            elif kind == "mfe":
                cut = h[k:q + 1].max() < E * (1 + ts["mfe_frac"] * tp / 100)
            else:
                pre = v[max(k - ts["vol_pre_bars"], 0):k]
                cut = len(pre) > 0 and v[k:q + 1].mean() < ts["vol_frac"] * pre.mean() and c[q] <= E
            if cut:
                j, why, X_px, ex_k = None, "time", c[q], q
    if why is None:
        if j is not None:
            if j > k and o[j] <= SL:
                why, X_px = "sl", o[j]
            elif j > k and o[j] >= TP:
                why, X_px = "tp", o[j]
            elif hs[j - k] and ht[j - k]:
                why, X_px, amb = "sl", SL, True
            elif hs[j - k]:
                why, X_px = "sl", SL
            else:
                why, X_px = "tp", TP
            ex_k = j
        else:
            ex_k = end if end < n else n - 1
            why, X_px = "flat", (o[end] if end < n else c[n - 1])
    ss = slip_of(X_px, cfg, scen)
    Xs = X_px * (1 - ss)
    ret = (Xs * (1 - fee)) / (E * (1 + fee)) - 1
    hold = (t[ex_k] - t[k]).total_seconds() / 60 + (1.0 if why == "time" else 0.5 if why in ("sl", "tp") else 0.0)
    seg_end = min(ex_k + 1, n)
    mfe = h[k:seg_end].max() / E - 1
    after = slice(ex_k + 1, min(ex_k + 1 + cfg["after_exit_bars"], end))
    tp_after = bool(why == "sl" and (h[after] >= TP).any())
    extra = float(h[after].max() / TP - 1) if why == "tp" and len(h[after]) else 0.0
    return dict(ret=ret * 100, why=why, amb=amb, E=E, SL=SL, TP=TP, X=Xs, ex_k=ex_k, t_out=t[ex_k], hold=hold,
                mfe=mfe * 100, tp_after=tp_after, extra=extra * 100, sb=sb, ss=ss)


# ═════════════════════ 성과 지표 ═════════════════════
def streaks(wins):
    best_w = best_l = cw = cl = 0
    for w in wins:
        if w:
            cw, cl = cw + 1, 0
        else:
            cl, cw = cl + 1, 0
        best_w, best_l = max(best_w, cw), max(best_l, cl)
    return best_w, best_l


def stats(rows):
    """rows: dict(ret %, day, hold, why, amb, tp_after, extra, t_in) 시간순"""
    if not rows:
        return dict(n=0)
    rows = sorted(rows, key=lambda r: (str(r["t_in"]), r.get("tk", "")))
    r = np.array([x["ret"] for x in rows])
    w, ls = r[r > 0], r[r <= 0]
    cum = np.cumsum(r)
    mdd = float(np.max(np.maximum.accumulate(np.concatenate([[0], cum])) - np.concatenate([[0], cum])))
    by_day = {}
    for x in rows:
        by_day[x["day"]] = by_day.get(x["day"], 0) + x["ret"]
    bw, bl = streaks(r > 0)
    tp = [x for x in rows if x["why"] == "tp"]
    return dict(n=len(r), win=len(w), loss=len(ls), wr=len(w) / len(r) * 100, avg_w=float(w.mean()) if len(w) else 0.0,
                avg_l=float(ls.mean()) if len(ls) else 0.0,
                payoff=float(w.mean() / -ls.mean()) if len(w) and len(ls) and ls.mean() < 0 else float("nan"),
                exp=float(r.mean()), cum=float(cum[-1]), mdd=mdd, max_lstreak=bl, max_wstreak=bw,
                day_min=min(by_day.values()), day_max=max(by_day.values()), days=len(by_day),
                hold=float(np.mean([x["hold"] for x in rows])), hold_max=float(max(x["hold"] for x in rows)),
                amb=sum(x["amb"] for x in rows), tp_after=sum(x["tp_after"] for x in rows),
                extra=float(np.mean([x["extra"] for x in tp])) if tp else 0.0,
                why={k: sum(1 for x in rows if x["why"] == k) for k in ("tp", "sl", "time", "flat", "acct")})


# ═════════════════════ 계좌 시뮬레이션 ═════════════════════
def qqq_frames(m1_qqq, days, cfg):
    """QQQ 그날 시가 대비 % (봉 마감 시각 기준) - 시장 급락 시 신규 진입 제한용"""
    out = {}
    for d in days:
        W = FC.day_windows(d, cfg)
        b = FC.regular_1m(m1_qqq, W) if W else None
        if b is None or b.empty:
            continue
        out[d] = pd.Series((b["Close"].to_numpy() / b["Open"].iloc[0] - 1) * 100, index=b.index + pd.Timedelta(minutes=1))
    return out


def sim_account(trades, Xs, days, cfg, qqq=None, integer=False, start_equity=None, **kw):
    """trades: 청산 결과가 붙은 진입 목록 (한 가지 손익절·슬리피지). 시각 순서대로:
    ① 그 봉 시가 진입 (점수 높은 순) ② 그 봉에서 끝나는 청산 ③ 봉 마감 평가 -> 일일 손실 한도면 전량 청산·중단
    kw: max_positions, position_pct, daily_loss_pct, reentry, reentry_wait_min, consec_loss_stop, market_filter_pct"""
    A = {**cfg["account"], **kw}
    fee = cfg["fee_pct"] / 100
    eq = float(start_equity if start_equity is not None else A["start_equity"])
    eq0, curve, done, lock_days = eq, [eq], [], 0
    skipped = dict(slots=0, held=0, reentry=0, locked=0, market=0, consec=0, cash=0, zero_qty=0)
    by_day = {}
    for x in trades:
        by_day.setdefault(x["day"], []).append(x)
    for d in days:
        day_tr = by_day.get(d, [])
        start_eq, cash, realized = eq, eq, 0.0
        open_, last_exit, entered, consec, locked = [], {}, set(), 0, False
        at_t = {}
        for x in sorted(day_tr, key=lambda x: (-x["score"], x["tk"])):
            at_t.setdefault(x["t_in"], []).append(x)
        grid = set(at_t)
        for x in day_tr:
            grid.update(Xs[(x["tk"], d)]["t"][x["k"]:x["ex_k"] + 1])
        mk = qqq.get(d) if qqq else None

        def close(p, px, why, T):
            nonlocal cash, realized, consec
            open_.remove(p)
            proceeds = p["qty"] * px * (1 - fee)
            pnl = proceeds - p["cost"]
            cash += proceeds
            realized += pnl
            last_exit[p["tk"]] = T
            consec = consec + 1 if pnl <= 0 else 0
            done.append(dict(p, X=px, why=why, t_out=T, pnl=pnl, ret=(px * (1 - fee) / (p["E"] * (1 + fee)) - 1) * 100,
                             ret_acct=pnl / start_eq * 100))
            curve.append(start_eq + realized)

        for T in sorted(grid):
            for x in at_t.get(T, []):
                if locked:
                    skipped["locked"] += 1
                    continue
                if A.get("consec_loss_stop") and consec >= A["consec_loss_stop"]:
                    skipped["consec"] += 1
                    continue
                if A.get("market_filter_pct") is not None and mk is not None:
                    qv = mk.asof(T)
                    if qv == qv and qv <= -A["market_filter_pct"]:
                        skipped["market"] += 1
                        continue
                if any(p["tk"] == x["tk"] for p in open_):
                    skipped["held"] += 1
                    continue
                if x["tk"] in entered and (not A["reentry"] or T - last_exit[x["tk"]] < pd.Timedelta(minutes=A["reentry_wait_min"])):
                    skipped["reentry"] += 1
                    continue
                if len(open_) >= A["max_positions"]:
                    skipped["slots"] += 1
                    continue
                budget = min(start_eq * A["position_pct"] / 100, cash)
                qty = math.floor(budget / (x["E"] * (1 + fee))) if integer else budget / (x["E"] * (1 + fee))
                if qty <= 0 or (not integer and budget < start_eq * 0.01):
                    skipped["zero_qty" if integer else "cash"] += 1
                    continue
                cost = qty * x["E"] * (1 + fee)
                cash -= cost
                X = Xs[(x["tk"], d)]
                open_.append(dict(x, qty=qty, cost=cost, _t=X["t"], _c=X["c"]))
                entered.add(x["tk"])
            for p in [p for p in open_ if p["t_out"] <= T]:
                close(p, p["X"], p["why"], T)
            if not locked:
                px_now = []
                for p in open_:
                    kk = max(int(np.searchsorted(p["_t"], T, side="right")) - 1, p["k"])
                    px_now.append((p, p["_c"][kk]))
                mtm = realized + sum(p["qty"] * px * (1 - fee) - p["cost"] for p, px in px_now)
                if mtm <= -A["daily_loss_pct"] / 100 * start_eq:
                    locked = True
                    lock_days += 1
                    for p, px in px_now:
                        close(p, px * (1 - slip_of(px, cfg, p["scen"])), "acct", T)
        for p in list(open_):
            close(p, p["X"], p["why"], p["t_out"])
        eq = start_eq + realized
    c = np.array(curve)
    peak = np.maximum.accumulate(c)
    return dict(trades=done, final=eq, ret=(eq / eq0 - 1) * 100, mdd=float(np.max((peak - c) / peak) * 100),
                skipped=skipped, lock_days=lock_days, start=eq0)


