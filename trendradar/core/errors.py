# coding=utf-8
"""运行期错误类型与进程退出码。

退出码约定（供 CI 判断）：
- 0：成功（允许部分平台失败，只打印 warning）
- 1：致命失败（热榜全部失败、存储失败、报告渲染失败、未处理异常、配置错误、
     RENDER_ONLY 时存储中没有可渲染的数据……）
- 2：部分失败超过可配置阈值（PLATFORM_FAILURE_THRESHOLD / advanced.crawler.failure_threshold）
"""

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 2


class TrendRadarError(RuntimeError):
    """项目内可预期的运行期错误基类。"""


class StorageError(TrendRadarError):
    """存储后端读写失败（数据未能落盘 / 上传）。"""


class ReportError(TrendRadarError):
    """报告生成 / 渲染失败。"""


class NoDataError(TrendRadarError):
    """没有可用于生成报告的数据（例如 RENDER_ONLY 但存储为空）。"""
