// Offline setup for sites built with site-offline-kit (offline_site.py).
//
// Which devices save the whole site for offline use is set by the build
// (data-offline on this script's tag):
//   "all"        every visitor (local builds, meant for this machine)
//   "installed"  only devices you set up (webflow-cloud builds): the site
//                opened as an installed app (Home Screen icon, desktop app
//                window), or any browser opened once with ?offline=1.
//                Everyone else just browses the site online.
// ?offline=0 removes the saved copy from a device again.
//
// While saving, a badge shows "Saving for offline… N / M", then "Ready
// offline ✓". Progress is read from the cache itself, so it also works on
// pages just outside the worker's scope (Webflow Cloud serves the app's home
// page at "/app" while the scope is "/app/").
(function () {
  if (!('serviceWorker' in navigator) || !window.caches) return;
  var script = document.currentScript;
  var here = (script && script.src) || location.href;
  var mode = (script && script.getAttribute('data-offline')) || 'all';
  var swUrl = new URL('sw.js', here).href;
  var listUrl = new URL('precache.json', here).href;
  var scope = new URL('./', swUrl).pathname;          // e.g. "/app/"
  var param = new URLSearchParams(location.search).get('offline');
  // A setup link whose page redirected before this script ran: the build adds
  // a tiny script at the top of <head> that remembers ?offline= for the tab.
  try {
    if (param === null) param = sessionStorage.getItem('offline-setup');
    sessionStorage.removeItem('offline-setup');
  } catch (_) {}

  var badge;
  function show(text, done) {
    if (!badge) {
      badge = document.createElement('div');
      badge.style.cssText =
        'position:fixed;left:12px;bottom:12px;z-index:2147483647;padding:8px 12px;' +
        'border-radius:8px;font:500 13px/1.2 -apple-system,system-ui,sans-serif;' +
        'color:#fff;background:rgba(20,20,20,.85);pointer-events:none;transition:opacity .4s';
      document.body.appendChild(badge);
    }
    badge.textContent = text;
    badge.style.opacity = '1';
    if (done) setTimeout(function () { badge.style.opacity = '0'; }, 4000);
  }

  function shownOnce() {
    try {
      if (sessionStorage.getItem('offline-ready-shown')) return true;
      sessionStorage.setItem('offline-ready-shown', '1');
    } catch (_) {}
    return false;
  }

  function installed() {
    return navigator.standalone === true ||                   // iOS Home Screen
      ['standalone', 'fullscreen', 'minimal-ui', 'window-controls-overlay']
        .some(function (m) { return matchMedia('(display-mode: ' + m + ')').matches; });
  }

  // Tidy the address bar after ?offline=1 / ?offline=0.
  function dropParam() {
    if (param === null) return;
    var u = new URL(location.href);
    u.searchParams.delete('offline');
    history.replaceState(null, '', u.pathname + u.search + u.hash);
  }

  // Set-up devices opening ".../app" (no slash, outside the scope) move to
  // ".../app/index.html", the address that loads offline.
  function intoScope() {
    if (location.pathname + '/' === scope) location.replace(scope + 'index.html' + location.hash);
  }

  async function watch(reg) {
    var list;
    try { list = await (await fetch(listUrl, { cache: 'no-store' })).json(); }
    catch (_) { return; } // offline: the copy is already saved, nothing to report
    var total = list.files.length;
    for (;;) {
      var done = 0;
      if (await caches.has(list.cache)) done = (await (await caches.open(list.cache)).keys()).length;
      var installing = !!(reg.installing || reg.waiting);
      if (done >= total) {
        if (!shownOnce()) show('Ready offline ✓', true);
        return;
      }
      if (!installing && reg.active && done > 0) {
        show('Saved, but ' + (total - done) + ' files failed — reload to retry', true);
        return;
      }
      show('Saving for offline… ' + done + ' / ' + total);
      await new Promise(function (r) { setTimeout(r, 1500); });
    }
  }

  async function removeCopy() {
    var reg = await navigator.serviceWorker.getRegistration(scope);
    if (reg) await reg.unregister();
    var list;
    try { list = await (await fetch(listUrl, { cache: 'no-store' })).json(); } catch (_) {}
    for (var k of await caches.keys()) {
      if (list && k.indexOf(list.app + '-') === 0) await caches.delete(k);
    }
    dropParam();
    show('Offline copy removed from this device', true);
  }

  window.addEventListener('load', async function () {
    if (param === '0') return removeCopy();
    var existing = await navigator.serviceWorker.getRegistration(scope);
    var wanted = mode === 'all' || param === '1' || installed() || !!existing;
    if (!wanted) return; // a visitor just browsing online: no download, no badge
    dropParam();
    try {
      var reg = await navigator.serviceWorker.register(swUrl);
      if (reg.active) intoScope();
      await watch(reg);
      intoScope();
    } catch (_) {}
  });
})();
