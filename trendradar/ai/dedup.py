# coding=utf-8
"""
AI 语义去重模块

多源同事件语义去重：不同平台/不同语言对同一事件的报道标题各异，
关键词精确匹配无法合并。本模块分两步合并：

1. 规范化精确去重（无 AI）：全角/半角、空白、标点、大小写归一后
   相同的标题直接合并，不消耗 token；
2. AI 语义判定：剩余标题按批送对话模型（如 glm-4-flash，免费），
   判定"同事件"分组（如 "iPhone 发布" vs "苹果新品发布会" vs
   "iPhone launch"），返回 JSON 编号分组。

设计约束：
- 失败静默降级：未启用/无 Key/解析失败/异常 → 返回原始列表，
  绝不拖垮报告生成主流程；
- AICache 持久缓存批次判定结果（kind="dedup"），跨运行零成本复用；
- 合并策略：保留首条为主条目，多来源以"+"连接，ranks/timeline 取
  并集，count 取 max（不虚增出现次数）。
"""

import json
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional

from trendradar.ai.cache import AICache
from trendradar.ai.client import AIClient
from trendradar.ai.prompt_loader import load_prompt_template
from trendradar.core.logger import get_logger

log = get_logger(__name__)

_CACHE_KIND = "dedup"


def _normalize_title(title: str) -> str:
    """标题规范化：全角转半角、小写、去空白与标点（用于精确去重先行）。"""
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", str(title)).lower()
    # 去掉所有非字母数字字符（含空白、标点、下划线；NFKC 下中文保留）
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE)


class AIDeduplicator:
    """AI 语义去重器"""

    def __init__(self, dedup_config: Dict[str, Any], ai_config: Dict[str, Any]):
        """
        初始化语义去重器

        Args:
            dedup_config: 语义去重配置 (AI_DEDUP)
            ai_config: AI 模型配置（LiteLLM 格式）
        """
        self.config = dedup_config
        self.enabled = dedup_config.get("ENABLED", False)
        self.batch_size = max(2, int(dedup_config.get("BATCH_SIZE", 30) or 30))
        self.batch_interval = max(0, int(dedup_config.get("BATCH_INTERVAL", 2) or 0))

        # 创建 AI 客户端（基于 LiteLLM）
        self.client = AIClient(ai_config)

        # AI 结果缓存：批次判定结果跨运行复用（kind="dedup"）
        self.cache = AICache(
            db_path=ai_config.get("CACHE_DB_PATH"),
            enabled=dedup_config.get("CACHE_ENABLED", True),
        )

        # 加载提示词模板；缺失时仅做规范化精确去重
        self.system_prompt, self.user_prompt_template = load_prompt_template(
            dedup_config.get("PROMPT_FILE", "ai_dedup_prompt.txt"),
            label="语义去重",
        )
        if self.enabled and not self.user_prompt_template:
            log.info("[语义去重] 提示词文件缺失，仅启用规范化精确去重（跳过 AI 判定）")

    # === 对外入口 ===

    def dedup_stats(self, stats: List[Dict]) -> List[Dict]:
        """
        对统计结果列表做语义去重（就地合并 titles，同步修正 count）。

        Args:
            stats: 统计结果列表，每个 stat 含 titles 列表

        Returns:
            传入的 stats（就地修改）
        """
        if not self.enabled or not stats:
            return stats

        for stat in stats:
            titles = stat.get("titles")
            if not isinstance(titles, list) or len(titles) < 2:
                continue

            merged = self.dedup_titles(titles)
            removed = len(titles) - len(merged)
            if removed > 0:
                stat["titles"] = merged
                # count 语义是"该组标题总数"，同步扣掉被合并掉的条目数
                try:
                    stat["count"] = max(0, int(stat.get("count", 0)) - removed)
                except (TypeError, ValueError):
                    pass

        return stats

    def dedup_titles(self, titles: List[Dict]) -> List[Dict]:
        """
        对单个分组的标题列表做语义去重。

        Args:
            titles: 标题条目列表（dict，含 title/source_name 等字段）

        Returns:
            合并后的新列表（不修改传入列表）；失败时原样返回
        """
        if not self.enabled or not self.client.api_key:
            return list(titles)

        try:
            return self._dedup_internal(titles)
        except Exception as e:
            log.info(f"[语义去重] 失败，跳过去重: {type(e).__name__}: {str(e)[:100]}")
            return list(titles)

    # === 内部实现 ===

    def _dedup_internal(self, titles: List[Dict]) -> List[Dict]:
        # 第一步：规范化精确去重（无 AI）—— norm 相同的条目直接合并
        norm_buckets: Dict[str, List[int]] = {}
        for idx, t in enumerate(titles):
            if not isinstance(t, dict):
                continue
            norm = _normalize_title(t.get("title", ""))
            if not norm:
                continue
            norm_buckets.setdefault(norm, []).append(idx)

        # 每个 norm 组合并为一条，代表条目保持原出现顺序
        reps: List[Dict] = []
        for norm, idx_list in norm_buckets.items():
            group = [titles[i] for i in idx_list]
            reps.append(self._merge_group(group))

        # 第二步：AI 语义判定（分批）——仅一组或无提示词时跳过
        if len(reps) < 2 or not self.user_prompt_template:
            return reps

        final: List[Dict] = []
        batches = [reps[i:i + self.batch_size] for i in range(0, len(reps), self.batch_size)]
        for batch_no, batch in enumerate(batches):
            if self.batch_interval > 0 and batch_no > 0:
                time.sleep(self.batch_interval)
            final.extend(self._dedup_batch(batch))

        return final

    def _dedup_batch(self, batch: List[Dict]) -> List[Dict]:
        """单批判定：缓存分流 → AI 调用 → JSON 解析 → 合并；失败返回原批。"""
        numbered = "\n".join(
            f"[{i}] {t.get('title', '')}" for i, t in enumerate(batch, 1)
        )

        # 缓存命中直接用历史分组结果
        cached = self.cache.get(_CACHE_KIND, numbered)
        if cached:
            groups = self._parse_groups(cached, len(batch))
            if groups is not None:
                return self._merge_groups(batch, groups)

        user_prompt = (
            self.user_prompt_template
            .replace("{count}", str(len(batch)))
            .replace("{titles}", numbered)
        )
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        response = self.client.chat(messages)
        groups = self._parse_groups(response, len(batch))
        if groups is None:
            log.info("[语义去重] 本批响应解析失败，保留原样")
            return list(batch)

        # 判定结果写入缓存（存规范 JSON，取回时按同一套解析校验）
        self.cache.put(
            _CACHE_KIND, numbered,
            json.dumps({"groups": groups}, ensure_ascii=False),
        )
        return self._merge_groups(batch, groups)

    def _parse_groups(self, response: str, expected_count: int) -> Optional[List[List[int]]]:
        """
        解析 AI 响应中的编号分组。

        Returns:
            分组列表（每项为 1-based 编号数组），解析失败返回 None。
            校验宽容：缺号自动补成单元素组，重复/越界编号忽略。
        """
        if not response or not str(response).strip():
            return None

        json_str = str(response)
        # 去掉 markdown 代码块标记
        if "```" in json_str:
            parts = json_str.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    json_str = part
                    break

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            data = None

        # json_repair 本地修复兜底
        if data is None:
            try:
                from json_repair import repair_json
                repaired = repair_json(json_str, return_objects=True)
                if isinstance(repaired, (dict, list)):
                    data = repaired
                    log.info("[语义去重] JSON 本地修复成功（json_repair）")
            except Exception:
                pass

        # 提取 groups
        raw_groups = None
        if isinstance(data, dict):
            raw_groups = data.get("groups")
        elif isinstance(data, list):
            raw_groups = data
        if not isinstance(raw_groups, list):
            return None

        seen = set()
        valid_groups: List[List[int]] = []
        for group in raw_groups:
            if not isinstance(group, list):
                continue
            ids = []
            for x in group:
                try:
                    i = int(x)
                except (TypeError, ValueError):
                    continue
                if 1 <= i <= expected_count and i not in seen:
                    ids.append(i)
                    seen.add(i)
            if ids:
                valid_groups.append(ids)

        if not valid_groups:
            return None

        # 未分配的编号自成一组（保守：缺号不代表可以被吞并）
        for i in range(1, expected_count + 1):
            if i not in seen:
                valid_groups.append([i])

        return valid_groups

    def _merge_groups(self, items: List[Dict], groups: List[List[int]]) -> List[Dict]:
        """按分组合并条目；组间按最早出现编号排序，保持列表大致有序。"""
        result: List[Dict] = []
        for ids in sorted(groups, key=lambda g: min(g)):
            group = [items[i - 1] for i in ids if 1 <= i <= len(items)]
            if not group:
                continue
            result.append(self._merge_group(group))
        return result

    def _merge_group(self, group: List[Dict]) -> Dict:
        """
        合并同事件条目：首条为主条目，来源"+"连接，ranks/timeline 取并集，
        count 取 max，is_new 任一为真。
        """
        if len(group) == 1:
            return group[0]

        primary = dict(group[0])

        # 来源合并（去重保序）
        sources = []
        for t in group:
            s = str(t.get("source_name", "") or "")
            if s and s not in sources:
                sources.append(s)
        if sources:
            primary["source_name"] = "+".join(sources)

        # 出现次数取 max（合并多源不虚增）
        try:
            primary["count"] = max(int(t.get("count", 1) or 1) for t in group)
        except (TypeError, ValueError):
            pass

        # ranks 并集去重（尽量数值排序）
        ranks = []
        for t in group:
            for r in (t.get("ranks") or []):
                if r not in ranks:
                    ranks.append(r)
        try:
            ranks.sort()
        except TypeError:
            pass
        primary["ranks"] = ranks

        # url / mobile_url / time_display 取首个非空
        for key in ("url", "mobile_url", "time_display"):
            if not primary.get(key):
                for t in group[1:]:
                    if t.get(key):
                        primary[key] = t[key]
                        break

        # is_new 任一为真
        primary["is_new"] = any(t.get("is_new") for t in group)

        # rank_timeline 并集（按时间排序去重）
        timeline = []
        for t in group:
            for item in (t.get("rank_timeline") or []):
                if item not in timeline:
                    timeline.append(item)
        if timeline:
            try:
                timeline.sort(key=lambda x: str(x.get("time", "")))
            except Exception:
                pass
            primary["rank_timeline"] = timeline

        return primary
