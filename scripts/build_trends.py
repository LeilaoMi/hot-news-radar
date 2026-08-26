#!/usr/bin/env python3
"""跨快照趋势分析
扫描最近 N 天的快照HTML, 提取(平台,标题)出现次数 → 
1. 持续热度榜(连续在榜时段数)
2. 多平台共振(同题多源)
输出 docs/reports/trends.html
"""
import re, html
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

BASE = Path("docs/reports")
DAYS = int(__import__('os').environ.get("TREND_DAYS", "3"))
SITE = "https://leilaomi.github.io/hot-news-radar"

TITLE_RE = re.compile(r'<a[^>]+href="(http[^"]+)"[^>]*>([^<]{8,120})</a>')

def extract(day_dir):
    """从单日目录所有快照提取 (标题 -> {platforms:set, times:[str]})"""
    agg = defaultdict(lambda: {"plats": set(), "times": [], "url": ""})
    for f in sorted(day_dir.glob("*.html")):
        try:
            s = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # 平台分组头: <h3>或类似结构里含平台名; 简化: 用每条链接前最近的平台徽章文本
        # 更稳的方式: 报告中每个 item 链接独立成立, 平台归因靠 nav 标签不靠谱 → 
        # 改为按 h4/h5 分组段计算
        for m in re.finditer(
            r'<span class="source-name">([^<]+)</span>'
            r'[\s\S]{0,600}?<a[^>]+href="(http[^"]+)"[^>]*>([^<]{4,200})</a>', s):
            plat = m.group(1).strip()
            title = html.unescape(m.group(3)).strip()
            url = m.group(2)
            if not (plat and title):
                continue
            ent = agg[title]
            ent["plats"].add(plat)
            ent["times"].append(f.name)
            if not ent["url"]:
                ent["url"] = url
    return agg

def main():
    cutoff = datetime.now() - timedelta(days=DAYS)
    all_agg = defaultdict(lambda: {"plats": set(), "count": 0, "days": set(), "url": "", "first": None})
    day_count = 0
    for d in sorted(BASE.iterdir(), reverse=True):
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", d.name)
        if not (d.is_dir() and m):
            continue
        dt = datetime.strptime(d.name, "%Y-%m-%d")
        if dt < cutoff:
            break
        day_count += 1
        day_agg = extract(d)
        for title, ent in day_agg.items():
            a = all_agg[title]
            a["plats"] |= ent["plats"]
            a["count"] += len(ent["times"])
            a["days"].add(d.name)
            if not a["url"]: a["url"] = ent["url"]
            if a["first"] is None or d.name < a["first"]:
                a["first"] = d.name

    rows = sorted(all_agg.items(), key=lambda kv: (-kv[1]["count"], -len(kv[1]["plats"])))
    total_titles = len(rows)
    hot = [r for r in rows if r[1]["count"] >= max(6, DAYS*2)]
    multi = [r for r in rows if len(r[1]["plats"]) >= 3]

    def table(items, col_header, val_fn):
        trs = "".join(
            f'<tr><td>{i+1}</td><td><a href="{html.escape(v["url"])}" target="_blank">{html.escape(t[:60])}</a></td>'
            f'<td>{val_fn(v)}</td><td>{html.escape(",".join(sorted(v["plats"]))[:60] or "-")}</td></tr>'
            for i,(t,v) in enumerate(items[:25]))
        return ('<table><thead><tr><th>#</th><th>标题</th><th>'+col_header+'</th><th>来源平台</th></tr></thead>'
                '<tbody>'+trs+'</tbody></table>')

    gen_at = datetime.now().strftime('%Y-%m-%d %H:%M')
    page = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>热点雷达 · 趋势洞察</title><style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f5f6f8;color:#24292f;line-height:1.55}}
nav{{background:#1a2233;color:#fff;padding:8px 16px;font-size:13px;display:flex;gap:18px}}
nav a{{color:#79c0ff;text-decoration:none}}
header{{background:#1a2233;color:#fff;padding:24px;text-align:center;margin-top:-33px}}
header p{{opacity:.75;font-size:13px;margin-top:4px}}
main{{max-width:980px;margin:0 auto;padding:18px}}
h2{{margin:26px 0 12px;font-size:18px}}
p.sum{{color:#666;font-size:13px;margin-bottom:8px}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06)}}
th,td{{padding:9px 12px;text-align:left;font-size:13.5px;border-bottom:1px solid #eee}}
th{{background:#fafbfc;color:#555}}
td:first-child{{color:#888;width:34px}}
td a{{color:#24292f;text-decoration:none}}
td a:hover{{color:#0969da}}
.tag{{display:inline-block;background:#eef3fd;color:#0969da;border-radius:5px;padding:1px 7px;font-size:12px;margin:1px}}
footer{{text-align:center;padding:22px;font-size:12px;color:#999}}</style></head><body>
<nav><a href="/">&#127919; 新闻中心</a><a href="archive.html" style="color:#d2a8ff">&#128193; 历史</a><a href="latest/current.html" style="color:#7ee787">&#128293; 实时</a></nav>
<header><h1>&#128200; 趋势洞察</h1><p>近 {day_count} 天 · 共分析 {total_titles} 条标题 · 每轮发布自动更新</p></header><main>
<h2>&#128293; 持续热度榜</h2>
<p class="sum">连续多时段/多日在榜 = 真正的重磅事件（上榜快照数 ≥ {max(6, DAYS*2)}）</p>
{table(hot, '在榜次数', lambda v: str(v['count'])+' 次 / '+str(len(v['days']))+' 天')}
<h2>&#127760; 多平台共振</h2>
<p class="sum">≥3 个平台同时报道的标题 — 全网级话题</p>
{table(multi, '覆盖平台', lambda v: '<span class="tag">'+str(len(v['plats']))+'</span>')}
<p style="text-align:center;margin-top:14px"><small>最后更新：{gen_at}</small></p>
</main><footer>Powered by TrendRadar 引擎 · GPL-3.0</footer></body></html>"""

    out = BASE / "trends.html"
    out.write_text(page, encoding="utf-8")
    print(f"趋势页已生成: {out} | 分析{day_count}天/{total_titles}标题 | 热度{len(hot)} / 共振{len(multi)}")

if __name__ == "__main__":
    main()
