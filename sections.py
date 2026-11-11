"""DART 공시 원문 XML에서 필요한 장(章)만 텍스트로 뽑습니다.

원문 구조: <SECTION-1><TITLE>I. 회사의 개요</TITLE> <SECTION-2><TITLE>1. 회사의 개요</TITLE> <P>…</P> <TABLE>…</TABLE> …
표는 행마다 ' | '로 이어 붙여 Claude가 읽기 좋게 만듭니다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from lxml import etree

# 사업보고서에서 가져올 장: (표시 이름, 제목 키워드 후보, 글자 상한)
ANNUAL_TARGETS = [
    ("회사의 개요", ["회사의개요"], 6000),
    ("회사의 연혁", ["회사의연혁"], 5000),
    ("사업의 개요", ["사업의개요"], 9000),
    ("주요 제품 및 서비스", ["주요제품및서비스", "주요제품등의현황", "주요제품"], 4000),
    ("원재료 및 생산설비", ["원재료및생산설비", "생산및설비", "생산능력"], 4000),
    ("매출 및 수주상황", ["매출및수주상황", "매출에관한사항"], 3000),
    ("기타 참고사항 (산업·시장)", ["기타참고사항"], 6000),
    ("최대주주 현황", ["최대주주및특수관계인의주식소유현황", "최대주주현황"], 2500),
]
# 감사보고서: 주석 앞부분의 일반사항
AUDIT_KEYWORDS = ["일반사항", "회사의개요", "회사의개황"]

_TEXT_TAGS = {"P", "TD", "TH", "TE", "TU", "SPAN", "TITLE", "A"}


@dataclass
class Section:
    title: str
    level: int
    text: str


def _clean(s: str) -> str:
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _key(s: str) -> str:
    s = re.sub(r"^[\sIVX0-9.()가-힣]{0,4}?[.)]\s*", "", s.strip())  # 'II.', '1.', '가.' 머리번호 제거
    return re.sub(r"[\s·ㆍ,()]", "", s)


def _render(el) -> str:
    """요소 하나를 읽기용 텍스트로 (표는 행 단위)."""
    out: list[str] = []

    def walk(e):
        tag = e.tag if isinstance(e.tag, str) else ""
        tag = tag.upper()
        if tag.startswith("SECTION-"):
            return  # 하위 장은 따로 다룸
        if tag == "TABLE":
            for tr in e.iter():
                if isinstance(tr.tag, str) and tr.tag.upper() == "TR":
                    cells = [_clean("".join(td.itertext())) for td in tr
                             if isinstance(td.tag, str) and td.tag.upper() in ("TD", "TH", "TE", "TU")]
                    cells = [c for c in cells if c]
                    if cells:
                        out.append(" | ".join(cells))
            out.append("")
            return
        if tag == "P":
            t = _clean("".join(e.itertext()))
            if t:
                out.append(t)
            return
        if tag == "TITLE":
            return
        for c in e:
            walk(c)

    for c in el:
        walk(c)
    return _clean("\n".join(out))


def parse(xml: bytes) -> tuple[list[Section], str]:
    """(장 목록, 문서 전체 텍스트)."""
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(xml, parser)
    except etree.XMLSyntaxError:
        root = None
    if root is None:  # 인코딩이 다른 옛 문서
        root = etree.fromstring(xml.decode("euc-kr", "ignore").encode("utf-8"), parser)
    sections: list[Section] = []
    for el in root.iter():
        tag = el.tag.upper() if isinstance(el.tag, str) else ""
        m = re.fullmatch(r"SECTION-(\d)", tag)
        if not m:
            continue
        title_el = next((c for c in el if isinstance(c.tag, str) and c.tag.upper() == "TITLE"), None)
        title = _clean("".join(title_el.itertext())) if title_el is not None else ""
        sections.append(Section(title, int(m.group(1)), _render(el)))
    full = _clean("\n".join(t for t in root.itertext()))
    return sections, full


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\n…(이하 생략)"


def pick_annual(sections: list[Section], budget: int) -> list[dict]:
    """사업보고서에서 필요한 장만 골라 글자 예산 안에서 돌려줌."""
    picked = []
    used = 0
    for label, keys, cap in ANNUAL_TARGETS:
        sec = next((s for s in sections if any(k in _key(s.title) for k in keys) and s.text), None)
        if not sec:
            continue
        room = min(cap, budget - used)
        if room < 300:
            break
        text = _cut(sec.text, room)
        picked.append({"label": label, "title": sec.title, "text": text})
        used += len(text)
    return picked


def pick_audit(full_text: str, budget: int) -> list[dict]:
    """감사보고서: 주석의 '일반사항(회사의 개요)' 부분부터 잘라 옴."""
    norm = re.sub(r"\s", "", full_text)
    for k in AUDIT_KEYWORDS:
        i = norm.find(k)
        if i >= 0:
            # 공백 제거 전 위치로 환산
            count, pos = 0, 0
            for pos, ch in enumerate(full_text):
                if not ch.isspace():
                    if count == i:
                        break
                    count += 1
            start = max(0, full_text.rfind("\n", 0, pos))
            return [{"label": "주석 – 일반사항", "title": k, "text": _cut(full_text[start:].strip(), min(budget, 8000))}]
    return [{"label": "감사보고서 본문 앞부분", "title": "", "text": _cut(full_text, min(budget, 6000))}]
