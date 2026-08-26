#!/usr/bin/env python3
"""数据分层保留策略（v1）
- KEEP_FULL_DAYS=90  : 90天内保留每小时全量快照
- 超过90天          : 只保留当天 daily.html 聚合一份, 删除小时级明细
- 永久保留           : archive.html 索引 + feed.xml + docs/reports/latest/
首次运行会对现有历史做一次性迁移。
"""
import os, re, shutil, sys
from pathlib import Path

BASE = Path("docs/reports")
ARCHIVE = BASE / "archive-daily"
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
KEEP_FULL_DAYS = int(os.environ.get("KEEP_FULL_DAYS", "90"))

def parse_date(name):
    m = DATE_RE.match(name)
    return datetime.strptime(m.group(0), "%Y-%m-%d") if m else None

from datetime import datetime, timedelta

def run():
    cutoff = datetime.now() - timedelta(days=KEEP_FULL_DAYS)
    ARCHIVE.mkdir(exist_ok=True)
    removed_dirs, archived_files = 0, 0
    for d in sorted(BASE.iterdir()):
        dt = parse_date(d.name) if d.is_dir() else None
        if not dt:
            continue
        if dt >= cutoff:
            continue  # 90天内, 不动
        # 需要归档的旧目录
        target_daily = ARCHIVE / f"{d.name}-daily.html"
        if target_daily.exists():
            shutil.rmtree(d); removed_dirs += 1
            continue
        # 用当天最后一个快照作为daily(如果latest/daily没有独立版本的话)
        files = sorted(d.glob("*.html"))
        if files:
            last = files[-1]
            shutil.copy(last, target_daily)
            archived_files += 1
        shutil.rmtree(d); removed_dirs += 1
    print(f"retention: 归档 {archived_files} 天到 archive-daily/, 清理 {removed_dirs} 个小时级目录")
    if removed_dirs or archived_files:
        # 重建归档页与feed以反映新结构
        for script in ("scripts/gen_archive.py", "scripts/gen_feed.py", "scripts/gen_daily_index.py"):
            r = os.system(f"python3 {script} >/dev/null 2>&1")
            if r != 0:
                print(f"warn: {script} 重建失败")
if __name__ == "__main__":
    run()
