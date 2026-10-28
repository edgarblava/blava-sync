// Blava Sync : met tous les fichiers en cache pour un fonctionnement 100 % hors ligne.
// Changer VERSION à chaque mise à jour de l'app pour forcer le rafraîchissement.
const VERSION = "blava-sync-v2";
const FILES = [
  "./", "index.html", "manifest.webmanifest", "qrcode.js",
  "icons/icon-192.png", "icons/icon-512.png", "icons/icon-maskable-512.png",
  "icons/apple-touch-icon.png", "icons/favicon-32.png",
  "fonts/inter-tight-latin-400-normal.woff2", "fonts/inter-tight-latin-500-normal.woff2",
  "fonts/inter-tight-latin-600-normal.woff2", "fonts/geist-mono-latin-400-normal.woff2",
  "fonts/geist-mono-latin-500-normal.woff2"
];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))
  ).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  e.respondWith(caches.match(e.request, { ignoreSearch: true }).then((hit) => hit || fetch(e.request)));
});
