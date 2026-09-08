# coding=utf-8
"""
AI 日报生成模块

将 daily 轮次的 AI 分析结果生成为独立日报页，直接发布到项目网页：
- 输出 output/html/ai-daily/index.html（最新一期）
- 输出 output/html/ai-daily/YYYY-MM-DD.html（按日期存档，当天覆盖更新）
- Publish 流程 `cp -r output/html/. docs/reports/` 自动带上，
  随 reports 分支（Pages 发布源）上线
- 仅生成网页，不走任何推送渠道

AI 未启用/分析失败时由调用方跳过；本模块自身失败也不影响主流程。
"""

import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from trendradar.core.logger import get_logger

log = get_logger(__name__)

# AI 五板块 → 展示标题（顺序即页面渲染顺序）
_SECTIONS = [
    ("core_trends", "核心热点与舆情态势", "&#128200;"),
    ("sentiment_controversy", "舆论风向与争议", "&#128172;"),
    ("signals", "异动与弱信号", "&#128226;"),
    ("rss_insights", "RSS 深度洞察", "&#128240;"),
    ("outlook_strategy", "研判与策略建议", "&#127919;"),
]

_PAGE_CSS = (
    '*{margin:0;padding:0;box-sizing:border-box}'
    "body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
    'background:#f5f6f8;color:#24292f;line-height:1.75}'
    'header{background:#1a2233;color:#fff;padding:28px 20px;text-align:center}'
    'header h1{font-size:22px;margin-bottom:6px}'
    'header p{opacity:.75;font-size:13px}'
    'nav{max-width:860px;margin:14px auto 0;padding:0 16px;font-size:13px}'
    'nav a{color:#0969da;text-decoration:none;margin-right:14px}'
    'main{max-width:860px;margin:0 auto;padding:16px 16px 8px}'
    '.stats{display:flex;gap:12px;justify-content:center;margin:6px 0 18px;flex-wrap:wrap}'
    '.stat{background:#fff;border-radius:10px;padding:10px 20px;box-shadow:0 1px 4px rgba(0,0,0,.06)}'
    '.stat b{font-size:18px;color:#0969da;display:block}'
    '.stat span{font-size:12px;color:#666}'
    'section{background:#fff;border-radius:12px;margin-bottom:14px;'
    'box-shadow:0 1px 4px rgba(0,0,0,.06);overflow:hidden}'
    'section h2{font-size:16px;padding:13px 18px;background:#fafbfc;'
    'border-bottom:1px solid #eee}'
    'section .bd{padding:14px 18px;font-size:14.5px}'
    'section .bd p{margin:0 0 10px}'
    '.chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-bottom:18px}'
    '.chips span{background:#eef3fd;color:#0969da;border:1px solid #d7e2fb;'
    'padding:4px 12px;border-radius:16px;font-size:13px}'
    'footer{text-align:center;padding:22px;font-size:12px;color:#999}'
)


def _text_to_html(text: str) -> str:
    """AI 文本转 HTML：转义后按空行分段、段内换行转 <br>。"""
    text = (text or "").strip()
    if not text:
        return ""
    escaped = html.escape(text)
    paragraphs = re.split(r"\n\s*\n", escaped)
    return "".join(
        "<p>" + p.strip().replace("\n", "<br>") + "</p>"
        for p in paragraphs
        if p.strip()
    )


def _summary_chips(stats: List[Dict]) -> List[str]:
    """数据摘要 chips：Top 关键词（条数降序取前 6）。"""
    try:
        ranked = sorted(
            stats, key=lambda s: int(s.get("count", 0) or 0), reverse=True
        )
    except (TypeError, ValueError):
        ranked = list(stats or [])
    chips = []
    for s in ranked[:6]:
        word = str(s.get("word", "") or "").strip()
        if not word:
            continue
        chips.append(f"{html.escape(word)} · {s.get('count', 0)}条")
    return chips


def render_ai_daily_html(
    ai_result: Any,
    stats: List[Dict],
    date_str: str,
    time_str: str,
) -> str:
    """
    渲染 AI 日报页 HTML。

    Args:
        ai_result: AIAnalysisResult（调用方保证 success=True）
        stats: 热榜统计结果（用于数据摘要，可为空）
        date_str: 日期（YYYY-MM-DD，时区感知）
        time_str: 时间（HH:MM）
    """
    total_titles = sum(
        int(s.get("count", 0) or 0) for s in (stats or [])
    )
    keyword_count = len([s for s in (stats or []) if s.get("word")])
    chips = _summary_chips(stats or [])

    body = []
    for field, title, icon in _SECTIONS:
        section_html = _text_to_html(getattr(ai_result, field, "") or "")
        if not section_html:
            continue
        body.append(
            f'<section><h2>{icon} {title}</h2><div class="bd">{section_html}</div></section>'
        )

    if not body:
        body.append(
            '<section><div class="bd" style="color:#8b949e;text-align:center;padding:28px">'
            '本期 AI 分析无实质内容</div></section>'
        )

    chips_html = (
        '<div class="chips">' + "".join(f"<span>{c}</span>" for c in chips) + "</div>"
        if chips else ""
    )

    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<title>AI 热点日报 · {date_str}</title>'
        f'<style>{_PAGE_CSS}</style></head><body>'
        '<header><h1>&#129302; AI 热点日报</h1>'
        f'<p>{date_str} · 由 AI 自动生成，仅供参考</p></header>'
        '<nav><a href="../index.html">&#8592; 最新报告</a>'
        '<a href="../archive.html">&#128193; 历史归档</a>'
        '<a href="index.html">&#127760; 今日日报</a></nav>'
        '<main>'
        '<div class="stats">'
        f'<div class="stat"><b>{keyword_count}</b><span>关键词</span></div>'
        f'<div class="stat"><b>{total_titles}</b><span>热点条目</span></div>'
        '</div>'
        + chips_html
        + "\n".join(body)
        + '</main><footer>Powered by TrendRadar · 生成于 '
        f'{date_str} {time_str}</footer></body></html>'
    )


def write_ai_daily(
    ai_result: Any,
    stats: List[Dict],
    output_dir: str = "output",
    now: Optional[datetime] = None,
) -> Optional[str]:
    """
    生成并落盘 AI 日报页。

    Args:
        ai_result: AIAnalysisResult（success=False 时跳过）
        stats: 热榜统计结果
        output_dir: 输出根目录（日报落盘到 output/html/ai-daily/）
        now: 当前时间（时区感知）；默认取本地时间

    Returns:
        最新一期文件路径；跳过/失败返回 None（失败已记录日志）
    """
    if ai_result is None or not getattr(ai_result, "success", False):
        return None

    try:
        now = now or datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")

        html_content = render_ai_daily_html(ai_result, stats, date_str, time_str)

        out_dir = Path(output_dir) / "html" / "ai-daily"
        out_dir.mkdir(parents=True, exist_ok=True)

        # 按日期存档（当天多次运行覆盖更新）
        archive_file = out_dir / f"{date_str}.html"
        archive_file.write_text(html_content, encoding="utf-8")

        # 最新一期（固定名，站点访问入口）
        index_file = out_dir / "index.html"
        index_file.write_text(html_content, encoding="utf-8")

        log.info(f"[AI日报] 已生成: {index_file}")
        return str(index_file)
    except Exception as e:
        # 日报是附加产物，任何失败都不影响主流程
        log.info(f"[AI日报] 生成失败（不影响主流程）: {type(e).__name__}: {str(e)[:100]}")
        return None
