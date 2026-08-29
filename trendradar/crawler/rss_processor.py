# coding=utf-8
"""RSS 抓取与处理。

从 NewsAnalyzer 拆出的一类职责：RSS 源的抓取、按报告模式分流、条目转换、
关键词过滤、独立 HTML 报告生成。

设计要点
--------
1. report_mode / rank_threshold 作为**方法参数**传入而非构造参数。
   调度器会在运行时覆盖 report_mode（见 core/loader 的 REPORT_MODE 覆盖逻辑），
   若在构造时固定下来，处理器会拿到过期的值。

2. 抓取统计（source_total / source_failed / total_count）由本类持有，
   NewsAnalyzer 通过同名属性转发，因此外部读取方式不变。

3. 依赖（RSSFetcher、count_rss_frequency）保持函数内延迟导入，
   与迁移前一致，避免引入循环导入和额外的启动开销。
"""
from typing import Dict, List, Optional, Tuple


class RSSProcessor:
    """RSS 数据的抓取与加工。"""

    def __init__(
        self,
        ctx,
        storage_manager,
        proxy_url: str = "",
    ):
        """
        Args:
            ctx: AppContext（需要 rss_enabled / rss_feeds / rss_config / config /
                 timezone / load_frequency_words）
            storage_manager: 存储后端
            proxy_url: 爬虫默认代理（RSS 无专属代理时回退使用）

        注意：frequency_file 与 report_mode 都在运行时可能被调度器覆盖
        （见 __main__ 的 _initialize_and_check_config），因此一律作为方法参数
        传入，绝不在此处固定。
        """
        self.ctx = ctx
        self.storage_manager = storage_manager
        self.proxy_url = proxy_url

        # 抓取统计（供报告头展示）
        self.source_total = 0
        self.source_failed = 0
        self.total_count = 0

    # ------------------------------------------------------------------
    # 抓取
    # ------------------------------------------------------------------
    def crawl(
        self, report_mode: str, rank_threshold: int, frequency_file: str
    ) -> Tuple[Optional[List[Dict]], Optional[List[Dict]], Optional[List[Dict]], set]:
        """
        执行 RSS 数据抓取

        Returns:
            (rss_items, rss_new_items, raw_rss_items, rss_new_urls) 元组：
            - rss_items: 统计条目列表（按模式处理，用于统计区块）
            - rss_new_items: 新增条目列表（用于新增区块）
            - raw_rss_items: 原始 RSS 条目列表（用于独立展示区）
            - rss_new_urls: 原始新增 RSS 条目的 URL 集合（用于 AI 模式 is_new 检测）
            如果未启用或失败返回 (None, None, None, set())
        """
        if not self.ctx.rss_enabled:
            return None, None, None, set()

        rss_feeds = self.ctx.rss_feeds
        if not rss_feeds:
            print("[RSS] 未配置任何 RSS 源")
            return None, None, None, set()

        try:
            from trendradar.crawler.rss import RSSFetcher, RSSFeedConfig

            # 构建 RSS 源配置
            feeds = []
            for feed_config in rss_feeds:
                # 读取并验证单个 feed 的 max_age_days（可选）
                max_age_days_raw = feed_config.get("max_age_days")
                max_age_days = None
                if max_age_days_raw is not None:
                    try:
                        max_age_days = int(max_age_days_raw)
                        if max_age_days < 0:
                            feed_id = feed_config.get("id", "unknown")
                            print(f"[警告] RSS feed '{feed_id}' 的 max_age_days 为负数，将使用全局默认值")
                            max_age_days = None
                    except (ValueError, TypeError):
                        feed_id = feed_config.get("id", "unknown")
                        print(f"[警告] RSS feed '{feed_id}' 的 max_age_days 格式错误：{max_age_days_raw}")
                        max_age_days = None

                feed = RSSFeedConfig(
                    id=feed_config.get("id", ""),
                    name=feed_config.get("name", ""),
                    url=feed_config.get("url", ""),
                    max_items=feed_config.get("max_items", 50),
                    enabled=feed_config.get("enabled", True),
                    max_age_days=max_age_days,  # None=使用全局，0=禁用，>0=覆盖
                )
                if feed.id and feed.url and feed.enabled:
                    feeds.append(feed)

            if not feeds:
                print("[RSS] 没有启用的 RSS 源")
                return None, None, None, set()

            # 创建抓取器
            rss_config = self.ctx.rss_config
            # RSS 代理：优先使用 RSS 专属代理，否则使用爬虫默认代理
            rss_proxy_url = rss_config.get("PROXY_URL", "") or self.proxy_url or ""
            # 获取配置的时区
            from trendradar.utils.time import DEFAULT_TIMEZONE

            timezone = self.ctx.config.get("TIMEZONE", DEFAULT_TIMEZONE)
            # 获取新鲜度过滤配置
            freshness_config = rss_config.get("FRESHNESS_FILTER", {})
            freshness_enabled = freshness_config.get("ENABLED", True)
            default_max_age_days = freshness_config.get("MAX_AGE_DAYS", 3)

            fetcher = RSSFetcher(
                feeds=feeds,
                request_interval=rss_config.get("REQUEST_INTERVAL", 2000),
                timeout=rss_config.get("TIMEOUT", 15),
                use_proxy=rss_config.get("USE_PROXY", False),
                proxy_url=rss_proxy_url,
                timezone=timezone,
                freshness_enabled=freshness_enabled,
                default_max_age_days=default_max_age_days,
            )

            # 抓取数据
            rss_data = fetcher.fetch_all()

            self.source_total = len(feeds)
            self.source_failed = len(rss_data.failed_ids)

            # 保存到存储后端
            if self.storage_manager.save_rss_data(rss_data):
                print(f"[RSS] 数据已保存到存储后端")

                # 处理 RSS 数据（按模式过滤）并返回用于合并推送
                return self.process_by_mode(rss_data, report_mode, rank_threshold, frequency_file)
            else:
                print(f"[RSS] 数据保存失败")
                return None, None, None, set()

        except ImportError as e:
            print(f"[RSS] 缺少依赖: {e}")
            print("[RSS] 请安装 feedparser: pip install feedparser")
            return None, None, None, set()
        except Exception as e:
            print(f"[RSS] 抓取失败: {e}")
            return None, None, None, set()

    # ------------------------------------------------------------------
    # 按模式分流
    # ------------------------------------------------------------------
    def process_by_mode(
        self, rss_data, report_mode: str, rank_threshold: int, frequency_file: str
    ) -> Tuple[Optional[List[Dict]], Optional[List[Dict]], Optional[List[Dict]], set]:
        """
        按报告模式处理 RSS 数据，返回与热榜相同格式的统计结构

        三种模式：
        - daily: 当日汇总，统计=当天所有条目，新增=本次新增条目
        - current: 当前榜单，统计=当前榜单条目，新增=本次新增条目
        - incremental: 增量模式，统计=新增条目，新增=无

        Args:
            rss_data: 当前抓取的 RSSData 对象
            report_mode: 报告模式（daily / current / incremental）
            rank_threshold: 排名阈值
            frequency_file: 关键词配置路径（运行时可能变化，故由调用方传入）

        Returns:
            (rss_stats, rss_new_stats, raw_rss_items, rss_new_urls) 元组
        """
        from trendradar.core.analyzer import count_rss_frequency

        # 从 display.regions.rss 统一控制 RSS 分析和展示
        rss_display_enabled = self.ctx.config.get("DISPLAY", {}).get("REGIONS", {}).get("RSS", True)

        # 加载关键词配置
        try:
            word_groups, filter_words, global_filters = self.ctx.load_frequency_words(frequency_file)
        except FileNotFoundError:
            word_groups, filter_words, global_filters = [], [], []

        timezone = self.ctx.timezone
        max_news_per_keyword = self.ctx.config.get("MAX_NEWS_PER_KEYWORD", 0)
        sort_by_position_first = self.ctx.config.get("SORT_BY_POSITION_FIRST", False)

        rss_stats = None
        rss_new_stats = None
        raw_rss_items = None  # 原始 RSS 条目列表（用于独立展示区）
        rss_new_urls = set()  # 原始新增 RSS URLs（未经关键词过滤）

        # 1. 首先获取原始条目（用于独立展示区，不受 display.regions.rss 影响）
        if report_mode == "incremental":
            new_items_dict = self.storage_manager.detect_new_rss_items(rss_data)
            if new_items_dict:
                raw_rss_items = self.convert_items_to_list(new_items_dict, rss_data.id_to_name)
        elif report_mode == "current":
            latest_data = self.storage_manager.get_latest_rss_data(rss_data.date)
            if latest_data:
                raw_rss_items = self.convert_items_to_list(latest_data.items, latest_data.id_to_name)
        else:  # daily
            all_data = self.storage_manager.get_rss_data(rss_data.date)
            if all_data:
                raw_rss_items = self.convert_items_to_list(all_data.items, all_data.id_to_name)

        # 如果 RSS 展示未启用，跳过关键词分析，只返回原始条目用于独立展示区
        if not rss_display_enabled:
            return None, None, raw_rss_items, rss_new_urls

        # 2. 获取新增条目（用于统计）
        new_items_dict = self.storage_manager.detect_new_rss_items(rss_data)
        new_items_list = None
        if new_items_dict:
            new_items_list = self.convert_items_to_list(new_items_dict, rss_data.id_to_name)
            if new_items_list:
                print(f"[RSS] 检测到 {len(new_items_list)} 条新增")
                # 收集原始新增 URLs（未经关键词过滤，用于 AI 模式 is_new 检测）
                rss_new_urls = {item["url"] for item in new_items_list if item.get("url")}

        # 3. 根据模式获取统计条目
        if report_mode == "incremental":
            # 增量模式：统计条目就是新增条目
            if not new_items_list:
                print("[RSS] 增量模式：没有新增 RSS 条目")
                return None, None, raw_rss_items, rss_new_urls

            rss_stats, total = count_rss_frequency(
                rss_items=new_items_list,
                word_groups=word_groups,
                filter_words=filter_words,
                global_filters=global_filters,
                new_items=new_items_list,  # 增量模式所有都是新增
                max_news_per_keyword=max_news_per_keyword,
                sort_by_position_first=sort_by_position_first,
                timezone=timezone,
                rank_threshold=rank_threshold,
                quiet=False,
            )
            if not rss_stats:
                print("[RSS] 增量模式：关键词匹配后没有内容")
                # 即使关键词匹配为空，也返回原始条目用于独立展示区
                return None, None, raw_rss_items, rss_new_urls

        elif report_mode == "current":
            # 当前榜单模式：统计=当前榜单所有条目
            # raw_rss_items 已在前面获取
            if not raw_rss_items:
                print("[RSS] 当前榜单模式：没有 RSS 数据")
                return None, None, None, rss_new_urls

            rss_stats, total = count_rss_frequency(
                rss_items=raw_rss_items,
                word_groups=word_groups,
                filter_words=filter_words,
                global_filters=global_filters,
                new_items=new_items_list,  # 标记新增
                max_news_per_keyword=max_news_per_keyword,
                sort_by_position_first=sort_by_position_first,
                timezone=timezone,
                rank_threshold=rank_threshold,
                quiet=False,
            )
            if not rss_stats:
                print("[RSS] 当前榜单模式：关键词匹配后没有内容")
                # 即使关键词匹配为空，也返回原始条目用于独立展示区
                return None, None, raw_rss_items, rss_new_urls

            # 生成新增统计
            if new_items_list:
                rss_new_stats, _ = count_rss_frequency(
                    rss_items=new_items_list,
                    word_groups=word_groups,
                    filter_words=filter_words,
                    global_filters=global_filters,
                    new_items=new_items_list,
                    max_news_per_keyword=max_news_per_keyword,
                    sort_by_position_first=sort_by_position_first,
                    timezone=timezone,
                    rank_threshold=rank_threshold,
                    quiet=True,
                )

        else:
            # daily 模式：统计=当天所有条目
            # raw_rss_items 已在前面获取
            if not raw_rss_items:
                print("[RSS] 当日汇总模式：没有 RSS 数据")
                return None, None, None, rss_new_urls

            rss_stats, total = count_rss_frequency(
                rss_items=raw_rss_items,
                word_groups=word_groups,
                filter_words=filter_words,
                global_filters=global_filters,
                new_items=new_items_list,  # 标记新增
                max_news_per_keyword=max_news_per_keyword,
                sort_by_position_first=sort_by_position_first,
                timezone=timezone,
                rank_threshold=rank_threshold,
                quiet=False,
            )
            if not rss_stats:
                print("[RSS] 当日汇总模式：关键词匹配后没有内容")
                # 即使关键词匹配为空，也返回原始条目用于独立展示区
                return None, None, raw_rss_items, rss_new_urls

            # 生成新增统计
            if new_items_list:
                rss_new_stats, _ = count_rss_frequency(
                    rss_items=new_items_list,
                    word_groups=word_groups,
                    filter_words=filter_words,
                    global_filters=global_filters,
                    new_items=new_items_list,
                    max_news_per_keyword=max_news_per_keyword,
                    sort_by_position_first=sort_by_position_first,
                    timezone=timezone,
                    rank_threshold=rank_threshold,
                    quiet=True,
                )

        self.total_count = total
        return rss_stats, rss_new_stats, raw_rss_items, rss_new_urls

    # ------------------------------------------------------------------
    # 条目转换 / 过滤 / 报告
    # ------------------------------------------------------------------
    def convert_items_to_list(self, items_dict: Dict, id_to_name: Dict) -> List[Dict]:
        """将 RSS 条目字典转换为列表格式，并应用新鲜度过滤（用于推送）"""
        from trendradar.utils.time import DEFAULT_TIMEZONE, calculate_days_old, is_within_days

        rss_items = []
        filtered_count = 0
        filtered_details = []  # 用于 DEBUG 模式下的详细日志

        # 获取新鲜度过滤配置
        rss_config = self.ctx.rss_config
        freshness_config = rss_config.get("FRESHNESS_FILTER", {})
        freshness_enabled = freshness_config.get("ENABLED", True)
        default_max_age_days = freshness_config.get("MAX_AGE_DAYS", 3)
        timezone = self.ctx.config.get("TIMEZONE", DEFAULT_TIMEZONE)
        debug_mode = self.ctx.config.get("DEBUG", False)

        # 构建 feed_id -> max_age_days 的映射
        feed_max_age_map = {}
        for feed_cfg in self.ctx.rss_feeds:
            feed_id = feed_cfg.get("id", "")
            max_age = feed_cfg.get("max_age_days")
            if max_age is not None:
                try:
                    feed_max_age_map[feed_id] = int(max_age)
                except (ValueError, TypeError):
                    pass

        for feed_id, items in items_dict.items():
            # 确定此 feed 的 max_age_days
            max_days = feed_max_age_map.get(feed_id)
            if max_days is None:
                max_days = default_max_age_days

            for item in items:
                # 应用新鲜度过滤（仅在启用时）
                if freshness_enabled and max_days > 0:
                    if item.published_at and not is_within_days(item.published_at, max_days, timezone):
                        filtered_count += 1
                        # 记录详细信息用于 DEBUG 模式
                        if debug_mode:
                            days_old = calculate_days_old(item.published_at, timezone)
                            feed_name = id_to_name.get(feed_id, feed_id)
                            filtered_details.append({
                                "title": item.title[:50] + "..." if len(item.title) > 50 else item.title,
                                "feed": feed_name,
                                "days_old": days_old,
                                "max_days": max_days,
                            })
                        continue  # 跳过超过指定天数的文章

                rss_items.append({
                    "title": item.title,
                    "feed_id": feed_id,
                    "feed_name": id_to_name.get(feed_id, feed_id),
                    "url": item.url,
                    "published_at": item.published_at,
                    "summary": item.summary,
                    "author": item.author,
                })

        # 输出过滤统计
        if filtered_count > 0:
            print(f"[RSS] 新鲜度过滤：跳过 {filtered_count} 篇超过指定天数的旧文章（仍保留在数据库中）")
            # DEBUG 模式下显示详细信息
            if debug_mode and filtered_details:
                print(f"[RSS] 被过滤的文章详情（共 {len(filtered_details)} 篇）：")
                for detail in filtered_details[:10]:  # 最多显示 10 条
                    days_str = f"{detail['days_old']:.1f}" if detail['days_old'] else "未知"
                    print(f"  - [{days_str}天前] [{detail['feed']}] {detail['title']} (限制: {detail['max_days']}天)")
                if len(filtered_details) > 10:
                    print(f"  ... 还有 {len(filtered_details) - 10} 篇被过滤")

        return rss_items

    def filter_by_keywords(self, rss_items: List[Dict], frequency_file: str) -> List[Dict]:
        """使用关键词文件过滤 RSS 条目"""
        try:
            word_groups, filter_words, global_filters = self.ctx.load_frequency_words(frequency_file)
            if word_groups or filter_words or global_filters:
                from trendradar.core.frequency import matches_word_groups
                filtered_items = []
                for item in rss_items:
                    title = item.get("title", "")
                    if matches_word_groups(title, word_groups, filter_words, global_filters):
                        filtered_items.append(item)

                original_count = len(rss_items)
                rss_items = filtered_items
                print(f"[RSS] 关键词过滤后剩余 {len(rss_items)}/{original_count} 条")

                if not rss_items:
                    print("[RSS] 关键词过滤后没有匹配内容")
                    return []
        except FileNotFoundError:
            # 关键词文件不存在时跳过过滤
            pass
        return rss_items

    def generate_html_report(self, rss_items: list, feeds_info: dict) -> str:
        """生成 RSS HTML 报告"""
        try:
            from pathlib import Path

            from trendradar.report.rss_html import render_rss_html_content

            html_content = render_rss_html_content(
                rss_items=rss_items,
                total_count=len(rss_items),
                feeds_info=feeds_info,
                get_time_func=self.ctx.get_time,
            )

            # 保存 HTML 文件（扁平化结构：output/html/日期/）
            date_folder = self.ctx.format_date()
            time_filename = self.ctx.format_time()
            output_dir = Path("output") / "html" / date_folder
            output_dir.mkdir(parents=True, exist_ok=True)

            file_path = output_dir / f"rss_{time_filename}.html"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            print(f"[RSS] HTML 报告已生成: {file_path}")
            return str(file_path)

        except Exception as e:
            print(f"[RSS] 生成 HTML 报告失败: {e}")
            return None
