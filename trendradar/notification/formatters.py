# coding=utf-8
"""
通知内容格式转换模块

提供不同推送平台间的格式转换功能
"""

import re
from typing import Dict

from trendradar.notification.escaping import escape_text, safe_url
from trendradar.report.helpers import clean_title, format_rank_display


def strip_markdown(text: str) -> str:
    """去除文本中的 markdown 语法格式，用于个人微信推送

    Args:
        text: 包含 markdown 格式的文本

    Returns:
        纯文本内容
    """
    # 转换链接 [text](url) -> text url（保留 URL）
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 \2', text)

    # 先保护 URL，避免后续 markdown 清洗误伤链接中的下划线等字符
    protected_urls: list[str] = []

    def _protect_url(match: re.Match) -> str:
        protected_urls.append(match.group(0))
        return f"@@URLTOKEN{len(protected_urls) - 1}@@"

    text = re.sub(r'https?://[^\s<>\]]+', _protect_url, text)

    # 去除粗体 **text** 或 __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'(?<!\w)__(?!\s)(.+?)(?<!\s)__(?!\w)', r'\1', text)

    # 去除斜体 *text* 或 _text_
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)', r'\1', text)

    # 去除删除线 ~~text~~
    text = re.sub(r'~~(.+?)~~', r'\1', text)

    # 去除图片 ![alt](url) -> alt
    text = re.sub(r'!\[(.+?)\]\(.+?\)', r'\1', text)

    # 去除行内代码 `code`
    text = re.sub(r'`(.+?)`', r'\1', text)

    # 去除引用符号 >
    text = re.sub(r'^>\s*', '', text, flags=re.MULTILINE)

    # 去除标题符号 # ## ### 等
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)

    # 去除水平分割线 --- 或 ***
    text = re.sub(r'^[\-\*]{3,}\s*$', '', text, flags=re.MULTILINE)

    # 去除 HTML 标签 <font color='xxx'>text</font> -> text
    text = re.sub(r'<font[^>]*>(.+?)</font>', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)

    # 清理多余的空行（保留最多两个连续空行）
    text = re.sub(r'\n{3,}', '\n\n', text)

    # 还原之前保护的 URL
    for idx, url in enumerate(protected_urls):
        text = text.replace(f"@@URLTOKEN{idx}@@", url)

    return text.strip()


def convert_markdown_to_mrkdwn(content: str) -> str:
    """
    将标准 Markdown 转换为 Slack 的 mrkdwn 格式

    转换规则：
    - **粗体** → *粗体*
    - [文本](url) → <url|文本>
    - 保留其他格式（代码块、列表等）

    Args:
        content: Markdown 格式的内容

    Returns:
        Slack mrkdwn 格式的内容
    """
    # 1. 转换链接格式: [文本](url) → <url|文本>
    content = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', content)

    # 2. 转换粗体: **文本** → *文本*
    content = re.sub(r'\*\*([^*]+)\*\*', r'*\1*', content)

    return content


# ---------------------------------------------------------------------------
# 推送渠道标题格式化（带逐字段转义）
#
# report.formatter.format_title_for_platform 是 HTML 报告/历史遗留接口，
# 其推送分支对标题、来源、关键词、链接不做（或只做部分）转义：
#   - Telegram parse_mode=HTML 下标题里一个 "<" / "&" 就会让整批 400
#   - 飞书/Slack/Discord 可被标题注入 <at id=all>、<!channel>、@everyone
# 推送侧统一使用本函数，所有外部字段先按渠道转义，链接只放行 http(s)。
# ---------------------------------------------------------------------------


# 渠道 → 标题样式
_TITLE_STYLE = {
    "feishu": "feishu",
    "dingtalk": "dingtalk",
    "wework": "markdown",
    "bark": "markdown",
    "ntfy": "ntfy",
    "telegram": "telegram",
    "slack": "slack",
}


def format_link(format_type: str, title: str, url: str) -> str:
    """生成渠道链接。title 为原始文本（内部转义），url 为原始链接（内部校验）。

    链接不安全/为空时只输出转义后的纯文本标题。
    """
    text = escape_text(title, format_type)
    link = safe_url(url)
    if not text:
        text = escape_text(link, format_type) if link else ""
    if not link:
        return text
    if format_type == "telegram":
        return f'<a href="{escape_text(link, "telegram")}">{text}</a>'
    if format_type == "slack":
        # Slack 链接 <url|text>：url 中的 | 与 > 已被 safe_url 编码
        return f"<{link}|{text}>"
    return f"[{text}]({link})"


def format_title_for_platform(
    platform: str, title_data: Dict, show_source: bool = True, show_keyword: bool = False
) -> str:
    """推送渠道统一标题格式化（输出与 report.formatter 版本一致，但所有字段已转义）

    Args:
        platform: feishu / dingtalk / wework / bark / ntfy / telegram / slack
        title_data: 标题数据字典（title, source_name, time_display, count, ranks,
            rank_threshold, url, mobile_url, is_new, matched_keyword）
        show_source: 是否显示来源名称
        show_keyword: 是否显示关键词标签
    """
    style = _TITLE_STYLE.get(platform, "markdown")
    esc_fmt = platform if platform in _TITLE_STYLE else "wework"

    ranks = title_data.get("ranks") or []
    rank_display = format_rank_display(
        ranks,
        title_data.get("rank_threshold", 10),
        "wework" if style == "markdown" else platform,
        rank_timeline=title_data.get("rank_timeline"),
    ) if ranks else ""

    raw_url = title_data.get("mobile_url") or title_data.get("url") or ""
    cleaned = clean_title(title_data.get("title", "") or "")
    if not cleaned:
        cleaned = safe_url(raw_url)
    formatted_title = format_link(esc_fmt, cleaned, raw_url)

    source = escape_text(title_data.get("source_name", ""), esc_fmt)
    keyword = escape_text(title_data.get("matched_keyword", ""), esc_fmt) if show_keyword else ""
    time_display = escape_text(title_data.get("time_display", ""), esc_fmt)
    count = title_data.get("count", 1) or 1
    title_prefix = "🆕 " if title_data.get("is_new") else ""

    if style == "feishu":
        if show_source:
            result = f"<font color='grey'>&#91;{source}&#93;</font> {title_prefix}{formatted_title}"
        elif show_keyword and keyword:
            result = f"<font color='blue'>&#91;{keyword}&#93;</font> {title_prefix}{formatted_title}"
        else:
            result = f"{title_prefix}{formatted_title}"
        if rank_display:
            result += f" {rank_display}"
        if time_display:
            result += f" <font color='grey'>- {time_display}</font>"
        if count > 1:
            result += f" <font color='green'>({count}次)</font>"
        return result

    if style == "telegram":
        if show_source:
            result = f"[{source}] {title_prefix}{formatted_title}"
        elif show_keyword and keyword:
            result = f"<b>[{keyword}]</b> {title_prefix}{formatted_title}"
        else:
            result = f"{title_prefix}{formatted_title}"
        if rank_display:
            result += f" {rank_display}"
        if time_display:
            result += f" <code>- {time_display}</code>"
        if count > 1:
            result += f" <code>({count}次)</code>"
        return result

    if style == "slack":
        if show_source:
            result = f"[{source}] {title_prefix}{formatted_title}"
        elif show_keyword and keyword:
            result = f"*[{keyword}]* {title_prefix}{formatted_title}"
        else:
            result = f"{title_prefix}{formatted_title}"
        if rank_display:
            result += f" {rank_display}"
        if time_display:
            result += f" `- {time_display}`"
        if count > 1:
            result += f" `({count}次)`"
        return result

    # dingtalk / wework / bark / ntfy（markdown 系）
    if show_source:
        result = f"[{source}] {title_prefix}{formatted_title}"
    elif show_keyword and keyword:
        result = f"[{keyword}] {title_prefix}{formatted_title}"
    else:
        result = f"{title_prefix}{formatted_title}"
    if rank_display:
        result += f" {rank_display}"
    if time_display:
        result += f" `- {time_display}`" if style == "ntfy" else f" - {time_display}"
    if count > 1:
        result += f" `({count}次)`" if style == "ntfy" else f" ({count}次)"
    return result
