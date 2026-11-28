# -*- coding: utf-8 -*-
"""
bf_research.py - 연구판 모의매매 (bf_live.py 안에서 같이 돎, 실제 주문 없음)

실전과 같은 신호(strategy=BF)를 받아 여러 버전을 동시에 가상 매매 (bf_config.json 의 research):
  ① 기준선 = 실전 규칙 (익절 +5 / 손절 -4, 종목 수 제한 없음)
  ② 승률형 = 익절 +3 / 손절 -4
  ③ 재진입 = 그날 같은 종목의 2번째 이후 신호만 (익절 +5 / 손절 -4)
진입: 신호 가격(토스 현재가) + 가격대별 슬리피지
청산: 몇 초마다 현재가 확인 -> 손절·익절가에 닿으면 그 가격에서 슬리피지만큼 낮게 (토스 조건주문 = 닿으면 시장가),
      진입 후 max_hold_min 분, 전량 청산 시각 (bf_live 와 같음)
비용: 수수료 편도 research.fee_pct (토스 0.1%)
같은 버전에서 이미 들고 있는 종목은 다시 안 삼 (실전 봇과 같음)
알림: 🧪 모의매매 텔레그램 - 환경변수 PAPER_TELEGRAM_TOKEN / PAPER_TELEGRAM_CHAT_ID, 없으면 알림설정.json
      (실전 봇 TELEGRAM_TOKEN 은 쓰지 않음 -> 실전 채팅과 섞이지 않음)
기록: state/research_state.json (열린 가상 포지션·오늘 신호 수, 재시작해도 이어감), state/research_trades.jsonl (끝난 거래)
"""
import json
import os
import time
import urllib.parse
import urllib.request

import pandas as pd

import fl_core as FC
from fl_core import iso, round_near

STATE_FILE = os.path.join(FC.STATE_DIR, "research_state.json")
TRADES_FILE = os.path.join(FC.STATE_DIR, "research_trades.jsonl")
ALERT_FILE = os.path.join(FC.HERE, "알림설정.json")
WHY_KO = {"tp": "익절", "sl": "손절", "time": "시간 청산", "flat": "장 마감 청산"}


def paper_telegram_conf():
    a = {}
    try:
        with open(ALERT_FILE, encoding="utf-8") as f:
            a = json.load(f)
    except (OSError, ValueError):
        pass
    return (os.environ.get("PAPER_TELEGRAM_TOKEN") or a.get("telegram_token") or "",
            os.environ.get("PAPER_TELEGRAM_CHAT_ID") or a.get("telegram_chat_id") or "")


def money(x):
    return FC.money(x)


class Research:
    def __init__(self, cfg, log=print, dry=False, state_file=STATE_FILE, trades_file=TRADES_FILE, sender=None):
        self.c = cfg
        r = cfg["research"]
        self.variants = r["variants"]
        self.fee = float(r.get("fee_pct", 0.1)) / 100
        self.tick_sec = float(r.get("tick_sec", 3))
        self.max_hold = float(cfg.get("max_hold_min") or 0)
        self.log, self.dry = log, dry
        self.state_file, self.trades_file = state_file, trades_file
        self.sender = sender
        self.day = None
        self.open, self.nth, self.signals = [], {}, []
        self.last_tick = 0.0
        self.last_px = {}

    # ───────── 상태 저장 ─────────
    def start_day(self, d):
        self.day = str(d)
        st = {}
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, encoding="utf-8") as f:
                    st = json.load(f)
            except (OSError, ValueError):
                st = {}
        if st.get("day") == self.day:
            self.open, self.nth, self.signals = st.get("open", []), st.get("nth", {}), st.get("signals", [])
            if self.open:
                self.log(f"[연구] 재시작 - 가상 포지션 {len(self.open)}개 이어서 추적")
        else:
            if st.get("open"):
                self.log(f"[연구] 지난 거래일 가상 포지션 {len(st['open'])}개는 기록 없이 정리 (가격 확인 불가)")
            self.open, self.nth, self.signals = [], {}, []
            self.save()

    def save(self):
        if self.dry and self.state_file == STATE_FILE:
            return
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(day=self.day, open=self.open, nth=self.nth, signals=self.signals), f, ensure_ascii=False)
        os.replace(tmp, self.state_file)

    def send(self, text):
        self.log(text.replace("\n", " | "))
        if self.sender:
            self.sender(text)
            return
        if self.dry:
            return
        token, chat = paper_telegram_conf()
        if not token or not chat:
            self.log("[연구] 🧪 텔레그램 설정 없음 (알림설정.json) - 알림 생략")
            return
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            with urllib.request.urlopen(req, timeout=10) as r:
                if not json.loads(r.read()).get("ok"):
                    self.log("[연구] 텔레그램 응답 오류")
        except Exception as e:
            self.log(f"[연구] 텔레그램 전송 실패: {type(e).__name__}")

    # ───────── 신호 -> 가상 매수 ─────────
    def on_signal(self, rec, now):
        tk, px = rec["tk"], float(rec["px"])
        n = self.nth.get(tk, 0) + 1
        self.nth[tk] = n
        self.signals.append(dict(tk=tk, t=rec["t"], px=px))
        sb = FC.slip_pct(px, self.c) / 100
        E = px * (1 + sb)
        lines = [f"🧪 연구 신호 · {tk} {money(px)} ({rec.get('px_src') or '?'}) · 오늘 {n}번째 신호"]
        for v in self.variants:
            tag = f"{v['id']} {v['name']}"
            if v.get("only_reentry") and n < 2:
                lines.append(f"{tag}  - 오늘 첫 신호라 안 삼")
                continue
            if any(p["v"] == v["id"] and p["tk"] == tk for p in self.open):
                lines.append(f"{tag}  - 이미 보유 중이라 안 삼")
                continue
            TP, SL = round_near(E * (1 + v["tp_pct"] / 100)), round_near(E * (1 - v["sl_pct"] / 100))
            self.open.append(dict(v=v["id"], name=v["name"], tk=tk, t_in=iso(now), px=px, E=E, TP=TP, SL=SL, nth=n,
                                  src=rec.get("px_src") or ""))
            lines.append(f"{tag}  매수 {money(E)} → 익절 {money(TP)} / 손절 {money(SL)}")
        lines.append("(가상 매매 · 실제 주문 없음)")
        lines.append("")
        lines += self.cum_lines()
        self.save()
        self.send("\n".join(lines))

    # ───────── 청산 확인 ─────────
    def close(self, p, X, why, now):
        self.open.remove(p)
        ret = (X * (1 - self.fee)) / (p["E"] * (1 + self.fee)) - 1
        hold = (pd.Timestamp(now) - pd.Timestamp(p["t_in"])).total_seconds() / 60
        row = dict(day=self.day, v=p["v"], name=p["name"], tk=p["tk"], t_in=p["t_in"], t_out=iso(now), px=p["px"],
                   E=round(p["E"], 6), X=round(X, 6), ret=round(ret * 100, 3), why=why, hold=round(hold, 1), nth=p["nth"])
        if not (self.dry and self.trades_file == TRADES_FILE):
            os.makedirs(os.path.dirname(self.trades_file), exist_ok=True)
            with open(self.trades_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def tick(self, now, W, quote, force=False):
        """열린 가상 포지션 현재가 확인 -> 손절·익절·시간 청산 (force=True 면 지금 바로, 전량 청산 때)"""
        if not self.open:
            return
        if not force and time.time() - self.last_tick < self.tick_sec:
            return
        self.last_tick = time.time()
        q = quote(sorted({p["tk"] for p in self.open}), W)
        for tk, (px, _) in q.items():
            self.last_px[tk] = px
        flat = now >= W["flat"]
        done = []
        for p in list(self.open):
            px = q.get(p["tk"], (None, None))[0]
            if px is None:
                if flat:
                    px = self.last_px.get(p["tk"])
                if px is None:
                    continue
            held = (pd.Timestamp(now) - pd.Timestamp(p["t_in"])).total_seconds() / 60
            slip = lambda x: x * (1 - FC.slip_pct(x, self.c) / 100)
            if px <= p["SL"]:
                done.append(self.close(p, slip(p["SL"]), "sl", now))
            elif px >= p["TP"]:
                done.append(self.close(p, slip(p["TP"]), "tp", now))
            elif flat:
                done.append(self.close(p, slip(px), "flat", now))
            elif self.max_hold and held >= self.max_hold:
                done.append(self.close(p, slip(px), "time", now))
        if done:
            self.save()
            lines = []
            for r in done:
                icon = "🟢" if r["ret"] > 0 else "🔴"
                lines.append(f"🧪 {icon} {r['v']} {r['name']} {WHY_KO[r['why']]} · {r['tk']}  {r['ret']:+.2f}%")
                lines.append(f"   {money(r['E'])} → {money(r['X'])} · 보유 {r['hold']:.0f}분")
            self.send("\n".join(lines))

    # ───────── 장 마감 요약 ─────────
    def history(self):
        rows = []
        if os.path.exists(self.trades_file):
            with open(self.trades_file, encoding="utf-8") as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        return rows

    @staticmethod
    def stat(rows):
        if not rows:
            return "0건"
        w = sum(r["ret"] > 0 for r in rows)
        tot = sum(r["ret"] for r in rows)
        return (f"{len(rows)}건 {w}승 {len(rows) - w}패 (승률 {w / len(rows) * 100:.0f}%) "
                f"평균 {tot / len(rows):+.2f}% · 합계 {tot:+.2f}%")

    def cum_lines(self):
        """전체 누적 수익률 (끝난 거래의 거래별 수익률 합, 버전별) - 신호 알림 맨 끝"""
        hist = self.history()
        if not hist:
            return ["📊 전체 누적: 아직 끝난 거래 없음 (첫 청산부터 표시)"]
        first = min(r["day"] for r in hist)
        lines = [f"📊 전체 누적 ({first[5:].replace('-', '/')}부터 · 끝난 거래 기준)"]
        for v in self.variants:
            a = [r for r in hist if r["v"] == v["id"]]
            if not a:
                lines.append(f"{v['id']} {v['name']}  거래 없음")
                continue
            w = sum(r["ret"] > 0 for r in a)
            lines.append(f"{v['id']} {v['name']}  수익률 {sum(r['ret'] for r in a):+.2f}% · 승률 {w / len(a) * 100:.0f}% "
                         f"({w}승 {len(a) - w}패)")
        return lines

    def day_end(self, now, W, quote):
        self.tick(now, W, quote, force=True)
        for p in list(self.open):                        # 가격을 끝까지 못 받은 것: 진입가로 정리 (따로 표시)
            self.close(p, p["E"], "flat", now)
            self.log(f"[연구] {p['tk']} 마감 가격 확인 불가 - 진입가로 정리")
        self.save()
        hist = self.history()
        today = [r for r in hist if r["day"] == self.day]
        days = sorted({r["day"] for r in hist})
        sig = ", ".join(dict.fromkeys(s["tk"] for s in self.signals))
        lines = [f"🧪 연구판 모의 결과 · {self.day}",
                 f"실전 신호 {len(self.signals)}건" + (f" ({sig})" if sig else " - 오늘은 신호 없음"), ""]
        for v in self.variants:
            t = [r for r in today if r["v"] == v["id"]]
            a = [r for r in hist if r["v"] == v["id"]]
            lines.append(f"{v['id']} {v['name']} (+{v['tp_pct']:g}/-{v['sl_pct']:g}{', 재진입만' if v.get('only_reentry') else ''})")
            lines.append(f"   오늘 {self.stat(t)}")
            lines.append(f"   누적 {len(days)}일 {self.stat(a)}")
        lines.append("")
        lines.append(f"(가상 매매 · 수수료 {self.fee * 100:g}%·슬리피지 반영 · 실제 주문 없음)")
        self.send("\n".join(lines))
