# coding=utf-8
"""分批：不截断、不出空批次、按渠道计量（字节 / UTF-16 字符）、Telegram 标签保持闭合"""

import re

import pytest

from trendradar.notification.batch import (
    add_batch_headers,
    get_max_batch_header_size,
    split_footer,
    split_long_line,
    split_text_to_limit,
    text_size,
    utf16_len,
)

from test_notification_helpers import (
    make_report,
    make_standalone,
    make_title,
    splitter,
    telegram_html_errors,
)

ALL_FORMATS = ["feishu", "dingtalk", "wework", "bark", "ntfy", "telegram", "slack"]


def _visible(text: str) -> str:
    """去掉标签和空白，便于比较「内容是否完整」"""
    return re.sub(r"\s+", "", re.sub(r"<[^<>]*>", "", text))


# --------------------------------------------------------------------------
# 计量
# --------------------------------------------------------------------------


def test_text_size_units():
    assert text_size("中文", "wework") == 6           # UTF-8 字节
    assert text_size("中文", "telegram") == 2         # 字符
    assert utf16_len("😀") == 2                       # 增补平面字符按 2 个 UTF-16 码元


# --------------------------------------------------------------------------
# 单行拆分
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["wework", "telegram", "ntfy"])
def test_split_long_line_loses_nothing(fmt):
    line = "这是一段没有换行的很长的AI分析，" * 900
    parts = split_long_line(line, 1000, fmt)
    assert len(parts) > 1
    assert "".join(parts) == line
    assert all(text_size(p, fmt) <= 1000 for p in parts)


def test_split_long_line_prefers_whitespace():
    line = " ".join(["word"] * 500)
    parts = split_long_line(line, 100, "wework")
    assert all(not p.startswith("ord") for p in parts)


def test_split_telegram_keeps_tags_balanced():
    line = '<b>粗体</b> <a href="https://x.com/?a=1&amp;b=2">' + "标题&amp;" * 1500 + "</a> 结尾"
    parts = split_long_line(line, 1000, "telegram")
    assert len(parts) > 1
    for p in parts:
        assert utf16_len(p) <= 1000
        assert telegram_html_errors(p) == []
    # 实体不会被切断
    assert all(not re.search(r"&[a-z]*$", p.split("</a>")[0]) for p in parts)


def test_split_text_to_limit_by_lines_then_chars():
    text = "短行\n" + "长" * 5000 + "\n尾行"
    parts = split_text_to_limit(text, 3000, "wework")
    assert all(text_size(p, "wework") <= 3000 for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


# --------------------------------------------------------------------------
# footer 识别 / 批次头部
# --------------------------------------------------------------------------


def test_split_footer_not_fooled_by_quote_block_in_body():
    body = "正文\n\n> AI 引用块，很长很长\n\n_斜体段落_\n" + "内容\n" * 10
    footer = "\n\n\n> 更新时间：2026-10-18 09:30:00"
    b, f = split_footer(body + footer)
    assert b + f == body + footer
    assert f.strip() == footer.strip()
    assert "AI 引用块" in b and "斜体段落" in b


def test_split_footer_absent():
    assert split_footer("只有正文\n\n> 引用") == ("只有正文\n\n> 引用", "")


def test_add_batch_headers_splits_single_oversized_batch():
    content = "行\n" * 3000 + "\n\n> 更新时间：2026-10-18 09:30:00"
    out = add_batch_headers([content], "wework", 4000)
    assert len(out) > 1
    assert all(text_size(b, "wework") <= 4000 for b in out)
    assert all("更新时间" in b for b in out)
    assert out[0].startswith("**[第 1/")


def test_add_batch_headers_single_small_batch_untouched():
    assert add_batch_headers(["hello"], "telegram", 4000) == ["hello"]


def test_header_reserve_covers_three_digit_batches():
    assert get_max_batch_header_size("feishu") >= len("**[第 999/999 批次]**\n\n".encode())


# --------------------------------------------------------------------------
# 端到端分批（splitter）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_huge_single_title_is_split_not_truncated(fmt):
    big = "超长标题" * 1200 + "END_MARKER"
    report = make_report([make_title(big, url="")])
    limit = 3800
    batches = splitter()(report, fmt, None, max_bytes=limit, mode="daily")
    assert all(text_size(b, fmt) <= limit for b in batches), [text_size(b, fmt) for b in batches]
    joined = "".join(batches)
    assert "END_MARKER" in joined
    assert joined.count("超") == 1200 and joined.count("题") >= 1200
    # 不应出现只有统计头 / 分区标题、没有任何正文的批次
    for b in batches:
        body = b.split("最热话题")[-1]
        assert re.search(r"超长|标题|新增|weibo", body), b[:300]
    if fmt == "telegram":
        for b in batches:
            assert telegram_html_errors(b) == []


@pytest.mark.parametrize("fmt", ["telegram", "ntfy", "bark", "wework"])
def test_long_ai_paragraph_without_newlines_kept(fmt):
    ai = "AI分析段落没有换行" * 1500 + "AI_END"
    batches = splitter()(make_report([]), fmt, None, max_bytes=3800, mode="daily", ai_content=ai)
    assert all(text_size(b, fmt) <= 3800 for b in batches)
    assert "AI_END" in "".join(batches)
    assert "".join(batches).count("落") == 1500


def test_telegram_full_report_is_valid_html():
    report = make_report()
    batches = splitter()(
        report, "telegram", {"remote_version": "<9.9>", "current_version": "1&2"},
        max_bytes=600, mode="daily", standalone_data=make_standalone(),
        rss_items=[{"word": "RSS<词>", "count": 1, "titles": [make_title("RSS a<b", source="BBC&")]}],
        rss_new_items=[{"word": "x", "count": 1, "titles": [make_title("RSS 新增 <x>", source="Feed<1>")]}],
    )
    assert len(batches) > 1
    for b in batches:
        assert utf16_len(b) <= 600
        assert telegram_html_errors(b) == [], b
    joined = "".join(batches)
    assert "javascript:" not in joined.lower()
    # 独立区 RSS 条目变成链接，而不是「标题 (url)」裸露
    assert '<a href="https://news.ycombinator.com/item?id=1&amp;x=%282%29">' in joined


def test_telegram_counts_characters_not_bytes():
    titles = [make_title(f"中文标题{i}" * 10) for i in range(40)]
    report = make_report(titles)
    tg = splitter()(report, "telegram", None, max_bytes=3800, mode="daily")
    assert all(utf16_len(b) <= 3800 for b in tg)
    # 中文按字符计 → 单批可容纳的字节数明显超过 3800
    assert max(len(b.encode("utf-8")) for b in tg) > 3800


def test_feishu_and_slack_injection_neutralized():
    report = make_report()
    feishu = "".join(splitter()(report, "feishu", None, max_bytes=29000, mode="daily",
                                standalone_data=make_standalone()))
    assert "<at id=all>" not in feishu
    slack = "".join(splitter()(report, "slack", None, max_bytes=4000, mode="daily",
                               standalone_data=make_standalone()))
    assert "<!channel>" not in slack
    assert "@everyone" not in slack


def test_slack_footer_bold_balanced():
    batches = splitter()(make_report(), "slack", {"remote_version": "9.9", "current_version": "1.0"},
                         max_bytes=4000, mode="daily")
    footer = batches[-1].split("_更新时间")[-1]
    assert "*1.0*_" in footer


@pytest.mark.parametrize("fmt", ["ntfy", "bark"])
def test_new_titles_formatted_for_ntfy_and_bark(fmt):
    report = make_report([])
    report["new_titles"] = [{"source_name": "知乎", "titles": [make_title("新增A"), make_title("新增B")]}]
    out = "".join(splitter()(report, fmt, None, max_bytes=3800, mode="daily"))
    # 以前 ntfy 第一条/ bark 后续条目会退化为无链接的裸标题
    assert out.count("](https://www.zhihu.com/question/1?a=1&b=2)") == 2


def test_normal_report_unchanged_shape():
    """常规规模数据：每批不超限，内容按顺序完整出现"""
    titles = [make_title(f"正常标题{i}") for i in range(15)]
    report = make_report(titles)
    for fmt in ALL_FORMATS:
        batches = splitter()(report, fmt, None, max_bytes=3800, mode="daily")
        joined = "".join(batches)
        for i in range(15):
            assert f"正常标题{i}" in joined
        assert all(text_size(b, fmt) <= 3800 for b in batches)
