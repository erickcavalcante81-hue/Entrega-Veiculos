/* Braga Veículos — Service Worker (PWA)
   Estratégia NETWORK-FIRST: quando há internet, sempre busca a versão mais
   recente do app (assim as atualizações chegam sozinhas); sem internet, cai
   no cache para continuar funcionando offline.
   Requisições de outras origens (Firebase/Firestore, gstatic, CDN do PDF.js)
   passam direto pela rede. */
const CACHE = 'braga-v58';
const CORE = [
  './',
  './index.html',
  './manifest.webmanifest',
  './firebase-config.js',
  './cloud-sync.js',
  './gae-logo.png',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(CORE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // Só tratamos recursos da própria origem; Firebase/CDN vão direto pela rede.
  if (url.origin !== self.location.origin) return;
  // Network-first: busca a versão nova; se falhar (offline), usa o cache.
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req).then((cached) => cached || caches.match('./index.html')))
  );
});
