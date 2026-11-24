# -*- coding: utf-8 -*-
"""
store.py - 포지션 저장소 (bot.py · monitor.py 공용)

형식 (강의와 같음): {"종목": {"symbol", "status", "chat_id", "budget", "signal_low", ...}, ...}
  status: APPROVED(승인, 주문 전) → ORDERING(주문 넣는 중) → PENDING(주문 접수, order_id) → OPEN(체결, buy_price·quantity)
저장 위치 (bot.env 의 POSITIONS_STORE):
  ssm  = AWS SSM Parameter Store  SSM_KEY(/scanner/positions), SSM_REGION(ap-northeast-2) - EC2 에 IAM 역할 필요
  file = 서버 파일 POSITIONS_FILE (기본 ~/bot/positions.json)
같은 서버의 두 프로그램이 동시에 고치지 않게 positions_lock() 으로 잠금 (읽기-수정-쓰기 사이)
"""
import json
import os
from contextlib import contextmanager

try:
    import fcntl
except ImportError:                                  # 윈도우 테스트용
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.environ.get("POSITIONS_STORE", "file").strip().lower()
SSM_KEY = os.environ.get("SSM_KEY", "/scanner/positions")
SSM_REGION = os.environ.get("SSM_REGION", "ap-northeast-2")
POSITIONS_FILE = os.environ.get("POSITIONS_FILE", os.path.join(HERE, "positions.json"))
LOCK_FILE = os.environ.get("POSITIONS_LOCK", os.path.join(HERE, "positions.lock"))
_client = None


def ssm():
    global _client
    if _client is None:
        import boto3
        _client = boto3.client("ssm", region_name=SSM_REGION)
    return _client


def describe():
    return f"SSM {SSM_REGION} {SSM_KEY}" if STORE == "ssm" else f"파일 {POSITIONS_FILE}"


@contextmanager
def positions_lock():
    f = open(LOCK_FILE, "a")
    try:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def load_positions():
    """없으면 {}. 읽기 오류는 그대로 예외 (빈 것으로 착각해 덮어쓰지 않게)"""
    if STORE == "ssm":
        c = ssm()
        try:
            return json.loads(c.get_parameter(Name=SSM_KEY)["Parameter"]["Value"])
        except c.exceptions.ParameterNotFound:
            return {}
    try:
        with open(POSITIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_positions(positions):
    if STORE == "ssm":
        c = ssm()
        if positions:
            c.put_parameter(Name=SSM_KEY, Value=json.dumps(positions, ensure_ascii=False, separators=(",", ":")),
                            Type="String", Overwrite=True)
        else:
            try:
                c.delete_parameter(Name=SSM_KEY)
            except c.exceptions.ParameterNotFound:
                pass
        return
    tmp = POSITIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=1)
    os.replace(tmp, POSITIONS_FILE)


def update_position(symbol, expect=None, **fields):
    """잠금 안에서 한 종목 필드 변경. expect(상태)가 다르거나 종목이 없으면 False"""
    with positions_lock():
        p = load_positions()
        cur = p.get(symbol)
        if cur is None or (expect and cur.get("status") != expect):
            return False
        cur.update(fields)
        save_positions(p)
        return True


def clear_symbol(symbol):
    with positions_lock():
        p = load_positions()
        if symbol in p:
            p.pop(symbol)
            save_positions(p)
