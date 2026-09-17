"""Minimal PostgREST client — Supabase REST read/write, stdlib only.

supabase-py 대신 얇은 래퍼를 쓴다: 음성 모듈이 필요한 건 테이블 읽기와
관리자 upsert/delete뿐이라 새 의존성을 들일 이유가 없다. 모든 함수는
실패 시 예외 대신 None/False를 돌려준다 — 호출부가 fallback을 결정한다.

필요한 환경변수(호출부에서 주입):
  SUPABASE_URL       https://<project>.supabase.co
  SUPABASE_ANON_KEY  읽기용 anon 키 (RLS 읽기 정책만 허용)
  SUPABASE_WRITE_KEY 쓰기용 service 키 — 음성 PC가 아니라 관리 도구에만 둔다
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


def _request(method, base, key, table, params=None, body=None, upsert=False, timeout=3.0):
    url = f"{base.rstrip('/')}/rest/v1/{table}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    if upsert:
        headers["Prefer"] = "resolution=merge-duplicates"
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read() or b""
    except (OSError, urllib.error.HTTPError):
        return None
    if not raw:
        return []
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, list) else [out]


def get_rows(base, key, table, params=None, timeout=3.0):
    """테이블 행 목록 → list[dict]. 실패 시 None (빈 테이블은 []로 구분된다)."""
    params = {"select": "*", **(params or {})}
    return _request("GET", base, key, table, params, timeout=timeout)


def upsert_rows(base, key, table, rows, timeout=5.0):
    """PK 기준 upsert — rows는 dict 또는 dict 리스트. 성공 시 True."""
    if isinstance(rows, dict):
        rows = [rows]
    out = _request("POST", base, key, table, body=rows, upsert=True, timeout=timeout)
    return out is not None


def delete_where(base, key, table, params, timeout=5.0):
    """필터에 맞는 행 삭제 — params는 PostgREST 필터({col: 'eq.x'}). 성공 시 True."""
    out = _request("DELETE", base, key, table, params=params, timeout=timeout)
    return out is not None
