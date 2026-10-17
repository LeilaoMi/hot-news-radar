#!/usr/bin/env python3
"""数据分层保留策略（v1）
- KEEP_FULL_DAYS=90  : 90天内保留每小时全量快照
- 超过90天          : 只保留当天一份归档页（archive-daily/<date>-daily.html），删除小时级明细
- 永久保留           : archive.html 索引 + feed.xml + docs/reports/latest/
首次运行会对现有历史做一次性迁移。

注意：本脚本只做清理与归档，不再在内部重跑 gen_daily_index/gen_archive/gen_feed
（审计 4.4：workflow 随后本来就会再跑一遍，内部重跑既重复又用 >/dev/null 吞掉错误）。
"""
import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_common import now_site, snapshots  # noqa: E402

BASE = Path("docs/reports")
ARCHIVE = BASE / "archive-daily"
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
KEEP_FULL_DAYS = int(os.environ.get("KEEP_FULL_DAYS", "90"))

SNAPSHOT_NOTICE_MARKER = "rdr-snapshot-notice"

# 快照页里的「上一时刻/下一时刻」导航条：链接是同目录相对路径（如 22-04.html），
# 复制到 archive-daily/ 后全部变成 404（审计 4.1），归档时必须删掉。
PN_RE = re.compile(r'<div id="rdr-pn"[^>]*>.*?</div>', re.S)
REPORT_TYPE_RE = re.compile(r'报告类型</span>\s*<span class=["\']info-value["\']>\s*([^<]+?)\s*<')

_NOTICE_STYLE = ('<div id="rdr-snapshot-notice" style="max-width:1100px;margin:12px auto;padding:10px 14px;'
                 'background:#fff7e6;border:1px solid #ffd591;border-radius:8px;'
                 'font-size:13px;line-height:1.7;color:#874d00">')

SNAPSHOT_NOTICE_HTML = _NOTICE_STYLE + """
<b>说明：</b>本页是当天最后一次抓取的<b>当前榜单快照</b>（原始文件 <code>{src}</code>），
并非程序在汇总时段生成的<b>当日汇总</b>报告。历史数据因超出保留期，小时级明细已清理，
故以此快照作为当日的归档代表。页内「报告类型」显示为「当前榜单」属正常现象。
</div>"""

DAILY_NOTICE_HTML = _NOTICE_STYLE + """
<b>说明：</b>本页是当天的<b>全天汇总</b>报告（原始文件 <code>{src}</code>）。
历史数据因超出保留期，小时级明细已清理，故以此报告作为当日的归档代表。
</div>"""


def parse_date(name):
    m = DATE_RE.match(name)
    return datetime.strptime(m.group(0), "%Y-%m-%d") if m else None


def report_type(html: str) -> str:
    m = REPORT_TYPE_RE.search(html)
    return m.group(1).strip() if m else ""


def prepare_archive_html(html: str, src_name: str) -> str:
    """把快照页改造成归档页：删时序导航（相对链接在归档目录下会 404），注入来源说明（幂等）。"""
    html = PN_RE.sub("", html)
    if SNAPSHOT_NOTICE_MARKER in html:
        return html
    kind = report_type(html)
    tpl = DAILY_NOTICE_HTML if ("汇总" in kind) else SNAPSHOT_NOTICE_HTML
    block = tpl.format(src=src_name)
    m = re.search(r"(<body[^>]*>)", html, re.I)
    return (html[: m.end()] + block + html[m.end():]) if m else (block + html)


def _mark_as_snapshot_archive(path, src_name):
    """给用快照冒充的归档页注入来源说明并删掉失效导航（幂等）。"""
    try:
        s = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    new = prepare_archive_html(s, src_name)
    if new == s:
        return False
    try:
        path.write_text(new, encoding="utf-8")
    except Exception:
        return False
    return True


def fix_existing_archives():
    """一次性修复：已有 archive-daily 页里残留的 rdr-pn 死链。"""
    fixed = 0
    if not ARCHIVE.is_dir():
        return 0
    for f in ARCHIVE.glob("*-daily.html"):
        try:
            s = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if PN_RE.search(s):
            f.write_text(PN_RE.sub("", s), encoding="utf-8")
            fixed += 1
    return fixed


def run():
    # 日期目录按站点时区命名；runner 是 UTC，用 now_site() 才不会偏 8 小时
    cutoff = now_site() - timedelta(days=KEEP_FULL_DAYS)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    removed_dirs, archived_files = 0, 0
    for d in sorted(BASE.iterdir()):
        dt = parse_date(d.name) if d.is_dir() else None
        if not dt:
            continue
        if dt >= cutoff:
            continue  # 保留期内, 不动
        target_daily = ARCHIVE / f"{d.name}-daily.html"
        if target_daily.exists():
            shutil.rmtree(d)
            removed_dirs += 1
            continue
        # 用当天最后一个时段快照作为该日的归档代表（只认 HH-MM.html）。
        files = snapshots(d)
        if files:
            last = files[-1]
            shutil.copy(last, target_daily)
            _mark_as_snapshot_archive(target_daily, last.name)
            archived_files += 1
        shutil.rmtree(d)
        removed_dirs += 1
    fixed = fix_existing_archives()
    print(f"retention: 归档 {archived_files} 天到 archive-daily/, 清理 {removed_dirs} 个小时级目录"
          f"{f'，修复 {fixed} 个归档页的失效导航' if fixed else ''}")


if __name__ == "__main__":
    run()
