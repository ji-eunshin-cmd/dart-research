"""OpenDART API 클라이언트 (읽기 전용, 캐시 포함).

쓰는 API (모두 무료, 키당 하루 2만 건):
  corpCode.xml       전체 회사 고유번호 목록 (zip)       → 하루 1번만 받음
  company.json       기업개황
  list.json          공시 검색 (사업보고서 A001 / 감사보고서 F001)
  document.xml       공시 원문 (zip 안의 XML)           → 접수번호별 영구 캐시
  hyslrSttus.json    최대주주 현황 (정기보고서 기준)
  fnlttSinglAcnt.json 단일회사 주요계정 (3개년)
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from lxml import etree

BASE = "https://opendart.fss.or.kr/api"
VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"
CORP_LIST_TTL = 7 * 86400  # 회사 목록(DART 공개 고유번호 목록)은 일주일에 한 번 새로 받음
RETENTION = 24 * 3600      # 그 밖의 캐시(공시 원문·API 응답)는 24시간 안에 지움
KST = timezone(timedelta(hours=9))

# 호출 속도 제한 (팀 전체가 키 하나를 같이 쓰므로 서버에서 한꺼번에 몰리지 않게 막음)
RATE_PER_SEC = float(os.environ.get("DART_RATE_PER_SEC", "5"))     # 초당 평균 호출 수
RATE_BURST = int(os.environ.get("DART_RATE_BURST", "10"))          # 순간적으로 허용하는 묶음
MAX_CONCURRENT = int(os.environ.get("DART_MAX_CONCURRENT", "4"))   # 동시에 나가는 요청 수
DAILY_LIMIT = int(os.environ.get("DART_DAILY_LIMIT", "18000"))     # 하루 상한 (OpenDART 2만 건보다 여유 있게)

STATUS_MSG = {
    "010": "등록되지 않은 DART API 키입니다.",
    "011": "사용할 수 없는 DART API 키입니다.",
    "012": "접근할 수 없는 IP입니다.",
    "020": "오늘 DART 조회 한도(2만 건)를 넘었습니다. 내일 다시 시도해 주세요.",
    "021": "조회 가능한 회사 수를 넘었습니다.",
    "100": "DART 요청 값이 올바르지 않습니다.",
    "800": "DART 시스템 점검 중입니다.",
    "900": "DART 쪽 오류입니다. 잠시 뒤 다시 시도해 주세요.",
}


class DartError(Exception):
    pass


def _kst_day() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


class RateLimiter:
    """토큰 버킷 + 동시 요청 수 제한 + 하루 호출 수 집계(KST 기준, 디스크에 보관해 재시작해도 이어짐)."""

    def __init__(self, per_sec: float, burst: int, concurrent: int, daily: int, state: Path | None):
        self.per_sec, self.burst, self.daily = max(per_sec, 0.1), max(burst, 1), daily
        self.tokens, self.t = float(self.burst), time.monotonic()
        self.lock = asyncio.Lock()
        self.sem = asyncio.Semaphore(max(concurrent, 1))
        self.state = state
        self.day, self.count, self.blocked_day = _kst_day(), 0, None
        if state and state.exists():
            try:
                d = json.loads(state.read_text())
                if d.get("day") == self.day:
                    self.count, self.blocked_day = int(d.get("count", 0)), d.get("blocked_day")
            except Exception:
                pass

    def _roll(self) -> None:
        today = _kst_day()
        if today != self.day:
            self.day, self.count, self.blocked_day = today, 0, None

    def _save(self) -> None:
        if not self.state:
            return
        try:
            self.state.write_text(json.dumps({"day": self.day, "count": self.count, "blocked_day": self.blocked_day}))
        except OSError:
            pass

    def check_quota(self) -> None:
        self._roll()
        if self.blocked_day == self.day:
            raise DartError(STATUS_MSG["020"])
        if self.count >= self.daily:
            raise DartError(f"오늘 이 커넥터의 DART 조회 상한({self.daily:,}건)에 도달했습니다. 자정(한국 시간) 이후 다시 시도해 주세요.")

    def mark_exhausted(self) -> None:
        """OpenDART가 한도 초과(020)를 알려 오면 오늘은 더 부르지 않음."""
        self._roll()
        self.blocked_day = self.day
        self._save()

    async def acquire(self) -> None:
        self.check_quota()
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.t) * self.per_sec)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    break
                await asyncio.sleep((1 - self.tokens) / self.per_sec)
            self.count += 1
            if self.count % 20 == 0:
                self._save()

    def stats(self) -> dict:
        self._roll()
        return {"day": self.day, "calls_today": self.count, "daily_limit": self.daily,
                "exhausted": self.blocked_day == self.day}


def norm_name(s: str) -> str:
    s = re.sub(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\s+", "", s or "")
    return s.lower()


@dataclass
class Corp:
    corp_code: str
    corp_name: str
    stock_code: str

    @property
    def listed(self) -> bool:
        return bool(self.stock_code.strip())

    def as_dict(self) -> dict:
        return {"corp_code": self.corp_code, "corp_name": self.corp_name,
                "stock_code": self.stock_code or None, "listed": self.listed}


class DartClient:
    def __init__(self, api_key: str, cache_dir: Path, http: httpx.AsyncClient | None = None):
        if not api_key:
            raise DartError("DART_API_KEY 환경변수가 비어 있습니다.")
        self.key = api_key
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._corps: list[Corp] | None = None
        self._corps_at = 0.0
        self._refresh: asyncio.Task | None = None
        self._json_cache: dict[str, tuple[float, Any]] = {}
        self._last_purge = 0.0
        self._inflight: dict[str, asyncio.Future] = {}
        self.jdir = self.cache / "json"
        self.jdir.mkdir(exist_ok=True)
        self.limiter = RateLimiter(RATE_PER_SEC, RATE_BURST, MAX_CONCURRENT, DAILY_LIMIT, self.cache / "usage.json")

    def purge(self, force: bool = False) -> None:
        """24시간 지난 공시 원문 파일과 메모리 캐시를 지움 (10분마다 한 번 확인)."""
        now = time.time()
        if not force and now - self._last_purge < 600:
            return
        self._last_purge = now
        for k in [k for k, (t, _) in self._json_cache.items() if now - t > RETENTION]:
            self._json_cache.pop(k, None)
        for p in [*self.cache.glob("doc_*.zip"), *self.jdir.glob("*.json")]:
            try:
                if now - p.stat().st_mtime > RETENTION:
                    p.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------ 저수준
    async def _get(self, path: str, **params) -> httpx.Response:
        self.purge()
        await self.limiter.acquire()
        try:
            async with self.limiter.sem:
                r = await self.http.get(f"{BASE}/{path}", params={"crtfc_key": self.key, **params})
        except httpx.HTTPError as e:
            raise DartError(f"DART 서버에 연결하지 못했습니다: {type(e).__name__}") from e
        if r.status_code != 200:
            raise DartError(f"DART 서버 응답 오류 (HTTP {r.status_code})")
        return r

    async def _json(self, path: str, ttl: float = 86400, **params) -> dict:
        ck = path + json.dumps(params, sort_keys=True)
        hit = self._json_cache.get(ck)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        f = self.jdir / (hashlib.sha256(ck.encode()).hexdigest()[:32] + ".json")
        try:  # 서버가 재시작돼도 디스크에 남은 응답을 다시 씀 (CACHE_DIR 볼륨이 있을 때)
            if f.exists() and time.time() - f.stat().st_mtime < min(ttl, RETENTION):
                data = json.loads(f.read_text(encoding="utf-8"))
                self._json_cache[ck] = (f.stat().st_mtime, data)
                return data
        except (OSError, ValueError):
            pass
        return await self._once(ck, lambda: self._fetch_json(ck, f, path, params))

    async def _once(self, key: str, make):
        """같은 요청이 동시에 여러 번 들어오면 DART에는 한 번만 보내고 결과를 나눠 씀."""
        fut = self._inflight.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            res = await make()
            fut.set_result(res)
            return res
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # 기다리는 쪽이 없어도 경고가 나지 않게
            raise
        finally:
            self._inflight.pop(key, None)

    async def _fetch_json(self, ck: str, f: Path, path: str, params: dict) -> dict:
        data = (await self._get(path, **params)).json()
        st = data.get("status")
        if st == "013":  # 조회된 데이터 없음
            data = {"status": "013", "list": []}
        elif st != "000":
            if st == "020":
                self.limiter.mark_exhausted()
            raise DartError(STATUS_MSG.get(st, f"DART 오류 {st}: {data.get('message', '')}"))
        self._json_cache[ck] = (time.time(), data)
        if len(self._json_cache) > 2000:
            self._json_cache.pop(next(iter(self._json_cache)))
        try:
            f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return data

    @staticmethod
    def _zip_error(content: bytes) -> None:
        """zip 대신 오류 XML/JSON이 오면 그 상태 코드로 오류를 냄."""
        head = content[:400].decode("utf-8", "ignore")
        m = re.search(r"<status>(\d+)</status>|\"status\"\s*:\s*\"(\d+)\"", head)
        if m:
            st = m.group(1) or m.group(2)
            if st == "013":
                raise DartError("공시 원문이 없습니다.")
            raise DartError(STATUS_MSG.get(st, f"DART 오류 {st}"))
        raise DartError("DART가 예상과 다른 응답을 보냈습니다.")

    def _zip_check(self, content: bytes) -> None:
        if not content.startswith(b"PK"):
            if re.search(rb"<status>020</status>|\"status\"\s*:\s*\"020\"", content[:400]):
                self.limiter.mark_exhausted()
            self._zip_error(content)

    # ------------------------------------------------------------ 회사 목록
    # DART 전체 회사 목록(수 MB)은 해외 서버에서 받는 데 몇 분 걸릴 수 있어서, 사용자를 기다리게 하지 않도록
    # 백그라운드로 받습니다. 일주일 지난 목록도 새 목록이 올 때까지는 그대로 씁니다.
    @staticmethod
    def _parse_corps(content: bytes) -> list[Corp]:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            xml = z.read(z.namelist()[0])
        root = etree.fromstring(xml, etree.XMLParser(recover=True, huge_tree=True))
        return [Corp((el.findtext("corp_code") or "").strip(), (el.findtext("corp_name") or "").strip(),
                     (el.findtext("stock_code") or "").strip()) for el in root.iter("list")]

    async def _download_corps(self) -> None:
        f = self.cache / "corpcode.zip"
        t0 = time.time()
        print(f"[DART] 회사 목록 받기 시작 (저장 위치 {f})", flush=True)
        try:
            buf = bytearray()
            last = t0
            async with self.http.stream("GET", f"{BASE}/corpCode.xml", params={"crtfc_key": self.key},
                                        timeout=httpx.Timeout(120.0, connect=15.0)) as r:
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    if time.time() - last > 20:
                        last = time.time()
                        print(f"[DART] 회사 목록 받는 중… {len(buf) / 1e6:.1f}MB, {last - t0:.0f}초", flush=True)
                    if time.time() - t0 > 900:
                        raise DartError("15분 안에 받지 못했습니다")
                status = r.status_code
            content = bytes(buf)
            if status != 200 or not content.startswith(b"PK"):
                self._zip_error(content)
            corps = self._parse_corps(content)
            f.write_bytes(content)
            self._corps, self._corps_at = corps, time.time()
            print(f"[DART] 회사 목록 {len(corps):,}곳 받음 ({len(content) / 1e6:.1f}MB, {time.time() - t0:.0f}초)", flush=True)
        except Exception as e:  # 다음 요청 때 다시 시도
            print(f"[DART] 회사 목록 받기 실패 ({time.time() - t0:.0f}초): {type(e).__name__}: {e}", flush=True)
            raise

    def warm(self) -> None:
        """회사 목록이 없거나 하루 지났으면 백그라운드로 받기 시작 (기다리지 않음)."""
        if self._corps is None:
            f = self.cache / "corpcode.zip"
            if f.exists():
                try:
                    self._corps, self._corps_at = self._parse_corps(f.read_bytes()), f.stat().st_mtime
                    print(f"[DART] 저장된 회사 목록 {len(self._corps):,}곳을 읽었습니다", flush=True)
                except Exception as e:
                    print(f"[DART] 저장된 회사 목록을 읽지 못함: {e}", flush=True)
        fresh = self._corps is not None and time.time() - self._corps_at < CORP_LIST_TTL
        if not fresh and (self._refresh is None or self._refresh.done()):
            self._refresh = asyncio.get_running_loop().create_task(self._download_corps())
            self._refresh.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def corps(self) -> list[Corp]:
        self.warm()
        if self._corps is not None:
            return self._corps
        try:  # 처음 한 번은 잠깐 기다려 봄
            await asyncio.wait_for(asyncio.shield(self._refresh), timeout=90)
        except asyncio.TimeoutError:
            raise DartError("DART 회사 목록을 처음 받는 중입니다(서버 시작 후 한 번, 몇 분 걸림). 1~2분 뒤 다시 찾아 주세요.")
        except Exception as e:
            raise DartError(f"DART 회사 목록을 받지 못했습니다: {e}") from e
        return self._corps or []

    async def search(self, name: str, limit: int = 10) -> list[Corp]:
        found = await self._search_code(name, limit)
        if found is not None and found:
            return found
        q = norm_name(name)
        if not q:
            return []
        exact, starts, contains = [], [], []
        for c in await self.corps():
            n = norm_name(c.corp_name)
            if n == q:
                exact.append(c)
            elif n.startswith(q):
                starts.append(c)
            elif q in n:
                contains.append(c)
        key = lambda c: (not c.listed, len(c.corp_name))  # 상장사·짧은 이름 먼저
        return (sorted(exact, key=key) + sorted(starts, key=key) + sorted(contains, key=key))[:limit]

    async def _search_code(self, query: str, limit: int) -> list[Corp] | None:
        """종목코드·고유번호로 찾기. 숫자만(앞부분 일치) 또는 와일드카드(* 여러 글자, ? 한 글자)를 쓰면 코드로 봅니다.
        예: 005930 → 삼성전자, 0059* → 0059로 시작하는 종목, *930 → 930으로 끝나는 종목, 00?930. 코드 검색이 아니면 None."""
        q = re.sub(r"\s+", "", query or "").upper()
        wild = "*" in q or "?" in q
        if not q or not re.fullmatch(r"[0-9A-Z*?]+", q) or not re.search(r"\d", q) or not (wild or q.isdigit()):
            return None
        corps = await self.corps()
        if q.isdigit() and len(q) == 8:  # DART 고유번호
            hit = [c for c in corps if c.corp_code == q]
            if hit:
                return hit
        if wild:
            rx = re.compile("^" + re.escape(q).replace(r"\*", ".*").replace(r"\?", ".") + "$")
            hits = [c for c in corps if c.stock_code and rx.match(c.stock_code.upper())]
        else:
            hits = [c for c in corps if c.stock_code and c.stock_code.startswith(q)]
        hits.sort(key=lambda c: (c.stock_code != q, c.stock_code))
        return hits[:limit]

    # ------------------------------------------------------------ 개별 API
    async def company(self, corp_code: str) -> dict:
        return await self._json("company.json", corp_code=corp_code)

    async def latest_report(self, corp_code: str, years: int = 3) -> dict | None:
        """가장 최근 사업보고서(A001), 없으면 감사보고서(F001). 정정공시는 최신 것."""
        end = date.today()
        bgn = end - timedelta(days=365 * years)
        for ty, dty in (("A", "A001"), ("F", "F001")):
            data = await self._json("list.json", ttl=21600, corp_code=corp_code, bgn_de=bgn.strftime("%Y%m%d"),
                                    end_de=end.strftime("%Y%m%d"), pblntf_ty=ty, pblntf_detail_ty=dty, page_count=20)
            items = [i for i in data.get("list", []) if ("사업보고서" in i.get("report_nm", "") or "감사보고서" in i.get("report_nm", ""))]
            if items:
                items.sort(key=lambda i: i.get("rcept_dt", ""), reverse=True)
                it = items[0]
                it["kind"] = "annual" if ty == "A" else "audit"
                return it
        return None

    async def periodic_reports(self, corp_code: str, days: int = 550) -> list[dict]:
        """최근 정기공시(사업·반기·분기보고서), 접수일 최신순."""
        end = date.today()
        data = await self._json("list.json", ttl=21600, corp_code=corp_code, bgn_de=(end - timedelta(days=days)).strftime("%Y%m%d"),
                                end_de=end.strftime("%Y%m%d"), pblntf_ty="A", page_count=40)
        items = [i for i in data.get("list", []) if re.search(r"(사업|반기|분기)보고서", i.get("report_nm", ""))]
        items.sort(key=lambda i: (i.get("rcept_dt", ""), i.get("rcept_no", "")), reverse=True)
        return items

    async def document_xml(self, rcept_no: str) -> bytes:
        self.purge()
        f = self.cache / f"doc_{rcept_no}.zip"
        if f.exists() and time.time() - f.stat().st_mtime <= RETENTION:
            content = f.read_bytes()
        else:
            async def fetch() -> bytes:
                data = (await self._get("document.xml", rcept_no=rcept_no)).content
                self._zip_check(data)
                f.write_bytes(data)
                return data
            content = await self._once("doc:" + rcept_no, fetch)
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            names = sorted(z.namelist(), key=lambda n: (not n.startswith(rcept_no), len(n)))
            return z.read(names[0])  # 본문(접수번호.xml)이 첫 번째

    async def shareholders(self, corp_code: str, year: str) -> list[dict]:
        data = await self._json("hyslrSttus.json", corp_code=corp_code, bsns_year=year, reprt_code="11011")
        return data.get("list", [])

    async def full_statements(self, corp_code: str, year: str, reprt_code: str, fs_div: str) -> list[dict]:
        """단일회사 전체 재무제표 (XBRL, 상장사·일부 비상장사). fs_div: CFS 연결 / OFS 별도"""
        data = await self._json("fnlttSinglAcntAll.json", corp_code=corp_code, bsns_year=year, reprt_code=reprt_code, fs_div=fs_div)
        return data.get("list", [])

    async def share_count(self, corp_code: str, year: str, reprt_code: str) -> list[dict]:
        """주식의 총수 현황 (발행주식총수·자기주식·유통주식수)"""
        data = await self._json("stockTotqySttus.json", corp_code=corp_code, bsns_year=year, reprt_code=reprt_code)
        return data.get("list", [])

    async def financials(self, corp_code: str, year: str) -> list[dict]:
        data = await self._json("fnlttSinglAcnt.json", corp_code=corp_code, bsns_year=year, reprt_code="11011")
        return data.get("list", [])
