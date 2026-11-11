"""DART 공시 원문 XML에서 필요한 장(章)만 텍스트로 뽑습니다.

원문 구조: <SECTION-1><TITLE>I. 회사의 개요</TITLE> <SECTION-2><TITLE>1. 회사의 개요</TITLE> <P>…</P> <TABLE>…</TABLE> …
표는 행마다 ' | '로 이어 붙여 Claude가 읽기 좋게 만듭니다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from lxml import etree

# 사업보고서에서 가져올 장: (표시 이름, 제목 키워드 후보, 글자 상한) — 이 순서가 예산 배분 우선순위
# 산업 장표에 꼭 필요한 '기타 참고사항(산업의 특성·시장 여건)'과 '사업의 개요'를 먼저 확보합니다.
ANNUAL_TARGETS = [
    ("회사의 개요", ["회사의개요"], 4000),
    ("사업의 개요", ["사업의개요"], 7000),
    ("기타 참고사항 (산업·시장)", ["기타참고사항"], 7000),
    ("회사의 연혁", ["회사의연혁"], 3500),
    ("주요 제품 및 서비스", ["주요제품및서비스", "주요제품등의현황", "주요제품"], 3000),
    ("원재료 및 생산설비", ["원재료및생산설비", "생산및설비", "생산능력"], 3000),
    ("매출 및 수주상황", ["매출및수주상황", "매출에관한사항"], 2000),
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


def _render(el, nested: bool = False) -> str:
    """요소 하나를 읽기용 텍스트로 (표는 행 단위). nested=True면 하위 장까지 포함."""
    out: list[str] = []

    def walk(e):
        tag = e.tag if isinstance(e.tag, str) else ""
        tag = tag.upper()
        if tag.startswith("SECTION-") and not nested:
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
            if nested:
                t = _clean("".join(e.itertext()))
                if t:
                    out.append(t)
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
    full = _render(root, nested=True) or _clean("\n".join(t for t in root.itertext()))
    return sections, full


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\n…(이하 생략)"


def pick_annual(sections: list[Section], budget: int) -> list[dict]:
    """사업보고서에서 필요한 장만 골라 글자 예산 안에서 돌려줌."""
    picked = []
    used = 0
    for label, keys, cap in ANNUAL_TARGETS:
        idx = next((i for i, s in enumerate(sections) if any(k in _key(s.title) for k in keys) and s.text), None)
        if idx is None:
            continue
        room = min(cap, budget - used)
        if room < 300:
            continue
        text = _cut(sections[idx].text, room)
        picked.append({"label": label, "title": sections[idx].title, "text": text, "_order": idx})
        used += len(text)
    picked.sort(key=lambda p: p.pop("_order"))
    return picked


def pick_audit(full_text: str, budget: int) -> list[dict]:
    """감사보고서: 주석 '1. 일반사항(회사의 개요)'부터 다음 주석 '2. …' 직전까지 (주주현황 표 포함)."""
    lines = full_text.split("\n")
    heads = [i for i, ln in enumerate(lines)
             if any(w in re.sub(r"\s", "", ln) for w in AUDIT_KEYWORDS) and len(re.sub(r"\s", "", ln)) <= 30]
    notes_at = next((i for i, ln in enumerate(lines) if re.sub(r"\s", "", ln) == "주석"), -1)
    # '주석' 제목 뒤의 첫 '일반사항' 제목 줄 (앞쪽 목차·감사의견 문단 제외), 없으면 마지막 제목 줄
    start = next((i for i in heads if i > notes_at), heads[-1] if heads else None)
    if start is None:
        return [{"label": "감사보고서 본문 앞부분", "title": "", "text": _cut(full_text, min(budget, 5000))}]
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^\s*2\s*[.)]\s*\S", lines[j]):
            end = j
            break
    text = _clean("\n".join(lines[start:end]))
    return [{"label": "주석 – 일반사항", "title": lines[start].strip(), "text": _cut(text, min(budget, 6000))}]
