# coding=utf-8
"""报告数据准备。

从 NewsAnalyzer 中拆出的一批「只依赖 AppContext、不改动任何实例状态」的方法。
它们本质是纯数据变换，独立成模块后便于单独测试，也让 __main__ 只保留编排职责。

迁移原则：逐行搬运，行为完全不变，仅把 self.ctx 换成显式传入的 ctx。
"""
from typing import Dict, List, Optional


def prepare_current_title_info(results: Dict, time_info: str) -> Dict:
    """从当前抓取结果构建标题信息。

    纯函数：不依赖任何实例状态，输入输出完全由参数决定。

    Args:
        results: 原始爬取结果 {platform_id: {title: title_data}}
        time_info: 当前时间标识（HH:MM）

    Returns:
        {platform_id: {title: {first_time, last_time, count, ranks, url, mobileUrl}}}
    """
    title_info = {}
    for source_id, titles_data in results.items():
        title_info[source_id] = {}
        for title, title_data in titles_data.items():
            ranks = title_data.get("ranks", [])
            url = title_data.get("url", "")
            mobile_url = title_data.get("mobileUrl", "")

            title_info[source_id][title] = {
                "first_time": time_info,
                "last_time": time_info,
                "count": 1,
                "ranks": ranks,
                "url": url,
                "mobileUrl": mobile_url,
            }
    return title_info


def prepare_standalone_data(
    ctx,
    results: Dict,
    id_to_name: Dict,
    title_info: Optional[Dict] = None,
    rss_items: Optional[List[Dict]] = None,
) -> Optional[Dict]:
    """
    从原始数据中提取独立展示区数据

    纯数据准备方法，不检查 display.regions.standalone 开关。
    各消费者自行决定是否使用：
    - AI 分析：由 ai.include_standalone 控制（在 AI 分析层门控）
    - HTML 报告 / 邮件：由 display.regions.standalone 控制（在 HTML 生成前过滤）
    - Webhook 推送：由 display.regions.standalone 控制（在 dispatcher 层门控）

    Args:
        ctx: AppContext 实例（需要其 config）
        results: 原始爬取结果 {platform_id: {title: title_data}}
        id_to_name: 平台 ID 到名称的映射
        title_info: 标题元信息（含排名历史、时间等）
        rss_items: RSS 条目列表

    Returns:
        独立展示数据字典，如果未配置数据源返回 None
    """
    display_config = ctx.config.get("DISPLAY", {})
    standalone_config = display_config.get("STANDALONE", {})

    platform_ids = standalone_config.get("PLATFORMS", [])
    rss_feed_ids = standalone_config.get("RSS_FEEDS", [])
    max_items = standalone_config.get("MAX_ITEMS", 20)

    if not platform_ids and not rss_feed_ids:
        return None

    standalone_data = {
        "platforms": [],
        "rss_feeds": [],
    }

    # 找出最新批次时间（类似 current 模式的过滤逻辑）
    latest_time = None
    if title_info:
        for source_titles in title_info.values():
            for title_data in source_titles.values():
                last_time = title_data.get("last_time", "")
                if last_time:
                    if latest_time is None or last_time > latest_time:
                        latest_time = last_time

    # 提取热榜平台数据
    for platform_id in platform_ids:
        if platform_id not in results:
            continue

        platform_name = id_to_name.get(platform_id, platform_id)
        platform_titles = results[platform_id]

        items = []
        for title, title_data in platform_titles.items():
            # 获取元信息（如果有 title_info）
            meta = {}
            if title_info and platform_id in title_info and title in title_info[platform_id]:
                meta = title_info[platform_id][title]

            # 只保留当前在榜的话题（last_time 等于最新时间）
            if latest_time and meta:
                if meta.get("last_time") != latest_time:
                    continue

            # 使用当前热榜的排名数据（title_data）进行排序
            # title_data 包含的是爬虫返回的当前排名，用于保证独立展示区的顺序与热榜一致
            current_ranks = title_data.get("ranks", [])
            current_rank = current_ranks[-1] if current_ranks else 0

            # 用于显示的排名范围：合并历史排名和当前排名
            historical_ranks = meta.get("ranks", []) if meta else []
            # 合并去重，保持顺序
            all_ranks = historical_ranks.copy()
            for rank in current_ranks:
                if rank not in all_ranks:
                    all_ranks.append(rank)
            display_ranks = all_ranks if all_ranks else current_ranks

            item = {
                "title": title,
                "url": title_data.get("url", ""),
                "mobileUrl": title_data.get("mobileUrl", ""),
                "rank": current_rank,  # 用于排序的当前排名
                "ranks": display_ranks,  # 用于显示的排名范围（历史+当前）
                "first_time": meta.get("first_time", ""),
                "last_time": meta.get("last_time", ""),
                "count": meta.get("count", 1),
                "rank_timeline": meta.get("rank_timeline", []),
            }
            items.append(item)

        # 按当前排名排序
        items.sort(key=lambda x: x["rank"] if x["rank"] > 0 else 9999)

        # 限制条数
        if max_items > 0:
            items = items[:max_items]

        if items:
            standalone_data["platforms"].append({
                "id": platform_id,
                "name": platform_name,
                "items": items,
            })

    # 提取 RSS 数据
    if rss_items and rss_feed_ids:
        # 按 feed_id 分组
        feed_items_map = {}
        for item in rss_items:
            feed_id = item.get("feed_id", "")
            if feed_id in rss_feed_ids:
                if feed_id not in feed_items_map:
                    feed_items_map[feed_id] = {
                        "name": item.get("feed_name", feed_id),
                        "items": [],
                    }
                feed_items_map[feed_id]["items"].append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "published_at": item.get("published_at", ""),
                    "author": item.get("author", ""),
                })

        # 限制条数并添加到结果
        for feed_id in rss_feed_ids:
            if feed_id in feed_items_map:
                feed_data = feed_items_map[feed_id]
                items = feed_data["items"]
                if max_items > 0:
                    items = items[:max_items]
                if items:
                    standalone_data["rss_feeds"].append({
                        "id": feed_id,
                        "name": feed_data["name"],
                        "items": items,
                    })

    # 如果没有任何数据，返回 None
    if not standalone_data["platforms"] and not standalone_data["rss_feeds"]:
        return None

    return standalone_data
