"""공시 재무제표(XBRL)에서 WACC용 자본구조 숫자를 뽑습니다.

이자부부채 분류 원칙
  합산: 단기차입금, 유동성장기차입금, 사채(전환·교환·BW·유동성 포함), 장기차입금, 기업어음·전자단기사채, (선택) 리스부채
  합산 안 함, 따로 표시(검토 항목): 사채할인발행차금·전환권조정 같은 조정 계정, 상환우선주·우선주부채, 파생상품부채
  → 회사마다 계정명이 달라서, 어떤 계정을 넣고 뺐는지 전부 돌려줍니다. 평가자가 확인 후 조정하면 됩니다.
"""
from __future__ import annotations

import re

PERIODS = {"annual": "11011", "q3": "11014", "half": "11012", "q1": "11013"}
PERIOD_KO = {"annual": "사업보고서(연간)", "q3": "3분기보고서", "half": "반기보고서", "q1": "1분기보고서"}

# 재무상태표·손익계산서 주요 합계 계정 (XBRL 표준 ID 우선, 계정명 대안)
TOTALS = {
    "assets": (["ifrs-full_Assets"], ["자산총계"]),
    "liabilities": (["ifrs-full_Liabilities"], ["부채총계"]),
    "equity": (["ifrs-full_Equity"], ["자본총계"]),
    "equity_parent": (["ifrs-full_EquityAttributableToOwnersOfParent"], ["지배기업의소유주에게귀속되는자본", "지배기업소유주지분", "지배기업소유지분"]),
    "cash": (["ifrs-full_CashAndCashEquivalents"], ["현금및현금성자산"]),
    "revenue": (["ifrs-full_Revenue"], ["매출액", "수익(매출액)", "영업수익", "매출"]),
    "operating_income": (["dart_OperatingIncomeLoss", "ifrs-full_ProfitLossFromOperatingActivities"], ["영업이익", "영업이익(손실)", "영업손실"]),
    "pretax_income": (["ifrs-full_ProfitLossBeforeTax"], ["법인세비용차감전순이익", "법인세비용차감전순이익(손실)", "법인세차감전순이익"]),
    "income_tax": (["ifrs-full_IncomeTaxExpenseContinuingOperations"], ["법인세비용", "법인세비용(수익)"]),
    "net_income": (["ifrs-full_ProfitLoss"], ["당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익"]),
    "finance_costs": (["ifrs-full_FinanceCosts"], ["금융원가", "금융비용"]),
}

_AGG_IDS = {"ifrs-full_CurrentFinancialLiabilities", "ifrs-full_NoncurrentFinancialLiabilities",
            "ifrs-full_OtherCurrentFinancialLiabilities", "ifrs-full_OtherNoncurrentFinancialLiabilities"}


def _debt_type(k: str) -> str | None:
    """공백 뺀 계정명 → 이자부부채 종류 (해당 없으면 None)."""
    if re.search(r"이자|미지급|발행비|보증금|예수", k):
        return None
    if "금융부채" in k and not re.search(r"파생|당기손익|기타금융부채|보증", k):
        return "금융부채(묶음)"  # 재무상태표에 차입금·사채·리스가 '금융부채'로 묶여 표시된 경우
    if "기업어음" in k or "전자단기사채" in k:
        return "기업어음"
    if "유동성장기부채" in k:
        return "유동성장기차입금"
    if "사채" in k:
        return "사채"
    if "차입금" in k or "차입부채" in k:
        if "유동성" in k:
            return "유동성장기차입금"
        if k.startswith("단기") or k.startswith("유동"):
            return "단기차입금"
        if k.startswith("장기") or k.startswith("비유동"):
            return "장기차입금"
        return "차입금"
    return None


_LEASE = r"리스부채"
_REVIEW = r"할인발행차금|전환권조정|신주인수권조정|교환권조정|상환할증금|우선주부채|상환우선주|파생상품부채|파생금융부채"
_DEBT_IDS = {
    "ifrs-full_ShorttermBorrowings": "단기차입금", "dart_ShortTermBorrowings": "단기차입금",
    "ifrs-full_CurrentPortionOfLongtermBorrowings": "유동성장기차입금", "dart_CurrentPortionOfLongTermBorrowings": "유동성장기차입금",
    "dart_CurrentPortionOfBonds": "사채", "dart_BondsIssued": "사채", "ifrs-full_BondsIssued": "사채",
    "ifrs-full_LongtermBorrowings": "장기차입금", "dart_LongTermBorrowingsGross": "장기차입금", "dart_LongTermBorrowings": "장기차입금",
    "ifrs-full_CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings": "차입금", "ifrs-full_NoncurrentPortionOfNoncurrentBorrowings": "장기차입금",
    "ifrs-full_Borrowings": "차입금", "ifrs-full_CurrentBorrowings": "차입금", "ifrs-full_NoncurrentBorrowings": "장기차입금",
}
_LEASE_IDS = {"ifrs-full_LeaseLiabilities", "ifrs-full_CurrentLeaseLiabilities", "ifrs-full_NoncurrentLeaseLiabilities"}


def _key(s: str) -> str:
    return re.sub(r"[\s·ㆍ]", "", s or "")


def amount(v) -> int | None:
    s = str(v or "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        n = int(float(s))
    except ValueError:
        return None
    return -n if neg else n


def cur(r: dict):
    """당기 금액. 분기·반기 손익은 누적(thstrm_add_amount)을 씀."""
    if r.get("sj_div") in ("IS", "CIS") and r.get("thstrm_add_amount") not in (None, "", "-"):
        return amount(r.get("thstrm_add_amount"))
    return amount(r.get("thstrm_amount"))


def find_total(rows: list[dict], key: str, sj: tuple[str, ...]) -> int | None:
    ids, names = TOTALS[key]
    cand = [r for r in rows if r.get("sj_div") in sj]
    for r in cand:
        if r.get("account_id") in ids:
            v = cur(r)
            if v is not None:
                return v
    names_k = [_key(n) for n in names]
    for r in cand:
        if _key(r.get("account_nm")) in names_k:
            v = cur(r)
            if v is not None:
                return v
    return None


def classify_debt(rows: list[dict], include_lease: bool) -> dict:
    """재무상태표 행에서 이자부부채 항목을 고름. 넣은 것·검토할 것·리스를 모두 근거로 돌려줌."""
    included, review, lease_rows = [], [], []
    for r in rows:
        if r.get("sj_div") != "BS":
            continue
        nm, aid = (r.get("account_nm") or "").strip(), r.get("account_id") or ""
        k = _key(nm)
        v = amount(r.get("thstrm_amount"))
        if v is None or v == 0:
            continue
        item = {"account": nm, "account_id": aid, "amount": v}
        if re.search(_REVIEW, k):
            review.append(item)
            continue
        if aid in _LEASE_IDS or re.search(_LEASE, k):
            if "자산" not in k and "채권" not in k:
                lease_rows.append({**item, "type": "리스부채"})
            continue
        if "자산" in k or "채권" in k or "합계" in k or "총계" in k:
            continue
        typ = "금융부채(묶음)" if "금융부채" in k and _debt_type(k) == "금융부채(묶음)" else (_DEBT_IDS.get(aid) or _debt_type(k))
        if typ:
            included.append({**item, "type": typ})
    if include_lease:
        included += lease_rows
    total = sum(i["amount"] for i in included)
    aggregated = [i for i in included if i["type"] == "금융부채(묶음)"]
    return {"items": included, "total": total, "lease_total": sum(i["amount"] for i in lease_rows),
            "lease_included": include_lease, "review_items": review,
            "approximate": bool(aggregated),
            "approximate_reason": ("재무상태표에 차입금이 '단기·장기금융부채'로 묶여 있어 그 금액을 그대로 넣었습니다. "
                                   "리스부채·기타 금융부채가 섞여 있을 수 있으니 사업보고서 주석의 차입금·사채·리스부채 내역으로 확인하세요.")
                                  if aggregated else None}


def pct(a: int | None, b: int | None) -> float | None:
    if a is None or not b:
        return None
    return round(a / b * 100, 2)


def summarize(rows: list[dict], include_lease: bool) -> dict:
    t = {k: find_total(rows, k, ("BS",) if k in ("assets", "liabilities", "equity", "equity_parent", "cash")
                       else ("IS", "CIS")) for k in TOTALS}
    debt = classify_debt(rows, include_lease)
    d = debt["total"]
    eq = t["equity"]
    tax_rate = round(t["income_tax"] / t["pretax_income"] * 100, 2) if t["income_tax"] is not None and t["pretax_income"] and t["pretax_income"] > 0 else None
    return {
        "totals": t,
        "interest_bearing_debt": debt,
        "net_debt": d - (t["cash"] or 0) if d is not None else None,
        "ratios": {
            "debt_ratio_pct": pct(t["liabilities"], eq),          # 부채비율 = 부채총계 / 자본총계
            "book_d_e_pct": pct(d, eq),                           # 장부가 D/E = 이자부부채 / 자본총계
            "net_d_e_pct": pct(d - (t["cash"] or 0), eq) if d is not None else None,
            "effective_tax_rate_pct": tax_rate,
        },
    }


def compact_statements(rows: list[dict], statements: list[str]) -> list[dict]:
    out = []
    for r in rows:
        sj = r.get("sj_div")
        if sj not in statements:
            continue
        prior = r.get("frmtrm_add_amount") if sj in ("IS", "CIS") and r.get("frmtrm_add_amount") not in (None, "", "-") else r.get("frmtrm_amount")
        try:
            ordv = int(r.get("ord") or 0)
        except ValueError:
            ordv = 0
        out.append({"st": sj, "ord": ordv, "account": (r.get("account_nm") or "").strip(), "id": r.get("account_id") or "",
                    "current": cur(r), "prior": amount(prior),
                    **({"prior2": amount(r.get("bfefrmtrm_amount"))} if r.get("bfefrmtrm_amount") not in (None, "") else {})})
    out.sort(key=lambda x: (x["st"], x["ord"]))  # 공시 화면의 표시 순서 (ord)
    return out
