"""
TrendRadar MCP Server - FastMCP 实现

使用 FastMCP 提供 MCP 工具服务器，支持 stdio 和 HTTP 两种传输模式。

安全默认值：
- HTTP 默认只监听 127.0.0.1；监听非回环地址时必须设置 MCP_AUTH_TOKEN，否则拒绝启动；
- HTTP 始终校验 Host / Origin（防 DNS rebinding）；
- stdio 模式下 stdout 只输出 JSON-RPC，所有日志 / print 都重定向到 stderr。
"""

import asyncio
import json
import logging
import os
import sys
from typing import List, Optional, Dict, Union

from fastmcp import FastMCP

from . import __version__
from .security import (
    DEFAULT_HTTP_HOST,
    DEFAULT_HTTP_PATH,
    DEFAULT_HTTP_PORT,
    ENV_ENABLE_SIDE_EFFECT_TOOLS,
    SIDE_EFFECT_TOOLS,
    InsecureBindError,
    env_flag,
    resolve_http_security,
    http_transport_kwargs,
)
from .utils import paths as _paths

from .tools.data_query import DataQueryTools
from .tools.analytics import AnalyticsTools
from .tools.search_tools import SearchTools
from .tools.config_mgmt import ConfigManagementTools
from .tools.system import SystemManagementTools
from .tools.storage_sync import StorageSyncTools
from .tools.article_reader import ArticleReaderTools
from .tools.notification import NotificationTools
from .utils.date_parser import DateParser
from .utils.errors import MCPError


logger = logging.getLogger("mcp_server")

# 创建 FastMCP 应用（version 让客户端看到的 serverInfo 是 MCP Server 自己的版本号）
mcp = FastMCP('trendradar-news', version=__version__)

# 全局工具实例（在第一次请求时初始化）
_tools_instances = {}


def _get_tools(project_root: Optional[str] = None):
    """获取或创建工具实例（单例模式）"""
    if not _tools_instances:
        if project_root:
            # 让模块级的配置读取（平台校验、权重配置）也跟随 --project-root
            _paths.set_project_root(project_root)
        _tools_instances['data'] = DataQueryTools(project_root)
        _tools_instances['analytics'] = AnalyticsTools(project_root)
        _tools_instances['search'] = SearchTools(project_root)
        _tools_instances['config'] = ConfigManagementTools(project_root)
        _tools_instances['system'] = SystemManagementTools(project_root)
        _tools_instances['storage'] = StorageSyncTools(project_root)
        _tools_instances['article'] = ArticleReaderTools(project_root)
        _tools_instances['notification'] = NotificationTools(project_root)
    return _tools_instances


# ==================== MCP Resources ====================

@mcp.resource("config://platforms")
async def get_platforms_resource() -> str:
    """
    获取支持的平台列表

    返回 config.yaml 中配置的所有平台信息，包括 ID 和名称。
    """
    tools = _get_tools()
    config = await asyncio.to_thread(
        tools['config'].get_current_config, section="crawler"
    )
    return json.dumps({
        "platforms": config.get("platforms", []),
        "description": "TrendRadar 支持的热榜平台列表"
    }, ensure_ascii=False, indent=2)


@mcp.resource("config://rss-feeds")
async def get_rss_feeds_resource() -> str:
    """
    获取 RSS 订阅源列表

    返回当前配置的所有 RSS 源信息。
    """
    tools = _get_tools()
    status = await asyncio.to_thread(tools['data'].get_rss_feeds_status)
    return json.dumps({
        "feeds": status.get("today_feeds", {}),
        "description": "TrendRadar 支持的 RSS 订阅源列表"
    }, ensure_ascii=False, indent=2)


@mcp.resource("data://available-dates")
async def get_available_dates_resource() -> str:
    """
    获取可用的数据日期范围

    返回本地存储中可查询的日期列表。
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['storage'].list_available_dates, source="local"
    )
    return json.dumps({
        "dates": result.get("data", {}).get("local", {}).get("dates", []),
        "description": "本地存储中可查询的日期列表"
    }, ensure_ascii=False, indent=2)


@mcp.resource("config://keywords")
async def get_keywords_resource() -> str:
    """
    获取关注词配置

    返回 frequency_words.txt 中配置的关注词分组。
    """
    tools = _get_tools()
    config = await asyncio.to_thread(
        tools['config'].get_current_config, section="keywords"
    )
    return json.dumps({
        "word_groups": config.get("word_groups", []),
        "total_groups": config.get("total_groups", 0),
        "description": "TrendRadar 关注词配置"
    }, ensure_ascii=False, indent=2)


# ==================== 日期解析工具（优先调用）====================

@mcp.tool
async def resolve_date_range(
    expression: str
) -> str:
    """
    【推荐优先调用】将自然语言日期表达式解析为标准日期范围

    **为什么需要这个工具？**
    用户经常使用"本周"、"最近7天"等自然语言表达日期，但 AI 模型自己计算日期
    可能导致不一致的结果。此工具在服务器端使用精确的当前时间计算，确保所有
    AI 模型获得一致的日期范围。

    **推荐使用流程：**
    1. 用户说"分析AI本周的情感倾向"
    2. AI 调用 resolve_date_range("本周") → 获取精确日期范围
    3. AI 调用 analyze_sentiment(topic="ai", date_range=上一步返回的date_range)

    Args:
        expression: 自然语言日期表达式，支持：
            - 单日: "今天", "昨天", "today", "yesterday"
            - 周: "本周", "上周", "this week", "last week"
            - 月: "本月", "上月", "this month", "last month"
            - 最近N天: "最近7天", "最近30天", "last 7 days", "last 30 days"
            - 动态: "最近5天", "last 10 days"（任意天数）

    Returns:
        JSON格式的日期范围，可直接用于其他工具的 date_range 参数：
        {
            "success": true,
            "expression": "本周",
            "date_range": {
                "start": "2025-11-18",
                "end": "2025-11-26"
            },
            "current_date": "2025-11-26",
            "description": "本周（周一到周日，11-18 至 11-26）"
        }

    Examples:
        用户："分析AI本周的情感倾向"
        AI调用步骤：
        1. resolve_date_range("本周")
           → {"date_range": {"start": "2025-11-18", "end": "2025-11-26"}, ...}
        2. analyze_sentiment(topic="ai", date_range={"start": "2025-11-18", "end": "2025-11-26"})

        用户："看看最近7天的特斯拉新闻"
        AI调用步骤：
        1. resolve_date_range("最近7天")
           → {"date_range": {"start": "2025-11-20", "end": "2025-11-26"}, ...}
        2. search_news(query="特斯拉", date_range={"start": "2025-11-20", "end": "2025-11-26"})
    """
    try:
        result = await asyncio.to_thread(DateParser.resolve_date_range_expression, expression)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except MCPError as e:
        return json.dumps({
            "success": False,
            "error": e.to_dict()
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": str(e)
            }
        }, ensure_ascii=False, indent=2)


# ==================== 数据查询工具 ====================

@mcp.tool
async def get_latest_news(
    platforms: Optional[List[str]] = None,
    limit: int = 50,
    include_url: bool = False
) -> str:
    """
    获取最新一批爬取的新闻数据，快速了解当前热点

    Args:
        platforms: 平台ID列表，如 ['zhihu', 'weibo']，不指定则使用所有平台
        limit: 返回条数限制，默认50，最大1000
        include_url: 是否包含URL链接，默认False（节省token）

    Returns:
        JSON格式的新闻列表

    **数据展示建议**
    - 默认展示全部返回数据，除非用户明确要求总结
    - 用户说"总结"或"挑重点"时才进行筛选
    - 用户问"为什么只显示部分"说明需要完整数据
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['data'].get_latest_news,
        platforms=platforms, limit=limit, include_url=include_url
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def get_trending_topics(
    top_n: int = 10,
    mode: str = 'current',
    extract_mode: str = 'keywords'
) -> str:
    """
    获取热点话题统计

    Args:
        top_n: 返回TOP N话题，默认10
        mode: 时间模式
            - "daily": 当日累计数据统计
            - "current": 最新一批数据统计（默认）
        extract_mode: 提取模式
            - "keywords": 统计预设关注词（基于 config/frequency_words.txt，默认）
            - "auto_extract": 自动从新闻标题提取高频词（无需预设，自动发现热点）

    Returns:
        JSON格式的话题频率统计列表

    Examples:
        - 使用预设关注词: get_trending_topics(mode="current")
        - 自动提取热点: get_trending_topics(extract_mode="auto_extract", top_n=20)
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['data'].get_trending_topics,
        top_n=top_n, mode=mode, extract_mode=extract_mode
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


# ==================== RSS 数据查询工具 ====================

@mcp.tool
async def get_latest_rss(
    feeds: Optional[List[str]] = None,
    days: int = 1,
    limit: int = 50,
    include_summary: bool = False
) -> str:
    """
    获取最新的 RSS 订阅数据（支持多日查询）

    RSS 数据与热榜新闻分开存储，按时间流展示，适合获取特定来源的最新内容。

    Args:
        feeds: RSS 源 ID 列表，如 ['hacker-news', '36kr']，不指定则返回所有源
        days: 获取最近 N 天的数据，默认 1（仅今天），最大 30 天
        limit: 返回条数限制，默认50，最大500
        include_summary: 是否包含文章摘要，默认False（节省token）

    Returns:
        JSON格式的 RSS 条目列表

    Examples:
        - get_latest_rss()
        - get_latest_rss(days=7, feeds=['hacker-news'])
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['data'].get_latest_rss,
        feeds=feeds, days=days, limit=limit, include_summary=include_summary
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def search_rss(
    keyword: str,
    feeds: Optional[List[str]] = None,
    days: int = 7,
    limit: int = 50,
    include_summary: bool = False
) -> str:
    """
    搜索 RSS 数据

    在 RSS 订阅数据中搜索包含指定关键词的文章。

    Args:
        keyword: 搜索关键词（必需）
        feeds: RSS 源 ID 列表，如 ['hacker-news', '36kr']
               - 不指定时：搜索所有 RSS 源
        days: 搜索最近 N 天的数据，默认 7 天，最大 30 天
        limit: 返回条数限制，默认50
        include_summary: 是否包含文章摘要，默认False

    Returns:
        JSON格式的匹配 RSS 条目列表

    Examples:
        - search_rss(keyword="AI")
        - search_rss(keyword="machine learning", feeds=['hacker-news'], days=14)
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['data'].search_rss,
        keyword=keyword,
        feeds=feeds,
        days=days,
        limit=limit,
        include_summary=include_summary
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def get_rss_feeds_status() -> str:
    """
    获取 RSS 源状态信息

    查看当前配置的 RSS 源及其数据统计信息。

    Returns:
        JSON格式的 RSS 源状态，包含：
        - available_dates: 有 RSS 数据的日期列表
        - total_dates: 总日期数
        - today_feeds: 今日各 RSS 源的数据统计
            - {feed_id}: { name, item_count }
        - generated_at: 生成时间

    Examples:
        - get_rss_feeds_status()  # 查看所有 RSS 源状态
    """
    tools = _get_tools()
    result = await asyncio.to_thread(tools['data'].get_rss_feeds_status)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def get_news_by_date(
    date_range: Optional[Union[Dict[str, str], str]] = None,
    platforms: Optional[List[str]] = None,
    limit: int = 50,
    include_url: bool = False
) -> str:
    """
    获取指定日期的新闻数据，用于历史数据分析和对比

    Args:
        date_range: 日期范围，支持多种格式:
            - 范围对象: {"start": "2025-01-01", "end": "2025-01-07"}
            - 自然语言: "今天", "昨天", "本周", "最近7天"
            - 单日字符串: "2025-01-15"
            - 默认值: "今天"
        platforms: 平台ID列表，如 ['zhihu', 'weibo']，不指定则使用所有平台
        limit: 返回条数限制，默认50，最大1000
        include_url: 是否包含URL链接，默认False（节省token）

    Returns:
        JSON格式的新闻列表，包含标题、平台、排名等信息
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['data'].get_news_by_date,
        date_range=date_range,
        platforms=platforms,
        limit=limit,
        include_url=include_url
    )
    return json.dumps(result, ensure_ascii=False, indent=2)



# ==================== 高级数据分析工具 ====================

@mcp.tool
async def analyze_topic_trend(
    topic: str,
    analysis_type: str = "trend",
    date_range: Optional[Union[Dict[str, str], str]] = None,
    granularity: str = "day",
    spike_threshold: float = 3.0,
    time_window: int = 24,
    lookahead_hours: int = 6,
    confidence_threshold: float = 0.7
) -> str:
    """
    统一话题趋势分析工具 - 整合多种趋势分析模式

    建议：使用自然语言日期时，先调用 resolve_date_range 获取精确日期范围。

    Args:
        topic: 话题关键词（必需）
        analysis_type: 分析类型
            - "trend": 热度趋势分析（默认）
            - "lifecycle": 生命周期分析
            - "viral": 异常热度检测
            - "predict": 话题预测
        date_range: 日期范围，格式 {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}，默认最近7天
        granularity: 时间粒度，默认"day"
        spike_threshold: 热度突增倍数阈值（viral模式），默认3.0
        time_window: 检测时间窗口小时数（viral模式），默认24
        lookahead_hours: 预测未来小时数（predict模式），默认6
        confidence_threshold: 置信度阈值（predict模式），默认0.7

    Returns:
        JSON格式的趋势分析结果

    Examples:
        - analyze_topic_trend(topic="AI", date_range={"start": "2025-01-01", "end": "2025-01-07"})
        - analyze_topic_trend(topic="特斯拉", analysis_type="lifecycle")
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['analytics'].analyze_topic_trend_unified,
        topic=topic,
        analysis_type=analysis_type,
        date_range=date_range,
        granularity=granularity,
        threshold=spike_threshold,
        time_window=time_window,
        lookahead_hours=lookahead_hours,
        confidence_threshold=confidence_threshold
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def analyze_data_insights(
    insight_type: str = "platform_compare",
    topic: Optional[str] = None,
    date_range: Optional[Union[Dict[str, str], str]] = None,
    min_frequency: int = 3,
    top_n: int = 20
) -> str:
    """
    统一数据洞察分析工具 - 整合多种数据分析模式

    Args:
        insight_type: 洞察类型，可选值：
            - "platform_compare": 平台对比分析（对比不同平台对话题的关注度）
            - "platform_activity": 平台活跃度统计（统计各平台发布频率和活跃时间）
            - "keyword_cooccur": 关键词共现分析（分析关键词同时出现的模式）
        topic: 话题关键词（可选，platform_compare模式适用）
        date_range: **【对象类型】** 日期范围（可选）
                    - **格式**: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
                    - **示例**: {"start": "2025-01-01", "end": "2025-01-07"}
                    - **重要**: 必须是对象格式，不能传递整数
        min_frequency: 最小共现频次（keyword_cooccur模式），默认3
        top_n: 返回TOP N结果（keyword_cooccur模式），默认20

    Returns:
        JSON格式的数据洞察分析结果

    Examples:
        - analyze_data_insights(insight_type="platform_compare", topic="人工智能")
        - analyze_data_insights(insight_type="platform_activity", date_range={"start": "2025-01-01", "end": "2025-01-07"})
        - analyze_data_insights(insight_type="keyword_cooccur", min_frequency=5, top_n=15)
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['analytics'].analyze_data_insights_unified,
        insight_type=insight_type,
        topic=topic,
        date_range=date_range,
        min_frequency=min_frequency,
        top_n=top_n
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def analyze_sentiment(
    topic: Optional[str] = None,
    platforms: Optional[List[str]] = None,
    date_range: Optional[Union[Dict[str, str], str]] = None,
    limit: int = 50,
    sort_by_weight: bool = True,
    include_url: bool = False
) -> str:
    """
    分析新闻的情感倾向和热度趋势

    建议：使用自然语言日期时，先调用 resolve_date_range 获取精确日期范围。

    Args:
        topic: 话题关键词（可选）
        platforms: 平台ID列表，如 ['zhihu', 'weibo']，不指定则使用所有平台
        date_range: 日期范围，格式 {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}，默认今天
        limit: 返回新闻数量，默认50，最大100（会对标题去重）
        sort_by_weight: 是否按热度权重排序，默认True
        include_url: 是否包含URL链接，默认False（节省token）

    Returns:
        JSON格式的分析结果，包含情感分布、热度趋势和相关新闻

    Examples:
        - analyze_sentiment(topic="AI", date_range={"start": "2025-01-01", "end": "2025-01-07"})
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['analytics'].analyze_sentiment,
        topic=topic,
        platforms=platforms,
        date_range=date_range,
        limit=limit,
        sort_by_weight=sort_by_weight,
        include_url=include_url
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def find_related_news(
    reference_title: str,
    date_range: Optional[Union[Dict[str, str], str]] = None,
    threshold: float = 0.5,
    limit: int = 50,
    include_url: bool = False
) -> str:
    """
    查找与指定新闻标题相关的其他新闻（支持当天和历史数据）

    Args:
        reference_title: 参考新闻标题（完整或部分）
        date_range: 日期范围（可选）
            - 不指定: 只查询今天的数据
            - "today", "yesterday", "last_week", "last_month": 预设值
            - {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}: 自定义范围
        threshold: 相似度阈值，0-1之间，默认0.5（越高匹配越严格）
        limit: 返回条数限制，默认50
        include_url: 是否包含URL链接，默认False（节省token）

    Returns:
        JSON格式的相关新闻列表，按相似度排序

    Examples:
        - find_related_news(reference_title="特斯拉降价")
        - find_related_news(reference_title="AI突破", date_range="last_week")
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['search'].find_related_news_unified,
        reference_title=reference_title,
        date_range=date_range,
        threshold=threshold,
        limit=limit,
        include_url=include_url
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def generate_summary_report(
    report_type: str = "daily",
    date_range: Optional[Union[Dict[str, str], str]] = None
) -> str:
    """
    每日/每周摘要生成器 - 自动生成热点摘要报告

    Args:
        report_type: 报告类型（daily/weekly）
        date_range: **【对象类型】** 自定义日期范围（可选）
                    - **格式**: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
                    - **示例**: {"start": "2025-01-01", "end": "2025-01-07"}
                    - **重要**: 必须是对象格式，不能传递整数

    Returns:
        JSON格式的摘要报告，包含Markdown格式内容
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['analytics'].generate_summary_report,
        report_type=report_type,
        date_range=date_range
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def aggregate_news(
    date_range: Optional[Union[Dict[str, str], str]] = None,
    platforms: Optional[List[str]] = None,
    similarity_threshold: float = 0.7,
    limit: int = 50,
    include_url: bool = False
) -> str:
    """
    跨平台新闻聚合 - 对相似新闻进行去重合并

    将不同平台报道的同一事件合并为一条聚合新闻，显示跨平台覆盖情况和综合热度。

    Args:
        date_range: 日期范围，不指定则查询今天
        platforms: 平台ID列表，如 ['zhihu', 'weibo']，不指定则使用所有平台
        similarity_threshold: 相似度阈值，0.3-1.0，默认0.7（越高越严格）
        limit: 返回聚合新闻数量，默认50
        include_url: 是否包含URL链接，默认False

    Returns:
        JSON格式的聚合结果，包含去重统计、聚合新闻列表和平台覆盖统计

    Examples:
        - aggregate_news()
        - aggregate_news(similarity_threshold=0.8)
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['analytics'].aggregate_news,
        date_range=date_range,
        platforms=platforms,
        similarity_threshold=similarity_threshold,
        limit=limit,
        include_url=include_url
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def compare_periods(
    period1: Union[Dict[str, str], str],
    period2: Union[Dict[str, str], str],
    topic: Optional[str] = None,
    compare_type: str = "overview",
    platforms: Optional[List[str]] = None,
    top_n: int = 10
) -> str:
    """
    时期对比分析 - 比较两个时间段的新闻数据

    对比不同时期的热点话题、平台活跃度、新闻数量等维度。

    **使用场景：**
    - 对比本周和上周的热点变化
    - 分析某个话题在两个时期的热度差异
    - 查看各平台活跃度的周期性变化

    Args:
        period1: 第一个时间段（基准期）
            - {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}: 日期范围
            - "today", "yesterday", "this_week", "last_week", "this_month", "last_month": 预设值
        period2: 第二个时间段（对比期，格式同 period1）
        topic: 可选的话题关键词（聚焦特定话题的对比）
        compare_type: 对比类型
            - "overview": 总体概览（默认）- 新闻数量、关键词变化、TOP新闻
            - "topic_shift": 话题变化分析 - 上升话题、下降话题、新出现话题
            - "platform_activity": 平台活跃度对比 - 各平台新闻数量变化
        platforms: 平台过滤列表，如 ['zhihu', 'weibo']
        top_n: 返回 TOP N 结果，默认10

    Returns:
        JSON格式的对比分析结果，包含：
        - periods: 两个时期的日期范围
        - compare_type: 对比类型
        - overview/topic_shift/platform_comparison: 具体对比结果（根据类型）

    Examples:
        - compare_periods(period1="last_week", period2="this_week")  # 周环比
        - compare_periods(period1="last_month", period2="this_month", compare_type="topic_shift")
        - compare_periods(
            period1={"start": "2025-01-01", "end": "2025-01-07"},
            period2={"start": "2025-01-08", "end": "2025-01-14"},
            topic="人工智能"
          )
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['analytics'].compare_periods,
        period1=period1,
        period2=period2,
        topic=topic,
        compare_type=compare_type,
        platforms=platforms,
        top_n=top_n
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


# ==================== 智能检索工具 ====================

@mcp.tool
async def search_news(
    query: str,
    search_mode: str = "keyword",
    date_range: Optional[Union[Dict[str, str], str]] = None,
    platforms: Optional[List[str]] = None,
    limit: int = 50,
    sort_by: str = "relevance",
    threshold: float = 0.6,
    include_url: bool = False,
    include_rss: bool = False,
    rss_limit: int = 20
) -> str:
    """
    统一搜索接口，支持多种搜索模式，可同时搜索热榜和RSS

    建议：使用自然语言日期时，先调用 resolve_date_range 获取精确日期范围。

    Args:
        query: 搜索关键词或内容片段
        search_mode: 搜索模式
            - "keyword": 精确关键词匹配（默认）
            - "fuzzy": 模糊内容匹配
            - "entity": 实体名称搜索（人物/地点/机构）
        date_range: 日期范围，格式 {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}，默认今天
        platforms: 平台ID列表，如 ['zhihu', 'weibo']，不指定则使用所有平台
        limit: 热榜返回条数限制，默认50
        sort_by: 排序方式 - "relevance"（相关度）/ "weight"（权重）/ "date"（日期）
        threshold: 相似度阈值（仅fuzzy模式），0-1，默认0.6
        include_url: 是否包含URL链接，默认False
        include_rss: 是否同时搜索RSS数据，默认False
        rss_limit: RSS返回条数限制，默认20

    Returns:
        JSON格式的搜索结果，包含热榜新闻列表和可选的RSS结果

    Examples:
        - search_news(query="AI")
        - search_news(query="AI", include_rss=True)
        - search_news(query="特斯拉", date_range={"start": "2025-01-01", "end": "2025-01-07"})
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['search'].search_news_unified,
        query=query,
        search_mode=search_mode,
        date_range=date_range,
        platforms=platforms,
        limit=limit,
        sort_by=sort_by,
        threshold=threshold,
        include_url=include_url,
        include_rss=include_rss,
        rss_limit=rss_limit
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


# ==================== 配置与系统管理工具 ====================

@mcp.tool
async def get_current_config(
    section: str = "all"
) -> str:
    """
    获取当前系统配置

    Args:
        section: 配置节，可选值：
            - "all": 所有配置（默认）
            - "crawler": 爬虫配置
            - "push": 推送配置
            - "keywords": 关键词配置
            - "weights": 权重配置

    Returns:
        JSON格式的配置信息
    """
    tools = _get_tools()
    result = await asyncio.to_thread(tools['config'].get_current_config, section=section)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def get_system_status() -> str:
    """
    获取系统运行状态和健康检查信息

    返回系统版本、数据统计、缓存状态等信息

    Returns:
        JSON格式的系统状态信息
    """
    tools = _get_tools()
    result = await asyncio.to_thread(tools['system'].get_system_status)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def check_version(
    proxy_url: Optional[str] = None
) -> str:
    """
    检查版本更新（同时检查 TrendRadar 和 MCP Server）

    比较本地版本与 GitHub 远程版本，判断是否需要更新。

    Args:
        proxy_url: 可选的代理URL，用于访问 GitHub（如 http://127.0.0.1:7890）

    Returns:
        JSON格式的版本检查结果，包含两个组件的版本对比和是否需要更新

    Examples:
        - check_version()
        - check_version(proxy_url="http://127.0.0.1:7890")
    """
    tools = _get_tools()
    result = await asyncio.to_thread(tools['system'].check_version, proxy_url=proxy_url)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def trigger_crawl(
    platforms: Optional[List[str]] = None,
    save_to_local: bool = False,
    include_url: bool = False
) -> str:
    """
    手动触发一次爬取任务（可选持久化）

    Args:
        platforms: 平台ID列表，如 ['zhihu', 'weibo']，不指定则使用所有平台
        save_to_local: 是否保存到本地 output 目录，默认 False
        include_url: 是否包含URL链接，默认False（节省token）

    Returns:
        JSON格式的任务状态信息，包含成功/失败平台列表和新闻数据

    Examples:
        - trigger_crawl(platforms=['zhihu'])
        - trigger_crawl(save_to_local=True)
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['system'].trigger_crawl,
        platforms=platforms, save_to_local=save_to_local, include_url=include_url
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


# ==================== 存储同步工具 ====================

@mcp.tool
async def sync_from_remote(
    days: int = 7
) -> str:
    """
    从远程存储拉取数据到本地

    用于 MCP Server 等场景：爬虫存到远程云存储（如 Cloudflare R2），
    MCP Server 拉取到本地进行分析查询。

    Args:
        days: 拉取最近 N 天的数据，默认 7 天
              - 0: 不拉取
              - 7: 拉取最近一周的数据
              - 30: 拉取最近一个月的数据

    Returns:
        JSON格式的同步结果，包含：
        - success: 是否成功
        - synced_files: 成功同步的文件数量
        - synced_dates: 成功同步的日期列表
        - skipped_dates: 跳过的日期（本地已存在）
        - failed_dates: 失败的日期及错误信息
        - message: 操作结果描述

    Examples:
        - sync_from_remote()  # 拉取最近7天
        - sync_from_remote(days=30)  # 拉取最近30天

    Note:
        需要在 config/config.yaml 中配置远程存储（storage.remote）或设置环境变量：
        - S3_ENDPOINT_URL: 服务端点
        - S3_BUCKET_NAME: 存储桶名称
        - S3_ACCESS_KEY_ID: 访问密钥 ID
        - S3_SECRET_ACCESS_KEY: 访问密钥
    """
    tools = _get_tools()
    result = await asyncio.to_thread(tools['storage'].sync_from_remote, days=days)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def get_storage_status() -> str:
    """
    获取存储配置和状态

    查看当前存储后端配置、本地和远程存储的状态信息。

    Returns:
        JSON格式的存储状态信息，包含本地/远程存储状态和拉取配置
    """
    tools = _get_tools()
    result = await asyncio.to_thread(tools['storage'].get_storage_status)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def list_available_dates(
    source: str = "both"
) -> str:
    """
    列出本地/远程可用的日期范围

    查看本地和远程存储中有哪些日期的数据可用。

    Args:
        source: 数据来源
            - "local": 仅本地
            - "remote": 仅远程
            - "both": 同时列出并对比（默认）

    Returns:
        JSON格式的日期列表，包含各来源的日期信息和对比结果

    Examples:
        - list_available_dates()
        - list_available_dates(source="local")
    """
    tools = _get_tools()
    result = await asyncio.to_thread(tools['storage'].list_available_dates, source=source)
    return json.dumps(result, ensure_ascii=False, indent=2)


# ==================== 文章内容读取工具 ====================

@mcp.tool
async def read_article(
    url: str,
    timeout: int = 30
) -> str:
    """
    读取指定 URL 的文章内容，返回 LLM 友好的 Markdown 格式

    通过 Jina AI Reader 将网页转换为干净的 Markdown，自动去除广告、导航栏等噪音内容。
    适合用于：阅读新闻正文、获取文章详情、分析文章内容。

    **典型使用流程：**
    1. 先用 search_news(include_url=True) 搜索新闻获取链接
    2. 再用 read_article(url=链接) 读取正文内容
    3. AI 对 Markdown 正文进行分析、摘要、翻译等

    Args:
        url: 文章链接（必需），以 http:// 或 https:// 开头
        timeout: 请求超时时间（秒），默认 30，最大 60

    Returns:
        JSON格式的文章内容，包含完整 Markdown 正文

    Examples:
        - read_article(url="https://example.com/news/123")

    Note:
        - 使用 Jina AI Reader 免费服务（100 RPM 限制）；可设置环境变量 JINA_API_KEY 提升限额
        - 隐私提示：URL 会发送给第三方服务 r.jina.ai，由其抓取页面
        - 每次请求间隔 5 秒（内置速率控制）
        - 部分付费墙/登录墙页面可能无法完整获取
    """
    tools = _get_tools()
    timeout = min(max(timeout, 10), 60)
    result = await asyncio.to_thread(
        tools['article'].read_article,
        url=url, timeout=timeout
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def read_articles_batch(
    urls: List[str],
    timeout: int = 30
) -> str:
    """
    批量读取多篇文章内容（最多 5 篇，间隔 5 秒）

    逐篇请求文章内容，每篇之间自动间隔 5 秒以遵守速率限制。

    **典型使用流程：**
    1. 先用 search_news(include_url=True) 搜索新闻获取多个链接
    2. 再用 read_articles_batch(urls=[...]) 批量读取正文
    3. AI 对多篇文章进行对比分析、综合报告

    Args:
        urls: 文章链接列表（必需），最多处理 5 篇
        timeout: 每篇的请求超时时间（秒），默认 30

    Returns:
        JSON格式的批量读取结果，包含每篇的完整内容和状态

    Examples:
        - read_articles_batch(urls=["https://a.com/1", "https://b.com/2"])

    Note:
        - 单次最多读取 5 篇，超出部分会被跳过
        - 5 篇约需 25-30 秒（每篇间隔 5 秒）
        - 单篇失败不影响其他篇的读取
    """
    tools = _get_tools()
    timeout = min(max(timeout, 10), 60)
    result = await asyncio.to_thread(
        tools['article'].read_articles_batch,
        urls=urls, timeout=timeout
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


# ==================== 通知推送工具 ====================


@mcp.tool
async def get_channel_format_guide(channel: Optional[str] = None) -> str:
    """
    获取通知渠道的格式化策略指南

    返回各渠道支持的 Markdown 特性、格式限制和最佳格式化提示词。
    在调用 send_notification 之前使用此工具，可以了解目标渠道的格式要求，
    从而生成最佳排版效果的消息内容。

    各渠道格式差异概览：
    - 飞书：纯文本消息（Markdown 符号与 <font> 标签会被剥离，链接保留为“文本 URL”）
    - 钉钉：支持 ### 标题、**粗体**、> 引用、--- 分割线，不支持颜色
    - 企业微信：仅支持 **粗体**、[链接](url)、> 引用，不支持标题和分割线
    - Telegram：自动转为 HTML，支持粗体/斜体/删除线/代码/链接/引用块
    - ntfy：支持标准 Markdown，不支持颜色
    - Bark：iOS 推送，仅支持粗体和链接，内容需精简
    - Slack：自动转为 mrkdwn，*粗体*、~删除线~、<url|链接>
    - 邮件：自动转为完整 HTML 网页，支持标题/样式/分割线
    - 通用 Webhook：标准 Markdown 或自定义模板

    Args:
        channel: 指定渠道 ID（可选），不指定返回所有渠道策略
                 可选值: feishu, dingtalk, wework, telegram, email, ntfy, bark, slack, generic_webhook

    Returns:
        JSON格式的渠道格式化策略，包含支持特性、限制和格式化提示词

    Examples:
        - get_channel_format_guide()  # 获取所有渠道策略
        - get_channel_format_guide(channel="feishu")  # 获取飞书策略
        - get_channel_format_guide(channel="telegram")  # 获取 Telegram 策略
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['notification'].get_channel_format_guide,
        channel=channel
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def get_notification_channels() -> str:
    """
    获取所有已配置的通知渠道及其状态

    检测 config.yaml 和 .env 环境变量中的通知渠道配置。
    支持 9 个渠道：飞书、钉钉、企业微信、Telegram、邮件、ntfy、Bark、Slack、通用 Webhook。

    Returns:
        JSON格式的渠道状态，包含每个渠道是否已配置及配置来源

    Examples:
        - get_notification_channels()
    """
    tools = _get_tools()
    result = await asyncio.to_thread(tools['notification'].get_notification_channels)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool
async def send_notification(
    message: str,
    title: str = "TrendRadar 通知",
    channels: Optional[List[str]] = None,
) -> str:
    """
    向已配置的通知渠道发送消息

    接受 markdown 格式内容，内部自动适配各渠道的格式要求和限制：
    - 飞书：纯文本消息（自动剥离 **、<font>、# 等格式符号，链接转为“文本 URL”）
    - 钉钉：Markdown（自动降级标题为 ###、剥离 <font> 标签和删除线）
    - 企业微信：Markdown（自动剥离 # 标题、---、<font> 标签、删除线）
    - Telegram：HTML（自动转换 **→<b>、*→<i>、~~→<s>、>→<blockquote>）
    - Email：HTML 邮件（完整网页样式，支持 # 标题、---、粗体斜体）
    - ntfy：Markdown（自动剥离 <font> 标签）
    - Bark：Markdown（自动简化为粗体+链接，适配 iOS 推送）
    - Slack：mrkdwn（自动转换 **→*、~~→~、[text](url)→<url|text>）
    - 通用 Webhook：Markdown（支持自定义模板）

    提示：发送前可调用 get_channel_format_guide 获取目标渠道的详细格式化策略，
    以生成最佳排版效果的消息内容。

    Args:
        message: markdown 格式的消息内容（必需）
        title: 消息标题，默认 "TrendRadar 通知"
        channels: 指定发送的渠道列表，不指定则发送到所有已配置渠道
                  可选值: feishu, dingtalk, wework, telegram, email, ntfy, bark, slack, generic_webhook

    Returns:
        JSON格式的发送结果，包含每个渠道的发送状态

    Examples:
        - send_notification(message="**测试消息**\\n这是一条测试通知")
        - send_notification(message="紧急通知", title="系统告警", channels=["feishu", "dingtalk"])
    """
    tools = _get_tools()
    result = await asyncio.to_thread(
        tools['notification'].send_notification,
        message=message, title=title, channels=channels
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


# ==================== 启动入口 ====================

_TOOL_GROUPS = [
    ("日期解析工具（推荐优先调用）", ["resolve_date_range"]),
    ("基础数据查询", ["get_latest_news", "get_news_by_date", "get_trending_topics"]),
    ("RSS 数据查询", ["get_latest_rss", "search_rss", "get_rss_feeds_status"]),
    ("智能检索工具", ["search_news", "find_related_news"]),
    ("高级数据分析", [
        "analyze_topic_trend", "analyze_data_insights", "analyze_sentiment",
        "aggregate_news", "compare_periods", "generate_summary_report",
    ]),
    ("配置与系统管理", ["get_current_config", "get_system_status", "check_version", "trigger_crawl"]),
    ("存储同步工具", ["sync_from_remote", "get_storage_status", "list_available_dates"]),
    ("文章内容读取", ["read_article", "read_articles_batch"]),
    ("通知推送工具", ["get_channel_format_guide", "get_notification_channels", "send_notification"]),
]


def _eprint(*args, **kwargs) -> None:
    """日志输出到 stderr（stdio 模式下 stdout 是 JSON-RPC 通道）"""
    print(*args, file=sys.stderr, **kwargs)


def _configure_logging() -> None:
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


def apply_tool_policy(enable_side_effect_tools: Optional[bool] = None) -> List[str]:
    """按 MCP_ENABLE_SIDE_EFFECT_TOOLS 移除有副作用的工具，返回被移除的工具名"""
    if enable_side_effect_tools is None:
        enable_side_effect_tools = env_flag(ENV_ENABLE_SIDE_EFFECT_TOOLS, default=True)
    removed = []
    if not enable_side_effect_tools:
        for name in SIDE_EFFECT_TOOLS:
            try:
                mcp.remove_tool(name)
                removed.append(name)
            except Exception:  # 已移除 / 不存在
                pass
    return removed


def _isolate_stdout_for_stdio():
    """stdio 模式：把进程级 stdout（fd 1）重定向到 stderr，JSON-RPC 走单独复制出来的句柄。

    这样无论是本项目还是 trendradar 内部、第三方库、子进程的 print，都不会混进协议流。
    返回 JSON-RPC 输出用的 anyio 异步文件。
    """
    import io

    import anyio

    sys.stdout.flush()
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    protocol_out = io.TextIOWrapper(
        os.fdopen(protocol_fd, "wb", buffering=0), encoding="utf-8", write_through=True
    )
    return anyio.wrap_file(protocol_out)


def _run_stdio() -> None:
    import functools

    import fastmcp.server.mixins.transport as _transport
    from mcp.server.stdio import stdio_server

    protocol_stdout = _isolate_stdout_for_stdio()
    # fastmcp 内部用 stdio_server() 默认取 sys.stdout；这里把协议输出固定到复制出来的真实 stdout
    _transport.stdio_server = functools.partial(stdio_server, stdout=protocol_stdout)
    mcp.run(transport='stdio', show_banner=False)


def _print_banner(transport: str, host: str, port: int, project_root: Optional[str],
                  auth_enabled: bool, removed_tools: List[str]) -> None:
    _eprint()
    _eprint("=" * 60)
    _eprint(f"  TrendRadar MCP Server v{__version__}")
    _eprint("=" * 60)
    _eprint(f"  传输模式: {transport.upper()}")
    if transport == 'stdio':
        _eprint("  协议: MCP over stdio（stdout 仅输出 JSON-RPC，日志在 stderr）")
    else:
        _eprint(f"  服务器监听: http://{host}:{port}{DEFAULT_HTTP_PATH}")
        _eprint(f"  鉴权: {'Bearer Token（MCP_AUTH_TOKEN）' if auth_enabled else '未启用（仅本机回环地址可用）'}")
        _eprint("  Host/Origin 校验: 已启用（防 DNS rebinding）")
    _eprint(f"  项目目录: {project_root or '默认（代码所在目录）'}")
    if removed_tools:
        _eprint(f"  已禁用的副作用工具: {', '.join(removed_tools)}")
    _eprint()
    _eprint("  已注册的工具:")
    idx = 0
    for group, names in _TOOL_GROUPS:
        visible = [n for n in names if n not in removed_tools]
        if not visible:
            continue
        _eprint(f"    === {group} ===")
        for n in visible:
            _eprint(f"    {idx:>2}. {n}")
            idx += 1
    _eprint("=" * 60)
    _eprint()


def run_server(
    project_root: Optional[str] = None,
    transport: str = 'stdio',
    host: str = DEFAULT_HTTP_HOST,
    port: int = DEFAULT_HTTP_PORT
):
    """
    启动 MCP 服务器

    Args:
        project_root: 项目根目录路径
        transport: 传输模式，'stdio' 或 'http'
        host: HTTP 模式的监听地址，默认 127.0.0.1（监听非回环地址必须设置 MCP_AUTH_TOKEN）
        port: HTTP 模式的监听端口，默认 3333
    """
    _configure_logging()

    if transport not in ('stdio', 'http'):
        raise ValueError(f"不支持的传输模式: {transport}")

    security = None
    if transport == 'http':
        # 先做安全检查，不安全的配置直接拒绝启动（抛出 InsecureBindError）
        security = resolve_http_security(host)

    # 初始化工具实例
    _get_tools(project_root)
    removed = apply_tool_policy()

    _print_banner(transport, host, port, project_root,
                  bool(security and security.auth_enabled), removed)

    if transport == 'stdio':
        _run_stdio()
    else:
        mcp.run(
            transport='http',
            host=host,
            port=port,
            path=DEFAULT_HTTP_PATH,
            show_banner=False,
            **http_transport_kwargs(security),
        )


def build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description='TrendRadar MCP Server - 新闻热点聚合 MCP 工具服务器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
环境变量:
  MCP_AUTH_TOKEN                HTTP 模式的 Bearer Token（监听非回环地址时必填，至少 16 字符）
  MCP_ALLOWED_HOSTS             额外允许的 Host 头（逗号分隔，如 mcp.example.com）
  MCP_ALLOWED_ORIGINS           额外允许的浏览器 Origin（逗号分隔）
  MCP_ENABLE_SIDE_EFFECT_TOOLS  设为 false 时不注册 send_notification / trigger_crawl / sync_from_remote
  JINA_API_KEY                  read_article 使用的 Jina Reader API Key（可选）

详细配置教程请查看: README-Cherry-Studio.md
        """
    )
    parser.add_argument(
        '--transport',
        choices=['stdio', 'http'],
        default='stdio',
        help='传输模式：stdio (默认) 或 http'
    )
    parser.add_argument(
        '--host',
        default=DEFAULT_HTTP_HOST,
        help=f'HTTP 模式的监听地址，默认 {DEFAULT_HTTP_HOST}（非回环地址需设置 MCP_AUTH_TOKEN）'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f'HTTP 模式的监听端口，默认 {DEFAULT_HTTP_PORT}'
    )
    parser.add_argument(
        '--project-root',
        help='项目根目录路径'
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口（`python -m mcp_server.server` 与 `trendradar-mcp` 共用）"""
    args = build_arg_parser().parse_args(argv)
    try:
        run_server(
            project_root=args.project_root,
            transport=args.transport,
            host=args.host,
            port=args.port
        )
    except InsecureBindError as e:
        _eprint(f"[TrendRadar MCP] {e}")
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
