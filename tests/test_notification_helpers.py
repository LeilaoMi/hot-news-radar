# coding=utf-8
"""通知模块测试的公共构造器与校验器（非测试文件，供 tests/test_notification_*.py 复用）"""

import html.parser
import logging
import re
from datetime import datetime
from functools import partial
from typing import List

from trendradar.notification.splitter import split_content_into_batches

FIXED_NOW = datetime(2026, 10, 18, 9, 30, 0)

NASTY_TITLES = [
    "标普500 & 纳指 <新高>",
    'a<b && c "引号" \'单引号\'',
    "<at id=all></at> 全体注意",
    "<!channel> 紧急 @everyone @here",
    "[伪造](https://evil.example) 链接",
    "S&P <独家> 财报",
]


def make_title(title, url="https://www.zhihu.com/question/1?a=1&b=2", source="知乎", **extra):
    data = {
        "title": title,
        "source_name": source,
        "time_display": "09:00~09:30",
        "count": 2,
        "ranks": [1, 3],
        "rank_threshold": 5,
        "url": url,
        "mobile_url": "",
        "is_new": False,
        "matched_keyword": "关键<词>",
    }
    data.update(extra)
    return data


def make_report(titles=None, word="财经<&>"):
    titles = titles if titles is not None else [make_title(t) for t in NASTY_TITLES]
    return {
        "stats": [{"word": word, "count": len(titles), "titles": titles}],
        "new_titles": [{"source_name": "华尔街<见闻>", "titles": [make_title("新增 <热点> & 测试")]}],
        "failed_ids": ["weibo<x>"],
        "id_to_name": {},
        "total_new_count": 1,
    }


def make_standalone():
    return {
        "platforms": [
            {
                "id": "wallstreetcn-hot",
                "name": "华尔街见闻",
                "items": [
                    {"title": "标普500 & 纳指 <新高>", "url": "https://wallstreetcn.com/?a=1&b=2", "rank": 1},
                    {"title": "javascript 链接", "url": "javascript:alert(1)", "rank": 2},
                ],
            }
        ],
        "rss_feeds": [
            {
                "id": "hn",
                "name": "Hacker <News>",
                "items": [
                    {"title": "Show HN: a<b>", "url": "https://news.ycombinator.com/item?id=1&x=(2)",
                     "author": "x<y>", "published_at": ""},
                ],
            }
        ],
    }


def splitter(**kwargs):
    """绑定固定时间的分批函数（与 AppContext.split_content 的调用方式一致）"""
    return partial(split_content_into_batches, get_time_func=lambda: FIXED_NOW, **kwargs)


# --------------------------------------------------------------------------
# Telegram HTML 校验器（近似 Bot API parse_mode=HTML 规则）
# --------------------------------------------------------------------------

TG_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre",
                   "blockquote", "tg-spoiler", "span"}


class _TgValidator(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack: List[str] = []
        self.errors: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in TG_ALLOWED_TAGS:
            self.errors.append(f"unsupported tag <{tag}>")
        if tag == "a":
            href = dict(attrs).get("href", "")
            if not href.startswith(("http://", "https://")):
                self.errors.append(f"unsafe href {href!r}")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}>")
        else:
            self.stack.pop()


def telegram_html_errors(text: str) -> List[str]:
    errors = []
    # 裸 & （不是实体开头）
    for m in re.finditer(r"&(?![A-Za-z]+;|#\d+;|#x[0-9A-Fa-f]+;)", text):
        errors.append(f"bare & @{m.start()}")
    # 裸 < （后面不是标签）
    for m in re.finditer(r"<(?![A-Za-z/])", text):
        errors.append(f"bare < @{m.start()}")
    v = _TgValidator()
    v.feed(text)
    v.close()
    errors.extend(v.errors)
    if v.stack:
        errors.append(f"unclosed {v.stack}")
    return errors


# --------------------------------------------------------------------------
# 日志捕获（trendradar logger propagate=False，caplog 抓不到）
# --------------------------------------------------------------------------


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: List[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    @property
    def text(self) -> str:
        return "\n".join(self.messages)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text is not None else ("" if json_data is None else str(json_data))

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


# --------------------------------------------------------------------------
# 校验器自检：保证下面的断言真的能抓到坏输出
# --------------------------------------------------------------------------


def test_validator_flags_known_bad_html():
    assert telegram_html_errors("a<b && c")
    assert telegram_html_errors("<a href=\"javascript:alert(1)\">x</a>")
    assert telegram_html_errors("<b>unclosed")
    assert telegram_html_errors("<div>x</div>")


def test_validator_accepts_good_html():
    assert telegram_html_errors('<b>[1]</b> <a href="https://x.com/?a=1&amp;b=2">S&amp;P &lt;新高&gt;</a>') == []
