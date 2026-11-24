# -*- coding: utf-8 -*-
"""
fl_core.py - Fashionably Late V4.0 (미국 급등주: 1차 급등 → 눌림목 → 9 EMA 의 VWAP 상향 돌파 매수) 공통 로직
실시간 모의(fl.py --paper), 실시간 신호(fl.py --live), 백테스트(fl_backtest.py)가 모두 이 코드를 씀
  -> 백테스트와 실전 판단이 어긋나지 않게 지표·신호·손익절·계좌 관리를 한 곳에 둠

[데이터 원칙] 미래 데이터 금지
  - 모든 지표는 그 봉까지의 값만 사용: VWAP(09:30 부터 누적), 9 EMA(재귀식), 거래량 평균(직전 20봉), 고점·저점(지금까지)
  - 1차 급등 고점 H = 지금까지의 장중 최고가. 더 높은 고가가 나오면 고점이 바뀌고 눌림목 추적을 처음부터 다시 함
  - 급등 시작점 S = 고점 H 이전의 최저가, 눌림목 저점 L = 고점 이후 지금까지의 최저가
    -> 어느 시점에서 판단해도 그 시점까지의 봉만으로 같은 값이 나옴 (test_fl.py 에서 검증)
"""
import copy
import json
import math
import os
from datetime import timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")
STATE_DIR = os.path.join(HERE, "state")
CONFIG_FILE = os.path.join(HERE, "fl_config.json")
ET_S, KST_S = "America/New_York", "Asia/Seoul"
MIN = pd.Timedelta(minutes=1)

DEFAULTS = {
    # ── 실행 모드 ──
    "trading_mode": "paper",            # paper = 모의투자 / live = 실전 (증권사 연동 전까지 실행 불가)
    "live_trading_enabled": False,      # 실전 주문 스위치 (사용자가 직접 켜야 함)
    "capital": 10000.0,
    "entry_mode": "B",                  # A = 실시간 교차 즉시 매수 / B = 1분봉 마감 확인 후 매수
    "log_other_mode": True,             # 모의 중 다른 모드 신호도 기록만 (A/B 비교용)
    # ── 1단계 검색기 ──
    "min_price": 0.5,
    "max_price": None,
    "min_change_pct": 15.0,             # 당일 상승률 (전일 종가 대비)
    "min_rvol": 3.0,                    # 상대거래량: 오늘 누적 / 과거 같은 시각 누적 평균
    "min_cum_volume": 500000,
    "rvol_days": 10,                    # 상대거래량 비교 과거 거래일 수
    "rvol_min_days": 5,                 # 이보다 과거 데이터가 적으면 아래 방식
    "rvol_fallback": "daily_linear",    # daily_linear = 10일 평균 일거래량 x 경과시간 비율 / exclude = 제외
    "max_spread_pct": 0.5,
    "spread_unknown": "allow",          # 호가가 없을 때: allow = 기록하고 진행 / skip = 매수 안 함
    "liquidity_min_bars_20": 18,        # 최근 20분 중 거래가 있었던 1분봉 수 하한
    "halt_gap_min": 5,                  # 최근 3봉 사이 공백이 이 분 이상이면 거래정지·유동성 부족 의심
    # ── 2·3단계 구조 ──
    "structure": "pullback",            # pullback = 1차 급등 후 눌림 저점 기준(변형) / lod = 당일 저점 기준(원본)
    "surge_min_pct": 10.0,              # 1차 급등: 시작점 S -> 고점 H 상승률 하한 (요청서에 수치 없음 -> 설정값)
    "retrace_min_pct": 20.0,
    "retrace_max_pct": 60.0,
    "no_new_low_bars": 3,
    "ema_len": 9,
    "ema_rise_bars": 3,
    "ema_flat_pct": 0.10,               # 9 EMA 3봉 변화율이 이 % 미만이면 '평평'
    "vol_avg_bars": 20,
    "vol_mult": 1.5,
    "max_vwap_ext_pct": 2.0,
    "conditions": {                     # 매수 필수 조건 (분석용으로 하나씩 끌 수 있음)
        "registered": True, "price": True, "liquidity": True,
        "surge": True, "retrace": True, "pullback_below_vwap": True, "pullback_volume_drop": True,
        "no_new_low": True, "ema_up3": True, "ema_slope": True, "ema_below_prev": True, "ema_cross": True,
        "price_above": True, "volume": True, "no_chase": True, "stop_width": True,
    },
    "filters": {                        # 선택 필터 (기본 꺼짐, 백테스트에서 효과 비교)
        "higher_low": False, "steady_rise": False,
        "daily_uptrend": False, "daily_sma_touch": False, "daily_volume_up": False, "daily_resistance": False,
    },
    "steady_max_bar_share": 0.6,        # 저점->신호까지 상승폭 중 한 봉이 차지하는 비율 상한 (장대양봉 한 개 방지)
    "sma_touch_pct": 3.0,
    "resistance_days": 20,
    # ── 손익절 ──
    "exit_style": "dynamic",            # dynamic = 눌림 저점 기준 3:1 / fixed = 고정 % (비교용)
    "stop_div": 3.0,                    # SL = E - D / 3
    "target_mult": 1.0,                 # TP = E + D x 1
    "fixed_tp_pct": 7.0,
    "fixed_sl_pct": 7.0,
    "min_stop_pct": 1.0,
    "max_stop_pct": 5.0,
    # ── 조기 청산 ──
    "early_exits": {"vwap_2close": True, "ema_turn_below": True, "ema_cross_down": True,
                    "flat_sideways": True, "no_progress": True},
    "flat_lookback_bars": 5,
    "flat_range_R": 0.5,                # 최근 5봉 고저폭 < 0.5R 이면 횡보
    "flat_min_hold_bars": 5,
    "no_progress_min": 12,              # 진입 후 12분 안에
    "no_progress_mfe_R": 1.0,           # 최대 상승폭이 1R(손절폭) 에 못 미치면 청산
    # ── 계좌 ──
    "position_pct": 30.0,
    "max_positions": 3,
    "max_total_pct": 90.0,
    "min_cash_pct": 10.0,
    "account_target_pct": 4.0,
    "account_stop_pct": 4.0,
    "account_lock": True,
    "risk_limit_pct": 4.0,              # 예상 계좌 손실(모든 손절이 났을 때) 한도
    "min_order_usd": 50.0,
    "no_reentry": True,
    # ── 시간 ──
    "buy_start_kst": "23:30",           # 신규 매수 한국 시각 (사용자 지정). 정규장 개장 전이면 개장부터
    "buy_end_kst": "02:30",
    "buy_start_et": "10:00",            # buy_*_kst 가 null 일 때 쓰는 뉴욕 시각 (요청서 원안)
    "buy_end_et": "13:30",
    "flatten_before_close_min": 10,
    "no_buy_before_flat_min": 20,       # 조기 폐장일: 강제 청산 20분 전부터 신규 매수 안 함
    "force_exit_kst": None,             # 예: "02:30" -> 한국 02:30 에 전량 청산 (기본 끔: 장 마감 10분 전)
    "holidays_extra": [],
    "early_closes_extra": {},
    # ── 비용 ──
    "fee_pct": 0.25,                    # 매수·매도 각각
    "slippage_tiers": [[1.0, 0.5], [5.0, 0.2], [10.0, 0.1], [None, 0.05]],   # [가격 상한, 슬리피지 %]
    "participation_pct": 10.0,          # 모의 체결: 1분 거래량의 10% 까지만 체결 (나머지는 다음 봉, 부분 체결)
    "buy_timeout_min": 2,
    # ── 우선순위 점수 가중치 ──
    "score_weights": {"rvol": 1.0, "dollar_volume": 1.0, "volume_recovery": 1.0, "ema_slope": 1.0,
                      "higher_low": 1.0, "vwap_stability": 1.0, "spread": 1.0, "slippage": 1.0, "stop_risk": 1.0},
    # ── 실시간 ──
    "poll_sec": 10,
    "scan_sec": 180,
    "bar_grace_sec": 5,
    "fresh_signal_sec": 90,
    "stale_data_min": 3,
    "data_fail_limit": 3,
    "halt_feed": True,
    "armed_vwap_gap_pct": 3.0,          # A 모드: 종가가 VWAP 아래 3% 안이면 10초마다 감시
    "monitor_html": True,
    "signal_queue": True,               # 신호를 state/fl_signal_queue.jsonl 에 기록 (토스 자동매수 봇 bot.py 가 읽음)
}

COND_ORDER = ["registered", "price", "liquidity", "surge", "retrace", "pullback_below_vwap", "pullback_volume_drop",
              "no_new_low", "ema_up3", "ema_slope", "ema_below_prev", "ema_cross", "price_above", "volume",
              "no_chase", "stop_width"]
COND_KO = {
    "registered": "검색기 (상승률·RVOL·거래량·주가)", "price": "주가 범위", "liquidity": "유동성 (거래 공백 없음)",
    "surge": "① 1차 급등", "retrace": "② 눌림 20~60%", "pullback_below_vwap": "눌림 중 VWAP 아래 마감",
    "pullback_volume_drop": "눌림 거래량 < 급등 거래량", "no_new_low": "③ 최근 3봉 새 저점 없음",
    "ema_up3": "④ 9EMA > 3봉 전", "ema_slope": "⑤ 9EMA 상승 기울기", "ema_below_prev": "⑥ 직전 9EMA ≤ VWAP",
    "ema_cross": "⑦ 9EMA > VWAP", "price_above": "⑧ 현재가 > 9EMA·VWAP", "volume": "⑨ 거래량 회복 1.5배",
    "no_chase": "⑩ VWAP +2% 이내", "stop_width": "손절폭 1~5%",
}
FILTER_ORDER = ["higher_low", "steady_rise", "daily_uptrend", "daily_sma_touch", "daily_volume_up", "daily_resistance"]
FILTER_KO = {"higher_low": "저점 상승 구조", "steady_rise": "꾸준한 상승 (장대양봉 1개 아님)",
             "daily_uptrend": "일봉 상승 추세 (전일 종가 > 10SMA, 5SMA > 10SMA)",
             "daily_sma_touch": "일봉 5·10 SMA 근처까지 눌림", "daily_volume_up": "전일 거래량 > 10일 평균",
             "daily_resistance": "익절가 전에 20일 고점 저항 없음"}
NOT_IN_LOD = {"surge", "retrace", "pullback_below_vwap", "pullback_volume_drop"}   # 원본(당일 저점)에는 해당 없음


def load_config(path=CONFIG_FILE):
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update({a: b for a, b in v.items() if not a.startswith("_")})
            else:
                cfg[k] = v
    return cfg


def with_cfg(cfg, **kw):
    """설정 복사본 (중첩 dict 는 'conditions.volume' 처럼 점으로)"""
    c = copy.deepcopy(cfg)
    for k, v in kw.items():
        k = k.replace("__", ".")
        if "." in k:
            a, b = k.split(".", 1)
            c[a][b] = v
        else:
            c[k] = v
    return c


# ═════════════════════ 시간 · 휴장일 ═════════════════════
NYSE_HOLIDAYS = {
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04",
    "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
    "2027-11-25", "2027-12-24",
}
NYSE_EARLY_CLOSE = {"2025-07-03": "13:00", "2025-11-28": "13:00", "2025-12-24": "13:00",
                    "2026-11-27": "13:00", "2026-12-24": "13:00", "2027-11-26": "13:00"}


def iso(t):
    return pd.Timestamp(t).isoformat()


def ts(x):
    return pd.Timestamp(x)


def at(d, hhmm):
    return pd.Timestamp(f"{d} {hhmm}").tz_localize(ET_S)


def now_et():
    return pd.Timestamp.now(tz=ET_S)


def clk(t):
    t = pd.Timestamp(t)
    return f"{t.tz_convert(ET_S):%H:%M}ET({t.tz_convert(KST_S):%H:%M}KST)"


def market_hours(d, cfg=None):
    """(정규장 시작, 정규장 끝) 또는 휴장이면 None. 조기 폐장(13:00) 반영"""
    cfg = cfg or DEFAULTS
    ds = str(pd.Timestamp(d).date())
    if pd.Timestamp(d).weekday() >= 5 or ds in NYSE_HOLIDAYS or ds in set(cfg.get("holidays_extra") or []):
        return None
    close = {**NYSE_EARLY_CLOSE, **(cfg.get("early_closes_extra") or {})}.get(ds, "16:00")
    return at(ds, "09:30"), at(ds, close)


def kst_on(o, hhmm):
    """한국 시각 hhmm 중 이 거래일(뉴욕 날짜)에 해당하는 시각 (한국 저녁 = 같은 날, 새벽 = 다음 날). 뉴욕 시각으로 반환"""
    dd = o.date()
    for k in (0, 1):
        x = pd.Timestamp(f"{dd + timedelta(days=k)} {hhmm}").tz_localize(KST_S).tz_convert(ET_S)
        if x.date() == dd:
            return x
    return None


def day_windows(d, cfg):
    """그날의 시간표: 개장·마감·강제청산·신규매수 시작/끝 (뉴욕 시각, 서머타임은 tz 가 자동 반영)"""
    mh = market_hours(d, cfg)
    if not mh:
        return None
    o, c = mh
    flat = c - pd.Timedelta(minutes=cfg["flatten_before_close_min"])
    if cfg.get("force_exit_kst"):
        x = kst_on(o, cfg["force_exit_kst"])
        if x is not None and o < x <= c:
            flat = min(flat, x)
    bs = kst_on(o, cfg["buy_start_kst"]) if cfg.get("buy_start_kst") else None
    be = kst_on(o, cfg["buy_end_kst"]) if cfg.get("buy_end_kst") else None
    bs = max(o, bs if bs is not None else at(o.date(), cfg["buy_start_et"]))        # 개장 전이면 개장부터
    be = min(be if be is not None else at(o.date(), cfg["buy_end_et"]),
             flat - pd.Timedelta(minutes=cfg["no_buy_before_flat_min"]))
    return dict(open=o, close=c, flat=flat, buy_start=bs, buy_end=be, early=c.hour < 16)


# ═════════════════════ 가격 · 비용 ═════════════════════
def tick_size(p):
    return 0.0001 if p < 1 else 0.01


def round_tick(x, up):
    """호가 단위 맞춤: $1 미만 $0.0001, $1 이상 $0.01. up=True 올림 (손절가), False 내림 (익절가)"""
    t = tick_size(x)
    k = x / t
    k = math.ceil(k - 1e-7) if up else math.floor(k + 1e-7)
    return round(k * t, 4)


def slip_pct(price, cfg):
    for up, pct in cfg["slippage_tiers"]:
        if up is None or price < up:
            return float(pct)
    return float(cfg["slippage_tiers"][-1][1])


def money(x):
    x = float(x)
    return f"${x:,.2f}" if x >= 1 else f"${x:.4f}"


def levels(E, L, cfg):
    """E = 실제 평균 체결가, L = 매수 신호 때 확정한 눌림 저점
    D = E - L,  SL = E - D/3,  TP = E + D  (손익비 3:1). 호가 단위로 맞춤. 계산 불가면 None"""
    if E is None or not (E > 0) or L is None or not (L == L):
        return None
    D = E - L
    if cfg.get("exit_style") == "fixed":
        SL = round_tick(E * (1 - cfg["fixed_sl_pct"] / 100), True)
        TP = round_tick(E * (1 + cfg["fixed_tp_pct"] / 100), False)
    else:
        if D <= 0:
            return None
        SL = round_tick(E - D / cfg["stop_div"], True)
        TP = round_tick(E + D * cfg["target_mult"], False)
    if not (SL < E < TP):
        return None
    return dict(E=E, L=L, D=D, R=E - SL, SL=SL, TP=TP, sl_pct=(E - SL) / E * 100, tp_pct=(TP - E) / E * 100,
                rr=(TP - E) / (E - SL))


# ═════════════════════ 데이터 → 지표 ═════════════════════
def regular_1m(raw, W):
    """원본 1분봉 -> 그날 정규장 봉만 (분 단위 시각, 중복 제거, 거래량 빈칸 0)"""
    cols = ["Open", "High", "Low", "Close", "Volume"]
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=cols)
    df = raw.dropna(subset=["Close"])
    idx = df.index if df.index.tz is not None else df.index.tz_localize("UTC")
    df = df.copy()
    df.index = idx.tz_convert(ET_S).floor("min")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df.index >= W["open"]) & (df.index < W["close"])][cols].astype(float)
    df["Volume"] = df["Volume"].fillna(0)
    return df


def rvol_profile(hist5, d, cfg):
    """과거 거래일 5분봉 -> 개장 후 m분 시점의 평균 누적 거래량 배열 (길이 391). 데이터 부족하면 None
    (오늘 이전 날짜만 사용, 조기 폐장일 제외)"""
    if hist5 is None or len(hist5) == 0:
        return None
    h = hist5.dropna(subset=["Close"])
    idx = h.index if h.index.tz is not None else h.index.tz_localize("UTC")
    idx = idx.tz_convert(ET_S)
    m = np.asarray(idx.hour * 60 + idx.minute)
    dd = np.array(idx.date)
    d = pd.Timestamp(d).date()
    keep = (dd < d) & (m >= 570) & (m < 960)
    vol = h["Volume"].fillna(0).to_numpy()[keep]
    m, dd = m[keep], dd[keep]
    days = [x for x in sorted(set(dd)) if str(x) not in NYSE_EARLY_CLOSE and market_hours(x, cfg)]
    rows = []
    for x in days[-int(cfg["rvol_days"]):]:
        sel = dd == x
        v = np.zeros(78)
        np.add.at(v, np.clip((m[sel] - 570) // 5, 0, 77), vol[sel])
        if v.sum() <= 0:
            continue
        cum = np.concatenate([[0.0], np.cumsum(v)])
        mins = np.arange(391)
        k5 = mins // 5
        rows.append(cum[np.minimum(k5, 78)] + (mins - 5 * k5) / 5 * v[np.minimum(k5, 77)])
    if len(rows) < int(cfg["rvol_min_days"]):
        return None
    return np.mean(rows, axis=0)


def daily_context(daily, d, cfg):
    """일봉(오늘 이전만) -> 전일 종가, 5·10 SMA, 추세, 거래량, 20일 고점, 10일 평균 거래량"""
    if daily is None or len(daily) == 0:
        return None
    df = daily.dropna(subset=["Close"])
    d = pd.Timestamp(d).date()
    prior = df[[pd.Timestamp(x).date() < d for x in df.index]]
    if prior.empty:
        return None
    c, v, h = prior["Close"].astype(float), prior["Volume"].astype(float), prior["High"].astype(float)
    sma5 = float(c.tail(5).mean()) if len(c) >= 5 else float("nan")
    sma10 = float(c.tail(10).mean()) if len(c) >= 10 else float("nan")
    pc = float(c.iloc[-1])
    return dict(prev_close=pc, prev_date=str(pd.Timestamp(prior.index[-1]).date()), sma5=sma5, sma10=sma10,
                uptrend=bool(pc > sma10 and sma5 > sma10) if sma10 == sma10 else False,
                vol_up=bool(len(v) >= 11 and v.iloc[-1] > v.iloc[-11:-1].mean()),
                resistance=float(h.tail(int(cfg["resistance_days"])).max()),
                adv=float(v.tail(10).mean()) if len(v) else float("nan"))


def build_frame(bars, W, prev_close, prof, adv, cfg, partial_sec=None):
    """정규장 1분봉 -> 지표 표. partial_sec 가 있으면 마지막 줄은 진행 중인 봉(그 봉 시작 후 경과 초)
    vwap = 09:30 부터 (고+저+종)/3 x 거래량 누적 / 누적 거래량, ema = 9 EMA (재귀식), va = 직전 20봉 평균 거래량"""
    F = bars.copy()
    if F.empty:
        return F
    o, h, l, c, v = (F[k].to_numpy(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    cv = np.cumsum(v)
    pv = np.cumsum((h + l + c) / 3 * v)
    with np.errstate(invalid="ignore", divide="ignore"):
        vw = np.where(cv > 0, pv / np.where(cv > 0, cv, 1), np.nan)
    vw = pd.Series(vw).ffill().fillna(pd.Series(c)).to_numpy()
    F["cumv"], F["vwap"] = cv, vw
    F["ema"] = pd.Series(c).ewm(span=cfg["ema_len"], adjust=False).mean().to_numpy()
    nb = int(cfg["vol_avg_bars"])
    F["va"] = pd.Series(v).shift(1).rolling(nb, min_periods=max(5, nb // 2)).mean().to_numpy()
    mins = np.asarray(((F.index - W["open"]).total_seconds() // 60).astype(int))
    end_m = (mins + 1).astype(float)
    vproj = v.copy()
    if partial_sec is not None:
        el = max(float(partial_sec), 15.0)
        end_m[-1] = mins[-1] + min(el, 60) / 60
        vproj[-1] = v[-1] * 60 / min(el, 60)            # 진행 중인 봉: 경과 시간으로 1분 거래량 환산
    F["vproj"] = vproj
    F["chg"] = (c / prev_close - 1) * 100 if prev_close else np.nan
    if prof is not None:
        den = np.interp(end_m, np.arange(len(prof)), prof)
    elif cfg["rvol_fallback"] == "daily_linear" and adv and adv == adv:
        den = adv * end_m / 390
    else:
        den = np.full(len(F), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        F["rvol"] = np.where(den > 0, cv / np.where(den > 0, den, 1), np.nan)
    F["m"] = mins
    F["gap"] = np.diff(np.concatenate([[-1], mins]))
    F["nb20"] = np.arange(1, len(F) + 1) - np.searchsorted(mins, mins - 19, side="left")
    F.attrs["partial"] = partial_sec is not None
    return F


def structure(F):
    """한 번 훑으면서 각 봉 시점의 고점 H, 급등 시작점 S, 눌림 저점 L, 당일 저점 LOD 등 (그 봉까지의 정보만)"""
    hi, lo, c = F["High"].to_numpy(), F["Low"].to_numpy(), F["Close"].to_numpy()
    vw, v = F["vwap"].to_numpy(), F["Volume"].to_numpy()
    n = len(F)
    out = {k: np.full(n, np.nan) for k in ("H", "S", "L", "svol", "pvol", "lod")}
    for k in ("Hi", "Si", "Li", "lodi", "piv"):
        out[k] = np.full(n, -1, dtype=int)
    out["below"] = np.zeros(n, bool)
    csv = np.concatenate([[0.0], np.cumsum(v)])
    curH, curHi, S_, Si_ = -np.inf, -1, np.nan, -1
    curL, curLi, below, piv = np.inf, -1, False, -1
    rmin, rarg = np.inf, -1
    for i in range(n):
        if lo[i] < rmin:
            rmin, rarg = lo[i], i
        if hi[i] > curH:                                  # 새 고점 -> 눌림목 추적 다시
            curH, curHi, S_, Si_ = hi[i], i, rmin, rarg
            curL, curLi, below = np.inf, -1, False
        else:
            if lo[i] < curL:
                curL, curLi = lo[i], i
            if c[i] < vw[i]:
                below = True
        if i >= 2 and lo[i - 1] < lo[i - 2] and lo[i - 1] <= lo[i]:   # i-1 이 저점(피벗)으로 확인됨
            piv = i - 1
        out["H"][i], out["Hi"][i], out["S"][i], out["Si"][i] = curH, curHi, S_, Si_
        if curLi >= 0:
            out["L"][i], out["Li"][i] = curL, curLi
            out["pvol"][i] = (csv[curLi + 1] - csv[curHi + 1]) / (curLi - curHi)
        out["svol"][i] = (csv[curHi + 1] - csv[Si_]) / (curHi - Si_ + 1)
        out["below"][i] = below
        out["lod"][i], out["lodi"][i] = rmin, rarg
        out["piv"][i] = piv
    return out


def conditions(F, S, cfg, mode="pullback", dctx=None):
    """각 봉에서 조건별 참/거짓 배열 + 계산값. 마지막 줄이 진행 중인 봉이면(A 모드) 그 봉 기준으로 보정"""
    n = len(F)
    c, h, lo = F["Close"].to_numpy(), F["High"].to_numpy(), F["Low"].to_numpy()
    e, w = F["ema"].to_numpy(), F["vwap"].to_numpy()
    idx = np.arange(n)
    T = np.ones(n, bool)
    K, X = {}, {}
    if mode == "lod":
        L, Li = S["lod"], S["lodi"]
        for k in NOT_IN_LOD:
            K[k] = T.copy()
        X["retr"] = np.full(n, np.nan)
        X["rise"] = np.full(n, np.nan)
    else:
        L, Li = S["L"], S["Li"]
        with np.errstate(invalid="ignore", divide="ignore"):
            rise = (S["H"] - S["S"]) / S["S"] * 100
            retr = (S["H"] - L) / (S["H"] - S["S"]) * 100
        K["surge"] = (rise >= cfg["surge_min_pct"]) & (Li > S["Hi"])
        K["retrace"] = (retr >= cfg["retrace_min_pct"]) & (retr <= cfg["retrace_max_pct"])
        K["pullback_below_vwap"] = S["below"].copy()
        K["pullback_volume_drop"] = np.nan_to_num(S["pvol"], nan=np.inf) < S["svol"]
        X["retr"], X["rise"] = retr, rise
    lc = idx.copy()
    if F.attrs.get("partial"):
        lc[-1] = n - 2                                     # 진행 중인 봉: 직전 3개 완성봉 + 이 봉에서 새 저점 없음
    K["no_new_low"] = (Li >= 0) & (Li <= lc - int(cfg["no_new_low_bars"]))
    rb = int(cfg["ema_rise_bars"])
    e3 = np.concatenate([np.full(rb, np.nan), e[:-rb]]) if n > rb else np.full(n, np.nan)
    e1 = np.concatenate([[np.nan], e[:-1]])
    w1 = np.concatenate([[np.nan], w[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = (e - e3) / e3 * 100
    K["ema_up3"] = e > e3
    K["ema_slope"] = (e > e1) & (slope >= cfg["ema_flat_pct"])
    K["ema_below_prev"] = e1 <= w1
    K["ema_cross"] = e > w
    K["price_above"] = (c > e) & (c > w)
    va = F["va"].to_numpy()
    K["volume"] = F["vproj"].to_numpy() >= cfg["vol_mult"] * np.nan_to_num(va, nan=np.inf)
    ext = (c - w) / w * 100
    K["no_chase"] = ext <= cfg["max_vwap_ext_pct"]
    reg = ((F["chg"].to_numpy() >= cfg["min_change_pct"]) & (np.nan_to_num(F["rvol"].to_numpy()) >= cfg["min_rvol"])
           & (F["cumv"].to_numpy() >= cfg["min_cum_volume"]) & (c >= cfg["min_price"]))
    K["registered"] = np.maximum.accumulate(reg) if n else reg
    K["price"] = (c >= cfg["min_price"]) & ((c <= cfg["max_price"]) if cfg.get("max_price") else T)
    gap3 = pd.Series(F["gap"].to_numpy()).rolling(3, min_periods=1).max().to_numpy()
    K["liquidity"] = (F["nb20"].to_numpy() >= np.minimum(cfg["liquidity_min_bars_20"], F["m"].to_numpy() + 1)) & \
                     (gap3 < cfg["halt_gap_min"])
    D = c - L
    with np.errstate(invalid="ignore", divide="ignore"):
        slp = D / cfg["stop_div"] / c * 100
    K["stop_width"] = (D > 0) & (slp >= cfg["min_stop_pct"] - 1e-9) & (slp <= cfg["max_stop_pct"] + 1e-9)
    # 선택 필터
    piv = S["piv"]
    K["higher_low"] = (piv > Li) & (Li >= 0) & (lo[np.maximum(piv, 0)] > L)
    K["steady_rise"] = T.copy()                            # 신호 후보 봉에서만 따로 계산 (steady_ok)
    if dctx:
        sm = [x for x in (dctx["sma5"], dctx["sma10"]) if x == x]
        K["daily_uptrend"] = T & dctx["uptrend"]
        K["daily_sma_touch"] = (np.min([np.abs(L / x - 1) * 100 for x in sm], axis=0) <= cfg["sma_touch_pct"]) if sm \
            else ~T
        K["daily_volume_up"] = T & dctx["vol_up"]
        res = dctx["resistance"]
        K["daily_resistance"] = (res <= c) | (res >= c + D * cfg["target_mult"])
    else:
        for k in ("daily_uptrend", "daily_sma_touch", "daily_volume_up", "daily_resistance"):
            K[k] = ~T
    X.update(L=L, Li=Li, slope=slope, ext=ext, slp=slp, D=D)
    return K, X


def steady_stats(F, Li, i):
    """눌림 저점 -> 신호 봉까지: 양봉 비율, 상승폭 중 가장 큰 한 봉의 비율 (장대양봉 1개 의존도)"""
    c, o = F["Close"].to_numpy(), F["Open"].to_numpy()
    if Li < 0 or i <= Li:
        return 0.0, 1.0
    seg = slice(Li + 1, i + 1)
    up = float(np.mean(c[seg] > o[seg]))
    move = c[i] - F["Low"].to_numpy()[Li]
    steps = np.diff(c[Li:i + 1])
    share = float(steps.max() / move) if move > 0 and len(steps) else 1.0
    return up, max(0.0, min(1.0, share))


def required_keys(cfg, mode):
    keys = [k for k in COND_ORDER if cfg["conditions"].get(k, True) and not (mode == "lod" and k in NOT_IN_LOD)]
    keys += [k for k in FILTER_ORDER if cfg["filters"].get(k)]
    return keys


def score_parts(s, cfg):
    """우선순위 점수 구성요소 (0~1)"""
    clip = lambda x: float(min(1.0, max(0.0, x))) if x == x else 0.0
    sp = s.get("spread_pct")
    return dict(
        rvol=clip((s["rvol"] or 0) / 10),
        dollar_volume=clip((math.log10(max(s["dollar"], 1)) - 6) / 2),          # $100만 -> 0, $1억 -> 1
        volume_recovery=clip((s["volr"] - 1) / 4),
        ema_slope=clip(s["slope"] / 0.5),
        higher_low=1.0 if s["higher_low"] else 0.0,
        vwap_stability=clip(s["up_frac"] * (1 - s["max_share"]) * 2),
        spread=0.5 if sp is None else clip(1 - sp / cfg["max_spread_pct"]),
        slippage=clip(1 - slip_pct(s["px"], cfg) / 0.5),
        stop_risk=clip(1 - s["sl_pct"] / cfg["max_stop_pct"]),
    )


def score_of(s, cfg):
    w = cfg["score_weights"]
    p = score_parts(s, cfg)
    tot = sum(max(0.0, float(w.get(k, 0))) for k in p)
    return round(100 * sum(p[k] * max(0.0, float(w.get(k, 0))) for k in p) / tot, 1) if tot else 0.0


def find_signals(F, S, cfg, W, tk="", mode=None, dctx=None, only_last=False, entry_mode="B", spread_pct=None):
    """매수 신호 (조건을 모두 통과하고 신규 매수 시간 안인 봉). 반환 (신호 목록, 조건표 K)"""
    mode = mode or cfg["structure"]
    if F.empty:
        return [], {}
    K, X = conditions(F, S, cfg, mode, dctx)
    keys = required_keys(cfg, mode)
    ok = np.ones(len(F), bool)
    for k in keys:
        ok &= K[k]
    partial = F.attrs.get("partial")
    tend = F.index + (pd.Timedelta(seconds=0) if partial else MIN)
    if partial:
        tend = tend.copy()
    intime = np.asarray((tend >= W["buy_start"]) & (tend <= W["buy_end"]))
    cand = np.where(ok & intime)[0]
    if only_last:
        cand = cand[cand == len(F) - 1]
    out = []
    c = F["Close"].to_numpy()
    for i in cand:
        Li = int(X["Li"][i])
        up, share = steady_stats(F, Li, i)
        if cfg["filters"].get("steady_rise") and share > cfg["steady_max_bar_share"]:
            continue
        px = float(c[i])
        L = float(X["L"][i])
        s = dict(tk=tk, day=str(W["open"].date()), mode=entry_mode, struct=mode, i=int(i), t=iso(F.index[i]),
                 t_sig=iso(tend[i]) if not partial else None, px=px,
                 ema=float(F["ema"].iat[i]), vwap=float(F["vwap"].iat[i]), L=L, L_t=iso(F.index[Li]),
                 H=float(S["H"][i]) if mode != "lod" else None, S=float(S["S"][i]) if mode != "lod" else None,
                 retr=float(X["retr"][i]) if X["retr"][i] == X["retr"][i] else None,
                 rise=float(X["rise"][i]) if X["rise"][i] == X["rise"][i] else None,
                 rvol=float(F["rvol"].iat[i]) if F["rvol"].iat[i] == F["rvol"].iat[i] else None,
                 chg=float(F["chg"].iat[i]), cumv=float(F["cumv"].iat[i]),
                 dollar=float(F["cumv"].iat[i] * F["vwap"].iat[i]),
                 volr=float(F["vproj"].iat[i] / F["va"].iat[i]) if F["va"].iat[i] > 0 else 0.0,
                 slope=float(X["slope"][i]), ext=float(X["ext"][i]), higher_low=bool(K["higher_low"][i]),
                 up_frac=up, max_share=share, spread_pct=spread_pct,
                 D=float(X["D"][i]), sl_pct=float(X["slp"][i]), tp_pct=float(X["D"][i] * cfg["target_mult"] / px * 100))
        s["score"] = score_of(s, cfg)
        out.append(s)
    return out, K


def stage_of(K, X, i, mode="pullback"):
    """감시 화면용 단계 이름 (마지막 완성봉 기준)"""
    if i < 0:
        return "데이터 없음"
    if not K["registered"][i]:
        return "검색 조건 미달"
    if mode != "lod":
        if not K["surge"][i]:
            return "1차 급등 대기"
        if not K["retrace"][i]:
            r = X["retr"][i]
            return f"눌림 {r:.0f}% (범위 밖)" if r == r else "눌림 대기"
    if not K["no_new_low"][i]:
        return "2단계 눌림목 감시 (저점 형성 중)"
    if not (K["ema_cross"][i]):
        return "3단계 모멘텀 회복 감시"
    return "4단계 매수 신호 확인"


# ═════════════════════ 청산 판단 ═════════════════════
def bar_exit(p, o, h, l, first, amb="cons"):
    """봉 안에서 익절·손절 도달. first = 진입한 봉(시가 체결 직후). 반환 (가격, 사유, 모호, 종류) 또는 None
    종류: stop = 손절 시장가(슬리피지), limit = 익절 지정가(슬리피지 없음)"""
    SL, TP = p["SL"], p["TP"]
    if not first:
        if o <= SL:
            return o, "손절 (시가가 손절가 아래 → 시가 체결)", False, "stop"
        if o >= TP:
            return o, "익절 (시가가 익절가 위 → 시가 체결)", False, "limit"
    hs, ht = l <= SL, h >= TP
    if hs and ht:
        if amb == "opt":
            return TP, "익절 (같은 봉 익절·손절 모호 → 낙관적)", True, "limit"
        return SL, "손절 (같은 봉 익절·손절 모호 → 보수적)", True, "stop"
    if hs:
        return SL, "손절", False, "stop"
    if ht:
        return TP, "익절", False, "limit"
    return None


def early_exit(F, j, p, cfg, held_min):
    """완성봉 j 마감 기준 조기 청산 사유 (각 조건 설정에서 켜고 끔). p["ej"] = 이 표에서 진입 봉 위치"""
    ee = cfg["early_exits"]
    c, e, w = F["Close"].to_numpy(), F["ema"].to_numpy(), F["vwap"].to_numpy()
    j0, R = p["ej"], p["R"]
    if ee.get("vwap_2close") and j - 1 >= j0 and c[j] < w[j] and c[j - 1] < w[j - 1]:
        return "조기: VWAP 아래 2봉 연속 마감"
    if ee.get("ema_turn_below") and j >= 1 and e[j] < e[j - 1] and c[j] < e[j]:
        return "조기: 9EMA 하락 전환 + 9EMA 아래 마감"
    if ee.get("ema_cross_down") and e[j] < w[j]:
        return "조기: 9EMA 가 VWAP 하향 돌파"
    nb = int(cfg["flat_lookback_bars"])
    if ee.get("flat_sideways") and j - j0 + 1 >= cfg["flat_min_hold_bars"] and j >= max(3, nb - 1):
        s3 = abs(e[j] - e[j - 3]) / e[j - 3] * 100
        rng = F["High"].to_numpy()[j - nb + 1:j + 1].max() - F["Low"].to_numpy()[j - nb + 1:j + 1].min()
        if s3 < cfg["ema_flat_pct"] and rng < cfg["flat_range_R"] * R:
            return "조기: 9EMA 평평 + 횡보"
    if ee.get("no_progress") and held_min >= cfg["no_progress_min"] and p["mfe"] - p["avg"] < cfg["no_progress_mfe_R"] * R:
        return f"조기: {cfg['no_progress_min']:g}분 동안 +{cfg['no_progress_mfe_R']:g}R 미도달"
    return None


# ═════════════════════ 계좌 · 주문 (모의 체결) ═════════════════════
class Account:
    """모의 계좌. 상태(st)는 JSON 으로 저장 -> 재시작해도 그대로 이어감
    순자산 = 현금 + 보유 수량 x 현재가. 수수료는 체결 때 현금에서 바로 뺌 -> 실현·미실현 손익이 중복되지 않음
    입출금(flows)은 수익률 계산에서 뺌"""

    def __init__(self, cfg, st, log=None, alert=None, on_save=None, signal=False):
        self.c, self.st, self.sig = cfg, st, signal
        self.log = log or (lambda m: None)
        self.alert = alert
        self.on_save = on_save
        st.setdefault("flows", 0.0)
        st.setdefault("cash", st.get("start_eq", cfg["capital"]))
        for k, v in (("pos", {}), ("orders", []), ("trades", []), ("rows", []), ("entered", []), ("prices", {}),
                     ("said", []), ("missed", {}), ("max_conc", 0)):
            st.setdefault(k, copy.deepcopy(v))
        st.setdefault("locked", None)
        st.setdefault("seq", 0)
        st.setdefault("n_sig", 0)

    # ── 조회 ──
    def px(self, tk):
        return self.st["prices"].get(tk) or self.st["pos"][tk]["avg"]

    def equity(self):
        return self.st["cash"] + sum(p["qty"] * self.px(tk) for tk, p in self.st["pos"].items())

    def base(self):
        return self.st["start_eq"] + self.st["flows"]

    def day_ret(self):
        return (self.equity() - self.base()) / self.base() * 100

    def realized(self):
        r = sum(t["pnl"] for t in self.st["trades"])
        for p in self.st["pos"].values():
            if p["sold"]:
                r += p["sell_value"] - p["sell_fee"] - p["sold"] * p["avg"] - p["buy_fee"] * p["sold"] / p["bought"]
        return r

    def unrealized(self):
        return sum(p["qty"] * (self.px(tk) - p["avg"]) - p["buy_fee"] * p["qty"] / p["bought"]
                   for tk, p in self.st["pos"].items())

    def open_buys(self):
        return [o for o in self.st["orders"] if o["status"] == "open"]

    def exposure(self):
        return sum(p["qty"] * p["avg"] for p in self.st["pos"].values()) + \
            sum((o["qty"] - o["filled"]) * o["ref"] * (1 + slip_pct(o["ref"], self.c) / 100) for o in self.open_buys())

    def pending_cost(self):
        f = 1 + self.c["fee_pct"] / 100
        return sum((o["qty"] - o["filled"]) * o["ref"] * (1 + slip_pct(o["ref"], self.c) / 100) * f for o in self.open_buys())

    def _loss_to_stop(self, q, px, SL):
        cost = (slip_pct(SL, self.c) + self.c["fee_pct"]) / 100
        return q * max(px - SL * (1 - cost), 0.0)

    def open_risk(self):
        """지금 보유·미체결이 모두 손절되면 잃는 금액 (수수료·슬리피지 포함)"""
        r = sum(self._loss_to_stop(p["qty"], self.px(tk), p["SL"]) for tk, p in self.st["pos"].items())
        for o in self.open_buys():
            q = o["qty"] - o["filled"]
            buy = o["ref"] * (1 + slip_pct(o["ref"], self.c) / 100) * (1 + self.c["fee_pct"] / 100)
            r += q * (buy - o["SL"] * (1 - (slip_pct(o["SL"], self.c) + self.c["fee_pct"]) / 100))
        return r

    def expected_loss_pct(self):
        return self.open_risk() / self.base() * 100

    def cap_qty(self, vol):
        pp = self.c.get("participation_pct")
        return None if not pp else int(max(0.0, vol) * pp / 100)

    # ── 기록 ──
    def say_once(self, key, text):
        if key in self.st["said"]:
            return
        self.st["said"].append(key)
        if self.alert:
            self.alert(text)

    def miss(self, why):
        self.st["missed"][why] = self.st["missed"].get(why, 0) + 1

    def save(self):
        if self.on_save:
            self.on_save()

    # ── 신규 주문 ──
    def size(self, E, SL):
        """주문 수량: 종목당 30%, 총 90%, 예비현금 10%, 예상 계좌 손실 한도(-4%) 안에서. 반환 (수량, 메모)"""
        c, base = self.c, self.base()
        slip, fee = slip_pct(E, c) / 100, c["fee_pct"] / 100
        unit = E * (1 + slip) * (1 + fee)
        amt = base * c["position_pct"] / 100
        if self.sig:
            return int(amt // unit), ""
        amt = min(amt, base * c["max_total_pct"] / 100 - self.exposure(),
                  self.st["cash"] - self.pending_cost() - base * c["min_cash_pct"] / 100)
        q_amt = int(max(0.0, amt) // unit)
        floor_eq = base * (1 - c["risk_limit_pct"] / 100)
        room = self.equity() - self.open_risk() - floor_eq
        per = unit - SL * (1 - (slip_pct(SL, c) + c["fee_pct"]) / 100)
        q_risk = int(max(0.0, room) // per) if per > 0 else 0
        note = ""
        if q_risk < q_amt:
            note = f"계좌 위험 한도로 수량 축소 ({q_amt}→{q_risk}주, 남은 위험 여유 ${max(room, 0):,.0f})"
        return min(q_amt, q_risk), note

    def submit(self, s, now, ref, block=None):
        """매수 신호 -> 검사 -> 모의 매수 주문. 기록 행과 결과 문구를 남김. 반환 주문 또는 None"""
        st, c = self.st, self.c
        st["n_sig"] += 1
        tk = s["tk"]
        row = dict(s, t_order=iso(now), ref=ref)
        res = None
        lv = levels(ref, s["L"], c)
        sw = (ref - s["L"]) / c["stop_div"] / ref * 100 if ref > s["L"] else None   # 진입 필터는 항상 눌림 저점 기준 손절폭
        if st["locked"] and not self.sig:
            res = f"계좌 잠금 후 신호 ({st['locked']['why']})"
        elif block:
            res = block
        elif c["no_reentry"] and tk in st["entered"]:
            res = "동일 종목 당일 재진입 금지"
        elif tk in st["pos"] or any(o["tk"] == tk for o in self.open_buys()):
            res = "중복 주문 금지 (보유·미체결)"
        elif not self.sig and len(st["pos"]) + len(self.open_buys()) >= c["max_positions"]:
            res = f"보유 한도 초과 ({c['max_positions']}종목, 미체결 포함)"
        elif lv is None or sw is None:
            res = "손익절 계산 불가 (예상 체결가 ≤ 눌림 저점)"
        elif c["conditions"].get("stop_width", True) and \
                not (c["min_stop_pct"] - 1e-9 <= sw <= c["max_stop_pct"] + 1e-9):
            res = f"손절폭 {sw:.2f}% (허용 {c['min_stop_pct']:g}~{c['max_stop_pct']:g}%) → 진입 안 함"
        elif s.get("spread_pct") is not None and s["spread_pct"] > c["max_spread_pct"]:
            res = f"호가 스프레드 {s['spread_pct']:.2f}% > {c['max_spread_pct']:g}%"
        elif s.get("spread_pct") is None and s.get("spread_checked") and c["spread_unknown"] == "skip":
            res = "호가 확인 불가 → 매수 안 함 (spread_unknown=skip)"
        if res is None:
            q, note = self.size(ref, lv["SL"])
            if q < 1 or q * ref < c["min_order_usd"]:
                res = "자금·위험 한도로 주문 수량 부족" + (f" ({note})" if note else "")
            else:
                st["seq"] += 1
                o = dict(id=st["seq"], tk=tk, qty=q, filled=0, status="open", created=iso(now),
                         expire=iso(ts(now) + pd.Timedelta(minutes=c["buy_timeout_min"])), ref=ref, L=s["L"],
                         SL=lv["SL"], sig=row)
                st["orders"].append(o)
                res = (f"매수 주문 {q:,}주 (예상 {money(ref)} · 손절 {money(lv['SL'])} -{lv['sl_pct']:.2f}% · "
                       f"익절 {money(lv['TP'])} +{lv['tp_pct']:.2f}%)" + (f" | {note}" if note else ""))
                row["result"] = res
                st["rows"].append(row)
                self.log(f"{tk} 신호 → {res}")
                return o
        self.miss(res.split(" (")[0])
        row["result"] = res
        st["rows"].append(row)
        self.log(f"{tk} 신호 → {res}")
        return None

    def cancel_buys(self, now, why):
        for o in self.open_buys():
            o["status"] = "cancelled"
            o["why"] = why
            self.log(f"{o['tk']} 미체결 매수 취소 ({why}) - 체결 {o['filled']}/{o['qty']}주")

    def expire_orders(self, now):
        for o in self.open_buys():
            if ts(now) >= ts(o["expire"]):
                o["status"] = "expired" if o["filled"] == 0 else "partial"
                o["why"] = f"{self.c['buy_timeout_min']:g}분 안에 체결 안 됨"
                self.log(f"{o['tk']} 매수 주문 만료 - 체결 {o['filled']}/{o['qty']}주")

    def fill_buy(self, o, raw_px, t, cap=None, j=None):
        """모의 매수 체결 (시장가: 슬리피지 더함). cap = 이번 봉에서 체결 가능한 최대 수량(거래량 x 10%)"""
        st, c = self.st, self.c
        q = o["qty"] - o["filled"]
        if cap is not None:
            q = min(q, cap)
        px = raw_px * (1 + slip_pct(raw_px, c) / 100)
        fee_r = c["fee_pct"] / 100
        if not self.sig:
            q = min(q, int(max(0.0, st["cash"]) // (px * (1 + fee_r))))
        if q <= 0:
            return 0
        cost = q * px
        fee = cost * fee_r
        st["cash"] -= cost + fee
        tk = o["tk"]
        p = st["pos"].get(tk)
        if p is None:
            p = dict(tk=tk, qty=0, bought=0, avg=0.0, buy_fee=0.0, sold=0, sell_value=0.0, sell_fee=0.0,
                     L=o["L"], entry_t=iso(t), entry_min=iso(ts(t).floor("min")), ej=j, order=o["id"],
                     sig=o["sig"], mfe=px, mae=px, exit_flag=None, why=None, amb=False, n_fills=0)
            st["pos"][tk] = p
            if tk not in st["entered"]:
                st["entered"].append(tk)
        p["avg"] = (p["avg"] * p["bought"] + cost) / (p["bought"] + q)
        p["bought"] += q
        p["qty"] += q
        p["buy_fee"] += fee
        p["n_fills"] += 1
        lv = levels(p["avg"], p["L"], c)                    # 실제 평균 체결가로 손익절 다시 계산
        if lv is None:
            p.update(SL=p["avg"] * 0.999, TP=p["avg"] * 1.001, R=p["avg"] * 0.001, D=0.0, sl_pct=0.1, tp_pct=0.1)
            p["exit_flag"] = "체결가가 눌림 저점 이하 → 즉시 청산"
        else:
            p.update(SL=lv["SL"], TP=lv["TP"], R=lv["R"], D=lv["D"], sl_pct=lv["sl_pct"], tp_pct=lv["tp_pct"])
        o["filled"] += q
        if o["filled"] >= o["qty"]:
            o["status"] = "filled"
        st["max_conc"] = max(st["max_conc"], len(st["pos"]))
        part = "" if o["status"] == "filled" else f" (부분 체결 {o['filled']}/{o['qty']}주)"
        self.log(f"{tk} 매수 체결 {q:,}주 @ {money(px)}{part} → 손절 {money(p['SL'])} / 익절 {money(p['TP'])}")
        if self.alert and not self.sig:
            s = o["sig"]
            lv = f"손절: {money(p['SL'])} (-{p['sl_pct']:.2f}%) · 익절: {money(p['TP'])} (+{p['tp_pct']:.2f}%)"
            acct = f"계좌 {self.day_ret():+.2f}% · 모두 손절 시 -{self.expected_loss_pct():.2f}%"
            if p["n_fills"] == 1:                         # 첫 체결: 신호 내용 + 텔레그램 버튼 (즉시 청산 / 절반 청산)
                name = f" ({s['name']})" if s.get("name") else ""
                self.alert(f"📈 매수 신호 → 모의 매수\n종목: {tk}{name}\n신호: {clk(t)}\n"
                           f"체결: {q:,}주 @ {money(px)} (${q * px:,.0f}, 계좌의 {q * px / self.base() * 100:.1f}%){part}\n"
                           f"{lv}\n눌림 저점 {money(p['L'])} · 당일 {s.get('chg', 0):+.0f}% · RVOL {s.get('rvol') or 0:.1f} · "
                           f"점수 {s.get('score') or 0:.0f}\n{acct}",
                           buttons=[[("❌ 즉시 청산", f"exit|{st['day']}|{tk}"), ("➖ 절반 청산", f"half|{st['day']}|{tk}")]])
            else:
                self.alert(f"🟢 추가 체결 {tk} {q:,}주 @ {money(px)}{part}\n평균 {money(p['avg'])} → {lv}\n{acct}")
        return q

    def sell(self, tk, raw_px, t, why, kind="market", cap=None):
        """모의 매도. kind: market·stop = 슬리피지 뺌, limit = 지정가 그대로. 전량 끝나면 거래 기록"""
        st, c = self.st, self.c
        p = st["pos"].get(tk)
        if not p or p["qty"] <= 0:
            return 0
        q = p["qty"] if cap is None else min(p["qty"], cap)
        if q <= 0:
            return 0
        px = raw_px if kind == "limit" else raw_px * (1 - slip_pct(raw_px, c) / 100)
        val = q * px
        fee = val * c["fee_pct"] / 100
        st["cash"] += val - fee
        p["qty"] -= q
        p["sold"] += q
        p["sell_value"] += val
        p["sell_fee"] += fee
        p["why"] = p["why"] or why
        p.setdefault("exit_t", iso(t))
        st["prices"][tk] = raw_px
        if p["qty"] > 0:
            self.log(f"{tk} 부분 매도 {q:,}주 @ {money(px)} ({why}) - 남은 {p['qty']:,}주")
            if kind == "stop":
                p["exit_flag"] = p["exit_flag"] or why + " (나머지)"
            return q
        self._close(tk, t)
        return q

    def _close(self, tk, t):
        st = self.st
        p = st["pos"].pop(tk)
        cost = p["bought"] * p["avg"] + p["buy_fee"]
        net = p["sell_value"] - p["sell_fee"]
        ex = p["sell_value"] / p["sold"]
        R = p["R"] if p["R"] > 0 else float("nan")
        s = p["sig"]
        tr = dict(tk=tk, day=st["day"], mode=s.get("mode"), struct=s.get("struct"), sig_t=s.get("t"),
                  entry_t=p["entry_t"], exit_t=iso(t), qty=p["bought"], avg=round(p["avg"], 6), L=p["L"], D=p["D"],
                  SL=p["SL"], TP=p["TP"], sl_pct=round(p["sl_pct"], 3), tp_pct=round(p["tp_pct"], 3),
                  exit_px=round(ex, 6), why=p["why"], amb=p["amb"],
                  gross_pct=round((ex / p["avg"] - 1) * 100, 3), net_pct=round((net / cost - 1) * 100, 3),
                  pnl=round(net - cost, 2), fees=round(p["buy_fee"] + p["sell_fee"], 2),
                  hold_min=round((ts(t) - ts(p["entry_t"])).total_seconds() / 60, 1),
                  mfe_pct=round((p["mfe"] / p["avg"] - 1) * 100, 3), mae_pct=round((p["mae"] / p["avg"] - 1) * 100, 3),
                  mfe_R=round((p["mfe"] - p["avg"]) / R, 2) if R == R else None,
                  mae_R=round((p["mae"] - p["avg"]) / R, 2) if R == R else None,
                  score=s.get("score"), sig_px=s.get("px"), rvol=s.get("rvol"), n_fills=p["n_fills"])
        st["trades"].append(tr)
        self.log(f"{tk} 청산 {p['bought']:,}주 평균 {money(ex)} ({p['why']}) {tr['net_pct']:+.2f}% (${tr['pnl']:+,.2f})")
        if self.alert:
            mark = "✅" if tr["net_pct"] > 0 else "🔻"
            head = "신호 결과" if self.sig else "청산"
            self.alert(f"{mark} {head} {tk} {p['why']}\n진입 {money(p['avg'])} → {money(ex)} ({tr['net_pct']:+.2f}%, 비용 반영)"
                       f" · {tr['hold_min']:g}분 보유" + ("" if self.sig else f"\n계좌 {self.day_ret():+.2f}%"))

    def mark(self, tk, px, hi=None, lo=None):
        self.st["prices"][tk] = px
        p = self.st["pos"].get(tk)
        if p:
            p["mfe"] = max(p["mfe"], hi if hi is not None else px)
            p["mae"] = min(p["mae"], lo if lo is not None else px)

    def check_account(self, t):
        """계좌 ±4% 처음 도달 -> 잠금 (모든 개별 매매보다 우선). 반환 True 면 방금 잠김"""
        if self.sig or self.st["locked"] or not self.c.get("account_lock", True):
            return False
        r = self.day_ret()
        if r >= self.c["account_target_pct"]:
            self.lock(t, f"계좌 +{self.c['account_target_pct']:g}% 도달", r)
            return True
        if r <= -self.c["account_stop_pct"]:
            self.lock(t, f"계좌 -{self.c['account_stop_pct']:g}% 도달", r)
            return True
        return False

    def lock(self, t, why, r):
        st = self.st
        st["locked"] = dict(t=iso(t), why=why, ret=round(r, 3), done=None)
        self.cancel_buys(t, why)
        for p in st["pos"].values():
            p["exit_flag"] = why + " → 전량 청산"
        self.log(f"⛔ {why} ({r:+.2f}%) → 신규 매수 중단, 미체결 취소, 전량 청산, 오늘 매매 종료")
        if self.alert:
            self.say_once(f"LOCK|{st['day']}", f"⛔ {why} ({r:+.2f}%)\n신규 매수 중단 · 미체결 취소 · 보유 {len(st['pos'])}종목 전량 청산 · "
                                               f"오늘 매매 종료 (재시작해도 오늘은 매수 안 함)")

    def lock_done(self, t):
        lk = self.st["locked"]
        if lk and not lk["done"] and not self.st["pos"]:
            lk["done"] = iso(t)
            lk["final_ret"] = round(self.day_ret(), 3)
            self.log(f"계좌 잠금 청산 완료: 최종 {lk['final_ret']:+.2f}% (도달 {lk['ret']:+.2f}%)")

    def add_flow(self, amount, why="입출금"):
        """입출금·환전으로 인한 잔액 변화 (수익률에서 제외)"""
        self.st["cash"] += amount
        self.st["flows"] += amount
        self.log(f"{why} {amount:+,.2f} (수익률 계산 제외)")


class RealBroker:
    """실전 주문용 자리. 증권사 API(주문·잔고·체결 조회·OCO/브래킷)를 연결하기 전까지는 실행되지 않음"""

    def __init__(self, cfg):
        raise NotImplementedError(
            "실전 주문은 아직 연결된 증권사 API 가 없어 실행할 수 없습니다. 지금은 trading_mode='paper' 만 됩니다.\n"
            "필요한 것: 해외주식 주문 API (지정가·시장가·스톱), 체결·잔고 조회, 부분 체결 통지, 가능하면 OCO/브래킷 주문")
