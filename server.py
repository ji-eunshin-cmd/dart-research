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

from dart_client import VIEWER, DartClient, DartError
from sections import parse, pick_annual, pick_audit

SECRET = os.environ.get("CONNECTOR_SECRET", "")
if len(SECRET) < 24 or not re.fullmatch(r"[A-Za-z0-9_-]+", SECRET):
    raise SystemExit(f"[시작 실패] CONNECTOR_SECRET 환경변수에 영문·숫자·_- 24자 이상을 넣어 주세요. (현재 {len(SECRET)}자)")

mcp = FastMCP(
    "DART Research",
    instructions="한국 DART 공시에서 회사개요·연혁·사업의 내용·최대주주·주요 재무를 가져옵니다. 먼저 search_company로 고유번호를 찾고 research_bundle을 부르세요.",
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


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    # Railway 헬스체크가 배포 직후 부르므로, 여기서 DART 회사 목록을 미리 받기 시작합니다.
    try:
        c = client()
        c.warm()
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
