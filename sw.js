// 앱 껍데기(HTML/아이콘) 캐시: 서버가 응답하면 항상 최신본을 쓰고(배포 즉시 반영),
// 2.5초 안에 응답이 없을 때만(서버가 잠들어 있을 때) 저장본을 먼저 보여준다. 데이터(/api)는 건드리지 않는다.
const C = 'shell-v2', SHELL = ['/', '/manifest.json', '/icon.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(C).then(c => c.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin || !SHELL.includes(u.pathname)) return;
  e.respondWith(caches.open(C).then(c => {
    const hit = () => c.match(u.pathname);  // ?url=, ?token= 같은 쿼리는 무시하고 같은 껍데기를 쓴다
    const net = fetch(e.request).then(r => { if (r.ok) c.put(u.pathname, r.clone()); return r; });
    const slow = new Promise(res => setTimeout(async () => res((await hit()) || net), 2500));
    return Promise.race([net.catch(async () => (await hit()) || Promise.reject(new Error('offline'))), slow]);
  }));
});
