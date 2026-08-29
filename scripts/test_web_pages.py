"""网页端实测：模拟 GitHub Pages 子路径部署 /hot-news-radar/
验证本轮修复：折叠按钮、相对路径导航、日期选择器、编辑器本地依赖、控制台无报错。
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8080/hot-news-radar"
results = []


def check(name, ok, detail=""):
    results.append((ok, name, detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  -- {detail}" if detail else ""))


def run(pw):
    browser = pw.chromium.launch()
    ctx = browser.new_context()
    page = ctx.new_page()

    errors, failed = [], []
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("requestfailed", lambda r: failed.append(f"{r.url} :: {r.failure}"))
    page.on("response", lambda r: failed.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)

    # ---------- 1. 首页 ----------
    print("\n== 1. 首页 index.html ==")
    page.goto(BASE + "/", wait_until="networkidle")
    check("标题正确", "Hot News Radar" in page.title(), page.title())
    cur = page.inner_text("#cur-meta")
    check("实时热榜条数已动态填充", "条" in cur, cur)
    arch = page.inner_text("#archive-meta")
    check("归档统计已动态填充", "快照" in arch or "天" in arch, arch)
    check("不再有 Unexpected token 'var'", not any("Unexpected token" in e for e in errors))

    # 图标
    m = page.evaluate("""async () => {
      const r = await fetch('manifest.json'); const j = await r.json();
      const i = await fetch(j.icons[0].src);
      return {ok: r.ok, name: j.name, start: j.start_url, icon: j.icons[0].src, iconOk: i.ok};
    }""")
    check("manifest.json 可访问", m["ok"], f"start_url={m['start']}")
    check("图标可访问", m["iconOk"], m["icon"])

    # 等 Service Worker 就绪并让预缓存跑完：
    # 否则后续快速跳转会打断 install 阶段的 c.add()，产生 ERR_ABORTED 假阳性。
    page.wait_for_function(
        "() => navigator.serviceWorker.controller !== null || "
        "!('serviceWorker' in navigator)",
        timeout=15000,
    )
    page.wait_for_timeout(2500)
    sw = page.evaluate("""async () => {
      const reg = await navigator.serviceWorker.getRegistration();
      return reg && reg.active ? reg.active.state : 'none';
    }""")
    check("Service Worker 已激活", sw == "activated", sw)

    # ---------- 2. 归档页 ----------
    print("\n== 2. 归档页 archive.html ==")
    page.goto(BASE + "/reports/archive.html", wait_until="networkidle")
    page.wait_for_timeout(1200)  # 给 SW 后台 fetch/caches.put 留时间，避免打断 in-flight 请求
    label = page.inner_text("label[for='q']") if page.locator("label[for='q']").count() else ""
    check("日期选择器有中文说明", "按日期定位" in label, label)
    older = page.locator("a[href='older.html']")
    check("更早归档入口存在且文案已改",
          older.count() > 0 and "90 天以前" in older.first.inner_text(),
          older.first.inner_text() if older.count() else "缺失")

    # 导航条相对路径可用性
    nav = page.evaluate("""async () => {
      const as = [...document.querySelectorAll('#rdr-nav a')];
      const out = [];
      for (const a of as) {
        const r = await fetch(a.getAttribute('href'));
        await r.text();  // 读完响应体，避免跳转时记为 ERR_ABORTED 假阳性
        out.push(a.getAttribute('href') + ' -> ' + r.status);
      }
      return out;
    }""")
    check("导航条全部链接可访问", all("-> 200" in n for n in nav), "; ".join(nav))

    # ---------- 3. 快照页（折叠按钮是重点） ----------
    print("\n== 3. 快照页（折叠按钮 / 搜索条） ==")
    # 动态取归档页里的第一个快照链接，避免把日期写死在测试里
    snap_href = page.evaluate(
        """() => {
             const a = document.querySelector('a.t');
             return a ? a.getAttribute('href') : null;
           }"""
    )
    check("能从归档页定位到快照页", bool(snap_href), snap_href or "无")
    if not snap_href:
        browser.close()
        return
    page.goto(BASE + "/reports/" + snap_href, wait_until="networkidle")
    page.wait_for_timeout(1200)
    fold = page.locator("#rdr-fold")
    check("折叠按钮已渲染（此前从未出现）", fold.count() > 0 and fold.is_visible(),
          fold.inner_text() if fold.count() else "不存在")
    if fold.count():
        fold.click()
        txt = page.inner_text("#rdr-fold")
        check("点击后文案切换为『展开全部分组』", "展开全部分组" in txt, txt)
        fold.click()
        check("再次点击可切回", "折叠分组" in page.inner_text("#rdr-fold"))
    check("页内搜索条存在", page.locator("#rdr-q").count() > 0)
    nav2 = page.evaluate("""async () => {
      const as = [...document.querySelectorAll('#rdr-nav a')];
      const out = [];
      for (const a of as) { const r = await fetch(a.getAttribute('href')); await r.text(); out.push(a.getAttribute('href') + ' -> ' + r.status); }
      return out;
    }""")
    check("快照页导航条链接可访问（相对前缀正确）", all("-> 200" in n for n in nav2), "; ".join(nav2))

    # ---------- 4. 编辑器 ----------
    print("\n== 4. 配置编辑器 editor.html ==")
    page.goto(BASE + "/editor.html", wait_until="networkidle")
    page.wait_for_timeout(1200)
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    check("Tailwind 已生效（本地 vendor，非 CDN）", bg == "rgb(243, 244, 246)", f"body bg = {bg}")
    fa = page.evaluate("""() => {
      const el = document.querySelector('.fa-solid, .fa-brands, .fa-regular');
      if (!el) return 'no-icon';
      return getComputedStyle(el, '::before').fontFamily || 'none';
    }""")
    check("FontAwesome 字体已本地加载", "Font Awesome" in fa, fa)
    check("编辑器无外部 CDN 请求",
          not any(("cdn.tailwindcss.com" in f) or ("cdnjs.cloudflare.com" in f) for f in failed),
          "")
    check("配置源已指向本仓库",
          page.evaluate("typeof REPO_NAME !== 'undefined' && REPO_NAME") == "hot-news-radar")
    fetch_ok = page.evaluate("""async () => {
      try { const r = await fetchRepoFile('version_configs'); const t = await r.text();
            return 'OK: ' + t.split('\\n')[0]; } catch (e) { return 'ERR: ' + e.message; }
    }""")
    print(f"  [INFO] 远程配置拉取（需外网）: {fetch_ok[:120]}")

    # ---------- 汇总 ----------
    print("\n== 控制台 / 网络 ==")
    real_errors = [e for e in errors if "favicon" not in e.lower()]
    check("无 JS 报错", len(real_errors) == 0, " | ".join(real_errors[:5]))
    bad = [f for f in failed if "favicon" not in f.lower()]
    check("无 404 / 请求失败", len(bad) == 0, " | ".join(bad[:5]))

    browser.close()


with sync_playwright() as pw:
    run(pw)

n_fail = sum(1 for ok, _, _ in results if not ok)
print(f"\n{'='*60}\n通过 {len(results)-n_fail}/{len(results)}")
sys.exit(1 if n_fail else 0)
