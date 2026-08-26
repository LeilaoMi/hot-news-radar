#!/usr/bin/env python3
"""历史报告归档页生成器
扫描 docs/reports/ 下的日期目录，生成按日期分组的静态归档首页 index.html
由 crawler.yml 的 Publish step 自动调用，无需人工干预
"""
import os, re
from pathlib import Path
from datetime import datetime

BASE = Path("docs/reports")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def esc(s): return s.replace("&","&").replace("<","<").replace(">",">")

def collect():
    """收集所有日期目录及其报告文件"""
    days = {}
    for d in sorted(BASE.iterdir(), reverse=True):
        if not (d.is_dir() and DATE_RE.match(d.name)):
            continue
        files = []
        for f in sorted(d.iterdir()):
            m = re.match(r"^(\d{2})-(\d{2})\.html$", f.name)
            if f.is_file() and m:
                hh, mm = m.groups()
                files.append((hh, mm, f.name))
        if files:
            days[d.name] = sorted(files, reverse=True)
    return days

def build_html(days):
    total = sum(len(v) for v in days.values())
    parts = [f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>热点雷达 · 历史归档</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
       background:#f5f6f8; color:#24292f; line-height:1.5; }}
header {{ background:#1a2233; color:#fff; padding:28px 20px; text-align:center; }}
header h1 {{ font-size:22px; margin-bottom:6px; }}
header p {{ opacity:.75; font-size:13px; }}
main {{ max-width:900px; margin:0 auto; padding:20px; }}
.stats {{ display:flex; gap:16px; justify-content:center; margin:18px 0; flex-wrap:wrap; }}
.stat {{ background:#fff; border-radius:10px; padding:12px 22px; box-shadow:0 1px 4px rgba(0,0,0,.06); }}
.stat b {{ font-size:20px; color:#0969da; display:block; }}
.stat span {{ font-size:12px; color:#666; }}
.day {{ background:#fff; border-radius:10px; margin-bottom:14px; overflow:hidden;
       box-shadow:0 1px 4px rgba(0,0,0,.06); }}
.day-h {{ padding:12px 18px; background:#fafbfc; border-bottom:1px solid #eee;
         display:flex; justify-content:space-between; align-items:center; cursor:pointer; }}
.day-h:hover {{ background:#f0f2f5; }}
.day-h b {{ font-size:15px; }}
.day-h small {{ color:#888; }}
.day-b {{ padding:10px 14px; display:flex; flex-wrap:wrap; gap:8px; }}
a.t {{ text-decoration:none; font-size:13px; color:#0969da; background:#eef3fd;
      border:1px solid #d7e2fb; padding:4px 10px; border-radius:6px; transition:.15s; }}
a.t:hover {{ background:#dbe7fc; }}
footer {{ text-align:center; padding:24px; font-size:12px; color:#999; }}
details[open] .day-h {{ border-bottom-color:#eee; }}
</style>
</head>
<body>
<header><h1>📡 热点雷达 · 历史归档</h1>
<p>每小时自动抓取的多平台热点快照，永久保存</p></header>
<main>
<div class="stats">
 <div class="stat"><b>{len(days)}</b><span>覆盖天数</span></div>
 <div class="stat"><b>{total}</b><span>快照总数</span></div>
 <div class="stat"><b>{days and max(len(v) for v in days.values()) or 0}</b><span>单日峰值</span></div>
</div>"""]

    for date, snaps in days.items():
        wd = "一二三四五六日"[datetime.strptime(date,"%Y-%m-%d").weekday()]
        parts.append(f"""<details class="day" open>
<summary class="day-h"><b>{date} 周{wd}</b><small>{len(snaps)} 个快照</small></summary>
<div class="day-b">""")
        for hh, mm, fn in snaps:
            parts.append(f'<a class="t" href="{date}/{fn}">{esc(hh+":"+mm)}</a>')
        parts.append("</div>\n</details>")

    parts.append(f"""
<p style="text-align:center;margin-top:10px"><small>最后更新：{datetime.now().strftime('%Y-%m-%d %H:%M')}</small></p>
</main>
<footer>Powered by TrendRadar · GPL-3.0 · 数据每小时自动更新</footer>
</body></html>""")
    return "\n".join(parts)

def main():
    BASE.mkdir(parents=True, exist_ok=True)
    days = collect()
    out = BASE / "archive.html"
    out.write_text(build_html(days), encoding="utf-8")
    print(f"归档页已生成: {out} ({len(days)} 天)")

if __name__ == "__main__":
    main()
