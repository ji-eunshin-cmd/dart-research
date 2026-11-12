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
<P>독립된 감사인의 감사보고서</P><P>회사의 개요를 포함한 재무제표에 대해 감사하였습니다.</P><P>주석</P><P>1. 일반사항</P><P>주식회사 라마바(이하 회사)는 2015년 설립되어 부산에서 플라스틱 사출품을 제조합니다.</P>
<TABLE><TR><TD>주주명</TD><TD>주식수</TD><TD>지분율(%)</TD></TR><TR><TD>이가상</TD><TD>100,000</TD><TD>100.0</TD></TR></TABLE>
<P>2. 재무제표의 작성기준</P><P>회사는 한국채택국제회계기준에 따라 재무제표를 작성하였습니다.</P><P>3. 유의적인 회계정책</P>
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
        if q["corp_code"] == "00222222":
            return httpx.Response(200, json={"status": "000", "corp_name": "가나다케미칼홀딩스", "corp_cls": "E"})
        if q["corp_code"] == "00333333":
            return httpx.Response(200, json={"status": "000", "corp_name": "라마바", "ceo_nm": "이가상", "corp_cls": "E", "stock_code": " ",
                                             "adres": "부산", "est_dt": "20150101", "induty_code": "22229", "acc_mt": "12"})
        return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})
    if p == "list.json":
        if q["corp_code"] == "00123456" and q["pblntf_ty"] == "A":
            return httpx.Response(200, json={"status": "000", "list": [
                {"rcept_no": "20250320000111", "report_nm": "사업보고서 (2024.12)", "rcept_dt": "20250320"},
                {"rcept_no": "20260318000222", "report_nm": "[기재정정]사업보고서 (2025.12)", "rcept_dt": "20260318"},
                {"rcept_no": "20261110000444", "report_nm": "분기보고서 (2026.09)", "rcept_dt": "20261110"}]})
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
    if p == "fnlttSinglAcntAll.json":
        if q["corp_code"] == "00123456" and q["fs_div"] == "CFS" and q["bsns_year"] == "2025":
            R = lambda sj, aid, nm, cur, prior="": {"sj_div": sj, "account_id": aid, "account_nm": nm, "thstrm_amount": cur,
                                                     "frmtrm_amount": prior, "rcept_no": "20260318000222", "corp_name": "가나다케미칼"}
            return httpx.Response(200, json={"status": "000", "list": [
                R("BS", "ifrs-full_CashAndCashEquivalents", "현금및현금성자산", "30000000000", "20000000000"),
                R("BS", "ifrs-full_RightofuseAssets", "사용권자산", "9000000000"),
                R("BS", "ifrs-full_ShorttermBorrowings", "단기차입금", "40000000000"),
                R("BS", "-표준계정코드 미사용-", "유동성장기부채", "10000000000"),
                R("BS", "ifrs-full_CurrentLeaseLiabilities", "유동리스부채", "2000000000"),
                R("BS", "-표준계정코드 미사용-", "전환사채", "15000000000"),
                R("BS", "-표준계정코드 미사용-", "전환권조정", "-1000000000"),
                R("BS", "-표준계정코드 미사용-", "미지급사채이자", "300000000"),
                R("BS", "ifrs-full_LongtermBorrowings", "장기차입금", "25000000000"),
                R("BS", "ifrs-full_NoncurrentLeaseLiabilities", "비유동리스부채", "5000000000"),
                R("BS", "ifrs-full_Liabilities", "부채총계", "150000000000"),
                R("BS", "ifrs-full_Equity", "자본총계", "200000000000"),
                R("IS", "ifrs-full_Revenue", "매출액", "123400000000", "110000000000"),
                R("IS", "ifrs-full_FinanceCosts", "금융원가", "4000000000"),
                R("IS", "ifrs-full_ProfitLossBeforeTax", "법인세비용차감전순이익", "20000000000"),
                R("IS", "ifrs-full_IncomeTaxExpenseContinuingOperations", "법인세비용", "4400000000"),
                R("CF", "x", "영업활동현금흐름", "1"),
            ]})
        if q["corp_code"] == "00222222" and q["fs_div"] == "CFS":
            R = lambda aid, nm, cur: {"sj_div": "BS", "account_id": aid, "account_nm": nm, "thstrm_amount": cur, "rcept_no": "1", "corp_name": ""}
            return httpx.Response(200, json={"status": "000", "list": [
                R("ifrs-full_CurrentFinancialLiabilities", "단기금융부채", "700"), R("ifrs-full_NoncurrentFinancialLiabilities", "장기금융부채", "300"),
                R("dart_CurrentDerivativeLiabilities", "유동파생상품부채", "50"), R("ifrs-full_CurrentFinancialAssets", "단기금융자산", "999"),
                R("ifrs-full_Liabilities", "부채총계", "2000"), R("ifrs-full_Equity", "자본총계", "4000")]})
        if q["corp_code"] == "00333333" and q["fs_div"] == "OFS" and q["bsns_year"] == "2025":
            return httpx.Response(200, json={"status": "000", "list": [
                {"sj_div": "BS", "account_id": "ifrs-full_Liabilities", "account_nm": "부채총계", "thstrm_amount": "500", "rcept_no": "20260401000333", "corp_name": "라마바"},
                {"sj_div": "BS", "account_id": "ifrs-full_Equity", "account_nm": "자본총계", "thstrm_amount": "1000", "rcept_no": "20260401000333", "corp_name": "라마바"},
                {"sj_div": "BS", "account_id": "x", "account_nm": "차입금", "thstrm_amount": "200", "rcept_no": "20260401000333", "corp_name": "라마바"}]})
        return httpx.Response(200, json={"status": "013", "message": "없음"})
    if p == "stockTotqySttus.json":
        return httpx.Response(200, json={"status": "000", "list": [
            {"se": "보통주", "istc_totqy": "10,000,000", "tesstk_co": "500,000", "distb_stock_co": "9,500,000"},
            {"se": "우선주", "istc_totqy": "100", "tesstk_co": "0", "distb_stock_co": "100"},
            {"se": "합계", "istc_totqy": "10,000,100", "tesstk_co": "500,000", "distb_stock_co": "9,500,100"}]})
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
    assert http.get("/health").json()["ok"] is True


def test_tools_listed_read_only(http):
    rpc(http, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    tools = rpc(http, "tools/list", {}, 2)["result"]["tools"]
    assert {t["name"] for t in tools} == {"search_company", "research_bundle", "financial_statements", "capital_structure", "fetch_url"}
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
    t = b["materials"][1]["text"]
    assert "플라스틱 사출품" in t and "이가상 | 100,000 | 100.0" in t
    assert "작성기준" not in t and "유의적인 회계정책" not in t and "감사하였습니다" not in t
    assert any("감사보고서만" in n for n in b["notes"])


def test_industry_section_kept_under_tight_budget(http):
    b = call(http, "research_bundle", {"corp_code": "00123456", "max_chars": 5000})["structuredContent"]
    t = b["materials"][1]["text"]
    assert "## 기타 참고사항 (산업·시장)" in t and "## 사업의 개요" in t
    assert t.index("## 회사의 개요") < t.index("## 사업의 개요") < t.index("## 기타 참고사항")  # 문서 순서 유지


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


def test_capital_structure_debt_classification(http):
    b = call(http, "capital_structure", {"companies": ["가나다케미칼", "00333333", "없는회사"], "year": 2025, "period": "annual"})["structuredContent"]
    a = b["companies"][0]
    d = a["interest_bearing_debt"]
    types = {i["account"]: i["type"] for i in d["items"]}
    assert types == {"단기차입금": "단기차입금", "유동성장기부채": "유동성장기차입금", "전환사채": "사채", "장기차입금": "장기차입금",
                     "유동리스부채": "리스부채", "비유동리스부채": "리스부채"}
    assert d["total"] == 97_000_000_000 and d["lease_total"] == 7_000_000_000
    assert [i["account"] for i in d["review_items"]] == ["전환권조정"]  # 합산 안 하고 검토로
    assert a["net_debt"] == 67_000_000_000
    assert a["ratios"] == {"debt_ratio_pct": 75.0, "book_d_e_pct": 48.5, "net_d_e_pct": 33.5, "effective_tax_rate_pct": 22.0}
    assert a["totals"]["finance_costs"] == 4_000_000_000
    assert a["shares"]["common"] == {"issued": 10_000_000, "treasury": 500_000, "outstanding": 9_500_000}
    assert a["basis"] == "연결" and a["url"].endswith("20260318000222")
    # 연결이 없으면 별도로, 못 찾은 회사는 오류만 그 회사에
    r2 = b["companies"][1]
    assert r2["basis"] == "별도" and r2["interest_bearing_debt"]["total"] == 200 and any("별도 기준" in n for n in r2["notes"])
    assert "찾지 못했습니다" in b["companies"][2]["error"]
    row = b["summary_table"][0]
    assert row["이자부부채_백만원"] == 97_000 and row["장부가DE_%"] == 48.5 and row["유통보통주식수"] == 9_500_000


def test_capital_structure_without_lease(http):
    a = call(http, "capital_structure", {"companies": ["00123456"], "year": 2025, "include_lease": False, "period": "annual"})["structuredContent"]["companies"][0]
    assert a["interest_bearing_debt"]["total"] == 90_000_000_000 and a["interest_bearing_debt"]["lease_total"] == 7_000_000_000


def test_financial_statements_and_missing_year(http):
    b = call(http, "financial_statements", {"company": "00123456", "year": 2025, "statements": "BS"})["structuredContent"]
    assert b["basis"] == "연결" and b["unit"] == "원" and all(r["st"] == "BS" for r in b["rows"])
    assert {"st": "BS", "ord": 0, "account": "단기차입금", "id": "ifrs-full_ShorttermBorrowings", "current": 40_000_000_000, "prior": None} in b["rows"]
    res = call(http, "financial_statements", {"company": "00123456", "year": 2024})
    assert res["isError"] and "재무제표가 DART에 없습니다" in res["content"][0]["text"]
    res = call(http, "financial_statements", {"company": "00123456", "year": 2025, "period": "monthly"})
    assert res["isError"] and "annual" in res["content"][0]["text"]


def test_aggregated_financial_liabilities_flagged(http):
    b = call(http, "capital_structure", {"companies": ["00222222"], "year": 2025, "period": "annual"})["structuredContent"]
    a = b["companies"][0]
    d = a["interest_bearing_debt"]
    assert a["corp_name"] == "가나다케미칼홀딩스"  # 고유번호로 불러도 회사명이 채워짐
    assert [(i["account"], i["type"]) for i in d["items"]] == [("단기금융부채", "금융부채(묶음)"), ("장기금융부채", "금융부채(묶음)")]
    assert d["total"] == 1000 and d["approximate"] is True
    assert any(n.startswith("[주석 확인 필요]") for n in a["notes"])
    assert b["summary_table"][0]["주석확인필요"] is True and b["summary_table"][0]["company"] == "가나다케미칼홀딩스"


def test_capital_latest_report_falls_back_to_annual(http):
    # 가장 최근 분기보고서(2026.09)는 XBRL이 없어 그 이전 사업보고서(2025.12)로 내려감
    r = call(http, "capital_structure", {"companies": ["00123456"]})["structuredContent"]
    a = r["companies"][0]
    assert r["period"] == "회사별 최근 정기보고서"
    assert a["report"] == "2025 사업보고서(연간)" and a["period"] == "annual" and a["filed"] == "2026-03-18"
    assert a["interest_bearing_debt"]["total"] > 0
    res = call(http, "capital_structure", {"companies": ["00123456"], "period": "annual"})
    assert res["isError"] and "year" in res["content"][0]["text"]
