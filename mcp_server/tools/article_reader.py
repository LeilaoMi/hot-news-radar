"""
文章内容读取工具

通过 Jina AI Reader API 将 URL 转换为 LLM 友好的 Markdown 格式。
支持单篇和批量读取，内置速率限制和并发控制。

"""

import os
import threading
import time
from typing import Dict, List
from urllib.parse import quote, urlsplit

import requests

from ..utils.errors import MCPError, InvalidParameterError
from ..utils.redact import safe_error


# Jina Reader 配置
JINA_READER_BASE = "https://r.jina.ai"
DEFAULT_TIMEOUT = 30  # 秒
MAX_BATCH_SIZE = 5  # 单次批量最大篇数
MAX_TIMEOUT = 60  # 单篇超时上限（秒）
BATCH_TOTAL_BUDGET = 120.0  # 批量读取总耗时上限（秒），避免 MCP 客户端先超时
BATCH_INTERVAL = 5.0  # 批量请求间隔（秒）


class ArticleReaderTools:
    """文章内容读取工具类"""

    def __init__(self, project_root: str = None, jina_api_key: str = None):
        """
        初始化文章读取工具

        Args:
            project_root: 项目根目录
            jina_api_key: Jina API Key（可选，有 Key 可提升速率限制）
        """
        self.project_root = project_root
        # 未显式传入时从环境变量 JINA_API_KEY 读取（可选）
        self.jina_api_key = jina_api_key or os.environ.get("JINA_API_KEY", "").strip() or None
        self._last_request_time = 0.0
        # 工具通过 asyncio.to_thread 并发执行，限速必须加锁
        self._throttle_lock = threading.Lock()

    def _build_headers(self) -> Dict[str, str]:
        """构建请求头"""
        headers = {
            "Accept": "text/markdown",
            "X-Return-Format": "markdown",
            "X-No-Cache": "true",
        }
        if self.jina_api_key:
            headers["Authorization"] = f"Bearer {self.jina_api_key}"
        return headers

    def _throttle(self):
        """速率控制：确保请求间隔 5 秒（线程安全）"""
        with self._throttle_lock:
            now = time.time()
            elapsed = now - self._last_request_time
            if elapsed < BATCH_INTERVAL:
                time.sleep(BATCH_INTERVAL - elapsed)
            self._last_request_time = time.time()

    @staticmethod
    def _validate_url(url: str) -> str:
        """只允许 http/https 且 host 非空；返回编码后可安全拼接到 Jina 路径里的 URL"""
        if not url or not isinstance(url, str):
            raise InvalidParameterError("无效的 URL", suggestion="URL 必须以 http:// 或 https:// 开头")
        url = url.strip()
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise InvalidParameterError(
                f"无效的 URL: {url[:200]}",
                suggestion="URL 必须以 http:// 或 https:// 开头，且包含域名"
            )
        # 编码空格、# 等会改变 Jina 请求含义的字符，保留 URL 结构字符
        return quote(url, safe=":/?&=%@+,;~!$'()*[]")

    def read_article(
        self,
        url: str,
        timeout: int = DEFAULT_TIMEOUT
    ) -> Dict:
        """
        读取单篇文章内容（Markdown 格式）

        Args:
            url: 文章链接
            timeout: 请求超时时间（秒），默认 30

        Returns:
            文章内容字典
        """
        try:
            safe_url = self._validate_url(url)
            try:
                timeout = max(1, min(int(timeout), MAX_TIMEOUT))
            except (TypeError, ValueError):
                timeout = DEFAULT_TIMEOUT

            self._throttle()

            response = requests.get(
                f"{JINA_READER_BASE}/{safe_url}",
                headers=self._build_headers(),
                timeout=timeout
            )

            if response.status_code == 200:
                return {
                    "success": True,
                    "data": {
                        "url": url,
                        "content": response.text,
                        "format": "markdown",
                        "content_length": len(response.text)
                    }
                }
            elif response.status_code == 429:
                return {
                    "success": False,
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": "Jina Reader 速率限制，请稍后重试",
                        "suggestion": "免费限制: 100 RPM / 2 并发，可配置 API Key 提升限额"
                    }
                }
            else:
                return {
                    "success": False,
                    "error": {
                        "code": "FETCH_FAILED",
                        "message": f"HTTP {response.status_code}: {response.reason}",
                        "url": url
                    }
                }

        except requests.Timeout:
            return {
                "success": False,
                "error": {
                    "code": "TIMEOUT",
                    "message": f"请求超时（{timeout}秒）",
                    "url": url,
                    "suggestion": "可尝试增加 timeout 参数"
                }
            }
        except MCPError as e:
            return {"success": False, "error": e.to_dict()}
        except Exception as e:
            return {
                "success": False,
                "error": {
                    "code": "REQUEST_ERROR",
                    "message": safe_error(e),
                    "url": url
                }
            }

    def read_articles_batch(
        self,
        urls: List[str],
        timeout: int = DEFAULT_TIMEOUT
    ) -> Dict:
        """
        批量读取多篇文章内容（最多 5 篇，间隔 5 秒）

        Args:
            urls: 文章链接列表
            timeout: 每篇的请求超时时间（秒）

        Returns:
            批量读取结果
        """
        try:
            if not urls:
                raise InvalidParameterError(
                    "URL 列表不能为空",
                    suggestion="请提供至少一个 URL"
                )

            # 限制最多 5 篇
            actual_urls = urls[:MAX_BATCH_SIZE]
            skipped = len(urls) - len(actual_urls)

            results = []
            succeeded = 0
            failed = 0
            budget_skipped = 0
            started = time.monotonic()

            for i, url in enumerate(actual_urls):
                remaining = BATCH_TOTAL_BUDGET - (time.monotonic() - started) - BATCH_INTERVAL
                if remaining < 5:
                    budget_skipped += 1
                    results.append({
                        "index": i + 1,
                        "url": url,
                        "success": False,
                        "data": None,
                        "error": {"code": "BUDGET_EXCEEDED", "message": f"超过批量总耗时上限 {int(BATCH_TOTAL_BUDGET)} 秒，未读取"},
                    })
                    failed += 1
                    continue
                try:
                    per_timeout = min(int(timeout), int(remaining))
                except (TypeError, ValueError):
                    per_timeout = min(DEFAULT_TIMEOUT, int(remaining))
                result = self.read_article(url=url, timeout=per_timeout)

                results.append({
                    "index": i + 1,
                    "url": url,
                    "success": result["success"],
                    "data": result.get("data"),
                    "error": result.get("error")
                })

                if result["success"]:
                    succeeded += 1
                else:
                    failed += 1

            return {
                "success": True,
                "summary": {
                    "description": "批量文章读取结果",
                    "requested": len(urls),
                    "processed": len(actual_urls),
                    "succeeded": succeeded,
                    "failed": failed,
                    "skipped": skipped,
                    "interval_seconds": BATCH_INTERVAL,
                },
                "articles": results,
                "note": (
                    (f"已跳过 {skipped} 篇（单次上限 {MAX_BATCH_SIZE} 篇）" if skipped > 0 else "")
                    + (f"；{budget_skipped} 篇因总耗时上限未读取" if budget_skipped else "")
                ).lstrip("；") or None
            }

        except MCPError as e:
            return {"success": False, "error": e.to_dict()}
        except Exception as e:
            return {
                "success": False,
                "error": {
                    "code": "BATCH_ERROR",
                    "message": safe_error(e)
                }
            }
