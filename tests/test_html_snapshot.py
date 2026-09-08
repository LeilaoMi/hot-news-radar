# coding=utf-8
"""HTML 渲染特征化快照测试（P1-2 拆分 html.py 的安全网）

锁定 render_html_content 的输出字节：重构期间任何输出变化都会导致本测试失败。

场景 A：全区域富数据（keyword 模式 / daily / 含 RSS、独立展示区、AI 分析、更新提示）
场景 B：platform 模式最小数据（current 模式 / 单区域 / 无可选数据）

更新黄金样本（有意的版式变更时才允许）：
    HTML_SNAPSHOT_UPDATE=1 python -m pytest tests/test_html_snapshot.py
更新后必须人工审查 diff 确认变化符合预期。
"""

import os
from datetime import datetime
from pathlib import Path

import pytest

from trendradar.report.html import render_html_content
from trendradar.ai.analyzer import AIAnalysisResult

GOLDEN_DIR = Path(__file__).parent / "golden"
FIXED_NOW = datetime(2026, 9, 8, 12, 30)


def _fixed_time() -> datetime:
    return FIXED_NOW


def _title(**kw):
    """构造 title_data（字段默认值与渲染端 .get 的期望一致）。"""
    base = {
        "title": "", "source_name": "", "url": "", "mobile_url": "",
        "is_new": False, "matched_keyword": "", "ranks": [],
        "rank_threshold": 10, "rank_timeline": [], "time_display": "", "count": 1,
    }
    base.update(kw)
    return base


def _rich_report_data() -> dict:
    """覆盖：多关键词 tab、排名/趋势/新增/无链接/多次上榜等标题形态。"""
    return {
        "stats": [
            {
                "word": "AI芯片", "count": 3, "titles": [
                    _title(title="英伟达发布新芯片", source_name="微博", url="https://example.com/1",
                           ranks=[1, 2], rank_timeline=[{"rank": 2}, {"rank": 2}, {"rank": 1}],
                           time_display="09-08 08:00 ~ 09-08 12:00", count=3),
                    _title(title="国产GPU突破7nm", source_name="知乎",
                           mobile_url="https://m.example.com/2", ranks=[5], is_new=True,
                           matched_keyword="AI芯片"),
                    _title(title="纯文本无链接新闻标题", source_name="百度热搜", ranks=[15]),
                ],
            },
            {
                "word": "新能源", "count": 1, "titles": [
                    _title(title="固态电池量产提前", source_name="微博", url="https://example.com/4", ranks=[2, 3]),
                ],
            },
        ],
        "new_titles": [
            {"source_name": "微博", "titles": [
                _title(title="新增新闻一", url="https://example.com/n1", ranks=[1]),
                _title(title="新增新闻二", ranks=[8, 9]),
            ]},
            {"source_name": "知乎", "titles": [
                _title(title="新增新闻三", mobile_url="https://m.example.com/n3", ranks=[2]),
            ]},
        ],
        "failed_ids": ["weibo", "douyin"],
        "total_new_count": 3,
        "hotlist_total": 100,
        "platform_total": 5,
        "rss_matched_count": 12,
        "rss_total_count": 50,
        "rss_source_total": 4,
        "rss_source_failed": 1,
    }


def _rss_items() -> list:
    """RSS 统计（覆盖有链接/无链接/NEW 标记）。"""
    return [
        {"word": "AI芯片", "count": 2, "titles": [
            {"title": "RSS文章一", "source_name": "Hacker News",
             "time_display": "09-08 10:00", "url": "https://example.com/r1", "is_new": False},
            {"title": "RSS文章二", "source_name": "阮一峰周刊",
             "time_display": "09-08 11:30", "url": "", "is_new": True},
        ]},
    ]


def _standalone_data() -> dict:
    """独立展示区（2 平台 + 1 RSS 源 → 触发 tab 栏渲染）。"""
    return {
        "platforms": [
            {"id": "zhihu", "name": "知乎热榜", "items": [
                {"title": "独立区条目一", "url": "https://example.com/s1", "rank": 1,
                 "ranks": [1, 2, 1], "first_time": "08-00", "last_time": "12-30", "count": 3},
                {"title": "独立区条目二", "url": "", "rank": 0, "ranks": [],
                 "first_time": "", "last_time": "", "count": 1},
            ]},
            {"id": "weibo", "name": "微博热搜", "items": [
                {"title": "独立区条目三", "url": "https://example.com/s3", "rank": 5,
                 "ranks": [5], "first_time": "09-00", "last_time": "09-00", "count": 2},
            ]},
        ],
        "rss_feeds": [
            {"id": "hacker-news", "name": "Hacker News", "items": [
                {"title": "独立区RSS条目", "url": "https://example.com/s4",
                 "published_at": "2026-09-08T08:00:00", "author": "someone"},
            ]},
        ],
    }


def _ai_result() -> AIAnalysisResult:
    """AI 分析成功态（5 板块齐全）。"""
    return AIAnalysisResult(
        core_trends="1. AI芯片持续走热\n2. 新能源车价格战",
        sentiment_controversy="固态电池路线存争议",
        signals="美股GPU板块异动",
        rss_insights="开源模型发布密集",
        outlook_strategy="关注半导体板块回调风险",
        standalone_summaries={"zhihu": "知乎榜聚焦教育话题"},
        success=True, skipped=False, error="",
        hotlist_analyzed=30, rss_analyzed=12, standalone_analyzed=3,
        include_rss=True, include_standalone=True,
    )


SCENARIOS = {
    # 场景 A：全区域富数据（keyword / daily / 含更新提示）
    "A": dict(
        report_data=_rich_report_data(),
        total_titles=100,
        mode="daily",
        update_info={"remote_version": "v3.4.0", "current_version": "v3.3.9"},
        region_order=["hotlist", "rss", "new_items", "standalone", "ai_analysis"],
        get_time_func=_fixed_time,
        rss_items=_rss_items(),
        rss_new_items=_rss_items(),
        display_mode="keyword",
        standalone_data=_standalone_data(),
        ai_analysis=_ai_result(),
        show_new_section=True,
    ),
    # 场景 B：platform 模式最小数据（current / 单区域 / AI 未启用）
    "B": dict(
        report_data={
            "stats": [{"word": "默认词", "count": 1, "titles": [
                _title(title="唯一标题", source_name="微博", ranks=[3]),
            ]}],
            "new_titles": [],
            "failed_ids": [],
            "total_new_count": 0,
        },
        total_titles=10,
        mode="current",
        region_order=["hotlist"],
        get_time_func=_fixed_time,
        display_mode="platform",
        show_new_section=False,
    ),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_html_render_snapshot(name):
    golden = GOLDEN_DIR / f"html_snapshot_{name}.html"
    rendered = render_html_content(**SCENARIOS[name])

    if os.environ.get("HTML_SNAPSHOT_UPDATE") == "1":
        GOLDEN_DIR.mkdir(exist_ok=True)
        golden.write_text(rendered, encoding="utf-8")
        pytest.skip(f"已更新黄金样本 {golden.name}，请人工审查 diff")

    assert golden.exists(), (
        f"黄金样本缺失: {golden}（首次生成请用 HTML_SNAPSHOT_UPDATE=1 运行本测试）"
    )
    expected = golden.read_text(encoding="utf-8")
    assert rendered == expected, (
        f"HTML 渲染输出与黄金样本不一致（{golden.name}）。"
        "若是有意的版式变更，请用 HTML_SNAPSHOT_UPDATE=1 重新生成并审查 diff；"
        "否则本次改动引入了渲染回归。"
    )
