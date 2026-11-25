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
  · 체결 확인 -> 손절·익절 SINGLE 시장가 조건주문 2개 (문서 11번) 등록, ID 저장, OPEN
      EXIT_MODE=fl (기본, 사용자 조건): E = 실제 평균 체결가, L = 신호 때 눌림 저점, D = E - L
          손절 = E - D/3 + 수수료 0.2% (당김), 익절 = E + D + 수수료 0.2%
      EXIT_MODE=pct (강의 예시): 손절 = 매수가 x (1 - 3% + 0.2%), 익절 = 매수가 x (1 + 5% + 0.2%)
  · OPEN: 정규장이면 두 조건주문이 살아 있는지 조회 -> 없으면 재등록. 단 현재가가 이미 트리거를 넘었으면 즉시 시장가 매도
          (방향이 반대로 등록될 위험). 정규장 밖이면 두 조건주문 모두 취소 (장 열리면 위 로직으로 재등록)
          현재가 >= 매수가 +3% -> 손절 조건주문 취소 후 매수가 + 수수료 0.2% 로 다시 등록 -> BREAKEVEN
  · BREAKEVEN: OPEN 과 같고 손절이 매수가(+수수료)로 고정
  · 어느 상태든 보유 0 (조건주문 체결) -> 남은 조건주문 정리 (not-found 인 쪽 = 실행된 쪽) -> 포지션 삭제 -> 텔레그램
  · 뉴욕 15:50 (조기 폐장일 12:50) = 장 마감 10분 전: 남은 종목 시장가 매도 -> CLOSING -> 매도 확인 후 '청산 완료'
      (시장가 매도 = POST /api/v1/orders side SELL: 문서에 예시 없음, 사용자 승인으로 사용)
[실제 주문 스위치] LIVE_ORDERS=true (bot.env) 일 때만 토스에 주문. 기본은 시험 모드 = 수량 계산·알림만 하고 승인 정리
[안전장치 (강의 코드에 추가)]
  · 주문 직전 상태를 ORDERING 으로 바꿔 둠 -> 주문 후 저장 전에 꺼져도 다음 실행이 같은 종목을 또 사지 않음
  · 주문 전 그 종목 기존 보유 수량을 기록 -> 원래 갖고 있던 주식을 체결로 착각하지 않음
  · 주문 실패·매수 불가는 알림 1번 후 정리 (1분마다 같은 실패 알림 반복 안 함)
  · 이전 실행이 아직 돌고 있으면 이번 실행은 건너뜀
키: 서버 /home/ubuntu/bot.env (TOSS_CLIENT_ID, TOSS_CLIENT_SECRET, TELEGRAM_TOKEN) - 크론은 run_with_env.sh 로 실행
"""
import gzip
import html
import json
import math
import os
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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
STOP_DIV           = 3.0           # 손절 = E - D / 3
TARGET_MULT        = 1.0           # 익절 = E + D x 1
EXIT_MODE          = os.environ.get("EXIT_MODE", "fl").strip().lower()   # fl = 눌림 저점 3:1 (사용자 조건) / pct = 강의 예시 %
FEE_PCT            = float(os.environ.get("FEE_PCT", "0.2"))             # 손절은 이만큼 당기고, 익절·브레이크이븐은 더함
SL_PCT             = float(os.environ.get("SL_PCT", "3"))                # pct 모드 손절 -3%
TP_PCT             = float(os.environ.get("TP_PCT", "5"))                # pct 모드 익절 +5%
BE_TRIGGER_PCT     = float(os.environ.get("BE_TRIGGER_PCT", "3"))        # 매수가 +3% 이상이면 손절을 매수가(+수수료)로
MAX_HOLD_MIN       = float(os.environ.get("MAX_HOLD_MIN", "0"))          # 체결 후 이 분이 지나면 시장가 청산 (0 = 끔)
FORCE_EXIT_KST     = os.environ.get("FORCE_EXIT_KST", "").strip()        # 예 "02:30": 한국 시각으로 전량 청산 (마감 10분 전보다 이르면)
KST = ZoneInfo("Asia/Seoul")
FLATTEN_BEFORE_MIN = 10            # 장 마감 10분 전 청산
COND_EXPIRE_DAYS   = 90            # 조건주문 유효기간 (문서: 등록일 + 90일 정도)

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


def close_time(now):
    ch, cm = NYSE_EARLY_CLOSE.get(f"{now:%Y-%m-%d}", (16, 0))
    return now.replace(hour=ch, minute=cm, second=0, microsecond=0)


def is_market_open(now=None):
    """미국 정규장: 뉴욕 09:30~16:00 (서머타임 자동), 주말·휴장일 제외, 조기 폐장일은 13:00 까지"""
    now = now or datetime.now(ET)
    if now.weekday() >= 5 or f"{now:%Y-%m-%d}" in NYSE_HOLIDAYS:
        return False
    return now.replace(hour=9, minute=30, second=0, microsecond=0) <= now < close_time(now)


def flatten_time(now):
    """전량 청산 시각: 장 마감 10분 전, FORCE_EXIT_KST(예 한국 02:30)가 더 이르면 그 시각 (서머타임 자동)"""
    ft = close_time(now) - timedelta(minutes=FLATTEN_BEFORE_MIN)
    if FORCE_EXIT_KST:
        h, m = map(int, FORCE_EXIT_KST.split(":"))
        for k in (0, 1):
            x = (datetime(now.year, now.month, now.day, h, m, tzinfo=KST) + timedelta(days=k)).astimezone(ET)
            if x.date() == now.date() and now.replace(hour=9, minute=30, second=0, microsecond=0) < x:
                ft = min(ft, x)
                break
    return ft


def flatten_reason(now):
    ft = flatten_time(now)
    if ft < close_time(now) - timedelta(minutes=FLATTEN_BEFORE_MIN):
        return f"한국 {FORCE_EXIT_KST} 전량 청산"
    return f"장 마감 {FLATTEN_BEFORE_MIN}분 전 청산"


def is_flatten_time(now=None):
    """장중이면서 전량 청산 시각 이후"""
    now = now or datetime.now(ET)
    return is_market_open(now) and now >= flatten_time(now)


def tick(p):
    return 0.0001 if p < 1 else 0.01


def round_tick(x, up):
    t = tick(x)
    k = math.ceil(x / t - 1e-7) if up else math.floor(x / t + 1e-7)
    return round(k * t, 4)


def fmt_px(p):
    return f"{p:.2f}" if p >= 1 else f"{p:.4f}"


def exit_levels(E, L):
    """(손절, 익절) - 수수료 보정: 손절은 FEE_PCT 만큼 매수가 쪽으로 당기고, 익절은 더함
    fl : 손절 = E - D/3 + 수수료, 익절 = E + D + 수수료 (D = E - 눌림 저점 L)
    pct: 손절 = E x (1 - 3% + 0.2%), 익절 = E x (1 + 5% + 0.2%)
    계산 불가(L 없음·체결가가 저점 이하)면 None"""
    fee = E * FEE_PCT / 100
    if EXIT_MODE == "pct":
        sl_raw, tp_raw = E * (1 - SL_PCT / 100) + fee, E * (1 + TP_PCT / 100) + fee
    else:
        if not L or E <= float(L):
            return None
        D = E - float(L)
        sl_raw, tp_raw = E - D / STOP_DIV + fee, E + D * TARGET_MULT + fee
        if sl_raw >= E:                                    # 손절폭이 수수료보다 좁으면 당기지 않음
            sl_raw = E - D / STOP_DIV
    SL, TP = round_tick(sl_raw, True), round_tick(tp_raw, False)
    return (SL, TP) if SL < E < TP else None


def breakeven_price(E):
    """브레이크이븐 손절가 = 매수가 + 수수료 0.2%"""
    return round_tick(E * (1 + FEE_PCT / 100), True)


def send_message(chat_id, text):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data),
                               timeout=10)
    except Exception as e:
        log(f"텔레그램 오류: {type(e).__name__}")


# ── Toss API (toss_api_docs.md) ──────────────────────────────────────────────

def _raw(resp):
    """응답 본문. 토스는 오류 응답을 gzip 으로 압축해서 보냄 (2026-11-25 확인) -> 풀어서 반환"""
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


def _read_error(e):
    body = _raw(e).decode("utf-8", errors="replace")
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
        return _json(r)["access_token"]


def toss_get(path, token, account_seq=None):
    req = urllib.request.Request(f"{TOSS_BASE}{path}")
    req.add_header("Authorization", f"Bearer {token}")
    if account_seq is not None:
        req.add_header("X-Tossinvest-Account", str(account_seq))
    with urllib.request.urlopen(req, timeout=10) as r:
        return _json(r)


def toss_post(path, token, account_seq, payload):
    req = urllib.request.Request(f"{TOSS_BASE}{path}", data=json.dumps(payload).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Tossinvest-Account", str(account_seq))
    with urllib.request.urlopen(req, timeout=15) as r:
        return _json(r)


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


def toss_delete(path, token, account_seq):
    req = urllib.request.Request(f"{TOSS_BASE}{path}", method="DELETE")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-Tossinvest-Account", str(account_seq))
    with urllib.request.urlopen(req, timeout=10) as r:
        return _json(r)


def place_market_buy(token, account_seq, symbol, quantity):
    return toss_post("/api/v1/orders", token, account_seq,
                     {"symbol": symbol, "side": "BUY", "orderType": "MARKET", "quantity": str(int(quantity))})


def place_market_sell(token, account_seq, symbol, quantity):
    """시장가 매도. 문서에는 BUY 예시만 있음 - 같은 주문 API 에 side SELL (사용자 승인)"""
    return toss_post("/api/v1/orders", token, account_seq,
                     {"symbol": symbol, "side": "SELL", "orderType": "MARKET", "quantity": str(int(quantity))})


def place_conditional_sell(token, account_seq, symbol, quantity, trigger):
    """조건주문 (문서 11번): 트리거 가격 도달 시 전량 시장가 매도. 현재가보다 낮으면 '내려가면', 높으면 '올라가면'"""
    expire = (datetime.now(ET) + timedelta(days=COND_EXPIRE_DAYS)).strftime("%Y-%m-%d")
    r = toss_post("/api/v1/conditional-orders", token, account_seq,
                  {"symbol": symbol, "type": "SINGLE", "orderType": "MARKET", "quantity": str(int(quantity)),
                   "expireDate": expire, "first": {"orderSide": "SELL", "triggerPrice": fmt_px(trigger)}})
    return r["result"]["conditionalOrderId"]


def conditional_alive(token, account_seq, cid):
    """조건주문이 살아 있나: GET /api/v1/conditional-orders/{id} (문서 11번 '활성 여부 확인')
    True = 조회됨, False = not-found(이미 실행·취소·만료, 문서 10번), None = 확인 실패(이번엔 건드리지 않음)"""
    if not cid:
        return False
    try:
        toss_get(f"/api/v1/conditional-orders/{cid}", token, account_seq)
        return True
    except urllib.error.HTTPError as e:
        code, msg = _read_error(e)
        if "not-found" in (code + msg) or e.code == 404:
            return False
        log(f"조건주문 {cid} 조회 오류 {e.code} {code}")
        return None
    except Exception as e:
        log(f"조건주문 {cid} 조회 오류 {type(e).__name__}")
        return None


def cancel_conditional(token, account_seq, cid):
    """'cancelled' (활성 -> 취소함) / 'gone' (not-found: 이미 실행·취소·만료) / 'error: ...'"""
    if not cid:
        return "none"
    try:
        toss_delete(f"/api/v1/conditional-orders/{cid}", token, account_seq)
        return "cancelled"
    except urllib.error.HTTPError as e:
        code, msg = _read_error(e)
        return "gone" if "not-found" in (code + msg) else f"error: {e.code} {code} {msg[:80]}"
    except Exception as e:
        return f"error: {type(e).__name__}"


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
    if is_flatten_time():                                         # 전량 청산 시각 이후엔 새로 사지 않음
        send_message(chat, f"⏭ {b(symbol)} 매수 안 함 - {flatten_reason(datetime.now(ET))} 시각이 지났어요. 승인은 정리했어요.")
        clear_symbol(symbol)
        return
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
    manage_open(token, symbol, dict(pos, status="OPEN", buy_price=round(buy_price, 4), quantity=filled))


# ── 손절·익절·장 마감 청산 ────────────────────────────────────────────────────

def pnl_text(E, px):
    return f"{(px / E - 1) * 100:+.2f}%" if E else ""


def manage_open(token, symbol, pos):
    """OPEN / BREAKEVEN 관리 (체결 직후에도 바로 호출)
    1) 보유 0 -> 정리·알림   2) 정규장 밖 -> 조건주문 전부 취소 (장 열리면 재등록)   3) 마감 10분 전 -> 시장가 청산
    4) OPEN 이고 현재가 >= 매수가 +3% -> 손절 조건주문 취소 후 매수가(+수수료)로 다시 = BREAKEVEN
    5) 손절·익절 조건주문이 없으면(not-found) 재등록. 단 현재가가 이미 트리거를 넘었으면 즉시 시장가 매도"""
    if not LIVE_ORDERS:
        log(f"{symbol} 보유 중 (시험 모드 - 손절·익절 주문 안 함)")
        return
    acc, E, qty = pos["account_seq"], float(pos["buy_price"]), int(float(pos["quantity"]))
    qty_now, _ = get_holding(token, acc, symbol)
    if qty_now is None:
        return
    if qty_now - float(pos.get("base_qty") or 0) < 1:
        return resolve_closed(token, symbol, pos)
    if not is_market_open():
        if pos.get("sl_id") or pos.get("tp_id"):
            for k in ("sl_id", "tp_id"):
                r = cancel_conditional(token, acc, pos.get(k))
                if r.startswith("error"):
                    log(f"{symbol} 정규장 외 조건주문 취소 오류 ({k}): {r}")
            update_position(symbol, sl_id=None, tp_id=None)
            log(f"{symbol} 정규장 외 - 손절·익절 조건주문 취소 (장이 열리면 다시 등록)")
        else:
            log(f"{symbol} 정규장 외 대기 (조건주문 없음)")
        return
    if is_flatten_time():
        return start_close(token, symbol, pos, flatten_reason(datetime.now(ET)))
    if MAX_HOLD_MIN and pos.get("filled_at"):                     # 진입 후 60분 지나면 시장가 청산
        held = (datetime.now(timezone.utc) - datetime.fromisoformat(pos["filled_at"])).total_seconds() / 60
        if held >= MAX_HOLD_MIN:
            return start_close(token, symbol, pos, f"진입 {MAX_HOLD_MIN:g}분 경과 청산")

    SL, TP = pos.get("SL"), pos.get("TP")
    if SL is None or TP is None:
        lv = exit_levels(E, pos.get("signal_low"))
        if lv is None:
            return start_close(token, symbol, pos, f"체결가 ${E:.2f} 가 눌림 저점 이하 (손절가 계산 불가) → 즉시 청산")
        SL, TP = lv
        update_position(symbol, SL=SL, TP=TP)
        pos = dict(pos, SL=SL, TP=TP)
    SL, TP = float(SL), float(TP)
    price = get_current_price(token, symbol)

    if pos["status"] == "OPEN" and price >= E * (1 + BE_TRIGGER_PCT / 100):           # 브레이크이븐
        BE = breakeven_price(E)
        if pos.get("sl_id"):
            cancel_conditional(token, acc, pos["sl_id"])
        update_position(symbol, status="BREAKEVEN", SL=BE, sl_id=None, be_at=now_iso())
        pos = dict(pos, status="BREAKEVEN", SL=BE, sl_id=None)
        log(f"{symbol} 브레이크이븐: 현재가 ${price:.2f} ({pnl_text(E, price)}) → 손절 ${fmt_px(SL)} → ${fmt_px(BE)}")
        send_message(pos["chat_id"], f"🔒 {b(symbol)} 브레이크이븐\n\n현재가 ${price:.2f} ({pnl_text(E, price)}) - 매수가 +{BE_TRIGGER_PCT:g}% 도달\n"
                                     f"손절을 ${fmt_px(SL)} → 매수가+수수료 ${fmt_px(BE)} 로 올렸어요. 익절 ${fmt_px(TP)} 은 그대로")
        SL = BE

    new, failed = {}, []
    for kind, trig in (("sl", SL), ("tp", TP)):
        alive = conditional_alive(token, acc, pos.get(f"{kind}_id"))
        if alive or alive is None:                                   # 살아 있음 / 확인 실패면 이번엔 그대로
            continue
        if kind == "sl" and price <= trig:                           # 방향이 반대로 등록될 위험 -> 즉시 매도
            return start_close(token, symbol, pos, f"현재가 ${price:.2f} 가 이미 손절가 ${fmt_px(trig)} 이하 → 즉시 시장가 매도")
        if kind == "tp" and price >= trig:
            return start_close(token, symbol, pos, f"현재가 ${price:.2f} 가 이미 익절가 ${fmt_px(trig)} 이상 → 즉시 시장가 매도")
        try:
            new[f"{kind}_id"] = place_conditional_sell(token, acc, symbol, qty, trig)
        except Exception as e:
            code, msg = _read_error(e) if isinstance(e, urllib.error.HTTPError) else ("", str(e))
            log(f"{symbol} {'손절' if kind == 'sl' else '익절'} 조건주문 등록 실패: {code} {msg}")
            failed.append("손절" if kind == "sl" else "익절")
    if new:
        update_position(symbol, **new)
        log(f"{symbol} 조건주문 등록: " + ", ".join(f"{'손절' if k == 'sl_id' else '익절'} ${fmt_px(SL if k == 'sl_id' else TP)}" for k in new))
    elif not failed:
        log(f"{symbol} {pos['status']} - 손절 ${fmt_px(SL)} / 익절 ${fmt_px(TP)} 조건주문 유지 (현재가 ${price:.2f}, {pnl_text(E, price)})")
    if not pos.get("exits_announced") and new:
        update_position(symbol, exits_announced=True)
        flat = flatten_time(datetime.now(ET))
        basis = (f"눌림 저점 ${fmt_px(float(pos['signal_low']))} 기준 3:1" if EXIT_MODE != "pct" else f"-{SL_PCT:g}% / +{TP_PCT:g}%")
        send_message(pos["chat_id"],
                     f"🛡 {b(symbol)} 손절·익절 등록 ({basis}" + (f", 수수료 {FEE_PCT:g}% 보정" if FEE_PCT else "") + f")\n\n매수가 ${E:.2f}\n"
                     f"손절 ${fmt_px(SL)} ({pnl_text(E, SL)}) · 익절 ${fmt_px(TP)} ({pnl_text(E, TP)})\n"
                     + (f"+{BE_TRIGGER_PCT:g}% 오르면 손절을 매수가+수수료로 올림 (브레이크이븐)\n" if BE_TRIGGER_PCT < 100 else "")
                     + (f"진입 {MAX_HOLD_MIN:g}분이 지나면 시장가 청산\n" if MAX_HOLD_MIN else "")
                     + f"늦어도 뉴욕 {flat:%H:%M} (한국 {flat.astimezone(KST):%H:%M}) 에 시장가 청산")
    if failed and not pos.get("exits_fail_alerted"):
        update_position(symbol, exits_fail_alerted=True)
        send_message(pos["chat_id"], f"⚠️ {b(symbol)} {', '.join(failed)} 조건주문 등록 실패 - 1분마다 다시 시도해요")


def start_close(token, symbol, pos, why):
    """시장가 매도로 청산 시작 -> CLOSING. 매도가 성공한 뒤에 조건주문 취소 (실패해도 손절·익절 보호 유지)"""
    acc, E = pos["account_seq"], float(pos.get("buy_price") or 0)
    qty_now, _ = get_holding(token, acc, symbol)
    if qty_now is None:
        return
    held = int(qty_now - float(pos.get("base_qty") or 0))
    if held < 1:
        return resolve_closed(token, symbol, pos)
    try:
        oid = place_market_sell(token, acc, symbol, held)["result"]["orderId"]
    except Exception as e:
        code, msg = _read_error(e) if isinstance(e, urllib.error.HTTPError) else ("", str(e))
        log(f"{symbol} 시장가 매도 실패: {code} {msg} - 조건주문을 취소하고 다시 시도")
        for k in ("sl_id", "tp_id"):                              # 조건주문이 수량을 묶고 있을 수 있음
            cancel_conditional(token, acc, pos.get(k))
        update_position(symbol, sl_id=None, tp_id=None)
        pos = dict(pos, sl_id=None, tp_id=None)
        try:
            oid = place_market_sell(token, acc, symbol, held)["result"]["orderId"]
        except Exception as e2:
            code, msg = _read_error(e2) if isinstance(e2, urllib.error.HTTPError) else ("", str(e2))
            log(f"{symbol} 시장가 매도 재시도 실패: {code} {msg}")
            if not pos.get("close_alerted"):
                update_position(symbol, close_alerted=True)
                send_message(pos["chat_id"], f"⚠️ {b(symbol)} 청산 매도 실패 ({html.escape(why)})\n오류: {html.escape(code or msg)}\n"
                                             f"1분마다 다시 시도해요. 급하면 토스 앱에서 직접 매도해주세요.")
            return
    for k in ("sl_id", "tp_id"):
        r = cancel_conditional(token, acc, pos.get(k))
        if r.startswith("error"):
            log(f"{symbol} 조건주문 취소 오류 ({k}): {r}")
    try:
        price = get_current_price(token, symbol)
    except Exception:
        price = 0.0
    update_position(symbol, status="CLOSING", close_reason=why, sell_order_id=oid, sell_qty=held,
                    closing_at=now_iso(), close_ref_price=price, sl_id=None, tp_id=None)
    log(f"{symbol} 청산 매도 주문 {held}주 ({why})")
    send_message(pos["chat_id"], f"⏰ {b(symbol)} {html.escape(why)}\n\n시장가 매도 주문 {held}주"
                                 + (f" (현재가 약 ${price:.2f}, {pnl_text(E, price)})" if price else ""))


def resolve_closed(token, symbol, pos):
    """보유가 사라짐 = 조건주문 실행(또는 직접 매도). 취소 시도로 어느 쪽이 실행됐는지 판단 후 정리"""
    acc, E = pos["account_seq"], float(pos.get("buy_price") or 0)
    r = {k: cancel_conditional(token, acc, pos.get(f"{k}_id")) for k in ("sl", "tp")}
    if r["sl"] == "gone" and r["tp"] != "gone":
        be = pos.get("status") == "BREAKEVEN"
        why, px, mark = ("브레이크이븐 손절 실행 (조건주문)" if be else "손절 실행 (조건주문)"), float(pos.get("SL") or 0), \
            ("🟰" if be else "🔻")
    elif r["tp"] == "gone" and r["sl"] != "gone":
        why, px, mark = "익절 실행 (조건주문)", float(pos.get("TP") or 0), "✅"
    else:
        why, mark = "청산 확인 (직접 매도했거나 조건주문 상태를 토스 앱에서 확인해주세요)", "ℹ️"
        try:
            px = get_current_price(token, symbol)
        except Exception:
            px = 0.0
    clear_symbol(symbol)
    log(f"{symbol} {why} - 매수 ${E:.2f} → 약 ${px:.2f} (조건주문 취소 결과 {r})")
    send_message(pos["chat_id"], f"{mark} {b(symbol)} {html.escape(why)}\n\n매수 ${E:.2f} → 약 ${px:.2f} "
                                 f"({pnl_text(E, px)}, 수수료 전) × {float(pos.get('quantity') or 0):g}주\n남은 조건주문은 취소했어요.")


def handle_closing(token, symbol, pos):
    qty_now, _ = get_holding(token, pos["account_seq"], symbol)
    if qty_now is None:
        return
    E = float(pos.get("buy_price") or 0)
    if qty_now - float(pos.get("base_qty") or 0) < 1:
        try:
            px = get_current_price(token, symbol)
        except Exception:
            px = float(pos.get("close_ref_price") or 0)
        clear_symbol(symbol)
        log(f"{symbol} 청산 완료 ({pos.get('close_reason')})")
        send_message(pos["chat_id"], f"✅ {b(symbol)} 청산 완료 - {html.escape(str(pos.get('close_reason')))}\n\n"
                                     f"매수 ${E:.2f} → 약 ${px:.2f} ({pnl_text(E, px)}, 수수료 전) × {pos.get('sell_qty')}주")
        return
    minutes = (datetime.now(timezone.utc) - datetime.fromisoformat(pos.get("closing_at") or now_iso())).total_seconds() / 60
    log(f"{symbol} 청산 매도 체결 대기 ({minutes:.0f}분)")
    if minutes >= PENDING_ALERT_MIN and not pos.get("closing_alerted"):
        update_position(symbol, closing_alerted=True)
        send_message(pos["chat_id"], f"⏳ {b(symbol)} 청산 매도 후 {minutes:.0f}분째 체결 확인이 안 돼요. 토스 앱에서 확인해주세요.")


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
            elif status in ("OPEN", "BREAKEVEN"):
                manage_open(token, symbol, pos)
            elif status == "CLOSING":
                handle_closing(token, symbol, pos)
        except Exception as e:
            log(f"{symbol} 처리 오류: {type(e).__name__}: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
