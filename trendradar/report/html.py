# coding=utf-8
"""
HTML 报告渲染模块

提供 HTML 格式的热点新闻报告生成功能
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Callable

from trendradar.report.helpers import html_escape, calculate_rank_trend
from trendradar.utils.time import convert_time_for_display
from trendradar.ai.formatter import render_ai_analysis_html_rich
from trendradar.report.html_styles import BASE_HEAD_HTML
from trendradar.report.html_script import TAIL_FOOTER_HTML, TAIL_SCRIPT_HTML


# 生成 RSS 统计内容
def render_rss_stats_html(stats: List[Dict], title: str = "RSS 订阅更新") -> str:
    """渲染 RSS 统计区块 HTML

        Args:
            stats: RSS 分组统计列表，格式与热榜一致：
                [
                    {
                        "word": "关键词",
                        "count": 5,
                        "titles": [
                            {
                                "title": "标题",
                                "source_name": "Feed 名称",
                                "time_display": "12-29 08:20",
                                "url": "...",
                                "is_new": True/False
                            }
                        ]
                    }
                ]
            title: 区块标题

        Returns:
            渲染后的 HTML 字符串
        """
    if not stats:
        return ""

    # 计算总条目数
    total_count = sum(stat.get("count", 0) for stat in stats)
    if total_count == 0:
        return ""

    rss_html = f"""
                <div class="rss-section">
                    <div class="rss-section-header">
                        <div class="rss-section-title">{title}</div>
                        <div class="rss-section-count">{total_count} 条</div>
                    </div>
                    <div class="rss-feeds-grid">"""

    # 按关键词分组渲染（与热榜格式一致）
    for stat in stats:
        keyword = stat.get("word", "")
        titles = stat.get("titles", [])
        if not titles:
            continue

        keyword_count = len(titles)

        rss_html += f"""
                    <div class="feed-group">
                        <div class="feed-header collapsible" onclick="this.parentElement.classList.toggle('collapsed')">
                            <div class="feed-name">{html_escape(keyword)} <span class="collapse-icon">▼</span></div>
                            <div class="feed-count">{keyword_count} 条</div>
                        </div>"""

        for title_data in titles:
            item_title = title_data.get("title", "")
            url = title_data.get("url", "")
            time_display = title_data.get("time_display", "")
            source_name = title_data.get("source_name", "")
            is_new = title_data.get("is_new", False)

            rss_html += """
                        <div class="rss-item">
                            <div class="rss-meta">"""

            if time_display:
                rss_html += f'<span class="rss-time">{html_escape(time_display)}</span>'

            if source_name:
                rss_html += f'<span class="rss-author">{html_escape(source_name)}</span>'

            if is_new:
                rss_html += '<span class="rss-author" style="color: #dc2626;">NEW</span>'

            rss_html += """
                            </div>
                            <div class="rss-title">"""

            escaped_title = html_escape(item_title)
            if url:
                escaped_url = html_escape(url)
                rss_html += f'<a href="{escaped_url}" target="_blank" class="rss-link">{escaped_title}</a>'
            else:
                rss_html += escaped_title

            rss_html += """
                            </div>
                        </div>"""

        rss_html += """
                    </div>"""

    rss_html += """
                    </div>
                </div>"""
    return rss_html


# 生成独立展示区内容
def render_standalone_html(data: Optional[Dict]) -> str:
    """渲染独立展示区 HTML（复用热点词汇统计区样式）

        Args:
            data: 独立展示数据，格式：
                {
                    "platforms": [
                        {
                            "id": "zhihu",
                            "name": "知乎热榜",
                            "items": [
                                {
                                    "title": "标题",
                                    "url": "链接",
                                    "rank": 1,
                                    "ranks": [1, 2, 1],
                                    "first_time": "08:00",
                                    "last_time": "12:30",
                                    "count": 3,
                                }
                            ]
                        }
                    ],
                    "rss_feeds": [
                        {
                            "id": "hacker-news",
                            "name": "Hacker News",
                            "items": [
                                {
                                    "title": "标题",
                                    "url": "链接",
                                    "published_at": "2025-01-07T08:00:00",
                                    "author": "作者",
                                }
                            ]
                        }
                    ]
                }

        Returns:
            渲染后的 HTML 字符串
        """
    if not data:
        return ""

    platforms = data.get("platforms", [])
    rss_feeds = data.get("rss_feeds", [])

    if not platforms and not rss_feeds:
        return ""

    # 计算总条目数
    total_platform_items = sum(len(p.get("items", [])) for p in platforms)
    total_rss_items = sum(len(f.get("items", [])) for f in rss_feeds)
    total_count = total_platform_items + total_rss_items

    if total_count == 0:
        return ""

    # 收集所有分组信息用于生成 tab
    all_groups = []
    for p in platforms:
        items = p.get("items", [])
        if items:
            all_groups.append({"name": p.get("name", p.get("id", "")), "count": len(items)})
    for f in rss_feeds:
        items = f.get("items", [])
        if items:
            all_groups.append({"name": f.get("name", f.get("id", "")), "count": len(items)})

    standalone_html = f"""
                <div class="standalone-section">
                    <div class="standalone-section-header">
                        <div class="standalone-section-title">独立展示区</div>
                        <div class="standalone-section-count">{total_count} 条</div>
                    </div>"""

    # 生成 tab 栏（2+ 分组时）
    if len(all_groups) >= 2:
        standalone_html += """
                    <div class="tab-bar standalone-tab-bar">"""
        for idx, g in enumerate(all_groups):
            active = ' active' if idx == 0 else ''
            standalone_html += f"""
                        <button class="tab-btn{active}" data-standalone-tab="{idx}">{html_escape(g["name"])}<span class="tab-count">{g["count"]}</span></button>"""
        standalone_html += f"""
                        <button class="tab-btn" data-standalone-tab="all">全部<span class="tab-count">{total_count}</span></button>
                    </div>"""

    standalone_html += """
                    <div class="standalone-groups-grid">"""

    group_idx = 0
    # 渲染热榜平台（复用 word-group 结构）
    for platform in platforms:
        platform_name = platform.get("name", platform.get("id", ""))
        items = platform.get("items", [])
        if not items:
            continue

        standalone_html += f"""
                    <div class="standalone-group" data-standalone-tab="{group_idx}">
                        <div class="standalone-header">
                            <div class="standalone-name">{html_escape(platform_name)}</div>
                            <div class="standalone-count">{len(items)} 条</div>
                        </div>"""

        # 渲染每个条目（复用 news-item 结构）
        for j, item in enumerate(items, 1):
            title = item.get("title", "")
            url = item.get("url", "") or item.get("mobileUrl", "")
            rank = item.get("rank", 0)
            ranks = item.get("ranks", [])
            first_time = item.get("first_time", "")
            last_time = item.get("last_time", "")
            count = item.get("count", 1)

            standalone_html += f"""
                        <div class="news-item">
                            <div class="news-number">{j}</div>
                            <div class="news-content">
                                <div class="news-header">"""

            # 排名显示（复用 rank-num 样式，无 # 前缀）
            if ranks:
                min_rank = min(ranks)
                max_rank = max(ranks)

                # 确定排名等级
                if min_rank <= 3:
                    rank_class = "top"
                elif min_rank <= 10:
                    rank_class = "high"
                else:
                    rank_class = ""

                if min_rank == max_rank:
                    rank_text = str(min_rank)
                else:
                    rank_text = f"{min_rank}-{max_rank}"

                standalone_html += f'<span class="rank-num {rank_class}">{rank_text}</span>'
            elif rank > 0:
                if rank <= 3:
                    rank_class = "top"
                elif rank <= 10:
                    rank_class = "high"
                else:
                    rank_class = ""
                standalone_html += f'<span class="rank-num {rank_class}">{rank}</span>'

            # 时间显示（复用 time-info 样式，将 HH-MM 转换为 HH:MM）
            if first_time and last_time and first_time != last_time:
                first_time_display = convert_time_for_display(first_time)
                last_time_display = convert_time_for_display(last_time)
                standalone_html += f'<span class="time-info">{html_escape(first_time_display)}~{html_escape(last_time_display)}</span>'
            elif first_time:
                first_time_display = convert_time_for_display(first_time)
                standalone_html += f'<span class="time-info">{html_escape(first_time_display)}</span>'

            # 出现次数（复用 count-info 样式）
            if count > 1:
                standalone_html += f'<span class="count-info">{count}次</span>'

            standalone_html += """
                                </div>
                                <div class="news-title">"""

            # 标题和链接（复用 news-link 样式）
            escaped_title = html_escape(title)
            if url:
                escaped_url = html_escape(url)
                standalone_html += f'<a href="{escaped_url}" target="_blank" class="news-link">{escaped_title}</a>'
            else:
                standalone_html += escaped_title

            standalone_html += """
                                </div>
                            </div>
                        </div>"""

        standalone_html += """
                    </div>"""
        group_idx += 1

    # 渲染 RSS 源（复用相同结构）
    for feed in rss_feeds:
        feed_name = feed.get("name", feed.get("id", ""))
        items = feed.get("items", [])
        if not items:
            continue

        standalone_html += f"""
                    <div class="standalone-group" data-standalone-tab="{group_idx}">
                        <div class="standalone-header">
                            <div class="standalone-name">{html_escape(feed_name)}</div>
                            <div class="standalone-count">{len(items)} 条</div>
                        </div>"""

        for j, item in enumerate(items, 1):
            title = item.get("title", "")
            url = item.get("url", "")
            published_at = item.get("published_at", "")
            author = item.get("author", "")

            standalone_html += f"""
                        <div class="news-item">
                            <div class="news-number">{j}</div>
                            <div class="news-content">
                                <div class="news-header">"""

            # 时间显示（格式化 ISO 时间）
            if published_at:
                try:
                    from datetime import datetime as dt
                    if "T" in published_at:
                        dt_obj = dt.fromisoformat(published_at.replace("Z", "+00:00"))
                        time_display = dt_obj.strftime("%m-%d %H:%M")
                    else:
                        time_display = published_at
                except Exception:
                    time_display = published_at

                standalone_html += f'<span class="time-info">{html_escape(time_display)}</span>'

            # 作者显示
            if author:
                standalone_html += f'<span class="source-name">{html_escape(author)}</span>'

            standalone_html += """
                                </div>
                                <div class="news-title">"""

            escaped_title = html_escape(title)
            if url:
                escaped_url = html_escape(url)
                standalone_html += f'<a href="{escaped_url}" target="_blank" class="news-link">{escaped_title}</a>'
            else:
                standalone_html += escaped_title

            standalone_html += """
                                </div>
                            </div>
                        </div>"""

        standalone_html += """
                    </div>"""
        group_idx += 1

    standalone_html += """
                    </div>
                </div>"""
    return standalone_html


def add_section_divider(content: str) -> str:
    """为内容的外层 div 添加 section-divider 类"""
    if not content or 'class="' not in content:
        return content
    first_class_pos = content.find('class="')
    if first_class_pos != -1:
        insert_pos = first_class_pos + len('class="')
        return content[:insert_pos] + "section-divider " + content[insert_pos:]
    return content


def _render_info_items(
    report_data: Dict, total_titles: int, mode: str, now: datetime,
    rss_new_items: Optional[List[Dict]], ai_analysis: Optional[Any],
) -> str:
    """渲染页头 8 项信息条（报告类型/生成时间/命中统计/AI 状态）"""
    parts_html = ""
    # 处理报告类型显示
    if mode == "current":
        mode_display = "当前榜单"
    elif mode == "incremental":
        mode_display = "增量分析"
    else:
        mode_display = "全天汇总"

    # 计算各项数据
    hot_news_count = sum(len(stat["titles"]) for stat in report_data["stats"])
    new_count = report_data.get("total_new_count", 0)

    # 从元数据获取 RSS 和平台信息
    hotlist_total = report_data.get("hotlist_total", total_titles)
    platform_total = report_data.get("platform_total", 0)
    failed_count = len(report_data.get("failed_ids", []))
    platform_success = platform_total - failed_count if platform_total else 0
    rss_matched = report_data.get("rss_matched_count", 0)
    rss_total = report_data.get("rss_total_count", 0)
    rss_source_total = report_data.get("rss_source_total", 0)
    rss_source_failed = report_data.get("rss_source_failed", 0)
    rss_source_success = max(0, rss_source_total - rss_source_failed)

    # 1. 报告类型
    parts_html += f"""
                    <div class="info-item">
                        <span class="info-label">报告类型</span>
                        <span class="info-value">{mode_display}</span>
                    </div>"""

    # 2. 生成时间
    parts_html += f"""
                    <div class="info-item">
                        <span class="info-label">生成时间</span>
                        <span class="info-value">{now.strftime("%m-%d %H:%M")}</span>
                    </div>"""

    # 3. 热榜命中
    parts_html += f"""
                    <div class="info-item">
                        <span class="info-label">热榜命中</span>
                        <span class="info-value">{hot_news_count} / {hotlist_total}</span>
                    </div>"""

    # 4. RSS 命中
    if rss_source_total > 0:
        rss_value = f"{rss_matched} / {rss_total}"
    else:
        rss_value = "未启用"
    parts_html += f"""
                    <div class="info-item">
                        <span class="info-label">RSS 命中</span>
                        <span class="info-value">{rss_value}</span>
                    </div>"""

    # 5. 热榜平台
    if platform_total > 0:
        platform_value = f"{platform_success}/{platform_total}"
    else:
        platform_value = "--"
    parts_html += f"""
                    <div class="info-item">
                        <span class="info-label">热榜平台</span>
                        <span class="info-value">{platform_value}</span>
                    </div>"""

    # 6. RSS 源
    if rss_source_total > 0:
        rss_source_value = f"{rss_source_success}/{rss_source_total}"
    else:
        rss_source_value = "--"
    parts_html += f"""
                    <div class="info-item">
                        <span class="info-label">RSS 源</span>
                        <span class="info-value">{rss_source_value}</span>
                    </div>"""

    # 7. 新增热点（热榜新增 + RSS 新增）
    rss_new_count = sum(len(stat.get("titles", [])) for stat in (rss_new_items or []))
    total_new = new_count + rss_new_count
    new_value = f"{new_count} + {rss_new_count}" if total_new > 0 else "0"
    parts_html += f"""
                    <div class="info-item">
                        <span class="info-label">新增热点</span>
                        <span class="info-value">{new_value}</span>
                    </div>"""

    # 8. AI 分析
    if ai_analysis and getattr(ai_analysis, "success", False):
        hotlist_analyzed = getattr(ai_analysis, "hotlist_analyzed", 0)
        rss_analyzed = getattr(ai_analysis, "rss_analyzed", 0)
        standalone_analyzed = getattr(ai_analysis, "standalone_analyzed", 0)
        ai_include_rss = getattr(ai_analysis, "include_rss", True)
        ai_include_standalone = getattr(ai_analysis, "include_standalone", False)

        ai_parts = [str(hotlist_analyzed)]
        if ai_include_rss:
            ai_parts.append(str(rss_analyzed))
        if ai_include_standalone:
            ai_parts.append(str(standalone_analyzed))
        ai_value = " + ".join(ai_parts) if sum(int(p) for p in ai_parts) > 0 else "0"
    elif ai_analysis:
        if getattr(ai_analysis, "skipped", False):
            ai_value = "已跳过"
        else:
            ai_value = "待配置"
    else:
        ai_value = "未启用"
    parts_html += f"""
                    <div class="info-item">
                        <span class="info-label">AI 分析</span>
                        <span class="info-value">{ai_value}</span>
                    </div>"""
    return parts_html


def _render_error_section(failed_ids: List) -> str:
    """渲染请求失败平台的错误区（无失败时返回空串，追加空串不改变输出）"""
    error_html = ""
    # 处理失败ID错误信息
    if failed_ids:
        error_html += """
                <div class="error-section">
                    <div class="error-title">⚠️ 请求失败的平台</div>
                    <ul class="error-list">"""
        for id_value in failed_ids:
            error_html += f'<li class="error-item">{html_escape(id_value)}</li>'
        error_html += """
                    </ul>
                </div>"""
    return error_html


def _render_hotlist_section(report_data: Dict, display_mode: str) -> str:
    """渲染热榜统计区（tab 栏 + 关键词分组 + 外层包装；无数据返回空串）"""
    # 生成热点词汇统计部分的HTML
    stats_html = ""
    tab_bar_html = ""
    if report_data["stats"]:
        total_count = len(report_data["stats"])

        # 生成 Tab 栏 HTML
        total_news_count = sum(s["count"] for s in report_data["stats"])
        tab_bar_html = '<div class="tab-bar-wrapper"><div class="tab-bar">'
        tab_bar_html += f'<button class="tab-btn" data-tab-index="all">全部<span class="tab-count">{total_news_count}</span></button>'
        for tab_i, tab_stat in enumerate(report_data["stats"]):
            escaped_tab_word = html_escape(tab_stat["word"])
            tab_count = tab_stat["count"]
            tab_bar_html += f'<button class="tab-btn" data-tab-index="{tab_i}">{escaped_tab_word}<span class="tab-count">{tab_count}</span></button>'
        tab_bar_html += '</div></div>'

        for i, stat in enumerate(report_data["stats"], 1):
            count = stat["count"]

            # 确定热度等级
            if count >= 10:
                count_class = "hot"
            elif count >= 5:
                count_class = "warm"
            else:
                count_class = ""

            escaped_word = html_escape(stat["word"])

            stats_html += f"""
                <div class="word-group" data-tab-index="{i - 1}">
                    <div class="word-header">
                        <div class="word-info">
                            <div class="word-name">{escaped_word}</div>
                            <div class="word-count {count_class}">{count} 条</div>
                        </div>
                        <div class="word-index"><span class="collapse-icon">▼</span>{i}/{total_count}</div>
                    </div>"""

            # 处理每个词组下的新闻标题，给每条新闻标上序号
            for j, title_data in enumerate(stat["titles"], 1):
                is_new = title_data.get("is_new", False)
                new_class = "new" if is_new else ""

                stats_html += f"""
                    <div class="news-item {new_class}">
                        <div class="news-number">{j}</div>
                        <div class="news-content">
                            <div class="news-header">"""

                # 根据 display_mode 决定显示来源还是关键词
                if display_mode == "keyword":
                    # keyword 模式：显示来源
                    stats_html += f'<span class="source-name">{html_escape(title_data["source_name"])}</span>'
                else:
                    # platform 模式：显示关键词
                    matched_keyword = title_data.get("matched_keyword", "")
                    if matched_keyword:
                        stats_html += f'<span class="keyword-tag">[{html_escape(matched_keyword)}]</span>'

                # 处理排名显示
                ranks = title_data.get("ranks", [])
                if ranks:
                    min_rank = min(ranks)
                    max_rank = max(ranks)
                    rank_threshold = title_data.get("rank_threshold", 10)

                    # 确定排名等级
                    if min_rank <= 3:
                        rank_class = "top"
                    elif min_rank <= rank_threshold:
                        rank_class = "high"
                    else:
                        rank_class = ""

                    if min_rank == max_rank:
                        rank_text = str(min_rank)
                    else:
                        rank_text = f"{min_rank}-{max_rank}"

                    # 计算趋势箭头
                    rank_timeline = title_data.get("rank_timeline", [])
                    trend = calculate_rank_trend(rank_timeline, ranks)
                    trend_html = ""
                    if trend == "up":
                        trend_html = '<span class="trend-up">📈</span>'
                    elif trend == "down":
                        trend_html = '<span class="trend-down">📉</span>'

                    stats_html += f'<span class="rank-num {rank_class}">{rank_text}</span>{trend_html}'

                # 处理时间显示
                time_display = title_data.get("time_display", "")
                if time_display:
                    # 简化时间显示格式，将波浪线替换为~
                    simplified_time = (
                        time_display.replace(" ~ ", "~")
                        .replace("[", "")
                        .replace("]", "")
                    )
                    stats_html += (
                        f'<span class="time-info">{html_escape(simplified_time)}</span>'
                    )

                # 处理出现次数
                count_info = title_data.get("count", 1)
                if count_info > 1:
                    stats_html += f'<span class="count-info">{count_info}次</span>'

                stats_html += """
                            </div>
                            <div class="news-title">"""

                # 处理标题和链接
                escaped_title = html_escape(title_data["title"])
                link_url = title_data.get("mobile_url") or title_data.get("url", "")

                if link_url:
                    escaped_url = html_escape(link_url)
                    stats_html += f'<a href="{escaped_url}" target="_blank" class="news-link">{escaped_title}</a>'
                else:
                    stats_html += escaped_title

                stats_html += """
                            </div>
                        </div>
                    </div>"""

            stats_html += """
                </div>"""

    # 给热榜统计添加外层包装
    if stats_html:
        stats_html = f"""
                <div class="hotlist-section">{tab_bar_html}{stats_html}
                </div>"""
    return stats_html


def _render_new_items_section(report_data: Dict, show_new_section: bool) -> str:
    """渲染本次新增热点区（未启用或无新增返回空串）"""
    # 生成新增新闻区域的HTML
    new_titles_html = ""
    if show_new_section and report_data["new_titles"]:
        new_titles_html += f"""
                <div class="new-section">
                    <div class="new-section-title">本次新增热点 (共 {report_data['total_new_count']} 条)</div>
                    <div class="new-sources-grid">"""

        for source_data in report_data["new_titles"]:
            escaped_source = html_escape(source_data["source_name"])
            titles_count = len(source_data["titles"])

            new_titles_html += f"""
                    <div class="new-source-group">
                        <div class="new-source-title">{escaped_source} · {titles_count}条</div>"""

            # 为新增新闻也添加序号
            for idx, title_data in enumerate(source_data["titles"], 1):
                ranks = title_data.get("ranks", [])

                # 处理新增新闻的排名显示
                rank_class = ""
                if ranks:
                    min_rank = min(ranks)
                    if min_rank <= 3:
                        rank_class = "top"
                    elif min_rank <= title_data.get("rank_threshold", 10):
                        rank_class = "high"

                    if len(ranks) == 1:
                        rank_text = str(ranks[0])
                    else:
                        rank_text = f"{min(ranks)}-{max(ranks)}"
                else:
                    rank_text = "?"

                new_titles_html += f"""
                        <div class="new-item">
                            <div class="new-item-number">{idx}</div>
                            <div class="new-item-rank {rank_class}">{rank_text}</div>
                            <div class="new-item-content">
                                <div class="new-item-title">"""

                # 处理新增新闻的链接
                escaped_title = html_escape(title_data["title"])
                link_url = title_data.get("mobile_url") or title_data.get("url", "")

                if link_url:
                    escaped_url = html_escape(link_url)
                    new_titles_html += f'<a href="{escaped_url}" target="_blank" class="news-link">{escaped_title}</a>'
                else:
                    new_titles_html += escaped_title

                new_titles_html += """
                                </div>
                            </div>
                        </div>"""

            new_titles_html += """
                    </div>"""

        new_titles_html += """
                    </div>
                </div>"""
    return new_titles_html


def render_html_content(
    report_data: Dict,
    total_titles: int,
    mode: str = "daily",
    update_info: Optional[Dict] = None,
    *,
    region_order: Optional[List[str]] = None,
    get_time_func: Optional[Callable[[], datetime]] = None,
    rss_items: Optional[List[Dict]] = None,
    rss_new_items: Optional[List[Dict]] = None,
    display_mode: str = "keyword",
    standalone_data: Optional[Dict] = None,
    ai_analysis: Optional[Any] = None,
    show_new_section: bool = True,
) -> str:
    """渲染HTML内容

    Args:
        report_data: 报告数据字典，包含 stats, new_titles, failed_ids, total_new_count
        total_titles: 新闻总数
        mode: 报告模式 ("daily", "current", "incremental")
        update_info: 更新信息（可选）
        region_order: 区域显示顺序列表
        get_time_func: 获取当前时间的函数（可选，默认使用 datetime.now）
        rss_items: RSS 统计条目列表（可选）
        rss_new_items: RSS 新增条目列表（可选）
        display_mode: 显示模式 ("keyword"=按关键词分组, "platform"=按平台分组)
        standalone_data: 独立展示区数据（可选），包含 platforms 和 rss_feeds
        ai_analysis: AI 分析结果对象（可选），AIAnalysisResult 实例
        show_new_section: 是否显示新增热点区域

    Returns:
        渲染后的 HTML 字符串
    """
    # 默认区域顺序
    default_region_order = ["hotlist", "rss", "new_items", "standalone", "ai_analysis"]
    if region_order is None:
        region_order = default_region_order

    html = BASE_HEAD_HTML

    # 使用提供的时间函数或默认 datetime.now
    if get_time_func:
        now = get_time_func()
    else:
        now = datetime.now()

    html += _render_info_items(report_data, total_titles, mode, now, rss_new_items, ai_analysis)


    html += """
                </div>
            </div>

            <div class="content">
                <div class="search-bar">
                    <input type="text" class="search-input" placeholder="搜索新闻标题..." oninput="handleSearch(this.value)">
                </div>"""

    html += _render_error_section(report_data["failed_ids"])

    stats_html = _render_hotlist_section(report_data, display_mode)

    new_titles_html = _render_new_items_section(report_data, show_new_section)



    # 生成 RSS 统计和新增 HTML
    rss_stats_html = render_rss_stats_html(rss_items, "RSS 订阅更新") if rss_items else ""
    rss_new_html = render_rss_stats_html(rss_new_items, "RSS 新增更新") if rss_new_items else ""

    # 生成独立展示区 HTML
    standalone_html = render_standalone_html(standalone_data)

    # 生成 AI 分析 HTML
    ai_html = render_ai_analysis_html_rich(ai_analysis) if ai_analysis else ""

    # 准备各区域内容映射
    region_contents = {
        "hotlist": stats_html,
        "rss": rss_stats_html,
        "new_items": (new_titles_html, rss_new_html),  # 元组，分别处理
        "standalone": standalone_html,
        "ai_analysis": ai_html,
    }


    # 按 region_order 顺序组装内容，动态添加分割线
    has_previous_content = False
    for region in region_order:
        content = region_contents.get(region, "")
        if region == "new_items":
            # 特殊处理 new_items 区域（包含热榜新增和 RSS 新增两部分）
            new_html, rss_new = content
            if new_html:
                if has_previous_content:
                    new_html = add_section_divider(new_html)
                html += new_html
                has_previous_content = True
            if rss_new:
                if has_previous_content:
                    rss_new = add_section_divider(rss_new)
                html += rss_new
                has_previous_content = True
        elif content:
            if has_previous_content:
                content = add_section_divider(content)
            html += content
            has_previous_content = True

    html += TAIL_FOOTER_HTML

    if update_info:
        html += f"""
                    <br>
                    <span style="color: #ea580c; font-weight: 500;">
                        发现新版本 {update_info['remote_version']}，当前版本 {update_info['current_version']}
                    </span>"""

    html += TAIL_SCRIPT_HTML

    return html
