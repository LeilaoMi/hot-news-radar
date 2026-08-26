#!/usr/bin/env python3
"""archive-daily/ 目录索引页生成器"""
from pathlib import Path
from datetime import datetime

BASE = Path("docs/reports")
ARCH = BASE / "archive-daily"
SITE = "https://leilaomi.github.io/hot-news-radar"

def main():
    if not ARCH.is_dir():
        return
    files = sorted(ARCH.glob("*-daily.html"), reverse=True)
    rows = []
    for f in files:
        d = f.name.replace("-daily.html", "")
        wd = "一二三四五六日"[datetime.strptime(d, "%Y-%m-%d").weekday()]
        rows.append(
            f'<a class="d" href="archive-daily/{f.name}"><b>{d} 周{wd}</b>'
            f'<span>全天汇总 →</span></a>')
    html = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>热点雷达 · 更早日汇总</title><style>'
        'body{font-family:-apple-system,"PingFang SC",sans-serif;background:#f5f6f8;'
        'color:#24292f;margin:0;line-height:1.5}'
        'header{background:#1a2233;color:#fff;padding:24px;text-align:center}'
        'main{max-width:760px;margin:20px auto;padding:0 16px}'
        'a.d{display:flex;justify-content:space-between;align-items:center;'
        'background:#fff;border-radius:10px;padding:14px 18px;margin-bottom:10px;'
        'text-decoration:none;color:#24292f;box-shadow:0 1px 4px rgba(0,0,0,.06)}'
        'a.d:hover{background:#f0f6ff}'
        'a.d span{color:#0969da;font-size:13px}</style></head><body>'
        '<header><h1>&#128230; 更早日期 · 全天汇总</h1>'
        '<p style="opacity:.75;font-size:13px">90 天前的数据已聚合为单日文件</p></header><main>'
        + "\n".join(rows) +
        '</main></body></html>')
    out = BASE / "older.html"
    out.write_text(html, encoding="utf-8")
    print(f"daily index: {out} ({len(files)} 天)")

if __name__ == "__main__":
    main()
