#!/usr/bin/env python3
"""历史报告归档页生成器（v2）
- 扫描 docs/reports/ 日期目录 → 按日期分组归档页 archive.html
- 页内日期搜索跳转 + 高亮
- 幂等注入站点导航条与展开/收起按钮到全部快照页
"""
import re
from pathlib import Path
from datetime import datetime

BASE = Path("docs/reports")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

NAV_HTML = (
    '<div id="rdr-nav" style="position:sticky;top:0;z-index:9999;background:#1a2233;'
    'color:#fff;padding:8px 16px;font-size:13px;display:flex;gap:18px;align-items:center;'
    'font-family:-apple-system,\'PingFang SC\',sans-serif">'
    '<a href="/" style="color:#79c0ff;text-decoration:none;font-weight:600">&#127919; 新闻中心</a>'
    '<a href="/reports/archive.html" style="color:#d2a8ff;text-decoration:none">&#128193; 历史</a>'
    '<a href="/reports/latest/daily.html" style="color:#7ee787;text-decoration:none">&#128202; 当日汇总</a>'
    '<a href="/editor.html" style="color:#ffa657;text-decoration:none">&#9881; 配置</a>'
    '<span style="margin-left:auto;opacity:.55">Hot News Radar</span>'
    '</div>'
    ''
)

SEARCH_JS = (
    '<script>document.getElementById("q").addEventListener("change",function(){'
    'var v=this.value;if(!v)return;'
    'var ts=document.querySelectorAll(".day summary b");'
    'for(var i=0;i<ts.length;i++){'
    'if(ts[i].textContent.indexOf(v)===0){'
    'var d=ts[i].closest("details");d.setAttribute("open","");'
    'document.querySelectorAll(".day").forEach(function(x){x.style.outline=""});'
    'd.style.outline="3px solid #0969da";'
    'd.scrollIntoView({behavior:"smooth",block:"start"});'
    'setTimeout(function(){d.style.outline=""},2200);return;}}'
    'alert("未找到 "+v+"（当天无快照）");});</script>'
)

COLLAPSE_JS = (
    '<script>(function(){var btn=document.createElement("button");btn.id="rdr-fold";btn.innerHTML="&#9776; 折叠分组";btn.style.cssText="position:fixed;bottom:20px;right:20px;z-index:9999;background:#f0f6ff;color:#0969da;border:1px solid #d0d7de;border-radius:20px;padding:8px 16px;font-size:13px;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.08);font-family:inherit"var folded=false;btn.onclick=function(){folded=!folded;this.innerHTML=folded?"&#9776; 展开全部分组":"&#9776; 折叠分组";document.querySelectorAll("details").forEach(function(d){folded?d.removeAttribute("open"):d.setAttribute("open","")});var groups=document.querySelectorAll(".feed-group, .group-header, section");groups.forEach(function(g){var kids=g.querySelectorAll(".news-item, .rss-item, .item");if(kids.length>8){kids.forEach(function(k,i){if(folded&&i>=5){k.style.display="none"}else{k.style.display=""}});var more=g.querySelector(".rdr-more");if(!more&&kids.length>8){more=document.createElement("div");more.className="rdr-more";more.style.cssText="text-align:center;padding:10px;color:#0969da;font-size:13px;cursor:pointer";g.appendChild(more)}if(folded){more.textContent="下拉显示全部 "+kids.length+" 条 ↓";more.onclick=function(){kids.forEach(function(k){k.style.display=""});more.remove()}}else if(more.parentNode){more.parentNode.removeChild(more)}}}})})();</script>'
)
SEARCHBAR_JS = (    '<div id="rdr-search" style="position:fixed;top:52px;left:50%;transform:translateX(-50%);z-index:9998;width:min(420px,86vw)"><input id="rdr-q" placeholder="&#128269; 在本页过滤标题…" style="width:100%;padding:9px 15px;font-size:13.5px;border:1px solid rgba(0,0,0,.12);border-radius:20px;outline:none;background:rgba(255,255,255,.92);backdrop-filter:blur(10px);box-shadow:0 2px 10px rgba(0,0,0,.08);font-family:inherit"><span id="rdr-hit" style="position:absolute;right:14px;top:9px;font-size:12px;color:#0969da"></span></div><script>(function(){var q=document.getElementById("rdr-q"),hit=document.getElementById("rdr-hit");if(!q)return;var pre=new URLSearchParams(location.search).get("q");function apply(kw){var items=document.querySelectorAll(".news-item,.rss-item,.item");var n=0;items.forEach(function(it){var t=it.textContent.toLowerCase();var ok=!kw||t.indexOf(kw)>-1;it.style.display=ok?"":"none";if(ok&&kw)n++;});hit.textContent=kw?(n+" 条命中"):"";}q.addEventListener("input",function(){apply(this.value.trim().toLowerCase())});if(pre){q.value=pre;apply(pre.toLowerCase())}})();</script>')
def esc(s):
    return s.replace("&","&").replace("<","<").replace(">",">")

def collect():
    days = {}
    for d in sorted(BASE.iterdir(), reverse=True):
        if d.name == "archive-daily":
            continue
        if not (d.is_dir() and DATE_RE.match(d.name)):
            continue
        files = []
        for f in sorted(d.iterdir()):
            m = re.match(r"^(\d{2})-(\d{2})\.html$", f.name)
            if f.is_file() and m:
                files.append((m.group(1), m.group(2), f.name))
        if files:
            days[d.name] = sorted(files, reverse=True)
    return days

def build_html(days):
    # 合并 archive-daily 层(90天前的日级汇总)为"单条目日期"
    daily_arch = BASE / "archive-daily"
    extra = []
    if daily_arch.is_dir():
        known = set(days.keys())
        for f in sorted(daily_arch.glob("*-daily.html"), reverse=True):
            d = f.name.replace("-daily.html", "")
            if d not in known:
                extra.append((d, f))
        extra.sort(reverse=True)
    total = sum(len(v) for v in days.values()) + len(extra)
    peak = max((len(v) for v in days.values()), default=0)
    css_extra = (
        ".search-wrap{max-width:900px;margin:0 auto;padding:0 20px 14px}"
        "#q{width:100%;padding:11px 16px;font-size:15px;border:1px solid #d7e2fb;"
        "border-radius:10px;outline:none;background:#fff;color:#24292f}"
        "#q:focus{border-color:#0969da;box-shadow:0 0 0 3px rgba(9,105,218,.12)}"
    )
    head = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>热点雷达 · 历史归档</title><style>'
        '*{margin:0;padding:0;box-sizing:border-box}'
        "body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
        'background:#f5f6f8;color:#24292f;line-height:1.5}'
        'header{background:#1a2233;color:#fff;padding:28px 20px;text-align:center}'
        'header h1{font-size:22px;margin-bottom:6px}'
        'header p{opacity:.75;font-size:13px}'
        'main{max-width:900px;margin:0 auto;padding:20px}'
        '.stats{display:flex;gap:16px;justify-content:center;margin:18px 0;flex-wrap:wrap}'
        '.stat{background:#fff;border-radius:10px;padding:12px 22px;box-shadow:0 1px 4px rgba(0,0,0,.06)}'
        '.stat b{font-size:20px;color:#0969da;display:block}'
        '.stat span{font-size:12px;color:#666}'
        '.day{background:#fff;border-radius:10px;margin-bottom:14px;overflow:hidden;'
        'box-shadow:0 1px 4px rgba(0,0,0,.06)}'
        '.day-h{padding:12px 18px;background:#fafbfc;border-bottom:1px solid #eee;'
        'display:flex;justify-content:space-between;align-items:center;cursor:pointer}'
        '.day-h:hover{background:#f0f2f5}'
        '.day-h b{font-size:15px}.day-h small{color:#888}'
        '.day-b{padding:10px 14px;display:flex;flex-wrap:wrap;gap:8px}'
        'a.t{text-decoration:none;font-size:13px;color:#0969da;background:#eef3fd;'
        'border:1px solid #d7e2fb;padding:4px 10px;border-radius:6px;transition:.15s}'
        'a.t:hover{background:#dbe7fc}'
        'footer{text-align:center;padding:24px;font-size:12px;color:#999}'
        'details[open] .day-h{border-bottom-color:#eee}' + css_extra +
        '</style></head><body>' + NAV_HTML +
        '<header><h1>&#128225; 热点雷达 · 历史归档</h1>'
        '<p>每小时自动抓取的多平台热点快照，永久保存</p></header>'
        '<div class="search-wrap"><input id="q" type="date"></div><main>'
        '<div class="stats">'
        '<div class="stat"><b>' + str(len(days)) + '</b><span>覆盖天数</span></div>'
        '<div class="stat"><b>' + str(total) + '</b><span>快照总数</span></div>'
        '<div class="stat"><b>' + str(peak) + '</b><span>单日峰值</span></div>'
        '</div>'
        '<p style="text-align:center;margin-top:14px"><a href="older.html" style="color:#0969da;font-size:14px">&#128230; 90 天前的更早日汇总 &#8594;</a></p>'
    )
    PAGE_SIZE = 30
    day_items = list(days.items())
    visible, hidden = day_items[:PAGE_SIZE], day_items[PAGE_SIZE:]
    body = []
    for date, snaps in visible:
        wd = "一二三四五六日"[datetime.strptime(date, "%Y-%m-%d").weekday()]
        body.append(
            '<details class="day" open><summary class="day-h"><b>' + date +
            ' 周' + wd + '</b><small>' + str(len(snaps)) + ' 个快照</small></summary>'
            '<div class="day-b">')
        for hh, mm, fn in snaps:
            body.append('<a class="t" href="' + date + '/' + fn + '">' + hh + ':' + mm + '</a>')
        body.append('</div></details>')
    if hidden:
        body.append('<div id="more-days" style="display:none">')
        for date, snaps in hidden:
            wd = "一二三四五六日"[datetime.strptime(date, "%Y-%m-%d").weekday()]
            body.append(
                '<details class="day"><summary class="day-h"><b>' + date +
                ' 周' + wd + '</b><small>' + str(len(snaps)) + ' 个快照</small></summary>'
                '<div class="day-b">')
            for hh, mm, fn in snaps:
                body.append('<a class="t" href="' + date + '/' + fn + '">' + hh + ':' + mm + '</a>')
            body.append('</div></details>')
        body.append('</div>')
        body.append('<button id="load-more" onclick="var m=document.getElementById(\'more-days\');'
                    'm.style.display=\'block\';this.remove()" '
                    'style="display:block;margin:16px auto;padding:10px 26px;background:#fff;'
                    'color:#0969da;border:1px solid #d0d7de;border-radius:22px;font-size:14px;'
                    'cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,.06)">'
                    '&#128230; 加载更早 ' + str(len(hidden)) + ' 天</button>')
    if extra:
        body.append('<details class="day" open><summary class="day-h">'
                    '<b>&#128230; 更早日份（每日汇总）</b><small>'
                    + str(len(extra)) + ' 天</small></summary><div class="day-b">')
        for d, f in extra:
            wd = "一二三四五六日"[datetime.strptime(d, "%Y-%m-%d").weekday()]
            body.append('<a class="t" href="archive-daily/' + f.name + '" '
                        'title="' + d + ' 全天汇总">' + d[5:] + ' 周' + wd + '</a>')
        body.append('</div></details>')
    foot = (
        '<p style="text-align:center;margin-top:10px"><small>最后更新：'
        + datetime.now().strftime('%Y-%m-%d %H:%M') + '</small></p></main>'
        '<footer>Powered by TrendRadar · GPL-3.0 · 数据每小时自动更新</footer>'
        '</body></html>'
    )
    return head + "\n".join(body) + foot + SEARCH_JS

def inject_nav(html_path):
    """幂等注入站点组件到快照页: 导航条 + 折叠按钮 + 页内搜索
    - 无导航的新页面: 注入全部组件
    - 已有导航的旧页面: 仅补插缺失组件(升级路径)
    """
    try:
        s = html_path.read_text(encoding="utf-8")
    except Exception:
        return False

    missing = []
    if "rdr-nav" not in s:
        missing.append(NAV_HTML)
    if "rdr-fold" not in s:
        missing.append(COLLAPSE_JS)
    if "rdr-search" not in s:
        missing.append(SEARCHBAR_JS)
    if not missing:
        return False

    inject = "".join(missing)
    m = re.search(r'(<body[^>]*>)', s)
    if m:
        s = s[:m.end()] + inject + s[m.end():]
    else:
        s = inject + s
    html_path.write_text(s, encoding="utf-8")
    return True

def inject_prevnext():
    """为同一日期目录内的快照页注入 上一时刻/下一时刻 导航（幂等：rdr-pn 标记）"""
    count = 0
    for d in sorted(BASE.iterdir()):
        if not (d.is_dir() and DATE_RE.match(d.name)):
            continue
        files = sorted(d.glob("*.html"))
        if len(files) < 2:
            continue
        for i, f in enumerate(files):
            try:
                s = f.read_text(encoding="utf-8")
            except Exception:
                continue
            if "rdr-pn" in s:
                continue
            prev_f = files[i - 1] if i > 0 else None          # 时间正序时前一个=更早
            next_f = files[i + 1] if i < len(files) - 1 else None
            def label(x, t):
                return ('<a href="' + x.name + '" style="color:#79c0ff;text-decoration:none">'
                        + t + ' &#183; ' + x.stem.replace('-', ':') + '</a>') if x else \
                       ('<span style="opacity:.3">' + t + '</span>')
            bar = ('<div id="rdr-pn" style="position:fixed;bottom:18px;left:50%;transform:translateX(-50%);'
                   'z-index:9998;background:rgba(255,255,255,.85);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);'
                   'border:1px solid rgba(0,0,0,.08);color:#24292f;border-radius:999px;padding:8px 18px;font-size:13px;'
                   'display:flex;gap:10px;align-items:center;'
                   'font-family:-apple-system,\'PingFang SC\',sans-serif;box-shadow:0 4px 22px rgba(0,0,0,.12)">'
                   + label(next_f, '更晚 &#9654;') + '</div>')
            m = re.search(r'(<body[^>]*>)', s)
            if m:
                s = s[:m.end()] + bar + s[m.end():]
            else:
                s = bar + s
            f.write_text(s, encoding="utf-8")
            count += 1
    return count

def main():
    BASE.mkdir(parents=True, exist_ok=True)
    days = collect()
    injected = 0
    for d in sorted(BASE.iterdir()):
        if d.is_dir() and DATE_RE.match(d.name):
            for f in d.glob("*.html"):
                if inject_nav(f):
                    injected += 1
    for n in ("current.html", "daily.html", "incremental.html"):
        f = BASE / "latest" / n
        if f.exists() and inject_nav(f):
            injected += 1
    if injected:
        print(f"导航条已注入 {injected} 个页面")
    pn = inject_prevnext()
    if pn:
        print(f"时序导航已添加 {pn} 个页面")
    out = BASE / "archive.html"
    out.write_text(build_html(days), encoding="utf-8")
    print(f"归档页已生成: {out} ({len(days)} 天)")

if __name__ == "__main__":
    main()
