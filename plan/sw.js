// Keeps a copy of the app on the iPhone, so it opens with every computer off and
// shows the last calendar and tasks it saw (the data itself is in localStorage).
// The page is fetched fresh whenever a computer answers, so an update shows on the next open.
const CACHE = 'plan-v1';
const FILES = ['./', 'manifest.webmanifest', 'icon.png'];
const url = f => new URL(f, self.registration.scope).href;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// A computer that is off never answers rather than refusing, so the network gets
// a few seconds; an error page (tailscale serve's 502 while the agent is down) counts as no answer.
async function fresh(req) {
  const c = await caches.open(CACHE);
  const key = req.mode === 'navigate' ? url('./') : req.url;
  try {
    const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 3000);
    const r = await fetch(req.url, { cache: 'no-store', signal: ctl.signal });   // by URL: a navigation Request takes no options
    clearTimeout(timer);
    if (r.ok) { c.put(key, r.clone()); return r; }
    return (await c.match(key)) || r;
  } catch {
    return (await c.match(key)) || Response.error();
  }
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== location.origin) return;
  if (req.mode === 'navigate' || FILES.some(f => url(f) === req.url)) e.respondWith(fresh(req));
});
