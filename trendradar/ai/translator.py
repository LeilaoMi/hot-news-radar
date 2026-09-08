# coding=utf-8
"""
AI 翻译器模块

对推送内容进行多语言翻译
基于 LiteLLM 统一接口，支持 100+ AI 提供商
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from trendradar.ai.cache import AICache
from trendradar.ai.client import AIClient
from trendradar.ai.prompt_loader import load_prompt_template
from trendradar.core.logger import get_logger

log = get_logger(__name__)


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

        # 创建 AI 客户端（基于 LiteLLM）
        self.client = AIClient(ai_config)

        # AI 结果缓存：同一标题跨运行/跨批次/daily 补跑零成本复用。
        # 开关：ai_translation.CACHE_ENABLED（默认开）+ env AI_CACHE_ENABLED；
        # 路径：ai.CACHE_DB_PATH → env AI_CACHE_DB → output/ai_cache.db
        self.cache = AICache(
            db_path=ai_config.get("CACHE_DB_PATH"),
            enabled=translation_config.get("CACHE_ENABLED", True),
        )

        # 加载提示词模板
        self.system_prompt, self.user_prompt_template = load_prompt_template(
            translation_config.get("PROMPT_FILE", "ai_translation_prompt.txt"),
            label="翻译",
        )

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

        # 缓存命中直接返回
        cache_kind = f"translate:{self.target_language}"
        cached = self.cache.get(cache_kind, text)
        if cached is not None:
            result.translated_text = cached
            result.success = True
            return result

        try:
            # 构建提示词
            user_prompt = self.user_prompt_template
            user_prompt = user_prompt.replace("{target_language}", self.target_language)
            user_prompt = user_prompt.replace("{content}", text)

            # 调用 AI API
            response = self._call_ai(user_prompt)
            result.translated_text = response.strip()
            result.success = True
            self.cache.put(cache_kind, text, result.translated_text)

        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            if len(error_msg) > 100:
                error_msg = error_msg[:100] + "..."
            result.error = f"翻译失败 ({error_type}): {error_msg}"

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

        # ── 缓存命中分流：仅将未命中的标题送去 API（kind 含目标语言隔离）──
        cache_kind = f"translate:{self.target_language}"
        original_by_index = dict(zip(non_empty_indices, non_empty_texts))
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

        if to_translate_texts:
            try:
                # 构建批量翻译内容（使用编号格式，仅含未命中缓存的条目）
                batch_content = self._format_batch_content(to_translate_texts)

                # 构建提示词
                user_prompt = self.user_prompt_template
                user_prompt = user_prompt.replace("{target_language}", self.target_language)
                user_prompt = user_prompt.replace("{content}", batch_content)

                # 记录 debug 信息（包含完整的 system + user prompt）
                if self.system_prompt:
                    batch_result.prompt = f"[system]\n{self.system_prompt}\n\n[user]\n{user_prompt}"
                else:
                    batch_result.prompt = user_prompt

                # 调用 AI API
                response = self._call_ai(user_prompt)

                # 记录 AI 原始响应
                batch_result.raw_response = response

                # 解析批量翻译结果
                translated_texts, raw_parsed_count = self._parse_batch_response(response, len(to_translate_texts))
                batch_result.parsed_count = raw_parsed_count

                # 填充结果（跳过空翻译，避免用空字符串覆盖原始标题）
                for idx, translated in zip(to_translate_indices, translated_texts):
                    if translated and translated.strip():
                        batch_result.results[idx].translated_text = translated
                        batch_result.results[idx].success = True
                        batch_result.success_count += 1
                        # 成功译文写入缓存，下次零成本复用
                        self.cache.put(cache_kind, original_by_index[idx], translated)
                    else:
                        batch_result.results[idx].translated_text = batch_result.results[idx].original_text
                        batch_result.results[idx].success = True
                        batch_result.success_count += 1

            except Exception as e:
                error_msg = f"批量翻译失败: {type(e).__name__}: {str(e)[:100]}"
                # 仅未命中缓存的条目标记失败；缓存命中的已是成功态，不受影响
                for idx in to_translate_indices:
                    batch_result.results[idx].error = error_msg
                batch_result.fail_count = len(to_translate_indices)

        return batch_result

    def _format_batch_content(self, texts: List[str]) -> str:
        """格式化批量翻译内容"""
        lines = []
        for i, text in enumerate(texts, 1):
            lines.append(f"[{i}] {text}")
        return "\n".join(lines)

    def _parse_batch_response(self, response: str, expected_count: int) -> tuple:
        """
        解析批量翻译响应

        Args:
            response: AI 响应文本
            expected_count: 期望的翻译数量

        Returns:
            tuple: (翻译结果列表, AI 原始解析出的条目数)
        """
        results = []
        lines = response.strip().split("\n")

        current_idx = None
        current_text = []

        for line in lines:
            # 尝试匹配 [数字] 格式
            stripped = line.strip()
            if stripped.startswith("[") and "]" in stripped:
                bracket_end = stripped.index("]")
                try:
                    idx = int(stripped[1:bracket_end])
                    # 保存之前的内容
                    if current_idx is not None:
                        results.append((current_idx, "\n".join(current_text).strip()))
                    current_idx = idx
                    current_text = [stripped[bracket_end + 1:].strip()]
                except ValueError:
                    if current_idx is not None:
                        current_text.append(line)
            else:
                if current_idx is not None:
                    current_text.append(line)

        # 保存最后一条
        if current_idx is not None:
            results.append((current_idx, "\n".join(current_text).strip()))

        # 基于 AI 返回的真实编号精确回填，而非位置顺序，避免 AI 漏号/乱序时整体错位
        raw_parsed_count = len(results)

        if results:
            # 编号 i(1-based) 对应位置 i-1；缺失的编号位置留空（由上层保留原文），
            # 不让后续译文顶替到相邻标题上
            idx_to_text = {}
            for idx, text in results:
                if 1 <= idx <= expected_count:
                    idx_to_text[idx] = text
            translated = [idx_to_text.get(i + 1, "") for i in range(expected_count)]
        else:
            # AI 未使用 [编号] 格式：回退为按行顺序提取
            translated = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("[") and "]" in stripped:
                    bracket_end = stripped.index("]")
                    translated.append(stripped[bracket_end + 1:].strip())
                elif stripped:
                    translated.append(stripped)
            raw_parsed_count = len(translated)

        # 确保返回正确数量
        while len(translated) < expected_count:
            translated.append("")

        return translated[:expected_count], raw_parsed_count

    def _call_ai(self, user_prompt: str) -> str:
        """调用 AI API（使用 LiteLLM）"""
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        return self.client.chat(messages)
