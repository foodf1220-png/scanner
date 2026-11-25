# -*- coding: utf-8 -*-
"""
bf_core.py - Bull Flag V3.1 (Ross Cameron Bull Flag / First Pullback) 공통 로직 - 백테스트·모의 전용

[흐름] SCANNER → TRACKING(깃대) → PULLBACK(눌림) → READY(매수 기준가) → BREAKOUT → BUY → HOLDING → EXIT
  · 1분 완성봉만 사용 (미래 데이터 금지): 깃대 시작점·고점은 그 봉까지의 정보로만 정하고, 첫 조정봉이 나오면 확정
  · 깃대: 최근 5봉 안 저점 → 고점 +5% 이상 (고점이 계속 높아지면 늘어남)
  · 눌림: 조정봉 1~4개, 되돌림 = (깃대 고점 - 눌림 저점) / (깃대 고점 - 시작점) ≤ 50%, 눌림 저가가 VWAP 위
      취소: 되돌림 50% 초과 / VWAP 하향 이탈 / 조정봉 4개 초과 / 거래 공백(거래정지·시세 이상)
  · 매수 기준가 = 가장 최근 조정 음봉의 고가 (음봉이 없으면 최근 조정봉 고가)
  · 돌파: 다음 봉 안에서 기준가를 넘는 순간 (매수 스톱). 체결 가정 = max(그 봉 시가, 기준가) + 슬리피지
  · 첫 번째 눌림목만: 돌파가 나오면(신호든 탈락이든) 그 깃대 추적은 끝
[데이터 한계] 야후 1분봉: 틱·초봉·호가·체결 방향 없음
  -> J(3초 유지)·K(호가 스프레드)·체결 방향 필터는 백테스트 불가로 표시
  -> I(돌파 거래량)는 돌파 봉 전체 거래량으로 근사 (봉 안의 돌파 이후 거래량까지 포함 = 낙관적)
  -> 진입한 봉 안에서는 진입 전후 순서를 몰라 그 봉 종가로만 손절·익절 판단, 다음 봉부터 고가·저가
"""
import copy
import json
import os
from collections import Counter

import numpy as np
import pandas as pd

import fl_core as FC
from fl_core import bar_exit, iso, round_near, slip_pct

HERE = FC.HERE
CONFIG_FILE = os.path.join(HERE, "bf_config.json")

BF_DEFAULTS = {
    "trading_mode": "paper",               # 백테스트·모의 기본. 실계좌는 live_trading_enabled 를 직접 켜야 (연결 안 함)
    "live_trading_enabled": False,
    "capital": 10000.0,
    "min_price": 0.5, "max_price": 20.0, "min_change_pct": 10.0, "min_rvol": 3.0, "rvol_days": 14, "rvol_min_days": 5,
    "max_float": 20_000_000, "float_unknown": "exclude",
    "pole_bars": 5, "pole_min_pct": 5.0,
    "pb_max_bars": 4, "max_retrace_pct": 50.0, "pb_vol_ratio": 0.6,
    "breakout_vol_mult": 1.5, "max_chase_pct": 0.5, "halt_gap_min": 5,
    "room_min_pct": 1.0, "max_vwap_ext_pct": 10.0, "fake_bars": 3,
    "filters": {"pb_volume": True, "breakout_volume": True, "hold_3s": True, "m5_trend": True, "m5_above": True,
                "m1_slope": False, "tape": False, "spread": True, "room": False, "vwap_ext": False, "first_only": True},
    "tp_pct": 4.0, "sl_pct": 4.0,
    "entry_min_price": 2.0, "entry_max_price": 20.0,   # 실시간 신호 진입가 범위 (bf_live.py)
    "position_pct": 30.0, "max_positions": 3, "max_total_pct": 90.0, "min_cash_pct": 10.0,
    "account_target_pct": 4.0, "account_stop_pct": 4.0, "account_lock": True,
    "buy_start_et": "09:31", "buy_end_et": "15:30", "flatten_before_close_min": 10,
    "fee_pct": 0.25, "slippage_tiers": [[1.0, 0.5], [5.0, 0.2], [10.0, 0.1], [None, 0.05]], "participation_pct": 10.0,
    "train_frac": 0.6,
}

FILTERS = {                                    # (이름, 1분봉으로 백테스트 가능?)
    "pb_volume": ("E 눌림 평균 거래량 ≤ 깃대 평균 60%", True),
    "breakout_volume": ("I 돌파 거래량 ≥ 눌림 평균 1.5배 (1분봉 근사)", True),
    "hold_3s": ("J 돌파가 위 3초 유지", False),
    "spread": ("K 호가 스프레드 ≤ 0.5%", False),
    "tape": ("최근 5초 매수 주도 체결 ≥ 60%", False),
    "m5_trend": ("5분봉 9EMA 상승", True),
    "m5_above": ("최근 완성 5분봉 종가 > 5분 9EMA", True),
    "m1_slope": ("1분봉 9EMA 상승 기울기", True),
    "room": ("깃대 고점까지 남은 여력 ≥ 1%", True),
    "vwap_ext": ("VWAP 대비 이격 ≤ 10%", True),
    "first_only": ("그날 첫 번째 깃대의 눌림목만", True),
}
TESTABLE = [k for k, (_, ok) in FILTERS.items() if ok]
VERSIONS = {
    "V1": [],
    "V2": ["pb_volume"],
    "V3": ["pb_volume", "breakout_volume"],
    "V4": ["pb_volume", "breakout_volume", "hold_3s"],
    "V5": ["pb_volume", "breakout_volume", "hold_3s", "m5_trend", "m5_above"],
    "V6": ["pb_volume", "breakout_volume", "hold_3s", "m5_trend", "m5_above", "tape", "spread"],
}


def load_config(path=CONFIG_FILE):
    cfg = copy.deepcopy(FC.DEFAULTS)
    cfg.update(copy.deepcopy(BF_DEFAULTS))
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if k.startswith("_"):
                    continue
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update({a: b for a, b in v.items() if not a.startswith("_")})
                else:
                    cfg[k] = v
    return derive(cfg)


def derive(cfg, tp=None, sl=None, lock=None):
    """fl_core.Account 용 키: 고정 % 손익절, 조기청산·손절폭 필터·위험 한도 없음 (요청서 비중 규칙만)"""
    c = copy.deepcopy(cfg)
    c.update(exit_style="fixed", fixed_tp_pct=cfg["tp_pct"] if tp is None else tp,
             fixed_sl_pct=cfg["sl_pct"] if sl is None else sl, risk_limit_pct=1e9, buy_start_kst=None,
             buy_end_kst=None, no_reentry=not cfg.get("allow_reentry", False), ema_len=9, vol_avg_bars=20, min_order_usd=1.0, structure="pullback")
    if lock is not None:
        c["account_lock"] = lock
    c["early_exits"] = {k: False for k in FC.DEFAULTS["early_exits"]}
    c["conditions"] = dict(FC.DEFAULTS["conditions"], stop_width=False)
    return c


def m5_context(F, hist5, day):
    """각 1분봉 시점에 이미 완성된 마지막 5분봉의 (종가, 9EMA, 직전 9EMA). 이전 거래일 5분봉으로 EMA 를 데움"""
    n = len(F)
    nan = np.full(n, np.nan)
    if n == 0:
        return nan, nan, nan
    today = F["Close"].groupby(F.index.floor("5min")).last()
    prior = pd.Series(dtype=float)
    if hist5 is not None and len(hist5):
        hh = hist5.dropna(subset=["Close"])
        idx = hh.index.tz_convert("America/New_York")
        m = np.asarray(idx.hour * 60 + idx.minute)
        keep = (np.array(idx.date) < day) & (m >= 570) & (m < 960)
        prior = pd.Series(hh["Close"].to_numpy()[keep], index=idx[keep]).tail(120)
    s = pd.concat([prior, today]) if len(prior) else today
    s = s[~s.index.duplicated(keep="last")].sort_index()
    ema = s.ewm(span=9, adjust=False).mean().to_numpy()
    done = (s.index + pd.Timedelta(minutes=5)).values                # 5분봉 완성 시각
    k = np.searchsorted(done, F.index.values, side="right") - 1
    c5, e5, e5p = nan.copy(), nan.copy(), nan.copy()
    ok, okp = k >= 0, k >= 1
    c5[ok] = s.to_numpy()[k[ok]]
    e5[ok] = ema[k[ok]]
    e5p[okp] = ema[k[okp] - 1]
    return c5, e5, e5p


def detect(F, cfg, W, tk, float_sh, m5, return_state=False):
    """반환 (돌파 후보 목록, 사건 카운트[, (마지막 상태, 패턴)]). 돌파 후보 = 기본 조건 통과 돌파 + 필터별 판정값
    return_state: 실시간용 - 마지막 완성봉 뒤의 상태 (READY 면 매수 기준가 trigger 등)"""
    o, h, l, c, v = (F[k].to_numpy(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    e, w = F["ema"].to_numpy(), F["vwap"].to_numpy()
    chg, rv, gap = F["chg"].to_numpy(), F["rvol"].to_numpy(), F["gap"].to_numpy()
    c5, e5, e5p = m5
    n = len(F)
    ev, out = Counter(), []
    st = {"state": "SCANNER", "P": None}
    poles = 0
    pb_max, retr_max = int(cfg["pb_max_bars"]), float(cfg["max_retrace_pct"])
    tend = F.index + pd.Timedelta(minutes=1)

    def cancel(why):
        ev[f"눌림 취소: {why}"] += 1
        st["state"], st["P"] = "SCANNER", None

    def add_pb(i):
        P = st["P"]
        P["pb"].append(i)
        if len(P["pb"]) > pb_max:
            return cancel(f"조정봉 {pb_max}개 초과")
        if gap[i] >= cfg["halt_gap_min"]:
            return cancel("거래 공백 (거래정지·시세 이상 의심)")
        P["pb_low"] = min(P["pb_low"], l[i])
        P["retr"] = (P["high"] - P["pb_low"]) / (P["high"] - P["start"]) * 100
        if P["retr"] > retr_max:
            return cancel(f"되돌림 {retr_max:g}% 초과")
        if l[i] < w[i]:
            return cancel("VWAP 하향 이탈")
        reds = [k for k in P["pb"] if c[k] < o[k]]
        P["trigger"] = h[reds[-1]] if reds else h[P["pb"][-1]]
        P["pb_vol"] = float(np.mean(v[P["pb"]]))
        st["state"] = "READY"

    def breakout(j, P):
        T = P["trigger"]
        if j < 2:
            return None, "데이터 부족"
        if not (c[j - 1] <= T):
            return None, "G 직전 체결가 > 기준가"
        entry = max(o[j], T)                                       # 매수 스톱: 기준가 (시가가 위면 시가)
        chase = (entry / T - 1) * 100
        if chase > cfg["max_chase_pct"]:
            return None, f"L 시가가 기준가 +{cfg['max_chase_pct']:g}% 초과 (추격 금지)"
        if not (W["buy_start"] <= F.index[j] and tend[j] <= W["buy_end"] + pd.Timedelta(minutes=1)):
            return None, "신규 매수 시간 밖"
        if not (cfg["min_price"] <= entry <= cfg["max_price"]):
            return None, "가격 범위 밖"
        if not (entry > e[j - 1] > w[j - 1]):
            return None, "F 현재가 > 9EMA > VWAP 아님"
        flags = dict(
            pb_volume=P["pb_vol"] <= cfg["pb_vol_ratio"] * P["pole_vol"],
            breakout_volume=v[j] >= cfg["breakout_vol_mult"] * P["pb_vol"],
            m5_trend=bool(e5[j] > e5p[j]) if e5[j] == e5[j] and e5p[j] == e5p[j] else False,
            m5_above=bool(c5[j] > e5[j]) if c5[j] == c5[j] and e5[j] == e5[j] else False,
            m1_slope=bool(e[j - 1] > e[j - 2]),
            room=(P["high"] / entry - 1) * 100 >= cfg["room_min_pct"],
            vwap_ext=(entry / w[j - 1] - 1) * 100 <= cfg["max_vwap_ext_pct"],
            first_only=P["no"] == 0,
            hold_3s=None, spread=None, tape=None)                     # 데이터 없음 -> 판정 불가
        rec = dict(tk=tk, day=str(W["open"].date()), j=j, t=iso(F.index[j]), entry_raw=float(entry), trigger=float(T),
                   start=float(P["start"]), t_start=iso(F.index[P["si"]]), pole_high=float(P["high"]),
                   t_high=iso(F.index[P["hi"]]), pole_pct=(P["high"] / P["start"] - 1) * 100,
                   pole_bars=P["hi"] - P["si"] + 1, pb_bars=len(P["pb"]), pb_low=float(P["pb_low"]),
                   retr=float(P["retr"]), pole_vol=P["pole_vol"], pb_vol=P["pb_vol"],
                   pb_vol_ratio=P["pb_vol"] / P["pole_vol"] if P["pole_vol"] else np.nan,
                   brk_vol_ratio=v[j] / P["pb_vol"] if P["pb_vol"] else np.nan, chase=chase,
                   vwap=float(w[j - 1]), ema=float(e[j - 1]), ext=(entry / w[j - 1] - 1) * 100,
                   room=(P["high"] / entry - 1) * 100, chg=float(chg[P["hi"]]), rvol=float(rv[P["hi"]]),
                   float_sh=float_sh, pole_no=P["no"], si=P["si"], hi=P["hi"], pb=list(P["pb"]), flags=flags)
        return rec, None

    for i in range(n):
        P = st["P"]
        if st["state"] == "READY":
            if h[i] > P["trigger"]:
                ev["돌파 발생"] += 1
                rec, why = breakout(i, P)
                if rec:
                    out.append(rec)
                    ev["돌파 후보 (기본 조건 통과)"] += 1
                else:
                    ev[f"돌파 탈락: {why}"] += 1
                st["state"], st["P"] = "SCANNER", None
                continue
            add_pb(i)
            continue
        if st["state"] == "TRACKING":
            if h[i] > P["high"]:
                P["high"], P["hi"] = h[i], i
                continue
            P["pole_vol"] = float(np.mean(v[P["si"]:P["hi"] + 1]))
            ev["깃대 확정 (첫 조정봉)"] += 1
            st["state"] = "PULLBACK"
            add_pb(i)
            continue
        lo = max(0, i - int(cfg["pole_bars"]) + 1)                 # SCANNER: 최근 5봉 안 저점 -> 지금 고점 +5%
        s = lo + int(np.argmin(l[lo:i + 1]))
        if l[s] <= 0 or h[i] < h[s:i + 1].max() or (h[i] / l[s] - 1) * 100 < cfg["pole_min_pct"]:
            continue
        ev["급등 포착 (5봉 안 +5%)"] += 1
        if not (cfg["min_price"] <= c[i] <= cfg["max_price"]):
            ev["검색기 탈락: 가격 $0.5~20 밖"] += 1
            continue
        if not (chg[i] >= cfg["min_change_pct"]):
            ev["검색기 탈락: 당일 상승률 10% 미만"] += 1
            continue
        if not (rv[i] == rv[i] and rv[i] >= cfg["min_rvol"]):
            ev["검색기 탈락: RVOL 3배 미만"] += 1
            continue
        ev["TRACKING 시작 (검색기 통과)"] += 1
        st["P"] = dict(start=l[s], si=s, high=h[i], hi=i, no=poles, pb=[], pb_low=np.inf)
        st["state"] = "TRACKING"
        poles += 1
    if return_state:
        return out, ev, (st["state"], st["P"])
    if st["state"] != "SCANNER":
        ev["장 끝까지 돌파 없음"] += 1
    return out, ev


def passes(rec, keys):
    """필터 목록을 모두 통과? (백테스트 불가 필터는 건너뜀)"""
    return all(rec["flags"][k] for k in keys if FILTERS[k][1])


def sim_signal(F, rec, cfg, W, tp, sl, amb="cons", max_hold=None):
    """신호 하나만 (계좌 한도 없이). 진입 봉은 종가로만 손익절 판단, 다음 봉부터 고가·저가 (같은 봉 둘 다면 amb)
    max_hold(분): 진입 후 그 시간이 지난 첫 봉 시가에 시장가 청산 (None 이면 cfg max_hold_min, 0 이면 끔)"""
    max_hold = cfg.get("max_hold_min") if max_hold is None else max_hold
    o, h, l, c = (F[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    j, raw = rec["j"], rec["entry_raw"]
    fee = cfg["fee_pct"] / 100
    E = raw * (1 + slip_pct(raw, cfg) / 100)
    TP, SL = round_near(E * (1 + tp / 100)), round_near(E * (1 - sl / 100))
    mfe, mae = max(E, c[j]), min(E, c[j])
    k_exit, raw_exit, why, amb_hit = None, None, None, False
    if c[j] <= SL:
        k_exit, raw_exit, why, kind = j, SL, "손절", "stop"
    elif c[j] >= TP:
        k_exit, raw_exit, why, kind = j, TP, "익절", "limit"
    k = j + 1
    while k_exit is None and k < len(F):
        if F.index[k] >= W["flat"]:
            k_exit, raw_exit, why, kind = k, o[k], "장 마감 청산", "market"
            break
        if max_hold and (F.index[k] - F.index[j]).total_seconds() / 60 >= max_hold:
            k_exit, raw_exit, why, kind = k, o[k], "시간 청산", "market"
            break
        r = bar_exit(dict(SL=SL, TP=TP), o[k], h[k], l[k], False, amb)
        if r:
            raw_exit, why, amb_hit, kind = r
            why = "익절" if kind == "limit" else "손절"
            k_exit = k
            break
        mfe, mae = max(mfe, h[k]), min(mae, l[k])
        k += 1
    if k_exit is None:
        k_exit, raw_exit, why, kind = len(F) - 1, c[-1], "장 마감 청산", "market"
    px = raw_exit if kind == "limit" else raw_exit * (1 - slip_pct(raw_exit, cfg) / 100)
    if why == "손절":
        mae = min(mae, raw_exit)
    if why == "익절":
        mfe = max(mfe, raw_exit)
    fb = int(cfg.get("fake_bars", 3))
    fake = any(c[x] < rec["trigger"] for x in range(j, min(k_exit, j + fb - 1) + 1))
    return dict(tk=rec["tk"], day=rec["day"], t=rec["t"], entry_raw=raw, avg=E, TP=TP, SL=SL, exit_raw=float(raw_exit),
                exit_px=float(px), why=why, amb=amb_hit, exit_t=iso(F.index[k_exit]),
                hold_min=(F.index[k_exit] - F.index[j]).total_seconds() / 60,
                set_pct=tp if why == "익절" else -sl if why == "손절" else (raw_exit / E - 1) * 100,
                pre_cost_pct=(raw_exit / raw - 1) * 100, gross_pct=(px / E - 1) * 100,
                net_pct=((px * (1 - fee)) / (E * (1 + fee)) - 1) * 100,
                mfe_pct=(mfe / E - 1) * 100, mae_pct=(mae / E - 1) * 100, fake=fake)
