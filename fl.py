# -*- coding: utf-8 -*-
"""
fl.py - Fashionably Late V4.0 미국 급등주 자동매매 (모의투자) · 실시간 신호 · 백테스트

[전략] 1차 급등 → 고점 → 거래량 감소 눌림목(C자리, 상승폭의 20~60%) → 저점 안정 → 9 EMA 상승 전환
       → 9 EMA 가 VWAP 를 아래에서 위로 돌파(B자리) → 매수 → 익절 TP = E + D / 손절 SL = E - D/3 (D = 체결가 - 눌림 저점)
[단계] 1 검색(전 종목 검색기, 3분마다) → 2 눌림목 감시 → 3 모멘텀 회복 감시 → 4 매수 신호 → 5 포지션·계좌 관리
[실행]
  python fl.py --paper         실시간 모의투자 (FL_모의투자.bat) - 감시 화면 logs/fl_monitor.html (10초마다 새로 고침)
  python fl.py --live          실시간 신호만 (계좌 한도 없이 모든 신호와 결과를 텔레그램으로, GitHub Actions 용)
  python fl.py --scan          지금(장 전이면 최근 장) 한 번 검색: 감시 목록·단계·그날 신호 (주문 없음)
  python fl.py --backtest      최근 약 20거래일 백테스트 + 비교(A~H) - 결과 logs/fl_backtest_날짜.txt
  python fl.py --compare-ab    모의 중 기록한 A 모드(실시간 교차)·B 모드(봉 마감) 신호를 같은 청산 규칙으로 비교
  python fl.py --premarket     장전 급등주 알림 (프리마켓 +15% 이상)
  python test_fl.py            검증 테스트
[설정] fl_config.json  [기록] logs/fl_*  [재시작 복구] state/fl_날짜.json  [실전 주문] 없음 (trading_mode=paper 만 동작)
"""
import argparse
import collections
import html
import json
import os
import re
import sys
import time
import traceback
import urllib.request

import numpy as np
import pandas as pd

from fl_core import (ET_S, KST_S, LOGS, MIN, STATE_DIR, Account, RealBroker, at, bar_exit, build_frame, clk,
                     conditions, daily_context, day_windows, early_exit, find_signals, iso, levels, load_config,
                     market_hours, money, now_et, regular_1m, rvol_profile, slip_pct, stage_of, structure, ts)

ACCOUNT_FILE = os.path.join(STATE_DIR, "fl_account.json")
HALT_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"


def _pb():
    import pullback                       # 기존 검증된 데이터 코드 재사용 (다운로드 재시도, 전 종목 검색기)
    return pullback


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


def state_path(d, tag=""):
    return os.path.join(STATE_DIR, f"fl{tag}_{pd.Timestamp(d):%Y%m%d}.json")


def carry_equity(cfg):
    a = load_json(ACCOUNT_FILE)
    return float(a["equity"]) if a and a.get("equity") else float(cfg["capital"])


def fetch_halts():
    """나스닥 거래정지 피드 (NYSE·AMEX 종목 포함, LULD 일시정지 LUDP 포함) -> {종목: 사유}. 실패하면 None"""
    try:
        req = urllib.request.Request(HALT_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    return parse_halts(xml, now_et())


def parse_halts(xml, now):
    """거래정지 피드 XML -> 지금 정지 중인 종목 {종목: 사유} (재개 시각이 지났으면 제외)"""
    out = {}
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        g = lambda tag: (re.search(rf"<ndaq:{tag}>(.*?)</ndaq:{tag}>", item, re.S) or [None, ""])[1].strip()
        sym = g("IssueSymbol")
        if not sym:
            continue
        try:
            halt = pd.Timestamp(f"{g('HaltDate')} {g('HaltTime')[:8]}").tz_localize(ET_S)
        except Exception:
            continue
        rd, rt = g("ResumptionDate"), g("ResumptionTradeTime")
        resume = None
        if rd and rt:
            try:
                resume = pd.Timestamp(f"{rd} {rt[:8]}").tz_localize(ET_S)
            except Exception:
                resume = None
        if halt <= now and (resume is None or resume > now):
            out[sym.replace(".", "-")] = f"{g('ReasonCode')} {halt:%H:%M} 정지" + (f", {resume:%H:%M} 재개 예정" if resume else "")
    return out


def get_spread(tk, quote=None):
    """호가 스프레드 % (매도1 - 매수1) / 중간값. 야후 시세 → 없으면 검색기 시세. 확인 불가면 None"""
    bid = ask = None
    try:
        info = _pb()._yf().Ticker(tk).info
        bid, ask = info.get("bid"), info.get("ask")
    except Exception:
        pass
    if not (bid and ask) and quote:
        bid, ask = quote.get("bid"), quote.get("ask")
    if bid and ask and bid > 0 and ask >= bid:
        return (ask - bid) / ((ask + bid) / 2) * 100
    return None


# ═════════════════════ 텔레그램 버튼 ═════════════════════
def tg_api(method, params, timeout=15):
    """텔레그램 봇 API (urllib). 토큰은 환경변수/알림설정.json 에서만 읽고 출력하지 않음. 실패하면 None"""
    from scanner import _http, telegram_conf
    token, _ = telegram_conf()
    if not token:
        return None
    data = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in params.items()}
    try:
        return json.loads(_http(f"https://api.telegram.org/bot{token}/{method}", data, timeout=timeout))
    except Exception:
        return None


def markup(buttons):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}


def ntfy_send(text, title):
    from scanner import _http, load_alert
    a = load_alert()
    if not a.get("ntfy_topic"):
        return False
    try:
        _http("https://ntfy.sh", json.dumps({"topic": a["ntfy_topic"], "title": title, "message": text}).encode("utf-8"),
              {"Content-Type": "application/json"}, timeout=10)
        return True
    except Exception:
        return False


def signal_cfg(cfg):
    """실시간 신호 모드: 계좌 한도·잠금 없이 모든 신호의 결과를 추적"""
    return dict(cfg, max_positions=10 ** 6, account_lock=False, participation_pct=None)


# ═════════════════════ 실시간 ═════════════════════
class LiveFL:
    def __init__(self, cfg, signal=False, until=None, resume=False):
        self.c, self.sig = (signal_cfg(cfg) if signal else cfg), signal
        self.until, self.resume = until, resume          # GitHub: 1부는 until(뉴욕 시각)에 상태 저장 후 끝, 2부가 이어받음
        self.title = "FL 신호" if signal else "FL 모의"
        self.tag = "_live" if signal else ""
        self.uni, self.quotes, self.cands, self.halted = None, {}, {}, {}
        self.armed, self.view, self.cov = set(), {}, {}
        self.fails, self.block, self.logf = 0, None, None
        self.last_min, self.next_scan, self.next_halt = None, None, None
        self.recent = collections.deque(maxlen=20)
        self.dry = False
        self.tg_warned = False
        self.used = collections.Counter()          # (종목, 매수/매도, 분) -> 이번 1분에 이미 체결한 수량

    def budget(self, tk, bars, now, side):
        """모의 체결 한도: 이번 1분 동안 (직전 완성봉 5개 평균 거래량 x 10%) - 이번 분에 이미 체결한 수량. 한도 없음 = None"""
        pp = self.c.get("participation_pct")
        if not pp:
            return None
        done = bars[bars.index + MIN <= now]
        vol = float(done["Volume"].tail(5).mean()) if len(done) else 0.0
        return max(0, int(vol * pp / 100) - self.used[(tk, side, iso(now.floor("min")))])

    def took(self, tk, side, now, q):
        self.used[(tk, side, iso(now.floor("min")))] += q or 0

    # ── 기록 ──
    def log(self, msg):
        n = now_et()
        line = f"[{n:%H:%M:%S}ET {n.tz_convert(KST_S):%H:%M}KST] {msg}"
        print(line, flush=True)
        self.recent.append(line)
        if self.logf:
            self.logf.write(line + "\n")
            self.logf.flush()

    def say(self, text, buttons=None):
        """알림: ntfy + 텔레그램. buttons = [[(글자, 콜백값), ...], ...] 이면 텔레그램 메시지에 버튼을 붙임"""
        if self.dry:
            return []
        from scanner import notify, telegram_conf
        if not buttons:
            return notify(text, self.title)
        sent = ["ntfy"] if ntfy_send(text, self.title) else []
        _, chat = telegram_conf()
        r = tg_api("sendMessage", {"chat_id": chat, "text": f"[{self.title}] {text}", "reply_markup": markup(buttons)})
        if r and r.get("ok"):
            sent.append("텔레그램")
        else:                                                          # 버튼 전송 실패 -> 버튼 없이라도
            from scanner import send_telegram
            if send_telegram(f"[{self.title}] {text}"):
                sent.append("텔레그램")
        return sent

    # ── 텔레그램 버튼 응답 (모의투자 봇만, 10초마다) ──
    def poll_telegram(self, now):
        if self.sig or self.dry:
            return
        st = self.acct.st
        if st.get("tg_offset") is None:                  # 처음: 지난 대화는 건너뜀
            r = tg_api("getUpdates", {"offset": -1, "timeout": 0})
            res = (r or {}).get("result") or []
            st["tg_offset"] = res[-1]["update_id"] + 1 if res else 0
            return
        r = tg_api("getUpdates", {"offset": st["tg_offset"], "timeout": 0,
                                  "allowed_updates": ["callback_query", "message"]})
        if not r or not r.get("ok"):
            if r and r.get("error_code") == 409 and not self.tg_warned:
                self.tg_warned = True
                self.log("텔레그램 버튼 응답 충돌: 같은 봇을 다른 프로그램이 읽고 있음 (PC 봇과 서버 봇이 같이 켜져 있는지 확인)")
            return
        for u in r.get("result", []):
            st["tg_offset"] = u["update_id"] + 1
            try:
                self.handle_update(u, now)
            except Exception as e:
                self.log(f"텔레그램 응답 처리 오류: {type(e).__name__}: {e}")

    def handle_update(self, u, now):
        """버튼: exit|날짜|종목 = 즉시 전량 청산, half|날짜|종목 = 절반 청산. 메시지 '상태' = 현재 상태 답장
        내 채팅방(알림설정의 chat id)에서 온 것만 처리"""
        from scanner import telegram_conf
        _, chat = telegram_conf()
        st = self.acct.st
        cq, msg = u.get("callback_query"), u.get("message")
        if msg:
            if str(msg.get("chat", {}).get("id")) == str(chat) and msg.get("text", "").strip() in ("상태", "/상태", "/status"):
                held = "\n".join(f"• {tk} {p['qty']:,}주 평균 {money(p['avg'])} → {money(self.acct.px(tk))} "
                                 f"({(self.acct.px(tk) / p['avg'] - 1) * 100:+.2f}%) 손절 {money(p['SL'])} 익절 {money(p['TP'])}"
                                 for tk, p in st["pos"].items())
                tg_api("sendMessage", {"chat_id": chat, "text": f"[{self.title}] 상태\n{self.status_line()}" + (f"\n{held}" if held else "")})
            return
        if not cq or str(cq.get("message", {}).get("chat", {}).get("id")) != str(chat):
            return
        parts = str(cq.get("data", "")).split("|")
        mid = cq.get("message", {}).get("message_id")
        if len(parts) != 3:
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "알 수 없는 버튼"})
            return
        act, day, tk = parts
        p = st["pos"].get(tk) if day == st["day"] else None
        if not p or p["qty"] <= 0:
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": f"{tk}: 이미 청산됐거나 오늘 보유 종목이 아닙니다"})
            tg_api("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid, "reply_markup": {"inline_keyboard": []}})
            return
        for o in self.acct.open_buys():                       # 청산 버튼 = 그 종목 남은 매수 주문도 취소
            if o["tk"] == tk:
                o["status"] = "partial" if o["filled"] else "cancelled"
                o["why"] = "텔레그램 청산 버튼"
                self.log(f"{tk} 남은 매수 주문 취소 (텔레그램 청산 버튼) - 체결 {o['filled']}/{o['qty']}주")
        half = p["qty"] // 2
        if act == "half" and half >= 1 and not p["exit_flag"]:
            p["manual_sell"] = p.get("manual_sell", 0) + half
            note = f"{tk} {half:,}주 절반 청산 주문 (남은 {p['qty'] - half:,}주는 손절·익절 그대로)"
            tg_api("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid,
                                              "reply_markup": markup([[("❌ 나머지 즉시 청산", f"exit|{day}|{tk}")]])})
        else:
            p["exit_flag"] = "수동 청산 (텔레그램 버튼)"
            note = f"{tk} {p['qty']:,}주 전량 청산 주문"
            tg_api("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid, "reply_markup": {"inline_keyboard": []}})
        tg_api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": note})
        self.log(f"텔레그램 버튼 → {note}")

    # ── 데이터 ──
    def fetch_1m(self, tks):
        if not tks:
            return {}
        try:
            data = _pb().download(sorted(tks), period="1d", interval="1m", prepost=False)
        except Exception as e:
            data = {}
            self.log(f"1분봉 요청 오류: {type(e).__name__}: {e}")
        if not data:
            self.fails += 1
            if self.fails >= self.c["data_fail_limit"]:
                self.set_block(f"시세 연결 실패 {self.fails}회 연속")
        else:
            self.fails = 0
            if self.block and self.block.startswith("시세 연결 실패"):
                self.set_block(None)
        return data

    def set_block(self, why):
        if why == self.block:
            return
        old, self.block = self.block, why
        if why:
            self.log(f"⚠️ 신규 매수 중단: {why} (보유 종목은 계속 관리)")
            self.say(f"⚠️ 신규 매수 중단: {why}\n보유 {len(self.acct.st['pos'])}종목 · 손절·익절 감시는 계속")
        else:
            self.log(f"신규 매수 재개 ({old} 해소)")

    def scan(self, now, W):
        """1단계: 전 종목 검색기 -> 장중 고가 ≥ 전일 종가 +15% & 거래량 50만 주 & 고가 $0.5 (필요조건) -> 감시 후보"""
        cfg, pb = self.c, _pb()
        t0 = time.time()
        try:
            quotes, total = pb.screen_all()
        except Exception as e:
            self.log(f"전 종목 검색 실패 ({type(e).__name__}) - 기존 후보 {len(self.cands)}종목으로 계속")
            return
        d = W["open"].date()
        q = {x["symbol"]: x for x in quotes if x.get("symbol")}
        self.quotes = q
        found = []
        for s in self.uni:
            x = q.get(s)
            if not x or pb.epoch_date(x.get("regularMarketTime")) != d:
                continue
            hi, pc, vol = x.get("regularMarketDayHigh"), x.get("regularMarketPreviousClose"), x.get("regularMarketVolume")
            if hi and pc and vol and hi >= pc * (1 + cfg["min_change_pct"] / 100) and vol >= cfg["min_cum_volume"] \
                    and hi >= cfg["min_price"]:
                found.append(s)
        new = sorted(s for s in found if s not in self.cands)
        if new:
            h5 = pb.download(new, period="1mo", interval="5m", prepost=False)
            daily = pb.download(new, period="2mo", interval="1d")
            for s in new:
                dctx = daily_context(daily.get(s), d, cfg)
                pc = q[s].get("regularMarketPreviousClose") or (dctx or {}).get("prev_close")
                if dctx and pc and abs(dctx["prev_close"] / pc - 1) * 100 > 10:
                    k = pc / dctx["prev_close"]          # 일봉 분할 반영 차이 -> 실시간 시세 기준으로
                    for f in ("prev_close", "sma5", "sma10", "resistance"):
                        dctx[f] *= k
                self.cands[s] = dict(pc=pc, prof=rvol_profile(h5.get(s), d, cfg), adv=(dctx or {}).get("adv"),
                                     dctx=dctx, name=self.uni.get(s, ""), added=iso(now))
            self.log(f"감시 후보 추가 {len(new)}: {', '.join(new[:15])}{' …' if len(new) > 15 else ''}")
        self.cov = dict(universe=len(self.uni), covered=sum(1 for s in self.uni if s in q), total=total,
                        found=len(found), watch=len(self.cands), sec=time.time() - t0)

    def watch_list(self, now, W):
        st = self.acct.st
        tks = set(st["pos"]) | {o["tk"] for o in self.acct.open_buys()}
        if now <= W["buy_end"] + MIN:
            tks |= set(self.cands)
        return tks

    def frame(self, tk, bars, W, partial_sec=None):
        info = self.cands.get(tk)
        if not info:
            return None, None
        F = build_frame(bars, W, info["pc"], info["prof"], info["adv"], self.c, partial_sec)
        return F, structure(F)

    def minute(self, now, W):
        """1분봉 마감마다: 감시 종목 전체 -> 지표·단계, B 모드 신호, 보유 종목 조기 청산 판단"""
        cfg, acct, st = self.c, self.acct, self.acct.st
        tks = self.watch_list(now, W)
        data = self.fetch_1m(tks | {"SPY"})
        if not data:
            return
        spy = regular_1m(data.get("SPY"), W)
        if len(spy):
            lag = (now - (spy.index[-1] + MIN)).total_seconds() / 60
            if lag > cfg["stale_data_min"]:
                self.set_block(f"시세 지연 {lag:.0f}분 (SPY 마지막 1분봉 {spy.index[-1]:%H:%M})")
            elif self.block and self.block.startswith("시세 지연"):
                self.set_block(None)
        cut = now - pd.Timedelta(seconds=cfg["bar_grace_sec"])
        armed = set()
        mode = cfg["structure"]
        for tk in sorted(tks):
            bars = regular_1m(data.get(tk), W)
            if bars.empty or tk not in self.cands:
                continue
            done = bars[bars.index + MIN <= cut]
            if done.empty:
                continue
            F, S = self.frame(tk, done, W)
            K, X = conditions(F, S, cfg, mode, self.cands[tk]["dctx"])
            i = len(F) - 1
            px = float(F["Close"].iat[i])
            if tk not in st["pos"]:
                st["prices"][tk] = px
            stg = stage_of(K, X, i, mode)
            self.view[tk] = dict(px=px, chg=F["chg"].iat[i], rvol=F["rvol"].iat[i], cumv=F["cumv"].iat[i],
                                 H=S["H"][i], L=X["L"][i], retr=X["retr"][i], ema=F["ema"].iat[i],
                                 vwap=F["vwap"].iat[i], slope=X["slope"][i], stage=stg, t=F.index[i])
            sigs, _ = find_signals(F, S, cfg, W, tk, mode, self.cands[tk]["dctx"], only_last=True, entry_mode="B")
            fresh = (now - (F.index[i] + MIN)).total_seconds() <= cfg["fresh_signal_sec"]
            for s in sigs:
                self.view[tk]["score"] = s["score"]
                if fresh:
                    self.on_signal(s, now, "B")
            p = st["pos"].get(tk)
            if p and not p["exit_flag"] and p.get("last_ee") != iso(F.index[i]):
                p["ej"] = int(F.index.searchsorted(ts(p["entry_min"])))
                p["last_ee"] = iso(F.index[i])
                if i >= p["ej"]:
                    held = (now - ts(p["entry_t"])).total_seconds() / 60
                    r = early_exit(F, i, p, cfg, held)
                    if r:
                        p["exit_flag"] = r
            if (K["registered"][i] and K["no_new_low"][i] and K["ema_slope"][i] and not K["ema_cross"][i]
                    and (mode == "lod" or (K["surge"][i] and K["retrace"][i]))
                    and px >= F["vwap"].iat[i] * (1 - cfg["armed_vwap_gap_pct"] / 100)):
                armed.add(tk)                         # 교차 직전: 10초마다 실시간 감시 (A 모드)
        self.armed = armed

    def on_signal(self, s, now, kind):
        """kind = 이번 신호의 모드. 설정한 진입 모드면 주문, 아니면 비교용 기록만"""
        cfg, acct, st = self.c, self.acct, self.acct.st
        key = f"{kind}|{s['tk']}|{s['t']}"
        if key in st.setdefault("seen", []):
            return
        st["seen"].append(key)
        if kind != cfg["entry_mode"]:
            if cfg.get("log_other_mode"):
                st.setdefault("shadow", []).append(dict(s, mode=kind, t_detect=iso(now)))
            return
        tk = s["tk"]
        s["name"] = self.cands.get(tk, {}).get("name", "")
        q = self.quotes.get(tk)
        s["spread_pct"] = None if self.dry else get_spread(tk, q)
        s["spread_checked"] = True
        block = None
        if tk in self.halted:
            block = f"거래정지 중 ({self.halted[tk]})"
        elif self.block:
            block = f"신규 매수 중단 ({self.block})"
        ref = st["prices"].get(tk, s["px"])
        if self.sig:
            lv = levels(ref, s["L"], cfg)
            if lv and (cfg["min_stop_pct"] <= lv["sl_pct"] <= cfg["max_stop_pct"]) and not block:
                self.say(f"📈 매수 신호 {tk} @ {money(ref)} ({kind} 모드, {clk(now)})\n"
                         f"손절 {money(lv['SL'])} (-{lv['sl_pct']:.2f}%) · 익절 {money(lv['TP'])} (+{lv['tp_pct']:.2f}%) · "
                         f"눌림 저점 {money(s['L'])}\n당일 {s['chg']:+.0f}% · RVOL {s['rvol'] or 0:.1f} · 점수 {s['score']:.0f}"
                         + (f" · 스프레드 {s['spread_pct']:.2f}%" if s["spread_pct"] is not None else ""))
        acct.submit(s, now, ref, block)

    def hot(self, now, W):
        """10초마다: 보유·미체결(+A 모드 교차 직전) 종목 -> 체결, 계좌 ±4%(우선), 익절·손절, 조기 청산 실행"""
        cfg, acct, st = self.c, self.acct, self.acct.st
        want_a = cfg["entry_mode"] == "A" or cfg.get("log_other_mode")
        tks = set(st["pos"]) | {o["tk"] for o in acct.open_buys()} | (self.armed if want_a and now <= W["buy_end"] else set())
        if not tks:
            return
        data = self.fetch_1m(tks)
        latest = {}
        for tk in tks:
            bars = regular_1m(data.get(tk), W)
            if bars.empty:
                continue
            latest[tk] = bars
            px = float(bars["Close"].iat[-1])
            halted = tk in self.halted
            for o in acct.open_buys():
                if o["tk"] == tk and not halted and now >= ts(o["created"]):
                    self.took(tk, "buy", now, acct.fill_buy(o, px, now, self.budget(tk, bars, now, "buy")))
            p = st["pos"].get(tk)
            if p:
                em = ts(p["entry_min"])
                after = bars[bars.index > em]
                hi = max([px] + list(after["High"]))
                lo = min([px] + list(after["Low"]))
                acct.mark(tk, px, hi, lo)
            else:
                st["prices"][tk] = px
        acct.check_account(now)                                   # 계좌 기준이 개별 종목보다 먼저
        for tk in list(st["pos"]):
            p = st["pos"][tk]
            bars = latest.get(tk)
            if bars is None or tk in self.halted:
                if tk in self.halted:
                    acct.say_once(f"HALT|{tk}|{self.halted[tk]}", f"⏸ {tk} 거래정지 중 ({self.halted[tk]}) - 재개 후 청산 처리")
                continue
            px = float(bars["Close"].iat[-1])
            if p["exit_flag"]:
                self.took(tk, "sell", now, acct.sell(tk, px, now, p["exit_flag"], "market",
                                                     self.budget(tk, bars, now, "sell")))
                continue
            if p.get("manual_sell"):                                   # 텔레그램 '절반 청산' 버튼
                b = self.budget(tk, bars, now, "sell")
                q = acct.sell(tk, px, now, "수동 절반 청산 (텔레그램 버튼)", "market",
                              p["manual_sell"] if b is None else min(b, p["manual_sell"]))
                self.took(tk, "sell", now, q)
                if tk in st["pos"]:
                    p["manual_sell"] -= q or 0
                    if p["manual_sell"] <= 0:
                        p.pop("manual_sell")
                continue
            em = ts(p["entry_min"])
            r = None
            chk = ts(p["chk"]) if p.get("chk") else em
            for t, row in bars[bars.index > chk - MIN].iterrows():
                if t <= em:
                    continue
                r = bar_exit(p, row["Open"], row["High"], row["Low"], t <= ts(p.get("chk") or em), "cons")
                if r:
                    break
            if r is None and (px <= p["SL"] or px >= p["TP"]):
                r = (px, "손절" if px <= p["SL"] else "익절", False, "stop" if px <= p["SL"] else "limit")
            p["chk"] = iso(bars.index[-1])
            if r:
                fill, why, amb, kind = r
                p["amb"] = p["amb"] or amb
                self.took(tk, "sell", now, acct.sell(tk, fill, now, why, kind, self.budget(tk, bars, now, "sell")))
        acct.check_account(now)
        acct.lock_done(now)
        if want_a and now <= W["buy_end"]:
            for tk in sorted(self.armed & set(latest)):
                bars = latest[tk]
                el = (now - bars.index[-1]).total_seconds()
                if el >= 60 or tk in st["pos"]:
                    continue
                F, S = self.frame(tk, bars, W, partial_sec=el)
                if F is None or len(F) < 5:
                    continue
                sigs, _ = find_signals(F, S, cfg, W, tk, cfg["structure"], self.cands[tk]["dctx"], only_last=True,
                                       entry_mode="A")
                for s in sigs:
                    s["t_sig"] = iso(now)
                    self.on_signal(s, now, "A")

    def flatten(self, now, why):
        acct, st = self.acct, self.acct.st
        acct.cancel_buys(now, why)
        if not st["pos"]:
            return
        data = self.fetch_1m(set(st["pos"]))
        for tk in list(st["pos"]):
            df = data.get(tk)
            px = float(df["Close"].dropna().iat[-1]) if df is not None and len(df.dropna(subset=["Close"])) else acct.px(tk)
            acct.sell(tk, px, now, why, "market")

    # ── 화면 ──
    def status_line(self):
        a, st = self.acct, self.acct.st
        held = ", ".join(f"{tk} {(a.px(tk) / p['avg'] - 1) * 100:+.1f}%" for tk, p in st["pos"].items())
        stages = collections.Counter(v["stage"].split(" (")[0] for v in self.view.values())
        return (f"계좌 {a.day_ret():+.2f}% | 보유 {len(st['pos'])}/{self.c['max_positions'] if not self.sig else '-'} [{held or '없음'}]"
                f" | 미체결 {len(a.open_buys())} | 감시 {len(self.cands)} (교차 직전 {len(self.armed)}) | "
                + ", ".join(f"{k} {v}" for k, v in stages.most_common(4))
                + (f" | 잠금: {st['locked']['why']}" if st["locked"] else "") + (f" | 매수 중단: {self.block}" if self.block else ""))

    def monitor(self, now, W):
        if not self.c.get("monitor_html"):
            return
        a, st, cfg = self.acct, self.acct.st, self.c
        e = html.escape
        f2 = lambda x, k=2: "-" if x is None or x != x else f"{x:,.{k}f}"
        state = ("잠금: " + st["locked"]["why"]) if st["locked"] else ("신규 매수 중단: " + self.block) if self.block else \
            ("신규 매수 가능" if W["buy_start"] <= now <= W["buy_end"] else "신규 매수 시간 아님 (보유 관리)")
        acc = [("당일 시작 순자산", f"${a.base():,.2f}"), ("현재 순자산", f"${a.equity():,.2f}"),
               ("계좌 수익률", f"{a.day_ret():+.2f}%"), ("실현 손익", f"${a.realized():+,.2f}"),
               ("미실현 손익", f"${a.unrealized():+,.2f}"), ("가용 현금", f"${st['cash']:,.2f}"),
               ("총 투자 비중", f"{a.exposure() / a.base() * 100:.1f}%"), ("예상 계좌 손실 (모두 손절 시)", f"-{a.expected_loss_pct():.2f}%"),
               ("계좌 익절 / 손절 기준", f"+{cfg['account_target_pct']:g}% / -{cfg['account_stop_pct']:g}%"),
               ("상태", state), ("진입 모드", "A 실시간 교차" if cfg["entry_mode"] == "A" else "B 1분봉 마감 확인")]
        rows = []
        for tk, p in st["pos"].items():
            px = a.px(tk)
            rows.append(f"<tr><td>{e(tk)}</td><td>{money(p['avg'])}</td><td>{money(px)}</td><td>{p['qty']:,}</td>"
                        f"<td>{p['qty'] * p['avg'] / a.base() * 100:.1f}%</td><td>{(px / p['avg'] - 1) * 100:+.2f}%</td>"
                        f"<td>{money(p['SL'])}</td><td>{money(p['TP'])}</td><td>{p['tp_pct'] / p['sl_pct']:.1f}:1</td>"
                        f"<td>-{a._loss_to_stop(p['qty'], px, p['SL']) / a.base() * 100:.2f}%</td>"
                        f"<td>{(now - ts(p['entry_t'])).total_seconds() / 60:.0f}분</td><td>{e(p['exit_flag'] or '-')}</td></tr>")
        order = lambda kv: ("4단계" not in kv[1]["stage"], "3단계" not in kv[1]["stage"], "2단계" not in kv[1]["stage"], kv[0])
        wl = []
        for tk, v in sorted(self.view.items(), key=order):
            wl.append(f"<tr><td>{e(tk)}</td><td>{e(self.cands.get(tk, {}).get('name', '')[:22])}</td><td>{money(v['px'])}</td>"
                      f"<td>{f2(v['chg'], 1)}%</td><td>{f2(v['rvol'], 1)}</td><td>{f2(v['cumv'], 0)}</td>"
                      f"<td>{money(v['H']) if v['H'] == v['H'] else '-'}</td><td>{money(v['L']) if v['L'] == v['L'] else '-'}</td>"
                      f"<td>{f2(v['retr'], 0)}%</td><td>{money(v['ema'])}</td><td>{money(v['vwap'])}</td><td>{f2(v['slope'], 2)}%</td>"
                      f"<td>{e(v['stage'])}{' ⚡' if tk in self.armed else ''}</td><td>{f2(v.get('score'), 0)}</td></tr>")
        logs = "<br>".join(e(x) for x in list(self.recent)[::-1])
        page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<title>FL 모니터</title><style>
body{{font-family:Malgun Gothic,sans-serif;background:#111;color:#ddd;margin:16px;font-size:13px}}
table{{border-collapse:collapse;margin:6px 0 16px}}td,th{{border:1px solid #333;padding:3px 7px;text-align:right}}
th{{background:#222}}td:first-child{{text-align:left;font-weight:bold}}h2{{font-size:15px;margin:10px 0 4px}}
.k td:first-child{{font-weight:normal;color:#aaa}}.log{{font-family:Consolas,monospace;font-size:12px;color:#aaa}}</style></head><body>
<h2>{e(self.title)} · {now:%Y-%m-%d %H:%M:%S} ET ({now.tz_convert(KST_S):%H:%M:%S} KST) · 신규 매수 {clk(W['buy_start'])}~{clk(W['buy_end'])} · 강제 청산 {clk(W['flat'])}</h2>
<table class="k">{''.join(f'<tr><td>{e(k)}</td><td>{e(v)}</td></tr>' for k, v in acc)}</table>
<h2>보유 종목 ({len(st['pos'])})</h2><table><tr><th>종목</th><th>평균 체결가</th><th>현재가</th><th>수량</th><th>비중</th><th>수익률</th>
<th>손절가</th><th>익절가</th><th>손익비</th><th>예상 계좌 손실</th><th>보유</th><th>청산 조건</th></tr>{''.join(rows)}</table>
<h2>감시 종목 ({len(self.view)}) · ⚡ = 교차 직전 실시간 감시</h2><table><tr><th>종목</th><th>이름</th><th>현재가</th><th>당일</th><th>RVOL</th>
<th>누적 거래량</th><th>1차 고점</th><th>눌림 저점</th><th>눌림 비율</th><th>9 EMA</th><th>VWAP</th><th>9EMA 기울기(3봉)</th><th>단계</th><th>점수</th></tr>{''.join(wl)}</table>
<h2>최근 기록</h2><div class="log">{logs}</div></body></html>"""
        path = os.path.join(LOGS, f"fl_monitor{self.tag}.html")
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                f.write(page)
            os.replace(path + ".tmp", path)
        except OSError:
            pass

    # ── 실행 ──
    def run(self):
        cfg = self.c
        now = now_et()
        d = now.date()
        os.makedirs(LOGS, exist_ok=True)
        self.logf = open(os.path.join(LOGS, f"fl{self.tag}_{d:%Y%m%d}.log"), "a", encoding="utf-8")
        if cfg["trading_mode"] != "paper" and not self.sig:
            if not cfg.get("live_trading_enabled"):
                self.log("실전 모드는 fl_config.json 의 live_trading_enabled 를 true 로 직접 켜야 합니다. 종료합니다.")
                return
            try:
                RealBroker(cfg)
            except NotImplementedError as e:
                self.log(str(e))
                return
        W = day_windows(d, cfg)
        if W is None:
            self.log(f"{d} 미국 휴장일(주말 포함)입니다.")
            if now.weekday() < 5:
                self.say(f"📭 {d} 미국 휴장일이라 매매하지 않습니다")
            return
        if now >= W["flat"]:
            self.log(f"오늘 운영 시간이 끝났습니다 (강제 청산 {clk(W['flat'])}).")
            return
        path = state_path(d, self.tag)
        st = load_json(path)
        restarted = st is not None
        if not restarted:
            eq = float(cfg["capital"]) if self.sig else carry_equity(cfg)
            st = dict(day=str(d), start_eq=eq, cash=eq, created=iso(now))

        def save():
            save_json(path, self.acct.st)
            self.write_records(d)
        self.acct = Account(cfg, st, self.log, self.say, on_save=save, signal=self.sig)
        from scanner import load_universe
        self.uni = load_universe()
        rule = (f"손절 = 체결가 - (체결가 - 눌림 저점)/3 · 익절 = 체결가 + (체결가 - 눌림 저점) · 손절폭 {cfg['min_stop_pct']:g}~"
                f"{cfg['max_stop_pct']:g}%만 진입 · 조기 청산 {sum(bool(v) for v in cfg['early_exits'].values())}종 · "
                f"{clk(W['flat'])} 강제 청산")
        self.log(f"FL {'실시간 신호' if self.sig else '모의투자'} {'재시작 - 상태 복구' if restarted else '시작'} | {d} | "
                 f"정규장 {clk(W['open'])}~{clk(W['close'])}{' (조기 폐장)' if W['early'] else ''} | 신규 매수 "
                 f"{clk(W['buy_start'])}~{clk(W['buy_end'])} | 시작 순자산 ${st['start_eq']:,.2f}")
        if restarted and not self.resume:
            a = self.acct
            self.say(f"🔄 재시작: 상태 복구 ({d})\n보유 {len(st['pos'])} / 미체결 {len(a.open_buys())} / 계좌 {a.day_ret():+.2f}%"
                     + (f" / 잠금: {st['locked']['why']}" if st["locked"] else ""))
        save()
        stop_at = at(d, self.until) if self.until else None
        start = W["open"] + MIN + pd.Timedelta(seconds=cfg["bar_grace_sec"])
        if now_et() < start:
            self.log(f"정규장 첫 1분봉 마감 대기 ({clk(start)})")
            while now_et() < start:
                time.sleep(min(30, max(1, (start - now_et()).total_seconds())))
        first, last_status = not restarted, None
        while True:
            now = now_et()
            if stop_at is not None and now >= stop_at and now < W["flat"]:
                save()
                self.log(f"{clk(stop_at)} 1부 종료 - 상태 저장, 2부에서 이어서 감시")
                return
            if now >= W["flat"]:
                try:
                    self.hot(now, W)
                except Exception as e:
                    self.log(f"마지막 시세 오류: {e}")
                self.flatten(now, f"장 마감 {cfg['flatten_before_close_min']:g}분 전 강제 청산"
                             if not cfg.get("force_exit_kst") else f"한국 {cfg['force_exit_kst']} 강제 청산")
                self.acct.lock_done(now)
                save()
                break
            try:
                self.cycle(now, W)
            except Exception as e:
                self.log(f"처리 오류 (다음 주기에 다시): {type(e).__name__}: {e}")
                traceback.print_exc()
            if first and self.cov:
                first = False
                v = self.cov
                self.say(f"📡 FL {'실시간 신호 감시' if self.sig else '모의투자'} 시작 ({d})\n"
                         f"검색: 보통주 {v['universe']:,}개 중 시세 {v['covered']:,}개 → 상승 {cfg['min_change_pct']:g}%·거래량 "
                         f"{cfg['min_cum_volume']:,} 후보 {v['found']}개\n매수: 1차 급등 → 눌림 {cfg['retrace_min_pct']:g}~"
                         f"{cfg['retrace_max_pct']:g}% → 9EMA 가 VWAP 상향 돌파 ({cfg['entry_mode']} 모드) · "
                         f"{clk(W['buy_start'])}~{clk(W['buy_end'])}\n{rule}"
                         + ("" if self.sig else f"\n종목당 {cfg['position_pct']:g}% · 최대 {cfg['max_positions']}종목 · "
                                                f"계좌 ±{cfg['account_target_pct']:g}% 전량 청산 후 종료"))
            save()
            self.monitor(now, W)
            if last_status is None or now - last_status >= pd.Timedelta(minutes=1):
                self.log("상태: " + self.status_line())
                last_status = now
            time.sleep(max(1.0, cfg["poll_sec"] - (now_et() - now).total_seconds()))
        self.monitor(now_et(), W)
        self.finish(d)

    def cycle(self, now, W):
        cfg = self.c
        if cfg.get("halt_feed") and (self.next_halt is None or now >= self.next_halt):
            h = fetch_halts()
            if h is not None:
                for tk in set(h) - set(self.halted):
                    if tk in self.cands or tk in self.acct.st["pos"]:
                        self.log(f"⏸ 거래정지 {tk}: {h[tk]}")
                self.halted = h
            self.next_halt = now + pd.Timedelta(seconds=60)
        if now <= W["buy_end"] and (self.next_scan is None or now >= self.next_scan):
            self.scan(now, W)
            self.next_scan = now + pd.Timedelta(seconds=cfg["scan_sec"])
        bar = (now - pd.Timedelta(seconds=cfg["bar_grace_sec"])).floor("min")
        if bar != self.last_min:
            self.minute(now, W)
            self.last_min = bar
        try:
            self.poll_telegram(now)                                    # 버튼은 바로 다음 시세 확인에서 실행
        except Exception as e:
            self.log(f"텔레그램 확인 오류: {type(e).__name__}")
        self.hot(now, W)
        self.acct.expire_orders(now)

    def write_records(self, d):
        st, day = self.acct.st, f"{pd.Timestamp(d):%Y%m%d}"
        for name, data in (("signals", st["rows"]), ("trades", st["trades"]), ("shadow", st.get("shadow", []))):
            if data:
                pd.DataFrame(data).to_csv(os.path.join(LOGS, f"fl{self.tag}_{name}_{day}.csv"), index=False,
                                          encoding="utf-8-sig")

    def finish(self, d):
        a, st = self.acct, self.acct.st
        T = st["trades"]
        wins = sum(1 for t in T if t["net_pct"] > 0)
        lines = [f"📊 오늘 FL {'신호' if self.sig else '모의'} 결과 ({d})"]
        if self.sig:
            lines.append(f"매수 신호 {len(T)}건 (조건 통과 신호 {st['n_sig']}건)")
        else:
            lines.append(f"거래 {len(T)}건 (이익 {wins}) | 계좌 {a.day_ret():+.2f}% (${a.equity() - a.base():+,.2f}) | "
                         f"실현 ${a.realized():+,.2f}")
            lk = st["locked"]
            lines.append(f"계좌 기준: {lk['why'] + ' → 최종 ' + format(lk.get('final_ret') or a.day_ret(), '+.2f') + '%' if lk else '±' + format(self.c['account_target_pct'], 'g') + '% 미도달'}")
            if st["missed"]:
                lines.append("못 산 신호: " + ", ".join(f"{k} {v}" for k, v in st["missed"].items()))
        for t in T[:15]:
            lines.append(f"• {t['tk']} {clk(t['entry_t'])} {money(t['avg'])} → {money(t['exit_px'])} {t['why']} ({t['net_pct']:+.2f}%)")
        if T:
            lines.append(f"승률 {wins}/{len(T)} ({wins / len(T) * 100:.0f}%) · 평균 {np.mean([t['net_pct'] for t in T]):+.2f}% (비용 반영)")
        sh = st.get("shadow", [])
        if sh:
            lines.append(f"비교용 기록: {', '.join(sorted({x['mode'] for x in sh}))} 모드 신호 {len(sh)}건 (python fl.py --compare-ab)")
        msg = "\n".join(lines)
        self.log(msg.replace("\n", " | "))
        a.say_once(f"END|{d}", msg)
        save_json(state_path(d, self.tag), st)
        self.write_records(d)
        if self.sig:
            return
        eq = a.equity()
        save_json(ACCOUNT_FILE, dict(equity=round(eq, 2), day=str(d)))
        p = os.path.join(LOGS, "fl_daily.csv")
        row = pd.DataFrame([dict(date=str(d), start=round(a.base(), 2), end=round(eq, 2), ret_pct=round(a.day_ret(), 3),
                                 trades=len(T), wins=wins, signals=st["n_sig"],
                                 lock=st["locked"]["why"] if st["locked"] else "",
                                 missed=json.dumps(st["missed"], ensure_ascii=False))])
        old = pd.read_csv(p, encoding="utf-8-sig") if os.path.exists(p) else pd.DataFrame()
        if len(old):
            old = old[old["date"].astype(str) != str(d)]
        pd.concat([old, row]).to_csv(p, index=False, encoding="utf-8-sig")

    # ── 한 번 검색 ──
    def scan_once(self):
        """주문 없이: 최근 장(장중이면 지금까지) 감시 목록·단계·그날 신호"""
        self.dry = True
        cfg = self.c
        pb = _pb()
        spy = pb.download(["SPY"], period="5d", interval="1m", prepost=False).get("SPY")
        d = spy.dropna(subset=["Close"]).index[-1].tz_convert(ET_S).date()
        W = day_windows(d, cfg)
        now = min(now_et(), W["flat"])
        self.acct = Account(cfg, dict(day=str(d), start_eq=cfg["capital"], cash=cfg["capital"]), self.log, None)
        from scanner import load_universe
        self.uni = load_universe()
        self.scan(now, W)
        data = pb.download(sorted(self.cands), period="5d", interval="1m", prepost=False) if self.cands else {}
        sigs = []
        for tk in sorted(self.cands):
            bars = regular_1m(data.get(tk), W)
            bars = bars[bars.index + MIN <= now]
            if bars.empty:
                continue
            F, S = self.frame(tk, bars, W)
            K, X = conditions(F, S, cfg, cfg["structure"], self.cands[tk]["dctx"])
            i = len(F) - 1
            self.view[tk] = dict(px=float(F["Close"].iat[i]), chg=F["chg"].iat[i], rvol=F["rvol"].iat[i],
                                 stage=stage_of(K, X, i, cfg["structure"]))
            s, _ = find_signals(F, S, cfg, W, tk, cfg["structure"], self.cands[tk]["dctx"])
            sigs += s
        v = self.cov
        lines = [f"🔎 FL 한 번 검색 ({d} 미국 장, {clk(now)} 까지)",
                 f"검색 범위: 보통주 {v.get('universe', 0):,}개 중 시세 {v.get('covered', 0):,}개 → 고가 +{cfg['min_change_pct']:g}%·"
                 f"거래량 {cfg['min_cum_volume']:,} 이상 {v.get('found', 0)}개 감시 ({v.get('sec', 0):.0f}초)"]
        stg = collections.Counter(x["stage"].split(" (")[0] for x in self.view.values())
        lines.append("단계: " + (", ".join(f"{k} {n}" for k, n in stg.most_common()) or "-"))
        top = sorted(self.view.items(), key=lambda kv: -(kv[1]["chg"] if kv[1]["chg"] == kv[1]["chg"] else -1e9))[:8]
        lines.append("상승 상위: " + ", ".join(f"{tk} {x['chg']:+.0f}% RVOL {x['rvol'] if x['rvol'] == x['rvol'] else 0:.1f}"
                                            for tk, x in top))
        first = {}
        for s in sigs:
            first.setdefault(s["tk"], s)
        lines.append(f"그날 매수 신호 {len(first)}종목 (B 모드, 종목별 첫 신호):")
        for s in sorted(first.values(), key=lambda s: s["t"]):
            lv = levels(s["px"], s["L"], cfg)
            lines.append(f"• {s['tk']} {clk(ts(s['t']) + MIN)} {money(s['px'])} 눌림 저점 {money(s['L'])} → 손절 "
                         f"{money(lv['SL'])} / 익절 {money(lv['TP'])} (점수 {s['score']:.0f})" if lv else f"• {s['tk']}")
        lines.append("※ 검색만 함: 주문·체결 없음")
        return "\n".join(lines)


# ═════════════════════ 장전 알림 · A/B 비교 ═════════════════════
def premarket_alert(cfg, top=15):
    pb = _pb()
    from scanner import load_universe
    now = now_et()
    d = now.date()
    uni = load_universe()
    quotes, _ = pb.screen_all()
    rows, traded = [], 0
    for x in quotes:
        if pb.epoch_date(x.get("preMarketTime")) != d:
            continue
        traded += 1
        s, pc, px = x.get("symbol"), x.get("preMarketChangePercent"), x.get("preMarketPrice")
        if s in uni and pc is not None and px and pc >= cfg["min_change_pct"] and px >= cfg["min_price"]:
            rows.append((s, float(pc), float(px), uni[s]))
    rows.sort(key=lambda r: -r[1])
    head = f"🌅 장전 급등주 · FL 감시 후보 (뉴욕 {now:%H:%M} = 한국 {now.tz_convert(KST_S):%H:%M})"
    W = day_windows(d, cfg)
    if W is None or traded == 0:
        return head + "\n오늘은 미국 휴장일이거나 프리마켓 거래가 없습니다"
    lines = [head, f"보통주 프리마켓 +{cfg['min_change_pct']:g}% 이상 ${cfg['min_price']:g} 이상 {len(rows)}개"
             + (f" (상위 {top})" if len(rows) > top else "")]
    for s, pc, px, name in rows[:top]:
        lines.append(f"• {s} {pc:+.1f}% {money(px)} | {name}")
    if not rows:
        lines.append("조건에 맞는 종목 없음")
    lines.append(f"정규장에서 1차 급등 → 눌림 {cfg['retrace_min_pct']:g}~{cfg['retrace_max_pct']:g}% → 9EMA 의 VWAP 상향 돌파 시 "
                 f"매수 신호 ({clk(W['buy_start'])}~{clk(W['buy_end'])}, RVOL {cfg['min_rvol']:g}배·거래량 "
                 f"{cfg['min_cum_volume']:,} 이상)")
    return "\n".join(lines)


def tg_test(minutes=10):
    """텔레그램 버튼 시험: 가짜 매수 알림(버튼 포함)을 보내고 minutes 분 동안 버튼 누름에 답함 (매매 없음)"""
    from scanner import telegram_conf
    _, chat = telegram_conf()
    r = tg_api("getUpdates", {"offset": -1, "timeout": 0})
    res = (r or {}).get("result") or []
    off = res[-1]["update_id"] + 1 if res else 0
    m = tg_api("sendMessage", {"chat_id": chat, "reply_markup": markup([[("❌ 즉시 청산", "exit|test|TEST"),
                                                                          ("➖ 절반 청산", "half|test|TEST")]]),
                               "text": "[FL 모의] 🧪 버튼 시험 (실제 매매 아님)\n📈 매수 신호 → 모의 매수\n종목: TEST (시험용)\n"
                                       "체결: 1,000주 @ $2.00 ($2,000, 계좌의 20.0%)\n손절: $1.95 (-2.50%) · 익절: $2.15 (+7.50%)\n"
                                       f"아래 버튼을 눌러 보세요 ({minutes}분 동안 응답)"})
    print("시험 메시지:", "보냄" if m and m.get("ok") else "실패", flush=True)
    end = time.time() + minutes * 60
    while time.time() < end:
        r = tg_api("getUpdates", {"offset": off, "timeout": 20, "allowed_updates": ["callback_query"]}, timeout=35)
        for u in (r or {}).get("result") or []:
            off = u["update_id"] + 1
            cq = u.get("callback_query")
            if not cq or str(cq.get("message", {}).get("chat", {}).get("id")) != str(chat):
                continue
            act = str(cq.get("data", "")).split("|")[0]
            mid = cq["message"]["message_id"]
            if act == "half":
                note = "시험: 절반 청산 버튼 확인 → 실제로는 500주 청산 주문 (남은 500주는 손절·익절 그대로)"
                tg_api("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid,
                                                  "reply_markup": markup([[("❌ 나머지 즉시 청산", "exit|test|TEST")]])})
            else:
                note = "시험: 즉시 청산 버튼 확인 → 실제로는 남은 수량 전량 시장가 청산"
                tg_api("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid, "reply_markup": {"inline_keyboard": []}})
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": note, "show_alert": True})
            print("버튼 눌림:", act, flush=True)
    tg_api("getUpdates", {"offset": off, "timeout": 0})
    print("시험 끝", flush=True)


def compare_ab(cfg):
    """모의 기록(state/fl_*.json)의 A 모드(실시간 교차 감지가)·B 모드(봉 마감 후 다음 봉 시가) 신호를 같은 청산 규칙으로"""
    import fl_backtest as bt
    pb = _pb()
    recs = []
    for f in sorted(os.listdir(STATE_DIR)) if os.path.isdir(STATE_DIR) else []:
        if re.match(r"fl_\d{8}\.json$", f):
            st = load_json(os.path.join(STATE_DIR, f)) or {}
            recs += [dict(r, mode=r.get("mode", "B")) for r in st.get("rows", [])]
            recs += st.get("shadow", [])
    if not recs:
        print("비교할 기록이 없습니다 (모의투자를 돌리면 state/fl_날짜.json 에 쌓입니다).")
        return
    out = collections.defaultdict(list)
    by_day = collections.defaultdict(list)
    for r in recs:
        by_day[r["day"]].append(r)
    for day, rs in sorted(by_day.items()):
        d = pd.Timestamp(day).date()
        W = day_windows(d, cfg)
        tks = sorted({r["tk"] for r in rs})
        m1 = pb.download(tks, start=str(d), end=str(d + pd.Timedelta(days=1)), interval="1m", prepost=False)
        for r in rs:
            bars = regular_1m(m1.get(r["tk"]), W)
            if bars.empty:
                continue
            F = build_frame(bars, W, None, None, None, cfg)
            if r["mode"] == "A":
                t = ts(r["t_detect"])
                j = int(F.index.searchsorted(t.floor("min"))) + 1          # 감지 봉의 나머지는 알 수 없음 -> 다음 봉부터 청산 판단
                E = r["px"] * (1 + slip_pct(r["px"], cfg) / 100)
            else:
                j = int(F.index.searchsorted(ts(r["t"]))) + 1
                if j >= len(F):
                    continue
                E = F["Open"].iat[j] * (1 + slip_pct(F["Open"].iat[j], cfg) / 100)
            tr = simulate_from(cfg, F, W, j, E, r["L"])
            if tr:
                out[r["mode"]].append(dict(tr, entry_px=E, sig_px=r["px"]))
    for m in ("A", "B"):
        T = out.get(m, [])
        print(f"{m} 모드 ({'실시간 교차' if m == 'A' else '봉 마감 확인'}): " + bt.ft(bt.tstats(T)))
        if T:
            print(f"   신호가 대비 진입가 평균 {np.mean([(t['entry_px'] / t['sig_px'] - 1) * 100 for t in T]):+.2f}% (슬리피지 포함)")


def simulate_from(cfg, F, W, j, E, L):
    """봉 j 부터 보유했다고 보고 같은 청산 규칙 적용 (비교용, 전량 체결)"""
    lv = levels(E, L, cfg)
    if lv is None or j >= len(F):
        return None
    p = dict(avg=E, SL=lv["SL"], TP=lv["TP"], R=lv["R"], mfe=E, mae=E, ej=j)
    o, h, l, c = (F[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    fee = cfg["fee_pct"] / 100
    t0 = F.index[j]
    flag = None
    for k in range(j, len(F)):
        t = F.index[k]
        if t >= W["flat"] or flag:
            x = o[k] * (1 - slip_pct(o[k], cfg) / 100)
            why = flag or "강제 청산"
            break
        r = bar_exit(p, o[k], h[k], l[k], k == j)
        if r:
            x = r[0] if r[3] == "limit" else r[0] * (1 - slip_pct(r[0], cfg) / 100)
            why = r[1]
            break
        p["mfe"], p["mae"] = max(p["mfe"], h[k]), min(p["mae"], l[k])
        flag = early_exit(F, k, p, cfg, (t + MIN - t0).total_seconds() / 60)
    else:
        x, why, t = c[-1], "데이터 끝", F.index[-1]
    net = (x * (1 - fee)) / (E * (1 + fee)) - 1
    return dict(entry_t=iso(t0), exit_t=iso(t), why=why, net_pct=net * 100, gross_pct=(x / E - 1) * 100,
                hold_min=(t - t0).total_seconds() / 60, mfe_pct=(p["mfe"] / E - 1) * 100, mae_pct=(p["mae"] / E - 1) * 100,
                mfe_R=(p["mfe"] - E) / lv["R"], mae_R=(p["mae"] - E) / lv["R"])


def main():
    ap = argparse.ArgumentParser(description="Fashionably Late V4.0 모의투자 / 신호 / 백테스트")
    ap.add_argument("--paper", action="store_true", help="실시간 모의투자")
    ap.add_argument("--live", action="store_true", help="실시간 신호 (GitHub Actions)")
    ap.add_argument("--scan", action="store_true", help="한 번 검색 (주문 없음)")
    ap.add_argument("--premarket", action="store_true", help="장전 급등주 알림")
    ap.add_argument("--backtest", action="store_true", help="백테스트")
    ap.add_argument("--compare-ab", action="store_true", help="A/B 모드 기록 비교")
    ap.add_argument("--days", type=int, default=None, help="백테스트 최근 거래일 수")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--telegram", action="store_true", help="--scan 결과를 텔레그램으로")
    ap.add_argument("--config", default=None)
    ap.add_argument("--until", default=None, help="뉴욕 HH:MM 에 상태 저장 후 종료 (GitHub 1부)")
    ap.add_argument("--resume", action="store_true", help="이어받기: 재시작 알림 안 보냄 (GitHub 2부)")
    ap.add_argument("--tg-test", type=int, nargs="?", const=10, default=None, help="텔레그램 버튼 시험 (분)")
    a = ap.parse_args()
    if a.tg_test:
        tg_test(a.tg_test)
        return
    cfg = load_config(a.config) if a.config else load_config()
    if a.backtest:
        import fl_backtest
        fl_backtest.backtest(cfg, a.days, not a.no_cache)
    elif a.compare_ab:
        compare_ab(cfg)
    elif a.premarket:
        from scanner import send_telegram
        msg = premarket_alert(cfg)
        print(msg)
        if not send_telegram(f"[FL 장전] {msg}"):
            sys.exit(1)
    elif a.scan:
        bot = LiveFL(cfg)
        msg = bot.scan_once()
        print(msg)
        if a.telegram:
            bot.dry = False
            bot.title = "FL 테스트 검색"
            print("보낸 곳:", ", ".join(bot.say(msg)) or "없음")
    elif a.paper or a.live:
        LiveFL(cfg, signal=a.live, until=a.until, resume=a.resume).run()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
