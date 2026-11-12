"""사람이 고른 기사·보고서 링크 하나의 본문을 읽어 옵니다 (검색은 하지 않음).

- HTML: 제목·언론사·날짜·본문만 골라냄. 네이버 뉴스는 전용 위치에서, 그 밖의 사이트는 <article> 또는 문단이 가장 많이 모인 영역에서.
- PDF: 글자가 들어 있는 PDF면 앞쪽부터 텍스트를 뽑음 (스캔 이미지 PDF는 비어서 옴).
- 내부망·사설 IP 주소는 막음. 크기 15MB, 시간 25초 제한.
"""
from __future__ import annotations

import asyncio
import io
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx
from lxml import html as lhtml

MAX_BYTES = 15_000_000
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
DROP = ("script", "style", "noscript", "iframe", "form", "nav", "footer", "header", "aside", "button", "svg", "figure")


class PageError(Exception):
    pass


async def _check_host(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise PageError("http(s)로 시작하는 링크만 읽을 수 있습니다.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80),
                                                             type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise PageError("주소를 찾을 수 없습니다. 링크를 확인해 주세요.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise PageError("내부망 주소는 읽을 수 없습니다.")


_check_host_real = _check_host  # 테스트용 원본 참조


async def download(url: str, http: httpx.AsyncClient) -> tuple[str, bytes, str]:
    """(최종 URL, 내용, content-type). 리다이렉트도 한 단계씩 주소를 검사."""
    for _ in range(6):
        await _check_host(url)
        async with http.stream("GET", url, headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"},
                               follow_redirects=False, timeout=httpx.Timeout(25.0, connect=10.0)) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code in (401, 402, 403):
                raise PageError(f"사이트가 접근을 막았습니다(HTTP {r.status_code}). 로그인·유료 페이지라면 본문을 붙여넣어 주세요.")
            if r.status_code != 200:
                raise PageError(f"페이지를 열지 못했습니다(HTTP {r.status_code}).")
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    raise PageError("파일이 15MB를 넘습니다. 필요한 부분만 붙여넣어 주세요.")
            return str(r.url), bytes(buf), r.headers.get("content-type", "")
    raise PageError("리다이렉트가 너무 많습니다.")


def _clean(t: str) -> str:
    t = re.sub(r"[ \t ​]+", " ", t or "")
    t = re.sub(r" *\n *", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _meta(doc, *names) -> str:
    for n in names:
        v = doc.xpath(f'//meta[@property="{n}" or @name="{n}"]/@content')
        if v and v[0].strip():
            return v[0].strip()
    return ""


def _text(el) -> str:
    for br in el.iter("br"):
        br.tail = "\n" + (br.tail or "")
    parts = []
    for node in el.iter():
        if node.tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "tr") and node is not el:
            node.tail = "\n" + (node.tail or "")
    parts.append(el.text_content())
    return _clean("".join(parts))


def _best_block(doc):
    """문단(p)·텍스트가 가장 많이 모인 요소."""
    best, score = None, 0
    for el in doc.iter("article", "main", "div", "section", "td"):
        ps = el.findall(".//p")
        txt = sum(len(p.text_content().strip()) for p in ps)
        if not ps:  # 문단 없이 <br>로만 된 기사
            own = len((el.text or "").strip()) + sum(len((c.tail or "").strip()) for c in el)
            txt = max(txt, own)
        links = sum(len(a.text_content()) for a in el.iter("a"))
        s = txt - links * 0.5
        if el.tag == "article":
            s *= 1.3
        if s > score:
            best, score = el, s
    return best


def decode(content: bytes, ctype: str) -> str:
    """HTTP 헤더 → meta charset → UTF-8 → CP949(EUC-KR) 순으로 판별."""
    cands = []
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    if m:
        cands.append(m.group(1))
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", content[:4096], re.I)
    if m:
        cands.append(m.group(1).decode("ascii", "ignore"))
    cands += ["utf-8", "cp949"]
    for enc in cands:
        enc = "cp949" if enc.lower() in ("euc-kr", "ks_c_5601-1987", "ksc5601") else enc
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", "replace")


def parse_html(url: str, content: bytes, ctype: str = "") -> dict:
    src = re.sub(r"^\s*<\?xml[^>]*\?>", "", decode(content, ctype))
    doc = lhtml.document_fromstring(src)
    title = _meta(doc, "og:title", "twitter:title") or (doc.findtext(".//title") or "").strip()
    site = _meta(doc, "og:article:author", "og:site_name", "author")
    date = _meta(doc, "article:published_time", "og:regDate", "pubdate", "date")
    host = urlparse(url).hostname or ""
    body_el = None
    if host.endswith("naver.com"):
        body_el = (doc.xpath('//*[@id="dic_area"]') or doc.xpath('//*[@id="newsct_article"]') or doc.xpath('//*[@id="articeBody"]') or [None])[0]
        press = doc.xpath('//*[contains(@class,"media_end_head_top_logo")]//img/@alt')
        if press:
            site = press[0].strip()
        d = doc.xpath('//*[contains(@class,"_ARTICLE_DATE_TIME")]/@data-date-time')
        if d:
            date = d[0]
        for junk in doc.xpath('//*[@id="dic_area"]//*[contains(@class,"end_photo_org") or contains(@class,"img_desc") or contains(@class,"vod_player")]'):
            junk.drop_tree()  # 뒤따르는 본문(tail)은 남김
    for el in list(doc.iter("script", "style", "noscript", "iframe", "svg")):
        if el.getparent() is not None:
            el.drop_tree()
    if body_el is None:
        for el in list(doc.iter(*DROP)):
            if el.getparent() is not None:
                el.drop_tree()
        body_el = _best_block(doc)
    text = _text(body_el) if body_el is not None else ""
    return {"title": title, "site": site, "published": (date or "")[:10], "text": text}


def parse_pdf(content: bytes, max_pages: int = 40) -> dict:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise PageError("PDF를 읽는 부품이 설치되어 있지 않습니다.")
    try:
        rd = PdfReader(io.BytesIO(content))
        pages = [(p.extract_text() or "") for p in rd.pages[:max_pages]]
        title = (rd.metadata.title if rd.metadata and rd.metadata.title else "") or ""
    except Exception as e:
        raise PageError(f"PDF를 읽지 못했습니다: {type(e).__name__}")
    text = _clean("\n\n".join(pages))
    return {"title": title.strip(), "site": "", "published": "", "text": text, "pages": len(rd.pages)}


async def fetch(url: str, http: httpx.AsyncClient, max_chars: int) -> dict:
    url = url.strip()
    final, content, ctype = await download(url, http)
    is_pdf = "pdf" in ctype.lower() or content[:5] == b"%PDF-"
    out = parse_pdf(content) if is_pdf else parse_html(final, content, ctype)
    notes = []
    if len(out["text"]) < 200:
        notes.append("본문을 거의 읽지 못했습니다. 화면이 뜬 뒤 본문을 불러오는 사이트이거나 로그인·유료·스캔 PDF일 수 있습니다. 본문을 직접 붙여넣어 주세요.")
    if len(out["text"]) > max_chars:
        notes.append(f"본문이 길어 앞 {max_chars:,}자만 가져왔습니다.")
        out["text"] = out["text"][:max_chars]
    if is_pdf and out.get("pages", 0) > 40:
        notes.append(f"PDF {out['pages']}쪽 중 앞 40쪽만 읽었습니다.")
    host = urlparse(final).hostname or ""
    return {"url": final, "kind": "pdf" if is_pdf else "html", "title": out["title"] or host,
            "site": out["site"] or host.removeprefix("www."), "published": out["published"],
            "chars": len(out["text"]), "text": out["text"], "notes": notes}
