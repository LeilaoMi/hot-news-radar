"""恶意输入测试：外部数据（RSS / 热榜）中的链接必须通过 http/https 白名单。

覆盖 javascript:、大小写混写、空白/控制字符/HTML 实体混淆、data:、vbscript: 等。
"""
import re

import pytest

from trendradar.crawler.fetcher import DataFetcher
from trendradar.crawler.rss.parser import RSSParser
from trendradar.report.formatter import format_title_for_platform
from trendradar.report.helpers import REPORT_CSP, safe_href
from trendradar.report.html import render_html_content, render_rss_stats_html, render_standalone_html
from trendradar.report.rss_html import render_rss_html_content
from trendradar.report.ai_daily import render_ai_daily_html
from trendradar.utils.url import sanitize_url

HOSTILE_URLS = [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(document.domain)",
    "  javascript:alert(1)",
    "\tjavascript:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "\x01javascript:alert(1)",
    "javascript&colon;alert(1)",
    "javascript&#58;alert(1)",
    "javascript&#x3A;alert(1)",
    "&#106;avascript:alert(1)",
    "javascript&amp;colon;alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "DATA:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "VBScript:msgbox(1)",
    "file:///etc/passwd",
    "/relative/path",
    "",
]

SAFE_URLS = [
    "https://example.com/a?b=1&c=2",
    "http://example.com/",
    "HTTPS://EXAMPLE.COM/x",
    "//cdn.example.com/x",
]

BAD_SCHEME_RE = re.compile(r"(javascript|vbscript|data|file)\s*:", re.IGNORECASE)


def _hrefs(html: str):
    return re.findall(r'href="([^"]*)"', html)


def _assert_no_hostile_href(html: str):
    import html as _html

    for href in _hrefs(html):
        decoded = _html.unescape(_html.unescape(href))
        squashed = "".join(ch for ch in decoded if ord(ch) > 0x20)
        assert not BAD_SCHEME_RE.match(squashed), f"危险 href: {href!r}"
        assert squashed.startswith(("http://", "https://", "//")) or squashed.lower().startswith(("http://", "https://")), href


@pytest.mark.parametrize("url", HOSTILE_URLS)
def test_sanitize_url_rejects_hostile(url):
    assert sanitize_url(url) == ""
    assert safe_href(url) == ""


@pytest.mark.parametrize("url", SAFE_URLS)
def test_sanitize_url_keeps_http(url):
    assert sanitize_url(url) == url


def test_safe_href_escapes_attribute_breakout():
    assert safe_href('https://x.com/"onmouseover="alert(1)') == "https://x.com/&quot;onmouseover=&quot;alert(1)"


def _stat_with_urls(urls):
    return [{
        "word": "测试",
        "count": len(urls),
        "position": 0,
        "titles": [
            {
                "title": f"<img src=x onerror=alert({i})>标题{i}",
                "source_name": "源<script>",
                "first_time": "10-00",
                "last_time": "10-00",
                "time_display": "10:00",
                "count": 1,
                "ranks": [1],
                "rank_threshold": 5,
                "url": u,
                "mobile_url": "",
                "is_new": True,
            }
            for i, u in enumerate(urls)
        ],
        "percentage": 0,
    }]


def test_hotlist_and_new_section_render_hostile_links_as_text():
    stats = _stat_with_urls(HOSTILE_URLS + SAFE_URLS)
    report_data = {
        "stats": stats,
        "new_titles": [{
            "source_id": "x", "source_name": "X",
            "titles": [{"title": "新增", "url": u, "mobile_url": "", "ranks": [1], "rank_threshold": 5,
                        "source_name": "X", "time_display": "", "count": 1, "is_new": True}
                       for u in HOSTILE_URLS],
        }],
        "failed_ids": ["<b>bad</b>"],
        "total_new_count": len(HOSTILE_URLS),
    }
    html = render_html_content(
        report_data, 10, "daily",
        update_info={"remote_version": "<script>alert(6)</script>", "current_version": "1.0.0"},
        rss_items=_stat_with_urls(HOSTILE_URLS),
        rss_new_items=_stat_with_urls(HOSTILE_URLS),
        standalone_data={
            "platforms": [{"id": "p", "name": "P", "items": [{"title": "s", "url": u, "ranks": [1]} for u in HOSTILE_URLS]}],
            "rss_feeds": [{"id": "f", "name": "F", "items": [{"title": "r", "url": u} for u in HOSTILE_URLS]}],
        },
    )
    _assert_no_hostile_href(html)
    assert "<img src=x" not in html
    assert "<script>alert(6)" not in html
    assert "<b>bad</b>" not in html
    # 安全链接仍然保留
    assert 'href="https://example.com/a?b=1&amp;c=2"' in html
    # 所有 target=_blank 都带 rel
    for tag in re.findall(r"<a [^>]*target=\"_blank\"[^>]*>", html):
        assert 'rel="noopener noreferrer"' in tag, tag
    # CSP
    assert '<meta http-equiv="Content-Security-Policy"' in html
    assert "object-src 'none'" in html and "base-uri 'none'" in html


def test_csp_allows_what_the_pages_need():
    # 内联脚本/事件、cdnjs 的 html2canvas、data:/blob: 图片
    assert "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com" in REPORT_CSP
    assert "img-src 'self' data: blob:" in REPORT_CSP
    assert "style-src 'self' 'unsafe-inline'" in REPORT_CSP


def test_rss_section_and_standalone_helpers():
    _assert_no_hostile_href(render_rss_stats_html(_stat_with_urls(HOSTILE_URLS)))
    _assert_no_hostile_href(render_standalone_html({
        "platforms": [{"id": "p", "name": "P", "items": [{"title": "s", "url": u} for u in HOSTILE_URLS]}],
        "rss_feeds": [],
    }))


def test_rss_page_renders_hostile_links_as_text():
    items = [{"title": f"t{i}", "feed_id": "f", "feed_name": "F", "url": u, "published_at": "",
              "summary": "<script>x</script>", "author": ""} for i, u in enumerate(HOSTILE_URLS + SAFE_URLS)]
    html = render_rss_html_content(items, len(items))
    _assert_no_hostile_href(html)
    assert "<script>x</script>" not in html
    assert "Content-Security-Policy" in html
    assert "saveAsImage(event)" in html


def test_ai_daily_page_has_csp():
    class R:
        core_trends = "<script>alert(1)</script>"

    html = render_ai_daily_html(R(), [{"word": "<b>w</b>", "count": 3}], "2026-10-18", "21:00")
    assert "Content-Security-Policy" in html
    assert "<script>alert(1)" not in html and "<b>w</b>" not in html


@pytest.mark.parametrize("url", HOSTILE_URLS)
def test_rss_parser_drops_hostile_links(url):
    import html as _html

    xml = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>c</title>'
        f"<item><title>恶意条目</title><link>{_html.escape(url)}</link></item>"
        "</channel></rss>"
    )
    items = RSSParser().parse(xml.encode("utf-8"), "test")
    assert items and items[0].title == "恶意条目"
    assert items[0].url == ""


def test_rss_parser_falls_back_to_safe_alternate_link():
    xml = (
        '<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>c</title>'
        '<entry><title>e</title><link rel="alternate" href="javascript:alert(1)"/>'
        '<link rel="related" href="https://ok.example.com/e"/><id>1</id></entry></feed>'
    )
    items = RSSParser().parse(xml, "test")
    assert items[0].url == "https://ok.example.com/e"


def test_json_feed_hostile_url_dropped():
    import json

    feed = {"version": "https://jsonfeed.org/version/1.1", "title": "j",
            "items": [{"id": "1", "title": "x", "url": "javascript:alert(1)"}]}
    items = RSSParser().parse(json.dumps(feed), "test")
    assert items[0].url == ""


@pytest.mark.parametrize("platform", ["telegram", "html", "feishu", "slack", "dingtalk", "wework", "ntfy", "bark"])
@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "vbscript:x"])
def test_formatter_drops_hostile_links_for_every_channel(platform, url):
    out = format_title_for_platform(platform, {
        "title": "标题", "source_name": "S", "time_display": "", "count": 1,
        "ranks": [1], "rank_threshold": 5, "url": url, "mobile_url": "",
    })
    assert not BAD_SCHEME_RE.search(out)


def test_formatter_telegram_escapes_everything():
    out = format_title_for_platform("telegram", {
        "title": 'a < b & "c"', "source_name": "<S>", "time_display": "<t>", "count": 1,
        "ranks": [1], "rank_threshold": 5, "url": 'https://x.com/?a=1&b="2"', "mobile_url": "",
    })
    assert "<S>" not in out and "<t>" not in out and "a < b" not in out
    assert 'href="https://x.com/?a=1&amp;b=&quot;2&quot;"' in out


def test_formatter_neutralizes_mass_mentions():
    feishu = format_title_for_platform("feishu", {
        "title": "<at id=all></at>大新闻", "source_name": "S", "time_display": "", "count": 1,
        "ranks": [1], "rank_threshold": 5, "url": "", "mobile_url": "",
    })
    assert "<at" not in feishu
    slack = format_title_for_platform("slack", {
        "title": "<!channel> 大新闻", "source_name": "S", "time_display": "", "count": 1,
        "ranks": [1], "rank_threshold": 5, "url": "https://x.com/a|b", "mobile_url": "",
    })
    assert "<!channel>" not in slack and "|b|" not in slack


def test_hotlist_fetcher_sanitizes_item_urls(monkeypatch):
    import json

    fetcher = DataFetcher(api_url="https://api.example.com/s")
    payload = json.dumps({"status": "success", "items": [
        {"title": "ok", "url": "https://a.com/1"},
        {"title": "bad", "url": "javascript:alert(1)", "mobileUrl": "data:text/html,x"},
    ]})
    monkeypatch.setattr(fetcher, "fetch_data", lambda id_info, **k: (payload, "p", "p"))
    results, _, failed = fetcher.crawl_websites(["p"], request_interval=0)
    assert failed == []
    assert results["p"]["ok"]["url"] == "https://a.com/1"
    assert results["p"]["bad"]["url"] == "" and results["p"]["bad"]["mobileUrl"] == ""
