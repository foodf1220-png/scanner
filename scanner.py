#!/usr/bin/env python3
"""
scanner.py - 미국 급등주 단타 실시간 스캐너 + 모의투자

[전략]
  검색   NYSE·NASDAQ·AMEX 보통주, 주가 $2 이상, 최근 2일 +20% 급등 없음, 오늘 +20% 이상
  신호   뉴욕 09:45~11:30 (한국 23:45~01:30, 서머타임 22:45~00:30) 에 마감한 1분봉이 모두 만족
           1) 전일 종가 대비 +20% 이상
           2) 종가 > VWAP
           3) 직전 1분봉 3개의 (최고가-최저가)/최저가 <= 3%
           4) 종가 < 직전 봉 고가
           5) 종가 >= 직전 봉 저가
         -> 다음 봉 시가 매수 (같은 종목은 하루 1번)
  청산   종목: +4% 익절 / -6% 손절
         계좌: 그날 계좌 손익(평가손익 포함)이 +5% 또는 -5% 면 전량 청산 후 그날 종료
         시간: 한국 03:00 (뉴욕 13:00, 서머타임 14:00) 남은 종목 전량 청산
  운용   계좌 5칸 (칸마다 계좌의 1/5), 칸이 비면 다음 신호에 다시 사용

[실행] 한국시간 21:00 이후:  python scanner.py   (또는 모의투자_시작.bat 더블클릭)
       장 시작을 기다렸다가 매매하고, 03:00 또는 할 일이 끝나면 자동 종료 (중지: Ctrl+C)
[기록] logs/  bot_날짜.log, trades_날짜.csv, signals_날짜.csv, daily_summary.csv
[휴대폰 알림] 매수·매도·계좌 종료·하루 결과를 알림설정.json 에 적힌 곳으로 보냄
       ntfy: 휴대폰에 ntfy 앱 설치 -> 알림설정.json 의 ntfy_topic 이름을 구독
       텔레그램: telegram_token 입력 후 봇에게 아무 메시지 보내고  python scanner.py --test-alert
       테스트:  python scanner.py --test-alert
[주의] 모의투자 전용 (실제 주문 없음). yfinance 분봉은 1~2분 늦을 수 있어 진입이 1~2분 늦어짐
"""
import argparse
import collections
import io
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")
ET, KST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Seoul")

CFG = dict(
    kst_start="21:00",        # 운영 시작 (한국시간)
    kst_end="03:00",          # 강제 청산·종료 (한국시간, 다음날)
    entry_start_et="09:45",   # 신규 진입 시작 (뉴욕, 신호 봉 기준)
    entry_end_et="11:30",     # 신규 진입 종료 (뉴욕)
    min_price=2.0,            # 최소 주가 ($)
    surge=0.20,               # 오늘 상승률 기준 (+20%)
    box_bars=3,               # 직전 박스 봉 수 (1분봉)
    box_max=0.03,             # 박스 변동폭 상한 (3%)
    tp=4.0,                   # 종목 익절 %
    sl=6.0,                   # 종목 손절 %
    slots=5,                  # 계좌 분할 수 = 동시 보유 최대 종목 수
    day_target=5.0,           # 계좌 하루 +5% 면 전량 청산 후 종료
    day_stop=5.0,             # 계좌 하루 -5% 면 전량 청산 후 종료
    max_per_stock=1,          # 종목당 하루 최대 매매 횟수
    cost=0.5,                 # 왕복 비용 % (손익 계산에 반영)
    capital=10000.0,          # 모의투자 계좌 금액 ($)
    lag_min=1,                # 야후 분봉 지연 여유 (분)
)

EXCHANGES = ["NMS", "NGM", "NCM", "NYQ", "ASE"]   # 스크리너 거래소 코드: NASDAQ 3개 시장, NYSE, AMEX
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
EXCLUDE_RE = r"\b(?:warrants?|rights?|units?|preferred|depositary|notes?|debentures?)\b|%"


# ═════════════════════ 휴대폰 알림 ═════════════════════
ALERT_FILE = os.path.join(HERE, "알림설정.json")


def load_alert():
    try:
        with open(ALERT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def notify(text, title="급등주 단타"):
    """알림설정.json 의 ntfy / 텔레그램으로 전송 (실패해도 매매는 계속)"""
    a, sent = load_alert(), []
    if a.get("ntfy_topic"):
        try:
            requests.post("https://ntfy.sh", json={"topic": a["ntfy_topic"], "title": title, "message": text},
                          timeout=10).raise_for_status()
            sent.append("ntfy")
        except Exception as e:
            print(f"  ! ntfy 알림 실패: {e}")
    if a.get("telegram_token") and a.get("telegram_chat_id"):
        try:
            requests.post(f"https://api.telegram.org/bot{a['telegram_token']}/sendMessage",
                          data={"chat_id": a["telegram_chat_id"], "text": f"[{title}] {text}"},
                          timeout=10).raise_for_status()
            sent.append("텔레그램")
        except Exception as e:
            print(f"  ! 텔레그램 알림 실패: {e}")
    return sent


def test_alert():
    a = load_alert()
    if a.get("telegram_token") and not a.get("telegram_chat_id"):   # 텔레그램 채팅방 번호 자동 찾기
        try:
            r = requests.get(f"https://api.telegram.org/bot{a['telegram_token']}/getUpdates", timeout=10).json()
            chats = [u["message"]["chat"]["id"] for u in r.get("result", []) if "message" in u]
            if chats:
                a["telegram_chat_id"] = str(chats[-1])
                with open(ALERT_FILE, "w", encoding="utf-8") as f:
                    json.dump(a, f, ensure_ascii=False, indent=2)
                print(f"텔레그램 채팅방 번호 저장: {a['telegram_chat_id']}")
            else:
                print("텔레그램: 먼저 휴대폰에서 만든 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
        except Exception as e:
            print(f"텔레그램 확인 실패: {e}")
    sent = notify("알림 테스트입니다. 이 메시지가 보이면 휴대폰 연동 완료!")
    print(f"보낸 곳: {', '.join(sent) if sent else '없음 (알림설정.json 확인)'}")
    if a.get("ntfy_topic"):
        print(f"ntfy 앱에서 구독할 이름(topic): {a['ntfy_topic']}")


# ═════════════════════ 보통주 목록 ═════════════════════
def _read_pipe(url):
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), sep="|", dtype=str)
    return df[~df.iloc[:, 0].str.startswith("File Creation Time", na=False)]


def load_universe():
    """NYSE·NASDAQ·AMEX 보통주 -> yfinance 표기 종목 코드 집합"""
    nq = _read_pipe(NASDAQ_URL)
    nq = nq[(nq["Test Issue"] == "N") & (nq["ETF"] == "N")][["Symbol", "Security Name"]]
    ot = _read_pipe(OTHER_URL)
    ot = ot[(ot["Test Issue"] == "N") & (ot["ETF"] == "N") & ot["Exchange"].isin(["N", "A"])]
    ot = ot[["ACT Symbol", "Security Name"]].rename(columns={"ACT Symbol": "Symbol"})
    u = pd.concat([nq, ot], ignore_index=True).dropna()
    u = u[u["Security Name"].str.contains("common stock", case=False)]
    u = u[~u["Security Name"].str.contains(EXCLUDE_RE, case=False, regex=True)]
    u = u[~u["Symbol"].str.contains(r"[\$\^=]", regex=True)]
    return set(u["Symbol"].str.replace(".", "-", regex=False))


# ═════════════════════ 시간 ═════════════════════
def _at(tz, hhmm, day):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def session_bounds(d, cfg):
    """뉴욕 날짜 d 의 (진입 시작, 진입 종료, 강제 청산) - 09:30(뉴욕) 기준 분"""
    o = _at(ET, "09:30", d)

    def tod(dt):
        return (dt.astimezone(ET) - o).total_seconds() / 60

    ks, ke = _at(KST, cfg["kst_start"], d), _at(KST, cfg["kst_end"], d)
    if ke <= ks:
        ke += timedelta(days=1)
    t0 = max(tod(_at(ET, cfg["entry_start_et"], d)), tod(ks), 0.0)
    t1 = min(tod(_at(ET, cfg["entry_end_et"], d)), tod(ke))
    return t0, t1, min(tod(ke), 390.0)


def clock(d, tod):
    et = _at(ET, "09:30", d) + timedelta(minutes=float(tod))
    return f"{et:%H:%M}ET({et.astimezone(KST):%H:%M}KST)"


def tod_index(idx):
    return np.asarray(idx.hour * 60 + idx.minute - 570)


def prev_info(closes):
    """오늘 이전 종가들 -> (전일 종가, 최근 2일 +20% 급등 없음 여부)"""
    c = [float(x) for x in closes if x == x]
    if not c:
        return None
    chg = [c[i] / c[i - 1] - 1 for i in range(max(1, len(c) - 2), len(c))]
    return c[-1], all(x < 0.20 for x in chg)


# ═════════════════════ 매매 엔진 ═════════════════════
class TS:
    __slots__ = ("pc", "fresh", "cpv", "cv", "dvol", "box", "last", "t_last")

    def __init__(self, pc, fresh, nbox):
        self.pc, self.fresh = pc, fresh
        self.cpv = self.cv = self.dvol = 0.0
        self.box = collections.deque(maxlen=nbox)   # 직전 봉들 (고가, 저가)
        self.last, self.t_last = None, -1


class Engine:
    def __init__(self, cfg, d, log, alert=None):
        self.c, self.d, self.log = cfg, d, log
        self.alert = alert or (lambda msg: None)
        self.t0, self.t1, self.cut = session_bounds(d, cfg)
        self.st, self.pending, self.pos = {}, {}, {}
        self.cnt, self.last_exit, self.skipped = collections.Counter(), {}, 0
        self.realized, self.stopped = 0.0, None
        self.trades, self.signals = [], []

    def add(self, tk, pc, fresh):
        if tk not in self.st:
            self.st[tk] = TS(pc, fresh, self.c["box_bars"])

    def _bar(self, tk, t, o, h, l, c, v, allow_signal):
        """봉 하나 반영: VWAP·박스 갱신 + 신호 판정"""
        s = self.st[tk]
        s.cpv += (h + l + c) / 3 * v
        s.cv += v
        s.dvol += c * v
        chg = c / s.pc - 1
        sig = False
        if allow_signal and s.fresh and s.cv > 0 and len(s.box) == self.c["box_bars"]:
            bh = max(b[0] for b in s.box)
            bl = min(b[1] for b in s.box)
            ph, pl = s.box[-1]
            sig = (chg >= self.c["surge"] and c >= self.c["min_price"] and c > s.cpv / s.cv
                   and bl > 0 and (bh - bl) / bl <= self.c["box_max"] and pl <= c < ph)
        s.box.append((h, l))
        s.last, s.t_last = c, t
        if tk in self.pos:
            self.pos[tk]["last"] = c
        return sig, chg

    def on_minute(self, t, rows):
        """t: 09:30 기준 분, rows: {종목: (시, 고, 저, 종, 거래량)} - 그 분에 마감된 봉들"""
        if self.stopped:
            return
        for tk in [k for k in self.pos if k in rows]:
            self._exit_check(tk, t, rows[tk])
        for tk, r in rows.items():
            if tk not in self.st:
                continue
            sig, chg = self._bar(tk, t, *r, True)
            if not sig or not (self.t0 <= t < self.t1) or not self._may_signal(tk, t):
                continue
            self.cnt[tk] += 1
            self.pending[tk] = (t, self.st[tk].dvol)
            self.signals.append(dict(date=self.d, ticker=tk, time=clock(self.d, t), close=round(r[3], 4),
                                     chg=round(chg * 100, 1)))
            self.log(f"신호 {tk} {clock(self.d, t)} 종가 ${r[3]:.2f} ({chg * 100:+.1f}%) -> 매수 대기")

    def _may_signal(self, tk, t):
        return (tk not in self.pos and tk not in self.pending
                and self.cnt[tk] < self.c["max_per_stock"] and t > self.last_exit.get(tk, -1e9))

    def silent(self, tk, t, r):
        """늦게 들어온 지난 봉: 지표만 갱신 (보유 중이면 손절·익절 확인)"""
        if tk in self.pos:
            self._exit_check(tk, t, r)
        self._bar(tk, t, *r, False)

    def fill_pending(self, prices, t):
        """대기 중인 매수를 지금 가격으로 체결 (그날 거래대금 큰 종목 먼저)"""
        for tk in sorted(list(self.pending), key=lambda k: -self.pending[k][1]):
            if prices.get(tk):
                self._fill(tk, prices[tk], t)

    def _fill(self, tk, price, t):
        sig_t, _ = self.pending.pop(tk)
        if self.stopped or t + 1 > self.cut:
            return
        if len(self.pos) >= self.c["slots"]:
            self.skipped += 1
            self.log(f"매수 건너뜀 {tk}: 계좌 {self.c['slots']}칸 모두 사용 중")
            return
        e = float(price)
        self.pos[tk] = dict(entry=e, stop=e * (1 - self.c["sl"] / 100), tp=e * (1 + self.c["tp"] / 100),
                            tod=t, sig=sig_t, last=e)
        qty = int(self.c["capital"] / self.c["slots"] // e)
        p = self.pos[tk]
        self.log(f"매수 {tk} {clock(self.d, t)} ${e:.2f} x {qty}주 | 익절 ${p['tp']:.2f} "
                 f"손절 ${p['stop']:.2f} | 보유 {len(self.pos)}/{self.c['slots']}칸")
        self.alert(f"🟢 매수 {tk} ${e:.2f} ({(e / self.st[tk].pc - 1) * 100:+.1f}%) {clock(self.d, t)}\n"
                   f"익절 ${p['tp']:.2f} / 손절 ${p['stop']:.2f} | {qty}주 | 보유 {len(self.pos)}/{self.c['slots']}칸")

    def _exit_check(self, tk, t, r):
        p = self.pos[tk]
        if t < p["tod"]:
            return
        o, h, l = r[0], r[1], r[2]
        if l <= p["stop"]:     # 같은 봉에서 익절·손절 둘 다 닿으면 손절 우선, 갭 하락이면 시가
            self._close(tk, p["stop"] if t == p["tod"] else min(o, p["stop"]), t, "손절")
        elif h >= p["tp"]:
            self._close(tk, p["tp"] if t == p["tod"] else max(o, p["tp"]), t, "익절")

    def _close(self, tk, px, t, why):
        p = self.pos.pop(tk)
        self.last_exit[tk] = t
        gross = (px / p["entry"] - 1) * 100
        net = gross - self.c["cost"]
        self.realized += net / self.c["slots"]
        usd = self.c["capital"] / self.c["slots"] * net / 100
        self.trades.append(dict(date=self.d, ticker=tk, signal=clock(self.d, p["sig"]), entry_time=clock(self.d, p["tod"]),
                                exit_time=clock(self.d, t), entry=round(p["entry"], 4), exit=round(px, 4),
                                gross_pct=round(gross, 3), net_pct=round(net, 3), usd=round(usd, 2), why=why))
        self.log(f"매도 {tk} {clock(self.d, t)} ${px:.2f} {why} {net:+.2f}% (${usd:+.0f}) | 하루 손익 {self.realized:+.2f}%")
        self.alert(f"{'✅' if net > 0 else '🔻'} 매도 {tk} ${px:.2f} {why} {net:+.2f}% (${usd:+.0f})\n"
                   f"{clock(self.d, t)} | 오늘 계좌 {self.realized:+.2f}%")

    def total_pct(self, prices):
        """계좌 하루 손익 % = 실현 + 평가 (비용 반영)"""
        unreal = sum(((prices.get(tk) or p["last"]) / p["entry"] - 1) * 100 - self.c["cost"]
                     for tk, p in self.pos.items()) / self.c["slots"]
        return self.realized + unreal

    def risk(self, t, prices):
        """계좌 +목표 / -한도, 강제 청산 시각 점검"""
        if self.stopped:
            return
        total = self.total_pct(prices)
        if total <= -self.c["day_stop"]:
            self.liquidate(prices, t, "계좌 손실한도")
        elif total >= self.c["day_target"]:
            self.liquidate(prices, t, "계좌 목표달성")
        elif t + 1 >= self.cut:
            self.liquidate(prices, t, "03시 청산")

    def liquidate(self, prices, t, why):
        for tk in list(self.pos):
            self._close(tk, prices.get(tk) or self.pos[tk]["last"], t, why)
        self.pending.clear()
        self.stopped = why
        self.log(f"=== {why}: 전량 정리, 오늘 매매 종료 | 하루 손익 {self.realized:+.2f}% ===")
        if why.startswith("계좌"):
            self.alert(f"⏹ {why}: 전량 정리하고 오늘 매매 종료 | 오늘 계좌 {self.realized:+.2f}%")


# ═════════════════════ 데이터 (yfinance) ═════════════════════
def _split(df, tickers):
    out = {}
    if df is None or df.empty:
        return out
    if not isinstance(df.columns, pd.MultiIndex):
        df = pd.concat({tickers[0]: df}, axis=1)
    lv = set(df.columns.get_level_values(0))
    for t in tickers:
        if t in lv:
            sub = df[t].dropna(subset=["Close"])
            if not sub.empty:
                out[t] = sub
    return out


class LiveFeed:
    def __init__(self):
        import yfinance as yf
        self.yf = yf

    def now(self):
        return datetime.now(ET)

    def sleep_until(self, dt):
        while True:
            rem = (dt - datetime.now(ET)).total_seconds()
            if rem <= 0:
                return
            time.sleep(min(rem, 30))

    def universe(self):
        return load_universe()

    def screen(self):
        """지금 +20% 이상 오른 종목 (NYSE·NASDAQ·AMEX) -> {종목: (현재가, 상승률%)}"""
        yf = self.yf
        q = yf.EquityQuery("and", [yf.EquityQuery("gte", ["percentchange", CFG["surge"] * 100]),
                                   yf.EquityQuery("is-in", ["exchange", *EXCHANGES])])
        try:
            r = yf.screen(q, sortField="percentchange", sortAsc=False, size=250)
        except Exception:
            return None
        return {x["symbol"]: (x.get("regularMarketPrice"), x.get("regularMarketChangePercent"))
                for x in r.get("quotes", []) if x.get("symbol")}

    def daily(self, tickers, d):
        df = self.yf.download(tickers, period="1mo", interval="1d", auto_adjust=True,
                              group_by="ticker", progress=False, threads=True)
        got = _split(df, tickers)
        return {t: (prev_info(got[t]["Close"][[x.date() < d for x in got[t].index]].tolist()) if t in got else None)
                for t in tickers}

    def bars(self, tickers, d):
        if not tickers:
            return {}
        df = self.yf.download(tickers, period="1d", interval="1m", prepost=False, auto_adjust=True,
                              group_by="ticker", progress=False, threads=True)
        return {t: x[x.index.date == d] for t, x in _split(df, tickers).items() if (x.index.date == d).any()}

    def market_open(self, d):
        return bool(self.bars(["SPY"], d))


# ═════════════════════ 실시간 세션 ═════════════════════
def run_session(feed, cfg, tag="", live=True):
    d = feed.now().date()
    os.makedirs(LOGS, exist_ok=True)
    name = d.strftime("%Y%m%d") + tag
    logf = open(os.path.join(LOGS, f"bot_{name}.log"), "a", encoding="utf-8")

    def log(msg):
        n = feed.now()
        line = f"[{n:%H:%M:%S}ET {n.astimezone(KST):%H:%M}KST] {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    t0, t1, cut = session_bounds(d, cfg)
    o = _at(ET, "09:30", d)
    if d.weekday() >= 5:
        log("주말이라 장이 열리지 않습니다.")
        return
    if feed.now() >= o + timedelta(minutes=cut):
        log("오늘 운영 시간(한국 %s 까지)이 지났습니다." % cfg["kst_end"])
        return
    log(f"설정: 진입 {clock(d, t0)}~{clock(d, t1)}, 종목 +{cfg['tp']}% / -{cfg['sl']}%, "
        f"계좌 {cfg['slots']}칸, 계좌 +{cfg['day_target']}% / -{cfg['day_stop']}% 청산, 강제 청산 {clock(d, cut)}")
    universe = feed.universe()
    log(f"보통주 {len(universe)}개")
    alert = notify if live else None
    eng = Engine(cfg, d, log, alert)
    if live:
        sent = notify(f"📡 모의투자 시작 | 진입 한국 {clock(d, t0)[-9:-4]}~{clock(d, t1)[-9:-4]}, "
                      f"종목 +{cfg['tp']}/-{cfg['sl']}%, 계좌 ±{cfg['day_target']}%")
        log(f"휴대폰 알림: {', '.join(sent) if sent else '설정 없음 (알림설정.json)'}")
    known, tracked, prices, floor = {}, set(), {}, 0
    last_t, first = -1, True

    if feed.now() < o + timedelta(minutes=1):
        log(f"장 시작 대기 중 (뉴욕 09:30 = 한국 {o.astimezone(KST):%H:%M})")
        feed.sleep_until(o + timedelta(minutes=1, seconds=8))
    if not feed.market_open(d):
        log("오늘은 휴장일입니다.")
        return

    while True:
        now = feed.now()
        tod = (now - o).total_seconds() / 60
        floor = int(tod)
        if tod >= cut:
            eng.liquidate(prices, floor, "03시 청산")
            break

        # 1) 급등주 검색: 지금 +20% 이상 -> 보통주, 최근 2일 급등 없음, $2 근처 이상이면 감시
        hits = feed.screen()
        if hits is None:
            log("검색 응답 실패 - 다음 분에 다시 시도")
            hits = {}
        new = [s for s in hits if s in universe and s not in known]
        if new:
            for s, k in feed.daily(new, d).items():
                px, pct = hits[s]
                if k and px and pct is not None and abs((px / k[0] - 1) * 100 - pct) > 5:
                    log(f"제외 {s}: 가격 데이터 불일치 (분할 등 의심)")
                    k = None
                known[s] = k
        for s, (px, pct) in hits.items():
            k = known.get(s)
            if k and k[1] and s not in tracked and (px or 0) >= cfg["min_price"] * 0.9:
                tracked.add(s)
                eng.add(s, *k)
                log(f"감시 추가 {s} ${px:.2f} ({pct:+.1f}%)")

        # 2) 1분봉 + 현재가
        T = floor - 1 - cfg["lag_min"]
        want = sorted(tracked | set(eng.pos) | set(eng.pending))
        bars = feed.bars(want, d) if want else {}
        prices = {}
        for s, df in bars.items():
            tx = tod_index(df.index)
            cur, done = df[tx == floor], df[tx < floor]
            if len(cur):
                prices[s] = float(cur["Open"].iloc[0])
            elif len(done):
                prices[s] = float(done["Close"].iloc[-1])
        if first:
            last_t, first = T - 1, False

        # 3) 마감된 봉 처리: 지난 분은 지표만, 새 분은 신호·손익절까지
        by_min = collections.defaultdict(dict)
        for s, df in bars.items():
            if s not in eng.st:
                continue
            for tt, row in zip(tod_index(df.index), df[["Open", "High", "Low", "Close", "Volume"]].itertuples(index=False)):
                tt = int(tt)
                if tt <= eng.st[s].t_last or tt > T:
                    continue
                r = tuple(float(x) for x in row)
                if tt <= last_t:
                    eng.silent(s, tt, r)
                else:
                    by_min[tt][s] = r
        for tt in sorted(by_min):
            eng.on_minute(tt, by_min[tt])
        last_t = max(last_t, T)

        # 4) 매수 체결 + 계좌 ±기준·03시 점검
        eng.fill_pending(prices, floor)
        eng.risk(floor, prices)
        if eng.stopped:
            break
        if tod > t1 + 2 and not eng.pos and not eng.pending:
            log("진입 시간이 끝났고 보유 종목이 없어 오늘 매매를 마칩니다.")
            break
        if floor % 5 == 0:
            held = ", ".join(f"{s} {(prices.get(s, p['last']) / p['entry'] - 1) * 100:+.1f}%" for s, p in eng.pos.items())
            log(f"상태: 감시 {len(tracked)}종목, 보유 {len(eng.pos)}/{cfg['slots']}칸 [{held or '없음'}], "
                f"계좌 손익 {eng.total_pct(prices):+.2f}% (실현 {eng.realized:+.2f}%)")
        feed.sleep_until(o + timedelta(minutes=floor + 1, seconds=8))

    if eng.pos:
        eng.liquidate(prices, floor, "종료")
    pd.DataFrame(eng.trades).to_csv(os.path.join(LOGS, f"trades_{name}.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(eng.signals).to_csv(os.path.join(LOGS, f"signals_{name}.csv"), index=False, encoding="utf-8-sig")
    wins = sum(t["net_pct"] > 0 for t in eng.trades)
    if live:
        sp = os.path.join(LOGS, "daily_summary.csv")
        pd.DataFrame([dict(date=d, trades=len(eng.trades), wins=wins, day_pct=round(eng.realized, 3),
                           day_usd=round(eng.realized * cfg["capital"] / 100, 2), end=eng.stopped or "종료")]
                     ).to_csv(sp, mode="a", header=not os.path.exists(sp), index=False, encoding="utf-8-sig")
    log(f"오늘 결과: 매매 {len(eng.trades)}회 (수익 {wins}회), 하루 손익 {eng.realized:+.2f}% "
        f"(${eng.realized * cfg['capital'] / 100:+,.0f}) | 기록: {LOGS}")
    if live:
        notify(f"📊 오늘 결과: 매매 {len(eng.trades)}회 (수익 {wins}회)\n"
               f"계좌 {eng.realized:+.2f}% (${eng.realized * cfg['capital'] / 100:+,.0f}) | {eng.stopped or '종료'}")
    logf.close()
    return eng


def main():
    ap = argparse.ArgumentParser(description="미국 급등주 단타 실시간 스캐너 + 모의투자")
    ap.add_argument("--capital", type=float, default=CFG["capital"], help="모의투자 계좌 금액 ($)")
    ap.add_argument("--test-alert", action="store_true", help="휴대폰 알림 테스트만 보내고 종료")
    args = ap.parse_args()
    if args.test_alert:
        test_alert()
        return
    cfg = {**CFG, "capital": args.capital}
    now = datetime.now(KST)
    print(f"현재 한국시간 {now:%Y-%m-%d %H:%M}, 뉴욕 {now.astimezone(ET):%H:%M}. 모의투자 시작 (중지: Ctrl+C)")
    run_session(LiveFeed(), cfg)


if __name__ == "__main__":
    main()
