# -*- coding: utf-8 -*-
"""
pullback.py - 미국주식 C자리 눌림목 -> B자리(두 고점 사이 지지선 재반등) 모의매매 + 백테스트

[C자리] 마감된 5분봉만 사용, 같은 거래일 안에서
  급등   최근 24개 완성봉 안에서 저점 -> 이후 다른 봉의 고가가 저가 대비 30% 이상 (가장 최근의 유효 급등 고점)
  눌림   급등 고점 이후 다른 봉의 저가가 고점 대비 20~40% 하락 -> 후보 (반등 확인 전 40% 초과 하락하면 취소)
  C 알림 눌림 이후 첫 완성봉이 ① 양봉 ② 종가 > 직전 2봉 고가 ③ 224 EMA <= 종가 <= EMA x 1.05
         ④ 종가 $0.5 이상 (설정 min_price, 사용자 추가)
[B자리] C 알림 이후
  H1     C봉부터 최고가 추적, 처음으로 종가 < 직전 종가인 봉에서 그때까지의 최고가
  중간   H1 봉 이후 다른 봉의 저가가 H1 대비 3% 이상 하락
  H2     다시 고가가 H1 ±1% -> 후보, 종가 < 직전 종가인 봉에서 확정 (확정 전 H1 +1% 초과면 취소)
  S      H1 봉과 H2 봉 사이 봉들의 최저가 (확정 후 고정)
  재접근 H2 확정 다음 봉부터 저가가 S ±1% 에 처음 들어온 봉
  B 신호 재접근 봉 또는 다음 2봉 중 첫 봉이 ① 양봉 ② 종가 > 직전 종가 ③ 종가 > S
  취소   H2 확정 후 종가 < S / 재접근 후 2봉 안에 반등 없음 / C 후 24봉 안에 B 없음 / 거래일 끝
[매매] 진입 신호봉(설정 entry_signal: 지금은 C, 원안은 B) 마감 후 실제 주문 가능한 다음 시세로 모의 매수
  (신호봉 종가 체결 가정 안 함). entry_signal = C 면 C 봉에서 바로 사고 B자리는 추적하지 않음
  종목당 당일 시작 순자산 25%, 미체결 포함 동시 최대 4종목, 동일 종목 추가매수 금지
  종목 평균 매수가 +4% 익절 / -7% 손절 (원안 ±7%) / 진입 30분 뒤 청산 / 한국 02:30 전량 청산 후 종료
  계좌 당일 +3% / -3% 처음 도달 -> 거래 잠금, 미체결 취소, 전량 청산, 다음 거래일까지 신규 진입 없음
[실행]
  python pullback.py --paper      실시간 모의매매 (눌림목_모의투자.bat)
  python pullback.py --live       실시간 신호 (GitHub Actions, 뉴욕 09:25 Cloudflare Worker 가 실행)
  python pullback.py --premarket  장전 급등주 알림 (한국 22:00 Cloudflare Worker 가 실행)
  python pullback.py --scan       지금(장 마감 후면 최근 장) 한 번 검색: 검색 범위·패턴 상태만 출력 (주문·알림 없음)
  python pullback.py --backtest   최근 약 30일(야후 1분봉 한도) 백테스트 + 비교 전략(익절 = H1)
  python test_pullback.py         검증 사례 테스트
[설정] pullback_config.json   [기록] logs/pullback_*   [재시작 복구] state/pullback_날짜.json
[데이터] yfinance (야후). 프리장·애프터 거래량은 야후가 0 으로 줌 -> 프리장 거래량 옵션은 켜면 '데이터 없음'으로 제외됨
"""
import argparse
import collections
import copy
import json
import math
import os
import re
import sys
import time
import traceback
from datetime import timedelta

import numpy as np
import pandas as pd

from scanner import EXCHANGES, HERE, LOGS, _split, load_universe, money, notify, send_telegram

ET_S, KST_S = "America/New_York", "Asia/Seoul"
CONFIG_FILE = os.path.join(HERE, "pullback_config.json")
STATE_DIR = os.path.join(HERE, "state")
ACCOUNT_FILE = os.path.join(STATE_DIR, "pullback_account.json")
TITLE = "눌림목 모의"

DEFAULTS = {
    "capital": 10000.0,
    "sessions": {"premarket": False, "regular": True},
    "premarket_start_et": "04:00",
    "regular_start_et": "09:30",
    "regular_end_et": "16:00",
    "flatten_before_end_min": 5,
    "force_exit_kst": "02:30",
    "force_exit_et": None,
    "entry_signal": "C",
    "max_hold_min": 30,
    "premarket_alert_pct": 20.0,
    "bar_minutes": 5,
    "surge_lookback_bars": 24,
    "surge_min_pct": 30.0,
    "pullback_min_pct": 20.0,
    "pullback_max_pct": 40.0,
    "ema_len": 224,
    "ema_min_bars": 224,
    "ema_band_pct": 5.0,
    "c_break_bars": 2,
    "min_price": 0.5,
    "mid_pullback_pct": 3.0,
    "mid_break_rule": "cancel",
    "h2_band_pct": 1.0,
    "s_band_pct": 1.0,
    "b_rebound_bars": 2,
    "b_max_bars_after_c": 24,
    "premarket_volume_filter": {"enabled": False, "min": 100000, "max": 1000000},
    "position_pct": 25.0,
    "max_positions": 4,
    "tp_pct": 4.0,
    "sl_pct": 7.0,
    "tp_mode": "pct",
    "account_target_pct": 3.0,
    "account_stop_pct": 3.0,
    "fee_pct": 0.25,
    "slippage_pct": 0.1,
    "entry_delay_min": 1,
    "buy_timeout_min": 3,
    "bar_grace_sec": 30,
    "poll_sec": 15,
    "fresh_window_min": 10,
    "prefilter_day_range_pct": 30.0,
    "prefilter_premarket_change_pct": 10.0,
    "data_check_pct": 10.0,
}


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
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, ensure_ascii=False, indent=2)
    return cfg


# ═════════════════════ 시간 ═════════════════════
def iso(t):
    return pd.Timestamp(t).isoformat()


def ts(x):
    return pd.Timestamp(x)


def hm(s):
    h, m = map(int, s.split(":"))
    return h * 60 + m


def at(d, hhmm):
    return pd.Timestamp(f"{d} {hhmm}").tz_localize(ET_S)


def now_et():
    return pd.Timestamp.now(tz=ET_S)


def clk(t):
    t = pd.Timestamp(t)
    return f"{t.tz_convert(ET_S):%H:%M}ET({t.tz_convert(KST_S):%H:%M}KST)"


def tpsl_text(cfg):
    """익절·손절 표시: 같으면 ±7%, 다르면 +4/-7%"""
    tp, sl = cfg["tp_pct"], cfg["sl_pct"]
    return f"±{tp:g}%" if tp == sl else f"+{tp:g}/-{sl:g}%"


def bar_td(cfg):
    return pd.Timedelta(minutes=cfg["bar_minutes"])


def sess_mask(idx, cfg):
    """설정한 세션(프리장·정규장) 안의 봉만 True (뉴욕 시각 기준)"""
    m = np.asarray(idx.hour * 60 + idx.minute)
    ps, rs, re = hm(cfg["premarket_start_et"]), hm(cfg["regular_start_et"]), hm(cfg["regular_end_et"])
    mask = np.zeros(len(m), bool)
    if cfg["sessions"].get("premarket"):
        mask |= (m >= ps) & (m < rs)
    if cfg["sessions"].get("regular"):
        mask |= (m >= rs) & (m < re)
    return mask


def session_times(d, cfg):
    """(감시 시작, 세션 끝, 전량 청산·종료 시각). force_exit_kst 가 있으면 서머타임과 상관없이 그 한국 시각"""
    s = at(d, cfg["premarket_start_et"] if cfg["sessions"].get("premarket") else cfg["regular_start_et"])
    e = at(d, cfg["regular_end_et"] if cfg["sessions"].get("regular") else cfg["regular_start_et"])
    flat = e - pd.Timedelta(minutes=cfg["flatten_before_end_min"])
    if cfg.get("force_exit_kst"):
        for k in (0, 1):                     # 한국 02:30 = 뉴욕 같은 날 12:30 (서머타임 13:30)
            x = pd.Timestamp(f"{d + timedelta(days=k)} {cfg['force_exit_kst']}").tz_localize(KST_S).tz_convert(ET_S)
            if s < x <= e:
                flat = x
                break
    elif cfg.get("force_exit_et"):
        flat = at(d, cfg["force_exit_et"])
    return s, e, min(flat, e)


# ═════════════════════ 패턴 (C -> H1 -> H2 -> S -> B) ═════════════════════
def find_surge(bars, i, cfg, used):
    """최근 N개 완성봉 안에서 저점 a -> 이후 다른 봉 b 의 고가가 30% 이상인 구간 (b 는 a 이후 지금까지의 최고가 봉)
    가장 최근의 유효 급등 고점을 고름. 이미 쓴 고점은 제외. 반환 (a, b) 또는 None"""
    lo = max(0, i - cfg["surge_lookback_bars"] + 1)
    k = 1 + cfg["surge_min_pct"] / 100
    best = None
    for a in range(lo, i):
        la = bars[a][3]
        if la <= 0:
            continue
        b = a + int(np.argmax([bars[j][2] for j in range(a, i + 1)]))     # 같은 최고가면 먼저 나온 봉
        if b <= a or b >= i or bars[b][2] < la * k:
            continue
        if iso(bars[b][0]) in used:
            continue
        if best is None or b > best[1] or (b == best[1] and la < bars[best[0]][3]):
            best = (a, b)
    return best


class Tracker:
    """종목 하나의 당일 패턴 상태. 상태 dict 는 JSON 으로 저장해 재시작 때 복구"""

    def __init__(self, tk, d=None):
        d = d or {}
        self.tk = tk
        self.ph = d.get("ph", "IDLE")          # IDLE PULL H1 MID H2W H2 SUP REB
        self.p = d.get("p", {})
        self.used = d.get("used", [])          # 이미 쓴 급등 고점 (같은 패턴 재사용 금지)
        self.last_t = d.get("last_t")          # 마지막으로 처리한 5분봉 (같은 봉 재처리 금지)
        self.notes = d.get("notes", {})

    def to_dict(self):
        return dict(ph=self.ph, p=self.p, used=self.used, last_t=self.last_t, notes=self.notes)

    def step(self, cfg, bars, T, i, ema, n, pm_vol=None):
        """마감된 5분봉 i 하나 처리. bars[j] = (시각, 시, 고, 저, 종), T[j] = 시각 문자열. 이벤트 목록 반환"""
        _, o, h, l, c = bars[i]
        ev = []
        P = self.p

        def emit(kind, **kw):
            ev.append(dict(type=kind, tk=self.tk, pid=kw.pop("pid", P.get("pid")), t=T[i], **kw))

        def end(kind, why):
            emit(kind, why=why)
            self.ph, self.p = "IDLE", {}

        if not (n >= cfg["ema_min_bars"] and ema == ema):
            if not self.notes.get("ema"):
                self.notes["ema"] = 1
                emit("SKIP", why=f"224 EMA 데이터 부족 ({int(n)}/{cfg['ema_min_bars']}봉) - 봉이 모일 때까지 건너뜀")
            return ev
        self.notes["ema_ok"] = 1

        if self.ph == "IDLE":
            s = find_surge(bars, i, cfg, set(self.used))
            if not s:
                return ev
            a, b = s
            peak, low0 = bars[b][2], bars[a][3]
            dd = (1 - l / peak) * 100
            if dd < cfg["pullback_min_pct"]:
                return ev
            self.used.append(T[b])
            info = dict(pid=f"{self.tk}@{T[b]}", peak=peak, peak_t=T[b], low0=low0, low0_t=T[a])
            if dd > cfg["pullback_max_pct"]:
                emit("NOCAND", why=f"고점 대비 {dd:.1f}% 하락 (눌림 {cfg['pullback_max_pct']:g}% 초과)", **info)
                return ev
            vf = cfg["premarket_volume_filter"]
            if vf.get("enabled"):
                if not pm_vol:
                    emit("NOCAND", why="프리장 누적 거래량 데이터 없음 (야후가 0 으로 줌)", **info)
                    return ev
                if not vf["min"] <= pm_vol <= vf["max"]:
                    emit("NOCAND", why=f"프리장 누적 거래량 {pm_vol:,.0f}주 (기준 {vf['min']:,}~{vf['max']:,})", **info)
                    return ev
            self.ph, self.p = "PULL", dict(info, pb_low=l, pb_low_t=T[i], reg_t=T[i], pm_vol=pm_vol)
            P = self.p
            emit("CAND", peak=peak, peak_t=T[b], low0=low0, pb_low=l, pb_pct=dd)
            return ev

        if self.ph == "PULL":
            dd = (1 - l / P["peak"]) * 100
            if dd > cfg["pullback_max_pct"]:
                end("CANCEL", f"반등 확인 전 고점 대비 {dd:.1f}% 하락 ({cfg['pullback_max_pct']:g}% 초과)")
                return ev
            if l < P["pb_low"]:
                P["pb_low"], P["pb_low_t"] = l, T[i]
            nb = cfg["c_break_bars"]
            if (i >= nb and c > o and all(c > bars[i - j][2] for j in range(1, nb + 1))
                    and ema <= c <= ema * (1 + cfg["ema_band_pct"] / 100)
                    and c >= cfg.get("min_price", 0)):             # ④ C봉 종가 최소 주가 이상 (사용자 추가)
                P.update(c_t=T[i], c_close=c, c_ema=float(ema), run_hi=h, run_hi_t=T[i])
                self.ph = "H1"
                emit("C", peak=P["peak"], peak_t=P["peak_t"], pb_low=P["pb_low"], pb_low_t=P["pb_low_t"],
                     pb_pct=(1 - P["pb_low"] / P["peak"]) * 100, ema=float(ema), close=c, h1=None)
                if cfg.get("entry_signal", "B") == "C":         # C 봉에서 바로 진입 -> B자리 추적 안 함
                    self.ph, self.p = "IDLE", {}
            return ev

        # ── C 이후 ──
        ci = T.index(P["c_t"])
        band, sb = cfg["h2_band_pct"] / 100, cfg["s_band_pct"] / 100
        mid_k = 1 - cfg["mid_pullback_pct"] / 100
        while True:
            ph = self.ph
            if ph == "H1":
                if h > P["run_hi"]:
                    P["run_hi"], P["run_hi_t"] = h, T[i]
                if c >= bars[i - 1][4]:
                    break
                P.update(h1=P["run_hi"], h1_t=P["run_hi_t"], h1_conf_t=T[i])
                self.ph = "MID"
                emit("H1", h1=P["h1"], h1_t=P["h1_t"])
                j1 = T.index(P["h1_t"])
                mids = [j for j in range(j1 + 1, i) if bars[j][3] <= P["h1"] * mid_k]
                if mids:                                   # 확정봉 전에 이미 3% 눌림이 있었음
                    P["mid_t"] = T[mids[0]]
                    self.ph = "H2W"
                continue                                   # 같은 봉을 다음 단계로 다시 확인
            if ph == "MID":
                if h > P["h1"] * (1 + band) and cfg.get("mid_break_rule", "cancel") == "cancel":
                    end("CANCEL", f"중간 눌림 전에 H1 {money(P['h1'])} 보다 1% 넘게 상승")     # 요청서에 없는 해석 (설정으로 끌 수 있음)
                    return ev
                if T[i] != P["h1_t"] and l <= P["h1"] * mid_k:
                    P["mid_t"] = T[i]
                    self.ph = "H2W"
                break                                      # 중간 눌림 봉은 H2 후보가 될 수 없음
            if ph == "H2W":
                if h > P["h1"] * (1 + band):
                    end("CANCEL", f"두 번째 상승 고가 {money(h)} 가 H1 {money(P['h1'])} +1% 초과")
                    return ev
                if h >= P["h1"] * (1 - band):
                    P.update(h2_hi=h, h2_t=T[i], h2_start_t=T[i])
                    self.ph = "H2"
                    continue
                break
            if ph == "H2":
                if T[i] != P["h2_start_t"]:
                    if h > P["h1"] * (1 + band):
                        end("CANCEL", f"H2 확정 전 고가 {money(h)} 가 H1 {money(P['h1'])} +1% 초과")
                        return ev
                    if h > P["h2_hi"]:
                        P["h2_hi"], P["h2_t"] = h, T[i]
                if c < bars[i - 1][4]:
                    j1, j2 = T.index(P["h1_t"]), T.index(P["h2_t"])
                    if j2 - j1 < 2:
                        end("CANCEL", "H1 봉·중간 저점 봉·H2 봉이 서로 다른 봉이어야 함")
                        return ev
                    js = min(range(j1 + 1, j2), key=lambda j: bars[j][3])
                    P.update(h2=P["h2_hi"], S=bars[js][3], s_t=T[js], h2_conf_t=T[i])
                    self.ph = "SUP"
                    emit("H2", h1=P["h1"], h2=P["h2"], S=P["S"])
                    if c < P["S"]:
                        end("CANCEL", f"H2 확정봉 종가 {money(c)} 가 S {money(P['S'])} 아래")
                        return ev
                break
            if ph == "SUP":
                if T[i] == P["h2_conf_t"]:
                    break
                if c < P["S"]:
                    end("CANCEL", f"H2 확정 후 종가 {money(c)} 가 S {money(P['S'])} 아래 (지지 실패)")
                    return ev
                if P["S"] * (1 - sb) <= l <= P["S"] * (1 + sb):
                    P.update(re_t=T[i], re_low=l)
                    self.ph = "REB"
                    emit("RE", S=P["S"], re_low=l)
                    continue
                break
            if ph == "REB":
                if c < P["S"]:
                    end("CANCEL", f"재접근 후 종가 {money(c)} 가 S {money(P['S'])} 아래")
                    return ev
                if c > o and c > bars[i - 1][4] and c > P["S"]:
                    emit("B", peak=P["peak"], pb_low=P["pb_low"], c_t=P["c_t"], h1=P["h1"], h1_t=P["h1_t"],
                         h2=P["h2"], h2_t=P["h2_t"], S=P["S"], s_t=P["s_t"], re_low=P["re_low"], re_t=P["re_t"],
                         close=c)
                    self.ph, self.p = "IDLE", {}
                    return ev
                if i - T.index(P["re_t"]) >= cfg["b_rebound_bars"]:
                    end("EXPIRE", f"재접근 후 {cfg['b_rebound_bars']}봉 안에 반등 양봉 없음")
                    return ev
                break
            break
        if self.ph != "IDLE" and i - ci >= cfg["b_max_bars_after_c"]:
            end("EXPIRE", f"C 알림 후 {cfg['b_max_bars_after_c']}봉 안에 B 신호 없음")
        return ev


def signal_order(events):
    """같은 시점에 여러 B 신호가 오면 처리 순서 고정: 신호봉 마감 시각 -> 종목 코드 (자금·자리 중복 배정 없음)"""
    return sorted(events, key=lambda e: (e["t_end"], e["tk"]))


# ═════════════════════ 모의 계좌·주문 (실시간·백테스트·테스트 공용) ═════════════════════
class Core:
    """5분봉 -> C/B 이벤트 -> 모의 주문, 1분 시세 -> 체결·종목 청산·계좌 청산
    mode: live (실시간, 종가 기준), cons (백테스트 보수), opt (백테스트 낙관)
    상태는 전부 self.st (dict, JSON 저장 가능) 에 있음 -> 재시작 복구"""

    def __init__(self, cfg, st, log=None, alert=None, mode="live", on_save=None):
        self.c, self.st, self.mode = cfg, st, mode
        self.sig = bool(cfg.get("signal_mode"))          # 실시간 신호 모드 (GitHub): 칸·계좌 제한 없이 모든 신호 안내
        self.entry_type = cfg.get("entry_signal", "B")   # 진입 신호: "C" (C 봉에서 바로) / "B" (요청서 원안)
        self.log = log or (lambda m: None)
        self.alert, self.on_save = alert, on_save
        defaults = dict(pos={}, orders={}, trk={}, sent=[], entered=[], rows=[], trades=[], last1m={}, prices={},
                        seq=0, locked=None, acct_amb=False, unknown=dict(tpsl=0, acct=0),
                        missed=dict(slots=0, lock=0, cash=0, other=0), n_c=0, n_b=0)
        for k, v in defaults.items():
            st.setdefault(k, copy.deepcopy(v))
        st.setdefault("cash", st.get("start_eq", cfg["capital"]))
        st.setdefault("start_eq", st["cash"])
        self.trk = {tk: Tracker(tk, d) for tk, d in st["trk"].items()}
        self._sent = set(st["sent"])

    # ── 저장·알림 ──
    def snapshot(self):
        self.st["trk"] = {tk: t.to_dict() for tk, t in self.trk.items()}
        self.st["sent"] = sorted(self._sent)
        return self.st

    def say_once(self, key, text):
        """같은 이벤트는 한 번만 알림. 먼저 '보냄'으로 저장한 뒤 보냄 (재시작해도 중복 알림 없음)"""
        if key in self._sent:
            return False
        self._sent.add(key)
        if self.on_save:
            self.on_save()
        if self.alert:
            self.alert(text)
        return True

    # ── 계좌 ──
    def fee(self):
        return self.c["fee_pct"] / 100

    def slip(self):
        return self.c["slippage_pct"] / 100

    def open_buys(self):
        return [o for o in self.st["orders"].values() if o["side"] == "BUY" and o["status"] == "NEW"]

    def slots_used(self):
        return len(set(self.st["pos"]) | {o["tk"] for o in self.open_buys()})

    def reserved(self, skip=None):
        return sum(o["reserved"] for o in self.open_buys() if o["id"] != skip)

    def equity(self, px=None):
        """현금 + 보유 종목 청산가치(매도 수수료 반영). 매수 수수료는 이미 현금에서 빠져 있어 중복 차감 없음"""
        px = px or {}
        st = self.st
        v = st["cash"]
        for tk, p in st["pos"].items():
            v += p["qty"] * px.get(tk, st["prices"].get(tk, p["avg"])) * (1 - self.fee())
        return v

    def day_ret(self, px=None):
        return (self.equity(px) / self.st["start_eq"] - 1) * 100

    # ── 5분봉 처리 ──
    def process_5m(self, tk, day5, fresh_from, pm_vol_fn=None):
        """그날 5분봉(EMA 포함) -> 아직 처리 안 한 봉만 패턴 진행. 실행 가능한 B 이벤트 목록 반환"""
        tr = self.trk.setdefault(tk, Tracker(tk))
        if day5 is None or day5.empty:
            return []
        T = [iso(x) for x in day5.index]
        bars = list(zip(day5.index, day5["Open"].astype(float), day5["High"].astype(float),
                        day5["Low"].astype(float), day5["Close"].astype(float)))
        ema, n = day5["ema"].to_numpy(float), day5["n"].to_numpy()
        start = int((day5.index <= ts(tr.last_t)).sum()) if tr.last_t else 0     # 이미 처리한 봉은 다시 안 봄
        out, bt = [], bar_td(self.c)
        for i in range(start, len(bars)):
            evs = tr.step(self.c, bars, T, i, float(ema[i]), int(n[i]), pm_vol_fn(bars[i][0]) if pm_vol_fn else None)
            tr.last_t = T[i]
            fresh = fresh_from is None or bars[i][0] + bt >= fresh_from
            for e in evs:
                e["fresh"], e["t_end"] = fresh, iso(bars[i][0] + bt)
                self._event(e)
                if e["type"] == self.entry_type:
                    out.append(e)
        return out

    def _event(self, e):
        k, tk = e["type"], e["tk"]
        st = self.st
        if k == "SKIP":
            self.log(f"건너뜀 {tk}: {e['why']}")
            st["rows"].append(dict(e))
        elif k in ("NOCAND", "CANCEL", "EXPIRE"):
            label = {"NOCAND": "후보 아님", "CANCEL": "취소", "EXPIRE": "만료"}[k]
            self.log(f"{label} {tk} {clk(e['t'])}: {e['why']}")
            st["rows"].append(dict(e))
        elif k == "CAND":
            self.log(f"눌림 후보 {tk} 고점 {money(e['peak'])} -> 저가 {money(e['pb_low'])} (-{e['pb_pct']:.1f}%) {clk(e['t'])}")
            st["rows"].append(dict(e))
        elif k == "H1":
            self.log(f"  {tk} H1 {money(e['h1'])} ({clk(e['h1_t'])})")
            st["rows"].append(dict(e))
        elif k == "H2":
            self.log(f"  {tk} H2 {money(e['h2'])} 확정, 지지선 S {money(e['S'])}")
            st["rows"].append(dict(e))
        elif k == "RE":
            self.log(f"  {tk} 지지선 재접근 저가 {money(e['re_low'])} (S {money(e['S'])})")
            st["rows"].append(dict(e))
        elif k == "C":
            st["n_c"] += 1
            if self.entry_type == "C":                    # C 진입: 기록·알림은 주문 단계에서 (주문 결과 포함)
                self.log(f"C 매수 신호 {tk} {clk(e['t'])} 종가 {money(e['close'])} (눌림 -{e['pb_pct']:.1f}%, EMA {money(e['ema'])})")
                return
            row = dict(e)
            st["rows"].append(row)
            msg = (f"🟡 C 알림 {tk} {clk(e['t'])} 5분봉 마감\n"
                   f"급등 고점 {money(e['peak'])} → 눌림 저점 {money(e['pb_low'])} (-{e['pb_pct']:.1f}%)\n"
                   f"224 EMA {money(e['ema'])} | C봉 종가 {money(e['close'])}\n주문 없음, B자리 추적 시작")
            self.log(msg.replace("\n", " | "))
            if e["fresh"]:
                self.say_once(f"C|{e['pid']}", msg)
            else:
                self.log(f"  (지난 봉이라 알림 생략: {tk} C)")

    def end_day(self, T):
        """거래일 끝: 진행 중인 후보·패턴 만료 (다음 날로 넘기지 않음)"""
        for tk, tr in self.trk.items():
            if tr.ph != "IDLE":
                self._event(dict(type="EXPIRE", tk=tk, pid=tr.p.get("pid"), t=iso(T), why=f"거래일 끝 ({tr.ph} 단계에서 만료)"))
                tr.ph, tr.p = "IDLE", {}

    # ── B 신호 -> 주문 ──
    def submit_b(self, e, now, ref, not_before):
        st, c = self.st, self.c
        tk, pid = e["tk"], e["pid"]
        st["n_b"] += 1
        why, oid, qty, amt = None, None, 0, 0.0
        if not e.get("fresh", True):
            why = "지난 봉 신호 (재시작·지연으로 늦게 확인) - 주문 안 함"
            st["missed"]["other"] += 1
        elif pid in st["entered"]:
            why = "같은 C 패턴은 진입 1번만 허용"
            st["missed"]["other"] += 1
        elif st["locked"]:
            why = f"거래 잠금 중 ({st['locked']['why']})"
            st["missed"]["lock"] += 1
        elif tk in st["pos"] or any(o["tk"] == tk for o in self.open_buys()):
            why = "같은 종목 보유·주문 중 (추가매수 금지)"
            st["missed"]["other"] += 1
        elif self.slots_used() >= c["max_positions"]:
            why = f"보유 한도 초과 (보유·미체결 {self.slots_used()}/{c['max_positions']}종목)"
            st["missed"]["slots"] += 1
        else:
            unit = ref * (1 + self.slip()) * (1 + self.fee())
            amt = min(st["start_eq"] * c["position_pct"] / 100, st["cash"] - self.reserved())
            qty = int(amt // unit) if unit > 0 and amt > 0 else 0
            if qty < 1:
                why = "가용 현금 부족"
                st["missed"]["cash"] += 1
        if why is None:
            st["seq"] += 1
            oid = f"BUY-{st['day'].replace('-', '')}-{st['seq']:03d}-{tk}"
            st["orders"][oid] = dict(id=oid, tk=tk, pid=pid, side="BUY", qty=qty, ref=ref, amt=amt,
                                     reserved=qty * ref * (1 + self.slip()) * (1 + self.fee()), status="NEW",
                                     created=iso(now), not_before=iso(not_before),
                                     deadline=iso(ts(not_before) + pd.Timedelta(minutes=c["buy_timeout_min"])),
                                     h1=e.get("h1"), signal_t=e["t"], seq=st["seq"])
            st["entered"].append(pid)
        result = f"모의 매수 주문 {qty}주 (약 ${qty * ref:,.0f})" if why is None else f"주문 안 함: {why}"
        if self.sig and why is None:
            result = (f"매수 신호 (다음 시세 진입 가정) | 익절 +{c['tp_pct']:g}% / 손절 -{c['sl_pct']:g}%"
                      + (f" / {c['max_hold_min']:g}분 뒤 청산" if c.get("max_hold_min") else "")
                      + (f" / 한국 {c['force_exit_kst']} 전량 청산" if c.get("force_exit_kst") else ""))
        st["rows"].append(dict(e, result=result, order_id=oid))
        if e["type"] == "C":
            msg = (f"🟡 C 매수 신호 {tk} {clk(e['t'])} 5분봉 마감\n"
                   f"급등 고점 {money(e['peak'])} → 눌림 저점 {money(e['pb_low'])} (-{e['pb_pct']:.1f}%)\n"
                   f"224 EMA {money(e['ema'])} | C봉 종가 {money(e['close'])}\n→ {result}")
        else:
            msg = (f"🔔 B 신호 {tk} {clk(e['t'])} 5분봉 마감\n"
                   f"H1 {money(e['h1'])} / H2 {money(e['h2'])} / 지지선 S {money(e['S'])}\n"
                   f"재접근 저가 {money(e['re_low'])} | B봉 종가 {money(e['close'])}\n→ {result}")
        self.log(msg.replace("\n", " | "))
        if e.get("fresh", True):
            self.say_once(f"{e['type']}|{pid}", msg)
        return oid

    # ── 1분 시세 ──
    def expire_orders(self, now):
        for o in self.open_buys():
            if now > ts(o["deadline"]):
                self._cancel(o, "제한 시간 안에 체결 시세 없음 (미체결 취소)")

    def _cancel(self, o, why):
        o.update(status="CANCELLED", cancel_why=why)
        self.log(f"주문 취소 {o['tk']} {o['id']}: {why}")

    def on_minute(self, T, rows, fill_field="open"):
        """T 분의 시세 rows {종목: (시, 고, 저, 종)}. 순서: 체결 -> 계좌 기준(우선) -> 종목 익절·손절 -> 계좌 기준"""
        st = self.st
        for o in sorted(self.open_buys(), key=lambda x: x["seq"]):
            if T > ts(o["deadline"]):
                self._cancel(o, "제한 시간 안에 체결 시세 없음 (미체결 취소)")
            elif o["tk"] in rows and T >= ts(o["not_before"]):
                r = rows[o["tk"]]
                self._fill(o, r[0] if fill_field == "open" else r[3], T, fill_field)
        for tk, r in rows.items():
            if tk in st["pos"]:
                st["prices"][tk] = r[3]
        if st["locked"]:
            return
        self._account(T, rows)
        if st["locked"]:
            return
        for tk in sorted(st["pos"]):
            if tk in rows:
                self._exit(tk, T, rows[tk])
        self.time_exits(T)                                  # 이 분에 시세가 없는 종목도 보유 시간 초과면 정리
        self._account(T, {tk: (r[3],) * 4 for tk, r in rows.items()})

    def _hold_why(self):
        return f"보유 {self.c['max_hold_min']:g}분 청산"

    def _time_due(self, p, T):
        hold = self.c.get("max_hold_min")
        return bool(hold) and T >= ts(p["entry_t"]) + pd.Timedelta(minutes=hold)

    def time_exits(self, T):
        """진입 후 max_hold_min(60분) 지나도 익절·손절이 안 났으면 최근 시세로 정리"""
        for tk in sorted(self.st["pos"]):
            p = self.st["pos"][tk]
            if self._time_due(p, T):
                q = self.st["prices"].get(tk, p["avg"])
                self._close(tk, T, q * (1 - self.slip()), self._hold_why(), None, q)

    def _fill(self, o, raw_px, T, fill_field):
        st, c = self.st, self.c
        px = raw_px * (1 + self.slip())
        if c.get("tp_mode") == "h1" and (not o.get("h1") or o["h1"] <= px):
            st["missed"].setdefault("h1_na", 0)
            st["missed"]["h1_na"] += 1
            self._cancel(o, f"비교 불가: H1 {money(o['h1'])} <= 진입가 {money(px)}")
            return
        avail = min(o.get("amt", math.inf), st["cash"] - self.reserved(skip=o["id"]))   # 목표 25% 와 가용 현금 안에서
        qty = min(o["qty"], int(avail // (px * (1 + self.fee()))))
        if qty < 1:
            self._cancel(o, "체결가 기준 가용 현금 부족")
            return
        cost = qty * px * (1 + self.fee())
        st["cash"] -= cost
        o.update(status="FILLED", filled=qty, avg=px, filled_t=iso(T), reserved=0.0)
        tp = o["h1"] if c.get("tp_mode") == "h1" else px * (1 + c["tp_pct"] / 100)
        p = dict(tk=o["tk"], pid=o["pid"], oid=o["id"], qty=qty, avg=px, cost=cost, tp=tp,
                 sl=px * (1 - c["sl_pct"] / 100), entry_t=iso(T), entry_min=iso(ts(T).floor("min")),
                 entry_field=fill_field, h1=o.get("h1"), to_h1=(o["h1"] / px - 1) * 100 if o.get("h1") else None,
                 signal_t=o["signal_t"])
        st["pos"][o["tk"]] = p
        st["prices"][o["tk"]] = px
        hold = c.get("max_hold_min")
        msg = (f"🟢 매수 체결 {o['tk']} {money(px)} x {qty}주 ({clk(T)})\n"
               + (f"H1까지 {p['to_h1']:+.1f}% | " if p["to_h1"] is not None else "")
               + f"익절 {money(p['tp'])} / 손절 {money(p['sl'])}"
               + (f" / {clk(ts(T) + pd.Timedelta(minutes=hold))} 시간 청산" if hold else "")
               + f" | 보유 {len(st['pos'])}/{c['max_positions']}")
        self.log(msg.replace("\n", " | "))
        if not self.sig:                                    # 신호 모드는 B 신호 알림에 이미 안내함
            self.say_once(f"F|{o['id']}", msg)

    def _exit(self, tk, T, r):
        p = self.st["pos"][tk]
        o, h, l, c = r
        if p["entry_field"] == "close" and iso(ts(T).floor("min")) == p["entry_min"]:
            o = h = l = c          # 실시간: 매수한 분봉은 매수 이후 시세(현재가)만 사용
        if self._time_due(p, T):   # 보유 60분: 그 분이 시작될 때 정리 (백테스트 = 그 분 시가, 실시간 = 현재가)
            px = o if p["entry_field"] == "open" else c
            self._close(tk, T, px * (1 - self.slip()), self._hold_why(), None, px)
            return
        tp, sl = p["tp"], p["sl"]
        if o <= sl:
            self._close(tk, T, o * (1 - self.slip()), "손절", sl, o)
        elif o >= tp:
            self._close(tk, T, o, "익절", tp, o)
        else:
            hit_sl, hit_tp = l <= sl, h >= tp
            amb = hit_sl and hit_tp
            if amb:
                self.st["unknown"]["tpsl"] += 1
                hit_sl = self.mode != "opt"            # 순서 불명: 보수=손절 먼저, 낙관=익절 먼저
                hit_tp = not hit_sl
            if hit_sl:
                self._close(tk, T, sl * (1 - self.slip()), "손절", sl, l, amb=amb)
            elif hit_tp:
                self._close(tk, T, tp, "익절", tp, h, amb=amb)

    def _close(self, tk, T, px, why, target, trigger, amb=False, quiet=False):
        st = self.st
        p = st["pos"].pop(tk)
        proceeds = p["qty"] * px * (1 - self.fee())
        st["cash"] += proceeds
        net = proceeds - p["cost"]
        tr = dict(day=st["day"], tk=tk, pid=p["pid"], order_id=p["oid"], signal_t=p["signal_t"], entry_t=p["entry_t"],
                  qty=p["qty"], avg=round(p["avg"], 6), h1=p["h1"],
                  to_h1_pct=round(p["to_h1"], 2) if p["to_h1"] is not None else None,
                  tp=round(p["tp"], 6), sl=round(p["sl"], 6), exit_t=iso(T), why=why,
                  target=None if target is None else round(target, 6), trigger=round(trigger, 6),
                  exit_px=round(px, 6), net_usd=round(net, 2), net_pct=round(net / p["cost"] * 100, 3), amb=amb)
        st["trades"].append(tr)
        icon = "✅" if why == "익절" else "🔻" if why == "손절" else "⏱" if why.startswith("보유") else "⏹"
        msg = (f"{icon} 매도 {tk} {why} ({clk(T)})\n"
               f"목표 {money(target) if target else '-'} | 발동 시세 {money(trigger)} | 체결 {money(px)}\n"
               f"손익 {tr['net_pct']:+.2f}% (${net:+,.2f}) | 오늘 계좌 {self.day_ret():+.2f}%")
        if self.sig:
            msg = (f"{icon} {tk} {why} ({clk(T)})\n"
                   f"진입 {money(p['avg'])} → 청산 {money(px)} | {tr['net_pct']:+.2f}% (수수료·슬리피지 반영)")
        self.log(msg.replace("\n", " | "))
        if not quiet:
            self.say_once(f"X|{p['oid']}", msg)

    def _account(self, T, rows):
        """당일 계좌 수익률 ±기준 처음 도달 -> 잠금 + 전량 청산 (종목 청산보다 우선)"""
        st, c = self.st, self.c
        if st["locked"]:
            return
        tgt, stp = c["account_target_pct"], c["account_stop_pct"]
        pc = {tk: (rows[tk][3] if tk in rows else st["prices"].get(tk, p["avg"])) for tk, p in st["pos"].items()}
        # 봉 안의 저가·고가로 볼 때도 종목 손절·익절가 너머는 안 봄 (그 전에 종목 주문이 먼저 체결됐을 것)
        pl = {tk: (max(rows[tk][2], p["sl"]) if tk in rows else pc[tk]) for tk, p in st["pos"].items()}
        ph = {tk: (min(rows[tk][1], p["tp"]) if tk in rows else pc[tk]) for tk, p in st["pos"].items()}
        rc = self.day_ret(pc)
        hit = None
        if self.mode == "cons":
            rl = self.day_ret(pl)
            if rl <= -stp:
                hit = (f"계좌 -{stp:g}% 도달", pl, rl)
            elif rc >= tgt:
                hit = (f"계좌 +{tgt:g}% 도달", pc, rc)
            if not st["acct_amb"] and rl <= -stp < rc:
                st["acct_amb"] = True
                st["unknown"]["acct"] += 1
        elif self.mode == "opt":
            rh = self.day_ret(ph)
            if rh >= tgt:
                hit = (f"계좌 +{tgt:g}% 도달", ph, rh)
            elif rc <= -stp:
                hit = (f"계좌 -{stp:g}% 도달", pc, rc)
            if not st["acct_amb"] and rh >= tgt > rc:
                st["acct_amb"] = True
                st["unknown"]["acct"] += 1
        else:
            if rc >= tgt:
                hit = (f"계좌 +{tgt:g}% 도달", pc, rc)
            elif rc <= -stp:
                hit = (f"계좌 -{stp:g}% 도달", pc, rc)
        if hit:
            self.lock(T, *hit)

    def lock(self, T, why, px, ret):
        """거래 잠금: 미체결 매수 취소 -> 보유 수량과 기존 매도 주문 대조 -> 남은 수량 전량 청산 -> 0 확인"""
        st = self.st
        st["locked"] = dict(t=iso(T), why=why, ret=round(ret, 3))
        cancelled = []
        for o in self.open_buys():
            self._cancel(o, f"거래 잠금 ({why})")
            cancelled.append(o["tk"])
        sold = []
        for tk in sorted(st["pos"]):
            p = st["pos"][tk]
            pending_sell = sum(o["qty"] for o in st["orders"].values()
                               if o["side"] == "SELL" and o["status"] == "NEW" and o["tk"] == tk)
            if p["qty"] - pending_sell > 0:   # 모의 매도는 즉시 체결이라 대기 매도는 없음
                q = px.get(tk, st["prices"].get(tk, p["avg"]))
                self._close(tk, T, q * (1 - self.slip()), why, None, q, quiet=True)
                sold.append(f"{tk} {money(q)}")
        left = [tk for tk, p in st["pos"].items() if p["qty"] > 0]
        final = self.day_ret()
        st["locked"].update(cancelled=cancelled, sold=sold, final=round(final, 3), left=left)
        msg = (f"⏹ {why} ({clk(T)}) → 거래 잠금 (다음 거래일까지 신규 진입 없음)\n"
               f"발동 시 계좌 {ret:+.2f}% | 취소한 주문: {', '.join(cancelled) or '없음'}\n"
               f"청산: {', '.join(sold) or '보유 없음'}\n최종 계좌 {final:+.2f}%"
               + (f"\n⚠️ 남은 수량 있음: {left}" if left else ""))
        self.log(msg.replace("\n", " | "))
        self.say_once(f"LOCK|{st['day']}", msg)

    def flatten(self, T, why="세션 종료 전 정리"):
        st = self.st
        for o in self.open_buys():
            self._cancel(o, why)
        for tk in sorted(st["pos"]):
            q = st["prices"].get(tk, st["pos"][tk]["avg"])
            self._close(tk, T, q * (1 - self.slip()), why, None, q)


# ═════════════════════ 데이터 (yfinance) ═════════════════════
def _yf():
    import logging
    import yfinance as yf
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)   # 'possibly delisted' 같은 경고 숨김
    return yf


def download(tickers, chunk=100, quiet=True, **kw):
    yf, out = _yf(), {}
    tickers = list(tickers)
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        for attempt in range(2):
            try:
                df = yf.download(part, group_by="ticker", auto_adjust=True, threads=True, progress=False, **kw)
                out.update(_split(df, part))
                break
            except Exception as e:
                if attempt:
                    print(f"  ! 다운로드 실패 ({part[0]}~): {e}")
                time.sleep(2)
        if not quiet:
            print(f"  {min(i + chunk, len(tickers))}/{len(tickers)}", end="\r", flush=True)
    if not quiet:
        print()
    return out


def screen_all():
    """야후 검색기로 NYSE·NASDAQ·AMEX 전 종목 시세 (250개씩, 약 30번 요청)"""
    yf = _yf()
    q = yf.EquityQuery("is-in", ["exchange", *EXCHANGES])
    quotes, off, total = [], 0, 0
    while True:
        for attempt in range(4):                     # 야후가 가끔 401·429 를 줌 -> 쉬었다 다시
            try:
                r = yf.screen(q, offset=off, size=250, sortField="intradaymarketcap", sortAsc=False)
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(3 * (attempt + 1))
        got = r.get("quotes", [])
        quotes += got
        off += len(got)
        total = r.get("total") or total
        if not got or off >= total or off >= 15000:
            break
    return quotes, total


def epoch_date(x):
    return pd.Timestamp(int(x), unit="s", tz="UTC").tz_convert(ET_S).date() if x else None


def prepare_5m(raw, cfg, cutoff=None):
    """원본 5분봉(프리·애프터 포함) -> 마감된 봉, 설정 세션만, 224 EMA(이전 거래일 포함)·누적 봉 수"""
    df = raw.dropna(subset=["Close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    idx = df.index
    df = df[(idx.second == 0) & (idx.minute % cfg["bar_minutes"] == 0)]      # 시각이 어긋난 봉 = 진행 중인 봉
    if cutoff is not None:
        df = df[df.index + bar_td(cfg) <= cutoff]
    df = df[sess_mask(df.index, cfg)].copy()
    df["ema"] = df["Close"].ewm(span=cfg["ema_len"], adjust=False).mean()
    df["n"] = np.arange(1, len(df) + 1)
    return df


def pm_volume_fn(raw, d, cfg):
    """프리장 누적 거래량 (봉 시작 시각 이전까지). 야후는 프리장 거래량을 0 으로 줘서 0 이면 None"""
    m = np.asarray(raw.index.hour * 60 + raw.index.minute)
    pre = raw[(raw.index.date == d) & (m >= hm(cfg["premarket_start_et"])) & (m < hm(cfg["regular_start_et"]))]
    vol = pre["Volume"].fillna(0)

    def f(t):
        v = float(vol[vol.index < t].sum())
        return v if v > 0 else None
    return f


def check_data(raw, daily, d, cfg):
    """이전 거래일 5분봉 마지막 정규장 종가 vs 일봉 종가가 10% 넘게 다른 날(분할 반영 오류)이 있으면
    그날까지의 5분봉은 버림 (EMA 가 엉터리 가격으로 계산되지 않게). 반환 (쓸 5분봉, 메모 또는 None)"""
    if daily is None or daily.empty or raw is None or raw.empty:
        return raw, None
    m = np.asarray(raw.index.hour * 60 + raw.index.minute)
    reg = raw[(m >= 570) & (m < 960)].dropna(subset=["Close"])
    last = reg.groupby(reg.index.date)["Close"].last()
    dc = {pd.Timestamp(x).date(): float(v) for x, v in daily["Close"].dropna().items()}
    bad = [(day, c5, dc[day]) for day, c5 in last.items()
           if day < d and dc.get(day) and abs(c5 / dc[day] - 1) * 100 > cfg["data_check_pct"]]
    if not bad:
        return raw, None
    day, c5, dv = max(bad)
    return (raw[raw.index.date > day],
            f"가격 데이터 불일치 {day} (5분봉 {c5:.4g} / 일봉 {dv:.4g}, 분할 반영 의심) → 그날까지 5분봉은 버리고 사용")


# ═════════════════════ 상태 저장 ═════════════════════
def state_path(d, tag=""):
    return os.path.join(STATE_DIR, f"pullback{tag}_{pd.Timestamp(d):%Y%m%d}.json")


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def carry_equity(cfg):
    a = load_json(ACCOUNT_FILE)
    return float(a["equity"]) if a and a.get("equity") else float(cfg["capital"])


def write_records(st, d, tag=""):
    """신호·체결 기록을 상태에서 통째로 다시 써서 재시작해도 중복 줄이 생기지 않게"""
    os.makedirs(LOGS, exist_ok=True)
    day = f"{pd.Timestamp(d):%Y%m%d}"
    if st["rows"]:
        pd.DataFrame(st["rows"]).to_csv(os.path.join(LOGS, f"pullback{tag}_signals_{day}.csv"), index=False, encoding="utf-8-sig")
    if st["trades"]:
        pd.DataFrame(st["trades"]).to_csv(os.path.join(LOGS, f"pullback{tag}_trades_{day}.csv"), index=False, encoding="utf-8-sig")


# ═════════════════════ 실시간 모의매매 ═════════════════════
class LivePaper:
    def __init__(self, cfg, dry=False, signal=False):
        self.c, self.dry, self.sig = cfg, dry, signal
        self.title = "실시간 신호" if signal else TITLE       # 텔레그램 [실시간 신호] / [눌림목 모의]
        self.tag = "_live" if signal else ""
        self.raw5, self.excluded, self.trimmed = {}, {}, {}
        self.uni, self.logf, self.cov = None, None, {}

    def log(self, msg):
        n = now_et()
        line = f"[{n:%H:%M:%S}ET {n.tz_convert(KST_S):%H:%M}KST] {msg}"
        print(line, flush=True)
        if self.logf:
            self.logf.write(line + "\n")
            self.logf.flush()

    def say(self, text):
        return [] if self.dry else notify(text, self.title)

    def coverage_text(self):
        v = self.cov
        if not v:
            return "검색 전"
        return (f"보통주 {v['universe']:,}개 중 검색기 시세 {v['covered']:,}개 (시세 없음 {v['missing']:,}개: 거래정지·신규상장 등) | "
                f"일중 변동폭 {self.c['prefilter_day_range_pct']:g}% 이상 등 후보 {v['cands']}개 → 5분봉 확인 {v['with5m']}개 | "
                f"제외: EMA 부족 {v['ema_short']}, 5분봉 없음 {v['no5m']} | 분할 의심 앞부분 잘라냄 {v['bad']} | {v['sec']:.0f}초")

    def scan(self, core, now, d):
        """전 종목 검색 -> 후보 5분봉 -> 마감된 새 봉만 패턴 처리 -> B 신호는 고정 순서로 주문"""
        cfg = self.c
        t0 = time.time()
        try:
            quotes, total = screen_all()
        except Exception as e:                       # 검색기 실패: 지금까지 찾은 후보로 5분봉 판정은 계속
            self.log(f"전 종목 검색 실패 ({type(e).__name__}: {e}) - 기존 후보 {len(self.raw5)}종목으로 계속")
            quotes, total = [], self.cov.get("total", 0)
        q = {x["symbol"]: x for x in quotes if x.get("symbol")}
        covered = [s for s in self.uni if s in q]
        rng = 1 + cfg["prefilter_day_range_pct"] / 100
        cands = set(self.raw5) if not q else set()
        for s in covered:
            x = q[s]
            if cfg["sessions"].get("regular") and epoch_date(x.get("regularMarketTime")) == d:
                hi, lo = x.get("regularMarketDayHigh"), x.get("regularMarketDayLow")
                if hi and lo and lo > 0 and hi / lo >= rng:      # 같은 날 저점 -> 고점 30% 상승의 필요조건
                    cands.add(s)
            if cfg["sessions"].get("premarket") and epoch_date(x.get("preMarketTime")) == d:
                pc = x.get("preMarketChangePercent")
                if pc is not None and abs(pc) >= cfg["prefilter_premarket_change_pct"]:
                    cands.add(s)
        cands |= {tk for tk, tr in core.trk.items() if tr.ph != "IDLE"} | set(core.st["pos"])
        cands -= set(self.excluded)
        new = sorted(x for x in cands if x not in self.raw5)
        old = sorted(x for x in cands if x in self.raw5)
        if new:   # 224 EMA 용 이전 거래일: 1개월 (소형주는 거래 없는 5분봉이 빠져 7일로는 224봉이 안 될 때가 많음)
            self.raw5.update(download(new, period="1mo", interval="5m", prepost=True))
            chk = [x for x in new if x in self.raw5]
            daily = download(chk, period="2mo", interval="1d") if chk else {}
            for x in chk:
                self.raw5[x], why = check_data(self.raw5[x], daily.get(x), d, cfg)
                if why:
                    self.trimmed[x] = why
                    self.log(f"{x}: {why}")
        if old:
            got = download(old, period="1d", interval="5m", prepost=True)
            for x, df in got.items():
                prev = self.raw5[x]
                self.raw5[x] = pd.concat([prev[prev.index.date < d], df])
        cutoff = now - pd.Timedelta(seconds=cfg["bar_grace_sec"])
        fresh_from = now - pd.Timedelta(minutes=cfg["fresh_window_min"])
        bsig, n5, no5 = [], 0, 0
        for tk in sorted(cands):
            if tk in self.excluded:
                continue
            raw = self.raw5.get(tk)
            if raw is None or raw.empty:
                no5 += 1
                continue
            d5 = prepare_5m(raw, cfg, cutoff)
            day5 = d5[d5.index.date == d]
            if day5.empty:
                continue
            n5 += 1
            pmf = pm_volume_fn(raw, d, cfg) if cfg["premarket_volume_filter"].get("enabled") else None
            bsig += core.process_5m(tk, day5, None if self.dry else fresh_from, pmf)
        self.cov = dict(universe=len(self.uni), total=total, covered=len(covered), missing=len(self.uni) - len(covered),
                        cands=len(cands), with5m=n5, no5m=no5, bad=len(self.trimmed),
                        ema_short=sum(1 for t in core.trk.values() if t.notes.get("ema") and not t.notes.get("ema_ok")),
                        sec=time.time() - t0)
        self.log("검색: " + self.coverage_text())
        if self.dry:
            core.st["rows"] += [dict(e, result="검색만 (주문 없음)") for e in bsig]
            return bsig
        for e in signal_order(bsig):
            core.submit_b(e, now, ref=e["close"], not_before=now.floor("min"))
        return bsig

    def poll_1m(self, core, now):
        """보유·미체결 종목의 1분봉(진행 중인 분 포함) -> 체결·익절·손절·계좌 기준"""
        st, cfg = core.st, self.c
        tks = sorted(set(st["pos"]) | {o["tk"] for o in core.open_buys()})
        if not tks:
            return
        data = download(tks, period="1d", interval="1m", prepost=True)
        mins = collections.defaultdict(dict)
        for tk in tks:
            df = data.get(tk)
            if df is None or df.empty:
                continue
            df = df.dropna(subset=["Close"]).sort_index()
            df.index = df.index.floor("min")
            df = df[~df.index.duplicated(keep="last")]
            df = df[sess_mask(df.index, cfg)]
            starts = [ts(o["created"]).floor("min") for o in core.open_buys() if o["tk"] == tk]
            if tk in st["pos"]:
                starts.append(ts(st["last1m"].get(tk) or st["pos"][tk]["entry_min"]))
            if not starts:
                continue
            df = df[df.index >= min(starts)]
            for t, r in zip(df.index, df[["Open", "High", "Low", "Close"]].itertuples(index=False)):
                mins[t][tk] = tuple(float(v) for v in r)
            if len(df):
                st["last1m"][tk] = iso(df.index[-1])
        for t in sorted(mins):
            core.on_minute(t, mins[t], fill_field="close")
        core.expire_orders(now)

    def status(self, core):
        st = core.st
        held = ", ".join(f"{tk} {(st['prices'].get(tk, p['avg']) / p['avg'] - 1) * 100:+.1f}%" for tk, p in st["pos"].items())
        active = sum(1 for t in core.trk.values() if t.ph != "IDLE")
        self.log(f"상태: 패턴 추적 {active}종목, 보유 {len(st['pos'])}/{self.c['max_positions']} [{held or '없음'}], "
                 f"미체결 {len(core.open_buys())}, 계좌 {core.day_ret():+.2f}%"
                 + (f", 잠금({st['locked']['why']})" if st["locked"] else ""))

    def run(self):
        cfg = self.c
        now = now_et()
        d = now.date()
        os.makedirs(LOGS, exist_ok=True)
        self.logf = open(os.path.join(LOGS, f"pullback{self.tag}_{d:%Y%m%d}.log"), "a", encoding="utf-8")
        if now.weekday() >= 5:
            self.log("주말이라 장이 열리지 않습니다.")
            return
        s0, s1, flat = session_times(d, cfg)
        if now >= flat:
            self.log(f"오늘 운영 시간이 끝났습니다 (전량 청산 시각 {clk(flat)}).")
            return
        path = state_path(d, self.tag)
        st = load_json(path)
        restarted = st is not None
        if not restarted:
            eq = float(cfg["capital"]) if self.sig else carry_equity(cfg)
            st = dict(day=str(d), start_eq=eq, cash=eq, created=iso(now))
        core = None

        def save():
            save_json(path, core.snapshot())
            write_records(core.st, d, self.tag)
        core = Core(cfg, st, self.log, self.say, mode="live", on_save=save)
        self.uni = load_universe()
        sess = "+".join(k for k, v in (("프리장", cfg["sessions"].get("premarket")), ("정규장", cfg["sessions"].get("regular"))) if v)
        self.log(f"눌림목 모의매매 {'재시작 - 상태 복구' if restarted else '시작'} | {d} | 세션 {sess} {clk(s0)}~{clk(s1)}, "
                 f"정리 {clk(flat)} | 당일 시작 순자산 ${st['start_eq']:,.2f}")
        if restarted:
            self.say(f"🔄 재시작: 상태 복구 ({d})\n보유 {len(st['pos'])} / 미체결 {len(core.open_buys())} / "
                     f"추적 {sum(1 for t in core.trk.values() if t.ph != 'IDLE')}종목 / 계좌 {core.day_ret():+.2f}%"
                     + (f" / 잠금: {st['locked']['why']}" if st["locked"] else ""))
        save()
        first_bar = s0 + bar_td(cfg) + pd.Timedelta(seconds=cfg["bar_grace_sec"])
        if now_et() < first_bar:
            self.log(f"첫 5분봉 마감 대기 ({clk(first_bar)})")
            while now_et() < first_bar:
                time.sleep(min(30, max(1, (first_bar - now_et()).total_seconds())))
        spy = download(["SPY"], period="1d", interval="5m", prepost=True).get("SPY")
        if spy is None or not len(spy[(spy.index.date == d) & sess_mask(spy.index, cfg)]):
            self.log("오늘은 미국 휴장일입니다.")
            self.say(f"📭 {d} 미국 휴장일이라 눌림목 감시를 하지 않습니다")
            return
        next_scan, first, last_status = now_et(), not restarted, now_et()
        while True:
            now = now_et()
            if now >= flat:
                try:
                    self.poll_1m(core, now)
                except Exception as e:
                    self.log(f"1분 시세 오류: {e}")
                core.flatten(now)
                core.end_day(now)
                save()
                break
            try:
                self.poll_1m(core, now)
            except Exception as e:
                self.log(f"1분 시세 오류 (다음에 다시): {e}")
            core.time_exits(now)                          # 시세가 안 들어오는 종목도 60분 지나면 정리
            if now >= next_scan:
                try:
                    self.scan(core, now, d)
                    self.poll_1m(core, now_et())          # 방금 낸 주문을 바로 현재 시세로 체결
                except Exception as e:
                    self.log(f"검색 오류 (다음 봉에 다시): {type(e).__name__}: {e}")
                    traceback.print_exc()
                if first:
                    first = False
                    rule = (f"익절 +{cfg['tp_pct']:g}% / 손절 -{cfg['sl_pct']:g}%"
                            + (f" / 진입 {cfg['max_hold_min']:g}분 뒤 청산" if cfg.get("max_hold_min") else "")
                            + f" / {clk(flat)} 전량 청산")
                    how = ("C자리(눌림 후 첫 반등 봉) 마감 → 바로 매수" if core.entry_type == "C"
                           else "C 알림은 주문 없이 추적, B자리에서 매수")
                    if self.sig:
                        self.say(f"📡 눌림목 실시간 신호 감시 시작 ({d} 미국 장)\n검색: {self.coverage_text()}\n"
                                 f"5분봉 마감마다 전 종목 확인 | {how} 신호 즉시 전송\n{rule}")
                    else:
                        self.say(f"📡 눌림목 모의매매 시작 ({d} 미국 장, {sess})\n검색: {self.coverage_text()}\n"
                                 f"{how} | 종목당 {cfg['position_pct']:g}% · "
                                 f"최대 {cfg['max_positions']}종목 · 계좌 ±{cfg['account_target_pct']:g}% 잠금\n{rule}")
                nxt = now.floor(f"{cfg['bar_minutes']}min") + bar_td(cfg)
                next_scan = nxt + pd.Timedelta(seconds=cfg["bar_grace_sec"])
            save()
            if now - last_status >= pd.Timedelta(minutes=5):
                self.status(core)
                last_status = now
            time.sleep(cfg["poll_sec"])
        self.finish(core, d)

    def finish(self, core, d):
        st = core.st
        trades = st["trades"]
        wins = sum(1 for t in trades if t["net_pct"] > 0)
        if self.sig:
            lines = [f"📋 오늘 눌림목 신호 정리 ({d})",
                     f"{core.entry_type} 매수 신호 {st['n_b']}건" + (f" (C 알림 {st['n_c']}건)" if core.entry_type == "B" else "")]
            for t in trades:
                lines.append(f"• {t['tk']} {clk(t['entry_t'])} 진입 {money(t['avg'])} → {t['why']} {money(t['exit_px'])} "
                             f"({t['net_pct']:+.2f}%)")
            if trades:
                lines.append(f"승률 {wins}/{len(trades)} ({wins / len(trades) * 100:.0f}%) · 평균 "
                             f"{np.mean([t['net_pct'] for t in trades]):+.2f}% (비용 반영)")
            msg = "\n".join(lines)
            self.log(msg.replace("\n", " | "))
            core.say_once(f"END|{d}", msg)
            return
        ret = core.day_ret()
        eq = core.equity()
        lock = st["locked"]
        msg = (f"📊 오늘 결과 ({d})\n거래 {len(trades)}건 (이익 {wins}) | 계좌 {ret:+.2f}% (${eq - st['start_eq']:+,.2f})\n"
               f"{core.entry_type} 매수 신호 {st['n_b']}건 (보유 한도로 놓침 {st['missed']['slots']}, 잠금 후 {st['missed']['lock']})\n"
               f"계좌 기준: {lock['why'] if lock else '±3% 미도달'}")
        self.log(msg.replace("\n", " | "))
        core.say_once(f"END|{d}", msg)
        save_json(ACCOUNT_FILE, dict(equity=round(eq, 2), day=str(d)))
        p = os.path.join(LOGS, "pullback_daily.csv")
        row = pd.DataFrame([dict(date=str(d), start=round(st["start_eq"], 2), end=round(eq, 2), ret_pct=round(ret, 3),
                                 trades=len(trades), wins=wins, c=st["n_c"], b=st["n_b"], missed_slots=st["missed"]["slots"],
                                 lock=lock["why"] if lock else "")])
        old = pd.read_csv(p, encoding="utf-8-sig") if os.path.exists(p) else pd.DataFrame()
        if len(old):
            old = old[old["date"].astype(str) != str(d)]
        pd.concat([old, row]).to_csv(p, index=False, encoding="utf-8-sig")

    def scan_once(self):
        """주문·알림 없이 한 번 검색: 오늘(장 마감 후면 최근 장) 검색 범위와 C/B 이벤트 출력"""
        cfg = self.c
        spy = download(["SPY"], period="5d", interval="5m", prepost=True).get("SPY")
        spy = spy[sess_mask(spy.index, cfg)]
        d = spy.index[-1].date()
        self.uni = load_universe()
        core = Core(cfg, dict(day=str(d)), self.log, None, mode="live")
        now = min(now_et(), session_times(d, cfg)[2])
        self.log(f"한 번 검색: {d} (뉴욕) 기준, {clk(now)} 까지 마감된 5분봉")
        self.scan(core, now, d)
        ev = [r for r in core.st["rows"] if r["type"] in ("C", "B")]
        names = {"PULL": "눌림 후 C 대기", "H1": "H1 추적", "MID": "중간 눌림 대기", "H2W": "H2 대기", "H2": "H2 확정 대기",
                 "SUP": "S 재접근 대기", "REB": "반등 양봉 대기"}
        active = {tk: names.get(t.ph, t.ph) for tk, t in core.trk.items() if t.ph != "IDLE"}
        lines = [f"🔎 눌림목 한 번 검색 ({d} 미국 장, {clk(now)} 까지 마감된 5분봉)",
                 f"검색 범위: {self.coverage_text()}",
                 (f"C 매수 신호 {sum(r['type'] == 'C' for r in ev)}건 (C 봉에서 바로 진입, 익절·손절 {tpsl_text(cfg)} · "
                  f"{cfg.get('max_hold_min') or '-'}분 · {clk(session_times(d, cfg)[2])} 청산)" if core.entry_type == "C" else
                  f"C 알림 {sum(r['type'] == 'C' for r in ev)}건 · B 신호 {sum(r['type'] == 'B' for r in ev)}건")]
        for r in ev:
            if r["type"] == "C":
                lines.append(f"🟡 C {r['tk']} {clk(r['t'])} | 고점 {money(r['peak'])} → 눌림 {money(r['pb_low'])} "
                             f"(-{r['pb_pct']:.1f}%) | EMA {money(r['ema'])} | 종가 {money(r['close'])}")
            else:
                lines.append(f"🔔 B {r['tk']} {clk(r['t'])} | H1 {money(r['h1'])} H2 {money(r['h2'])} S {money(r['S'])} | "
                             f"재접근 {money(r['re_low'])} | 종가 {money(r['close'])}")
        lines.append("진행 중 패턴: " + (", ".join(f"{k}({v})" for k, v in sorted(active.items())) or "없음"))
        lines.append("※ 검색만 함: 주문·체결 없음")
        msg = "\n".join(lines)
        print("\n" + msg)
        return msg


# ═════════════════════ 백테스트 ═════════════════════
def _day_minutes(m1, d, cfg, start, flat):
    df = m1[(m1.index.date == d)]
    df = df[sess_mask(df.index, cfg)]
    return df[(df.index >= start) & (df.index < flat)]


def run_trade(cfg, e, df1, strategy, mode):
    """공통 표본: 신호 하나를 계좌 한도 없이 단독으로 매매 (실시간과 같은 Core 사용)"""
    c2 = dict(cfg, tp_mode=strategy, max_positions=1, account_target_pct=1e9, account_stop_pct=1e9, position_pct=100)
    st = dict(day=str(ts(e["t"]).date()), start_eq=1e7, cash=1e7)
    core = Core(c2, st, mode=mode)
    t_end = ts(e["t_end"])
    core.submit_b(dict(e, fresh=True), t_end, ref=e["close"], not_before=t_end + pd.Timedelta(minutes=cfg["entry_delay_min"]))
    last = None
    for t, r in zip(df1.index, df1[["Open", "High", "Low", "Close"]].itertuples(index=False)):
        if t < t_end:
            continue
        core.on_minute(t, {e["tk"]: tuple(float(v) for v in r)}, "open")
        last = t
        if not st["pos"] and not core.open_buys():
            break
    if st["pos"]:
        core.flatten(last, "세션 종료 전 정리")
    o = next(iter(st["orders"].values()), None)
    if st["trades"]:
        return dict(st["trades"][0], status="ok")
    return dict(status="na" if o and "비교 불가" in o.get("cancel_why", "") else "nofill",
                why=(o or {}).get("cancel_why", "주문 없음"))


def run_account_day(cfg, d, sigs, m1, start_eq, strategy, mode):
    """계좌 단위 하루: 보유 한도·현금·계좌 ±3% 잠금 적용"""
    c2 = dict(cfg, tp_mode=strategy)
    st = dict(day=str(d), start_eq=start_eq, cash=start_eq)
    core = Core(c2, st, mode=mode)
    _, _, flat = session_times(d, cfg)
    sigs = signal_order(sigs)
    if not sigs:
        return st, core
    t0 = ts(sigs[0]["t_end"])
    frames = {tk: _day_minutes(m1[tk], d, cfg, t0, flat) for tk in {e["tk"] for e in sigs} if tk in m1}
    timeline = sorted(set().union(*[set(f.index) for f in frames.values()])) if frames else []
    rows_at = collections.defaultdict(dict)
    for tk, f in frames.items():
        for t, r in zip(f.index, f[["Open", "High", "Low", "Close"]].itertuples(index=False)):
            rows_at[t][tk] = tuple(float(v) for v in r)
    k = 0
    for t in timeline:
        while k < len(sigs) and ts(sigs[k]["t_end"]) <= t:     # 신호봉 마감 때 이미 알려진 신호 -> 이 분 처리 전에 주문
            e = sigs[k]
            core.submit_b(dict(e, fresh=True), ts(e["t_end"]), ref=e["close"],
                          not_before=ts(e["t_end"]) + pd.Timedelta(minutes=cfg["entry_delay_min"]))
            k += 1
        core.on_minute(t, rows_at[t], "open")
    while k < len(sigs):                          # 1분봉이 없어 주문 못 낸 신호도 기록 (한도·잠금 집계용)
        e = sigs[k]
        core.submit_b(dict(e, fresh=True), ts(e["t_end"]), ref=e["close"], not_before=ts(e["t_end"]))
        k += 1
    if st["pos"] or core.open_buys():
        core.flatten(timeline[-1] if timeline else flat, "세션 종료 전 정리")
    return st, core


def _stats(trades):
    n = len(trades)
    if not n:
        return dict(n=0, win=0.0, avg=0.0, tp=0, sl=0, other=0, amb=0)
    net = np.array([t["net_pct"] for t in trades])
    why = collections.Counter(t["why"] for t in trades)
    return dict(n=n, win=(net > 0).mean() * 100, avg=net.mean(), tp=why.get("익절", 0), sl=why.get("손절", 0),
                other=n - why.get("익절", 0) - why.get("손절", 0), amb=sum(1 for t in trades if t.get("amb")))


def backtest(cfg, ndays=None, use_cache=True):
    yf = _yf()
    t0 = time.time()
    out_lines = []

    def P(s=""):
        print(s, flush=True)
        out_lines.append(s)

    today = now_et().date()
    spy = yf.download("SPY", period="2mo", interval="1d", progress=False, auto_adjust=True)
    days = [pd.Timestamp(x).date() for x in spy.index]
    lim = today - timedelta(days=29)                               # 야후 1분봉은 최근 30일까지
    _, end_today, _ = session_times(today, cfg)
    days = [x for x in days if x >= lim and (x < today or now_et() >= end_today)]
    if ndays:
        days = days[-ndays:]
    dayset = set(days)
    P(f"눌림목 백테스트 | 기간 {days[0]} ~ {days[-1]} ({len(days)}거래일) | 세션 "
      f"{'프리장+' if cfg['sessions'].get('premarket') else ''}{'정규장' if cfg['sessions'].get('regular') else ''}")
    # 1) 전체 보통주 일봉 -> 일중 변동폭 30% 이상인 (종목, 날짜) = 같은 날 저점->고점 30% 상승의 필요조건
    uni = load_universe()
    cpath = os.path.join(HERE, "cache", f"pullback_bt_{days[0]:%Y%m%d}_{days[-1]:%Y%m%d}.pkl")
    cache = pd.read_pickle(cpath) if use_cache and os.path.exists(cpath) else dict(daily={}, raw5={})
    print(f"[1/5] 전체 보통주 {len(uni):,}개 일봉" + (" (캐시)" if cache["daily"] else ""))
    daily = cache["daily"] or download(sorted(uni), period="3mo", interval="1d", quiet=False)
    rng = 1 + cfg["prefilter_day_range_pct"] / 100
    cand = collections.defaultdict(list)
    for tk, df in daily.items():
        dd = np.array([pd.Timestamp(x).date() for x in df.index])
        ok = ((df["High"] / df["Low"]) >= rng) & (df["Low"] > 0)
        for x in dd[ok.to_numpy()]:
            if x in dayset:
                cand[tk].append(x)
    n_td = sum(len(v) for v in cand.values())
    P(f"[검색 범위] 보통주 {len(uni):,}개 중 일봉 확보 {len(daily):,}개 (없음 {len(uni) - len(daily):,}개) → "
      f"일중 변동폭 {cfg['prefilter_day_range_pct']:g}% 이상 {n_td}건 (종목·일), 고유 {len(cand)}종목")
    if cfg["sessions"].get("premarket"):
        P("  ※ 프리장 포함 설정이지만 일봉 고가·저가는 정규장 기준이라 프리장에서만 난 급등은 백테스트 후보에서 빠질 수 있음")
    # 2) 5분봉 (프리·애프터 포함 원본, EMA 는 이전 거래일 포함)
    print(f"[2/5] 후보 {len(cand)}종목 5분봉")
    s5, e5 = days[0] - timedelta(days=25), days[-1] + timedelta(days=1)     # 224 EMA 용 이전 거래일 (5분봉은 60일까지)
    raw5 = {tk: cache["raw5"][tk] for tk in cand if tk in cache["raw5"]}
    miss = sorted(tk for tk in cand if tk not in raw5)
    if miss:
        raw5.update(download(miss, chunk=50, start=str(s5), end=str(e5), interval="5m", prepost=True, quiet=False))
    if use_cache:
        os.makedirs(os.path.dirname(cpath), exist_ok=True)
        pd.to_pickle(dict(daily=daily, raw5={**cache["raw5"], **raw5}), cpath)
    # 3) 패턴
    print("[3/5] 패턴 확인")
    events, skip = [], collections.Counter()
    for tk in sorted(cand):
        raw = raw5.get(tk)
        if raw is None or raw.empty:
            skip["5분봉 없음"] += len(cand[tk])
            continue
        for d in cand[tk]:
            raw_d, why = check_data(raw[raw.index.date <= d], daily.get(tk), d, cfg)
            if why:
                skip["분할 의심 앞부분 잘라냄 (제외 아님)"] += 1
            d5 = prepare_5m(raw_d, cfg)
            day5 = d5[d5.index.date == d]
            day5 = day5[day5.index + bar_td(cfg) <= session_times(d, cfg)[2]]   # 실시간 봇처럼 02:30 이후 봉은 안 봄
            if day5.empty:
                skip["그날 5분봉 없음"] += 1
                continue
            core = Core(cfg, dict(day=str(d)), mode="cons")
            pmf = pm_volume_fn(raw, d, cfg) if cfg["premarket_volume_filter"].get("enabled") else None
            bs = core.process_5m(tk, day5, None, pmf)              # B 는 반환값으로 옴 (실시간은 주문 단계에서 기록)
            core.st["rows"] += [dict(b, result="백테스트") for b in bs]
            core.end_day(day5.index[-1] + bar_td(cfg))
            nt = core.trk[tk].notes
            if nt.get("ema"):
                skip["224 EMA 부족 (하루 전체)" if not nt.get("ema_ok") else "224 EMA 부족 (장 초반 일부 봉)"] += 1
            events += [dict(r, day=str(d)) for r in core.st["rows"]]
    ent = cfg.get("entry_signal", "B")
    B = [e for e in events if e["type"] == ent]                   # 진입 신호 (C 봉 또는 B 봉)
    sig_name = "C 매수 신호" if ent == "C" else "B 신호"
    tp_name = (f"기본 ±{cfg['tp_pct']:g}%" if cfg["tp_pct"] == cfg["sl_pct"]
               else f"기본 +{cfg['tp_pct']:g}/-{cfg['sl_pct']:g}%")
    strats = [("pct", tp_name)] + ([("h1", "비교 익절=H1")] if ent == "B" else [])
    P(f"  5분봉 확인 {n_td - skip['5분봉 없음']}건 | " + (", ".join(f"{k} {v}" for k, v in skip.items()) or ""))
    stage = collections.defaultdict(set)
    for e in events:
        stage[e["type"]].add(e["pid"])
    if ent == "C":
        P(f"[패턴 단계] 눌림 후보 {len(stage['CAND'])} → C 매수 신호 {len(stage['C'])} (C 봉에서 바로 진입, B 추적 안 함)")
    else:
        P(f"[패턴 단계] 눌림 후보 {len(stage['CAND'])} → C 알림 {len(stage['C'])} → H1 {len(stage['H1'])} → "
          f"H2 확정(S 기록) {len(stage['H2'])} → S 재접근 {len(stage['RE'])} → B 신호 {len(stage['B'])}")
    norm = lambda s: re.sub(r"\$?[\d][\d,.]*%?", "#", s)                    # 숫자 빼고 사유별로 묶기
    for title, pids in (("눌림 후보 중 C 전에 끝남", stage["CAND"] - stage["C"]),) + \
            ((("C 알림 이후 B 전에 끝남", stage["C"] - stage["B"]),) if ent == "B" else ()):
        why = collections.Counter(norm(e["why"]) for e in events if e["type"] in ("CANCEL", "EXPIRE") and e["pid"] in pids)
        P(f"   [{title}] " + " / ".join(f"{k}: {v}" for k, v in why.most_common(6)))
    nocand = collections.Counter(norm(e["why"]) for e in events if e["type"] == "NOCAND")
    if nocand:
        P("   [후보 아님] " + " / ".join(f"{k}: {v}" for k, v in nocand.most_common(4)))
    if not B:
        P(f"{sig_name}가 없어 매매가 없습니다 (승률 계산 불가).")
        _save_bt(out_lines, events, [], [])
        return
    # 4) 1분봉 (체결·청산 순서 확인용)
    print(f"[4/5] {sig_name} {len(B)}건 1분봉")
    need = collections.defaultdict(set)
    for e in B:
        need[e["tk"]].add(pd.Timestamp(e["day"]).date())
    m1 = collections.defaultdict(list)
    w = days[0]
    while w <= days[-1]:
        we = w + timedelta(days=7)
        tks = sorted(tk for tk, ds in need.items() if any(w <= x < we for x in ds))
        if tks:
            for tk, df in download(tks, chunk=50, start=str(w), end=str(we), interval="1m", prepost=True).items():
                m1[tk].append(df)
        w = we
    m1 = {tk: pd.concat(v).sort_index() for tk, v in m1.items()}
    m1 = {tk: df[~df.index.duplicated(keep="last")] for tk, df in m1.items()}
    # 5) 공통 표본 비교
    print("[5/5] 매매 시뮬레이션")
    P("")
    P(f"━━ ① 공통 표본: 같은 {sig_name}·같은 진입을 계좌 한도 없이 각각 청산 ━━")
    common = []
    for e in signal_order(B):
        d = pd.Timestamp(e["day"]).date()
        _, _, flat = session_times(d, cfg)
        df1 = _day_minutes(m1[e["tk"]], d, cfg, ts(e["t_end"]), flat) if e["tk"] in m1 else pd.DataFrame()
        res = {(s, m): run_trade(cfg, e, df1, s, m) for s, _ in strats for m in ("cons", "opt")}
        common.append((e, res))
    base_ok = [x for x in common if x[1][("pct", "cons")]["status"] == "ok"]
    if ent == "B":
        both = [x for x in base_ok if x[1][("h1", "cons")]["status"] == "ok"]
        na = sum(1 for x in base_ok if x[1][("h1", "cons")]["status"] == "na")
        P(f"{sig_name} {len(B)}건 중 체결 {len(base_ok)}건 (1분봉 없음 등 미체결 {len(B) - len(base_ok)}건) | "
          f"H1 <= 진입가라 비교 불가 {na}건 | 두 전략 공통 표본 {len(both)}건")
        samples = (("체결된 전체", base_ok), ("공통 표본", both))
    else:
        P(f"{sig_name} {len(B)}건 중 체결 {len(base_ok)}건 (1분봉 없음 등 미체결 {len(B) - len(base_ok)}건) | "
          f"비교 전략(익절=H1)은 C 진입 시점에 H1 이 아직 없어 해당 없음")
        samples = (("체결된 전체", base_ok),)
    hdr = f"{'':16}{'거래':>5}{'승률':>8}{'평균순손익':>11}{'익절':>5}{'손절':>5}{'시간청산':>8}{'불명':>5}"
    for label, sample in samples:
        P(f"[{label}]")
        P(hdr)
        for s, name in strats:
            if s == "h1" and label == "체결된 전체":
                continue
            for m, mn in (("cons", "보수"), ("opt", "낙관")):
                st_ = _stats([x[1][(s, m)] for x in sample if x[1][(s, m)]["status"] == "ok"])
                P(f"{name + ' ' + mn:16}{st_['n']:>5}{st_['win']:>7.1f}%{st_['avg']:>+10.2f}%{st_['tp']:>5}{st_['sl']:>5}"
                  f"{st_['other']:>8}{st_['amb']:>5}")
    # 6) 계좌 단위
    P("")
    P("━━ ② 계좌 단위: 종목당 25%, 최대 4종목, 계좌 ±3% 잠금 적용 ━━")
    by_day = collections.defaultdict(list)
    for e in B:
        by_day[pd.Timestamp(e["day"]).date()].append(e)
    acct_trades = []
    P(f"{'':16}{'거래':>5}{'승률':>8}{'거래당':>9}{'일평균':>9}{'누적':>9}{'최대낙폭':>9}{'한도놓침':>8}{'잠금놓침':>8}"
      f"{'+3%일':>6}{'-3%일':>6}{'미도달':>6}{'불명':>6}")
    daily_rows = []
    for s, name in strats:
        for m, mn in (("cons", "보수"), ("opt", "낙관")):
            eq, curve, rets, trades = cfg["capital"], [cfg["capital"]], [], []
            miss_s = miss_l = up = dn = amb = 0
            for d in days:
                st_, core = run_account_day(cfg, d, by_day.get(d, []), m1, eq, s, m)
                end = core.equity()
                rets.append((end / eq - 1) * 100)
                eq = end
                curve.append(eq)
                trades += st_["trades"]
                miss_s += st_["missed"]["slots"]
                miss_l += st_["missed"]["lock"]
                amb += st_["unknown"]["tpsl"] + st_["unknown"]["acct"]
                if st_["locked"]:
                    up += st_["locked"]["why"].startswith("계좌 +")
                    dn += st_["locked"]["why"].startswith("계좌 -")
                for t in st_["trades"]:
                    acct_trades.append(dict(t, strategy=name, mode=mn))
                if st_["trades"] and s == "pct" and m == "cons":
                    w_ = sum(t["net_pct"] > 0 for t in st_["trades"])
                    daily_rows.append(f"   {d} 거래 {len(st_['trades'])} (이익 {w_}) 계좌 {rets[-1]:+.2f}%"
                                      + (f" [{st_['locked']['why']}]" if st_["locked"] else ""))
            c_ = np.array(curve)
            mdd = ((c_ / np.maximum.accumulate(c_)) - 1).min() * 100
            ss = _stats(trades)
            P(f"{name + ' ' + mn:16}{ss['n']:>5}{ss['win']:>7.1f}%{ss['avg']:>+8.2f}%{np.mean(rets):>+8.2f}%"
              f"{(eq / cfg['capital'] - 1) * 100:>+8.1f}%{mdd:>8.1f}%{miss_s:>8}{miss_l:>8}{up:>6}{dn:>6}{len(days) - up - dn:>6}{amb:>6}")
    if daily_rows:
        P(f"[거래가 있었던 날 ({tp_name} 보수)]")
        for x in daily_rows:
            P(x)
    P("")
    P("※ 보수 = 한 1분봉 안에서 익절·손절 둘 다 닿으면 손절 먼저, 계좌 -3% 는 보유 종목 저가 기준 / "
      "낙관 = 익절 먼저, 계좌 +3% 는 고가 기준. 불명 = 1분봉으로도 순서를 알 수 없는 건수")
    P(f"※ 비용: 매수·매도 각 {cfg['fee_pct']:g}% 수수료 + 시장가성 체결 {cfg['slippage_pct']:g}% 슬리피지 (익절 지정가는 슬리피지 없음)")
    P(f"※ 진입: {ent} 신호봉 마감 {cfg['entry_delay_min']}분 뒤 1분봉 시가. 청산: 익절 +{cfg['tp_pct']:g}% · 손절 -{cfg['sl_pct']:g}%"
      + (f" · 진입 {cfg['max_hold_min']:g}분 뒤" if cfg.get("max_hold_min") else "")
      + f" · {clk(session_times(days[-1], cfg)[2])} 전량 (그 이후 봉은 패턴도 안 봄)")
    P(f"(소요 {time.time() - t0:.0f}초)")
    rows = []
    for e, res in common:
        r = dict(day=e["day"], tk=e["tk"], t=e["t"], type=e["type"], peak=e.get("peak"), pb_low=e.get("pb_low"),
                 h1=e.get("h1"), h2=e.get("h2"), S=e.get("S"), close=e["close"])
        for (s, m), x in res.items():
            r[f"{s}_{m}"] = x.get("net_pct") if x["status"] == "ok" else x["status"]
            r[f"{s}_{m}_why"] = x.get("why")
        rows.append(r)
    _save_bt(out_lines, events, rows, acct_trades)


def _save_bt(lines, events, common_rows, acct_trades):
    os.makedirs(LOGS, exist_ok=True)
    tag = f"{now_et():%Y%m%d}"
    with open(os.path.join(LOGS, f"pullback_backtest_{tag}.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if events:
        pd.DataFrame(events).to_csv(os.path.join(LOGS, f"pullback_backtest_events_{tag}.csv"), index=False, encoding="utf-8-sig")
    if common_rows:
        pd.DataFrame(common_rows).to_csv(os.path.join(LOGS, f"pullback_backtest_common_{tag}.csv"), index=False, encoding="utf-8-sig")
    if acct_trades:
        pd.DataFrame(acct_trades).to_csv(os.path.join(LOGS, f"pullback_backtest_trades_{tag}.csv"), index=False, encoding="utf-8-sig")
    print(f"\n저장: logs/pullback_backtest_{tag}.txt (+ events / common / trades CSV)")


# ═════════════════════ 장전 알림 (한국 22:00) ═════════════════════
def premarket_alert(cfg, top=15):
    """프리마켓 +20% 이상 보통주 = 오늘 정규장 급등·눌림 후보 (전 종목 검색기, 약 20초)"""
    now = now_et()
    d = now.date()
    uni = load_universe()
    quotes, _ = screen_all()
    traded, rows = 0, []
    for x in quotes:
        if epoch_date(x.get("preMarketTime")) != d:
            continue
        traded += 1
        s, pc, px = x.get("symbol"), x.get("preMarketChangePercent"), x.get("preMarketPrice")
        if s in uni and pc is not None and px and pc >= cfg["premarket_alert_pct"]:
            rows.append((s, float(pc), float(px), pd.Timestamp(int(x["preMarketTime"]), unit="s", tz="UTC"), uni[s]))
    rows.sort(key=lambda r: -r[1])
    _, _, flat = session_times(d, cfg)
    head = f"🌅 장전 급등주 · 눌림목 후보 (뉴욕 {now:%H:%M} = 한국 {now.tz_convert(KST_S):%H:%M})"
    if traded == 0:
        return head + "\n오늘은 프리마켓 거래가 없습니다 (미국 휴장일일 수 있음)"
    lines = [head, f"보통주 프리마켓 +{cfg['premarket_alert_pct']:g}% 이상 {len(rows)}개" + (f" (상위 {top})" if len(rows) > top else "")]
    for s, pc, px, t, name in rows[:top]:
        lines.append(f"• {s} {pc:+.1f}% {money(px)} | 최근 체결 {t.tz_convert(ET_S):%H:%M}ET\n   {name}")
    if not rows:
        lines.append("조건에 맞는 종목 없음")
    how = "C자리(눌림 후 첫 반등 봉)에서 매수 신호" if cfg.get("entry_signal", "B") == "C" else "C 알림, B자리에서 매수 신호"
    lines.append(f"정규장 {clk(at(d, cfg['regular_start_et']))}부터 5분봉으로 급등 → 20~40% 눌림 → {how} "
                 f"(청산 {tpsl_text(cfg)} · 진입 {cfg.get('max_hold_min') or '-'}분 뒤 · {clk(flat)} 전량)")
    return "\n".join(lines)


# ═════════════════════ 실행 ═════════════════════
def signal_cfg(cfg):
    """실시간 신호 모드: 칸·현금·계좌 제한 없이 모든 B 신호의 결과(익절·손절·60분·02:30)를 추적해 알림"""
    return dict(cfg, signal_mode=True, max_positions=10 ** 6, position_pct=1.0, capital=1e9,
                account_target_pct=1e9, account_stop_pct=1e9)


def main():
    ap = argparse.ArgumentParser(description="C자리 눌림목 -> B자리 모의매매 / 백테스트")
    ap.add_argument("--paper", action="store_true", help="실시간 모의매매")
    ap.add_argument("--live", action="store_true", help="실시간 신호 (GitHub Actions): C 알림·B 매수 신호·청산 결과를 텔레그램으로")
    ap.add_argument("--premarket", action="store_true", help="장전 급등주 알림 (한국 22:00)")
    ap.add_argument("--scan", action="store_true", help="한 번 검색 (주문·알림 없음)")
    ap.add_argument("--backtest", action="store_true", help="최근 약 30일 백테스트")
    ap.add_argument("--days", type=int, default=None, help="백테스트 거래일 수 (기본: 1분봉 가능한 전체)")
    ap.add_argument("--telegram", action="store_true", help="--scan 결과를 [테스트] 로 텔레그램(+ntfy)에 보냄")
    ap.add_argument("--no-cache", action="store_true", help="백테스트 데이터를 캐시(cache/) 없이 새로 받음")
    ap.add_argument("--config", default=CONFIG_FILE, help="설정 파일 경로")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.live:
        print(f"눌림목 실시간 신호 (한국 {pd.Timestamp.now(tz=KST_S):%Y-%m-%d %H:%M}, 뉴욕 {now_et():%H:%M})")
        LivePaper(signal_cfg(cfg), signal=True).run()
        return
    if args.premarket:                                   # 결과·오류와 상관없이 텔레그램 1개
        try:
            msg = premarket_alert(cfg)
        except Exception as e:
            traceback.print_exc()
            msg = f"⚠️ 장전 급등주 확인 중 오류\n{type(e).__name__}: {e}"
        print(msg)
        if not send_telegram(msg):
            sys.exit(1)
        return
    if args.backtest:
        backtest(cfg, args.days, use_cache=not args.no_cache)
    elif args.scan:
        msg = LivePaper(cfg, dry=True).scan_once()
        if args.telegram:
            sent = notify("[테스트] " + msg, TITLE)
            print(f"보낸 곳: {', '.join(sent) if sent else '없음 (알림설정.json·환경변수 확인)'}")
    else:
        print(f"눌림목 모의매매 (한국 {pd.Timestamp.now(tz=KST_S):%Y-%m-%d %H:%M}, 뉴욕 {now_et():%H:%M}) 중지: Ctrl+C")
        LivePaper(cfg).run()


if __name__ == "__main__":
    main()
