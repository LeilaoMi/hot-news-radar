# coding=utf-8
"""进程级 AI 时间预算与跨功能熔断。

动机：分析 / 翻译 / 去重 / 筛选各自有预算时，AI 服务挂起的 worst-case 会叠加
（审计实测约 18 分钟/进程，daily 补跑再来一遍），超过 30 分钟 cron 间隔后
被 cancel-in-progress 取消，整轮零发布。本模块给**整个进程**一个共享的 AI
截止时间，所有经过 ``AIClient`` 的调用共用：

- 总预算：env ``AI_TOTAL_BUDGET_SECONDS`` > yaml ``ai.total_budget_seconds`` > 480 秒，
  从本进程第一次 AI 调用开始计时；<=0 表示不限制。
- 剩余预算小于 ``min_request_seconds``（env ``AI_MIN_REQUEST_SECONDS`` > yaml
  ``ai.min_request_seconds`` > 15 秒）时不再发起新请求；每次请求的 timeout
  被压到 ``min(timeout, 剩余预算 / 尝试次数)``，单次调用不会越过截止时间。
- 跨功能熔断：连续 ``circuit_breaker_threshold``（env ``AI_CIRCUIT_BREAKER_THRESHOLD``
  > yaml > 3）次调用异常（超时 / 连接失败 / 5xx 等）后，本进程剩余 AI 功能全部跳过。

预算耗尽或熔断时 ``AIClient.chat`` 抛 ``AIBudgetExhausted``，各功能按"失败、保留原文"
降级，主流程照常发布。
"""

import threading
import time
from typing import Optional

from trendradar.ai import settings
from trendradar.core.logger import get_logger

log = get_logger(__name__)


class AIBudgetExhausted(RuntimeError):
    """AI 全局时间预算耗尽或熔断打开，本次调用未发起。"""


def _num(env_key: str, yaml_key: str, default: float) -> float:
    v = settings.env_float(env_key)
    if v is None:
        try:
            raw = settings.raw_section("ai").get(yaml_key)
            v = float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            v = None
    return default if v is None else v


class AIBudget:
    def __init__(
        self,
        total_seconds: Optional[float] = None,
        min_request_seconds: Optional[float] = None,
        circuit_threshold: Optional[int] = None,
    ):
        self.total_seconds = (
            total_seconds if total_seconds is not None
            else _num("AI_TOTAL_BUDGET_SECONDS", "total_budget_seconds", settings.DEFAULT_TOTAL_BUDGET_SECONDS)
        )
        self.min_request_seconds = (
            min_request_seconds if min_request_seconds is not None
            else _num("AI_MIN_REQUEST_SECONDS", "min_request_seconds", 15.0)
        )
        self.circuit_threshold = int(
            circuit_threshold if circuit_threshold is not None
            else _num("AI_CIRCUIT_BREAKER_THRESHOLD", "circuit_breaker_threshold", 3)
        )
        self._lock = threading.Lock()
        self._start: Optional[float] = None
        self._consecutive_failures = 0
        self.circuit_open = False
        self._exhausted_logged = False

    # --- 计时 ---
    def _ensure_started(self) -> None:
        if self._start is None:
            self._start = time.monotonic()
            if self.total_seconds > 0:
                log.info(f"[AI预算] 本进程 AI 总预算 {self.total_seconds:.0f}s（开始计时）")

    def elapsed(self) -> float:
        return 0.0 if self._start is None else time.monotonic() - self._start

    def remaining(self) -> float:
        if self.total_seconds <= 0:
            return float("inf")
        return self.total_seconds - self.elapsed()

    def exhausted(self) -> bool:
        if self.circuit_open:
            return True
        if self.total_seconds <= 0 or self._start is None:
            return False
        return self.remaining() < self.min_request_seconds

    def acquire(self, timeout: float, attempts: int = 1) -> float:
        """发起请求前调用：返回本次可用的 timeout；预算不足 / 熔断时抛 AIBudgetExhausted。"""
        with self._lock:
            self._ensure_started()
            if self.circuit_open:
                raise AIBudgetExhausted("AI 熔断已打开（连续调用失败，疑似服务不可用），跳过本次调用")
            remaining = self.remaining()
            if remaining < self.min_request_seconds:
                if not self._exhausted_logged:
                    self._exhausted_logged = True
                    log.warning(
                        f"[AI预算] 本进程 AI 总预算 {self.total_seconds:.0f}s 已耗尽"
                        f"（已用 {self.elapsed():.0f}s），跳过剩余 AI 调用"
                    )
                raise AIBudgetExhausted(
                    f"AI 总预算耗尽（已用 {self.elapsed():.0f}s / {self.total_seconds:.0f}s）"
                )
            attempts = max(1, int(attempts))
            try:
                timeout = float(timeout)
            except (TypeError, ValueError):
                timeout = 60.0
            if remaining != float("inf"):
                timeout = min(timeout, remaining / attempts)
            return max(1.0, timeout)

    # --- 熔断 ---
    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self, exc: BaseException) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if (self.circuit_threshold > 0 and not self.circuit_open
                    and self._consecutive_failures >= self.circuit_threshold):
                self.circuit_open = True
                log.error(
                    f"[AI预算] 连续 {self._consecutive_failures} 次 AI 调用失败"
                    f"（最后一次: {type(exc).__name__}），熔断本进程剩余 AI 功能"
                )


_budget: Optional[AIBudget] = None
_budget_lock = threading.Lock()


def get_budget() -> AIBudget:
    global _budget
    with _budget_lock:
        if _budget is None:
            _budget = AIBudget()
        return _budget


def reset_budget(budget: Optional[AIBudget] = None) -> AIBudget:
    """测试用：替换/重建进程级预算。"""
    global _budget
    with _budget_lock:
        _budget = budget if budget is not None else AIBudget()
        return _budget
