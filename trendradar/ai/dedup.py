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
- 失败静默降级：未启用/无 Key/解析失败/截断/异常 → 返回原始列表，
  绝不拖垮报告生成主流程；无法规范化的条目（纯 emoji/标点、非 dict）原样保留；
- 预算与熔断在**实例级**共享（跨所有 stat），并接入进程级 AI 总预算；
- AI 分组做本地约束：单组超过 max_group_size（默认 5）视为不可信，拆回单条；
- 分批前按字符 bigram 相似度把疑似同事件的标题排到一起，减少跨批漏合并；
- AICache 缓存批次判定结果（kind 含模型与提示词 hash），同一 job 内（主流程 + daily 补跑）
  复用；跨运行复用需 CI 持久化缓存文件（见 ai.cache_db_path）；
- 合并策略：保留首条为主条目，多来源以"+"连接；ranks/rank_timeline 只合并与主条目
  **同一来源**的条目（不同平台的排名不可比），其余来源的排名记入 merged_sources；
  count 取 max（不虚增出现次数）。
"""

import hashlib
import json
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional

from trendradar.ai import settings
from trendradar.ai.budget import AIBudgetExhausted, get_budget
from trendradar.ai.cache import AICache
from trendradar.ai.client import AIClient
from trendradar.ai.prompt_loader import load_prompt_template
from trendradar.core.logger import get_logger

log = get_logger(__name__)

_CACHE_KIND = "dedup"


_DEFAULT_MAX_GROUP_SIZE = 5
_DEFAULT_BUDGET_SECONDS = 300
_SIMILAR_THRESHOLD = 0.35


def _normalize_title(title: str) -> str:
    """标题规范化：全角转半角、小写、去空白与标点（用于精确去重先行）。

    保留 ``+ # %`` 与数字间的小数点，避免 ``C++``≡``C``、``3.5%``≡``35%`` 这类误合并。
    """
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", str(title)).lower()
    s = re.sub(r"(?<=\d)\.(?=\d)", "\x00", s)          # 数字间小数点先占位
    s = re.sub(r"[^\w+#%\x00]+|_+", "", s, flags=re.UNICODE)
    return s.replace("\x00", ".")


def _sanitize_title(text: Any) -> str:
    """送入 prompt 前清洗：换行/控制字符折叠为空格，防止伪造 ``[k]`` 编号行。"""
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))
    return re.sub(r"\s{2,}", " ", s).strip()


def _bigrams(norm: str) -> set:
    if len(norm) < 2:
        return {norm} if norm else set()
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def _order_by_similarity(norms: List[str]) -> List[int]:
    """贪心聚簇排序：每条标题后面紧跟与它 bigram Jaccard ≥ 阈值的标题，返回位置序列。"""
    grams = [_bigrams(n) for n in norms]
    placed = [False] * len(norms)
    order: List[int] = []
    for i in range(len(norms)):
        if placed[i]:
            continue
        placed[i] = True
        order.append(i)
        gi = grams[i]
        if not gi:
            continue
        for j in range(i + 1, len(norms)):
            if placed[j] or not grams[j]:
                continue
            inter = len(gi & grams[j])
            if inter and inter / len(gi | grams[j]) >= _SIMILAR_THRESHOLD:
                placed[j] = True
                order.append(j)
    return order


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
        raw = settings.raw_section("ai_dedup")
        try:
            self.max_group_size = max(2, int(dedup_config.get("MAX_GROUP_SIZE") or raw.get("max_group_size") or _DEFAULT_MAX_GROUP_SIZE))
        except (TypeError, ValueError):
            self.max_group_size = _DEFAULT_MAX_GROUP_SIZE
        try:
            self.budget_seconds = float(dedup_config.get("BUDGET_SECONDS") or raw.get("budget_seconds") or _DEFAULT_BUDGET_SECONDS)
        except (TypeError, ValueError):
            self.budget_seconds = float(_DEFAULT_BUDGET_SECONDS)

        # 按功能解析模型/温度（env AI_DEDUP_MODEL > env AI_MODEL > yaml ai.dedup_model > ai.model）
        ai_config = settings.feature_ai_config(ai_config, "dedup")

        # 创建 AI 客户端（基于 LiteLLM）
        self.client = AIClient(ai_config)

        # 加载提示词模板；缺失时仅做规范化精确去重
        self.system_prompt, self.user_prompt_template = load_prompt_template(
            dedup_config.get("PROMPT_FILE", "ai_dedup_prompt.txt"),
            label="语义去重",
        )

        # AI 结果缓存：批次判定结果复用（kind 含模型与提示词 hash，换模型/改 prompt 自动失效）
        self.cache = AICache(
            db_path=ai_config.get("CACHE_DB_PATH"),
            enabled=dedup_config.get("CACHE_ENABLED", True),
        )
        prompt_hash = hashlib.sha256(
            f"{self.system_prompt}\x00{self.user_prompt_template}".encode("utf-8")
        ).hexdigest()[:10]
        self.cache_kind = f"{_CACHE_KIND}:v2:{self.client.model}:{prompt_hash}"
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

    def _state(self) -> Dict[str, Any]:
        """实例级预算/熔断状态：跨所有 stat 共享（审计 #8：此前按 stat 重置）。"""
        st = getattr(self, "_run_state", None)
        if st is None:
            st = {"start": None, "consecutive": 0, "stopped": ""}
            self._run_state = st
        return st

    def _should_stop(self) -> str:
        st = self._state()
        if st["stopped"]:
            return st["stopped"]
        if get_budget().exhausted():
            st["stopped"] = "AI 总预算耗尽或熔断"
        elif st["start"] is not None and time.monotonic() - st["start"] > getattr(
                self, "budget_seconds", _DEFAULT_BUDGET_SECONDS):
            st["stopped"] = f"去重累计耗时超预算 {getattr(self, 'budget_seconds', _DEFAULT_BUDGET_SECONDS):.0f}s"
        return st["stopped"]

    def _dedup_internal(self, titles: List[Dict]) -> List[Dict]:
        # 第一步：规范化精确去重（无 AI）—— norm 相同的条目直接合并。
        # 非 dict / 规范化为空（纯 emoji、纯标点）的条目原样透传，不丢弃（审计 #9）
        slots: List[Any] = []            # 输出槽：("raw", item) 或 ("norm", key)
        norm_buckets: Dict[str, List[int]] = {}
        for idx, t in enumerate(titles):
            norm = _normalize_title(t.get("title", "")) if isinstance(t, dict) else ""
            if not norm:
                slots.append(("raw", t))
                continue
            if norm not in norm_buckets:
                norm_buckets[norm] = []
                slots.append(("norm", norm))
            norm_buckets[norm].append(idx)

        reps: List[Dict] = []
        rep_norms: List[str] = []
        rep_slot: Dict[str, int] = {}
        for norm, idx_list in norm_buckets.items():
            rep_slot[norm] = len(reps)
            reps.append(self._merge_group([titles[i] for i in idx_list]))
            rep_norms.append(norm)

        def assemble(rep_outputs: Dict[int, Optional[Dict]]) -> List[Dict]:
            out: List[Dict] = []
            for kind, val in slots:
                if kind == "raw":
                    out.append(val)
                else:
                    item = rep_outputs.get(rep_slot[val], reps[rep_slot[val]])
                    if item is not None:
                        out.append(item)
            return out

        # 第二步：AI 语义判定（分批）——仅一组或无提示词时跳过
        if len(reps) < 2 or not self.user_prompt_template:
            return assemble({})

        # 疑似同事件的标题排到一起再切批，减少同一事件落到不同批次（审计 #11）
        order = _order_by_similarity(rep_norms)
        batches = [order[i:i + self.batch_size] for i in range(0, len(order), self.batch_size)]

        # 预算与熔断：AI 服务不可用时每批会挂满超时。去重是可选增强：
        # 超预算/连续失败/全局 AI 预算耗尽后放弃剩余批（保留未合并的原文），不影响主流程。
        st = self._state()
        if st["start"] is None:
            st["start"] = time.monotonic()
        outputs: Dict[int, Optional[Dict]] = {}
        for batch_no, positions in enumerate(batches):
            if len(positions) < 2:
                continue
            reason = self._should_stop()
            if reason:
                log.warning(f"[语义去重] {reason}，放弃剩余 {len(batches) - batch_no} 批（保留原文）")
                break
            if self.batch_interval > 0 and batch_no > 0:
                time.sleep(self.batch_interval)
            batch = [reps[p] for p in positions]
            try:
                groups = self._dedup_batch_groups(batch)
            except AIBudgetExhausted as e:
                st["stopped"] = str(e)
                log.warning(f"[语义去重] {e}，跳过剩余批次（保留原文）")
                break
            except Exception as e:
                # 单批失败就地消化，不让异常冒泡丢弃已成功的批次
                log.warning(f"[语义去重] 第 {batch_no + 1} 批失败: {type(e).__name__}: {str(e)[:150]}")
                groups = None

            if groups is None:
                st["consecutive"] += 1
                if st["consecutive"] >= 2:
                    st["stopped"] = f"连续 {st['consecutive']} 批失败（疑似 AI 服务不可用）"
                continue
            st["consecutive"] = 0
            for ids in groups:
                members = [positions[i - 1] for i in ids]
                head = min(members)                     # 保留最早出现的条目为主条目
                ordered = sorted(members)
                outputs[head] = self._merge_group([reps[m] for m in ordered])
                for m in ordered[1:]:
                    outputs[m] = None                   # 已并入主条目

        return assemble(outputs)

    def _dedup_batch_groups(self, batch: List[Dict]) -> Optional[List[List[int]]]:
        """单批判定：缓存 → AI 调用 → JSON 解析与约束校验；失败返回 None。"""
        numbered = "\n".join(
            f"[{i}] {_sanitize_title(t.get('title', ''))}" for i, t in enumerate(batch, 1)
        )
        cache_kind = getattr(self, "cache_kind", _CACHE_KIND)

        cached = self.cache.get(cache_kind, numbered)
        if cached:
            groups = self._parse_groups(cached, len(batch))
            if groups is not None:
                return groups

        user_prompt = (
            self.user_prompt_template
            .replace("{count}", str(len(batch)))
            .replace("{max_group_size}", str(getattr(self, "max_group_size", _DEFAULT_MAX_GROUP_SIZE)))
            .replace("{titles}", numbered)
        )
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        # 截断（finish_reason=length）由 client 抛 AIResponseTruncated → 本批按失败处理
        response = self.client.chat(messages)
        groups = self._parse_groups(response, len(batch))
        if groups is None:
            log.info("[语义去重] 本批响应解析失败，保留原样")
            return None

        # 判定结果写入缓存（存规范 JSON，取回时按同一套解析校验）
        self.cache.put(cache_kind, numbered, json.dumps({"groups": groups}, ensure_ascii=False))
        return groups

    def _dedup_batch(self, batch: List[Dict]) -> List[Dict]:
        """兼容旧接口：单批判定并合并；失败返回原批。"""
        groups = self._dedup_batch_groups(batch)
        if groups is None:
            return list(batch)
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
        rejected_oversize = False
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
            if not ids:
                continue
            max_size = getattr(self, "max_group_size", _DEFAULT_MAX_GROUP_SIZE)
            if len(ids) > max_size:
                # 超大组不可信（整批被"吞掉"的典型形态，含提示词注入）：拆回单条
                log.info(f"[语义去重] 忽略超大分组（{len(ids)} 条 > 上限 {max_size}），拆回单条")
                for i in ids:
                    seen.discard(i)
                rejected_oversize = True
                continue
            valid_groups.append(ids)

        if not valid_groups and not rejected_oversize:
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

        # ranks：只合并与主条目同一来源（同一榜单）的排名；不同平台的排名不可比，
        # 混在一起会出现"排名:1-30"这类无意义数据（审计 #23），改记入 merged_sources
        primary_source = group[0].get("source_name", "")
        same_source = [t for t in group if t.get("source_name", "") == primary_source]
        ranks = []
        for t in same_source:
            for r in (t.get("ranks") or []):
                if r not in ranks:
                    ranks.append(r)
        try:
            ranks.sort()
        except TypeError:
            pass
        primary["ranks"] = ranks
        primary["merged_sources"] = [
            {
                "source_name": t.get("source_name", ""),
                "title": t.get("title", ""),
                "ranks": list(t.get("ranks") or []),
            }
            for t in group
        ]

        # url / mobile_url / time_display 取首个非空
        for key in ("url", "mobile_url", "time_display"):
            if not primary.get(key):
                for t in group[1:]:
                    if t.get(key):
                        primary[key] = t[key]
                        break

        # is_new 任一为真
        primary["is_new"] = any(t.get("is_new") for t in group)

        # rank_timeline：同样只合并同一来源（按时间排序去重）
        timeline = []
        for t in same_source:
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
