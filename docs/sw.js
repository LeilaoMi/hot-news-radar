// Hot News Radar Service Worker
const CACHE = 'rdr-v2';

// 预缓存核心页面。
// 使用相对路径：站点既能通过自定义域名根路径访问，也能通过
// leilaomi.github.io/hot-news-radar/ 子路径访问；绝对路径在后者下会 404，
// 而 addAll 中任意一项失败都会导致整个 install 失败、SW 永远装不上。
const CORE = ['./', './reports/latest/current.html', './reports/archive.html', './editor.html'];

// 历史快照页（reports/YYYY-MM-DD/*.html）内容不再变化、几乎不会被二次访问，
// 若照单全收地缓存，Cache Storage 会随小时快照无限膨胀直至超出配额（配额写满后
// put 会静默失败、离线能力整体失效）。因此这类请求完全不接管，交给浏览器默认网络栈。
const SNAPSHOT_RE = /\/reports\/\d{4}-\d{2}-\d{2}\//;

self.addEventListener('install', e => {
  // 逐个 add 并吞掉单条失败：任一核心页面 404 不应让整个 SW 装不上
  e.waitUntil(
    caches
      .open(CACHE)
      .then(c => Promise.all(CORE.map(u => c.add(u).catch(() => null))))
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

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (SNAPSHOT_RE.test(url.pathname)) return;

  // HTML: network-first，离线时回落缓存
  if ((e.request.headers.get('accept') || '').includes('text/html')) {
    e.respondWith(
      fetch(e.request)
        .then(r => {
          if (r.ok) {
            const cp = r.clone();
            caches.open(CACHE).then(c => c.put(e.request, cp));
          }
          return r;
        })
        // 离线且未命中缓存时，用 Response.error() 明确失败，
        // 避免 respondWith(undefined) 抛 TypeError
        .catch(() => caches.match(e.request).then(hit => hit || Response.error()))
    );
    return;
  }

  // 静态资源: cache-first
  e.respondWith(
    caches.match(e.request).then(
      hit =>
        hit ||
        fetch(e.request).then(r => {
          if (r.ok) {
            const cp = r.clone();
            caches.open(CACHE).then(c => c.put(e.request, cp));
          }
          return r;
        })
    )
  );
});
