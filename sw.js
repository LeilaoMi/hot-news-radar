// Hot News Radar Service Worker
//
// 缓存名带构建版本：CI 的 Publish 步骤会把 4642c6399694 替换成
// 「日期-run_id」。每次发布 sw.js 内容都会变化，浏览器据此安装新 SW，
// activate 时清掉旧版本缓存，老用户也能拿到新的编辑器脚本/样式（审计 3.3）。
// 本地直接打开时保留占位符，退化为 rdr-dev。
const BUILD = '4642c6399694';
const CACHE = 'rdr-' + (BUILD.indexOf('__') === 0 ? 'dev' : BUILD);

// 预缓存核心页面（只为离线兜底；在线时这些页面一律走网络）。
// 使用相对路径：站点既能通过自定义域名根路径访问，也能通过
// leilaomi.github.io/hot-news-radar/ 子路径访问；绝对路径在后者下会 404。
const CORE = ['./', './reports/latest/current.html', './reports/archive.html', './editor.html'];

// 历史快照页（reports/YYYY-MM-DD/*.html）内容不再变化、几乎不会被二次访问，
// 若照单全收地缓存，Cache Storage 会随小时快照无限膨胀直至超出配额。
// 因此这类请求完全不接管，交给浏览器默认网络栈。
const SNAPSHOT_RE = /\/reports\/\d{4}-\d{2}-\d{2}\//;

// 需要「网络优先」的请求：
//   - 页面导航与任何 HTML（含 index.html 里 fetch() 的 reports/latest/current.html、
//     reports/archive.html —— fetch() 默认 Accept 是 */*，旧实现会走 cache-first，
//     导致首页计数永远停在安装 SW 那一刻）
//   - reports/ 下的一切、以及 json/xml 数据文件（feed、manifest）
function isNetworkFirst(req, url) {
  if (req.mode === 'navigate') return true;
  if ((req.headers.get('accept') || '').includes('text/html')) return true;
  if (/\.(html?|json|xml)$/i.test(url.pathname) || url.pathname.endsWith('/')) return true;
  if (url.pathname.indexOf('/reports/') !== -1) return true;
  return false;
}

self.addEventListener('install', e => {
  // 逐个 add 并吞掉单条失败：任一核心页面 404 不应让整个 SW 装不上
  e.waitUntil(
    caches
      .open(CACHE)
      .then(c => Promise.all(CORE.map(u => c.add(new Request(u, { cache: 'reload' })).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches
      .keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function putInCache(req, resp) {
  if (resp && resp.ok && resp.type === 'basic') {
    const cp = resp.clone();
    caches.open(CACHE).then(c => c.put(req, cp)).catch(() => {});
  }
  return resp;
}

self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (SNAPSHOT_RE.test(url.pathname)) return;

  if (isNetworkFirst(req, url)) {
    // network-first：在线永远拿最新；离线回落缓存；都没有时明确失败
    e.respondWith(
      fetch(req)
        .then(r => putInCache(req, r))
        .catch(() => caches.match(req).then(hit => hit || Response.error()))
    );
    return;
  }

  // 静态资源（js/css/图片/字体）：stale-while-revalidate
  // 先用缓存秒开，同时后台拉新版本更新缓存，下次访问即生效。
  e.respondWith(
    caches.match(req).then(hit => {
      const net = fetch(req).then(r => putInCache(req, r)).catch(() => null);
      if (hit) {
        e.waitUntil(net);
        return hit;
      }
      return net.then(r => r || Response.error());
    })
  );
});
