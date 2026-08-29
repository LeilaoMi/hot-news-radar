# coding=utf-8
"""AI 分析服务。

从 NewsAnalyzer 拆出的一类职责：加载分析数据、按 AI 模式准备统计、
调用 AIAnalyzer 执行分析并处理调度幂等。

设计要点
--------
1. frequency_file 作为**方法参数**传入。它会在运行时被调度器覆盖
   （见 __main__ 的 _initialize_and_check_config），构造时固定会拿到过期值。

2. 只依赖 AppContext 与已模块化的数据准备函数，不反向依赖 NewsAnalyzer，
   因此不存在循环导入。

3. AIAnalyzer 保持延迟导入：它最终会引入 litellm，顶层导入会让 CLI 启动
   从 0.2s 退化到 7.6s（历史上踩过这个坑）。
"""
from typing import Dict, List, Optional, Tuple

from trendradar.report.data_preparer import prepare_current_title_info


class AIAnalysisService:
    """AI 分析的编排与执行。"""

    def __init__(self, ctx):
        """
        Args:
            ctx: AppContext（需要 platform_ids / read_today_titles / detect_new_titles /
                 load_frequency_words / count_frequency / create_scheduler /
                 format_date / format_time / get_time / config / display_mode /
                 weight_config / rank_threshold）
        """
        self.ctx = ctx

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------
    def load_analysis_data(
        self,
        frequency_file: str,
        quiet: bool = False,
    ) -> Optional[Tuple[Dict, Dict, Dict, Dict, List, List]]:
        """统一的数据加载和预处理，使用当前监控平台列表过滤历史数据"""
        try:
            # 获取当前配置的监控平台ID列表
            current_platform_ids = self.ctx.platform_ids
            if not quiet:
                print(f"当前监控平台: {current_platform_ids}")

            all_results, id_to_name, title_info = self.ctx.read_today_titles(
                current_platform_ids, quiet=quiet
            )

            if not all_results:
                print("没有找到当天的数据")
                return None

            total_titles = sum(len(titles) for titles in all_results.values())
            if not quiet:
                print(f"读取到 {total_titles} 个标题（已按当前监控平台过滤）")

            new_titles = self.ctx.detect_new_titles(current_platform_ids, quiet=quiet)
            word_groups, filter_words, global_filters = self.ctx.load_frequency_words(frequency_file)

            return (
                all_results,
                id_to_name,
                title_info,
                new_titles,
                word_groups,
                filter_words,
                global_filters,
            )
        except Exception as e:
            print(f"数据加载失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 按模式准备数据
    # ------------------------------------------------------------------
    def prepare_analysis_data(
        self,
        frequency_file: str,
        ai_mode: str,
        current_results: Optional[Dict] = None,
        current_id_to_name: Optional[Dict] = None,
    ) -> Tuple[List[Dict], Optional[Dict]]:
        """
        为 AI 分析准备指定模式的数据

        Args:
            frequency_file: 关键词配置路径（运行时可能变化）
            ai_mode: AI 分析模式 (daily/current/incremental)
            current_results: 当前抓取的结果（用于 incremental 模式）
            current_id_to_name: 当前的平台映射（用于 incremental 模式）

        Returns:
            Tuple[stats, id_to_name]: 统计数据和平台映射
        """
        try:
            from trendradar.core.analyzer import convert_keyword_stats_to_platform_stats

            word_groups, filter_words, global_filters = self.ctx.load_frequency_words(frequency_file)

            if ai_mode == "incremental":
                # incremental 模式：使用当前抓取的数据
                if not current_results or not current_id_to_name:
                    print("[AI] incremental 模式需要当前抓取数据，但未提供")
                    return [], None

                # 准备当前时间信息
                time_info = self.ctx.format_time()
                title_info = prepare_current_title_info(current_results, time_info)

                # 检测新增标题
                new_titles = self.ctx.detect_new_titles(list(current_results.keys()))

                # 统计计算
                stats, _ = self.ctx.count_frequency(
                    current_results,
                    word_groups,
                    filter_words,
                    current_id_to_name,
                    title_info,
                    new_titles,
                    mode="incremental",
                    global_filters=global_filters,
                    quiet=True,
                )

                # 如果是 platform 模式，转换数据结构
                if self.ctx.display_mode == "platform" and stats:
                    stats = convert_keyword_stats_to_platform_stats(
                        stats,
                        self.ctx.weight_config,
                        self.ctx.rank_threshold,
                    )

                return stats, current_id_to_name

            elif ai_mode in ["daily", "current"]:
                # 加载历史数据
                analysis_data = self.load_analysis_data(frequency_file, quiet=True)
                if not analysis_data:
                    print(f"[AI] 无法加载历史数据用于 {ai_mode} 模式分析")
                    return [], None

                (
                    all_results,
                    id_to_name,
                    title_info,
                    new_titles,
                    _,
                    _,
                    _,
                ) = analysis_data

                # 统计计算
                stats, _ = self.ctx.count_frequency(
                    all_results,
                    word_groups,
                    filter_words,
                    id_to_name,
                    title_info,
                    new_titles,
                    mode=ai_mode,
                    global_filters=global_filters,
                    quiet=True,
                )

                # 如果是 platform 模式，转换数据结构
                if self.ctx.display_mode == "platform" and stats:
                    stats = convert_keyword_stats_to_platform_stats(
                        stats,
                        self.ctx.weight_config,
                        self.ctx.rank_threshold,
                    )

                return stats, id_to_name
            else:
                print(f"[AI] 未知的 AI 模式: {ai_mode}")
                return [], None

        except Exception as e:
            print(f"[AI] 准备 {ai_mode} 模式数据时出错: {e}")
            if self.ctx.config.get("DEBUG", False):
                import traceback
                traceback.print_exc()
            return [], None

    # ------------------------------------------------------------------
    # 执行分析
    # ------------------------------------------------------------------
    def run_analysis(
        self,
        frequency_file: str,
        stats: List[Dict],
        rss_items: Optional[List[Dict]],
        mode: str,
        report_type: str,
        id_to_name: Optional[Dict],
        current_results: Optional[Dict] = None,
        schedule=None,
        standalone_data: Optional[Dict] = None,
    ):
        """执行 AI 分析"""
        # 延迟导入：AIAnalyzer 会引入 litellm，放到顶层会显著拖慢 CLI 启动
        from trendradar.ai import AIAnalysisResult, AIAnalyzer

        analysis_config = self.ctx.config.get("AI_ANALYSIS", {})
        if not analysis_config.get("ENABLED", False):
            return None

        # 调度系统决策
        if not schedule.analyze:
            print("[AI] 调度器: 当前时间段不执行 AI 分析")
            return None

        if schedule.once_analyze and schedule.period_key:
            scheduler = self.ctx.create_scheduler()
            date_str = self.ctx.format_date()
            if scheduler.already_executed(schedule.period_key, "analyze", date_str):
                print(f"[AI] 调度器: 时间段 {schedule.period_name or schedule.period_key} 今天已分析过，跳过")
                return None
            else:
                print(f"[AI] 调度器: 时间段 {schedule.period_name or schedule.period_key} 今天首次分析")

        print("[AI] 正在进行 AI 分析...")
        try:
            ai_config = self.ctx.config.get("AI", {})
            # 分析任务可配置独立模型（如推理模型 glm-4.7-flash），未配置则沿用全局 ai.model
            analysis_model = ai_config.get("analysis_model", "")
            if analysis_model:
                ai_config = dict(ai_config)
                ai_config["MODEL"] = analysis_model
            debug_mode = self.ctx.config.get("DEBUG", False)
            analyzer = AIAnalyzer(ai_config, analysis_config, self.ctx.get_time, debug=debug_mode)

            # 确定 AI 分析使用的模式
            ai_mode_config = analysis_config.get("MODE", "follow_report")
            if ai_mode_config == "follow_report":
                # 跟随推送报告模式
                ai_mode = mode
                ai_stats = stats
                ai_id_to_name = id_to_name
            elif ai_mode_config in ["daily", "current", "incremental"]:
                # 使用独立配置的模式，需要重新准备数据
                ai_mode = ai_mode_config
                if ai_mode != mode:
                    print(f"[AI] 使用独立分析模式: {ai_mode} (推送模式: {mode})")
                    print(f"[AI] 正在准备 {ai_mode} 模式的数据...")

                    # 根据 AI 模式重新准备数据
                    ai_stats, ai_id_to_name = self.prepare_analysis_data(
                        frequency_file, ai_mode, current_results, id_to_name
                    )
                    if not ai_stats:
                        print(f"[AI] 警告: 无法准备 {ai_mode} 模式的数据，回退到推送模式数据")
                        ai_stats = stats
                        ai_id_to_name = id_to_name
                        ai_mode = mode
                else:
                    ai_stats = stats
                    ai_id_to_name = id_to_name
            else:
                # 配置错误，回退到跟随模式
                print(f"[AI] 警告: 无效的 ai_analysis.mode 配置 '{ai_mode_config}'，使用推送模式 '{mode}'")
                ai_mode = mode
                ai_stats = stats
                ai_id_to_name = id_to_name

            # 提取平台列表
            platforms = list(ai_id_to_name.values()) if ai_id_to_name else []

            # 提取关键词列表
            keywords = [s.get("word", "") for s in ai_stats if s.get("word")] if ai_stats else []

            # 确定报告类型
            if ai_mode != mode:
                # 根据 AI 模式确定报告类型
                ai_report_type = {
                    "daily": "当日汇总",
                    "current": "当前榜单",
                    "incremental": "增量更新"
                }.get(ai_mode, report_type)
            else:
                ai_report_type = report_type

            # 独立 AI 模式（ai_mode != 推送 mode）下，rss_items/standalone_data 仍是推送 mode 的数据，
            # 与 ai_mode 的热榜 ai_stats 不同源。为避免时间窗错配的数据误导分析，独立模式下不向 AI
            # 传入 RSS/独立展示区，使其专注于 ai_mode 的热榜分析（同 mode 时正常传入）。
            ai_rss_stats = rss_items if ai_mode == mode else None
            ai_standalone = standalone_data if ai_mode == mode else None
            if ai_mode != mode and (rss_items or standalone_data):
                print(f"[AI] 独立分析模式（{ai_mode}）：RSS/独立展示区与推送模式（{mode}）不同源，本次分析仅聚焦热榜")

            result = analyzer.analyze(
                stats=ai_stats,
                rss_stats=ai_rss_stats,
                report_mode=ai_mode,
                report_type=ai_report_type,
                platforms=platforms,
                keywords=keywords,
                standalone_data=ai_standalone,
            )

            # 设置 AI 分析使用的模式
            if result.success:
                result.ai_mode = ai_mode
                if result.error:
                    # 成功但有警告（如 JSON 解析问题但使用了原始文本）
                    print(f"[AI] 分析完成（有警告: {result.error}）")
                else:
                    print("[AI] 分析完成")

                # 记录 AI 分析
                if schedule.once_analyze and schedule.period_key:
                    scheduler = self.ctx.create_scheduler()
                    date_str = self.ctx.format_date()
                    scheduler.record_execution(schedule.period_key, "analyze", date_str)
            elif result.skipped:
                print(f"[AI] {result.error}")
            else:
                print(f"[AI] 分析失败: {result.error}")

            return result
        except Exception as e:
            import sys
            import traceback
            error_type = type(e).__name__
            error_msg = str(e)
            # 截断过长的错误消息
            if len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            print(f"[AI] 分析出错 ({error_type}): {error_msg}")
            # 详细错误日志到 stderr
            print(f"[AI] 详细错误堆栈:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return AIAnalysisResult(success=False, error=f"{error_type}: {error_msg}")
