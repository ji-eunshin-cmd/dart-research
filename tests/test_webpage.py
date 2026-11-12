"""링크 본문 읽기(fetch_url): 네이버 뉴스·일반 기사·EUC-KR·PDF·차단 주소를 가짜 응답으로 검증합니다."""
import asyncio
import io
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webpage  # noqa: E402

BODY = "국내 전자급 과산화수소 시장은 2025년 1조2천억원 규모로 추정된다. " * 8

NAVER = f"""<html><head><meta property="og:title" content="반도체 소재 시장 커진다"><title>네이버 뉴스</title>
<script>var x = 1;</script></head><body>
<div class="media_end_head_top_logo"><img alt="한국경제"></div>
<span class="media_end_head_info_datestamp_time _ARTICLE_DATE_TIME" data-date-time="2026-10-02 09:30:00">2026.10.02.</span>
<ul class="nav"><li><a href="#">정치</a></li><li><a href="#">경제</a></li></ul>
<article id="dic_area" class="go_trans _article_content">
<span class="end_photo_org"><img src="a.jpg"><em class="img_desc">사진 설명은 빠져야 함</em></span>
{BODY}<br><br>둘째 문단입니다. 2030년까지 연평균 8% 성장이 예상된다.
</article>
<div class="comment">댓글 영역</div></body></html>"""

GENERIC = f"""<html><head><meta charset="euc-kr"><meta property="og:site_name" content="가상일보">
<meta property="article:published_time" content="2026-09-01T10:00:00+09:00"><title>산업 동향</title></head><body>
<header><a href="/">홈</a><a href="/a">메뉴1</a></header>
<div class="side"><p><a href="/x">많이 본 기사 1</a></p><p><a href="/y">많이 본 기사 2</a></p></div>
<div class="content"><h1>산업 동향</h1><p>{BODY}</p><p>업계는 공급 부족이 이어질 것으로 본다.</p></div>
<footer>저작권</footer></body></html>""".encode("euc-kr")


def _pdf() -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "Market size 2025: 1.2 trillion KRW. " * 3)
    c.showPage(); c.save()
    return buf.getvalue()


def _client(routes):
    def handler(req: httpx.Request):
        r = routes.get(str(req.url))
        if r is None:
            return httpx.Response(404)
        return r
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    async def ok(url):
        if "10.0.0.1" in url:
            raise webpage.PageError("내부망 주소는 읽을 수 없습니다.")
        if not url.startswith("http"):
            raise webpage.PageError("http(s)로 시작하는 링크만 읽을 수 있습니다.")
    monkeypatch.setattr(webpage, "_check_host", ok)


def run(url, routes, max_chars=15000):
    async def go():
        async with _client(routes) as h:
            return await webpage.fetch(url, h, max_chars)
    return asyncio.run(go())


def test_naver_article():
    u = "https://n.news.naver.com/mnews/article/015/0000000001"
    r = run(u, {u: httpx.Response(200, html=NAVER)})
    assert r["title"] == "반도체 소재 시장 커진다"
    assert r["site"] == "한국경제" and r["published"] == "2026-10-02"
    assert "1조2천억원" in r["text"] and "연평균 8%" in r["text"]
    assert "사진 설명" not in r["text"] and "댓글" not in r["text"] and "정치" not in r["text"]
    assert r["notes"] == []


def test_generic_euckr_and_redirect():
    u, final = "https://news.example.com/a", "https://news.example.com/b"
    r = run(u, {u: httpx.Response(302, headers={"location": "/b"}),
                final: httpx.Response(200, content=GENERIC, headers={"content-type": "text/html"})})
    assert r["url"] == final and r["site"] == "가상일보" and r["published"] == "2026-09-01"
    assert "공급 부족" in r["text"] and "많이 본 기사" not in r["text"] and "저작권" not in r["text"]


def test_pdf_and_truncation():
    u = "https://example.com/report.pdf"
    r = run(u, {u: httpx.Response(200, content=_pdf(), headers={"content-type": "application/pdf"})}, max_chars=1000)
    assert r["kind"] == "pdf" and "Market size 2025" in r["text"]


def test_empty_body_note_and_errors():
    u = "https://spa.example.com/"
    r = run(u, {u: httpx.Response(200, html="<html><body><div id=root></div></body></html>")})
    assert r["notes"] and "붙여넣어" in r["notes"][0]
    with pytest.raises(webpage.PageError):
        run("https://paywall.example.com/", {"https://paywall.example.com/": httpx.Response(403)})
    with pytest.raises(webpage.PageError):
        run("http://10.0.0.1/admin", {})


def test_check_host_blocks_private():
    async def go():
        return await webpage.__dict__["_check_host_real"]("http://127.0.0.1/")
    # 실제 함수(모킹 전)는 모듈 로드 시 보관한 것으로 검사
    with pytest.raises(webpage.PageError):
        asyncio.run(go())
