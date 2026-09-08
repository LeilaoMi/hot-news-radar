# coding=utf-8
"""统一日志模块。

项目此前 600+ 处 print() 分散在各模块：无级别、无时间戳、无法按级别
检索（Actions 排障「AI 待配置」事故的定位成本即由此放大）。本模块提供
轻量封装，逐步收敛 hot path 的输出：

- ``get_logger(name)``：返回 trendradar 命名空间下的 logger。
- ``log_exception(log, message, exc)``：统一错误输出（单行 类型+消息，
  DEBUG 时附完整堆栈），替代散落各处的 ``except Exception as e: print(e)``。

设计约束
--------
- 仅用标准库，不引入第三方依赖。
- INFO 级别输出与原 print 完全一致（无前缀），对现有用户与日志解析零影响；
  WARNING/ERROR 分别加 ``WARN: `` / ``ERROR: `` 前缀，便于 ``grep ERROR`` 定位。
- 不在 import 时做文件 IO 或复杂配置（CLI 启动速度敏感，参见
  analysis_service 顶部关于 litellm 延迟导入的注释）。
- 全局仅挂载一次 handler，重复调用 get_logger 幂等。
"""
import logging
import os
import sys

_CONFIGURED = False

# 模块级缓存 DEBUG 开关判定，避免热路径反复读环境变量
_DEBUG_ENABLED: bool = os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes")


class _TrendRadarFormatter(logging.Formatter):
    """INFO 与 print 输出保持一致；WARN/ERROR 加级别前缀便于检索。"""

    def format(self, record: logging.LogRecord) -> str:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式化失败不能拖垮业务流程
            msg = str(record.msg)
        if record.levelno >= logging.ERROR:
            return f"ERROR: {msg}"
        if record.levelno >= logging.WARNING:
            return f"WARN: {msg}"
        return msg


def _ensure_configured() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger("trendradar")
    root.setLevel(logging.DEBUG if _DEBUG_ENABLED else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_TrendRadarFormatter())
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str = "trendradar") -> logging.Logger:
    """获取 trendradar 命名空间下的 logger。

    传入模块 ``__name__`` 时自动归一到 ``trendradar.*`` 前缀下，
    保证统一走同一个 handler 配置。
    """
    _ensure_configured()
    if name == "trendradar" or name.startswith("trendradar."):
        return logging.getLogger(name)
    return logging.getLogger(f"trendradar.{name}")


def debug_enabled() -> bool:
    """是否开启调试模式（环境变量 DEBUG=1/true/yes）。"""
    return _DEBUG_ENABLED


def log_exception(log: logging.Logger, message: str, exc: BaseException) -> None:
    """统一的异常输出：单行「类型+消息」，DEBUG 模式追加完整堆栈。

    替代 ``except Exception as e: print(f"xxx: {e}")`` 模式——
    后者丢失异常类型，且无法按级别检索。
    """
    log.error("%s (%s): %s", message, type(exc).__name__, exc)
    if _DEBUG_ENABLED:
        import traceback

        log.error(traceback.format_exc())
