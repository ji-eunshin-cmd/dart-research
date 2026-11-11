"""DART 응답 형식을 흉내 낸 가짜 서버로 MCP 프로토콜 전체(초기화 → 도구 목록 → 호출)를 검증합니다."""
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SECRET = "test_secret_0123456789abcdef"
os.environ["CONNECTOR_SECRET"] = SECRET
os.environ["DART_API_KEY"] = "dummy"

import server  # noqa: E402
from dart_client import DartClient  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

CORP_XML = """<?xml version="1.0" encoding="UTF-8"?><result>
<list><corp_code>00123456</corp_code><corp_name>가나다케미칼</corp_name><stock_code>123450</stock_code><modify_date>20260101</modify_date></list>
<list><corp_code>00999999</corp_name_bad></list>
<list><corp_code>00222222</corp_code><corp_name>가나다케미칼홀딩스</corp_name><stock_code> </stock_code><modify_date>20260101</modify_date></list>
<list><corp_code>00333333</corp_code><corp_name>(주)라마바</corp_name><stock_code> </stock_code><modify_date>20260101</modify_date></list>
</result>"""

DOC_XML = """<?xml version="1.0" encoding="utf-8"?>
<DOCUMENT><DOCUMENT-NAME ACODE="11011">사업보고서</DOCUMENT-NAME><BODY>
<SECTION-1><TITLE ATOC="Y">I. 회사의 개요</TITLE>
  <SECTION-2><TITLE ATOC="Y">1. 회사의 개요</TITLE>
    <P>가. 회사의 법적ㆍ상업적 명칭: 당사의 명칭은 주식회사 가나다케미칼입니다.</P>
    <P>나. 설립일자: 2008년 3월 15일 설립되었습니다.</P>
    <TABLE><TR><TH>구분</TH><TH>회사수</TH></TR><TR><TD>연결 종속회사</TD><TD>2</TD></TR></TABLE>
  </SECTION-2>
  <SECTION-2><TITLE ATOC="Y">2. 회사의 연혁</TITLE><P>2012.06 광양 제1공장 준공</P><P>2019.11 광양 제2공장 준공</P></SECTION-2>
</SECTION-1>
<SECTION-1><TITLE ATOC="Y">II. 사업의 내용</TITLE>
  <SECTION-2><TITLE ATOC="Y">1. 사업의 개요</TITLE><P>당사는 전자급 과산화수소를 생산합니다. 국내 시장규모는 2025년 1.2조원입니다.</P></SECTION-2>
  <SECTION-2><TITLE ATOC="Y">2. 주요 제품 및 서비스</TITLE><TABLE><TR><TD>전자급 H2O2</TD><TD>72.4%</TD></TR></TABLE></SECTION-2>
  <SECTION-2><TITLE ATOC="Y">3. 원재료 및 생산설비</TITLE><P>연간 생산능력 100,000톤</P></SECTION-2>
  <SECTION-2><TITLE ATOC="Y">7. 기타 참고사항</TITLE><P>가. 산업의 특성: 반도체 업황에 연동됩니다 &amp; 진입장벽이 높습니다.</P></SECTION-2>
</SECTION-1>
<SECTION-1><TITLE ATOC="Y">XII. 그 밖에 투자자 보호를 위하여 필요한 사항</TITLE><P>우발채무 없음</P></SECTION-1>
</BODY></DOCUMENT>"""

AUDIT_XML = """<?xml version="1.0" encoding="utf-8"?><DOCUMENT><BODY><SECTION-1><TITLE>감사보고서</TITLE>
<P>독립된 감사인의 감사보고서</P><P>주석</P><P>1. 일반사항</P><P>주식회사 라마바(이하 회사)는 2015년 설립되어 부산에서 플라스틱 사출품을 제조합니다.</P>
</SECTION-1></BODY></DOCUMENT>"""


def zbytes(name: str, data: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, data.encode("utf-8"))
    return buf.getvalue()


CALLS: list[str] = []


def handler(req: httpx.Request) -> httpx.Response:
    p, q = req.url.path.rsplit("/", 1)[-1], dict(req.url.params)
    CALLS.append(p)
    assert q["crtfc_key"] == "dummy"
    if p == "corpCode.xml":
        return httpx.Response(200, content=zbytes("CORPCODE.xml", CORP_XML))
    if p == "company.json":
        if q["corp_code"] == "00123456":
            return httpx.Response(200, json={"status": "000", "corp_name": "가나다케미칼", "corp_name_eng": "GANADA CHEMICAL", "ceo_nm": "김가상",
                                             "corp_cls": "Y", "stock_code": "123450", "adres": "전라남도 광양시 가상로 1", "est_dt": "20080315",
                                             "induty_code": "20119", "acc_mt": "12", "hm_url": "www.example.com"})
        if q["corp_code"] == "00333333":
            return httpx.Response(200, json={"status": "000", "corp_name": "라마바", "ceo_nm": "이가상", "corp_cls": "E", "stock_code": " ",
                                             "adres": "부산", "est_dt": "20150101", "induty_code": "22229", "acc_mt": "12"})
        return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})
    if p == "list.json":
        if q["corp_code"] == "00123456" and q["pblntf_ty"] == "A":
            return httpx.Response(200, json={"status": "000", "list": [
                {"rcept_no": "20250320000111", "report_nm": "사업보고서 (2024.12)", "rcept_dt": "20250320"},
                {"rcept_no": "20260318000222", "report_nm": "[기재정정]사업보고서 (2025.12)", "rcept_dt": "20260318"}]})
        if q["corp_code"] == "00333333" and q["pblntf_ty"] == "F":
            return httpx.Response(200, json={"status": "000", "list": [{"rcept_no": "20260401000333", "report_nm": "감사보고서 (2025.12)", "rcept_dt": "20260401"}]})
        return httpx.Response(200, json={"status": "013", "message": "없음"})
    if p == "document.xml":
        if q["rcept_no"] == "20260318000222":
            return httpx.Response(200, content=zbytes("20260318000222.xml", DOC_XML))
        if q["rcept_no"] == "20260401000333":
            return httpx.Response(200, content=zbytes("20260401000333.xml", AUDIT_XML))
    if p == "hyslrSttus.json":
        assert q["bsns_year"] == "2025" and q["reprt_code"] == "11011"
        return httpx.Response(200, json={"status": "000", "list": [
            {"nm": "가나다홀딩스", "relate": "최대주주", "stock_knd": "보통주", "trmend_posesn_stock_co": "5,100,000", "trmend_posesn_stock_qota_rt": "51.00"},
            {"nm": "가나다홀딩스", "relate": "최대주주", "stock_knd": "우선주", "trmend_posesn_stock_co": "100", "trmend_posesn_stock_qota_rt": "1.00"},
            {"nm": "계", "relate": "-", "stock_knd": "보통주", "trmend_posesn_stock_co": "5,100,000", "trmend_posesn_stock_qota_rt": "51.00"}]})
    if p == "fnlttSinglAcnt.json":
        return httpx.Response(200, json={"status": "000", "list": [
            {"fs_div": "OFS", "account_nm": "매출액", "thstrm_amount": "1", "frmtrm_amount": "1", "bfefrmtrm_amount": "1"},
            {"fs_div": "CFS", "account_nm": "매출액", "thstrm_amount": "123,400,000,000", "frmtrm_amount": "110,000,000,000", "bfefrmtrm_amount": "95,000,000,000"},
            {"fs_div": "CFS", "account_nm": "영업이익", "thstrm_amount": "15,000,000,000", "frmtrm_amount": "-", "bfefrmtrm_amount": "9,000,000,000"}]})
    return httpx.Response(200, json={"status": "100", "message": "bad"})


@pytest.fixture(scope="module")
def app_client():
    with TestClient(server.app) as c:  # MCP 세션 관리자는 프로세스당 한 번만 시작
        yield c


@pytest.fixture
def http(app_client, tmp_path):
    CALLS.clear()
    server._client = DartClient("dummy", tmp_path, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    yield app_client
    server._client = None


H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(c, method, params=None, id_=1):
    r = c.post(f"/{SECRET}/mcp", headers=H, json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
    assert r.status_code == 200, r.text
    return r.json()


def call(c, tool, args):
    rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    res = rpc(c, "tools/call", {"name": tool, "arguments": args}, 2)["result"]
    if not res.get("isError"):
        res["structuredContent"] = res.get("structuredContent") or json.loads(res["content"][0]["text"])
    return res


def test_wrong_secret_is_404(http):
    r = http.post("/wrong/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 404
    assert http.get("/health").json() == {"ok": True}


def test_tools_listed_read_only(http):
    rpc(http, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    tools = rpc(http, "tools/list", {}, 2)["result"]["tools"]
    assert {t["name"] for t in tools} == {"search_company", "research_bundle"}
    assert all(t["annotations"]["readOnlyHint"] for t in tools)


def test_search_ignores_corp_suffix_and_ranks(http):
    res = call(http, "search_company", {"name": "(주)가나다 케미칼"})
    names = [r["corp_name"] for r in res["structuredContent"]["results"]]
    assert names == ["가나다케미칼", "가나다케미칼홀딩스"]
    assert res["structuredContent"]["results"][0]["listed"] is True
    res = call(http, "search_company", {"name": "라마바"})
    assert res["structuredContent"]["results"][0]["corp_code"] == "00333333"
    call(http, "search_company", {"name": "가나다"})
    assert CALLS.count("corpCode.xml") == 1  # 회사 목록은 한 번만 받음


def test_bundle_annual_report(http):
    res = call(http, "research_bundle", {"corp_code": "00123456"})
    b = res["structuredContent"]
    assert not res.get("isError")
    assert b["report"]["rcept_no"] == "20260318000222"  # 정정공시 포함 최신
    assert [s["title"] for s in b["sources"]] == ["DART 기업개황", "2025 사업보고서"]
    assert b["sources"][1]["url"].endswith("rcpNo=20260318000222")
    s1, s2 = b["materials"][0]["text"], b["materials"][1]["text"]
    assert "설립일: 2008년 3월 15일" in s1 and "유가증권시장 상장" in s1
    for must in ["## 회사의 개요", "구분 | 회사수", "## 회사의 연혁", "2019.11 광양 제2공장", "## 사업의 개요", "1.2조원",
                 "## 주요 제품 및 서비스", "전자급 H2O2 | 72.4%", "## 원재료 및 생산설비", "## 기타 참고사항 (산업·시장)", "진입장벽",
                 "가나다홀딩스 | 최대주주 | 5,100,000주 | 51.00%", "매출액 | 950억원 | 1,100억원 | 1,234억원", "영업이익 | 90억원 | - | 150억원", "(연결, 억원)"]:
        assert must in s2, must
    assert "우선주" not in s2 and "우발채무" not in s2
    assert b["notes"] == []
    # 같은 공시는 원문을 다시 받지 않음
    call(http, "research_bundle", {"corp_code": "00123456"})
    assert CALLS.count("document.xml") == 1


def test_bundle_audit_only_company(http):
    b = call(http, "research_bundle", {"corp_code": "00333333"})["structuredContent"]
    assert b["report"]["kind"] == "audit" and b["sources"][1]["title"] == "2025 감사보고서"
    assert "플라스틱 사출품" in b["materials"][1]["text"]
    assert any("감사보고서만" in n for n in b["notes"])


def test_errors_are_tool_errors(http):
    res = call(http, "research_bundle", {"corp_code": "123"})
    assert res["isError"] and "8자리" in res["content"][0]["text"]
    res = call(http, "research_bundle", {"corp_code": "00000001"})
    assert res["isError"] and "기업개황이 없습니다" in res["content"][0]["text"]


def test_dart_limit_message(http, tmp_path):
    def limited(req):
        return httpx.Response(200, json={"status": "020", "message": "사용한도 초과"})
    server._client = DartClient("dummy", tmp_path / "x", httpx.AsyncClient(transport=httpx.MockTransport(limited)))
    res = call(http, "research_bundle", {"corp_code": "00123456"})
    assert res["isError"] and "2만 건" in res["content"][0]["text"]
