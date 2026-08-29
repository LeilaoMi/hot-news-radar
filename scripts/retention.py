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

SNAPSHOT_NOTICE_MARKER = "rdr-snapshot-notice"

SNAPSHOT_NOTICE_HTML = """<div id="rdr-snapshot-notice" style="max-width:1100px;margin:12px auto;padding:10px 14px;
background:#fff7e6;border:1px solid #ffd591;border-radius:8px;
font-size:13px;line-height:1.7;color:#874d00">
<b>说明：</b>本页是当天最后一次抓取的<b>当前榜单快照</b>（原始文件 <code>{src}</code>），
并非程序在汇总时段生成的<b>当日汇总</b>报告。历史数据因超出保留期，小时级明细已清理，
故以此快照作为当日的归档代表。页内「报告类型」显示为「当前榜单」属正常现象。
</div>"""


def _mark_as_snapshot_archive(path, src_name):
    """给用快照冒充的归档页注入来源说明（幂等）。"""
    try:
        s = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    if SNAPSHOT_NOTICE_MARKER in s:
        return False
    # 插到导航条之后、正文之前：优先插在 <body> 后，其次插到最前面
    import re as _re
    m = _re.search(r"(<body[^>]*>)", s, _re.I)
    block = SNAPSHOT_NOTICE_HTML.format(src=src_name)
    s = (s[: m.end()] + block + s[m.end():]) if m else (block + s)
    try:
        path.write_text(s, encoding="utf-8")
    except Exception:
        return False
    return True


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
        # 用当天最后一个快照作为该日的归档代表。
        #
        # 注意：这个快照是「当前榜单」(current) 模式生成的，并不是真正的「当日汇总」
        # (daily)。早期实现直接把它命名成 *-daily.html，导致归档页的「当日汇总」
        # 点进去看到的是当前榜单内容、报告类型和生成时间都对不上。
        # 这里保留归档（历史数据仍有参考价值），但注入一条醒目提示说明其真实来源，
        # 避免误导。
        files = sorted(d.glob("*.html"))
        if files:
            last = files[-1]
            shutil.copy(last, target_daily)
            _mark_as_snapshot_archive(target_daily, last.name)
            archived_files += 1
        shutil.rmtree(d); removed_dirs += 1
    print(f"retention: 归档 {archived_files} 天到 archive-daily/, 清理 {removed_dirs} 个小时级目录")
    if removed_dirs or archived_files:
        # 重建归档页与feed以反映新结构。
        # gen_daily_index 必须排在 gen_archive 之前：archive.html 依据 older.html
        # 是否存在来决定是否渲染「更早归档」入口，顺序反了会漏掉该入口。
        for script in ("scripts/gen_daily_index.py", "scripts/gen_archive.py", "scripts/gen_feed.py"):
            r = os.system(f"python3 {script} >/dev/null 2>&1")
            if r != 0:
                print(f"warn: {script} 重建失败")
if __name__ == "__main__":
    run()
