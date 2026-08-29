# coding=utf-8
"""
TrendRadar 主程序

热点新闻聚合与分析工具
支持: python -m trendradar
"""

import argparse
import os
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from trendradar.context import AppContext
from trendradar import __version__
from trendradar.core import load_config
from trendradar.core.analyzer import convert_keyword_stats_to_platform_stats
from trendradar.crawler import DataFetcher
from trendradar.crawler.rss_processor import RSSProcessor
from trendradar.report.data_preparer import (
    prepare_current_title_info,
    prepare_standalone_data,
)
from trendradar.storage import convert_crawl_results_to_news_data
from trendradar.utils.time import DEFAULT_TIMEZONE, is_within_days, calculate_days_old
from trendradar.ai import AIAnalyzer, AIAnalysisResult
from trendradar.ai.analysis_service import AIAnalysisService
from trendradar.core.scheduler import ResolvedSchedule
from trendradar.commands import check_all_versions, run_doctor, run_test_notification, handle_status_commands
from trendradar.commands.version import _fetch_remote_version, _parse_version



def has_notification_configured(cfg: Dict) -> bool:
    """判断是否至少配置了一个可用的通知渠道。

    抽成纯函数是为了可测试：渠道判定一旦写错，表现是「明明配了 webhook 却静默不推送」，
    这种问题靠肉眼看日志很难发现。注意 Telegram / 邮箱 / Ntfy 是**多字段联合判定**，
    只填其中一项不算配置完成。
    """
    return any(
        [
            cfg["FEISHU_WEBHOOK_URL"],
            cfg["DINGTALK_WEBHOOK_URL"],
            cfg["WEWORK_WEBHOOK_URL"],
            (cfg["TELEGRAM_BOT_TOKEN"] and cfg["TELEGRAM_CHAT_ID"]),
            (cfg["EMAIL_FROM"] and cfg["EMAIL_PASSWORD"] and cfg["EMAIL_TO"]),
            (cfg["NTFY_SERVER_URL"] and cfg["NTFY_TOPIC"]),
            cfg["BARK_URL"],
            cfg["SLACK_WEBHOOK_URL"],
            cfg["GENERIC_WEBHOOK_URL"],
        ]
    )


def has_valid_content(
    report_mode: str, stats: List[Dict], new_titles: Optional[Dict] = None
) -> bool:
    """判断本次结果是否值得推送。

    - incremental / current：只看热榜有没有命中关键词的条目
    - daily（及兜底）：热榜命中 **或** 存在新增条目，二者满足其一

    daily 之所以额外看 new_titles：全天汇总要覆盖「当天出现过但此刻已跌出榜单」的新闻，
    若只按瞬时榜单判断，这类内容会被整条丢掉。
    """
    has_matched_news = any(stat["count"] > 0 for stat in stats or [])

    if report_mode in ("incremental", "current"):
        return has_matched_news

    has_new_news = bool(
        new_titles and any(len(titles) > 0 for titles in new_titles.values())
    )
    return has_matched_news or has_new_news


@dataclass
class PipelineOutcome:
    """分析流水线的一次执行结果。

    三种报告模式（current / daily / incremental）跑的是同一条流水线，差别只在于
    **数据来源**（全天累计的历史数据 vs 本次抓取的数据）以及是否要用历史数据回写
    id_to_name / new_titles / results。原先这些差别被写成三份近乎逐字的分支，改一
    处漏一处。收敛成具名结构后，调用点只需 `out.stats` 而不必记 9 元组的位置。
    """

    stats: List[Dict]
    html_file: Optional[str]
    ai_result: Optional["AIAnalysisResult"]
    rss_items: Optional[List[Dict]]
    rss_new_items: Optional[List[Dict]]
    standalone_data: Optional[Dict]
    # 以下四项会被回写到调用方，供通知与报告使用
    results: Dict = field(default_factory=dict)
    id_to_name: Dict = field(default_factory=dict)
    title_info: Dict = field(default_factory=dict)
    new_titles: Dict = field(default_factory=dict)


# === 主分析器 ===
class NewsAnalyzer:
    """新闻分析器"""

    # 模式策略定义
    MODE_STRATEGIES = {
        "incremental": {
            "mode_name": "增量模式",
            "description": "增量模式（只关注新增新闻，无新增时不推送）",
            "report_type": "增量分析",
            "should_send_notification": True,
        },
        "current": {
            "mode_name": "当前榜单模式",
            "description": "当前榜单模式（当前榜单匹配新闻 + 新增新闻区域 + 按时推送）",
            "report_type": "当前榜单",
            "should_send_notification": True,
        },
        "daily": {
            "mode_name": "全天汇总模式",
            "description": "全天汇总模式（所有匹配新闻 + 新增新闻区域 + 按时推送）",
            "report_type": "全天汇总",
            "should_send_notification": True,
        },
    }

    def __init__(self, config: Optional[Dict] = None):
        # 使用传入的配置或加载新配置
        if config is None:
            print("正在加载配置...")
            config = load_config()
        print(f"TrendRadar v{__version__} 配置加载完成")
        print(f"监控平台数量: {len(config['PLATFORMS'])}")
        print(f"时区: {config.get('TIMEZONE', DEFAULT_TIMEZONE)}")

        # 创建应用上下文
        self.ctx = AppContext(config)

        self.request_interval = self.ctx.config["REQUEST_INTERVAL"]
        self.report_mode = self.ctx.config["REPORT_MODE"]
        self.frequency_file = None
        self.filter_method = None  # None=使用全局配置 ctx.filter_method
        self.interests_file = None  # None=使用全局配置 ai_filter.interests_file
        self.rank_threshold = self.ctx.rank_threshold
        self.is_github_actions = os.environ.get("GITHUB_ACTIONS") == "true"
        self.is_docker_container = self._detect_docker_environment()
        self.update_info = None
        self.proxy_url = None
        self._setup_proxy()
        self.data_fetcher = DataFetcher(
            self.proxy_url,
            api_url=self.ctx.config.get("PLATFORMS_API_URL") or None,
        )

        # 报告头部元数据
        # 说明：RSS 相关的 source_total / source_failed / total_count 已移交
        # RSSProcessor 持有并在抓取时更新，这里只保留编排层自己的计数器。
        self._rss_matched_count = 0
        self._hotlist_total_count = 0

        # 初始化存储管理器（使用 AppContext）
        self._init_storage_manager()
        # AI 分析服务（只依赖 ctx，可在此处创建）
        self._ai = AIAnalysisService(self.ctx)
        # RSS 处理器依赖 storage_manager，因此必须放在其后创建
        self._rss = RSSProcessor(
            ctx=self.ctx,
            storage_manager=self.storage_manager,
            proxy_url=self.proxy_url or "",
        )
        # 注意：update_info 由 main() 函数设置，避免重复请求远程版本

    def _init_storage_manager(self) -> None:
        """初始化存储管理器（使用 AppContext）"""
        # 获取数据保留天数（支持环境变量覆盖）
        env_retention = os.environ.get("STORAGE_RETENTION_DAYS", "").strip()
        if env_retention:
            # 环境变量覆盖配置
            self.ctx.config["STORAGE"]["RETENTION_DAYS"] = int(env_retention)

        self.storage_manager = self.ctx.get_storage_manager()
        print(f"存储后端: {self.storage_manager.backend_name}")

        retention_days = self.ctx.config.get("STORAGE", {}).get("RETENTION_DAYS", 0)
        if retention_days > 0:
            print(f"数据保留天数: {retention_days} 天")

    def _detect_docker_environment(self) -> bool:
        """检测是否运行在 Docker 容器中"""
        try:
            if os.environ.get("DOCKER_CONTAINER") == "true":
                return True

            if os.path.exists("/.dockerenv"):
                return True

            return False
        except Exception:
            return False

    def _should_open_browser(self) -> bool:
        """判断是否应该打开浏览器"""
        return not self.is_github_actions and not self.is_docker_container

    def _setup_proxy(self) -> None:
        """设置代理配置"""
        if not self.is_github_actions and self.ctx.config["USE_PROXY"]:
            self.proxy_url = self.ctx.config["DEFAULT_PROXY"]
            print("本地环境，使用代理")
        elif not self.is_github_actions and not self.ctx.config["USE_PROXY"]:
            print("本地环境，未启用代理")
        else:
            print("GitHub Actions环境，不使用代理")

    def _set_update_info_from_config(self) -> None:
        """从已缓存的远程版本设置更新信息（不再重复请求）"""
        try:
            version_url = self.ctx.config.get("VERSION_CHECK_URL", "")
            if not version_url:
                return

            remote_version = _fetch_remote_version(version_url, self.proxy_url)
            if remote_version:
                need_update = _parse_version(__version__) < _parse_version(remote_version)
                if need_update:
                    self.update_info = {
                        "current_version": __version__,
                        "remote_version": remote_version,
                    }
        except Exception as e:
            print(f"版本检查出错: {e}")

    def _get_mode_strategy(self) -> Dict:
        """获取当前模式的策略配置"""
        return self.MODE_STRATEGIES.get(self.report_mode, self.MODE_STRATEGIES["daily"])

    def _has_notification_configured(self) -> bool:
        """检查是否配置了任何通知渠道"""
        return has_notification_configured(self.ctx.config)

    def _has_valid_content(
        self, stats: List[Dict], new_titles: Optional[Dict] = None
    ) -> bool:
        """检查是否有有效的新闻内容"""
        return has_valid_content(self.report_mode, stats, new_titles)

    def _prepare_ai_analysis_data(
        self,
        ai_mode: str,
        current_results: Optional[Dict] = None,
        current_id_to_name: Optional[Dict] = None,
    ) -> Tuple[List[Dict], Optional[Dict]]:
        """为 AI 分析准备指定模式的数据（实现已迁至 ai/analysis_service.py）"""
        return self._ai.prepare_analysis_data(
            self.frequency_file, ai_mode, current_results, current_id_to_name
        )

    def _run_ai_analysis(
        self,
        stats: List[Dict],
        rss_items: Optional[List[Dict]],
        mode: str,
        report_type: str,
        id_to_name: Optional[Dict],
        current_results: Optional[Dict] = None,
        schedule: ResolvedSchedule = None,
        standalone_data: Optional[Dict] = None,
    ) -> Optional[AIAnalysisResult]:
        """执行 AI 分析（实现已迁至 ai/analysis_service.py）"""
        return self._ai.run_analysis(
            frequency_file=self.frequency_file,
            stats=stats,
            rss_items=rss_items,
            mode=mode,
            report_type=report_type,
            id_to_name=id_to_name,
            current_results=current_results,
            schedule=schedule,
            standalone_data=standalone_data,
        )

    def _load_analysis_data(
        self,
        quiet: bool = False,
    ) -> Optional[Tuple[Dict, Dict, Dict, Dict, List, List]]:
        """统一的数据加载和预处理，使用当前监控平台列表过滤历史数据（实现已迁至 ai/analysis_service.py）"""
        return self._ai.load_analysis_data(self.frequency_file, quiet=quiet)

    def _prepare_current_title_info(self, results: Dict, time_info: str) -> Dict:
        """从当前抓取结果构建标题信息（实现已迁至 report/data_preparer.py）"""
        return prepare_current_title_info(results, time_info)

    def _prepare_standalone_data(
        self,
        results: Dict,
        id_to_name: Dict,
        title_info: Optional[Dict] = None,
        rss_items: Optional[List[Dict]] = None,
    ) -> Optional[Dict]:
        """
        从原始数据中提取独立展示区数据

        纯数据准备方法，不检查 display.regions.standalone 开关。
        各消费者自行决定是否使用：
        - AI 分析：由 ai.include_standalone 控制（在 _run_ai_analysis 层门控）
        - HTML 报告 / 邮件：由 display.regions.standalone 控制（在 HTML 生成前过滤）
        - Webhook 推送：由 display.regions.standalone 控制（在 dispatcher 层门控）

        Args:
            results: 原始爬取结果 {platform_id: {title: title_data}}
            id_to_name: 平台 ID 到名称的映射
            title_info: 标题元信息（含排名历史、时间等）
            rss_items: RSS 条目列表

        Returns:
            独立展示数据字典，如果未配置数据源返回 None
        """
        # 实现已迁至 report/data_preparer.py（纯数据变换，不依赖实例状态）
        return prepare_standalone_data(
            self.ctx, results, id_to_name, title_info, rss_items
        )

    def _run_analysis_pipeline(
        self,
        data_source: Dict,
        mode: str,
        title_info: Dict,
        new_titles: Dict,
        word_groups: List[Dict],
        filter_words: List[str],
        id_to_name: Dict,
        failed_ids: Optional[List] = None,
        global_filters: Optional[List[str]] = None,
        quiet: bool = False,
        rss_items: Optional[List[Dict]] = None,
        rss_new_items: Optional[List[Dict]] = None,
        standalone_data: Optional[Dict] = None,
        schedule: ResolvedSchedule = None,
        rss_new_urls: Optional[set] = None,
    ) -> Tuple[List[Dict], Optional[str], Optional[AIAnalysisResult], Optional[List[Dict]], Optional[Dict], Optional[List[Dict]]]:
        """统一的分析流水线：数据处理 → 统计计算（关键词/AI筛选）→ AI分析 → HTML生成"""

        # 根据筛选策略选择数据处理方式
        if self.filter_method == "ai":
            # === AI 筛选策略 ===
            print("[筛选] 使用 AI 智能筛选策略")
            ai_filter_result = self.ctx.run_ai_filter(interests_file=self.interests_file)

            if ai_filter_result and ai_filter_result.success:
                print(f"[筛选] AI 筛选完成: {ai_filter_result.total_matched} 条匹配, {len(ai_filter_result.tags)} 个标签")
                # 转换为与关键词匹配相同的数据结构
                stats, ai_rss_stats, ai_rss_new_stats = self.ctx.convert_ai_filter_to_report_data(
                    ai_filter_result, mode=mode,
                    new_titles=new_titles, rss_new_urls=rss_new_urls,
                )
                total_titles = sum(len(titles) for titles in data_source.values())

                # AI 筛选成功：无条件用 AI 结果替换 RSS 主区与新增区（与热榜 stats 一致，
                # 不因 AI 命中为空而回退到关键词结果）
                rss_items = ai_rss_stats
                rss_new_items = ai_rss_new_stats
            else:
                # AI 筛选失败，回退到关键词匹配
                error_msg = ai_filter_result.error if ai_filter_result else "未知错误"
                print(f"[筛选] AI 筛选失败: {error_msg}，回退到关键词匹配")
                stats, total_titles = self.ctx.count_frequency(
                    data_source, word_groups, filter_words,
                    id_to_name, title_info, new_titles,
                    mode=mode, global_filters=global_filters, quiet=quiet,
                )
        else:
            # === 关键词匹配策略（默认）===
            stats, total_titles = self.ctx.count_frequency(
                data_source, word_groups, filter_words,
                id_to_name, title_info, new_titles,
                mode=mode, global_filters=global_filters, quiet=quiet,
            )

        self._hotlist_total_count = total_titles

        # 如果是 platform 模式，转换数据结构
        if self.ctx.display_mode == "platform" and stats:
            stats = convert_keyword_stats_to_platform_stats(
                stats,
                self.ctx.weight_config,
                self.ctx.rank_threshold,
            )

        # AI 分析（如果启用，用于 HTML 报告）
        ai_result = None
        ai_config = self.ctx.config.get("AI_ANALYSIS", {})
        if ai_config.get("ENABLED", False) and stats:
            # 获取模式策略来确定报告类型
            mode_strategy = self._get_mode_strategy()
            report_type = mode_strategy["report_type"]
            ai_result = self._run_ai_analysis(
                stats, rss_items, mode, report_type, id_to_name,
                current_results=data_source, schedule=schedule,
                standalone_data=standalone_data
            )

        # 翻译 RSS 和独立展示区内容（如果启用）— 在 HTML 生成前执行，确保网页版也能展示翻译内容
        # standalone_data 在此翻译一次后贯穿到推送阶段复用，避免重复翻译并保证网页与推送译文一致
        # 热榜翻译在推送时由 dispatch_all 处理 report_data
        trans_config = self.ctx.config.get("AI_TRANSLATION", {})
        translate_report_func = None  # 供 HTML 翻译热榜 report_data（在过滤之后翻译）
        if trans_config.get("ENABLED", False):
            dispatcher = self.ctx.create_notification_dispatcher()
            display_regions = self.ctx.config.get("DISPLAY", {}).get("REGIONS", {})
            _, rss_items, rss_new_items, standalone_data = \
                dispatcher.translate_content(
                    report_data={"stats": [], "new_titles": []},
                    rss_items=rss_items,
                    rss_new_items=rss_new_items,
                    standalone_data=standalone_data,
                    display_regions=display_regions,
                )

            # 热榜 report_data 翻译回调：HTML 在 prepare_report_data 过滤之后调用，
            # 仅翻译热榜（skip_rss/skip_standalone 跳过已在上游翻译的 RSS/独立区），网页版热榜展示译文
            def translate_report_func(rd, _d=dispatcher, _r=display_regions):
                translated_rd, _, _, _ = _d.translate_content(
                    report_data=rd, display_regions=_r,
                    skip_rss=True, skip_standalone=True,
                )
                return translated_rd

        # 计算 RSS 匹配条数（供 HTML 和推送共用）
        self._rss_matched_count = sum(stat.get("count", 0) for stat in rss_items) if rss_items else 0

        # HTML生成（如果启用）— 使用翻译后的数据
        html_file = None
        if self.ctx.config["STORAGE"]["FORMATS"]["HTML"]:
            display_regions = self.ctx.config.get("DISPLAY", {}).get("REGIONS", {})
            html_standalone = standalone_data if display_regions.get("STANDALONE", False) else None
            html_ai = ai_result if display_regions.get("AI_ANALYSIS", True) else None
            html_file = self.ctx.generate_html(
                stats,
                total_titles,
                failed_ids=failed_ids,
                new_titles=new_titles,
                id_to_name=id_to_name,
                mode=mode,
                update_info=self.update_info if self.ctx.config["SHOW_VERSION_UPDATE"] else None,
                rss_items=rss_items,
                rss_new_items=rss_new_items,
                ai_analysis=html_ai,
                standalone_data=html_standalone,
                frequency_file=self.frequency_file,
                report_metadata={
                    "hotlist_total": total_titles,
                    "platform_total": len(self.ctx.platform_ids),
                    "rss_matched_count": self._rss_matched_count,
                    "rss_total_count": self._rss.total_count,
                    "rss_source_total": self._rss.source_total,
                    "rss_source_failed": self._rss.source_failed,
                },
                translate_report_func=translate_report_func,
            )

        return stats, html_file, ai_result, rss_items, standalone_data, rss_new_items

    def _send_notification_if_needed(
        self,
        stats: List[Dict],
        report_type: str,
        mode: str,
        failed_ids: Optional[List] = None,
        new_titles: Optional[Dict] = None,
        id_to_name: Optional[Dict] = None,
        html_file_path: Optional[str] = None,
        rss_items: Optional[List[Dict]] = None,
        rss_new_items: Optional[List[Dict]] = None,
        standalone_data: Optional[Dict] = None,
        ai_result: Optional[AIAnalysisResult] = None,
        current_results: Optional[Dict] = None,
        schedule: ResolvedSchedule = None,
    ) -> bool:
        """统一的通知发送逻辑，包含所有判断条件，支持热榜+RSS合并推送+AI分析+独立展示区"""
        has_notification = self._has_notification_configured()
        cfg = self.ctx.config

        # 检查是否有有效内容（热榜或RSS）
        has_news_content = self._has_valid_content(stats, new_titles)
        has_rss_content = bool(rss_items and len(rss_items) > 0)
        has_any_content = has_news_content or has_rss_content

        # 计算热榜匹配条数
        news_count = sum(len(stat.get("titles", [])) for stat in stats) if stats else 0
        rss_count = sum(stat.get("count", 0) for stat in rss_items) if rss_items else 0

        if (
            cfg["ENABLE_NOTIFICATION"]
            and has_notification
            and has_any_content
        ):
            # 输出推送内容统计
            content_parts = []
            if news_count > 0:
                content_parts.append(f"热榜 {news_count} 条")
            if rss_count > 0:
                content_parts.append(f"RSS {rss_count} 条")
            total_count = news_count + rss_count
            print(f"[推送] 准备发送：{' + '.join(content_parts)}，合计 {total_count} 条")

            # 调度系统决策
            if not schedule.push:
                print("[推送] 调度器: 当前时间段不执行推送")
                return False

            if schedule.once_push and schedule.period_key:
                scheduler = self.ctx.create_scheduler()
                date_str = self.ctx.format_date()
                if scheduler.already_executed(schedule.period_key, "push", date_str):
                    print(f"[推送] 调度器: 时间段 {schedule.period_name or schedule.period_key} 今天已推送过，跳过")
                    return False
                else:
                    print(f"[推送] 调度器: 时间段 {schedule.period_name or schedule.period_key} 今天首次推送")

            # AI 分析：优先使用传入的结果，避免重复分析
            if ai_result is None:
                ai_config = cfg.get("AI_ANALYSIS", {})
                if ai_config.get("ENABLED", False):
                    ai_result = self._run_ai_analysis(
                        stats, rss_items, mode, report_type, id_to_name,
                        current_results=current_results, schedule=schedule,
                        standalone_data=standalone_data,
                    )

            # 准备报告数据
            report_data = self.ctx.prepare_report(stats, failed_ids, new_titles, id_to_name, mode, frequency_file=self.frequency_file)

            # 注入元数据（用于推送头部展示）
            report_data["hotlist_total"] = self._hotlist_total_count
            report_data["platform_total"] = len(self.ctx.platform_ids)
            report_data["rss_matched_count"] = self._rss_matched_count
            report_data["rss_total_count"] = self._rss.total_count
            report_data["rss_source_total"] = self._rss.source_total
            report_data["rss_source_failed"] = self._rss.source_failed

            # 是否发送版本更新信息
            update_info_to_send = self.update_info if cfg["SHOW_VERSION_UPDATE"] else None

            # 使用 NotificationDispatcher 发送到所有渠道
            # RSS/独立展示区数据已在分析流水线中翻译过，跳过重复翻译（仅翻译热榜 report_data）
            dispatcher = self.ctx.create_notification_dispatcher()
            results = dispatcher.dispatch_all(
                report_data=report_data,
                report_type=report_type,
                update_info=update_info_to_send,
                proxy_url=self.proxy_url,
                mode=mode,
                html_file_path=html_file_path,
                rss_items=rss_items,
                rss_new_items=rss_new_items,
                ai_analysis=ai_result,
                standalone_data=standalone_data,
                skip_translation=True,
            )

            if not results:
                print("未配置任何通知渠道，跳过通知发送")
                return False

            # 记录推送成功
            if any(results.values()):
                if schedule.once_push and schedule.period_key:
                    scheduler = self.ctx.create_scheduler()
                    date_str = self.ctx.format_date()
                    scheduler.record_execution(schedule.period_key, "push", date_str)

            return True

        elif cfg["ENABLE_NOTIFICATION"] and not has_notification:
            print("⚠️ 警告：通知功能已启用但未配置任何通知渠道，将跳过通知发送")
        elif not cfg["ENABLE_NOTIFICATION"]:
            print(f"跳过{report_type}通知：通知功能已禁用")
        elif (
            cfg["ENABLE_NOTIFICATION"]
            and has_notification
            and not has_any_content
        ):
            mode_strategy = self._get_mode_strategy()
            if self.report_mode == "incremental":
                if not has_rss_content:
                    print("跳过通知：增量模式下未检测到匹配的新闻和RSS")
                else:
                    print("跳过通知：增量模式下新闻未匹配到关键词")
            else:
                print(
                    f"跳过通知：{mode_strategy['mode_name']}下未检测到匹配的新闻"
                )

        return False

    def _initialize_and_check_config(self) -> bool:
        """通用初始化和配置检查。返回 True 表示可以继续执行。"""
        now = self.ctx.get_time()
        print(f"当前北京时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")

        if not self.ctx.config["ENABLE_CRAWLER"]:
            print("爬虫功能已禁用（ENABLE_CRAWLER=False），程序退出")
            return False

        has_notification = self._has_notification_configured()
        if not self.ctx.config["ENABLE_NOTIFICATION"]:
            print("通知功能已禁用（ENABLE_NOTIFICATION=False），将只进行数据抓取")
        elif not has_notification:
            print("未配置任何通知渠道，将只进行数据抓取，不发送通知")
        else:
            print("通知功能已启用，将发送通知")

        mode_strategy = self._get_mode_strategy()
        print(f"报告模式: {self.report_mode}")
        print(f"运行模式: {mode_strategy['description']}")
        return True

    def _crawl_data(self) -> Tuple[Dict, Dict, List]:
        """执行数据爬取"""
        ids = []
        domain_rules = {}
        for platform in self.ctx.platforms:
            if "name" in platform:
                ids.append((platform["id"], platform["name"]))
            else:
                ids.append(platform["id"])
            expected_domain = platform.get("expected_domain", "")
            if expected_domain:
                domain_rules[platform["id"]] = expected_domain

        print(
            f"配置的监控平台: {[p.get('name', p['id']) for p in self.ctx.platforms]}"
        )
        print(f"开始爬取数据，请求间隔 {self.request_interval} 毫秒")
        Path("output").mkdir(parents=True, exist_ok=True)

        results, id_to_name, failed_ids = self.data_fetcher.crawl_websites(
            ids, self.request_interval, domain_rules=domain_rules
        )

        # 转换为 NewsData 格式并保存到存储后端
        crawl_time = self.ctx.format_time()
        crawl_date = self.ctx.format_date()
        news_data = convert_crawl_results_to_news_data(
            results, id_to_name, failed_ids, crawl_time, crawl_date
        )

        # 保存到存储后端（SQLite）
        if self.storage_manager.save_news_data(news_data):
            print(f"数据已保存到存储后端: {self.storage_manager.backend_name}")

        # 保存 TXT 快照（如果启用）
        txt_file = self.storage_manager.save_txt_snapshot(news_data)
        if txt_file:
            print(f"TXT 快照已保存: {txt_file}")

        return results, id_to_name, failed_ids

    def _crawl_rss_data(self) -> Tuple[Optional[List[Dict]], Optional[List[Dict]], Optional[List[Dict]], set]:
        """执行 RSS 数据抓取（实现已迁至 crawler/rss_processor.py）"""
        return self._rss.crawl(
            report_mode=self.report_mode,
            rank_threshold=self.rank_threshold,
            frequency_file=self.frequency_file,
        )

    def _process_rss_data_by_mode(self, rss_data) -> Tuple[Optional[List[Dict]], Optional[List[Dict]], Optional[List[Dict]], set]:
        """按报告模式处理 RSS 数据，返回与热榜相同格式的统计结构（实现已迁至 crawler/rss_processor.py）"""
        return self._rss.process_by_mode(
            rss_data,
            report_mode=self.report_mode,
            rank_threshold=self.rank_threshold,
            frequency_file=self.frequency_file,
        )

    def _convert_rss_items_to_list(self, items_dict: Dict, id_to_name: Dict) -> List[Dict]:
        """将 RSS 条目字典转换为列表格式，并应用新鲜度过滤（用于推送）（实现已迁至 crawler/rss_processor.py）"""
        return self._rss.convert_items_to_list(items_dict, id_to_name)

    def _filter_rss_by_keywords(self, rss_items: List[Dict]) -> List[Dict]:
        """使用关键词文件过滤 RSS 条目（实现已迁至 crawler/rss_processor.py）"""
        return self._rss.filter_by_keywords(rss_items, self.frequency_file)

    def _generate_rss_html_report(self, rss_items: list, feeds_info: dict) -> str:
        """生成 RSS HTML 报告（实现已迁至 crawler/rss_processor.py）"""
        return self._rss.generate_html_report(rss_items, feeds_info)

    def _run_pipeline_on_history(
        self,
        analysis_data: Tuple,
        id_to_name: Dict,
        raw_rss_items: Optional[List[Dict]],
        word_groups: List[Dict],
        filter_words: List[str],
        global_filters: Optional[List[str]],
        failed_ids: List,
        rss_items: Optional[List[Dict]],
        rss_new_items: Optional[List[Dict]],
        schedule: ResolvedSchedule,
        rss_new_urls: Optional[Set],
    ) -> PipelineOutcome:
        """以全天累计的历史数据为输入跑分析流水线（current / daily 共有路径）。

        ``analysis_data`` 是 _load_analysis_data() 返回的 7 元组，前四项依次为
        all_results / id_to_name / title_info / new_titles，其余三项是历史占位数据，
        本路径不使用。
        """
        (
            all_results,
            historical_id_to_name,
            historical_title_info,
            historical_new_titles,
            _,
            _,
            _,
        ) = analysis_data

        # 使用历史数据准备独立展示区数据（包含完整的 title_info）
        standalone_data = self._prepare_standalone_data(
            all_results, historical_id_to_name, historical_title_info, raw_rss_items
        )

        (
            stats,
            html_file,
            ai_result,
            rss_items,
            standalone_data,
            rss_new_items,
        ) = self._run_analysis_pipeline(
            all_results,
            self.report_mode,
            historical_title_info,
            historical_new_titles,
            word_groups,
            filter_words,
            historical_id_to_name,
            failed_ids=failed_ids,
            global_filters=global_filters,
            rss_items=rss_items,
            rss_new_items=rss_new_items,
            standalone_data=standalone_data,
            schedule=schedule,
            rss_new_urls=rss_new_urls,
        )

        return PipelineOutcome(
            stats=stats,
            html_file=html_file,
            ai_result=ai_result,
            rss_items=rss_items,
            rss_new_items=rss_new_items,
            standalone_data=standalone_data,
            results=all_results,
            # 历史数据的映射可能缺少本次新出现的平台，用本次抓取的补上
            id_to_name={**historical_id_to_name, **id_to_name},
            title_info=historical_title_info,
            new_titles=historical_new_titles,
        )

    def _run_pipeline_on_current(
        self,
        results: Dict,
        id_to_name: Dict,
        new_titles: Dict,
        time_info: Dict,
        raw_rss_items: Optional[List[Dict]],
        word_groups: List[Dict],
        filter_words: List[str],
        global_filters: Optional[List[str]],
        failed_ids: List,
        rss_items: Optional[List[Dict]],
        rss_new_items: Optional[List[Dict]],
        schedule: ResolvedSchedule,
        rss_new_urls: Optional[Set],
    ) -> PipelineOutcome:
        """以本次抓取的数据为输入跑分析流水线。

        用于 incremental 模式，以及 daily 模式当天还没有任何历史数据时的降级路径。
        """
        title_info = self._prepare_current_title_info(results, time_info)
        standalone_data = self._prepare_standalone_data(
            results, id_to_name, title_info, raw_rss_items
        )

        (
            stats,
            html_file,
            ai_result,
            rss_items,
            standalone_data,
            rss_new_items,
        ) = self._run_analysis_pipeline(
            results,
            self.report_mode,
            title_info,
            new_titles,
            word_groups,
            filter_words,
            id_to_name,
            failed_ids=failed_ids,
            global_filters=global_filters,
            rss_items=rss_items,
            rss_new_items=rss_new_items,
            standalone_data=standalone_data,
            schedule=schedule,
            rss_new_urls=rss_new_urls,
        )

        return PipelineOutcome(
            stats=stats,
            html_file=html_file,
            ai_result=ai_result,
            rss_items=rss_items,
            rss_new_items=rss_new_items,
            standalone_data=standalone_data,
            results=results,
            id_to_name=id_to_name,
            title_info=title_info,
            new_titles=new_titles,
        )

    def _execute_mode_strategy(
        self, mode_strategy: Dict, results: Dict, id_to_name: Dict, failed_ids: List,
        rss_items: Optional[List[Dict]] = None,
        rss_new_items: Optional[List[Dict]] = None,
        raw_rss_items: Optional[List[Dict]] = None,
        rss_new_urls: Optional[set] = None,
    ) -> Optional[str]:
        """执行模式特定逻辑，支持热榜+RSS合并推送

        简化后的逻辑：
        - 每次运行都生成 HTML 报告（时间戳快照 + latest/{mode}.html + index.html）
        - 根据模式发送通知
        """
        # 调度系统
        scheduler = self.ctx.create_scheduler()
        schedule = scheduler.resolve()

        # 使用 schedule 决定的 report_mode 覆盖全局配置
        effective_mode = schedule.report_mode
        if effective_mode != self.report_mode:
            print(f"[调度] 报告模式覆盖: {self.report_mode} -> {effective_mode}")
        self.report_mode = effective_mode

        # 重新获取 mode_strategy，确保 report_type 与覆盖后的 report_mode 一致
        mode_strategy = self._get_mode_strategy()

        # 使用 schedule 决定的 frequency_file 覆盖默认值
        self.frequency_file = schedule.frequency_file

        # 使用 schedule 决定的筛选策略覆盖默认值
        self.filter_method = schedule.filter_method or self.ctx.filter_method

        # 使用 schedule 决定的 AI 筛选兴趣文件覆盖默认值
        self.interests_file = schedule.interests_file

        # 如果调度器说不采集，则直接跳过
        if not schedule.collect:
            print("[调度] 当前时间段不执行数据采集，跳过分析流水线")
            return None
        # 获取当前监控平台ID列表
        current_platform_ids = self.ctx.platform_ids

        new_titles = self.ctx.detect_new_titles(current_platform_ids)
        time_info = self.ctx.format_time()
        word_groups, filter_words, global_filters = self.ctx.load_frequency_words(self.frequency_file)

        # current / daily 基于全天累计数据，incremental 只用本次抓取的数据。
        # 两种来源各对应一个 helper，避免原先三份近乎逐字的分支各改各的。
        pipeline_kwargs = dict(
            word_groups=word_groups,
            filter_words=filter_words,
            global_filters=global_filters,
            failed_ids=failed_ids,
            rss_items=rss_items,
            rss_new_items=rss_new_items,
            schedule=schedule,
            rss_new_urls=rss_new_urls,
        )

        if self.report_mode == "current":
            analysis_data = self._load_analysis_data()
            if not analysis_data:
                print("❌ 严重错误：无法读取刚保存的数据文件")
                raise RuntimeError("数据一致性检查失败：保存后立即读取失败")
            print(
                "current模式：使用过滤后的历史数据，包含平台："
                f"{list(analysis_data[0].keys())}"
            )
            out = self._run_pipeline_on_history(
                analysis_data, id_to_name, raw_rss_items, **pipeline_kwargs
            )
        elif self.report_mode == "daily":
            analysis_data = self._load_analysis_data()
            if analysis_data:
                out = self._run_pipeline_on_history(
                    analysis_data, id_to_name, raw_rss_items, **pipeline_kwargs
                )
            else:
                # 当天还没有历史数据（例如首跑）时降级为只用本次抓取的数据。
                # 注意与 current 的区别：current 读不到刚写入的数据属于一致性故障，
                # 必须报错而非静默降级，否则会推送出一份与榜单不符的报告。
                out = self._run_pipeline_on_current(
                    results, id_to_name, new_titles, time_info,
                    raw_rss_items, **pipeline_kwargs,
                )
        else:
            # incremental 模式：只使用当前抓取的数据
            out = self._run_pipeline_on_current(
                results, id_to_name, new_titles, time_info,
                raw_rss_items, **pipeline_kwargs,
            )

        if out.html_file:
            print(f"HTML报告已生成: {out.html_file}")
            print(f"最新报告已更新: output/html/latest/{self.report_mode}.html")

        # 发送通知
        if mode_strategy["should_send_notification"]:
            # standalone_data 已在分析流水线中翻译，直接复用（不再重新 prepare 原文，
            # 避免覆盖译文、避免重复翻译，并保证网页报告与推送译文一致）
            self._send_notification_if_needed(
                out.stats,
                mode_strategy["report_type"],
                self.report_mode,
                failed_ids=failed_ids,
                new_titles=out.new_titles,
                id_to_name=out.id_to_name,
                html_file_path=out.html_file,
                rss_items=out.rss_items,
                rss_new_items=out.rss_new_items,
                standalone_data=out.standalone_data,
                ai_result=out.ai_result,
                current_results=out.results,
                schedule=schedule,
            )

        # 打开浏览器（仅在非容器环境）
        if self._should_open_browser() and out.html_file:
            file_url = "file://" + str(Path(out.html_file).resolve())
            print(f"正在打开HTML报告: {file_url}")
            webbrowser.open(file_url)
        elif self.is_docker_container and out.html_file:
            print(f"HTML报告已生成（Docker环境）: {out.html_file}")

        return out.html_file

    def run(self) -> None:
        """执行分析流程"""
        try:
            if not self._initialize_and_check_config():
                return

            mode_strategy = self._get_mode_strategy()

            # 抓取热榜数据
            results, id_to_name, failed_ids = self._crawl_data()

            # 抓取 RSS 数据（如果启用），返回统计条目、新增条目和原始条目
            rss_items, rss_new_items, raw_rss_items, rss_new_urls = self._crawl_rss_data()

            # 执行模式策略，传递 RSS 数据用于合并推送
            self._execute_mode_strategy(
                mode_strategy, results, id_to_name, failed_ids,
                rss_items=rss_items, rss_new_items=rss_new_items,
                raw_rss_items=raw_rss_items, rss_new_urls=rss_new_urls
            )

        except Exception as e:
            print(f"分析流程执行出错: {e}")
            if self.ctx.config.get("DEBUG", False):
                raise
        finally:
            # 清理资源（包括过期数据清理和数据库连接关闭）
            self.ctx.cleanup()


def main():
    """主程序入口"""
    parser = argparse.ArgumentParser(
        description="TrendRadar - 热点新闻聚合与分析工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
调度状态命令:
  --show-schedule        显示当前调度状态（时间段、行为开关）
诊断命令:
  --doctor               运行环境与配置体检
  --test-notification    发送测试通知到已配置渠道

示例:
  python -m trendradar                    # 正常运行
  python -m trendradar --show-schedule    # 查看当前调度状态
  python -m trendradar --doctor           # 运行一键体检
  python -m trendradar --test-notification # 测试通知渠道连通性
"""
    )
    parser.add_argument("--show-schedule", action="store_true", help="显示当前调度状态")
    parser.add_argument("--doctor", action="store_true", help="运行环境与配置体检")
    parser.add_argument("--test-notification", action="store_true", help="发送测试通知到已配置渠道")

    args = parser.parse_args()

    debug_mode = False
    try:
        if args.doctor:
            ok = run_doctor()
            if not ok:
                raise SystemExit(1)
            return

        config = load_config()

        if args.show_schedule:
            handle_status_commands(config)
            return

        if args.test_notification:
            ok = run_test_notification(config)
            if not ok:
                raise SystemExit(1)
            return

        version_url = config.get("VERSION_CHECK_URL", "")
        configs_version_url = config.get("CONFIGS_VERSION_CHECK_URL", "")

        need_update = False
        remote_version = None
        if version_url:
            need_update, remote_version = check_all_versions(version_url, configs_version_url)

        analyzer = NewsAnalyzer(config=config)

        if analyzer.is_github_actions and need_update and remote_version:
            analyzer.update_info = {
                "current_version": __version__,
                "remote_version": remote_version,
            }

        debug_mode = analyzer.ctx.config.get("DEBUG", False)
        analyzer.run()
    except FileNotFoundError as e:
        print(f"❌ 配置文件错误: {e}")
        print("\n请确保以下文件存在:")
        print("  • config/config.yaml")
        print("  • config/frequency_words.txt")
        print("\n参考项目文档进行正确配置")
    except Exception as e:
        print(f"❌ 程序运行错误: {e}")
        if debug_mode:
            raise


if __name__ == "__main__":
    main()
