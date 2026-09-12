// Минимальный service worker для установки PWA на Android/Chrome.
//
// Задача узкая и сознательно ограниченная: НЕ кэшировать медицинские
// данные (/api/*, /export.pdf) — они должны всегда идти в сеть, чтобы
// пользователь никогда не увидел устаревшие показатели офлайн.
// Кэшируется только статическая оболочка (иконки, сам HTML-шелл),
// чтобы приложение открывалось мгновенно и не показывало белый экран
// при плохой связи.

const CACHE_NAME = 'medical-diary-shell-v1';
const SHELL_URLS = [
  '/',
  '/manifest.json',
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(SHELL_URLS).catch(function () {
        // Не блокируем установку SW, если какой-то из URL не закэшировался
        // (например, "/" требует авторизации и вернул редирект на /login).
      });
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.filter(function (k) { return k !== CACHE_NAME; })
            .map(function (k) { return caches.delete(k); })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', function (event) {
  const url = new URL(event.request.url);

  // Медицинские данные и любые API-запросы — всегда напрямую в сеть,
  // без какого-либо кэша. Это осознанное ограничение, а не недосмотр.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/export.pdf')) {
    return;
  }

  // Только собственные статические GET-запросы пробуем отдать из кэша
  // при отсутствии сети (network-first, cache как запасной вариант).
  if (event.request.method !== 'GET') {
    return;
  }

  event.respondWith(
    fetch(event.request).catch(function () {
      return caches.match(event.request);
    })
  );
});
