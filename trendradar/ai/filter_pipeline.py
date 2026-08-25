# coding=utf-8
"""
AI 筛选流水线

从 context.py 提取的完整 AI 筛选业务流程：
标签管理 → 待分类新闻收集 → 批量 AI 分类 → 结果保存 → 报告数据转换
"""

from typing import Any, Callable, Dict, List, Optional

from trendradar.ai.filter import AIFilter, AIFilterResult
from trendradar.utils.time import (
    DEFAULT_TIMEZONE,
    convert_time_for_display,
    format_iso_time_friendly,
    is_within_days,
)


class AIFilterPipeline:
    """AI 筛选流水线，编排标签提取、批量分类、结果存储的完整流程"""

    def __init__(
        self,
        config: Dict[str, Any],
        storage_manager: Any,
        get_time_func: Callable,
    ):
        self.config = config
        self.storage = storage_manager
        self.get_time = get_time_func

        self._ai_config = config.get("AI", {})
        self._filter_config = config.get("AI_FILTER", {})
        self._debug = config.get("DEBUG", False)

        rss_config = config.get("RSS", {})
        self._rss_enabled = rss_config.get("ENABLED", False)
        self._rss_feeds = rss_config.get("FEEDS", [])

        freshness_config = rss_config.get("FRESHNESS_FILTER", {})
        self._freshness_enabled = freshness_config.get("ENABLED", True)
        self._default_max_age_days = freshness_config.get("MAX_AGE_DAYS", 3)
        self._timezone = config.get("TIMEZONE", DEFAULT_TIMEZONE)

        self._priority_sort_enabled = config.get("FILTER", {}).get("PRIORITY_SORT_ENABLED", False)
        self._rank_threshold = config.get("RANK_THRESHOLD", 50)
        self._max_news = config.get("MAX_NEWS_PER_KEYWORD", 0)

        self._feed_max_age_map = self._build_feed_max_age_map()

    def _build_feed_max_age_map(self) -> Dict[str, int]:
        result = {}
        for feed_cfg in self._rss_feeds:
            feed_id = feed_cfg.get("id", "")
            max_age = feed_cfg.get("max_age_days")
            if max_age is not None:
                try:
                    result[feed_id] = int(max_age)
                except (ValueError, TypeError):
                    pass
        return result

    def run(self, interests_file: Optional[str] = None) -> Optional[AIFilterResult]:
        """
        执行 AI 智能筛选完整流程

        1. 读取兴趣描述文件，计算 hash
        2. 对比数据库 prompt_hash，决定是否重新提取标签
        3. 收集待分类新闻（去重）
        4. 按 batch_size 分组调用 AI 分类
        5. 保存结果
        6. 查询 active 结果，按标签分组返回
        """
        filter_config = self._filter_config

        ai_filter = AIFilter(self._ai_config, filter_config, self.get_time, self._debug)

        configured_interests = interests_file or filter_config.get("INTERESTS_FILE")
        effective_interests_file = configured_interests or "ai_interests.txt"

        if self._debug:
            print(f"[AI筛选][DEBUG] === 配置信息 ===")
            print(f"[AI筛选][DEBUG] 存储后端: {self.storage.backend_name}")
            print(f"[AI筛选][DEBUG] batch_size={filter_config.get('BATCH_SIZE', 200)}, "
                  f"batch_interval={filter_config.get('BATCH_INTERVAL', 5)}")
            print(f"[AI筛选][DEBUG] interests_file={effective_interests_file}")
            print(f"[AI筛选][DEBUG] prompt_file={filter_config.get('PROMPT_FILE', 'prompt.txt')}")
            print(f"[AI筛选][DEBUG] extract_prompt_file={filter_config.get('EXTRACT_PROMPT_FILE', 'extract_prompt.txt')}")

        # 1. 读取兴趣描述
        interests_content = ai_filter.load_interests_content(configured_interests)
        if not interests_content:
            return AIFilterResult(success=False, error="兴趣描述文件为空或不存在")

        current_hash = ai_filter.compute_interests_hash(interests_content, effective_interests_file)

        if self._debug:
            print(f"[AI筛选][DEBUG] 兴趣描述 hash: {current_hash}")
            print(f"[AI筛选][DEBUG] 兴趣描述内容 ({len(interests_content)} 字符):\n{interests_content}")

        # 2. 开启批量模式
        self.storage.begin_batch()

        # 3. 检查提示词是否变更
        stored_hash = self.storage.get_latest_prompt_hash(interests_file=effective_interests_file)

        if self._debug:
            print(f"[AI筛选][DEBUG] 数据库存储 hash: {stored_hash}")
            print(f"[AI筛选][DEBUG] hash 对比: stored={stored_hash} vs current={current_hash} → {'匹配' if stored_hash == current_hash else '不匹配'}")

        if stored_hash != current_hash:
            self._handle_tag_update(
                ai_filter, interests_content, current_hash, stored_hash,
                effective_interests_file, filter_config,
            )

        # 获取当前 active 标签
        active_tags = self.storage.get_active_ai_filter_tags(interests_file=effective_interests_file)
        if self._debug:
            print(f"[AI筛选][DEBUG] 从数据库获取 active 标签: {len(active_tags)} 个")
            for t in active_tags:
                print(f"[AI筛选][DEBUG]   id={t['id']} tag={t['tag']} priority={t.get('priority', 9999)} version={t.get('version')} hash={t.get('prompt_hash', '')[:8]}...")

        if not active_tags:
            self.storage.end_batch()
            return AIFilterResult(success=False, error="没有可用的标签")

        print(f"[AI筛选] 使用 {len(active_tags)} 个标签")

        # 4. 收集待分类新闻
        pending_news, pending_rss, all_news, analyzed_hotlist, all_rss, analyzed_rss, freshness_filtered_rss = self._collect_pending_news(effective_interests_file)

        self._print_pending_stats(
            all_news, analyzed_hotlist, pending_news,
            all_rss, analyzed_rss, pending_rss, freshness_filtered_rss,
        )

        total_pending = len(pending_news) + len(pending_rss)
        if total_pending == 0:
            print("[AI筛选] 没有新增新闻需要分类")

        # 5. 批量分类
        total_results, succeeded_news_ids, succeeded_rss_ids = self._classify_batches(
            ai_filter, pending_news, pending_rss, active_tags, interests_content, filter_config,
        )

        # 6. 保存结果
        self._save_results(
            total_results, succeeded_news_ids, succeeded_rss_ids,
            effective_interests_file, current_hash,
        )

        # 7. 结束批量模式
        self.storage.end_batch()

        # 8. 查询并组装返回结果
        all_results = self.storage.get_active_ai_filter_results(interests_file=effective_interests_file)

        if self._debug:
            print(f"[AI筛选][DEBUG] === 最终汇总 ===")
            print(f"[AI筛选][DEBUG] 数据库 active 分类结果: {len(all_results)} 条")
            tag_counts: dict = {}
            for r in all_results:
                tag_name = r.get("tag", "?")
                src_type = r.get("source_type", "?")
                key = f"{tag_name}({src_type})"
                tag_counts[key] = tag_counts.get(key, 0) + 1
            for key, count in sorted(tag_counts.items()):
                print(f"[AI筛选][DEBUG]   {key}: {count} 条")

        return self._build_filter_result(all_results, active_tags, total_pending)

    def _handle_tag_update(
        self,
        ai_filter: AIFilter,
        interests_content: str,
        current_hash: str,
        stored_hash: Optional[str],
        effective_interests_file: str,
        filter_config: Dict,
    ) -> None:
        new_version = self.storage.get_latest_ai_filter_tag_version() + 1
        threshold = filter_config.get("RECLASSIFY_THRESHOLD", 0.6)

        if stored_hash is None:
            print(f"[AI筛选] 首次运行 ({effective_interests_file})，提取标签...")
            tags_data = ai_filter.extract_tags(interests_content)
            if not tags_data:
                self.storage.end_batch()
                raise _TagExtractionError()
            tags_data = _with_ordered_priorities(tags_data, start_priority=1)
            saved_count = self.storage.save_ai_filter_tags(tags_data, new_version, current_hash, interests_file=effective_interests_file)
            print(f"[AI筛选] 已保存 {saved_count} 个标签 (版本 {new_version})")
            return

        old_tags = self.storage.get_active_ai_filter_tags(interests_file=effective_interests_file)
        update_result = ai_filter.update_tags(old_tags, interests_content)

        if update_result is None:
            print(f"[AI筛选] AI 标签更新失败，回退到重新提取")
            tags_data = ai_filter.extract_tags(interests_content)
            if not tags_data:
                self.storage.end_batch()
                raise _TagExtractionError()
            tags_data = _with_ordered_priorities(tags_data, start_priority=1)
            deprecated_count = self.storage.deprecate_all_ai_filter_tags(interests_file=effective_interests_file)
            self.storage.clear_analyzed_news(interests_file=effective_interests_file)
            saved_count = self.storage.save_ai_filter_tags(tags_data, new_version, current_hash, interests_file=effective_interests_file)
            print(f"[AI筛选] 废弃 {deprecated_count} 个旧标签, 保存 {saved_count} 个新标签 (版本 {new_version})")
            return

        change_ratio = update_result["change_ratio"]
        keep_tags = update_result["keep"]
        add_tags = update_result["add"]
        remove_tags = update_result["remove"]

        if self._debug:
            print(f"[AI筛选][DEBUG] AI 标签更新: keep={len(keep_tags)}, add={len(add_tags)}, remove={len(remove_tags)}, change_ratio={change_ratio:.2f}, threshold={threshold:.2f}")

        if change_ratio >= threshold:
            print(f"[AI筛选] 兴趣文件变更: {effective_interests_file} (AI change_ratio={change_ratio:.2f} >= threshold={threshold:.2f} → 全量重分类)")
            tags_data = ai_filter.extract_tags(interests_content)
            if not tags_data:
                self.storage.end_batch()
                raise _TagExtractionError()
            tags_data = _with_ordered_priorities(tags_data, start_priority=1)
            deprecated_count = self.storage.deprecate_all_ai_filter_tags(interests_file=effective_interests_file)
            self.storage.clear_analyzed_news(interests_file=effective_interests_file)
            saved_count = self.storage.save_ai_filter_tags(tags_data, new_version, current_hash, interests_file=effective_interests_file)
            print(f"[AI筛选] 废弃 {deprecated_count} 个旧标签, 保存 {saved_count} 个新标签 (版本 {new_version})")
        else:
            self._apply_incremental_update(
                old_tags, keep_tags, add_tags, remove_tags,
                change_ratio, threshold, new_version, current_hash,
                effective_interests_file,
            )

    def _apply_incremental_update(
        self,
        old_tags, keep_tags, add_tags, remove_tags,
        change_ratio, threshold, new_version, current_hash,
        effective_interests_file,
    ) -> None:
        print(f"[AI筛选] 兴趣文件变更: {effective_interests_file} (AI change_ratio={change_ratio:.2f} < threshold={threshold:.2f} → 增量更新)")
        print(f"[AI筛选]   保留 {len(keep_tags)} 个标签, 新增 {len(add_tags)} 个, 废弃 {len(remove_tags)} 个")

        if remove_tags:
            remove_set = set(remove_tags)
            removed_ids = [t["id"] for t in old_tags if t["tag"] in remove_set]
            if removed_ids:
                self.storage.deprecate_specific_ai_filter_tags(removed_ids)
                if self._debug:
                    print(f"[AI筛选][DEBUG] 废弃标签 IDs: {removed_ids}")

        keep_with_priority = []
        if keep_tags:
            self.storage.update_ai_filter_tag_descriptions(keep_tags, interests_file=effective_interests_file)
            keep_with_priority = _with_ordered_priorities(keep_tags, start_priority=1)
            self.storage.update_ai_filter_tag_priorities(keep_with_priority, interests_file=effective_interests_file)

        if add_tags:
            add_start = keep_with_priority[-1]["priority"] + 1 if keep_with_priority else 1
            add_with_priority = _with_ordered_priorities(add_tags, start_priority=add_start)
            saved_count = self.storage.save_ai_filter_tags(add_with_priority, new_version, current_hash, interests_file=effective_interests_file)
            if self._debug:
                print(f"[AI筛选][DEBUG] 新增保存 {saved_count} 个标签")

        self.storage.update_ai_filter_tags_hash(effective_interests_file, current_hash)

        if add_tags:
            cleared = self.storage.clear_unmatched_analyzed_news(interests_file=effective_interests_file)
            if cleared > 0:
                print(f"[AI筛选]   清除 {cleared} 条不匹配记录，将在新标签下重新分析")

    def _collect_pending_news(self, effective_interests_file: str):
        all_news = self.storage.get_all_news_ids()
        analyzed_hotlist = self.storage.get_analyzed_news_ids("hotlist", interests_file=effective_interests_file)
        pending_news = [n for n in all_news if n["id"] not in analyzed_hotlist]

        pending_rss = []
        freshness_filtered_rss = 0
        all_rss = []
        analyzed_rss = set()

        if self._rss_enabled:
            all_rss = self.storage.get_all_rss_ids()

            fresh_rss = []
            for n in all_rss:
                published_at = n.get("published_at", "")
                feed_id = n.get("source_id", "")
                max_days = self._feed_max_age_map.get(feed_id, self._default_max_age_days)
                if self._freshness_enabled and max_days > 0 and published_at:
                    if not is_within_days(published_at, max_days, self._timezone):
                        freshness_filtered_rss += 1
                        continue
                fresh_rss.append(n)

            analyzed_rss = self.storage.get_analyzed_news_ids("rss", interests_file=effective_interests_file)
            pending_rss = [n for n in fresh_rss if n["id"] not in analyzed_rss]

        return pending_news, pending_rss, all_news, analyzed_hotlist, all_rss, analyzed_rss, freshness_filtered_rss

    def _print_pending_stats(self, all_news, analyzed_hotlist, pending_news, all_rss, analyzed_rss, pending_rss, freshness_filtered_rss):
        hotlist_total = len(all_news)
        hotlist_skipped = len(analyzed_hotlist)
        hotlist_pending = len(pending_news)
        print(f"[AI筛选] 热榜: 总计 {hotlist_total} 条, 已分析跳过 {hotlist_skipped} 条, 本次发送AI分析 {hotlist_pending} 条")
        if self._rss_enabled:
            rss_total = len(all_rss)
            rss_skipped = len(analyzed_rss)
            rss_pending = len(pending_rss)
            freshness_info = f", 新鲜度过滤 {freshness_filtered_rss} 条" if freshness_filtered_rss > 0 else ""
            print(f"[AI筛选] RSS: 总计 {rss_total} 条{freshness_info}, 已分析跳过 {rss_skipped} 条, 本次发送AI分析 {rss_pending} 条")

    def _classify_batches(self, ai_filter, pending_news, pending_rss, active_tags, interests_content, filter_config):
        batch_size = filter_config.get("BATCH_SIZE", 200)
        batch_interval = filter_config.get("BATCH_INTERVAL", 5)
        total_results = []
        batch_count = 0

        succeeded_news_ids = []
        for i in range(0, len(pending_news), batch_size):
            if batch_count > 0 and batch_interval > 0:
                import time
                print(f"[AI筛选] 批次间隔等待 {batch_interval} 秒...")
                time.sleep(batch_interval)
            batch = pending_news[i:i + batch_size]
            titles_for_ai = [
                {"id": n["id"], "title": n["title"], "source": n.get("source_name", "")}
                for n in batch
            ]
            batch_results = ai_filter.classify_batch(titles_for_ai, active_tags, interests_content)
            batch_count += 1
            if batch_results is None:
                print(f"[AI筛选] 热榜批次 {i // batch_size + 1}: {len(batch)} 条 → 分类失败，将在下次运行重试")
                continue
            for r in batch_results:
                r["source_type"] = "hotlist"
            total_results.extend(batch_results)
            succeeded_news_ids.extend(n["id"] for n in batch)
            print(f"[AI筛选] 热榜批次 {i // batch_size + 1}: {len(batch)} 条 → {len(batch_results)} 条匹配")

        succeeded_rss_ids = []
        for i in range(0, len(pending_rss), batch_size):
            if batch_count > 0 and batch_interval > 0:
                import time
                print(f"[AI筛选] 批次间隔等待 {batch_interval} 秒...")
                time.sleep(batch_interval)
            batch = pending_rss[i:i + batch_size]
            titles_for_ai = [
                {"id": n["id"], "title": n["title"], "source": n.get("source_name", "")}
                for n in batch
            ]
            batch_results = ai_filter.classify_batch(titles_for_ai, active_tags, interests_content)
            batch_count += 1
            if batch_results is None:
                print(f"[AI筛选] RSS 批次 {i // batch_size + 1}: {len(batch)} 条 → 分类失败，将在下次运行重试")
                continue
            for r in batch_results:
                r["source_type"] = "rss"
            total_results.extend(batch_results)
            succeeded_rss_ids.extend(n["id"] for n in batch)
            print(f"[AI筛选] RSS 批次 {i // batch_size + 1}: {len(batch)} 条 → {len(batch_results)} 条匹配")

        return total_results, succeeded_news_ids, succeeded_rss_ids

    def _save_results(self, total_results, succeeded_news_ids, succeeded_rss_ids, effective_interests_file, current_hash):
        if total_results:
            saved = self.storage.save_ai_filter_results(total_results)
            print(f"[AI筛选] 保存 {saved} 条分类结果")
            if self._debug and saved != len(total_results):
                print(f"[AI筛选][DEBUG] !! 保存数量不一致: 期望 {len(total_results)}, 实际 {saved}（可能有重复记录被跳过）")

        matched_hotlist_ids = {r["news_item_id"] for r in total_results if r.get("source_type") == "hotlist"}
        matched_rss_ids = {r["news_item_id"] for r in total_results if r.get("source_type") == "rss"}

        if succeeded_news_ids:
            self.storage.save_analyzed_news(
                succeeded_news_ids, "hotlist", effective_interests_file,
                current_hash, matched_hotlist_ids
            )

        if succeeded_rss_ids:
            self.storage.save_analyzed_news(
                succeeded_rss_ids, "rss", effective_interests_file,
                current_hash, matched_rss_ids
            )

        if succeeded_news_ids or succeeded_rss_ids:
            total_analyzed = len(succeeded_news_ids) + len(succeeded_rss_ids)
            total_matched = len(matched_hotlist_ids) + len(matched_rss_ids)
            print(f"[AI筛选] 已记录 {total_analyzed} 条新闻分析状态 (匹配 {total_matched}, 不匹配 {total_analyzed - total_matched})")

    def _build_filter_result(
        self,
        raw_results: List[Dict],
        tags: List[Dict],
        total_processed: int,
    ) -> AIFilterResult:
        tag_priority_map = {}
        for idx, t in enumerate(tags, start=1):
            tag_name = str(t.get("tag", "")).strip() if isinstance(t, dict) else ""
            if not tag_name:
                continue
            try:
                tag_priority_map[tag_name] = int(t.get("priority", idx))
            except (TypeError, ValueError):
                tag_priority_map[tag_name] = idx

        tag_groups: Dict[str, Dict] = {}
        seen_titles: Dict[str, set] = {}

        for r in raw_results:
            tag_name = r["tag"]
            if tag_name not in tag_groups:
                raw_priority = r.get("tag_priority", tag_priority_map.get(tag_name, 9999))
                try:
                    tag_position = int(raw_priority)
                except (TypeError, ValueError):
                    tag_position = 9999
                tag_groups[tag_name] = {
                    "tag": tag_name,
                    "description": r.get("tag_description", ""),
                    "position": tag_position,
                    "count": 0,
                    "items": [],
                }
                seen_titles[tag_name] = set()

            title = r["title"]
            if title in seen_titles[tag_name]:
                continue
            seen_titles[tag_name].add(title)

            tag_groups[tag_name]["items"].append({
                "title": title,
                "source_id": r.get("source_id", ""),
                "source_name": r.get("source_name", ""),
                "url": r.get("url", ""),
                "mobile_url": r.get("mobile_url", ""),
                "rank": r.get("rank", 0),
                "ranks": r.get("ranks", []),
                "first_time": r.get("first_time", ""),
                "last_time": r.get("last_time", ""),
                "count": r.get("count", 1),
                "relevance_score": r.get("relevance_score", 0),
                "source_type": r.get("source_type", "hotlist"),
            })
            tag_groups[tag_name]["count"] += 1

        if self._priority_sort_enabled:
            sorted_tags = sorted(
                tag_groups.values(),
                key=lambda x: (x.get("position", 9999), -x["count"], x["tag"]),
            )
        else:
            sorted_tags = sorted(
                tag_groups.values(),
                key=lambda x: (-x["count"], x.get("position", 9999), x["tag"]),
            )

        total_matched = sum(t["count"] for t in sorted_tags)

        return AIFilterResult(
            tags=sorted_tags,
            total_matched=total_matched,
            total_processed=total_processed,
            success=True,
        )

    def convert_to_report_data(
        self,
        ai_filter_result: AIFilterResult,
        mode: str = "daily",
        new_titles: Optional[Dict] = None,
        rss_new_urls: Optional[set] = None,
    ) -> tuple:
        """
        将 AI 筛选结果转换为与关键词匹配相同的数据结构

        Returns:
            (hotlist_stats, rss_stats, rss_new_stats)
        """
        hotlist_stats = []
        rss_stats = []
        rss_new_stats = []
        min_score = self._filter_config.get("MIN_SCORE", 0)

        latest_time = None
        if mode == "current":
            for tag_data in ai_filter_result.tags:
                for item in tag_data.get("items", []):
                    if item.get("source_type", "hotlist") == "hotlist":
                        last_time = item.get("last_time", "")
                        if last_time and (latest_time is None or last_time > latest_time):
                            latest_time = last_time
            if latest_time:
                print(f"[AI筛选] current 模式：最新时间 {latest_time}，过滤已下榜新闻")

        filtered_count = 0
        for tag_data in ai_filter_result.tags:
            tag_name = tag_data.get("tag", "")
            items = tag_data.get("items", [])
            if not items:
                continue

            hotlist_titles = []
            rss_titles = []

            for item in items:
                source_type = item.get("source_type", "hotlist")

                if mode == "current" and latest_time and source_type == "hotlist":
                    if item.get("last_time", "") != latest_time:
                        filtered_count += 1
                        continue

                if min_score > 0:
                    score = item.get("relevance_score", 0)
                    if score < min_score:
                        continue

                first_time = item.get("first_time", "")
                last_time = item.get("last_time", "")
                if source_type == "rss":
                    if self._freshness_enabled and first_time:
                        feed_id = item.get("source_id", "")
                        max_days = self._feed_max_age_map.get(feed_id, self._default_max_age_days)
                        if max_days > 0 and not is_within_days(first_time, max_days, self._timezone):
                            continue
                    time_display = format_iso_time_friendly(first_time, self._timezone, include_date=True) if first_time else ""
                else:
                    if first_time and last_time and first_time != last_time:
                        first_display = convert_time_for_display(first_time)
                        last_display = convert_time_for_display(last_time)
                        time_display = f"[{first_display} ~ {last_display}]"
                    elif first_time:
                        time_display = convert_time_for_display(first_time)
                    else:
                        time_display = ""

                if source_type == "rss":
                    is_new = False
                    if rss_new_urls:
                        item_url = item.get("url", "")
                        is_new = item_url in rss_new_urls if item_url else False
                else:
                    is_new = False
                    if new_titles:
                        item_source_id = item.get("source_id", "")
                        item_title = item.get("title", "")
                        if item_source_id in new_titles:
                            is_new = item_title in new_titles[item_source_id]

                if mode == "incremental" and not is_new:
                    continue

                title_entry = {
                    "title": item.get("title", ""),
                    "source_name": item.get("source_name", ""),
                    "url": item.get("url", ""),
                    "mobile_url": item.get("mobile_url", ""),
                    "ranks": item.get("ranks", []),
                    "rank_threshold": self._rank_threshold,
                    "count": item.get("count", 1),
                    "is_new": is_new,
                    "time_display": time_display,
                    "matched_keyword": tag_name,
                }

                if source_type == "rss":
                    rss_titles.append(title_entry)
                else:
                    hotlist_titles.append(title_entry)

            if hotlist_titles:
                if self._max_news > 0:
                    hotlist_titles = hotlist_titles[:self._max_news]
                hotlist_stats.append({
                    "word": tag_name,
                    "count": len(hotlist_titles),
                    "position": tag_data.get("position", 9999),
                    "titles": hotlist_titles,
                })

            if rss_titles:
                if self._max_news > 0:
                    rss_titles = rss_titles[:self._max_news]
                rss_stats.append({
                    "word": tag_name,
                    "count": len(rss_titles),
                    "position": tag_data.get("position", 9999),
                    "titles": rss_titles,
                })
                new_rss_titles = [t for t in rss_titles if t.get("is_new")]
                if new_rss_titles:
                    rss_new_stats.append({
                        "word": tag_name,
                        "count": len(new_rss_titles),
                        "position": tag_data.get("position", 9999),
                        "titles": new_rss_titles,
                    })

        if mode == "current" and filtered_count > 0:
            total_kept = sum(s["count"] for s in hotlist_stats)
            print(f"[AI筛选] current 模式：过滤 {filtered_count} 条已下榜新闻，保留 {total_kept} 条当前在榜")

        if min_score > 0:
            hotlist_kept = sum(s["count"] for s in hotlist_stats)
            rss_kept = sum(s["count"] for s in rss_stats)
            total_kept = hotlist_kept + rss_kept
            parts = [f"热榜 {hotlist_kept} 条"]
            if rss_kept > 0:
                parts.append(f"RSS {rss_kept} 条")
            print(f"[AI筛选] 分数过滤：min_score={min_score}，保留 {total_kept} 条 score≥{min_score} ({', '.join(parts)})")

        sort_key_priority = lambda x: (x.get("position", 9999), -x["count"], x["word"])
        sort_key_count = lambda x: (-x["count"], x.get("position", 9999), x["word"])
        sort_key = sort_key_priority if self._priority_sort_enabled else sort_key_count
        hotlist_stats.sort(key=sort_key)
        rss_stats.sort(key=sort_key)
        rss_new_stats.sort(key=sort_key)

        return hotlist_stats, rss_stats, rss_new_stats


class _TagExtractionError(Exception):
    pass


def _with_ordered_priorities(tags: List[Dict], start_priority: int = 1) -> List[Dict]:
    normalized: List[Dict] = []
    priority = start_priority
    for tag_data in tags:
        if not isinstance(tag_data, dict):
            continue
        tag_name = str(tag_data.get("tag", "")).strip()
        if not tag_name:
            continue
        item = dict(tag_data)
        item["tag"] = tag_name
        item["priority"] = priority
        normalized.append(item)
        priority += 1
    return normalized
