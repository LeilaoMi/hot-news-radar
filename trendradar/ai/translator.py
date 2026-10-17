# coding=utf-8
"""
AI 翻译器模块

对推送内容进行多语言翻译
基于 LiteLLM 统一接口，支持 100+ AI 提供商

对齐安全（审计 #2/#15）：
- 批量翻译要求模型输出 ``[n] 译文`` 编号格式（兼容 ``【n】``、``n.``、``n、``、``n)`` 等变体）；
- 编号缺失、重复、越界或条数不符时**整批作废**（不做按行顺序回退），按二分拆批重试，
  最终落到单条请求；单条请求无编号时仅在恰好一行输出时接受；
- 截断（finish_reason=length）同样作废并拆批重试；
- 只有通过校验的译文才写缓存；缓存 kind 带目标语言、模型与提示词 hash，过期由 AICache TTL 控制。
"""

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from trendradar.ai import settings
from trendradar.ai.budget import AIBudgetExhausted, get_budget
from trendradar.ai.cache import AICache
from trendradar.ai.client import AIClient
from trendradar.ai.prompt_loader import load_prompt_template
from trendradar.core.logger import get_logger

log = get_logger(__name__)

# 编号行：[1] / 【1】 / 1. / 1、 / 1) / 1） / 1: / 1：，其后可带 : ： . - 等分隔符
_NUMBERED_LINE = re.compile(
    r"^\s*(?:\[\s*(\d+)\s*\]|【\s*(\d+)\s*】|(\d+)\s*[\.、\)）:：])\s*[:：\.\-、]?\s*(.*)$"
)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _sanitize_title(text: str) -> str:
    """送入批量 prompt 前清洗标题：换行/控制字符折叠为空格，防止注入 ``[k]`` 行篡改其它条目。"""
    s = _CONTROL_CHARS.sub(" ", str(text).replace("\r", " ").replace("\n", " "))
    return re.sub(r"\s{2,}", " ", s).strip()


@dataclass
class TranslationResult:
    """翻译结果"""
    translated_text: str = ""       # 翻译后的文本
    original_text: str = ""         # 原始文本
    success: bool = False           # 是否成功
    error: str = ""                 # 错误信息


@dataclass
class BatchTranslationResult:
    """批量翻译结果"""
    results: List[TranslationResult] = field(default_factory=list)
    success_count: int = 0
    fail_count: int = 0
    total_count: int = 0
    prompt: str = ""                # debug: 发送给 AI 的完整 prompt
    raw_response: str = ""          # debug: AI 原始响应
    parsed_count: int = 0           # debug: AI 响应解析出的条目数


class AITranslator:
    """AI 翻译器"""

    def __init__(self, translation_config: Dict[str, Any], ai_config: Dict[str, Any]):
        """
        初始化 AI 翻译器

        Args:
            translation_config: AI 翻译配置 (AI_TRANSLATION)
            ai_config: AI 模型配置（LiteLLM 格式）
        """
        self.translation_config = translation_config
        self.ai_config = ai_config

        # 翻译配置
        self.enabled = translation_config.get("ENABLED", False)
        self.target_language = translation_config.get("LANGUAGE", "English")
        self.scope = translation_config.get("SCOPE", {"HOTLIST": True, "RSS": True, "STANDALONE": True})

        # loader 曾漏读 ai_translation 的 batch_size / batch_interval / cache_enabled（审计 #14）：
        # 这里从 yaml 补齐并**写回同一个配置字典**，调用方（dispatcher 按 BATCH_SIZE 分批）
        # 无需改动即可生效；env AI_TRANSLATION_BATCH_SIZE / _BATCH_INTERVAL / _CACHE_ENABLED 优先
        self._fill_missing_config(translation_config)
        self.batch_size = max(1, int(translation_config.get("BATCH_SIZE", 100) or 100))
        self.batch_interval = max(0.0, float(translation_config.get("BATCH_INTERVAL", 2) or 0))
        # 对齐失败时的二分重试深度（2 = 一批最多拆成 4 份，额外请求 ≤ 6 次；0 = 不重试直接保留原文）
        split_raw = settings.env_str("AI_TRANSLATION_MAX_SPLIT_DEPTH") or settings.raw_section("ai_translation").get("max_split_depth", 2)
        try:
            self.max_split_depth = max(0, int(split_raw))
        except (TypeError, ValueError):
            self.max_split_depth = 2

        # 按功能解析模型/温度/端点（env AI_TRANSLATION_MODEL > env AI_MODEL > yaml ai.translation_model > ai.model）
        ai_config = settings.feature_ai_config(ai_config, "translation")
        self.ai_config = ai_config

        # 创建 AI 客户端（基于 LiteLLM）
        self.client = AIClient(ai_config)

        # 加载提示词模板
        self.system_prompt, self.user_prompt_template = load_prompt_template(
            translation_config.get("PROMPT_FILE", "ai_translation_prompt.txt"),
            label="翻译",
        )

        # AI 结果缓存：同一标题跨批次/daily 补跑零成本复用。
        # 开关：ai_translation.cache_enabled（默认开），env AI_CACHE_ENABLED=false 为总闸；
        # 路径：env AI_CACHE_DB > yaml ai.cache_db_path > output/ai_cache.db
        self.cache = AICache(
            db_path=ai_config.get("CACHE_DB_PATH"),
            enabled=translation_config.get("CACHE_ENABLED", True),
        )
        prompt_hash = hashlib.sha256(
            f"{self.system_prompt}\x00{self.user_prompt_template}".encode("utf-8")
        ).hexdigest()[:10]
        self.cache_kind = f"translate:{self.target_language}:{self.client.model}:{prompt_hash}"

    @staticmethod
    def _fill_missing_config(translation_config: Dict[str, Any]) -> None:
        raw = settings.raw_section("ai_translation")
        specs = (
            ("BATCH_SIZE", "batch_size", int),
            ("BATCH_INTERVAL", "batch_interval", float),
            ("CACHE_ENABLED", "cache_enabled", settings.parse_bool),
        )
        for key, yaml_key, cast in specs:
            env_raw = settings.env_str(f"AI_TRANSLATION_{key}")
            value = None
            if env_raw:
                try:
                    value = cast(env_raw)
                except (TypeError, ValueError):
                    log.warning(f"[翻译] 环境变量 AI_TRANSLATION_{key}={env_raw!r} 无效，已忽略")
            if value is None and key not in translation_config and raw.get(yaml_key) is not None:
                try:
                    value = cast(raw.get(yaml_key))
                except (TypeError, ValueError):
                    value = None
            if value is not None:
                translation_config[key] = value

    def _get_cache_kind(self) -> str:
        return getattr(self, "cache_kind", None) or f"translate:{self.target_language}"

    @staticmethod
    def _plausible(original: str, translated: str) -> bool:
        """译文健全性：非空、长度不过分离谱、不残留编号前缀。"""
        if not translated or not translated.strip():
            return False
        if len(translated) > max(200, 8 * len(original)):
            return False
        if _NUMBERED_LINE.match(translated) and not _NUMBERED_LINE.match(original):
            return False
        return True

    def translate(self, text: str) -> TranslationResult:
        """
        翻译单条文本

        Args:
            text: 要翻译的文本

        Returns:
            TranslationResult: 翻译结果
        """
        result = TranslationResult(original_text=text)

        if not self.enabled:
            result.error = "翻译功能未启用"
            return result

        if not self.client.api_key:
            result.error = "未配置 AI API Key"
            return result

        if not text or not text.strip():
            result.translated_text = text
            result.success = True
            return result

        # 单条也走批量路径：同一套编号对齐校验、缓存与预算逻辑
        batch = self.translate_batch([text])
        if batch.results:
            return batch.results[0]
        result.error = "翻译失败"
        return result

    def translate_batch(self, texts: List[str]) -> BatchTranslationResult:
        """
        批量翻译文本（单次 API 调用）

        Args:
            texts: 要翻译的文本列表

        Returns:
            BatchTranslationResult: 批量翻译结果
        """
        batch_result = BatchTranslationResult(total_count=len(texts))

        if not self.enabled:
            for text in texts:
                batch_result.results.append(TranslationResult(
                    original_text=text,
                    error="翻译功能未启用"
                ))
            batch_result.fail_count = len(texts)
            return batch_result

        if not self.client.api_key:
            for text in texts:
                batch_result.results.append(TranslationResult(
                    original_text=text,
                    error="未配置 AI API Key"
                ))
            batch_result.fail_count = len(texts)
            return batch_result

        if not texts:
            return batch_result

        # 过滤空文本
        non_empty_indices = []
        non_empty_texts = []
        for i, text in enumerate(texts):
            if text and text.strip():
                non_empty_indices.append(i)
                non_empty_texts.append(text)

        # 初始化结果列表
        for text in texts:
            batch_result.results.append(TranslationResult(original_text=text))

        # 空文本直接标记成功
        for i, text in enumerate(texts):
            if not text or not text.strip():
                batch_result.results[i].translated_text = text
                batch_result.results[i].success = True
                batch_result.success_count += 1

        if not non_empty_texts:
            return batch_result

        # ── 缓存命中分流：仅将未命中的标题送去 API（kind 含目标语言/模型/提示词隔离）──
        cache_kind = self._get_cache_kind()
        to_translate_indices = list(non_empty_indices)
        to_translate_texts = list(non_empty_texts)
        if self.cache.enabled:
            to_translate_indices, to_translate_texts = [], []
            for i, text in zip(non_empty_indices, non_empty_texts):
                cached = self.cache.get(cache_kind, text)
                if cached is not None:
                    batch_result.results[i].translated_text = cached
                    batch_result.results[i].success = True
                    batch_result.success_count += 1
                else:
                    to_translate_indices.append(i)
                    to_translate_texts.append(text)
            cached_count = len(non_empty_texts) - len(to_translate_texts)
            if cached_count:
                log.info(
                    f"[AI翻译] 缓存命中 {cached_count}/{len(non_empty_texts)} 条，"
                    f"实际请求 {len(to_translate_texts)} 条"
                )

        if not to_translate_texts:
            return batch_result

        batch_size = max(1, int(getattr(self, "batch_size", 100) or 100))
        interval = float(getattr(self, "batch_interval", 0) or 0)
        pending = list(zip(to_translate_indices, to_translate_texts))
        chunks = [pending[k:k + batch_size] for k in range(0, len(pending), batch_size)]
        for chunk_no, chunk in enumerate(chunks):
            if chunk_no > 0 and interval > 0:
                time.sleep(interval)
            if get_budget().exhausted():
                self._mark_failed(batch_result, [i for i, _ in chunk], "AI 时间预算耗尽，保留原文")
                continue
            self._translate_chunk(batch_result, chunk, cache_kind, depth=0)

        return batch_result

    def _mark_failed(self, batch_result: BatchTranslationResult, indices: List[int], error: str) -> None:
        for idx in indices:
            r = batch_result.results[idx]
            if not r.success:
                r.error = error
                r.translated_text = r.translated_text or ""
                batch_result.fail_count += 1

    def _translate_chunk(self, batch_result, chunk, cache_kind: str, depth: int) -> None:
        """翻译一组 (原始位置, 原文)；对齐校验失败时二分拆批重试，失败条目保留原文、不写缓存。"""
        indices = [i for i, _ in chunk]
        texts = [t for _, t in chunk]
        try:
            batch_content = self._format_batch_content(texts)
            user_prompt = self.user_prompt_template
            user_prompt = user_prompt.replace("{target_language}", self.target_language)
            user_prompt = user_prompt.replace("{content}", batch_content)

            if not batch_result.prompt:
                if self.system_prompt:
                    batch_result.prompt = f"[system]\n{self.system_prompt}\n\n[user]\n{user_prompt}"
                else:
                    batch_result.prompt = user_prompt

            response = self._call_ai(user_prompt) or ""
            batch_result.raw_response = (
                response if not batch_result.raw_response
                else batch_result.raw_response + "\n---\n" + response
            )
            translated, parsed_count = self._parse_batch_response(response, len(texts))
            batch_result.parsed_count += parsed_count
            reason = "" if translated is not None else f"编号对齐校验失败（期望 {len(texts)} 条，解析 {parsed_count} 条）"
        except AIBudgetExhausted as e:
            self._mark_failed(batch_result, indices, f"批量翻译跳过: {e}")
            return
        except Exception as e:
            error_msg = f"批量翻译失败: {type(e).__name__}: {str(e)[:200]}"
            # 必须打出来：此前异常被静默吞掉，CI 日志里只有 litellm 的
            # 尾部提示，看不到真实病因（429/401/连接超时无法区分）
            log.error(error_msg)
            from trendradar.ai.client import AIResponseTruncated
            if isinstance(e, AIResponseTruncated) and len(chunk) > 1:
                translated, reason = None, "输出被截断"
            else:
                self._mark_failed(batch_result, indices, error_msg)
                return

        if translated is None:
            if len(chunk) > 1 and depth < getattr(self, "max_split_depth", 2) and not get_budget().exhausted():
                log.warning(f"[AI翻译] {reason}，拆分为两批重试（{len(chunk)} 条）")
                mid = len(chunk) // 2
                self._translate_chunk(batch_result, chunk[:mid], cache_kind, depth + 1)
                self._translate_chunk(batch_result, chunk[mid:], cache_kind, depth + 1)
                return
            log.warning(f"[AI翻译] {reason}，本批 {len(chunk)} 条保留原文（不写缓存）")
            self._mark_failed(batch_result, indices, f"翻译结果无法对齐: {reason}")
            return

        for idx, original, text in zip(indices, texts, translated):
            r = batch_result.results[idx]
            if self._plausible(original, text):
                r.translated_text = text
                r.success = True
                batch_result.success_count += 1
                if text != original:
                    self.cache.put(cache_kind, original, text)
            else:
                # 单条译文不可信：保留原文，按"未翻译"处理，不写缓存
                r.translated_text = original
                r.success = True
                batch_result.success_count += 1

    def _format_batch_content(self, texts: List[str]) -> str:
        """格式化批量翻译内容（标题先清洗换行，杜绝伪造编号行）"""
        return "\n".join(f"[{i}] {_sanitize_title(text)}" for i, text in enumerate(texts, 1))

    def _parse_batch_response(self, response: str, expected_count: int) -> tuple:
        """
        解析批量翻译响应（严格对齐）

        Returns:
            tuple: (译文列表 或 None, 解析出的条目数)。
            None 表示整批不可信（编号缺失/重复/越界/条数不符，或无编号且无法安全对齐），
            调用方应丢弃并重试，**不得**按行顺序回退。
        """
        lines = [ln.strip() for ln in (response or "").strip().split("\n")]
        entries: Dict[int, str] = {}
        duplicate = False
        out_of_range = False
        parsed = 0
        for line in lines:
            if not line:
                continue
            m = _NUMBERED_LINE.match(line)
            if not m:
                continue  # 前导语/结束语/多余说明：忽略（不参与对齐）
            idx = int(m.group(1) or m.group(2) or m.group(3))
            text = m.group(4).strip()
            parsed += 1
            if not 1 <= idx <= expected_count:
                out_of_range = True
                continue
            if idx in entries:
                duplicate = True
                continue
            entries[idx] = text

        if parsed == 0:
            # 无任何编号：仅单条请求且恰好一行非空输出时可安全接受
            non_empty = [ln for ln in lines if ln]
            if expected_count == 1 and len(non_empty) == 1:
                return [non_empty[0]], 1
            return None, 0

        if duplicate or out_of_range or len(entries) != expected_count:
            return None, parsed

        return [entries[i] for i in range(1, expected_count + 1)], parsed

    def _call_ai(self, user_prompt: str) -> str:
        """调用 AI API（使用 LiteLLM）"""
        messages = []
        if self.system_prompt:
            # system 提示词同样含 {target_language} 占位符（此前未替换，模型看到的是字面量）
            messages.append({
                "role": "system",
                "content": self.system_prompt.replace("{target_language}", self.target_language),
            })
        messages.append({"role": "user", "content": user_prompt})

        return self.client.chat(messages)
