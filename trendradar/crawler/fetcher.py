# coding=utf-8
"""
数据获取器模块

负责从 NewsNow API 抓取新闻数据，支持：
- 单个平台数据获取
- 批量平台数据爬取
- 自动重试机制
- 代理支持
"""

import json
import random
import time
from typing import Dict, List, Tuple, Optional, Union
from urllib.parse import urlencode, urlparse

import requests

from trendradar.core.logger import get_logger
from trendradar.utils.url import sanitize_url

log = get_logger(__name__)


class DataFetcher:
    """数据获取器"""

    # 默认 API 地址（newsnow 项目: https://github.com/ourongxing/newsnow）
    DEFAULT_API_URL = "https://newsnow.busiyi.world/api/s"

    # 默认请求头
    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }

    # newsnow 返回 status=cache 时，updatedTime 超过该时长视为过期数据（秒）
    DEFAULT_MAX_CACHE_AGE = 2 * 3600

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        api_url: Optional[str] = None,
        max_cache_age: Optional[int] = None,
    ):
        """
        初始化数据获取器

        Args:
            proxy_url: 代理服务器 URL（可选）
            api_url: API 基础 URL（可选，默认使用 DEFAULT_API_URL）
            max_cache_age: 缓存数据最大可接受年龄（秒），0 关闭检查，None 使用默认 2 小时
        """
        self.proxy_url = proxy_url
        self.api_url = api_url or self.DEFAULT_API_URL
        self.max_cache_age = self.DEFAULT_MAX_CACHE_AGE if max_cache_age is None else max_cache_age
        # 本轮因「缓存过期 / 空数据」被判失败的平台及原因，供日志与报告使用
        self.failure_reasons: Dict[str, str] = {}
        if urlparse(self.api_url).scheme == "http":
            log.warning(
                f"⚠️ 热榜 API 使用明文 http（{self.api_url}），数据可被中间人篡改；"
                "建议改为 https（expected_domain 只校验条目链接，不保护 API 本身）"
            )

    def _build_api_url(self, id_value: str) -> str:
        """构造请求地址：兼容 api_url 自带查询参数的情况（不再用字符串硬拼 ?id=）。"""
        sep = "&" if "?" in self.api_url else "?"
        return f"{self.api_url}{sep}{urlencode({'id': id_value})}&latest"

    def _check_freshness(self, data_json: Dict, now_ts: Optional[float] = None) -> Optional[str]:
        """status=cache 时校验 updatedTime；返回 None 表示可接受，否则返回原因。"""
        if not self.max_cache_age or data_json.get("status") != "cache":
            return None
        updated = data_json.get("updatedTime")
        if updated in (None, ""):
            return None
        try:
            updated = float(updated)
        except (TypeError, ValueError):
            return None
        if updated > 1e12:  # 毫秒时间戳
            updated /= 1000.0
        now_ts = time.time() if now_ts is None else now_ts
        age = now_ts - updated
        if age > self.max_cache_age:
            return f"缓存数据已过期 {age / 3600:.1f} 小时（阈值 {self.max_cache_age / 3600:.1f} 小时）"
        return None

    @staticmethod
    def _check_domain_safety(
        items: List[Dict],
        expected_domain: str,
    ) -> Optional[str]:
        """
        校验返回数据中的链接是否为 HTTPS 且域名匹配预期（支持子域名）

        同时校验 url 与 mobileUrl 两个字段，使用标准库解析主机名，
        避免 userinfo（如 https://baidu.com@evil.com）绕过校验。

        Args:
            items: API 返回的数据项列表
            expected_domain: 预期域名（如 "baidu.com"）

        Returns:
            None 表示安全，否则返回第一个异常描述
        """
        expected = expected_domain.lower().strip()
        if not expected:
            return None

        for item in items:
            for field in ("url", "mobileUrl"):
                url = item.get(field, "")
                if not url:
                    continue
                parsed = urlparse(url)
                if parsed.scheme != "https":
                    return f"{url} (非 HTTPS 或格式异常)"
                hostname = (parsed.hostname or "").lower()
                if hostname != expected and not hostname.endswith("." + expected):
                    return f"{hostname} (来自 {url})"
        return None

    def fetch_data(
        self,
        id_info: Union[str, Tuple[str, str]],
        max_retries: int = 2,
        min_retry_wait: int = 3,
        max_retry_wait: int = 5,
    ) -> Tuple[Optional[str], str, str]:
        """
        获取指定ID数据，支持重试

        Args:
            id_info: 平台ID 或 (平台ID, 别名) 元组
            max_retries: 最大重试次数
            min_retry_wait: 最小重试等待时间（秒）
            max_retry_wait: 最大重试等待时间（秒）

        Returns:
            (响应文本, 平台ID, 别名) 元组，失败时响应文本为 None
        """
        if isinstance(id_info, tuple):
            id_value, alias = id_info
        else:
            id_value = id_info
            alias = id_value

        url = self._build_api_url(id_value)

        proxies = None
        if self.proxy_url:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}

        retries = 0
        while retries <= max_retries:
            try:
                response = requests.get(
                    url,
                    proxies=proxies,
                    headers=self.DEFAULT_HEADERS,
                    timeout=10,
                )
                response.raise_for_status()

                data_text = response.text
                data_json = json.loads(data_text)

                status = data_json.get("status", "未知")
                if status not in ["success", "cache"]:
                    raise ValueError(f"响应状态异常: {status}")

                stale_reason = self._check_freshness(data_json)
                if stale_reason:
                    # 过期缓存重试也拿不到新数据，直接判失败，避免把旧榜单当作当前榜单
                    log.warning(f"请求 {id_value} 返回过期缓存: {stale_reason}")
                    self.failure_reasons[id_value] = stale_reason
                    return None, id_value, alias

                status_info = "最新数据" if status == "success" else "缓存数据"
                log.info(f"获取 {id_value} 成功（{status_info}）")
                return data_text, id_value, alias

            except Exception as e:
                retries += 1
                if retries <= max_retries:
                    base_wait = random.uniform(min_retry_wait, max_retry_wait)
                    additional_wait = (retries - 1) * random.uniform(1, 2)
                    wait_time = base_wait + additional_wait
                    log.info(f"请求 {id_value} 失败: {e}. {wait_time:.2f}秒后重试...")
                    time.sleep(wait_time)
                else:
                    log.info(f"请求 {id_value} 失败: {e}")
                    return None, id_value, alias

        return None, id_value, alias

    def crawl_websites(
        self,
        ids_list: List[Union[str, Tuple[str, str]]],
        request_interval: int = 100,
        domain_rules: Optional[Dict[str, str]] = None,
    ) -> Tuple[Dict, Dict, List]:
        """
        爬取多个网站数据

        Args:
            ids_list: 平台ID列表，每个元素可以是字符串或 (平台ID, 别名) 元组
            request_interval: 请求间隔（毫秒）
            domain_rules: 域名安全校验规则，格式 {平台ID: 预期域名}（可选）

        Returns:
            (结果字典, ID到名称的映射, 失败ID列表) 元组
        """
        results = {}
        id_to_name = {}
        failed_ids = []
        domain_rules = domain_rules or {}
        self.failure_reasons = {}

        for i, id_info in enumerate(ids_list):
            if isinstance(id_info, tuple):
                id_value, name = id_info
            else:
                id_value = id_info
                name = id_value

            id_to_name[id_value] = name
            response, _, _ = self.fetch_data(id_info)

            if response:
                try:
                    data = json.loads(response)
                    items = data.get("items", [])

                    # 域名安全校验
                    expected_domain = domain_rules.get(id_value, "")
                    if expected_domain:
                        bad_reason = self._check_domain_safety(items, expected_domain)
                        if bad_reason:
                            log.info(f"⚠️ 安全警告: {name}({id_value}) 返回数据未通过域名安全校验！")
                            log.info(f"   预期域名: https://*.{expected_domain}")
                            log.info(f"   异常来源: {bad_reason}")
                            log.info(f"   当前 API 地址: {self.api_url}")
                            log.info("   该平台数据已丢弃，请检查 API 来源是否可信")
                            failed_ids.append(id_value)
                            continue

                    if not items:
                        log.warning(f"{name}({id_value}) 返回空列表，计为失败")
                        self.failure_reasons[id_value] = "返回空数据"
                        failed_ids.append(id_value)
                        continue

                    results[id_value] = {}

                    for index, item in enumerate(items, 1):
                        title = item.get("title")
                        # 跳过无效标题（None、float、空字符串）
                        if title is None or isinstance(title, float) or not str(title).strip():
                            continue
                        title = str(title).strip()
                        # 协议白名单：未配置 expected_domain 的平台也不允许 javascript:/data: 链接入库
                        url = sanitize_url(item.get("url", "") or "")
                        mobile_url = sanitize_url(item.get("mobileUrl", "") or "")

                        if title in results[id_value]:
                            results[id_value][title]["ranks"].append(index)
                        else:
                            results[id_value][title] = {
                                "ranks": [index],
                                "url": url,
                                "mobileUrl": mobile_url,
                            }
                except json.JSONDecodeError:
                    log.info(f"解析 {id_value} 响应失败")
                    failed_ids.append(id_value)
                except Exception as e:
                    log.info(f"处理 {id_value} 数据出错: {e}")
                    failed_ids.append(id_value)
            else:
                failed_ids.append(id_value)

            # 请求间隔（除了最后一个）
            if i < len(ids_list) - 1:
                actual_interval = request_interval + random.randint(-10, 20)
                actual_interval = max(50, actual_interval)
                time.sleep(actual_interval / 1000)

        log.info(f"成功: {list(results.keys())}, 失败: {failed_ids}")
        return results, id_to_name, failed_ids
