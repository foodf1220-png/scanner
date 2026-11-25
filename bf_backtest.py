# -*- coding: utf-8 -*-
"""
bf_backtest.py - Bull Flag V3.1 백테스트 (python bf_backtest.py)

데이터: 야후 1분봉(최근 30일 한도) + 5분봉(RVOL 14거래일, 5분 9EMA) + 일봉 + 유통주식수(Ticker.info, 현재 값)
비교: V1~V6 (V4·V6 은 틱·초봉·호가 데이터가 없어 백테스트 불가 표시), V3.1 = 학습 구간에서 고른 필터 조합
  1) 신호 단위 (계좌 한도 없이 신호마다 1번)  2) 3종목·30%·총 90% (계좌 ±4% 없음)  3) + 계좌 ±4% 청산
학습/검증: 날짜순 앞 60% 학습, 뒤 40% 검증. V3.1 조합·손익절 개선안은 학습 구간에서만 고르고 검증 구간은 확인만
"""
import collections
import itertools
import json
import os
import sys
import time
from datetime import timedelta

import numpy as np
import pandas as pd

import bf_core as BC
import fl_backtest as FB
import fl_core as FC
from fl_core import MIN, Account, bar_exit, day_windows, ts

CACHE = os.path.join(FC.HERE, "cache")
CHART_DIR = os.path.join(FC.LOGS, "bf_charts")
TP_GRID = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0]
SL_GRID = [2.0, 3.0, 4.0, 5.0, 6.0]


# ═════════════════════ 데이터 ═════════════════════
def float_of(floats, tk):
    f = floats.get(tk) or {}
    return f.get("float") or f.get("shares")


def has_day(df, d):
    if df is None or len(df) == 0:
        return False
    return d in set(df.index.tz_convert("America/New_York").date)


def load(cfg, P):
    from pullback import download
    from scanner import load_universe
    c = pd.read_pickle(os.path.join(CACHE, "bf_cand.pkl"))
    days, cand = c["days"], c["cand"]
    daily = pd.read_pickle(os.path.join(CACHE, f"bf_daily_{days[-1]:%Y%m%d}.pkl"))
    floats = json.load(open(os.path.join(CACHE, "bf_float.json"), encoding="utf-8"))
    uni = load_universe()
    n_pairs = sum(map(len, cand.values()))
    keep, unknown, big = {}, 0, 0
    for tk, ds in cand.items():
        fv = float_of(floats, tk)
        if fv is None:
            unknown += len(ds)
            if cfg["float_unknown"] != "exclude":
                keep[tk] = ds
        elif fv < cfg["max_float"]:
            keep[tk] = ds
        else:
            big += len(ds)
    P(f"[검색 범위] 보통주 {len(uni):,}개 → 일봉 필요조건(고가 ≥ 전일 종가 +{cfg['min_change_pct']:g}%, $0.5~20 구간) "
      f"{n_pairs}건 ({len(cand)}종목) → 유통주식수 {cfg['max_float'] / 1e6:g}백만 주 미만 {sum(map(len, keep.values()))}건 "
      f"({len(keep)}종목) | 탈락: 유통주식 초과 {big}건, 유통주식 정보 없음 {unknown}건")
    path = os.path.join(CACHE, f"bf_bt_{days[0]:%Y%m%d}_{days[-1]:%Y%m%d}.pkl")
    bc = pd.read_pickle(path) if os.path.exists(path) else dict(m1={}, h5={})
    m1, h5 = bc["m1"], bc["h5"]
    for f in sorted(os.listdir(CACHE)):                              # FL 백테스트 캐시 재사용
        if f.startswith("fl_bt_") and f.endswith(".pkl"):
            fc = pd.read_pickle(os.path.join(CACHE, f))
            for tk in keep:
                if tk in fc.get("m1", {}) and tk not in m1:
                    m1[tk] = fc["m1"][tk]
                if fc.get("h5", {}).get(tk) is not None and tk not in h5:
                    h5[tk] = fc["h5"][tk]
    need = {(tk, d) for tk, ds in keep.items() for d in ds if not has_day(m1.get(tk), d)}
    print(f"[1분봉] 필요 {sum(map(len, keep.values()))}건 중 캐시 없음 {len(need)}건 받기", flush=True)
    w = days[0]
    while w <= days[-1]:
        we = w + timedelta(days=7)
        tks = sorted({tk for tk, d in need if w <= d < we})
        if tks:
            got = download(tks, chunk=50, start=str(w), end=str(we), interval="1m", prepost=False, quiet=False)
            for tk, df in got.items():
                m1[tk] = pd.concat([m1[tk], df]) if tk in m1 else df
        w = we
    m1 = {tk: df[~df.index.duplicated(keep="last")].sort_index() for tk, df in m1.items()}
    miss5 = sorted(tk for tk in keep if h5.get(tk) is None)
    print(f"[5분봉] RVOL 14일·5분 EMA 용 {len(miss5)}종목 받기", flush=True)
    if miss5:
        s5 = max(days[0] - timedelta(days=30), FC.now_et().date() - timedelta(days=58))
        h5.update(download(miss5, chunk=50, start=str(s5), end=str(days[-1] + timedelta(days=1)), interval="5m",
                           prepost=False, quiet=False))
    pd.to_pickle(dict(m1=m1, h5=h5), path)
    return days, keep, daily, floats, m1, h5


def build(cfg, days, keep, daily, floats, m1, h5, P):
    items, recs, ev, skip = {}, [], collections.Counter(), collections.Counter()
    for tk in sorted(keep):
        for d in keep[tk]:
            it, why = FB.build_day(tk, d, cfg, m1, h5, daily)
            if not it:
                skip[why] += 1
                continue
            W = it["W"]
            m5 = BC.m5_context(it["F"], h5.get(tk), d)
            out, e = BC.detect(it["F"], cfg, W, tk, float_of(floats, tk), m5)
            ev.update(e)
            it["m5"] = m5
            items[(tk, str(d))] = it
            recs += out
    recs.sort(key=lambda r: (r["day"], r["t"], r["tk"]))
    for i, r in enumerate(recs):
        r["id"] = i
    P(f"[데이터] 1분봉 확보 {len(items)}건 (종목·일)" + (" | " + ", ".join(f"{k} {v}" for k, v in skip.items()) if skip else ""))
    return items, recs, ev


# ═════════════════════ 계좌 단위 시뮬레이션 ═════════════════════
def sim_account(cfg, days, items, recs, amb="cons"):
    """3종목·30%·총 90%·예비 10%, 매수 스톱 체결(기준가 또는 시가), 1분 거래량 10% 까지, 계좌 ±4% (cfg account_lock)"""
    eq = float(cfg["capital"])
    rows, trades = [], []
    by_day = collections.defaultdict(list)
    for r in recs:
        by_day[r["day"]].append(r)
    for d in days:
        ds = str(d)
        W = day_windows(d, cfg)
        frames = {k[0]: it["F"] for k, it in items.items() if k[1] == ds}
        st = dict(day=ds, start_eq=eq, cash=eq)
        acct = Account(cfg, st)
        A = {tk: tuple(F[x].to_numpy() for x in ("Open", "High", "Low", "Close", "Volume")) for tk, F in frames.items()}
        pos_of = {tk: {t: j for j, t in enumerate(F.index)} for tk, F in frames.items()}
        times = sorted(set().union(*[set(F.index) for F in frames.values()])) if frames else []
        by_t = collections.defaultdict(list)
        for r in by_day.get(ds, []):
            by_t[ts(r["t"])].append(r)
        cap = acct.cap_qty
        for t in [x for x in times if x <= W["flat"]]:
            bars = {tk: pos_of[tk][t] for tk in frames if t in pos_of[tk]}
            Pp = st["pos"]
            if t >= W["flat"]:
                for tk in list(Pp):
                    if tk in bars:
                        acct.sell(tk, A[tk][0][bars[tk]], t, "장 마감 청산", "market", cap(A[tk][4][bars[tk]]))
                continue
            for tk in list(Pp):
                if tk in bars and Pp[tk]["exit_flag"]:
                    acct.sell(tk, A[tk][0][bars[tk]], t, Pp[tk]["exit_flag"], "market", cap(A[tk][4][bars[tk]]))
            mh = cfg.get("max_hold_min")
            for tk in list(Pp):
                p = Pp.get(tk)
                if p and tk in bars and mh and (t - ts(p["entry_min"])).total_seconds() / 60 >= mh:
                    acct.sell(tk, A[tk][0][bars[tk]], t, "시간 청산", "market", cap(A[tk][4][bars[tk]]))
            for tk in list(Pp):
                p = Pp.get(tk)
                if p and tk in bars and ts(p["entry_min"]) < t:
                    j = bars[tk]
                    rr = bar_exit(p, A[tk][0][j], A[tk][1][j], A[tk][2][j], False, amb)
                    if rr:
                        px, why, a, kind = rr
                        p["amb"] = p["amb"] or a
                        acct.sell(tk, px, t, "익절" if kind == "limit" else "손절", kind, cap(A[tk][4][j]))
            for r in sorted(by_t.get(t, []), key=lambda r: (r["pole_no"], r["tk"])):
                tk = r["tk"]
                j = bars.get(tk)
                if j is None:
                    continue
                s = dict(tk=tk, day=ds, mode="BF", struct="bullflag", t=r["t"], px=r["entry_raw"], L=r["pb_low"],
                         score=None, rvol=r["rvol"])
                o = acct.submit(s, t, r["entry_raw"])
                if not o:
                    continue
                acct.fill_buy(o, r["entry_raw"], t, cap(A[tk][4][j]), j=j)
                if o["status"] == "open":                         # 매수 스톱: 남은 수량은 쫓지 않음
                    o["status"] = "partial" if o["filled"] else "expired"
                p = Pp.get(tk)
                if p:
                    cl = A[tk][3][j]
                    if cl <= p["SL"]:
                        acct.sell(tk, p["SL"], t, "손절", "stop", cap(A[tk][4][j]))
                    elif cl >= p["TP"]:
                        acct.sell(tk, p["TP"], t, "익절", "limit", cap(A[tk][4][j]))
            for tk, j in bars.items():
                p = Pp.get(tk)
                fresh = p is not None and ts(p["entry_min"]) == t
                acct.mark(tk, A[tk][3][j], None if (p is None or fresh) else A[tk][1][j],
                          None if (p is None or fresh) else A[tk][2][j])
            acct.check_account(t + MIN)
            acct.lock_done(t + MIN)
            st["max_conc"] = max(st["max_conc"], len(Pp))
        for tk in list(st["pos"]):
            acct.sell(tk, acct.px(tk), W["flat"], "장 마감 청산 (마지막 시세)", "market")
        end = acct.equity()
        lk = st["locked"]
        rows.append(dict(day=ds, start=eq, end=end, ret=(end / eq - 1) * 100, trades=len(st["trades"]),
                         lock=lk["why"] if lk else "", max_conc=st["max_conc"], missed=dict(st["missed"])))
        trades += st["trades"]
        eq = end
    return rows, trades


# ═════════════════════ 통계 ═════════════════════
def cat(why):
    w = str(why)
    return ("TP" if w.startswith("익절") else "SL" if w.startswith("손절") else "ACC" if w.startswith("계좌")
            else "TIME" if w.startswith("시간") else "EOD")


def tstats(T):
    n = len(T)
    if not n:
        return dict(n=0)
    T = sorted(T, key=lambda t: t.get("t") or t.get("entry_t"))
    net = np.array([t["net_pct"] for t in T])
    k = collections.Counter(cat(t["why"]) for t in T)
    wsum, lsum = net[net > 0].sum(), net[net <= 0].sum()
    streak = best = 0
    for t in T:
        streak = streak + 1 if cat(t["why"]) == "SL" else 0
        best = max(best, streak)
    g = lambda key: float(np.mean([t[key] for t in T if t.get(key) is not None])) if any(t.get(key) is not None for t in T) else float("nan")
    return dict(n=n, tp=k["TP"], sl=k["SL"], eod=k["EOD"], acc=k["ACC"], win=(net > 0).mean() * 100,
                win4=k["TP"] / (k["TP"] + k["SL"]) * 100 if k["TP"] + k["SL"] else float("nan"),
                avg=net.mean(), sum=net.sum(), comp=(np.prod(1 + net / 100) - 1) * 100,
                pf=wsum / abs(lsum) if lsum < 0 else float("inf"), consec=best,
                fake=np.mean([t["fake"] for t in T]) * 100 if "fake" in T[0] else float("nan"),
                hold=g("hold_min"), mfe=g("mfe_pct"), mae=g("mae_pct"), pre=g("pre_cost_pct"), setp=g("set_pct"),
                amb=sum(1 for t in T if t.get("amb")))


def fmt(s):
    if not s.get("n"):
        return "거래 0건"
    pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"{s['n']}건 (익 {s['tp']} / 손 {s['sl']} / 마감 {s['eod']}" + (f" / 계좌 {s['acc']}" if s.get("acc") else "")
            + f") | 승률 {s['win']:.0f}% (±4% 도달만 " + (f"{s['win4']:.0f}%" if s['win4'] == s['win4'] else "-")
            + f") | 평균 {s['avg']:+.2f}%" + (f" (비용 전 {s['pre']:+.2f}%)" if s['pre'] == s['pre'] else "")
            + f" | 합계 {s['sum']:+.1f}% | PF {pf} | 연속 손절 {s['consec']}"
            + (f" | 가짜 돌파 {s['fake']:.0f}%" if s['fake'] == s['fake'] else "")
            + f" | 보유 {s['hold']:.0f}분 | MFE {s['mfe']:+.1f}% MAE {s['mae']:+.1f}%")


def astats(rows, trades):
    r = np.array([x["ret"] for x in rows])
    eq = np.concatenate([[1.0], np.cumprod(1 + r / 100)])
    mdd = (eq / np.maximum.accumulate(eq) - 1).min() * 100
    return dict(cum=(eq[-1] - 1) * 100, mdd=mdd, avg=r.mean(), up=sum(1 for x in rows if x["lock"].startswith("계좌 +")),
                dn=sum(1 for x in rows if x["lock"].startswith("계좌 -")), days=sum(1 for x in rows if x["trades"]))


# ═════════════════════ 차트 ═════════════════════
def chart(it, rec, res, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False
    F = it["F"]
    j = rec["j"]
    k_exit = int(F.index.searchsorted(ts(res["exit_t"])))
    a, b = max(0, rec["si"] - 15), min(len(F), max(k_exit, j) + 12)
    S = F.iloc[a:b]
    x = np.arange(len(S))
    fig, (ax, axv) = plt.subplots(2, 1, figsize=(12, 7), sharex=True, gridspec_kw=dict(height_ratios=[3, 1]))
    up = S["Close"] >= S["Open"]
    ax.vlines(x, S["Low"], S["High"], color="#555", lw=0.8)
    ax.bar(x, (S["Close"] - S["Open"]).abs().clip(lower=1e-6), bottom=np.minimum(S["Open"], S["Close"]),
           color=np.where(up, "#d33", "#36c"), width=0.7)
    ax.plot(x, S["vwap"], color="#e90", lw=1.2, label="VWAP")
    ax.plot(x, S["ema"], color="#9c27b0", lw=1, label="1분 9EMA")
    rel = lambda k: k - a
    ax.scatter([rel(rec["si"])], [rec["start"]], marker="^", s=80, color="green", zorder=5, label="깃대 시작")
    ax.scatter([rel(rec["hi"])], [rec["pole_high"]], marker="v", s=80, color="black", zorder=5, label="깃대 고점")
    ax.axhline(rec["pb_low"], color="gray", ls=":", lw=1, label="눌림 저점")
    ax.axhline(rec["trigger"], color="#e91e63", ls="--", lw=1, label="매수 기준가")
    ax.axhline(res["TP"], color="#2e7d32", ls="-.", lw=1, label="익절가")
    ax.axhline(res["SL"], color="#c62828", ls="-.", lw=1, label="손절가")
    ax.scatter([rel(j)], [res["avg"]], marker="o", s=70, color="#e91e63", zorder=6, label="실제 진입")
    ax.scatter([rel(k_exit)], [res["exit_px"]], marker="x", s=80, color="black", zorder=6, label="청산")
    ax.set_xlim(-1, len(S))
    ax.legend(loc="upper left", fontsize=7, ncol=3)
    ax.set_title(title, fontsize=10)
    axv.bar(x, S["Volume"], color=np.where(up, "#d33", "#36c"), width=0.7)
    step = max(1, len(S) // 12)
    axv.set_xticks(x[::step])
    axv.set_xticklabels([f"{t:%H:%M}" for t in S.index[::step]], fontsize=7)
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


# ═════════════════════ 본체 ═════════════════════
def main():
    t0 = time.time()
    cfg = BC.load_config()
    lines = []

    def P(s=""):
        print(s, flush=True)
        lines.append(s)

    days, keep, daily, floats, m1, h5 = load(cfg, P)
    nt = int(len(days) * cfg["train_frac"])
    train, valid = {str(d) for d in days[:nt]}, {str(d) for d in days[nt:]}
    P(f"Bull Flag V3.1 백테스트 | {days[0]} ~ {days[-1]} ({len(days)}거래일) | 학습 {days[0]}~{days[nt - 1]} ({nt}일) · "
      f"검증 {days[nt]}~{days[-1]} ({len(days) - nt}일) | 1분봉 정규장, 신규 매수 09:31~15:30 ET, 15:50 ET 청산")
    items, recs, ev = build(cfg, days, keep, daily, floats, m1, h5, P)
    P("[조건별 탈락 (종목·일 전체)]")
    order = ["급등 포착 (5봉 안 +5%)", "검색기 탈락: 가격 $0.5~20 밖", "검색기 탈락: 당일 상승률 10% 미만", "검색기 탈락: RVOL 3배 미만",
             "TRACKING 시작 (검색기 통과)", "깃대 확정 (첫 조정봉)"]
    for k in order + sorted(k for k in ev if k not in order):
        P(f"  {k}: {ev[k]}")

    res = {}                                                     # 신호 단위 결과 (기본 ±4%)
    for amb in ("cons", "opt"):
        res[amb] = {r["id"]: BC.sim_signal(items[(r["tk"], r["day"])]["F"], r, cfg, items[(r["tk"], r["day"])]["W"],
                                           cfg["tp_pct"], cfg["sl_pct"], amb) for r in recs}
    sel = lambda keys, period=None: [r for r in recs if BC.passes(r, keys) and (period is None or r["day"] in period)]
    T = lambda rs, amb="cons": [res[amb][r["id"]] for r in rs]

    P("")
    P(f"━━ 1) 신호 단위 성능 (계좌 한도 없이, 익절 +{cfg['tp_pct']:g}% / 손절 -{cfg['sl_pct']:g}%, 같은 봉 모호하면 손절 먼저) ━━")
    for name, keys in BC.VERSIONS.items():
        blocked = [BC.FILTERS[k][0] for k in keys if not BC.FILTERS[k][1]]
        newb = [k for k in keys if not BC.FILTERS[k][1] and k not in BC.VERSIONS.get(f"V{int(name[1]) - 1}", [])] \
            if name != "V1" else []
        if newb:
            P(f"  {name}: 백테스트 불가 - {', '.join(BC.FILTERS[k][0] for k in newb)} 는 틱·초봉·호가 데이터 필요 (야후 1분봉만 있음)")
            if name == "V6":
                continue
            P(f"      (참고: 그 조건을 빼고 앞 버전 조건만으로 계산)")
        for pname, per in (("전체", None), ("학습", train), ("검증", valid)):
            P(f"  {name} [{pname}] {fmt(tstats(T(sel(keys, per))))}")
    P("  ※ I(돌파 거래량)는 돌파 봉 전체 거래량으로 근사 (봉 안의 돌파 이후 거래량 포함 → 실제보다 유리)")
    P("  ※ 진입 봉 안에서는 진입 전후 순서를 알 수 없어 그 봉 종가로만 손익절 판단")
    P(f"  [낙관적 (같은 봉 모호하면 익절 먼저)] V1 {fmt(tstats(T(sel([]), 'opt')))}")

    # ── V3.1: 학습 구간에서 필터 조합 고르기 ──
    P("")
    P("━━ V3.1 = 학습 구간에서 고른 필터 조합 (검증 구간으로는 고르지 않음) ━━")
    v1_train = len(sel([], train))
    min_n = max(8, int(0.15 * v1_train))
    cands = []
    for m in range(len(BC.TESTABLE) + 1):
        for combo in itertools.combinations(BC.TESTABLE, m):
            s = tstats(T(sel(list(combo), train)))
            if s.get("n", 0) >= min_n:
                cands.append((s["avg"], s["pf"], combo, s))
    cands.sort(key=lambda x: (-x[0], -x[1]))
    P(f"  후보 {len(cands)}개 조합 (학습 거래 {min_n}건 이상), 기준 = 학습 구간 거래당 평균 수익률(비용 반영)")
    for avg, pf, combo, s in cands[:5]:
        vs = tstats(T(sel(list(combo), valid)))
        P(f"  · {'+'.join(combo) or '필터 없음'}: 학습 {s['n']}건 승률 {s['win']:.0f}% 평균 {s['avg']:+.2f}% → "
          f"검증 {vs.get('n', 0)}건" + (f" 승률 {vs['win']:.0f}% 평균 {vs['avg']:+.2f}%" if vs.get("n") else ""))
    best = list(cands[0][2]) if cands else []
    P(f"  ▶ V3.1 = {' + '.join(BC.FILTERS[k][0] for k in best) or '필터 없음 (V1)'}")
    for pname, per in (("학습", train), ("검증", valid), ("전체", None)):
        P(f"  V3.1 [{pname}] {fmt(tstats(T(sel(best, per))))}")

    # ── 계좌 단위 ──
    P("")
    P("━━ 2) 3종목 · 종목당 30% · 총 90% · 현금 10% (계좌 ±4% 없음)  /  3) + 계좌 ±4% 청산 ━━")
    acct_res = {}
    for name, keys in [(k, v) for k, v in BC.VERSIONS.items() if k in ("V1", "V2", "V3", "V5")] + [("V3.1", best)]:
        for lock in (False, True):
            c2 = BC.derive(cfg, lock=lock)
            rows, AT = sim_account(c2, days, items, sel(keys))
            a = astats(rows, AT)
            acct_res[(name, lock)] = (rows, AT)
            P(f"  {name} {'3) ±4% 청산' if lock else '2) 비중만'}: 누적 {a['cum']:+.2f}% · MDD {a['mdd']:.2f}% · 거래일 {a['days']} · "
              f"+4% 도달 {a['up']}일 · -4% 도달 {a['dn']}일")
            P(f"      {fmt(tstats(AT))}")
    rows, AT = acct_res[("V3.1", True)]
    miss = collections.Counter()
    for r in rows:
        miss.update(r["missed"])
    acc_tr = [t for t in AT if cat(t["why"]) == "ACC"]
    P(f"  V3.1 계좌 ±4% 청산 때문에 ±4% 전에 정리된 거래: {len(acc_tr)}건"
      + (f" (평균 {np.mean([t['net_pct'] for t in acc_tr]):+.2f}%)" if acc_tr else ""))
    if miss:
        P("  V3.1 못 산 신호: " + " / ".join(f"{k} {v}" for k, v in miss.most_common()))

    # ── 손익절 개선 (학습 구간에서 고르고 검증) ──
    P("")
    P(f"━━ 손익절 개선안 (V3.1 신호, 학습 구간 거래당 평균으로 선택 → 검증 확인) ━━")
    rs_tr, rs_va = sel(best, train), sel(best, valid)
    grid = {}
    for tp in TP_GRID:
        for sl in SL_GRID:
            g = lambda rs: tstats([BC.sim_signal(items[(r["tk"], r["day"])]["F"], r, cfg, items[(r["tk"], r["day"])]["W"],
                                                 tp, sl) for r in rs])
            grid[(tp, sl)] = (g(rs_tr), g(rs_va))
    P("  학습 구간 거래당 평균(%) - 행: 익절, 열: 손절")
    P("        " + "".join(f"  -{sl:g}%  " for sl in SL_GRID))
    for tp in TP_GRID:
        P(f"  +{tp:g}%  " + "".join(f"{grid[(tp, sl)][0].get('avg', float('nan')):+7.2f} " for sl in SL_GRID))
    ok = [(k, v) for k, v in grid.items() if v[0].get("n")]
    bk, (btr, bva) = max(ok, key=lambda kv: kv[1][0]["avg"])
    base = grid[(cfg["tp_pct"], cfg["sl_pct"])]
    P(f"  기본 +{cfg['tp_pct']:g}/-{cfg['sl_pct']:g}%: 학습 {fmt(base[0])}")
    P(f"                     검증 {fmt(base[1])}")
    P(f"  ▶ 학습 최선 +{bk[0]:g}/-{bk[1]:g}%: 학습 {fmt(btr)}")
    P(f"                     검증 {fmt(bva)}")

    # ── 차트 ──
    os.makedirs(CHART_DIR, exist_ok=True)
    for sub in ("win", "loss"):
        os.makedirs(os.path.join(CHART_DIR, sub), exist_ok=True)
        for f in os.listdir(os.path.join(CHART_DIR, sub)):
            os.remove(os.path.join(CHART_DIR, sub, f))
    nchart = 0
    for r in sel(best):
        rr = res["cons"][r["id"]]
        it = items[(r["tk"], r["day"])]
        sub = "win" if rr["net_pct"] > 0 else "loss"
        title = (f"{r['tk']} {r['day']} {ts(r['t']):%H:%M}ET | 깃대 +{r['pole_pct']:.1f}% ({r['pole_bars']}봉) 눌림 {r['pb_bars']}봉 "
                 f"되돌림 {r['retr']:.0f}% | 진입 {rr['avg']:.3f} → {rr['why']} {rr['net_pct']:+.2f}% ({rr['hold_min']:.0f}분)")
        try:
            chart(it, r, rr, os.path.join(CHART_DIR, sub, f"{r['day']}_{r['tk']}_{ts(r['t']):%H%M}.png"), title)
            nchart += 1
        except Exception as e:
            print("차트 실패", r["tk"], e)
    P("")
    P(f"차트 {nchart}장 저장: logs/bf_charts/win, logs/bf_charts/loss (V3.1 신호 전부)")
    day = f"{FC.now_et():%Y%m%d}"
    with open(os.path.join(FC.LOGS, f"bf_backtest_{day}.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    pd.DataFrame([dict({k: v for k, v in r.items() if k not in ("pb", "flags")}, **{f"f_{k}": v for k, v in r["flags"].items()},
                       **{f"r_{k}": v for k, v in res["cons"][r["id"]].items() if k not in ("tk", "day", "t")})
                  for r in recs]).to_csv(os.path.join(FC.LOGS, f"bf_backtest_signals_{day}.csv"), index=False, encoding="utf-8-sig")
    P(f"저장: logs/bf_backtest_{day}.txt, logs/bf_backtest_signals_{day}.csv (소요 {time.time() - t0:.0f}초)")


if __name__ == "__main__":
    main()
