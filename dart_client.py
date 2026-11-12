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
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
from lxml import etree

BASE = "https://opendart.fss.or.kr/api"
VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"
CORP_LIST_TTL = 7 * 86400  # 회사 목록(DART 공개 고유번호 목록)은 일주일에 한 번 새로 받음
RETENTION = 24 * 3600      # 그 밖의 캐시(공시 원문·API 응답)는 24시간 안에 지움

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

    def purge(self, force: bool = False) -> None:
        """24시간 지난 공시 원문 파일과 메모리 캐시를 지움 (10분마다 한 번 확인)."""
        now = time.time()
        if not force and now - self._last_purge < 600:
            return
        self._last_purge = now
        for k in [k for k, (t, _) in self._json_cache.items() if now - t > RETENTION]:
            self._json_cache.pop(k, None)
        for p in self.cache.glob("doc_*.zip"):
            try:
                if now - p.stat().st_mtime > RETENTION:
                    p.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------ 저수준
    async def _get(self, path: str, **params) -> httpx.Response:
        self.purge()
        try:
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
        data = (await self._get(path, **params)).json()
        st = data.get("status")
        if st == "013":  # 조회된 데이터 없음
            data = {"status": "013", "list": []}
        elif st != "000":
            raise DartError(STATUS_MSG.get(st, f"DART 오류 {st}: {data.get('message', '')}"))
        self._json_cache[ck] = (time.time(), data)
        if len(self._json_cache) > 2000:
            self._json_cache.pop(next(iter(self._json_cache)))
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
            content = (await self._get("document.xml", rcept_no=rcept_no)).content
            if not content.startswith(b"PK"):
                self._zip_error(content)
            f.write_bytes(content)
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
