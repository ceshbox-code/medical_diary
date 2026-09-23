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

// ---------------------------------------------------------------- Web Push
// Уведомления о приёме лекарств и напоминания об измерениях. Текст приходит
// с сервера и уже нейтральный (без названий лекарств и значений).
// Каждое push-событие ОБЯЗАТЕЛЬНО показывает уведомление (требование браузеров,
// в первую очередь Safari), поэтому при любой ошибке разбора показывается
// запасной вариант.
self.addEventListener('push', function (event) {
  var data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = {};
  }
  if (!data || typeof data !== 'object') { data = {}; }
  var title = (typeof data.title === 'string' && data.title) ? data.title : 'Медицинский дневник';
  var options = {
    body: (typeof data.body === 'string') ? data.body : '',
    icon: '/android-icon-192x192.png',
    data: { tab: (data.tab === 'meds' || data.tab === 'input') ? data.tab : 'meds' }
  };
  // renotify допустим только вместе с tag, иначе showNotification бросает TypeError.
  if (typeof data.tag === 'string' && data.tag) {
    options.tag = data.tag;
    options.renotify = true;
  }
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var tab = (event.notification.data && event.notification.data.tab) || 'meds';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        if ('focus' in c) {
          c.postMessage({ type: 'open-tab', tab: tab });
          return c.focus();
        }
      }
      if (self.clients.openWindow) { return self.clients.openWindow('/?tab=' + tab); }
    })
  );
});
