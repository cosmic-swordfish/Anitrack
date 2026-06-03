const CACHE = 'anitrack-v1';
const STATIC = [
  '/static/favicon.png',
  '/static/logo-512.png',
  '/static/manifest.json',
  '/static/glass.css',
  '/static/container.js',
  '/static/button.js',
];

// Install: cache static assets
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(STATIC))
  );
  self.skipWaiting();
});

// Activate: clean old caches
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch: network first, fall back to cache for static assets
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Always go network-first for API/dynamic routes
  if (url.pathname.startsWith('/al/') ||
      url.pathname.startsWith('/mal/') ||
      url.pathname.startsWith('/anilist') ||
      url.pathname === '/sync' ||
      url.pathname === '/diff') {
    e.respondWith(fetch(e.request));
    return;
  }

  // Static assets: cache first
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
