"""상대가치 평가용 주요 재무 지표: 계정 찾기와 LTM 계산."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from finance import ltm, metrics, prior_ytd, ytd  # noqa: E402


def R(sj, aid, nm, cur, prior=None, add=None, prior_add=None, ord_=0):
    r = {"sj_div": sj, "account_id": aid, "account_nm": nm, "thstrm_amount": cur, "frmtrm_amount": prior, "ord": str(ord_)}
    if add is not None:
        r["thstrm_add_amount"] = add
    if prior_add is not None:
        r["frmtrm_add_amount"] = prior_add
    return r


ANNUAL = [
    R("IS", "ifrs-full_Revenue", "매출액", "1,000"), R("IS", "dart_OperatingIncomeLoss", "영업이익", "100"),
    R("IS", "ifrs-full_IncomeTaxExpenseContinuingOperations", "법인세비용", "20"),
    R("IS", "ifrs-full_ProfitLoss", "당기순이익", "70", ord_=10),
    R("IS", "ifrs-full_ProfitLossAttributableToOwnersOfParent", "지배기업의 소유주에게 귀속되는 당기순이익", "60", ord_=11),
    R("CF", "ifrs-full_AdjustmentsForDepreciationExpense", "감가상각비", "50"),
    R("CF", "-표준계정코드 미사용-", "무형자산상각비", "5"), R("CF", "-표준계정코드 미사용-", "사용권자산상각비", "3"),
    R("CF", "-표준계정코드 미사용-", "대손상각비", "9"), R("CF", "-표준계정코드 미사용-", "무형자산손상차손", "7"),
]
# 3분기 보고서: 손익은 3개월(thstrm) / 누적(add), 현금흐름표는 누적
Q3 = [
    R("IS", "ifrs-full_Revenue", "매출액", "300", "250", add="900", prior_add="750"),
    R("IS", "dart_OperatingIncomeLoss", "영업이익", "30", "20", add="95", prior_add="70"),
    R("IS", "ifrs-full_IncomeTaxExpenseContinuingOperations", "법인세비용", "5", "4", add="18", prior_add="15"),
    R("IS", "ifrs-full_ProfitLoss", "분기순이익", "25", "18", add="65", prior_add="52"),
    R("IS", "ifrs-full_ProfitLossAttributableToOwnersOfParent", "지배기업소유주지분", "20", "15", add="55", prior_add="45"),
    R("CF", "ifrs-full_AdjustmentsForDepreciationExpense", "감가상각비", "40", "36"),
    R("CF", "-표준계정코드 미사용-", "무형자산상각비", "4", "3"),
]


def test_annual_metrics_and_da_items():
    m = metrics(ANNUAL)
    assert (m["revenue"], m["operating_income"], m["income_tax"], m["ni_parent"], m["net_income"]) == (1000, 100, 20, 60, 70)
    assert m["da"] == 58 and m["da_source"] == "현금흐름표"
    assert [i["account"] for i in m["da_items"]] == ["감가상각비", "무형자산상각비", "사용권자산상각비"]  # 대손·손상 제외
    assert m["ni_parent_is_total"] is False


def test_ltm():
    a, c, p = metrics(ANNUAL), metrics(Q3, ytd), metrics(Q3, prior_ytd)
    assert c["revenue"] == 900 and p["revenue"] == 750 and c["da"] == 44 and p["da"] == 39
    lt = ltm(a, c, p)
    assert lt["revenue"] == 1000 + 900 - 750
    assert lt["operating_income"] == 100 + 95 - 70
    assert lt["ni_parent"] == 60 + 55 - 45 and lt["net_income"] == 70 + 65 - 52
    assert lt["da"] == 58 + 44 - 39
    assert lt["parts"]["revenue"] == {"annual": 1000, "cur": 900, "prior": 750}
    assert lt["src"]["ni_parent"] == "지배기업소유주지분" and lt["src"]["da"] == "감가상각비 + 무형자산상각비"


def test_separate_statements_fall_back_to_net_income_and_missing_da():
    m = metrics([R("IS", "ifrs-full_ProfitLoss", "당기순이익", "70"), R("IS", "ifrs-full_Revenue", "매출액", "10")])
    assert m["ni_parent"] == 70 and m["ni_parent_is_total"] is True
    assert m["da"] is None and m["da_items"] == []
    lt = ltm(m, m, {**m, "revenue": None})
    assert lt["revenue"] is None and lt["da"] is None


def test_search_by_stock_code_and_wildcard():
    import asyncio
    from dart_client import Corp, DartClient
    c = DartClient.__new__(DartClient)
    corps = [Corp("00126380", "삼성전자", "005930"), Corp("00164779", "에스케이하이닉스", "000660"),
             Corp("00401731", "LG전자", "066570"), Corp("00999999", "3S", "060310"), Corp("00888888", "비상장", "")]

    async def fake():
        return corps
    c.corps = fake
    run = lambda q: [x.corp_name for x in asyncio.run(c.search(q, 10))]
    assert run("005930") == ["삼성전자"]
    assert run("0059*") == ["삼성전자"]
    assert run("*930") == ["삼성전자"]
    assert run("0?0660") == ["에스케이하이닉스"]
    assert run("06*") == ["3S", "LG전자"]
    assert run("00") == ["에스케이하이닉스", "삼성전자"]   # 숫자만 → 앞부분 일치
    assert run("00126380") == ["삼성전자"]                 # 8자리 고유번호
    assert run("3S") == ["3S"]                              # 숫자가 섞인 회사명은 이름 검색
    assert run("삼성") == ["삼성전자"]


def test_lease_hidden_in_other_financial_liabilities_is_flagged():
    from finance import classify_debt
    rows = [{"sj_div": "BS", "account_id": "ifrs-full_ShorttermBorrowings", "account_nm": "단기차입금", "thstrm_amount": "16,900"},
            {"sj_div": "BS", "account_id": "ifrs-full_OtherCurrentFinancialLiabilities", "account_nm": "기타금융부채", "thstrm_amount": "59,001"},
            {"sj_div": "BS", "account_id": "ifrs-full_OtherNoncurrentFinancialLiabilities", "account_nm": "기타금융부채", "thstrm_amount": "12,705"}]
    d = classify_debt(rows, True)
    assert d["total"] == 16900 and d["lease_maybe_in_other"] is True
    assert [i["account"] for i in d["review_items"]] == ["기타금융부채 (유동)", "기타금융부채 (비유동)"]
    rows.append({"sj_div": "BS", "account_id": "ifrs-full_LeaseLiabilities", "account_nm": "리스부채", "thstrm_amount": "3,000"})
    d2 = classify_debt(rows, True)
    assert d2["lease_maybe_in_other"] is False and d2["total"] == 19900
