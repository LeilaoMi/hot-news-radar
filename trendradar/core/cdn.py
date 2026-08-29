# coding=utf-8
"""
CDN 回退模块

为版本检查等远程请求提供多源回退能力。
默认使用 GitHub 原始链接，失败后自动切换到 CDN 备用源。
同一会话中记住可用源的索引，后续请求从该源开始尝试。
"""

import re
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# 从任意 GitHub raw 链接里提取 owner/repo/path，而不是写死某一个仓库。
# 这样把 version_check_url 指向自己的仓库后，CDN 多源回退依然可用
# （raw.githubusercontent.com 在国内经常被墙，jsDelivr 回退非常关键）。
_GITHUB_RAW_PATTERN = re.compile(
    r"^https://raw\.githubusercontent\.com/"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+)/"
    r"(?:refs/heads/)?(?P<branch>[^/]+)/(?P<path>.+)$"
)

_CDN_TEMPLATES = [
    ("https://raw.githubusercontent.com/{owner}/{repo}/refs/heads/{branch}/", "GitHub"),
    ("https://fastly.jsdelivr.net/gh/{owner}/{repo}@{branch}/", "fastly.jsdelivr.net"),
    ("https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/", "cdn.jsdelivr.net"),
    ("https://gcore.jsdelivr.net/gh/{owner}/{repo}@{branch}/", "gcore.jsdelivr.net"),
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/plain, */*",
    "Cache-Control": "no-cache",
}

_TIMEOUT = 5

_state = {"last_ok": 0}


def _extract_path(url: str):
    """解析 GitHub raw 链接，返回 (sources, labels, path)；非 GitHub 链接返回 None。"""
    m = _GITHUB_RAW_PATTERN.match(url)
    if not m:
        return None
    owner, repo, branch, path = (
        m.group("owner"), m.group("repo"), m.group("branch"), m.group("path"),
    )
    sources = [tpl.format(owner=owner, repo=repo, branch=branch) for tpl, _ in _CDN_TEMPLATES]
    labels = {src: label for src, (_, label) in zip(sources, _CDN_TEMPLATES)}
    return sources, labels, path


def _do_request(url: str, proxies: Optional[dict]) -> str:
    resp = requests.get(url, headers=_HEADERS, proxies=proxies, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.text.strip()


def fetch_with_fallback(
    url: str,
    proxy_url: Optional[str] = None,
) -> Optional[str]:
    """从上次成功的源开始轮转尝试，非 GitHub 链接直接请求。"""
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    parsed = _extract_path(url)
    if parsed is None:
        try:
            return _do_request(url, proxies)
        except Exception as e:
            logger.warning("[版本检查] 获取失败: %s", e)
            return None

    sources, labels, path = parsed
    n = len(sources)
    start = _state["last_ok"]

    for offset in range(n):
        idx = (start + offset) % n
        source = sources[idx]
        try:
            content = _do_request(source + path, proxies)
            if idx != start:
                logger.info("[版本检查] 已切换到: %s", labels.get(source, source))
            _state["last_ok"] = idx
            return content
        except Exception:
            logger.debug("[版本检查] %s 不可用，尝试下一个源", labels.get(source, source))

    logger.warning("[版本检查] 所有源均不可用")
    return None
