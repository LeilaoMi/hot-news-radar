# coding=utf-8
"""
AI 客户端模块

基于 LiteLLM 的统一 AI 模型接口
支持 100+ AI 提供商（OpenAI、DeepSeek、Gemini、Claude、国内模型等）
"""

import os
from dataclasses import dataclass
from typing import Any, Dict, List

from trendradar.ai.budget import AIBudgetExhausted, get_budget
from trendradar.core.logger import get_logger

log = get_logger(__name__)


class AIResponseTruncated(RuntimeError):
    """模型输出因 max_tokens 被截断（finish_reason == "length"）。

    截断的 JSON 经 json_repair 也能"修好"，若当作成功会把残缺内容推送出去，
    所以统一按失败处理（调用方可选择精简后重试）。``content`` 保留残缺输出供调试。
    """

    def __init__(self, content: str = "", finish_reason: str = "length"):
        super().__init__(f"AI 输出被截断（finish_reason={finish_reason}），请调大 max_tokens 或减小批大小")
        self.content = content
        self.finish_reason = finish_reason


@dataclass
class ChatResponse:
    content: str = ""
    finish_reason: str = ""
    reasoning_content: str = ""


def _content_to_text(content: Any) -> str:
    # 某些模型/提供商返回 list（内容块）而非 str，统一转为 str
    if isinstance(content, list):
        return "\n".join(
            item.get("text", str(item)) if isinstance(item, dict) else str(item)
            for item in content
        )
    if content is None:
        return ""
    return content if isinstance(content, str) else str(content)


class AIClient:
    """统一的 AI 客户端（基于 LiteLLM）

    所有调用都经过进程级 AI 预算（见 ``trendradar.ai.budget``）：预算耗尽或熔断时
    抛 ``AIBudgetExhausted``；``finish_reason == "length"`` 时抛 ``AIResponseTruncated``。
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化 AI 客户端

        Args:
            config: AI 配置字典
                - MODEL: 模型标识（格式: provider/model_name）
                - API_KEY: API 密钥
                - API_BASE: API 基础 URL（可选）
                - TEMPERATURE: 采样温度
                - MAX_TOKENS: 最大生成 token 数
                - TIMEOUT: 单次请求超时时间（秒）
                - NUM_RETRIES: 重试次数（可选，默认 0：一次逻辑调用只发 1 次 HTTP 请求）
                - FALLBACK_MODELS: 备用模型列表（可选）
                - EXTRA_PARAMS: 透传给 litellm 的额外参数（可选，如 top_p）
        """
        self.model = config.get("MODEL", "deepseek/deepseek-chat")
        self.api_key = config.get("API_KEY") or os.environ.get("AI_API_KEY", "")
        self.api_base = config.get("API_BASE", "")
        self.temperature = config.get("TEMPERATURE", 1.0)
        self.max_tokens = config.get("MAX_TOKENS", 5000)
        self.timeout = config.get("TIMEOUT", 60)
        # litellm 语义：num_retries=N 时 SDK 层与 wrapper 层都会重试，一次逻辑调用
        # 最多约 (N+2) 次 HTTP 尝试；默认 0 保证单次尝试，耗时可预测
        self.num_retries = config.get("NUM_RETRIES", 0)
        self.fallback_models = config.get("FALLBACK_MODELS", []) or []
        extra = config.get("EXTRA_PARAMS") or {}
        self.extra_params = dict(extra) if isinstance(extra, dict) else {}
        self.feature = config.get("FEATURE", "")

    def _attempts(self, num_retries: Any) -> int:
        try:
            n = int(num_retries or 0)
        except (TypeError, ValueError):
            n = 0
        attempts = 1 if n <= 0 else n + 2
        return attempts * (1 + len(self.fallback_models))

    def chat_ex(self, messages: List[Dict[str, str]], **kwargs) -> ChatResponse:
        """调用 AI 模型，返回内容与 finish_reason（不因截断抛异常）。

        Raises:
            AIBudgetExhausted: 全局预算耗尽或熔断
            Exception: API 调用失败
        """
        num_retries = kwargs.get("num_retries", self.num_retries)
        budget = get_budget()
        timeout = budget.acquire(kwargs.get("timeout", self.timeout), self._attempts(num_retries))

        # 构建请求参数：extra_params < 默认参数 < 调用方 kwargs
        params: Dict[str, Any] = dict(self.extra_params)
        params.update({
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "timeout": timeout,
            "num_retries": num_retries,
        })

        if self.api_key:
            params["api_key"] = self.api_key
        if self.api_base:
            params["api_base"] = self.api_base

        # 添加 max_tokens（如果配置了且不为 0）
        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        if max_tokens and max_tokens > 0:
            params["max_tokens"] = max_tokens

        if self.fallback_models:
            params["fallbacks"] = self.fallback_models

        for key, value in kwargs.items():
            if key not in ("temperature", "timeout", "num_retries", "max_tokens"):
                params[key] = value

        # 调用 LiteLLM（延迟导入：litellm 导入耗时约 6.7 秒，未启用 AI 时不应付出该开销）
        from litellm import completion

        try:
            response = completion(**params)
        except AIBudgetExhausted:
            raise
        except Exception as e:
            budget.record_failure(e)
            raise
        budget.record_success()

        choice = response.choices[0]
        message = choice.message
        content = _content_to_text(getattr(message, "content", None))
        finish_reason = str(getattr(choice, "finish_reason", "") or "")
        reasoning = _content_to_text(getattr(message, "reasoning_content", None))
        if not content.strip():
            # 推理模型可能把 token 全花在思维链上，只返回 reasoning_content
            log.warning(
                f"[AI] {self.feature or 'AI'} 返回空 content（finish_reason={finish_reason or '-'}"
                f"{', 有 reasoning_content ' + str(len(reasoning)) + ' 字符' if reasoning else ''}）；"
                "推理模型请关闭 thinking 或调大 max_tokens/timeout"
            )
        return ChatResponse(content=content, finish_reason=finish_reason, reasoning_content=reasoning)

    def chat(
        self,
        messages: List[Dict[str, str]],
        **kwargs
    ) -> str:
        """
        调用 AI 模型进行对话

        Args:
            messages: 消息列表，格式: [{"role": "system/user/assistant", "content": "..."}]
            **kwargs: 额外参数，会覆盖默认配置

        Returns:
            str: AI 响应内容

        Raises:
            AIBudgetExhausted: 全局预算耗尽或熔断
            AIResponseTruncated: 输出被 max_tokens 截断（finish_reason == "length"）
            Exception: API 调用失败时抛出异常
        """
        resp = self.chat_ex(messages, **kwargs)
        if resp.finish_reason == "length":
            log.warning(f"[AI] {self.feature or 'AI'} 输出被截断（finish_reason=length），按失败处理")
            raise AIResponseTruncated(resp.content, resp.finish_reason)
        return resp.content

    def validate_config(self) -> tuple[bool, str]:
        """
        验证配置是否有效

        Returns:
            tuple: (是否有效, 错误信息)
        """
        if not self.model:
            return False, "未配置 AI 模型（model）"

        if not self.api_key:
            return False, "未配置 AI API Key，请在 config.yaml 或环境变量 AI_API_KEY 中设置"

        # 验证模型格式（应该包含 provider/model）
        if "/" not in self.model:
            return False, f"模型格式错误: {self.model}，应为 'provider/model' 格式（如 'deepseek/deepseek-chat'）"

        return True, ""
