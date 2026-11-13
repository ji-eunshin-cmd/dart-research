"""DART Research — claude.ai 커스텀 커넥터(MCP 서버).

도구 2개 (모두 읽기 전용, LLM 호출 없음):
  search_company   회사명 → DART 고유번호 후보
  research_bundle  고유번호 → 기업개황 + 최근 사업보고서(없으면 감사보고서) 주요 장 + 최대주주 + 주요 재무
                   를 출처(S1, S2)별 자료로 묶어서 돌려줌. 리서치 아티팩트가 이 결과를 그대로 자료 칸에 넣습니다.

접근 제한: 커넥터 주소에 비밀값이 들어갑니다 → https://<도메인>/<CONNECTOR_SECRET>/mcp
환경변수: DART_API_KEY (필수), CONNECTOR_SECRET (필수, 24자 이상), CACHE_DIR (선택)
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import date
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

import asyncio

import httpx

from dart_client import VIEWER, DartClient, DartError
from finance import PERIOD_KO, PERIODS, compact_statements, ltm, metrics, prior_ytd, summarize, ytd
from sections import parse, pick_annual, pick_audit
from webpage import PageError, fetch as fetch_page

SECRET = os.environ.get("CONNECTOR_SECRET", "")
if len(SECRET) < 24 or not re.fullmatch(r"[A-Za-z0-9_-]+", SECRET):
    raise SystemExit(f"[시작 실패] CONNECTOR_SECRET 환경변수에 영문·숫자·_- 24자 이상을 넣어 주세요. (현재 {len(SECRET)}자)")

mcp = FastMCP(
    "DART Research",
    instructions=("한국 DART 공시를 읽습니다. 회사개요·산업 자료는 search_company → research_bundle, "
                  "공시 재무제표는 financial_statements, WACC 대용기업의 이자부부채·순차입금·장부가 D/E·발행주식수는 capital_structure(여러 회사 한 번에)를 쓰세요. "
                  "금액 단위는 원입니다. 이자부부채는 넣은 계정(items)과 검토할 계정(review_items)을 함께 사용자에게 보여 주세요. "
                  "사용자가 준 기사·보고서 링크의 본문은 fetch_url로 읽습니다(검색 기능은 없음). "
                  "리서치 JSON을 보고서 앱에 넣을 때는 report_status로 보고서를 확인하고 send_research로 보냅니다."),
    stateless_http=True,
    json_response=True,
    streamable_http_path=f"/{SECRET}/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_client: DartClient | None = None


def client() -> DartClient:
    global _client
    if _client is None:
        cache = Path(os.environ.get("CACHE_DIR") or Path(tempfile.gettempdir()) / "dart-cache")
        _client = DartClient(os.environ.get("DART_API_KEY", ""), cache)
    return _client


READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
CORP_CLS = {"Y": "유가증권시장 상장", "K": "코스닥 상장", "N": "코넥스 상장", "E": "비상장"}


def _ymd(s: str) -> str:
    s = (s or "").strip()
    return f"{s[:4]}년 {int(s[4:6])}월 {int(s[6:8])}일" if re.fullmatch(r"\d{8}", s) else s


def _num(s) -> int | None:
    try:
        return int(str(s).replace(",", "").strip())
    except ValueError:
        return None


@mcp.tool(annotations=READ)
async def search_company(name: str, limit: int = 10) -> dict:
    """회사명으로 DART 고유번호(corp_code)를 찾습니다. (주)·주식회사 표기와 공백은 무시하고, 정확히 일치 → 앞부분 일치 → 포함 순으로 최대 limit개를 돌려줍니다. listed=true는 상장사입니다."""
    try:
        found = await client().search(name, max(1, min(int(limit), 20)))
    except DartError as e:
        raise ToolError(str(e)) from e
    return {"query": name, "results": [c.as_dict() for c in found]}


@mcp.tool(annotations=READ)
async def research_bundle(corp_code: str, max_chars: int = 30000) -> dict:
    """DART 고유번호(8자리)로 평가보고서 회사개요·산업 장표에 필요한 자료를 한 번에 모읍니다: 기업개황(S1), 가장 최근 사업보고서의 회사의 개요·연혁·사업의 개요·주요 제품·생산설비·기타 참고사항(산업·시장)·최대주주와 주요 재무(S2). 사업보고서가 없으면 감사보고서 주석의 일반사항을 씁니다. 결과의 materials 배열이 출처별 본문, sources가 출처 목록입니다. max_chars는 본문 전체 글자 상한(5000~60000)."""
    if not re.fullmatch(r"\d{8}", corp_code or ""):
        raise ToolError("corp_code는 8자리 숫자입니다. search_company로 먼저 찾으세요.")
    budget = max(5000, min(int(max_chars), 60000))
    c = client()
    today = date.today().isoformat()
    notes: list[str] = []
    try:
        prof = await c.company(corp_code)
        if prof.get("status") == "013":
            raise ToolError("이 고유번호의 기업개황이 없습니다.")
        rep = await c.latest_report(corp_code)
    except DartError as e:
        raise ToolError(str(e)) from e

    name = prof.get("corp_name", "")
    profile_lines = [
        f"회사명: {name}" + (f" ({prof.get('corp_name_eng')})" if prof.get("corp_name_eng") else ""),
        f"대표이사: {prof.get('ceo_nm', '')}",
        f"설립일: {_ymd(prof.get('est_dt', ''))}",
        f"본점 소재지: {prof.get('adres', '')}",
        f"상장 구분: {CORP_CLS.get(prof.get('corp_cls', ''), prof.get('corp_cls', ''))}" + (f" (종목코드 {prof.get('stock_code')})" if prof.get("stock_code", "").strip() else ""),
        f"업종코드(KSIC): {prof.get('induty_code', '')}",
        f"결산월: {prof.get('acc_mt', '')}월",
    ]
    if prof.get("hm_url"):
        profile_lines.append(f"홈페이지: {prof['hm_url']}")
    sources = [{"id": "S1", "title": "DART 기업개황", "type": "dart", "url": "https://dart.fss.or.kr/", "accessed": today}]
    materials = [{"source_id": "S1", "title": "DART 기업개황", "type": "dart", "url": "https://dart.fss.or.kr/", "text": "\n".join(profile_lines)}]

    if rep is None:
        notes.append("최근 3년 안에 사업보고서·감사보고서가 없습니다. 회사 제공 자료(IR, 회사소개서)를 추가해 주세요.")
        return {"corp_code": corp_code, "corp_name": name, "report": None, "sources": sources, "materials": materials, "notes": notes}

    rno, rnm, rdt = rep["rcept_no"], rep.get("report_nm", "").strip(), rep.get("rcept_dt", "")
    m = re.search(r"\((\d{4})\.(\d{2})\)", rnm)
    fy = m.group(1) if m else str(int(rdt[:4]) - 1 if rdt else date.today().year - 1)
    label = f"{fy} {'사업보고서' if rep['kind'] == 'annual' else '감사보고서'}"
    url = VIEWER.format(rno)
    sources.append({"id": "S2", "title": label, "type": "dart", "url": url, "accessed": today})

    parts: list[str] = []
    try:
        xml = await c.document_xml(rno)
        secs, full = parse(xml)
        picked = pick_annual(secs, budget - 4000) if rep["kind"] == "annual" else []
        if not picked:
            picked = pick_audit(full, budget - 4000)
            if rep["kind"] == "annual":
                notes.append("사업보고서에서 장 제목을 찾지 못해 본문 앞부분을 넣었습니다.")
        for p in picked:
            parts.append(f"## {p['label']}\n{p['text']}")
    except DartError as e:
        notes.append(f"공시 원문을 가져오지 못했습니다: {e}")

    if rep["kind"] == "annual":
        try:
            rows = await c.shareholders(corp_code, fy)
            lines = []
            for r in rows:
                if "보통" not in (r.get("stock_knd") or "보통"):
                    continue
                if r.get("nm") != "계" and (_num(r.get("trmend_posesn_stock_co")) or 0) == 0:
                    continue  # 보유 주식이 없는 특수관계인은 생략
                lines.append(f"{r.get('nm', '')} | {r.get('relate', '') or '-'} | {r.get('trmend_posesn_stock_co', '')}주 | {r.get('trmend_posesn_stock_qota_rt', '')}%")
            if lines:
                parts.append(f"## 최대주주 및 특수관계인 (보통주, {fy}년말)\n성명 | 관계 | 주식수 | 지분율\n" + "\n".join(lines))
        except DartError as e:
            notes.append(f"최대주주 현황을 가져오지 못했습니다: {e}")
        try:
            rows = await c.financials(corp_code, fy)
            fs = [r for r in rows if r.get("fs_div") == "CFS"] or [r for r in rows if r.get("fs_div") == "OFS"]
            want = ("매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계")
            lines = []
            for r in fs:
                if r.get("account_nm") in want:
                    vals = [_num(r.get(k)) for k in ("bfefrmtrm_amount", "frmtrm_amount", "thstrm_amount")]
                    lines.append(r["account_nm"] + " | " + " | ".join(f"{v / 1e8:,.0f}억원" if v is not None else "-" for v in vals))
            if lines:
                y = int(fy)
                kind = "연결" if fs and fs[0].get("fs_div") == "CFS" else "별도"
                parts.append(f"## 주요 재무 ({kind}, 억원)\n계정 | {y - 2} | {y - 1} | {y}\n" + "\n".join(lines))
        except DartError as e:
            notes.append(f"주요 재무를 가져오지 못했습니다: {e}")
    else:
        notes.append("상장·대형 비상장사가 아니라 감사보고서만 있습니다. 연혁·주주·산업 내용이 부족하면 회사 제공 자료를 추가해 주세요.")

    materials.append({"source_id": "S2", "title": label, "type": "dart", "url": url,
                      "text": f"[{name} {label}, 접수번호 {rno}, 접수일 {rdt}]\n\n" + "\n\n".join(parts)})
    return {"corp_code": corp_code, "corp_name": name,
            "report": {"rcept_no": rno, "report_nm": rnm, "rcept_dt": rdt, "kind": rep["kind"], "url": url},
            "sources": sources, "materials": materials, "notes": notes}


async def _resolve(c: DartClient, ref: str) -> tuple[str | None, str | None, str | None]:
    """고유번호 8자리 또는 회사명 → (corp_code, corp_name, 안내문)."""
    ref = (ref or "").strip()
    if re.fullmatch(r"\d{8}", ref):
        prof = await c.company(ref)  # 하루 캐시
        if prof.get("status") == "013":
            return None, None, f"고유번호 {ref}에 해당하는 회사가 없습니다."
        return ref, prof.get("corp_name"), None
    hits = await c.search(ref, 3)
    if not hits:
        return None, None, f"'{ref}'을(를) DART에서 찾지 못했습니다."
    h = hits[0]
    note = None if len(hits) == 1 or h.corp_name.replace(" ", "") == ref.replace(" ", "") else \
        f"'{ref}' → '{h.corp_name}'({h.corp_code})로 골랐습니다. 다른 회사면 고유번호로 다시 요청하세요."
    return h.corp_code, h.corp_name, note


async def _statements(c: DartClient, corp_code: str, year: int, period: str, consolidated: bool) -> tuple[list[dict], str, list[str]]:
    notes = []
    order = ["CFS", "OFS"] if consolidated else ["OFS", "CFS"]
    for i, fs in enumerate(order):
        rows = await c.full_statements(corp_code, str(year), PERIODS[period], fs)
        if rows:
            if i:
                notes.append(f"{'연결' if order[0] == 'CFS' else '별도'} 재무제표가 없어 {'연결' if fs == 'CFS' else '별도'} 기준으로 가져왔습니다.")
            return rows, fs, notes
    raise ToolError(f"{year}년 {PERIOD_KO[period]} 재무제표가 DART에 없습니다(아직 미공시이거나 XBRL 재무제표가 없는 회사).")


def _check_period(year: int, period: str) -> None:
    if period not in PERIODS:
        raise ToolError("period는 annual, q3, half, q1 중 하나입니다.")
    if not 2015 <= int(year) <= date.today().year:
        raise ToolError("year는 2015년 이후 사업연도입니다.")


@mcp.tool(annotations=READ)
async def financial_statements(company: str, year: int, period: str = "annual", consolidated: bool = True,
                               statements: str = "BS,IS") -> dict:
    """한 회사의 공시 재무제표(XBRL) 전체 계정을 가져옵니다. company는 회사명 또는 고유번호 8자리, year는 사업연도,
    period는 annual(사업보고서)·q3·half·q1, consolidated=True면 연결(없으면 별도로 대체), statements는 BS·IS·CIS·CF·SCE 중 쉼표로.
    금액 단위는 원이며 current=당기, prior=전기(분기·반기 손익은 누적)."""
    _check_period(year, period)
    c = client()
    try:
        code, name, note = await _resolve(c, company)
        if not code:
            raise ToolError(note)
        rows, fs, notes = await _statements(c, code, year, period, consolidated)
    except DartError as e:
        raise ToolError(str(e)) from e
    sts = [x.strip().upper() for x in statements.split(",") if x.strip()]
    rno = rows[0].get("rcept_no", "")
    return {"corp_code": code, "corp_name": name or rows[0].get("corp_name", ""), "year": year, "period": PERIOD_KO[period],
            "basis": "연결" if fs == "CFS" else "별도", "unit": "원", "rcept_no": rno, "url": VIEWER.format(rno) if rno else None,
            "notes": [n for n in [note, *notes] if n], "rows": compact_statements(rows, sts)}


async def _latest(c: DartClient, code: str, consolidated: bool) -> tuple[list[dict], str, list[str], int, str, dict]:
    """오늘 기준 가장 최근에 공시된 정기보고서의 재무제표. 결산월(acc_mt)로 보고서 종류를 판별하고,
    XBRL이 아직 없으면 그 이전 보고서로 내려감."""
    try:
        acc = int((await c.company(code)).get("acc_mt") or 12)
    except (DartError, ValueError):
        acc = 12
    kinds = {0: "annual", 3: "q1", 6: "half", 9: "q3"}
    seen, tried = set(), []
    for it in await c.periodic_reports(code):
        m = re.search(r"\((\d{4})\.(\d{2})\)", it.get("report_nm", ""))
        if not m:
            continue
        yy, mm = int(m.group(1)), int(m.group(2))
        period = kinds.get((mm - acc) % 12)
        if not period or (yy, mm) in seen:
            continue
        seen.add((yy, mm))
        tried.append(it["report_nm"].strip())
        for year in (yy, yy - 1) if acc != 12 else (yy,):
            try:
                rows, fs, notes = await _statements(c, code, year, period, consolidated)
                return rows, fs, notes, year, period, it
            except ToolError:
                continue
        if len(seen) >= 3:
            break
    raise ToolError("최근 정기보고서의 XBRL 재무제표를 찾지 못했습니다" + (f"(확인한 보고서: {', '.join(tried)})" if tried else "(최근 1년 반 정기공시 없음)") + ".")


async def _metrics_for(c: DartClient, code: str, rows: list[dict], fs: str, year: int, period: str, consolidated: bool) -> dict:
    """상대가치 평가용 주요 재무 지표: 직전 연간과 LTM(직전 연간 + 당기 누적 − 전기 동기 누적)."""
    notes = []
    if period == "annual":
        a = metrics(rows)
        return {"annual": {"label": f"{year} 연간", **a}, "ltm": {"label": f"{year} 연간 (최근 보고서가 사업보고서)", **a}, "notes": notes}
    try:
        arows, afs, _ = await _statements(c, code, year - 1, "annual", consolidated)
    except ToolError:
        return {"annual": None, "ltm": None, "notes": [f"{year - 1}년 사업보고서 재무제표가 없어 연간·LTM을 계산하지 못했습니다."]}
    if afs != fs:
        notes.append(f"연간은 {'연결' if afs == 'CFS' else '별도'}, 최근 보고서는 {'연결' if fs == 'CFS' else '별도'} 기준이라 LTM이 섞였습니다.")
    a, cy, py = metrics(arows), metrics(rows, ytd), metrics(rows, prior_ytd)
    lt = ltm(a, cy, py)
    if a.get("da") is None or cy.get("da") is None:
        notes.append("감가상각비가 재무제표 본문에 없습니다. 주석(유형·무형·사용권자산 변동)에서 확인하세요.")
    rno = (arows[0].get("rcept_no") if arows else "") or ""
    return {"annual": {"label": f"{year - 1} 연간", "rcept_no": rno, "url": VIEWER.format(rno) if rno else None, **a},
            "ltm": {"label": f"LTM ({year - 1} 연간 + {year} {PERIOD_KO[period]} 누적 − {year - 1} 동기 누적)",
                    "cur_label": f"{year} {PERIOD_KO[period]} 누적", "prior_label": f"{year - 1} 동기 누적", **lt}, "notes": notes}


async def _one_capital(c: DartClient, ref: str, year: int | None, period: str, consolidated: bool, include_lease: bool,
                       include_metrics: bool = True) -> dict:
    try:
        code, name, note = await _resolve(c, ref)
        if not code:
            return {"input": ref, "error": note}
        filed = None
        if period == "latest":
            rows, fs, notes, year, period, it = await _latest(c, code, consolidated)
            filed = it.get("rcept_dt")
        else:
            rows, fs, notes = await _statements(c, code, year, period, consolidated)
        s = summarize(rows, include_lease)
        shares = {}
        try:
            for r in await c.share_count(code, str(year), PERIODS[period]):  # 고른 보고서와 같은 시점
                if (r.get("se") or "").replace(" ", "") in ("보통주", "합계"):
                    key = "common" if "보통" in r.get("se", "") else "total"
                    shares[key] = {"issued": _num(r.get("istc_totqy")), "treasury": _num(r.get("tesstk_co")),
                                   "outstanding": _num(r.get("distb_stock_co"))}
        except DartError as e:
            notes.append(f"주식 수를 가져오지 못했습니다: {e}")
        if s["interest_bearing_debt"]["approximate"]:
            notes.append("[주석 확인 필요] " + s["interest_bearing_debt"]["approximate_reason"])
        if s["interest_bearing_debt"]["review_items"]:
            notes.append("review_items의 조정·우선주·파생 계정은 합산하지 않았습니다. 필요하면 반영하세요.")
        if s["totals"]["equity"] is None:
            notes.append("자본총계 계정을 찾지 못했습니다. rows로 직접 확인하세요.")
        mets = None
        if include_metrics:
            try:
                mets = await _metrics_for(c, code, rows, fs, year, period, consolidated)
            except DartError as e:
                mets = {"annual": None, "ltm": None, "notes": [f"주요 재무 지표를 가져오지 못했습니다: {e}"]}
        rno = rows[0].get("rcept_no", "")
        return {"input": ref, "corp_code": code, "corp_name": name or rows[0].get("corp_name", ""), "basis": "연결" if fs == "CFS" else "별도",
                "report": f"{year} {PERIOD_KO[period]}", "year": year, "period": period,
                "filed": f"{filed[:4]}-{filed[4:6]}-{filed[6:8]}" if filed else None, "rcept_no": rno, "url": VIEWER.format(rno) if rno else None, "unit": "원",
                **s, "shares": shares, "metrics": mets, "notes": [n for n in [note, *notes] if n]}
    except ToolError as e:
        return {"input": ref, "error": str(e)}
    except DartError as e:
        return {"input": ref, "error": str(e)}


@mcp.tool(annotations=READ)
async def capital_structure(companies: list[str], year: int | None = None, period: str = "latest", consolidated: bool = True,
                            include_lease: bool = True, include_metrics: bool = True) -> dict:
    """WACC 대용기업용: 여러 회사(최대 15곳, 회사명 또는 고유번호)의 공시 재무제표에서 이자부부채(단기·유동성장기·장기차입금, 사채, 기업어음, 리스부채 선택)를
    계정 근거와 함께 합산하고 현금, 순차입금, 부채총계, 자본총계, 비지배지분, 부채비율, 장부가 D/E, 유효세율, 금융원가, 발행·자기·유통 보통주식수를 돌려줍니다.
    include_metrics=True(기본)면 상대가치 평가용 주요 재무 지표(매출액, 감가상각비(무형·사용권자산 상각 포함), 영업이익, 법인세비용, 지배주주귀속순이익)를
    직전 연간과 LTM(직전 연간 + 당기 누적 − 전기 동기 누적)으로 companies[].metrics에 함께 돌려줍니다.
    period='latest'(기본)면 회사마다 오늘 기준 가장 최근에 공시된 정기보고서(사업·반기·분기)를 골라 쓰고 year는 무시합니다.
    순차입금 = 이자부부채(리스 포함 여부는 include_lease) − 현금및현금성자산.
    시가 D/E는 이자부부채 ÷ (평가기준일 주가 × 유통주식수)로 계산하세요(주가는 이 도구에 없음). 금액 단위 원."""
    if period != "latest":
        if year is None:
            raise ToolError("period를 직접 지정할 때는 year도 넣어 주세요.")
        _check_period(year, period)
    refs = [r for r in dict.fromkeys(x.strip() for x in companies) if r][:15]
    if not refs:
        raise ToolError("companies에 회사명이나 고유번호를 넣어 주세요.")
    c = client()
    sem = asyncio.Semaphore(4)

    async def run(ref):
        async with sem:
            return await _one_capital(c, ref, year, period, consolidated, include_lease, include_metrics)

    results = await asyncio.gather(*(run(r) for r in refs))
    table = []
    for r in results:
        if "error" in r:
            table.append({"company": r["input"], "error": r["error"]})
            continue
        t, d = r["totals"], r["interest_bearing_debt"]
        m = lambda v: round(v / 1e6) if v is not None else None
        table.append({"company": r["corp_name"], "report": r["report"], "basis": r["basis"], "이자부부채_백만원": m(d["total"]), "현금_백만원": m(t["cash"]),
                      "순차입금_백만원": m(r["net_debt"]), "부채총계_백만원": m(t["liabilities"]), "자본총계_백만원": m(t["equity"]), "비지배지분_백만원": m(t.get("nci")),
                      "부채비율_%": r["ratios"]["debt_ratio_pct"], "장부가DE_%": r["ratios"]["book_d_e_pct"],
                      "유효세율_%": r["ratios"]["effective_tax_rate_pct"],
                      "자기주식수": (r["shares"].get("common") or {}).get("treasury"),
                      "유통보통주식수": (r["shares"].get("common") or {}).get("outstanding"),
                      "주석확인필요": d["approximate"]})
    return {"year": year, "period": "회사별 최근 정기보고서" if period == "latest" else PERIOD_KO[period], "lease_included": include_lease,
            "summary_table": table, "companies": results}


@mcp.tool(annotations=READ)
async def fetch_url(url: str, max_chars: int = 15000) -> dict:
    """사용자가 고른 기사·보고서 링크 하나의 본문을 읽어 옵니다(HTML 기사, 글자가 든 PDF). 제목·언론사(site)·게재일·본문(text)을 돌려줍니다. 네이버 뉴스는 기사 본문만 골라냅니다. 로그인·유료 페이지나 화면이 뜬 뒤 본문을 불러오는 사이트는 본문이 비어 notes에 안내가 들어갑니다. 검색은 하지 않습니다."""
    try:
        return await fetch_page(url, client().http, max(1000, min(int(max_chars), 40000)))
    except PageError as e:
        raise ToolError(str(e))
    except httpx.HTTPError as e:
        raise ToolError(f"페이지에 연결하지 못했습니다: {type(e).__name__}")


# ---------------------------------------------------------------- 보고서 앱으로 리서치 보내기
REPORT_APP_URL = os.environ.get("REPORT_APP_URL", "").rstrip("/")
RESEARCH_TOKEN = os.environ.get("RESEARCH_TOKEN", "")


async def _report_app(method: str, report_id: str, body: dict | None = None) -> dict:
    if not REPORT_APP_URL or len(RESEARCH_TOKEN) < 24:
        raise ToolError("커넥터에 REPORT_APP_URL·RESEARCH_TOKEN이 설정되지 않아 보고서 앱으로 보낼 수 없습니다.")
    rid = (report_id or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", rid):
        raise ToolError("보고서 ID는 보고서 앱 화면의 64자리 영문 소문자·숫자입니다.")
    try:
        r = await client().http.request(method, f"{REPORT_APP_URL}/api/reports/{rid}/research", json=body,
                                        headers={"X-Research-Token": RESEARCH_TOKEN}, timeout=httpx.Timeout(90.0, connect=10.0))
    except httpx.HTTPError as e:
        raise ToolError(f"보고서 앱에 연결하지 못했습니다: {type(e).__name__}")
    try:
        data = r.json()
    except ValueError:
        raise ToolError(f"보고서 앱 응답을 읽지 못했습니다(HTTP {r.status_code}). 앱에 리서치 받기 기능이 배포됐는지 확인하세요.")
    if r.status_code >= 400 or not data.get("ok"):
        msg = data.get("error") or " / ".join(data.get("errors") or []) or f"HTTP {r.status_code}"
        raise ToolError(msg)
    return data


@mcp.tool(annotations=READ)
async def report_status(report_id: str) -> dict:
    """보고서 앱의 보고서(64자리 ID)가 살아 있는지, 회사명·보고서 유형, 이미 받은 리서치가 있는지 확인합니다."""
    return await _report_app("GET", report_id)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def send_research(report_id: str, research: dict) -> dict:
    """리서치 아티팩트가 만든 회사개요 리서치 JSON(통일 양식 v2)을 보고서 앱의 해당 보고서에 넣습니다.
    이미 만든 보고서 PPT가 있으면 바로 채우고, 없으면 보고서 생성 때 채웁니다. 같은 보고서에 다시 보내면 덮어씁니다.
    커넥터는 JSON을 저장하지 않고 전달만 합니다(보고서 앱에서 24시간 뒤 보고서와 함께 삭제)."""
    if not isinstance(research, dict):
        raise ToolError("research는 JSON 객체여야 합니다.")
    return await _report_app("POST", report_id, research)


# ---------------------------------------------------------------- 화면 코드 배포 (팀원 아티팩트 자동 업데이트)
UI_DIR = Path(__file__).resolve().parent / "ui"


@mcp.tool(annotations=READ)
async def ui_bundle(name: str = "research") -> dict:
    """팀원 계정의 '껍데기' 아티팩트가 열릴 때 최신 화면 코드(HTML)를 받아 갑니다. name: research(회사개요 리서치·대용기업 자본구조)."""
    import hashlib
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", name or ""):
        raise ToolError("화면 이름이 올바르지 않습니다.")
    path = UI_DIR / f"{name}.html"
    if not path.is_file():
        raise ToolError(f"'{name}' 화면이 서버에 없습니다.")
    html = path.read_text(encoding="utf-8")
    return {"name": name, "version": hashlib.sha256(html.encode()).hexdigest()[:12], "bytes": len(html.encode()), "html": html}


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    # Railway 헬스체크가 배포 직후 부르므로, 여기서 DART 회사 목록을 미리 받기 시작합니다.
    try:
        c = client()
        c.warm()
        c.purge()
        ready = c._corps is not None
    except Exception:
        ready = False
    return JSONResponse({"ok": True, "company_list_ready": ready})


app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    print(f"DART Research 시작: 0.0.0.0:{port} (MCP 경로 /<CONNECTOR_SECRET>/mcp, 키 {'있음' if os.environ.get('DART_API_KEY') else '없음'})", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")
