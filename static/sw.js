/* Family Shopping List service worker.
 *
 * Strategy:
 *   - Static assets (CSS / JS / icons / manifest / stock svg): cache-first.
 *   - Recipe view pages, recipes listing: stale-while-revalidate so the
 *     phone shows the last-known version instantly and updates on next load.
 *   - Shopping-list home page: network-first (we want fresh check state).
 *   - POST/PUT/DELETE: never intercept; let them fail naturally if offline.
 *
 * Bump CACHE_VERSION when the shell changes so old caches are evicted.
 */
const CACHE_VERSION = "v23";
const SHELL_CACHE = `shell-${CACHE_VERSION}`;
const PAGE_CACHE  = `pages-${CACHE_VERSION}`;
const IMAGE_CACHE = `images-${CACHE_VERSION}`;

const SHELL_ASSETS = [
  "/static/style.css",
  "/static/app.js",
  "/static/manifest.json",
  "/static/stock-recipe.svg",
  "/static/beep.wav",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => !k.endsWith(CACHE_VERSION)).map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

function isStaticAsset(url) {
  return url.pathname.startsWith("/static/");
}
function isHomePage(url) {
  return url.pathname === "/" || url.pathname === "";
}
function isUploadedImage(url) {
  return url.pathname.startsWith("/static/uploads/");
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Cache-first for static assets — they're versioned by filename via the
  // caches we busted on activate.
  if (isStaticAsset(url) && !isUploadedImage(url)) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(SHELL_CACHE).then((c) => c.put(req, copy));
        return res;
      }))
    );
    return;
  }

  // Uploaded recipe photos: cache-first with background refresh.
  if (isUploadedImage(url)) {
    event.respondWith(
      caches.match(req).then((hit) => {
        const network = fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(IMAGE_CACHE).then((c) => c.put(req, copy));
          return res;
        }).catch(() => hit);
        return hit || network;
      })
    );
    return;
  }

  // Home page (the shopping list): network-first so checks stay fresh.
  if (isHomePage(url)) {
    event.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(PAGE_CACHE).then((c) => c.put(req, copy));
        return res;
      }).catch(() => caches.match(req).then((hit) => hit || offlineFallback()))
    );
    return;
  }

  // Recipes listing and edit/new forms: network-first so a freshly saved
  // recipe shows up immediately. The cached copy is only used if the
  // network is unavailable. Also covers the meal-plan calendar.
  const isListingOrForm =
    url.pathname === "/recipes" ||
    url.pathname === "/plan" ||
    url.pathname.endsWith("/edit") ||
    url.pathname.endsWith("/new");
  if (isListingOrForm) {
    event.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(PAGE_CACHE).then((c) => c.put(req, copy));
        return res;
      }).catch(() => caches.match(req).then((hit) => hit || offlineFallback()))
    );
    return;
  }
  // Individual recipe pages (cook view): stale-while-revalidate so they
  // open instantly and update in the background.
  if (url.pathname.startsWith("/recipes/")) {
    event.respondWith(
      caches.match(req).then((hit) => {
        const network = fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(PAGE_CACHE).then((c) => c.put(req, copy));
          return res;
        }).catch(() => hit || offlineFallback());
        return hit || network;
      })
    );
    return;
  }
  // Everything else: pass through (POSTs, /list/* mutations).
});

function offlineFallback() {
  return new Response(
    "<!doctype html><meta charset=utf-8><title>Offline</title>" +
    "<style>body{font-family:sans-serif;padding:32px;text-align:center;color:#374151}</style>" +
    "<h1>Offline</h1><p>This page hasn't been opened on this device while online yet.</p>" +
    "<p><a href=\"/\">Try the home page</a></p>",
    { headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}
