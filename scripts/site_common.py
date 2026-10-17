#!/usr/bin/env python3
"""scripts/ 共用的小工具：站点时区、快照文件识别、站点 URL。

各脚本以 ``python3 scripts/xxx.py`` 方式运行，sys.path[0] 就是 scripts/，
可以直接 ``import site_common``；测试里按路径加载时由脚本自行把 scripts/ 加进 sys.path。
"""
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# 报告正文、快照文件名、日期目录全部使用站点时区的墙钟。
# runner 时钟是 UTC，直接 datetime.now() 会差 8 小时（审计 4.3）。
SITE_TZ_NAME = os.environ.get("SITE_TZ", "Asia/Shanghai")
SITE_TZ = ZoneInfo(SITE_TZ_NAME)

# 时段快照文件名：HH-MM.html。日期目录里可能出现别的 html（例如 rss_HH-MM.html、
# daily.html），所有按快照处理的地方都必须先过滤，否则 strptime 会抛 ValueError
# 让整个发布步骤中断（审计 3.8）。
SNAPSHOT_RE = re.compile(r"^\d{2}-\d{2}$")
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DEFAULT_SITE = "https://leilaomi.github.io/hot-news-radar"


def now_site() -> datetime:
    """站点时区的当前墙钟（naive，便于与报告里的无时区时间比较）。"""
    return datetime.now(SITE_TZ).replace(tzinfo=None)


def is_snapshot(path: Path) -> bool:
    return path.is_file() and path.suffix == ".html" and bool(SNAPSHOT_RE.fullmatch(path.stem))


def snapshots(day_dir: Path) -> list:
    """日期目录内的时段快照（HH-MM.html），按时间正序。"""
    return sorted((p for p in day_dir.glob("*.html") if is_snapshot(p)), key=lambda p: p.name)


def site_url(docs_dir: Path = Path("docs")) -> str:
    """站点的规范根 URL（不带结尾斜杠）。

    优先级：环境变量 SITE_URL > docs/CNAME > github.io 子路径。
    写死 github.io 会让 feed 里每个链接都多一次 301（审计 4.5）。
    """
    env = os.environ.get("SITE_URL", "").strip()
    if env:
        return env.rstrip("/")
    cname = docs_dir / "CNAME"
    try:
        host = cname.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        host = ""
    if host:
        return f"https://{host}".rstrip("/")
    return DEFAULT_SITE
