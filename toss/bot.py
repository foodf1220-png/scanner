# -*- coding: utf-8 -*-
"""
bot.py - 토스 자동매수 봇 (FL 신호 → 자동 승인 → 다음 강의 monitor.py 가 주문)

[흐름]
  1. 서버의 FL 모의투자 봇(fl.py)이 매수 신호를 낼 때마다 state/fl_signal_queue.jsonl 에 한 줄 기록
  2. bot.py 가 새 줄을 읽음 (2분 넘게 지난 신호는 무시)
  3. 토스 API: 토큰 → 계좌 → 달러 예수금 (+ 현재가) 조회 (toss_api_docs.md 규격)
  4. 자동 승인 규칙: 동시에 최대 3종목, 종목당 계좌의 30%, 현금 10% 는 항상 남김
     -> 포지션 저장소(store.py: SSM /scanner/positions 또는 서버 파일)에 status APPROVED 로 저장,
        텔레그램에 '달러 예수금: $XXX' 와 [❌ 승인 취소] 버튼
  5. monitor.py(다음 강의)가 APPROVED 를 시장가로 주문하면 PENDING. 5분 안에 주문이 안 들어가면 승인 만료
  6. [❌ 승인 취소]: PENDING 이면 토스 주문도 취소 (이미 체결됐으면 알려 주고 기록 유지)
  - 예수금 조회 실패 -> '예수금 조회 실패, 잠시 후 다시 시도해주세요' + [🔁 다시 시도] [⏭ 패스]
  - 텔레그램에 '상태' -> 승인·주문·보유 종목과 달러 예수금 답장
[키] 코드에 쓰지 않음. 서버 /home/ubuntu/bot.env (systemd EnvironmentFile):
     TOSS_CLIENT_ID, TOSS_CLIENT_SECRET, TELEGRAM_TOKEN(강의용 봇), TELEGRAM_CHAT_ID
     선택: MAX_POSITIONS(3), POSITION_PCT(30), CASH_RESERVE_PCT(10), APPROVAL_TTL_MIN(5)
[실행] python3 bot.py          (bot.service 가 실행)
       python3 bot.py --check  점검만: 환경변수·토스 예수금·텔레그램 봇 확인 (메시지 안 보냄)
표준 라이브러리만 사용
"""
import gzip
import html
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store                                        # 포지션 저장소 (SSM /scanner/positions 또는 서버 파일) - monitor.py 와 공용
from store import clear_symbol, load_positions, positions_lock, save_positions

TOSS_CLIENT_ID     = os.environ["TOSS_CLIENT_ID"]
TOSS_CLIENT_SECRET = os.environ["TOSS_CLIENT_SECRET"]
TG_TOKEN           = os.environ["TELEGRAM_TOKEN"]
TG_CHAT            = str(os.environ["TELEGRAM_CHAT_ID"])
TOSS_BASE = "https://openapi.tossinvest.com"

HERE           = os.path.dirname(os.path.abspath(__file__))
STATE_FILE     = os.path.join(HERE, "bot_state.json")
SIGNAL_QUEUE   = os.environ.get("FL_SIGNAL_QUEUE", "/home/ubuntu/scanner/state/fl_signal_queue.jsonl")
ACCEPT         = {x.strip() for x in os.environ.get("ACCEPT_STRATEGY", "BF").split(",") if x.strip()}   # 실전으로 받을 전략
STRATEGY_NAME  = {"BF": "Bull Flag", "FL": "FL"}
MONITOR_LOG    = os.environ.get("MONITOR_LOG", "/home/ubuntu/monitor.log")

MAX_POSITIONS      = int(os.environ.get("MAX_POSITIONS", "3"))
POSITION_PCT       = float(os.environ.get("POSITION_PCT", "30"))
CASH_RESERVE_PCT   = float(os.environ.get("CASH_RESERVE_PCT", "10"))
APPROVAL_TTL_MIN   = float(os.environ.get("APPROVAL_TTL_MIN", "5"))
SIGNAL_MAX_AGE_SEC = 120          # 이보다 오래된 신호는 자동 승인 안 함 (봇 재시작 때 밀린 신호 등)
RETRY_MAX_AGE_SEC  = 300          # [🔁 다시 시도] 는 신호 후 5분까지만
MIN_BUY_USD        = 1.0
KST, ET = ZoneInfo("Asia/Seoul"), ZoneInfo("America/New_York")


def log(msg):
    print(f"[{datetime.now(KST):%m-%d %H:%M:%S} KST] {msg}", flush=True)


def money(x):
    x = float(x)
    return f"${x:,.2f}" if x >= 1 else f"${x:.4f}"


def ts(s):
    return datetime.fromisoformat(s)


def now_utc():
    return datetime.now(timezone.utc)


def clock(t):
    t = ts(t) if isinstance(t, str) else t
    return f"{t.astimezone(KST):%H:%M} KST ({t.astimezone(ET):%H:%M} ET)"


# ═════════════════════ 텔레그램 ═════════════════════
def tg(method, params, timeout=15):
    data = urllib.parse.urlencode({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                                   for k, v in params.items()}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"ok": False, "error_code": e.code}
    except Exception as e:
        log(f"텔레그램 {method} 실패: {type(e).__name__}")       # 주소에 토큰이 있어 내용은 출력 안 함
        return {"ok": False}


def send_message(chat_id, text, buttons=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": buttons}
    r = tg("sendMessage", p)
    return r.get("result", {}).get("message_id") if r.get("ok") else None


def edit_message(chat_id, message_id, text, buttons=None):
    if message_id is None:
        return send_message(chat_id, text, buttons)
    tg("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": "true", "reply_markup": {"inline_keyboard": buttons or []}})
    return message_id


def answer(cq_id, text=""):
    tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": text})


# ═════════════════════ 토스 API (toss_api_docs.md) ═════════════════════
def _raw(resp):
    """응답 본문. 토스는 오류 응답을 gzip 으로 압축해서 보냄 -> 풀어서 반환"""
    data = resp.read()
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip" or data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError:
            pass
    return data


def _json(resp):
    data = _raw(resp)
    return json.loads(data) if data else {}


def toss_get_token():
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": TOSS_CLIENT_ID,
                                   "client_secret": TOSS_CLIENT_SECRET}).encode()
    req = urllib.request.Request(f"{TOSS_BASE}/oauth2/token", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=10) as r:
        return _json(r)["access_token"]


def toss_get(path, token, account_seq=None):
    req = urllib.request.Request(f"{TOSS_BASE}{path}")
    req.add_header("Authorization", f"Bearer {token}")
    if account_seq is not None:
        req.add_header("X-Tossinvest-Account", str(account_seq))
    with urllib.request.urlopen(req, timeout=10) as r:
        return _json(r)


def toss_delete(path, token, account_seq):
    req = urllib.request.Request(f"{TOSS_BASE}{path}", method="DELETE")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-Tossinvest-Account", str(account_seq))
    with urllib.request.urlopen(req, timeout=10) as r:
        return _json(r)


def get_account_seq(token):
    return toss_get("/api/v1/accounts", token)["result"][0]["accountSeq"]


def get_buying_power(token, account_seq):
    return float(toss_get("/api/v1/buying-power?currency=USD", token, account_seq)["result"]["cashBuyingPower"])


def get_price(token, symbol):
    return float(toss_get(f"/api/v1/prices?symbols={urllib.parse.quote(symbol)}", token)["result"][0]["lastPrice"])


def held_quantity(token, account_seq, symbol):
    for item in toss_get(f"/api/v1/holdings?symbol={urllib.parse.quote(symbol)}", token, account_seq)["result"]["items"]:
        if item.get("symbol") == symbol:
            return float(item.get("quantity") or 0)
    return 0.0


def toss_cancel_order(pos):
    """("cancelled" | "gone" | "error: ...", token). gone = not-found (이미 체결·취소·만료)"""
    token = toss_get_token()
    try:
        toss_delete(f"/api/v1/orders/{pos['order_id']}", token, pos["account_seq"])
        return "cancelled", token
    except urllib.error.HTTPError as e:
        body = _raw(e).decode("utf-8", "replace")
        return ("gone" if "not-found" in body else f"error: HTTP {e.code} {body[:120]}"), token


# ═════════════════════ 자동 승인 ═════════════════════
def account_plan(buying_power, positions):
    """3종목 x 30% + 현금 10%. 토스 문서에 전체 잔고 API 가 없어 계좌 = 예수금 + 봇이 주문·보유 중인 금액"""
    reserved = sum(float(p.get("budget") or 0) for p in positions.values() if p.get("status") == "APPROVED")
    committed = sum(float(p.get("budget") or 0) for p in positions.values() if p.get("status") != "APPROVED")
    total = buying_power + committed
    per_stock = total * POSITION_PCT / 100
    spendable = buying_power - reserved - total * CASH_RESERVE_PCT / 100
    return dict(total=total, reserved=reserved, per_stock=per_stock,
                amount=round(max(0.0, min(per_stock, spendable)), 2))


def signal_text(s):
    name = f" ({html.escape(s['name'][:30])})" if s.get("name") else ""
    strat = STRATEGY_NAME.get(s.get("strategy", "FL"), s.get("strategy", "FL"))
    lines = [f"📈 <b>{strat} 매수 신호</b>", f"종목: <b>{html.escape(s['tk'])}</b>{name}",
             f"신호: {clock(s['t_detect'])} · 신호가 {money(s['px'])}"]
    if s.get("trigger"):
        lines.append(f"매수 기준가 {money(s['trigger'])} 돌파 · 깃대 +{s.get('pole_pct', 0):.1f}% · 되돌림 {s.get('retr', 0):.0f}% · "
                     f"눌림 {s.get('pb_bars', 0)}봉")
    if s.get("SL") and s.get("TP"):
        lines.append(f"손절 {money(s['SL'])} (-{s['sl_pct']:.2f}%) · 익절 {money(s['TP'])} (+{s['tp_pct']:.2f}%) · "
                     f"눌림 저점 {money(s['L'])}")
    lines.append(f"당일 {s.get('chg', 0):+.0f}% · RVOL {s.get('rvol') or 0:.1f}" + (f" · 점수 {s['score']:.0f}" if s.get("score") else ""))
    return "\n".join(lines)


def run_monitor_now():
    """승인 즉시 monitor.py 한 번 실행 (1분 크론을 기다리지 않음). 동시에 돌면 monitor.py 가 알아서 건너뜀"""
    try:
        out = open(MONITOR_LOG, "a")
        subprocess.Popen([sys.executable, os.path.join(HERE, "monitor.py")], stdout=out, stderr=subprocess.STDOUT,
                         cwd=HERE, start_new_session=True)
    except Exception as e:
        log(f"monitor.py 즉시 실행 실패 (1분 크론이 처리): {type(e).__name__}: {e}")


def retry_buttons(sid, label="🔁 다시 시도"):
    return [[{"text": label, "callback_data": f"retry:{sid}"}, {"text": "⏭ 패스", "callback_data": f"pass:{sid}"}]]


def day_lock():
    """monitor.py 가 기록한 오늘 계좌 잠금 (+5% / -3% 도달) 이 있으면 그 정보"""
    try:
        with open(os.path.join(HERE, "account_day.json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d.get("locked") if d.get("date") == f"{datetime.now(ET):%Y-%m-%d}" else None


def auto_approve(state, sid, message_id):
    s = state["signals"][sid]
    tk = s["tk"]
    head = signal_text(s)
    lk = day_lock()
    if lk:
        return edit_message(TG_CHAT, message_id, f"{head}\n\n⏭ {html.escape(lk['why'])} ({lk['ret']:+.2f}%) - 오늘은 신규 매수 안 해요.")
    today = f"{datetime.now(ET):%Y-%m-%d}"
    entered = state.setdefault("entered", {})
    for d in [d for d in entered if d != today]:
        entered.pop(d)
    if tk in entered.get(today, []):
        return edit_message(TG_CHAT, message_id, f"{head}\n\n⏭ 오늘 이미 매매한 종목이라 다시 사지 않아요 (당일 재진입 금지).")
    try:
        positions = load_positions()
    except Exception as e:
        log(f"{tk} 포지션 저장소 읽기 실패: {type(e).__name__}: {e}")
        return edit_message(TG_CHAT, message_id, f"{head}\n\n⚠️ 포지션 저장소({store.STORE}) 조회 실패, 잠시 후 다시 시도해주세요.",
                            retry_buttons(sid))
    if tk in positions:
        return edit_message(TG_CHAT, message_id, f"{head}\n\n⏭ 이미 승인·주문·보유 중인 종목이에요 ({positions[tk].get('status')}).")
    if len(positions) >= MAX_POSITIONS:
        return edit_message(TG_CHAT, message_id,
                            f"{head}\n\n⏭ 자동 매수 안 함 - 동시 보유 한도 {MAX_POSITIONS}종목이 찼어요 ({', '.join(positions)}).",
                            retry_buttons(sid))
    try:
        token = toss_get_token()
        account_seq = get_account_seq(token)
        buying_power = get_buying_power(token, account_seq)
    except Exception as e:
        log(f"{tk} 예수금 조회 실패: {type(e).__name__}: {e}")
        return edit_message(TG_CHAT, message_id, f"{head}\n\n⚠️ 예수금 조회 실패, 잠시 후 다시 시도해주세요.", retry_buttons(sid))
    try:
        price = get_price(token, tk)
    except Exception:
        price = None
    with positions_lock():
        positions = load_positions()                          # 조회하는 사이 바뀌었을 수 있어 다시 확인
        if tk in positions or len(positions) >= MAX_POSITIONS:
            return edit_message(TG_CHAT, message_id, f"{head}\n\n⏭ 방금 다른 신호로 자리가 찼어요.", retry_buttons(sid))
        plan = account_plan(buying_power, positions)
        amount = plan["amount"]
        if amount < MIN_BUY_USD or (price and amount < price):
            why = (f"토스 현재가 {money(price)} 라 ${amount:.2f} 로는 1주도 살 수 없어요 (소수점 주식 불가)."
                   if price and amount >= MIN_BUY_USD else
                   f"현금 {CASH_RESERVE_PCT:g}% 를 남기면 쓸 수 있는 돈이 없어요.")
            return edit_message(TG_CHAT, message_id,
                                f"{head}\n\n⏭ 자동 매수 안 함\n달러 예수금: <b>${buying_power:.2f}</b> · 계좌 추정 "
                                f"${plan['total']:.2f}\n{why}", retry_buttons(sid))
        positions[tk] = dict(symbol=tk, status="APPROVED", chat_id=TG_CHAT, message_id=message_id, budget=amount,
                             signal_low=s.get("L"), stop=s.get("SL"), target=s.get("TP"), signal_price=s.get("px"),
                             signal_id=sid, approved_at=now_utc().isoformat(), source=s.get("strategy", "FL"))
        save_positions(positions)
        n = len(positions)
    entered.setdefault(today, []).append(tk)                      # 당일 재진입 금지 (승인 취소해도 그날은 다시 안 삼)
    log(f"{tk} 자동 승인 ${amount:.2f} (예수금 ${buying_power:.2f}, {n}/{MAX_POSITIONS})")
    run_monitor_now()
    shares = f" → 약 {int(amount // price)}주 (토스 현재가 {money(price)})" if price else ""
    return edit_message(TG_CHAT, message_id,
                        f"{head}\n\n🤖 <b>자동 승인</b> ({n}/{MAX_POSITIONS}종목)\n"
                        f"달러 예수금: <b>${buying_power:.2f}</b> · 계좌 추정 ${plan['total']:.2f}\n"
                        f"매수 금액: <b>${amount:.2f}</b> (계좌의 {amount / plan['total'] * 100:.0f}%){shares}\n"
                        f"주문은 monitor.py 가 시장가로 넣어요 ({APPROVAL_TTL_MIN:g}분 안에 안 들어가면 승인 만료)",
                        [[{"text": "❌ 승인 취소", "callback_data": f"cancel:{tk}"}]])


def on_signal(state, s):
    if s.get("strategy", "FL") not in ACCEPT:                 # 실전은 정한 전략만 (FL 모의 신호는 무시)
        return
    age = (now_utc() - ts(s["t_detect"])).total_seconds()
    if age > SIGNAL_MAX_AGE_SEC:
        log(f"{s['tk']} 신호가 {age:.0f}초 지나서 건너뜀")
        return
    state["seq"] = state.get("seq", 0) + 1
    sid = str(state["seq"])
    state["signals"][sid] = s
    for k in sorted(state["signals"], key=int)[:-50]:          # 최근 50개만 기억
        state["signals"].pop(k)
    if s.get("block"):
        send_message(TG_CHAT, f"{signal_text(s)}\n\n⏭ 건너뜀: {html.escape(str(s['block']))}")
        return
    mid = send_message(TG_CHAT, f"{signal_text(s)}\n\n⏳ 토스 예수금 확인 중…")
    auto_approve(state, sid, mid)


def handle_cancel(cq, symbol):
    mid = cq["message"]["message_id"]
    try:
        pos = load_positions().get(symbol)
    except Exception as e:
        log(f"{symbol} 포지션 저장소 읽기 실패: {e}")
        return answer(cq["id"], "저장소 조회 실패, 잠시 후 다시 눌러주세요")
    if pos and pos.get("status") == "ORDERING":
        return answer(cq["id"], "지금 주문을 넣는 중이에요. 잠시 후 다시 눌러주세요")
    if not pos:
        answer(cq["id"], f"{symbol}: 이미 정리된 종목이에요")
        return edit_message(TG_CHAT, mid, f"↩ <b>{html.escape(symbol)}</b> 이미 정리된 종목이에요.")
    note = ""
    if pos.get("status") == "PENDING" and pos.get("order_id") and pos.get("account_seq"):
        try:
            result, token = toss_cancel_order(pos)
            if result == "gone" and held_quantity(token, pos["account_seq"], symbol) > 0:
                answer(cq["id"], "이미 체결돼서 취소할 수 없어요")
                return edit_message(TG_CHAT, mid, f"⚠️ <b>{html.escape(symbol)}</b> 이미 체결돼서 취소할 수 없어요. (보유 중)")
            if result.startswith("error"):
                raise RuntimeError(result)
            note = "토스 매수 주문도 취소했어요.\n" if result == "cancelled" else "토스 주문은 이미 없어요 (취소·만료).\n"
        except Exception as e:
            log(f"{symbol} 주문 취소 실패: {e}")
            answer(cq["id"], "토스 주문 취소 실패")
            return edit_message(TG_CHAT, mid, f"⚠️ <b>{html.escape(symbol)}</b> 토스 주문 취소 실패 - 토스 앱에서 주문을 확인해주세요.",
                                [[{"text": "❌ 다시 취소", "callback_data": f"cancel:{symbol}"}]])
    elif pos.get("status") not in ("APPROVED", "PENDING"):
        answer(cq["id"], "이미 보유 중이라 승인 취소 대상이 아니에요")
        return
    clear_symbol(symbol)
    answer(cq["id"], "승인 취소")
    sid = pos.get("signal_id")
    log(f"{symbol} 승인 취소 ({pos.get('status')})")
    edit_message(TG_CHAT, mid, f"↩ <b>{html.escape(symbol)}</b> 자동 승인을 취소했어요.\n{note}",
                 retry_buttons(sid, "🔁 다시 매수") if sid else None)


def status_text():
    lines = ["📋 <b>상태</b>"]
    try:
        token = toss_get_token()
        lines.append(f"달러 예수금: <b>${get_buying_power(token, get_account_seq(token)):.2f}</b>")
    except Exception:
        lines.append("달러 예수금: 조회 실패, 잠시 후 다시 시도해주세요")
    try:
        positions = load_positions()
    except Exception:
        return "\n".join(lines + [f"포지션 저장소({store.STORE}) 조회 실패"])
    lines.append(f"종목 {len(positions)}/{MAX_POSITIONS} (종목당 {POSITION_PCT:g}% · 현금 {CASH_RESERVE_PCT:g}% 유지)")
    for tk, p in positions.items():
        lines.append(f"• {html.escape(tk)} {p.get('status')} ${float(p.get('budget') or 0):.2f}")
    return "\n".join(lines)


def handle_update(state, u):
    msg, cq = u.get("message"), u.get("callback_query")
    if msg:
        if str(msg.get("chat", {}).get("id")) != TG_CHAT:
            return
        text = (msg.get("text") or "").strip()
        if text in ("상태", "/상태", "/status"):
            send_message(TG_CHAT, status_text())
        elif text == "/start":
            send_message(TG_CHAT, f"🤖 토스 자동매수 봇입니다. FL 신호가 오면 자동 승인해요 "
                                  f"({MAX_POSITIONS}종목 · 종목당 {POSITION_PCT:g}% · 현금 {CASH_RESERVE_PCT:g}%).\n'상태' 라고 보내면 현황을 알려드려요.")
        return
    if not cq or str(cq.get("message", {}).get("chat", {}).get("id")) != TG_CHAT:
        return
    act, _, arg = str(cq.get("data", "")).partition(":")
    mid = cq["message"]["message_id"]
    if act == "cancel":
        return handle_cancel(cq, arg)
    s = state["signals"].get(arg)
    if not s:
        answer(cq["id"], "오래된 신호예요")
        return edit_message(TG_CHAT, mid, "⏭ 오래된 신호라 처리하지 않아요.")
    if act == "pass":
        answer(cq["id"], "패스")
        return edit_message(TG_CHAT, mid, f"{signal_text(s)}\n\n⏭ 패스했어요.")
    if act == "retry":
        if (now_utc() - ts(s["t_detect"])).total_seconds() > RETRY_MAX_AGE_SEC:
            answer(cq["id"], "신호가 오래돼서 다시 시도하지 않아요")
            return edit_message(TG_CHAT, mid, f"{signal_text(s)}\n\n⏭ 신호가 {RETRY_MAX_AGE_SEC // 60}분 넘게 지나서 다시 시도하지 않아요.")
        answer(cq["id"], "다시 시도")
        auto_approve(state, arg, mid)


def expire_approvals():
    """APPROVED 인 채로 일정 시간이 지나면 (monitor.py 가 주문하지 않음) 승인 만료 -> 자리 비움"""
    expired = []
    with positions_lock():                                    # 저장소 오류면 예외 -> main 이 기록하고 다음에 다시
        positions = load_positions()
        for tk, p in list(positions.items()):
            if p.get("status") == "APPROVED" and p.get("approved_at") and \
                    (now_utc() - ts(p["approved_at"])).total_seconds() > APPROVAL_TTL_MIN * 60:
                expired.append(positions.pop(tk))
        if expired:
            save_positions(positions)
    for p in expired:
        log(f"{p['symbol']} 승인 만료")
        edit_message(TG_CHAT, p.get("message_id"),
                     f"⌛ <b>{html.escape(p['symbol'])}</b> 승인 만료 - {APPROVAL_TTL_MIN:g}분 안에 주문이 들어가지 않았어요 "
                     f"(monitor.py 확인). 자리를 비웠어요.")


# ═════════════════════ 실행 ═════════════════════
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("signals", {})
    return st


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def read_queue(state):
    try:
        size = os.path.getsize(SIGNAL_QUEUE)
    except OSError:
        return
    pos = state.get("queue_pos", size)
    if size < pos:                                           # 파일이 새로 만들어졌으면 처음부터
        pos = 0
    if size == pos:
        state["queue_pos"] = pos
        return
    with open(SIGNAL_QUEUE, "rb") as f:
        f.seek(pos)
        chunk = f.read()
    end = chunk.rfind(b"\n") + 1                              # 쓰는 중인 마지막 줄은 다음에
    state["queue_pos"] = pos + end
    for line in chunk[:end].decode("utf-8", "replace").splitlines():
        if line.strip():
            try:
                on_signal(state, json.loads(line))
            except Exception as e:
                log(f"신호 처리 오류: {type(e).__name__}: {e}")
                traceback.print_exc()


def check():
    print("환경변수:", {k: bool(os.environ.get(k)) for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")})
    try:
        token = toss_get_token()
        print(f"토스: 달러 예수금 ${get_buying_power(token, get_account_seq(token)):.2f}")
    except Exception as e:
        print(f"토스: 실패 {type(e).__name__}: {e}")
    me = tg("getMe", {})
    print("텔레그램 봇:", "@" + me["result"]["username"] if me.get("ok") else f"실패 {me.get('error_code')}")
    print("신호 파일:", SIGNAL_QUEUE, "(있음)" if os.path.exists(SIGNAL_QUEUE) else "(아직 없음 - FL 봇이 첫 신호 때 만듦)")
    try:
        print("포지션 저장소:", store.describe(), f"{len(load_positions())}종목")
    except Exception as e:
        print("포지션 저장소:", store.describe(), f"조회 실패 {type(e).__name__}: {e}")


def main():
    if "--check" in sys.argv:
        return check()
    state = load_state()
    if "queue_pos" not in state:                              # 처음 켤 때 밀린 신호는 건너뜀
        state["queue_pos"] = os.path.getsize(SIGNAL_QUEUE) if os.path.exists(SIGNAL_QUEUE) else 0
    r = tg("getUpdates", {"offset": -1, "timeout": 0})
    res = r.get("result") or []
    offset = res[-1]["update_id"] + 1 if res else 0
    log(f"토스 자동매수 봇 시작 ({MAX_POSITIONS}종목 · {POSITION_PCT:g}% · 현금 {CASH_RESERVE_PCT:g}%) 신호 파일 {SIGNAL_QUEUE}")
    send_message(TG_CHAT, "🤖 토스 자동매수 봇 시작\n" + status_text().replace("📋 <b>상태</b>\n", ""))
    last_expire, warned = 0.0, False
    while True:
        try:
            read_queue(state)
            r = tg("getUpdates", {"offset": offset, "timeout": 3, "allowed_updates": ["message", "callback_query"]}, timeout=15)
            if r.get("ok"):
                for u in r["result"]:
                    offset = u["update_id"] + 1
                    try:
                        handle_update(state, u)
                    except Exception as e:
                        log(f"텔레그램 처리 오류: {type(e).__name__}: {e}")
                        traceback.print_exc()
            elif r.get("error_code") == 409:
                if not warned:
                    log("텔레그램 충돌(409): 같은 봇을 다른 프로그램도 읽고 있어요. 강의용 봇 토큰을 따로 쓰세요.")
                    warned = True
                time.sleep(5)
            else:
                time.sleep(2)
            if time.time() - last_expire > 30:
                expire_approvals()
                last_expire = time.time()
            save_state(state)
        except Exception as e:
            log(f"오류 (5초 뒤 계속): {type(e).__name__}: {e}")
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
