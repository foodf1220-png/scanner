# -*- coding: utf-8 -*-
"""
toss_quote.py - 토스 실시간 현재가 (bf_live.py 돌파 확인용, toss_api_docs.md 6. 현재가 조회)

  GET /api/v1/prices?symbols={SYMBOL}  (헤더: Authorization)  ->  result[0].lastPrice
  키: 환경변수 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET, 없으면 서버 bot.env (TOSS_ENV_FILE) 에서 그 두 줄만 읽음
  토스는 등록된 서버 고정 IP 에서만 응답 -> PC 에서는 키가 없어 꺼짐 (bf_live 는 야후 1분봉으로 대신 확인)
"""
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://openapi.tossinvest.com"
ENV_FILE = os.environ.get("TOSS_ENV_FILE", "/home/ubuntu/bot.env")
TOKEN_MAX_AGE = 600          # 토큰 10분마다 새로 (문서에 유효시간이 없어 짧게), 401 이면 바로 새로
TIMEOUT = 4


def _keys():
    cid, sec = os.environ.get("TOSS_CLIENT_ID"), os.environ.get("TOSS_CLIENT_SECRET")
    if (not cid or not sec) and os.path.exists(ENV_FILE):
        try:
            for line in open(ENV_FILE, encoding="utf-8"):
                k, _, v = line.strip().partition("=")
                v = v.strip().strip('"').strip("'")
                if k.strip() == "TOSS_CLIENT_ID" and not cid:
                    cid = v
                elif k.strip() == "TOSS_CLIENT_SECRET" and not sec:
                    sec = v
        except OSError:
            pass
    return cid, sec


def _json(resp):
    data = resp.read()
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip" or data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)              # 토스는 오류 응답을 gzip 으로 보냄
        except OSError:
            pass
    return json.loads(data) if data else {}


class TossQuote:
    def __init__(self):
        self._cid, self._sec = _keys()
        self._token, self._t_token = None, 0.0
        self.pause_until = 0.0                        # 요청 제한(429) 등으로 잠시 쉬는 중이면 그 시각까지 야후 사용

    @property
    def enabled(self):
        return bool(self._cid and self._sec)

    def available(self):
        return self.enabled and time.time() >= self.pause_until

    def _get_token(self, force=False):
        if force or not self._token or time.time() - self._t_token > TOKEN_MAX_AGE:
            data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self._cid,
                                           "client_secret": self._sec}).encode()
            req = urllib.request.Request(f"{BASE}/oauth2/token", data=data, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                self._token = _json(r)["access_token"]
            self._t_token = time.time()
        return self._token

    def price(self, symbol):
        """토스 현재가 (float). 실패하면 예외 (키·토큰 내용은 메시지에 넣지 않음)"""
        for attempt in (0, 1):
            req = urllib.request.Request(f"{BASE}/api/v1/prices?symbols={urllib.parse.quote(symbol)}")
            req.add_header("Authorization", f"Bearer {self._get_token(force=attempt == 1)}")
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    return float(_json(r)["result"][0]["lastPrice"])
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:    # 토큰 만료 -> 새로 받아 한 번 더
                    continue
                if e.code == 429:
                    self.pause_until = time.time() + 30
                code = ""
                try:
                    code = (_json(e).get("error") or {}).get("code") or ""
                except Exception:
                    pass
                raise RuntimeError(f"토스 현재가 HTTP {e.code} {code}".strip()) from None
        raise RuntimeError("토스 현재가: 토큰 재발급 후에도 401")
