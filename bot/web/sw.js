// Minimal service worker: makes the terminal installable and lets the shell open offline.
// Network-first so updates show up immediately; live data always goes over the WebSocket.
const CACHE = "bot-terminal-v1";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icon.svg"];
self.addEventListener("install", e => e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL).catch(() => {}))));
self.addEventListener("activate", e => e.waitUntil(
  caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin || /\/(ws|mcp|healthz)$/.test(u.pathname)) return;
  e.respondWith(fetch(e.request).then(r => {
    const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); return r;
  }).catch(() => caches.match(e.request).then(r => r || caches.match("./"))));
});
