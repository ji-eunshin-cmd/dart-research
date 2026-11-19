"""호출 속도 제한·동시 요청 합치기·디스크 캐시·하루 상한 테스트."""
import asyncio
import os
import sys
import time

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dart_client  # noqa: E402
from dart_client import DartClient, DartError, RateLimiter  # noqa: E402


def make(tmp_path, handler):
    return DartClient("k", tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def ok_handler(calls):
    def h(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={"status": "000", "corp_name": "가나다"})
    return h


def test_same_request_is_sent_once_when_concurrent(tmp_path):
    calls = []

    async def slow(req):
        calls.append(1)
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"status": "000", "x": 1})

    async def run():
        c = make(tmp_path, slow)
        res = await asyncio.gather(*(c.company("00000001") for _ in range(8)))
        assert all(r["x"] == 1 for r in res)
    asyncio.run(run())
    assert len(calls) == 1


def test_disk_cache_survives_restart(tmp_path):
    calls = []

    async def run():
        await make(tmp_path, ok_handler(calls)).company("00000001")
        await make(tmp_path, ok_handler(calls)).company("00000001")  # 새 인스턴스 = 재시작
    asyncio.run(run())
    assert len(calls) == 1


def test_disk_cache_purged_after_24h(tmp_path):
    calls = []

    async def run():
        c = make(tmp_path, ok_handler(calls))
        await c.company("00000001")
        past = time.time() - dart_client.RETENTION - 60
        for f in (tmp_path / "json").glob("*.json"):
            os.utime(f, (past, past))
        c.purge(force=True)
        assert not list((tmp_path / "json").glob("*.json"))
    asyncio.run(run())


def test_rate_limit_spaces_calls():
    async def run():
        rl = RateLimiter(per_sec=20, burst=2, concurrent=4, daily=1000, state=None)
        t0 = time.monotonic()
        for _ in range(6):  # 2개는 바로, 나머지 4개는 1/20초 간격
            await rl.acquire()
        return time.monotonic() - t0
    assert asyncio.run(run()) >= 0.18


def test_daily_cap_blocks_and_persists(tmp_path):
    state = tmp_path / "usage.json"

    async def run():
        rl = RateLimiter(100, 100, 4, daily=20, state=state)
        for _ in range(20):
            await rl.acquire()
        with pytest.raises(DartError, match="상한"):
            await rl.acquire()
        rl2 = RateLimiter(100, 100, 4, daily=20, state=state)  # 재시작해도 오늘 건수 유지
        assert rl2.stats()["calls_today"] == 20
    asyncio.run(run())


def test_limit_exceeded_stops_further_calls(tmp_path):
    calls = []

    def limited(req):
        calls.append(1)
        return httpx.Response(200, json={"status": "020", "message": "사용한도 초과"})

    async def run():
        c = make(tmp_path, limited)
        for code in ("00000001", "00000002", "00000003"):
            with pytest.raises(DartError, match="2만 건"):
                await c.company(code)
        assert c.limiter.stats()["exhausted"]
    asyncio.run(run())
    assert len(calls) == 1  # 한도 초과를 들은 뒤로는 DART에 다시 묻지 않음
