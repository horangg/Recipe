// 앱 껍데기(HTML/아이콘)를 기기에 저장해 두고 즉시 보여준다. 서버가 잠들어 있어도 화면은 바로 열리고, 뒤에서 최신본으로 갱신한다.
// 데이터(/api)는 건드리지 않는다.
const C = 'shell-v1', SHELL = ['/', '/manifest.json', '/icon.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(C).then(c => c.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin || !SHELL.includes(u.pathname)) return;
  e.respondWith(caches.open(C).then(async c => {
    const hit = await c.match(u.pathname);  // ?url=, ?token= 같은 쿼리는 무시하고 같은 껍데기를 쓴다
    const net = fetch(e.request).then(r => { if (r.ok) c.put(u.pathname, r.clone()); return r; }).catch(() => hit);
    return hit || net;
  }));
});
