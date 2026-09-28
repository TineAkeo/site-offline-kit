# site-offline-kit

Turn a website you own into an **installable, offline app** for iPads, phones
and kiosks. Point it at a URL, pick where it will be hosted, and it builds a
folder that saves the whole site on first visit and then works with no internet.

Built and tested on Webflow sites. It needs only Python 3.8+, with no installs.

```bash
python3 offline_site.py https://www.example.com                         # local / Netlify / Cloudflare Pages
python3 offline_site.py https://www.example.com --target webflow-cloud  # Webflow Cloud app at /app
```

The output goes to `out/<site>-<target>/`, or wherever `--out` points.

## What it does

1. **Crawls every page** it can reach from the home page. That includes links
   in scripts, such as `location.replace('/chapter1')`.
2. **Saves the files pages use** from the site and common CDNs: Webflow's file
   server, jsDelivr, cdnjs, unpkg, Google Fonts and others. Pages are rewritten
   to use the saved copies. Webflow's font loader (`WebFont.load`) is handled
   too.
3. **Removes what can't work offline:**
   - Analytics and trackers (Google Tag Manager/Analytics, Hotjar, Clarity,
     Meta, LinkedIn, HubSpot tracking…).
   - Cookie banners (CookieYes, Cookiebot, OneTrust…).
   - Iframes pointing at other sites (YouTube, Vimeo, maps, 3D tours, forms)
     and the scripts that drive them (HubSpot forms, Calendly, Typeform…).
   - The empty Webflow wrappers those leave behind.
4. **Adds offline support:**
   - `sw.js`, a service worker that saves everything, answers the byte-range
     requests iPad Safari makes for video, and handles hosts that redirect
     `page.html` → `page`.
   - `offline.js`, which shows a "Saving for offline… N / M" → "Ready
     offline ✓" badge.
   - `manifest.json`, so the site can be added to the Home Screen as an app.
   - A `noindex` tag, so the copy stays out of search engines.
5. **Cleans up.** Rebuilding into the same folder deletes files the new build
   no longer uses. Dotfiles (`.git`) and `README.md` are never touched.

Animations (GSAP, Webflow interactions, Lenis, Swiper, Finsweet) keep working;
they're just scripts that get saved like everything else.

## Targets

| | `local` (default) | `webflow-cloud` |
|---|---|---|
| Served at | a domain root | a mount path, `--base` (default `/app`) |
| Images & video | downloaded into the folder | left on Webflow's file server, saved by the service worker on first visit |
| Size | the whole site (can be 100s of MB) | pages and scripts only (a few MB) |
| Extra files | `robots.txt`, `_headers` (Netlify / Cloudflare Pages) | `webflow.json` (static app) |
| Other | | renames `@` in paths (Webflow Cloud returns 404 for them) and checks the 100 MB limit |

### Test a `local` build

```bash
cd out/example.com-local && python3 -m http.server 8000
```

Open http://localhost:8000/ and wait for "Ready offline ✓". You can then stop
the server and the site still loads in that browser. Don't open `index.html`
by double-clicking; it has to be served.

### Deploy a `webflow-cloud` build

1. Put the output folder in a GitHub repo, with `index.html` and
   `webflow.json` at the top level. To build straight into your repo checkout:
   `--out ~/path/to/repo`.
2. In Webflow: **Apps → Webflow Cloud → Create new app**. Pick the repo and
   choose **Existing site**. Set the mount path to match `--base` (`/app`),
   then Deploy.
3. Every push to the branch redeploys. Installed devices update the next time
   they open the app online.

The "No package.json found" warning in Webflow's deploy form is expected for
static apps.

## Installing on an iPad

1. Open the site in Safari on Wi-Fi, e.g. `https://yoursite.com/app/`.
2. Share → **Add to Home Screen**, then open it from the icon.
3. Wait for **Ready offline ✓**.
4. Test it: turn on Airplane Mode, force-quit, reopen from the icon.

To test a fresh download:
- **Chrome:** DevTools → Application → *Clear site data*.
- **iPad:** delete the Home Screen icon (it has its own storage), or go to
  Settings → Apps → Safari → Advanced → Website Data.

## Per-site tweaks

Some things are specific to one site, like a "Watch video" button whose video
was removed, or a whole section that only launches a 3D tour. Put rules for
them in `sites/<host>.json`, e.g. `sites/example.com.json` (without `www.`).
It's loaded automatically, or you can pass `--config file.json`.

```json
{
  "remove_sections_containing": ["id=\"open-tour\""],
  "remove_elements": [["<div class=\"popup-video\"", "div"]],
  "remove_patterns": ["<a\\b[^>]*data-virtual=\"open\"[^>]*>.*?</a>"]
}
```

- `remove_sections_containing`: removes the whole `<section>` around this text.
- `remove_elements`: `[opening-tag regex, tag name]`; removes that element and
  everything inside it.
- `remove_patterns`: regexes removed from the HTML.

`sites/reframe.systems.json` is a worked example.

## Options

| Option | |
|---|---|
| `--target local\|webflow-cloud` | where it will be hosted |
| `--out DIR` | output folder |
| `--base /path` | URL path it's served under (webflow-cloud default `/app`) |
| `--remote-media` | also leave Webflow media on the CDN for a `local` build |
| `--name "App name"` | Home Screen name (default: from the site's title) |
| `--asset-host HOST` | also save files from this host (repeatable) |
| `--keep-embeds` | keep YouTube/Vimeo/maps/form embeds (they only work online) |
| `--allow-indexing` | don't add `noindex` |
| `--max-pages N` | crawl limit (default 500) |
| `--config FILE` | per-site rules (default `sites/<host>.json`) |

## Limits worth knowing

- **It's a snapshot.** Rebuild and redeploy after changing the site.
  Rebuild too after deleting or replacing images in Webflow, since
  `webflow-cloud` builds point at the file addresses that existed at build
  time.
- **Pages must be reachable** from the home page, by links or by paths
  written in scripts. Password-protected pages and anything that needs a
  server (search, form submissions, logins, e-commerce checkout) won't work
  offline.
- **Pagination addresses** like `?page=2` serve the first page's saved copy
  offline.
- **One version of each video is saved.** A `.webm` is skipped when an `.mp4`
  twin exists, since every current browser plays mp4.
- The first visit downloads everything; for media-heavy sites that can take
  a minute or two on good Wi-Fi.
- Only use it on sites you own or have permission to copy.
