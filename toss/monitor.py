# -*- coding: utf-8 -*-
"""
monitor.py - 승인된 종목 주문·체결 확인 (크론으로 1분마다). 5강 강의 코드 기준 + 안전장치

포지션 저장소: store.py (강의대로 SSM /scanner/positions, 또는 서버 파일 - bot.env 의 POSITIONS_STORE)
  · 포지션이 없으면 바로 종료
  · APPROVED: 미국 정규장(뉴욕 09:30~16:00, 서머타임·휴장일·조기 폐장 반영)이 아니면 '매수 대기 중' 출력만
      장중이면 토스 달러 예수금·현재가 조회 -> min(승인 금액, 예수금) 으로 살 수 있는 정수 수량
      1주도 못 사면 '매수 불가' 텔레그램 + 저장소에서 삭제
      살 수 있으면 시장가 매수 -> PENDING(+order_id) 저장 -> '주문 접수' 텔레그램
  · PENDING: 토스 보유 내역으로 체결 확인 -> OPEN(+매수가·수량) 저장 + '체결 완료' 텔레그램. 아직이면 다음 실행 때
  · OPEN: 손절·익절은 다음 강의
[실제 주문 스위치] LIVE_ORDERS=true (bot.env) 일 때만 토스에 주문. 기본은 시험 모드 = 수량 계산·알림만 하고 승인 정리
[안전장치 (강의 코드에 추가)]
  · 주문 직전 상태를 ORDERING 으로 바꿔 둠 -> 주문 후 저장 전에 꺼져도 다음 실행이 같은 종목을 또 사지 않음
  · 주문 전 그 종목 기존 보유 수량을 기록 -> 원래 갖고 있던 주식을 체결로 착각하지 않음
  · 주문 실패·매수 불가는 알림 1번 후 정리 (1분마다 같은 실패 알림 반복 안 함)
  · 이전 실행이 아직 돌고 있으면 이번 실행은 건너뜀
키: 서버 /home/ubuntu/bot.env (TOSS_CLIENT_ID, TOSS_CLIENT_SECRET, TELEGRAM_TOKEN) - 크론은 run_with_env.sh 로 실행
"""
import html
import json
import os
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:
    fcntl = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
from store import clear_symbol, load_positions, update_position

TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
TOSS_CLIENT_ID     = os.environ["TOSS_CLIENT_ID"]
TOSS_CLIENT_SECRET = os.environ["TOSS_CLIENT_SECRET"]
LIVE_ORDERS        = os.environ.get("LIVE_ORDERS", "false").strip().lower() in ("1", "true", "yes")
PENDING_ALERT_MIN  = 15            # 주문 후 이 시간이 지나도 체결 확인이 안 되면 1번 알림

TOSS_BASE = "https://openapi.tossinvest.com"
ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))

NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
    "2027-11-25", "2027-12-24",
}
NYSE_EARLY_CLOSE = {"2026-11-27": (13, 0), "2026-12-24": (13, 0), "2027-11-26": (13, 0)}


def log(msg):
    print(f"[{datetime.now(ET):%m-%d %H:%M:%S} ET] {msg}", flush=True)


def is_market_open(now=None):
    """미국 정규장: 뉴욕 09:30~16:00 (서머타임 자동), 주말·휴장일 제외, 조기 폐장일은 13:00 까지"""
    now = now or datetime.now(ET)
    day = f"{now:%Y-%m-%d}"
    if now.weekday() >= 5 or day in NYSE_HOLIDAYS:
        return False
    ch, cm = NYSE_EARLY_CLOSE.get(day, (16, 0))
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return open_t <= now < close_t


def send_message(chat_id, text):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data),
                               timeout=10)
    except Exception as e:
        log(f"텔레그램 오류: {type(e).__name__}")


# ── Toss API (toss_api_docs.md) ──────────────────────────────────────────────

def _read_error(e):
    body = e.read().decode("utf-8", errors="replace")
    try:
        err = json.loads(body).get("error", {})
        return err.get("code", ""), err.get("message", body[:200])
    except ValueError:
        return "", body[:200]


def toss_get_token():
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": TOSS_CLIENT_ID,
                                   "client_secret": TOSS_CLIENT_SECRET}).encode()
    req = urllib.request.Request(f"{TOSS_BASE}/oauth2/token", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["access_token"]


def toss_get(path, token, account_seq=None):
    req = urllib.request.Request(f"{TOSS_BASE}{path}")
    req.add_header("Authorization", f"Bearer {token}")
    if account_seq is not None:
        req.add_header("X-Tossinvest-Account", str(account_seq))
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def toss_post(path, token, account_seq, payload):
    req = urllib.request.Request(f"{TOSS_BASE}{path}", data=json.dumps(payload).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Tossinvest-Account", str(account_seq))
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_account_seq(token):
    return toss_get("/api/v1/accounts", token)["result"][0]["accountSeq"]


def get_available_usd(token, account_seq):
    return float(toss_get("/api/v1/buying-power?currency=USD", token, account_seq)["result"]["cashBuyingPower"])


def get_current_price(token, symbol):
    return float(toss_get(f"/api/v1/prices?symbols={urllib.parse.quote(symbol)}", token)["result"][0]["lastPrice"])


def get_holding(token, account_seq, symbol):
    """(보유 수량, 평균 매수가). 목록에 없으면 (0, 0). 조회 실패면 (None, None)"""
    try:
        items = toss_get(f"/api/v1/holdings?symbol={urllib.parse.quote(symbol)}", token, account_seq) \
            .get("result", {}).get("items", [])
        for h in items:
            if h.get("symbol") == symbol:
                return float(h.get("quantity") or 0), float(h.get("averagePurchasePrice") or 0)
        return 0.0, 0.0
    except Exception as e:
        log(f"{symbol} 보유 내역 조회 오류 (다음에 다시): {type(e).__name__}: {e}")
        return None, None


def place_market_buy(token, account_seq, symbol, quantity):
    return toss_post("/api/v1/orders", token, account_seq,
                     {"symbol": symbol, "side": "BUY", "orderType": "MARKET", "quantity": str(int(quantity))})


# ── 상태별 처리 ──────────────────────────────────────────────────────────────

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def b(s):
    return f"<b>{html.escape(str(s))}</b>"


def handle_approved(token, symbol, pos):
    if not is_market_open():
        log(f"{symbol} 매수 대기 중 (미국 정규장 시간 아님)")
        return
    chat = pos["chat_id"]
    account_seq = get_account_seq(token)
    requested = float(pos.get("budget") or 0)
    available = get_available_usd(token, account_seq)
    budget = min(requested, available)
    price = get_current_price(token, symbol)
    qty = int(budget // price) if price > 0 else 0
    log(f"{symbol} 승인 ${requested:.2f} / 예수금 ${available:.2f} / 현재가 ${price:.2f} → {qty}주")

    if qty < 1:
        why = (f"달러 예수금 ${available:.2f} 부족 - 토스 앱에서 달러로 환전해주세요." if available < requested and available < price
               else f"${budget:.2f} 로는 1주(${price:.2f})를 살 수 없어요 (소수점 주식 불가).")
        send_message(chat, f"⚠️ {b(symbol)} 매수 불가\n\n{why}")
        clear_symbol(symbol)
        return

    if not LIVE_ORDERS:
        send_message(chat, f"🧪 {b(symbol)} 시험 모드 - 실제 주문 안 함\n\n"
                           f"주문했다면: {qty}주 × ${price:.2f} = ${qty * price:.2f}\n"
                           f"(승인 ${requested:.2f} · 달러 예수금 ${available:.2f})\n"
                           f"실제 주문은 청산(손절·익절)을 붙인 뒤 LIVE_ORDERS=true 로 켭니다. 승인은 정리했어요.")
        clear_symbol(symbol)
        return

    base_qty, base_avg = get_holding(token, account_seq, symbol)
    if base_qty is None:
        return                                                    # 보유 내역을 못 읽으면 다음 실행 때 (주문 안 함)
    if not update_position(symbol, expect="APPROVED", status="ORDERING", ordering_at=now_iso()):
        log(f"{symbol} 그사이 승인 취소·변경됨 - 주문 안 함")
        return
    try:
        result = place_market_buy(token, account_seq, symbol, qty)
    except urllib.error.HTTPError as e:
        code, msg = _read_error(e)
        log(f"{symbol} 주문 실패 {e.code} {code}: {msg}")
        if code == "insufficient-buying-power":
            text = f"달러 잔고 부족 (예수금 ${available:.2f}) - 토스 앱에서 달러로 환전해주세요."
        elif e.code == 403:
            text = "토스가 이 서버 IP 를 허용하지 않아요 - 개발자 센터 허용 IP 확인 (3.232.83.165)."
        else:
            text = f"오류: {html.escape(msg)}"
        send_message(chat, f"⚠️ {b(symbol)} 매수 실패\n{text}\n승인은 정리했어요.")
        clear_symbol(symbol)
        return
    except Exception as e:                                        # 응답을 못 받음: 주문이 들어갔을 수도 있음 -> ORDERING 유지
        log(f"{symbol} 주문 응답 오류: {type(e).__name__}: {e}")
        send_message(chat, f"⚠️ {b(symbol)} 주문 응답을 받지 못했어요. 토스 앱에서 주문 여부를 확인해주세요.")
        return
    order_id = result["result"]["orderId"]
    update_position(symbol, status="PENDING", order_id=order_id, ref_price=price, account_seq=account_seq,
                    order_qty=qty, base_qty=base_qty, base_avg=base_avg, ordered_at=now_iso())
    log(f"{symbol} 시장가 매수 주문 접수 {qty}주 (주문 {order_id})")
    send_message(chat, f"🛒 {b(symbol)} 시장가 매수 주문 접수\n\n{qty}주 × ${price:.2f} = ${qty * price:.2f}\n체결되면 다시 알려드릴게요.")


def handle_pending(token, symbol, pos):
    qty, avg = get_holding(token, pos["account_seq"], symbol)
    if qty is None:
        return
    base_qty, base_avg = float(pos.get("base_qty") or 0), float(pos.get("base_avg") or 0)
    filled = qty - base_qty                                        # 주문 전부터 갖고 있던 수량은 빼고
    ordered = int(pos.get("order_qty") or 0)
    minutes = (datetime.now(timezone.utc) - datetime.fromisoformat(pos.get("ordered_at") or now_iso())).total_seconds() / 60
    if filled < 1 or (ordered and filled < ordered and is_market_open()):
        log(f"{symbol} 아직 체결 미확인 (보유 {qty:g}주, 주문 전 {base_qty:g}주) - 다음 실행 때 다시")
        if minutes >= PENDING_ALERT_MIN and not pos.get("pending_alerted"):
            update_position(symbol, pending_alerted=True)
            send_message(pos["chat_id"], f"⏳ {b(symbol)} 주문 후 {minutes:.0f}분째 체결 확인이 안 돼요. 토스 앱에서 주문 상태를 확인해주세요.")
        return
    if base_qty > 0 and base_avg > 0 and avg > 0:
        buy_price = (qty * avg - base_qty * base_avg) / filled     # 원래 보유분을 뺀 이번 체결 평균가
    else:
        buy_price = avg if avg > 0 else float(pos.get("ref_price") or 0)
    update_position(symbol, expect="PENDING", status="OPEN", buy_price=round(buy_price, 4), quantity=filled,
                    filled_at=now_iso())
    log(f"{symbol} 체결 완료 {filled:g}주 @ ${buy_price:.4f}")
    part = f" (주문 {ordered}주 중 일부)" if ordered and filled < ordered else ""
    send_message(pos["chat_id"], f"🔔 {b(symbol)} 체결 완료!\n\n매수가: ${buy_price:.2f} × {filled:g}주{part}")


def handle_ordering(symbol, pos):
    """주문 넣다가 꺼진 흔적: 다시 사지 않고 1번만 알림"""
    log(f"{symbol} 주문 넣는 중 상태로 남아 있음 - 다시 주문하지 않음")
    if not pos.get("ordering_alerted"):
        update_position(symbol, ordering_alerted=True)
        send_message(pos["chat_id"], f"⚠️ {b(symbol)} 주문 결과를 확인하지 못했어요. 토스 앱에서 주문 여부를 확인해주세요.")


# ── 메인 ────────────────────────────────────────────────────────────────────

def main():
    run_lock = open(os.path.join(HERE, "monitor.run.lock"), "a")
    if fcntl:
        try:
            fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("이전 실행이 아직 진행 중 - 이번 실행은 건너뜀")
            return
    try:
        positions = load_positions()
    except Exception as e:
        log(f"포지션 저장소({store.describe()}) 읽기 실패: {type(e).__name__}: {e}")
        return
    if not positions:
        print("포지션 없음", flush=True)
        return
    log(f"포지션 {len(positions)}개 {list(positions)} | 저장소 {store.describe()} | "
        f"{'실제 주문' if LIVE_ORDERS else '시험 모드 (실제 주문 안 함)'}")
    if all(p.get("status") == "APPROVED" for p in positions.values()) and not is_market_open():
        for symbol in positions:
            log(f"{symbol} 매수 대기 중 (미국 정규장 시간 아님)")
        return
    try:
        token = toss_get_token()
    except Exception as e:
        log(f"토스 토큰 발급 실패: {type(e).__name__}: {e}")
        return
    for symbol, pos in list(positions.items()):
        try:
            status = pos.get("status")
            if status == "APPROVED":
                handle_approved(token, symbol, pos)
            elif status == "PENDING":
                handle_pending(token, symbol, pos)
            elif status == "ORDERING":
                handle_ordering(symbol, pos)
            elif status == "OPEN":
                log(f"{symbol} 보유 중 (손절·익절은 다음 강의)")
        except Exception as e:
            log(f"{symbol} 처리 오류: {type(e).__name__}: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
