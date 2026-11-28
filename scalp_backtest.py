# -*- coding: utf-8 -*-
"""
scalp_backtest.py - 스캘핑 3전략 x 손익절 A/B/C 백테스트 (python scalp_backtest.py)

데이터: SCALP_DATA 폴더 (기본 C:\\Users\\hyeonil\\scalp_data) - 야후 1분봉 (11/02~11/27 + 전날 예열), 5분봉 (RVOL), 일봉
체결: 토스 방식 (시장가 매수, 조건주문 = 닿으면 시장가 매도), 수수료 편도 0.1%, 슬리피지 가격대별 (+ 민감도)
결과: logs/scalp_report.md
"""
import collections
import os
import random
import time

import numpy as np
import pandas as pd

import fl_core as FC
import scalp_core as SC

DATA = os.environ.get("SCALP_DATA", r"C:\Users\hyeonil\scalp_data")
REPORT = os.path.join(FC.LOGS, "scalp_report.md")
STUDY_FROM = "2026-11-02"
NAMES = {"S1": "EMA+프랙탈", "S2": "SMA+RSI", "S3": "볼린저+추세선", "ALL": "3전략 통합"}
DETECTORS = [
    ("S1", lambda X, c: SC.strat1(X, c)),
    ("S1_n3", lambda X, c: SC.strat1(X, c, n=c["fractal_n_alt"])),
    ("S1_100", lambda X, c: SC.strat1(X, c, allow100=True)),
    ("S2", SC.strat2),
    ("S3", lambda X, c: SC.strat3(X, c, "close")),
    ("S3_rt", lambda X, c: SC.strat3(X, c, "rt")),
]


# ═════════════════════ 데이터 ═════════════════════
def load():
    daily = pd.read_pickle(os.path.join(DATA, "daily.pkl"))
    c = pd.read_pickle(os.path.join(DATA, "cands.pkl"))
    parts = collections.defaultdict(list)
    for i in range(5):
        for tk, df in pd.read_pickle(os.path.join(DATA, f"m1_w{i}.pkl")).items():
            parts[tk].append(df)
    m1 = {}
    for tk, dfs in parts.items():
        df = pd.concat(dfs) if len(dfs) > 1 else dfs[0]
        m1[tk] = df[~df.index.duplicated(keep="last")].sort_index()
    h5 = pd.read_pickle(os.path.join(DATA, "h5.pkl"))
    return daily, c["days"], c["cands"], m1, h5


def prepare(cfg, P):
    daily, days_all, cands, m1, h5 = load()
    study = [d for d in days_all if d >= STUDY_FROM]
    prev = {d: days_all[i - 1] for i, d in enumerate(days_all) if i}
    Xs, raw, E = {}, {}, collections.defaultdict(list)
    drop, cnt, miss = collections.Counter(), collections.Counter(), collections.defaultdict(collections.Counter)
    lim = int(os.environ.get("SCALP_MAX_CANDS", "0"))              # 빠른 시험용 (하루 후보 수 제한)
    for d in study:
        W, Wp = FC.day_windows(d, cfg), FC.day_windows(prev[d], cfg)
        for tk in (cands.get(d, [])[:lim] if lim else cands.get(d, [])):
            cnt["후보"] += 1
            bars = FC.regular_1m(m1.get(tk), W)
            if bars.empty:
                drop["그날 1분봉 없음"] += 1
                continue
            dctx = FC.daily_context(daily.get(tk), d, cfg)
            if not dctx:
                drop["전일 종가 없음"] += 1
                continue
            dd = daily[tk]
            today = dd[[str(pd.Timestamp(x).date()) == d for x in dd.index]]
            if len(today) and abs(bars["Close"].iloc[-1] / float(today["Close"].iloc[0]) - 1) > 0.10:
                drop["일봉·분봉 종가 10% 넘게 다름 (분할 의심)"] += 1
                continue
            prevb = FC.regular_1m(m1.get(tk), Wp)
            if len(prevb) and abs(prevb["Close"].iloc[-1] / dctx["prev_close"] - 1) > 0.10:
                k = prevb["Close"].iloc[-1] / dctx["prev_close"]
                dctx["prev_close"] *= k
                dctx["adv"] /= k
                cnt["전일 종가를 분봉 기준으로 맞춤"] += 1
            prof = FC.rvol_profile(h5.get(tk), d, cfg)
            X = SC.build_day(tk, d, prevb, bars, dctx, prof, cfg, W)
            if X["reg_t"] is None:
                drop["장중 검색 조건 미달 (+5%·50만 주·RVOL 3)"] += 1
                continue
            cnt["감시 목록 등록"] += 1
            cnt["RVOL " + X["rvol_src"]] += 1
            if not len(prevb):
                cnt["전날 분봉 없음 (예열 부족)"] += 1
            got = False
            for name, fn in DETECTORS:
                sigs, ms = fn(X, cfg)
                miss[name].update(ms)
                es, dr = SC.entries(X, sigs, cfg)
                miss[name].update({"진입 제외 " + k: v for k, v in dr.items()})
                if es:
                    E[name] += es
                    got = True
            if got:
                Xs[(tk, d)] = X
                raw[(tk, d)] = (prevb, bars, dctx, prof, W)
    P(f"[데이터] 분석 {len(study)}거래일 ({study[0]}~{study[-1]}), 일봉 후보 {cnt['후보']}건 -> 감시 목록 {cnt['감시 목록 등록']}건")
    return study, Xs, raw, E, drop, cnt, miss


def lookahead_check(cfg, Xs, raw, E, n_each=40, seed=7):
    """신호 봉까지만 잘라서 다시 계산해도 그 봉까지의 신호가 똑같은지 (미래 데이터 참조 검사)"""
    rnd = random.Random(seed)
    res = {}
    for name, fn in DETECTORS:
        ok = bad = 0
        pool = E[name]
        for e in rnd.sample(pool, min(n_each, len(pool))):
            X = Xs[(e["tk"], e["day"])]
            prevb, bars, dctx, prof, W = raw[(e["tk"], e["day"])]
            i = e["i"]
            full = sorted(s["i"] for s in fn(X, cfg)[0] if s["i"] <= i)
            Xt = SC.build_day(e["tk"], e["day"], prevb, bars.iloc[:i - X["s0"] + 1], dict(dctx), prof, cfg, W)
            cut = sorted(s["i"] for s in fn(Xt, cfg)[0])
            if full == cut and i in cut:
                ok += 1
            else:
                bad += 1
        res[name] = (ok, bad)
    return res


# ═════════════════════ 시뮬레이션 ═════════════════════
def run(cfg, Xs, entries, ex, scen="tier", fee=None, tstop=None, struct=False):
    tp, sl = cfg["exits"][ex]
    out = []
    for e in entries:
        r = SC.sim_exit(Xs[(e["tk"], e["day"])], e, tp, sl, cfg, scen, fee, tstop, struct)
        out.append({**e, **r, "scen": scen})
    return out


# ═════════════════════ 표 ═════════════════════
def f2(x, d=2, sign=True):
    if x is None or x != x:
        return "-"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def basic_head(first="구분"):
    return [f"| {first} | 거래 | 승 | 패 | 승률 | 평균 수익 | 평균 손실 | 손익비 | 거래당 기대값 | 누적 (거래별 합) | MDD |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]


def basic_row(name, s):
    if not s.get("n"):
        return f"| {name} | 0 | | | | | | | | | |"
    return (f"| {name} | {s['n']} | {s['win']} | {s['loss']} | {s['wr']:.0f}% | {f2(s['avg_w'])}% | {f2(s['avg_l'])}% | "
            f"{f2(s['payoff'], 2, False)} | **{f2(s['exp'])}%** | {f2(s['cum'], 1)}%p | -{s['mdd']:.1f}%p |")


def risk_head(first="구분"):
    return [f"| {first} | 최대 연속 손실 | 최대 연속 수익 | 일일 최대 손실 | 일일 최대 수익 | 평균 보유 | 최대 보유 | "
            f"손절 후 익절선 도달 | 익절 후 추가 상승 | 같은 봉 손익절 (손절 처리) | 익절/손절/시간/마감 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]


def risk_row(name, s):
    if not s.get("n"):
        return f"| {name} | | | | | | | | | | |"
    w = s["why"]
    return (f"| {name} | {s['max_lstreak']} | {s['max_wstreak']} | {f2(s['day_min'], 1)}%p | {f2(s['day_max'], 1)}%p | "
            f"{s['hold']:.1f}분 | {s['hold_max']:.0f}분 | {s['tp_after']}건 ({s['tp_after'] / max(w['sl'], 1) * 100:.0f}%) | "
            f"{f2(s['extra'])}% | {s['amb']}건 | {w['tp']}/{w['sl']}/{w['time']}/{w['flat']} |")


def acct_head(first="구분"):
    return [f"| {first} | 거래 | 승률 | 거래당 평균 | 누적 수익률 | MDD | 손실 한도 날 | 못 산 신호 (슬롯/보유중/재진입/한도) |",
            "|---|---:|---:|---:|---:|---:|---:|---|"]


def acct_row(name, a):
    T = a["trades"]
    n = len(T)
    wr = sum(t["pnl"] > 0 for t in T) / n * 100 if n else 0
    avg = np.mean([t["ret"] for t in T]) if n else float("nan")
    k = a["skipped"]
    return (f"| {name} | {n} | {wr:.0f}% | {f2(avg)}% | **{f2(a['ret'], 1)}%** | -{a['mdd']:.1f}% | {a['lock_days']} | "
            f"{k['slots']}/{k['held']}/{k['reentry']}/{k['locked']} |")


def bucket_table(cfg, Xs, base, key_fn, order, title, P):
    P(f"\n**{title}** (거래 수는 B 기준, 값 = 거래당 기대값, 가격대별 슬리피지·수수료 반영)\n")
    P("| 전략 | 구간 | 거래 | 승률(B) | A 기대값 | B 기대값 | C 기대값 | 평균 수익(B) | 평균 손실(B) | 평균 보유(B) | 최대 연속 손실(B) |")
    P("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for sn, rows in base.items():
        for g in order:
            sel = [e for e in rows if key_fn(e) == g]
            if not sel:
                continue
            S = {ex: SC.stats(run(cfg, Xs, sel, ex)) for ex in "ABC"}
            b = S["B"]
            P(f"| {NAMES[sn]} | {g} | {b['n']} | {b['wr']:.0f}% | {f2(S['A']['exp'])}% | {f2(b['exp'])}% | {f2(S['C']['exp'])}% | "
              f"{f2(b['avg_w'])}% | {f2(b['avg_l'])}% | {b['hold']:.1f}분 | {b['max_lstreak']} |")


def price_b(e):
    p = e["price"]
    return "$1 미만" if p < 1 else "$1~3" if p < 3 else "$3~10" if p < 10 else "$10~20" if p < 20 else "$20~50" if p < 50 else "$50 이상"


def dollar_b(e):
    x = e["dollar"] / 1e6
    return "$1M 미만" if x < 1 else "$1~5M" if x < 5 else "$5~20M" if x < 20 else "$20~100M" if x < 100 else "$100M 이상"


def rvol_b(e):
    r = e["rvol"]
    return "3~5" if r < 5 else "5~10" if r < 10 else "10~20" if r < 20 else "20 이상"


def rvold_b(e):
    r = e["rvol_daily"]
    if r != r:
        return "?"
    return "1 미만" if r < 1 else "1~2" if r < 2 else "2~5" if r < 5 else "5 이상"


def mkt_b(e):
    q = e.get("mkt")
    if q is None or q != q:
        return "?"
    return "QQQ 상승 (+0.5% 이상)" if q >= 0.5 else "QQQ 하락 (-0.5% 이하)" if q <= -0.5 else "QQQ 보합"


# ═════════════════════ 실행 ═════════════════════
def main():
    t0 = time.time()
    cfg = SC.load_config()
    lines = []

    def P(s=""):
        print(s, flush=True)
        lines.append(s)

    study, Xs, raw, E, drop, cnt, miss = prepare(cfg, P)
    qqq_m1 = pd.read_pickle(os.path.join(DATA, "m1_w0.pkl")).get("QQQ")
    parts = [pd.read_pickle(os.path.join(DATA, f"m1_w{i}.pkl")).get("QQQ") for i in range(5)]
    qqq_m1 = pd.concat([p for p in parts if p is not None])
    qqq_m1 = qqq_m1[~qqq_m1.index.duplicated(keep="last")].sort_index()
    qqq = SC.qqq_frames(qqq_m1, study, cfg)
    for es in E.values():
        for e in es:
            q = qqq.get(e["day"])
            e["mkt"] = float(q.asof(e["t_in"])) if q is not None else float("nan")
    base = {"S1": [e for e in E["S1"] if e["typ"] in ("E20", "E50")], "S2": list(E["S2"]),
            "S3": [e for e in E["S3"] if e["vol_ok"]]}
    allb = sorted(base["S1"] + base["S2"] + base["S3"], key=lambda e: (e["t_in"], -e["score"]))
    half = study[:cfg["train_days"]], study[cfg["train_days"]:]

    P("# 스캘핑 3전략 × 손익절 A/B/C 백테스트")
    P(f"\n- 기간: {study[0]} ~ {study[-1]} ({len(study)}거래일, 야후 1분봉 30일 한도) · 학습 {half[0][0]}~{half[0][-1]} / 검증 {half[1][0]}~{half[1][-1]}")
    P("- 체결 (토스): 신호 봉 마감 -> 다음 봉 시가에 시장가 매수 / 손절·익절은 조건주문 = 가격에 닿으면 시장가 매도 (익절도 슬리피지)")
    P("- 비용: 수수료 편도 0.1% + 슬리피지 편도 ($1 미만 0.5%, $5 미만 0.2%, $10 미만 0.1%, 그 이상 0.05%)")
    P("- 손익절: A +1.5/-1.0, B +2.0/-1.5, C +3.0/-2.0 (체결가 기준, 가까운 호가로) · 마감 10분 전 시가 강제 청산 · 신규 매수 15:45 ET 까지")
    P("- 같은 1분봉 안에서 손절·익절이 둘 다 닿으면 손절로 처리 (틱 데이터 없음) -> 건수 따로 표시")
    P(f"- 일봉 후보 {cnt['후보']}건 -> 장중 검색 조건 통과 {cnt['감시 목록 등록']}건 | 제외: " + ", ".join(f"{k} {v}" for k, v in drop.items()))
    P(f"- RVOL: 같은 시각대 10일 기준 {cnt['RVOL tod']}건, 5분봉 부족해서 일 거래량 비례로 대신 {cnt['RVOL daily_linear']}건 · "
      f"전날 분봉 없음 {cnt['전날 분봉 없음 (예열 부족)']}건 · 분할 보정 {cnt['전일 종가를 분봉 기준으로 맞춤']}건")
    P(f"- 신호(진입) 수: " + ", ".join(f"{k} {len(v)}" for k, v in E.items()))
    for k, v in miss.items():
        P(f"  - {k} 탈락·제외: " + ", ".join(f"{a} {b}" for a, b in v.items() if b))
    la = lookahead_check(cfg, Xs, raw, E)
    P("- 미래 데이터 참조 검사 (신호 봉까지 잘라서 다시 계산 -> 같은 신호): " +
      ", ".join(f"{k} {ok}/{ok + bad} 통과" for k, (ok, bad) in la.items()))
    P(f"- 데이터로 못 한 것: 스프레드 0.5% 필터(호가 없음), 30초 시간 손절(초 단위 없음), 뉴스·희석 위험 분류, 상장폐지 종목 포함(현재 상장 종목만 = 생존 편향), 지정가·부분 체결")

    # 1. 전략별 A/B/C
    R = {(sn, ex): run(cfg, Xs, rows, ex) for sn, rows in {**base, "ALL": allb}.items() for ex in "ABC"}
    P("\n## 1. 전략별 A/B/C 기본 성과 (신호 단위: 자금 제약 없이 신호마다 1번, 비용 반영)\n")
    for x in basic_head():
        P(x)
    for sn in ("S1", "S2", "S3", "ALL"):
        for ex in "ABC":
            P(basic_row(f"{NAMES[sn]} {ex}", SC.stats(R[(sn, ex)])))
    P("\n**위험 지표**\n")
    for x in risk_head():
        P(x)
    for sn in ("S1", "S2", "S3", "ALL"):
        for ex in "ABC":
            P(risk_row(f"{NAMES[sn]} {ex}", SC.stats(R[(sn, ex)])))

    # 2. 계좌
    P("\n## 2. 계좌 단위 (동시 4종목 × 25%, 같은 종목 그날 1번, 일일 -3% 전량 청산·중단, 복리)\n")
    for x in acct_head():
        P(x)
    for sn in ("S1", "S2", "S3", "ALL"):
        for ex in "ABC":
            P(acct_row(f"{NAMES[sn]} {ex}", SC.sim_account(R[(sn, ex)], Xs, study, cfg)))

    # 3. 슬리피지 민감도
    P("\n## 3. 슬리피지 민감도 (거래당 기대값 / 누적 합) - 수수료 0.1% 는 '비용 전' 만 0\n")
    P("| 전략 | 손익절 | 비용 전 | 슬리피지 0% | 0.05% | 0.1% | 0.2% | 가격대별 (기본) |")
    P("|---|---|---:|---:|---:|---:|---:|---:|")
    for sn in ("S1", "S2", "S3", "ALL"):
        rows = base[sn] if sn != "ALL" else allb
        for ex in "ABC":
            cells = [SC.stats(run(cfg, Xs, rows, ex, 0.0, fee=0.0))]
            cells += [SC.stats(run(cfg, Xs, rows, ex, s)) for s in cfg["slippage_scenarios"]]
            cells.append(SC.stats(R[(sn, ex)]))
            P(f"| {NAMES[sn]} | {ex} | " + " | ".join(f"{f2(s['exp'])}% / {f2(s['cum'], 0)}" for s in cells) + " |")

    # 4. 학습/검증
    P("\n## 4. 학습 구간 vs 검증 구간 (같은 조건, 기대값 / 승률 / 거래)\n")
    P("| 전략 | 손익절 | 학습 | 검증 |")
    P("|---|---|---|---|")
    for sn in ("S1", "S2", "S3", "ALL"):
        for ex in "ABC":
            c = []
            for hd in half:
                s = SC.stats([r for r in R[(sn, ex)] if r["day"] in hd])
                c.append(f"{f2(s['exp'])}% / {s['wr']:.0f}% / {s['n']}" if s["n"] else "0건")
            P(f"| {NAMES[sn]} | {ex} | {c[0]} | {c[1]} |")

    # 5. 시간대
    P("\n## 5. 시간대별 성과 (진입 시각, 미국 동부)")
    order = [b[2] for b in SC.TIME_BUCKETS]
    bucket_table(cfg, Xs, base, lambda e: e["bucket"], order, "시간대별", P)
    P("\n**장 초반 30분 (09:30~10:00) 제외 시**\n")
    P("| 전략 | 손익절 | 전체 기대값 | 제외 후 기대값 | 전체 계좌 | 제외 후 계좌 |")
    P("|---|---|---:|---:|---:|---:|")
    for sn in ("S1", "S2", "S3", "ALL"):
        for ex in "ABC":
            allr = R[(sn, ex)]
            ex30 = [r for r in allr if r["bucket"] != "장 초반"]
            a1 = SC.sim_account(allr, Xs, study, cfg)
            a2 = SC.sim_account(ex30, Xs, study, cfg)
            P(f"| {NAMES[sn]} | {ex} | {f2(SC.stats(allr)['exp'])}% | {f2(SC.stats(ex30).get('exp'))}% | {f2(a1['ret'], 1)}% | {f2(a2['ret'], 1)}% |")

    # 6. 시간 손절
    P("\n## 6. 시간 손절 (진입 후 m분 시점 판정, 30초는 1분봉으로 불가) - 거래당 기대값 / 승률\n")
    TS = [None] + [(m, k) for m in cfg["time_stop"]["minutes"] for k in ("ret", "mfe", "mom")]
    lab = {None: "없음", "ret": "수익률", "mfe": "MFE", "mom": "모멘텀"}
    P("| 전략 | 손익절 | " + " | ".join("없음" if t is None else f"{t[0]}분 {lab[t[1]]}" for t in TS) + " |")
    P("|---|---|" + "---:|" * len(TS))
    tsres = {}
    for sn in ("S1", "S2", "S3"):
        for ex in "ABC":
            cells = []
            for t in TS:
                s = SC.stats(run(cfg, Xs, base[sn], ex, tstop=t))
                tsres[(sn, ex, t)] = s
                cells.append(f"{f2(s['exp'])}% / {s['wr']:.0f}%")
            P(f"| {NAMES[sn]} | {ex} | " + " | ".join(cells) + " |")

    # 7. 구조적 손절
    P("\n## 7. 구조적 손절 (고정 % 와 구조 손절가 중 먼저 닿는 쪽) - 전략1 프랙탈 저점, 전략2 신호 봉 저점, 전략3 조정 저점·하단 밴드\n")
    for x in basic_head():
        P(x)
    for sn in ("S1", "S2", "S3"):
        for ex in "ABC":
            P(basic_row(f"{NAMES[sn]} {ex} 고정%", SC.stats(R[(sn, ex)])))
            P(basic_row(f"{NAMES[sn]} {ex} +구조", SC.stats(run(cfg, Xs, base[sn], ex, struct=True))))

    # 8. 전략별 세부
    P("\n## 8. 전략별 세부 비교 (거래당 기대값, A / B / C)\n")
    P("| 비교 | 거래 | 승률(B) | A | B | C |")
    P("|---|---:|---:|---:|---:|---:|")

    def line(label, rows):
        if not rows:
            P(f"| {label} | 0 | | | | |")
            return
        S = {ex: SC.stats(run(cfg, Xs, rows, ex)) for ex in "ABC"}
        P(f"| {label} | {S['B']['n']} | {S['B']['wr']:.0f}% | {f2(S['A']['exp'])}% | {f2(S['B']['exp'])}% | {f2(S['C']['exp'])}% |")

    s1 = E["S1"]
    line("전략1 EMA20 눌림 (기본)", [e for e in s1 if e["typ"] == "E20"])
    line("전략1 EMA50 깊은 눌림 (기본)", [e for e in s1 if e["typ"] == "E50"])
    line("전략1 EMA100 회복 (실험, 기본 규칙상 금지)", [e for e in E["S1_100"] if e["typ"] == "E100"])
    line(f"전략1 프랙탈 {cfg['fractal_n_alt']} (좌우 {cfg['fractal_n_alt']}봉, 실험)", [e for e in E["S1_n3"] if e["typ"] in ("E20", "E50")])
    if s1:
        P(f"| 전략1 프랙탈 확정→진입 평균 {np.mean([e['lag_conf'] for e in s1]):.1f}봉, 저점→진입 평균 {np.mean([e['lag_low'] for e in s1]):.1f}봉 | | | | | |")
    s2 = E["S2"]
    line("전략2 기본 (거래량·패턴 조건 없음)", s2)
    line("전략2 + 거래량 1.5배", [e for e in s2 if e["vol_ok"]])
    line("전략2 거래량 부족", [e for e in s2 if not e["vol_ok"]])
    line("전략2 + 캔들 패턴 (아무거나)", [e for e in s2 if e["pattern"]])
    line("전략2 패턴 없음", [e for e in s2 if not e["pattern"]])
    line("전략2 핀바", [e for e in s2 if e["pin"]])
    line("전략2 상승 장악형", [e for e in s2 if e["eng"]])
    line("전략2 삼각 수렴 돌파", [e for e in s2 if e["tri"]])
    s3 = E["S3"]
    line("전략3 종가 돌파 + 거래량 (기본)", [e for e in s3 if e["vol_ok"]])
    line("전략3 종가 돌파, 거래량 없음", [e for e in s3 if not e["vol_ok"]])
    line("전략3 실시간 돌파 + 거래량", [e for e in E["S3_rt"] if e["vol_ok"]])
    line("전략3 실시간 돌파 (전부)", E["S3_rt"])
    for g in ("강한 상승", "보통", "횡보"):
        line(f"전략3 기본 · 종목 60분 흐름 {g}", [e for e in s3 if e["vol_ok"] and e["regime"] == g])
    for sn in ("S1", "S2", "S3"):
        for g in ("QQQ 상승 (+0.5% 이상)", "QQQ 보합", "QQQ 하락 (-0.5% 이하)"):
            line(f"{NAMES[sn]} · {g}", [e for e in base[sn] if mkt_b(e) == g])

    # 9. 종목 특성
    P("\n## 9. 종목 특성별 (분석용 구분, 선정 때 가격 제한 없음)")
    bucket_table(cfg, Xs, base, price_b, ["$1 미만", "$1~3", "$3~10", "$10~20", "$20~50", "$50 이상"], "가격대별", P)
    bucket_table(cfg, Xs, base, dollar_b, ["$1M 미만", "$1~5M", "$5~20M", "$20~100M", "$100M 이상"], "신호 때까지 거래대금별", P)
    bucket_table(cfg, Xs, base, rvol_b, ["3~5", "5~10", "10~20", "20 이상"], "RVOL (같은 시각대 기준)별", P)
    bucket_table(cfg, Xs, base, rvold_b, ["1 미만", "1~2", "2~5", "5 이상"], "RVOL (누적 / 10일 평균 일 거래량)별", P)
    P("\n뉴스 유무별: 과거 시점 뉴스 데이터 없음 -> 측정 불가")

    # 10. 자금 관리
    P("\n## 10. 자금 관리 변형 (3전략 통합 계좌)\n")
    P("| 변형 | A 누적 / MDD / 거래 | B 누적 / MDD / 거래 | C 누적 / MDD / 거래 |")
    P("|---|---|---|---|")
    variants = [("기본 (4종목, 재진입 금지, -3%)", {}), ("동시 1종목", dict(max_positions=1, position_pct=100)),
                ("동시 2종목", dict(max_positions=2, position_pct=50)), ("동시 3종목", dict(max_positions=3, position_pct=33.3)),
                ("재진입 허용 (대기 없음)", dict(reentry=True)), ("재진입 허용 (청산 후 10분 대기)", dict(reentry=True, reentry_wait_min=10)),
                ("연속 3패 시 그날 중단", dict(consec_loss_stop=3)), ("QQQ 시가 대비 -1% 이하면 신규 금지", dict(market_filter_pct=1.0)),
                ("일일 손실 한도 없음", dict(daily_loss_pct=1e9))]
    for name, kw in variants:
        cells = []
        for ex in "ABC":
            a = SC.sim_account(R[("ALL", ex)], Xs, study, cfg, qqq=qqq, **kw)
            cells.append(f"{f2(a['ret'], 1)}% / -{a['mdd']:.1f}% / {len(a['trades'])}")
        P(f"| {name} | " + " | ".join(cells) + " |")
    P("\n**전략 따로 운영 vs 통합 (B, 계좌 기본 설정)**: " + ", ".join(
        f"{NAMES[sn]} {f2(SC.sim_account(R[(sn, 'B')], Xs, study, cfg)['ret'], 1)}%" for sn in ("S1", "S2", "S3", "ALL")))

    # 11. 실제 계좌
    P(f"\n## 11. 실제 계좌 ${cfg['real_account_usd']:.0f} · 정수 주 (토스 API 소수점 불가) · 종목당 25%\n")
    P("| 전략 | 손익절 | 체결된 거래 | 가격이 높아 못 산 신호 | 최종 금액 | 수익률 | MDD |")
    P("|---|---|---:|---:|---:|---:|---:|")
    for sn in ("S1", "S2", "S3", "ALL"):
        for ex in "ABC":
            a = SC.sim_account(R[(sn, ex)], Xs, study, cfg, integer=True, start_equity=cfg["real_account_usd"])
            P(f"| {NAMES[sn]} | {ex} | {len(a['trades'])} | {a['skipped']['zero_qty']} | ${a['final']:.2f} | {f2(a['ret'], 1)}% | -{a['mdd']:.1f}% |")
    P(f"\n_실행 {time.time() - t0:.0f}초_")
    os.makedirs(FC.LOGS, exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n보고서: {REPORT}")


if __name__ == "__main__":
    main()
