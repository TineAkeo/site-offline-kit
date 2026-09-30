#!/usr/bin/env python3
"""offline_site.py - turn a website you own into an installable offline app.

Crawls every page of the site, saves pages and the files they use, strips
trackers, cookie banners and third-party embeds that can't work offline, and
adds a service worker so the site installs to an iPad/phone Home Screen and
keeps working with no internet.

    python3 offline_site.py https://www.example.com                        # local folder
    python3 offline_site.py https://www.example.com --target webflow-cloud # static app at /app

Targets
  local          Everything downloaded; serve the folder at a domain root
                 (python3 -m http.server, Netlify, Cloudflare Pages...).
  webflow-cloud  Static Webflow Cloud app under --base (default /app). Images
                 and video stay on Webflow's CDN to fit the 100 MB deploy
                 limit; the service worker still saves them for offline.

Per-site tweaks (extra things to remove) go in sites/<host>.json, loaded
automatically, or pass --config. Standard library only; Python 3.8+.
"""
import argparse, gzip, hashlib, html as htmlmod, io, json, os, re, shutil, sys, tarfile, threading, zlib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urljoin, unquote, quote
from urllib.request import Request, urlopen

KIT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# Hosts whose files pages use are saved (or, with remote media, left on the CDN).
CDN_HOSTS = [
    "cdn.prod.website-files.com", "assets-global.website-files.com",
    "uploads-ssl.webflow.com", "d3e54v103j8qbb.cloudfront.net",
    "cdnjs.cloudflare.com", "unpkg.com", "cdn.jsdelivr.net", "ajax.googleapis.com",
    "code.jquery.com", "fonts.googleapis.com", "fonts.gstatic.com",
]
WEBFLOW_MEDIA_HOSTS = {"cdn.prod.website-files.com", "assets-global.website-files.com",
                       "uploads-ssl.webflow.com"}
PAGE_EXTS = {"", ".html", ".htm"}

# --- Things that can't work offline -------------------------------------------------
TRACKERS = (r"googletagmanager|google-analytics|gtag\(|dataLayer\.push|hotjar|clarity\.ms|"
            r"cdn\.segment|analytics\.js|connect\.facebook\.net|fbq\(|snap\.licdn\.com|"
            r"linkedin\.com/insight|js\.hs-scripts|js\.hs-analytics|js\.hs-banner|"
            r"cookieyes|cookiebot|cookielaw|onetrust|termly|iubenda|b2bjsstore|"
            r"plausible\.io|googleadservices|doubleclick|analytics\.tiktok|static\.ads-twitter")
EMBED_APIS = (r"player\.vimeo\.com|Vimeo\.Player|youtube\.com/iframe_api|YT\.Player|"
              r"hsforms\.net|hbspt\.forms|assets\.calendly\.com|Calendly\.|embed\.typeform|"
              r"fast\.wistia|forms\.copper\.com")

def _script_re(words):
    return re.compile(
        r'<script\b[^>]*\bsrc="[^"]*(?:%s)[^"]*"[^>]*>\s*</script>'
        r'|<script\b[^>]*>(?:(?!</script>).)*?(?:%s)(?:(?!</script>).)*</script>' % (words, words),
        re.S | re.I)

TRACKER_RE = re.compile(
    _script_re(TRACKERS).pattern +
    r'|<noscript>\s*<(?:iframe|img)[^>]*(?:googletagmanager|facebook\.com/tr|licdn)[^>]*>\s*(?:</iframe>)?\s*</noscript>'
    r'|<link\b[^>]*(?:cookieyes|cookiebot|onetrust)[^>]*>', re.S | re.I)
EMBED_SCRIPT_RE = _script_re(EMBED_APIS)

MAX_PAGES_DEFAULT = 500

# --- Build state (set in main) ------------------------------------------------------
OUT = None            # output folder
BASE = ""             # URL prefix, e.g. "/app"
SITE_HOSTS = set()    # the site's own host(s)
START = None          # start URL
REMOTE_HOSTS = set()  # CDN hosts left as absolute URLs
NO_AT = False         # rename "@" in paths (Webflow Cloud 404s them)
ASSET_HOSTS = list(CDN_HOSTS)
CONFIG = {}
KEEP_EMBEDS = False

lock = threading.Lock()
asset_map = {}        # absolute url -> local href
remote_urls = set()   # CDN URLs for the service worker to save
produced = set()      # local paths written/kept this build (for pruning)
failed = []


# --- Fetching and local paths -------------------------------------------------------
def fetch(url):
    req = Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    with urlopen(req, timeout=60) as r:
        data = r.read()
        enc = r.headers.get("Content-Encoding", "")
        if enc == "gzip":
            data = gzip.decompress(data)
        elif enc == "deflate":
            data = zlib.decompress(data)
        return data, r.headers.get("Content-Type", ""), r.geturl()


def local_asset_path(url, ctype=""):
    parts = urlsplit(url if not url.startswith("//") else "https:" + url)
    path = unquote(parts.path)
    if path.endswith("/"):
        path += "index"
    if "." not in os.path.basename(path):
        for key, ext in (("javascript", ".js"), ("css", ".css"), ("json", ".json")):
            if key in ctype:
                path += ext
                break
    if parts.query:  # e.g. Google Fonts css2?family=...: one file per distinct query
        stem, ext = os.path.splitext(path)
        path = f"{stem}-{hashlib.sha1(parts.query.encode()).hexdigest()[:8]}{ext}"
    if NO_AT:
        path = path.replace("@", "_at_")
    return "/assets/" + parts.netloc + path


def href_for(local):
    return BASE + quote(local, safe="/")


def save(local, data):
    fs = os.path.join(OUT, local.lstrip("/"))
    os.makedirs(os.path.dirname(fs), exist_ok=True)
    with open(fs, "wb") as f:
        f.write(data)
    with lock:
        produced.add(local)


def is_page_path(path):
    return os.path.splitext(unquote(path))[1].lower() in PAGE_EXTS


def norm_page(path):
    p = unquote(path)
    p = re.sub(r"\.html?$", "", p, flags=re.I).rstrip("/")
    if p.endswith("/index"):
        p = p[:-6]
    return p or "/"


def page_local(norm):
    return "/index.html" if norm == "/" else norm + ".html"


# --- Assets -------------------------------------------------------------------------
HOSTS_RE = None
ASSET_URL_RE = None
CSS_URL_RE = re.compile(
    r'url\(\s*(?:"([^"]*)"|\'([^\']*)\'|((?:\\.|[^\'")\s\\])+))\s*\)|@import\s+([\'"])([^\'"]+)\4')


def build_asset_regex():
    global HOSTS_RE, ASSET_URL_RE
    HOSTS_RE = "|".join(re.escape(h) for h in ASSET_HOSTS)
    # URL chars, allowing balanced "(1)" and CSS-escaped "\(1\)" in file names,
    # but not JS template syntax like ${n}.
    ASSET_URL_RE = re.compile(
        r'(?:https?:)?//(?:%s)/(?:[^\s"\'<>,\\&$`{}()]|\\[()]|\([^\s"\'<>()]*\))+' % HOSTS_RE)


def asset_host_ok(host):
    return host in ASSET_HOSTS or host in SITE_HOSTS


def note_remote(absu, css_text=None):
    """Record a URL that stays on its CDN; follow url()s inside remote CSS."""
    with lock:
        if absu in remote_urls:
            return
        remote_urls.add(absu)
    if css_text is None and absu.endswith(".css"):
        try:
            css_text = fetch(absu)[0].decode("utf-8", "replace")
        except Exception as e:
            failed.append((absu, str(e)))
            return
    for m in CSS_URL_RE.finditer(css_text or ""):
        ref = m.group(1) or m.group(2) or m.group(3) or m.group(5) or ""
        if ref and not ref.startswith(("data:", "#")):
            u = urljoin(absu, re.sub(r"\\(.)", r"\1", ref))
            if asset_host_ok(urlsplit(u).netloc):
                note_remote(u)


def download_asset(url):
    """Save one asset (recursing into CSS). Returns the href to use for it."""
    absu = "https:" + url if url.startswith("//") else url
    absu = absu.replace("\\(", "(").replace("\\)", ")")
    if urlsplit(absu).netloc in REMOTE_HOSTS:
        note_remote(absu)
        return url  # leave the reference exactly as written
    with lock:
        if absu in asset_map:
            return asset_map[absu] or href_for(local_asset_path(absu))
        asset_map[absu] = None  # reserve
    existing = local_asset_path(absu)
    fs = os.path.join(OUT, existing.lstrip("/"))
    if not re.search(r"\.(css|js)$", existing) and "?" not in absu and os.path.isfile(fs):
        with lock:
            produced.add(existing)
            asset_map[absu] = href_for(existing)
        return asset_map[absu]
    try:
        data, ctype, final = fetch(absu)
    except Exception as e:
        failed.append((absu, str(e)))
        with lock:
            asset_map[absu] = href_for(existing)
        return asset_map[absu]
    local = local_asset_path(absu, ctype)
    if "css" in ctype or local.endswith(".css"):
        data = rewrite_css(data.decode("utf-8", "replace"), final).encode("utf-8")
    elif "javascript" in ctype or local.endswith(".js"):
        js = rewrite_abs_assets(data.decode("utf-8", "replace"))
        if NO_AT:  # Finsweet builds `attributes-${name}@${version}/` at runtime
            js = re.sub(r"(attributes-\$\{\w+\})@(\$\{\w+\})", r"\1_at_\2", js)
        data = js.encode("utf-8")
    save(local, data)
    with lock:
        asset_map[absu] = href_for(local)
    print(f"  asset {len(data)//1024:>7} KB  {local[:100]}")
    return asset_map[absu]


def rewrite_css(css, base_url):
    def repl(m):
        ref = m.group(1) or m.group(2) or m.group(3) or m.group(5) or ""
        if not ref or ref.startswith(("data:", "#", BASE + "/assets/")):
            return m.group(0)  # (already pointing at a saved copy)
        absu = urljoin(base_url, re.sub(r"\\(.)", r"\1", ref))
        host = urlsplit(absu).netloc
        if not asset_host_ok(host):
            return m.group(0)
        if host in REMOTE_HOSTS:
            note_remote(absu)
            return m.group(0)
        href = download_asset(absu)
        return f'@import "{href}"' if m.group(5) else f'url("{href}")'
    return CSS_URL_RE.sub(repl, css)


def rewrite_abs_assets(text):
    urls = set(ASSET_URL_RE.findall(text))
    with ThreadPoolExecutor(8) as ex:
        results = dict(zip(urls, ex.map(download_asset, urls)))
    for u in sorted(urls, key=len, reverse=True):  # longest first: no prefix clobbering
        text = text.replace(u, results[u])
    return text


# --- Removing what can't work offline -----------------------------------------------
def remove_element(html, open_re, tag):
    """Remove each element whose opening tag matches open_re, with its nested
    children (balanced by counting <tag ...> / </tag>)."""
    tag_re = re.compile(r"<(/?)%s\b[^>]*>" % tag, re.I)
    while True:
        m = re.search(open_re, html)
        if not m:
            return html
        depth, end = 0, None
        for t in tag_re.finditer(html, m.start()):
            depth += -1 if t.group(1) else 1
            if depth == 0:
                end = t.end()
                break
        if end is None:
            return html
        html = html[:m.start()] + html[end:]


def remove_enclosing(html, marker, tag):
    """Remove the innermost <tag> element that contains marker."""
    while True:
        i = html.find(marker)
        if i < 0:
            return html
        start = html.rfind("<" + tag, 0, i)
        if start < 0:
            return html
        before = len(html)
        html = html[:start] + remove_element(html[start:], r"^<" + tag + r"\b", tag)
        if len(html) == before:
            return html


def strip_offline_incompatible(html):
    for marker in CONFIG.get("remove_sections_containing", []):
        html = remove_enclosing(html, marker, "section")
    for pat in CONFIG.get("remove_patterns", []):
        html = re.sub(pat, "", html, flags=re.S | re.I)
    # Site rules first: they may identify elements by the embeds inside them.
    for open_re, tag in CONFIG.get("remove_elements", []):
        html = remove_element(html, open_re, tag)
    html = TRACKER_RE.sub("", html)
    if not KEEP_EMBEDS:
        html = EMBED_SCRIPT_RE.sub("", html)
        # iframes pointing at other sites (YouTube, Vimeo, maps, 3D tours, forms...)
        def iframe(m):
            src = re.search(r'\s(?:data-)?src="([^"]*)"', m.group(0))
            host = urlsplit(urljoin(START, htmlmod.unescape(src.group(1)))).netloc if src else ""
            return m.group(0) if (not src or host in SITE_HOSTS) else ""
        html = re.sub(r"<iframe\b[^>]*>.*?</iframe>", iframe, html, flags=re.S | re.I)
    if not KEEP_EMBEDS:
        # Webflow wrappers left empty by the removals above
        html = re.sub(r'<figure\b[^>]*w-richtext-figure-type-video[^>]*>(?:(?!</figure>|<iframe|<video).)*</figure>',
                      "", html, flags=re.S | re.I)
        empty = r'<div\b[^>]*class="[^"]*\b(?:w-embed|w-video|w-iframe)\b[^"]*"[^>]*>(?:\s|<!--.*?-->)*</div>'
        for _ in range(3):
            html = re.sub(empty, "", html, flags=re.S | re.I)
    return html


# --- Pages --------------------------------------------------------------------------
ATTR_RE = re.compile(
    r'(\s(?:src|href|poster|srcset|data-src|data-srcset|data-poster-url|data-video-urls)\s*=\s*)(["\'])(.*?)\2',
    re.S | re.I)


def rewrite_attrs(html, page_url, on_page):
    """Point links at saved pages and same-site files at saved copies.
    (Files on CDN hosts are handled by rewrite_abs_assets.)"""
    def one(u):
        raw = u.strip()
        if not raw or raw.startswith(("#", "data:", "mailto:", "tel:", "javascript:", "blob:", "{")):
            return u
        absu = urljoin(page_url, htmlmod.unescape(raw))
        sp = urlsplit(absu)
        if sp.scheme not in ("http", "https"):
            return u
        if sp.netloc not in SITE_HOSTS:
            # Whole-attribute CDN URLs with queries/commas (Google Fonts css2)
            # that the free-text URL scan would cut short.
            if sp.netloc in ASSET_HOSTS and sp.query and sp.netloc not in REMOTE_HOSTS:
                return download_asset(absu.split("#")[0])
            return u
        if is_page_path(sp.path):
            norm = norm_page(sp.path)
            on_page(norm, sp.path)
            suffix = ("?" + sp.query if sp.query else "") + ("#" + sp.fragment if sp.fragment else "")
            return href_for(page_local(norm)) + htmlmod.escape(suffix, quote=False)
        return download_asset(absu.split("#")[0])

    def repl(m):
        attr, q, val = m.groups()
        name = attr.strip().split("=")[0].strip().lower()
        if name in ("srcset", "data-srcset"):
            items = []
            for item in val.split(","):
                bits = item.strip().split(None, 1)
                if bits:
                    bits[0] = one(bits[0])
                items.append(" ".join(bits))
            val = ", ".join(items)
        elif name == "data-video-urls":
            val = ",".join(one(x) for x in val.split(","))
        else:
            val = one(val)
        return attr + q + val + q
    return ATTR_RE.sub(repl, html)


def add_google_fonts_from_webfont_loader(html):
    """Webflow's WebFont.load({google:{families:[...]}}) fetches Google Fonts CSS
    at runtime; save that CSS and link it directly so fonts work offline."""
    m = re.search(r"WebFont\.load\(\{\s*google:\s*\{\s*families:\s*\[([^\]]*)\]", html)
    if not m:
        return html
    fams = re.findall(r'"([^"]+)"|\'([^\']+)\'', m.group(1))
    fams = [a or b for a, b in fams]
    if not fams:
        return html
    css = "https://fonts.googleapis.com/css?family=" + "|".join(quote(f, safe=":,") for f in fams)
    href = download_asset(css)
    return html.replace("</head>", f'<link href="{href}" rel="stylesheet" type="text/css"></head>', 1)


SCRIPT_PAGE_RE = re.compile(r'''(["'`])(/[a-z0-9/_.-]*)([#?][^"'`\s]*)?\1''', re.I)


def rewrite_script_page_paths(seen):
    """Inline scripts navigate with strings like "/about"; point them at the
    saved .html pages (and under BASE) like the rewritten links."""
    def fix_script(m):
        def repl(q):
            if not is_page_path(q.group(2)):
                return q.group(0)
            norm = norm_page(q.group(2))
            if norm == "/" or page_local(norm) not in produced:  # only pages we saved
                return q.group(0)
            return q.group(1) + href_for(page_local(norm)) + (q.group(3) or "") + q.group(1)
        return m.group(1) + SCRIPT_PAGE_RE.sub(repl, m.group(2)) + m.group(3)
    for norm in seen:
        fs = os.path.join(OUT, page_local(norm).lstrip("/"))
        if not os.path.isfile(fs):
            continue
        with open(fs, encoding="utf-8") as f:
            html = f.read()
        new = re.sub(r"(<script\b[^>]*>)(.*?)(</script>)", fix_script, html, flags=re.S)
        if new != html:
            with open(fs, "w", encoding="utf-8") as f:
                f.write(new)


def fetch_finsweet_chunks():
    """Finsweet Attributes v2 lazy-loads dist/chunk-*.js at runtime, so they
    never appear in the HTML. Pull the whole dist/ folder via jsDelivr's API."""
    loader = "https://cdn.jsdelivr.net/npm/@finsweet/attributes@2/attributes.js"
    if loader not in asset_map:
        return
    api = "https://data.jsdelivr.com/v1/packages/npm/@finsweet/attributes"
    try:
        ver = json.loads(fetch(api + "/resolved?specifier=2")[0])["version"]
        tree = json.loads(fetch(f"{api}@{ver}?structure=flat")[0])
    except Exception as e:
        failed.append((api, str(e)))
        return
    names = [f["name"] for f in tree["files"] if f["name"].startswith("/dist/") and f["name"].endswith(".js")]
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda n: download_asset("https://cdn.jsdelivr.net/npm/@finsweet/attributes@2" + n), names))


# --- Offline wiring -----------------------------------------------------------------
PRECACHE_SKIP = re.compile(r"(^/README|^/sw\.js$|^/_headers$|^/robots\.txt$|^/webflow\.json$|^/precache\.json$)")


def webm_with_mp4_twin(urls):
    """.webm files whose .mp4 twin is also listed: every browser that matters
    plays the mp4, so don't spend offline storage on both."""
    def stem(u):
        return re.sub(r"[_-]?(webm|mp4)\.(webm|mp4)$|\.(webm|mp4)$", "", u, flags=re.I)
    mp4s = {stem(u) for u in urls if u.lower().endswith(".mp4")}
    return {u for u in urls if u.lower().endswith(".webm") and stem(u) in mp4s}


def verify_remote():
    """Drop CDN URLs that are dead (pages sometimes reference deleted images)."""
    def ok(u):
        try:
            with urlopen(Request(u, headers={"User-Agent": UA, "Range": "bytes=0-0"}), timeout=30) as r:
                return r.status in (200, 206)
        except Exception as e:
            failed.append((u, str(e)))
            return False
    urls = sorted(remote_urls)
    with ThreadPoolExecutor(16) as ex:
        alive = list(ex.map(ok, urls))
    return [u for u, a in zip(urls, alive) if a]


def app_id():
    host = sorted(SITE_HOSTS, key=len)[0]
    slug = re.sub(r"[^a-z0-9]+", "-", (host + BASE).lower()).strip("-")
    return slug or "site"


def add_offline_support(name, noindex, start_page="/index.html", offline_for="all"):
    head = f'<link rel="manifest" href="{BASE}/manifest.json">'
    if noindex:
        head += '<meta name="robots" content="noindex, nofollow">'
    body = f'<script src="{BASE}/offline.js" data-offline="{offline_for}"></script>'
    shutil.copy(os.path.join(KIT, "runtime", "offline.js"), os.path.join(OUT, "offline.js"))
    produced.update({"/offline.js", "/sw.js", "/precache.json", "/manifest.json"})

    with open(os.path.join(OUT, "index.html"), encoding="utf-8") as f:
        home = f.read()
    icon = (re.search(r'<link\b[^>]*rel="apple-touch-icon"[^>]*href="([^"]+)"', home)
            or re.search(r'<link\b[^>]*href="([^"]+)"[^>]*rel="apple-touch-icon"', home)
            or re.search(r'<link\b[^>]*rel="(?:shortcut )?icon"[^>]*href="([^"]+)"', home)
            or re.search(r'<link\b[^>]*href="([^"]+)"[^>]*rel="(?:shortcut )?icon"', home))
    with open(os.path.join(OUT, "manifest.json"), "w") as f:
        json.dump({
            "name": name, "short_name": name[:12],
            "start_url": href_for(start_page), "scope": f"{BASE}/",
            "display": "standalone", "background_color": "#ffffff", "theme_color": "#ffffff",
            "icons": [{"src": icon.group(1), "sizes": "256x256"}] if icon else [],
        }, f, indent=2)

    local_files, sig = [], hashlib.sha1()
    for local in sorted(produced):
        fs = os.path.join(OUT, local.lstrip("/"))
        if not os.path.isfile(fs):
            continue
        if local.endswith(".html"):
            with open(fs, encoding="utf-8") as f:
                html = f.read()
            if '/offline.js"' not in html:
                html = html.replace("</head>", head + "</head>", 1)
                html = html.replace("</body>", body + "</body>", 1)
                with open(fs, "w", encoding="utf-8") as f:
                    f.write(html)
        if PRECACHE_SKIP.search(local):
            continue
        local_files.append(href_for(local))
        st = os.stat(fs)
        sig.update(f"{local}:{st.st_size}:{int(st.st_mtime)}".encode())

    remote = verify_remote() if remote_urls else []
    everything = local_files + remote
    skip = webm_with_mp4_twin(everything)
    files = [u for u in everything if u not in skip] + [href_for("/precache.json")]
    sig.update("\n".join(remote).encode())
    version = sig.hexdigest()[:10]
    app = app_id()

    with open(os.path.join(OUT, "precache.json"), "w") as f:
        json.dump({"app": app, "version": version, "cache": f"{app}-{version}", "files": files}, f, indent=0)
    with open(os.path.join(KIT, "runtime", "sw.js")) as f:
        sw = f.read().replace("__APP__", app).replace("__VERSION__", version)
    with open(os.path.join(OUT, "sw.js"), "w") as f:
        f.write(sw)
    return len(files), len(remote), version


def site_name(home_html, fallback):
    m = (re.search(r'<meta\b[^>]*property="og:site_name"[^>]*content="([^"]+)"', home_html)
         or re.search(r"<title>([^<]+)</title>", home_html))
    if not m:
        return fallback
    title = htmlmod.unescape(m.group(1)).strip()
    return re.split(r"\s+[|\-–—]\s+", title)[0].strip() or fallback


def write_target_files(target, noindex):
    if target == "webflow-cloud":
        with open(os.path.join(OUT, "webflow.json"), "w") as f:
            f.write('{\n  "cloud": {\n    "framework": "static"\n  }\n}\n')
        produced.add("/webflow.json")
    elif not BASE:  # a domain root: Netlify / Cloudflare Pages / local server
        if noindex:
            with open(os.path.join(OUT, "robots.txt"), "w") as f:
                f.write("User-agent: *\nDisallow: /\n")
            produced.add("/robots.txt")
        with open(os.path.join(OUT, "_headers"), "w") as f:
            f.write(("/*\n  X-Robots-Tag: noindex, nofollow\n\n" if noindex else "") +
                    "/sw.js\n  Cache-Control: no-cache\n\n/precache.json\n  Cache-Control: no-cache\n")
        produced.add("/_headers")


def prune_stale():
    """Delete files a previous build left that this build didn't produce
    (dotfiles like .git and a README.md are never touched)."""
    removed = 0
    for dirpath, dirnames, names in os.walk(OUT, topdown=False):
        rel_dir = os.path.relpath(dirpath, OUT)
        if rel_dir != "." and rel_dir.split(os.sep)[0].startswith("."):
            continue
        for n in names:
            local = "/" + os.path.relpath(os.path.join(dirpath, n), OUT).replace(os.sep, "/")
            if n.startswith(".") or local in ("/README.md",) or local in produced:
                continue
            os.remove(os.path.join(dirpath, n))
            removed += 1
        if dirpath != OUT and not os.listdir(dirpath):
            os.rmdir(dirpath)
    return removed


def compressed_mb():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for local in produced:
            fs = os.path.join(OUT, local.lstrip("/"))
            if os.path.isfile(fs):
                tar.add(fs, arcname=local)
    return buf.tell() / 1048576


# --- Main ---------------------------------------------------------------------------
def main():
    global OUT, BASE, START, NO_AT, CONFIG, KEEP_EMBEDS
    ap = argparse.ArgumentParser(
        description="Turn a website you own into an installable offline app.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Targets")[1])
    ap.add_argument("url", help="the site's home page, e.g. https://www.example.com")
    ap.add_argument("--target", choices=["local", "webflow-cloud"], default="local")
    ap.add_argument("--out", help="output folder (default: out/<host>-<target>)")
    ap.add_argument("--base", help='URL path the app is served under (webflow-cloud default "/app")')
    ap.add_argument("--remote-media", action="store_true",
                    help="leave Webflow CDN files on the CDN (automatic for webflow-cloud)")
    ap.add_argument("--config", help="per-site JSON of extra removals (default: sites/<host>.json)")
    ap.add_argument("--name", help="app name on the Home Screen (default: from the site's title)")
    ap.add_argument("--asset-host", action="append", default=[],
                    help="another host whose files should be saved (repeatable)")
    ap.add_argument("--keep-embeds", action="store_true",
                    help="keep YouTube/Vimeo/maps/form embeds (they won't work offline)")
    ap.add_argument("--allow-indexing", action="store_true",
                    help="don't mark the copy noindex (default: keep it out of search engines)")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT)
    ap.add_argument("--offline-for", choices=["all", "installed"],
                    help="who saves the site for offline: every visitor, or only devices "
                         "you set up (installed app / ?offline=1). Default: installed for "
                         "webflow-cloud, all for local")
    args = ap.parse_args()

    START = args.url if "://" in args.url else "https://" + args.url
    host = urlsplit(START).netloc
    bare = host[4:] if host.startswith("www.") else host
    SITE_HOSTS.update({host, bare, "www." + bare})
    ASSET_HOSTS.extend(args.asset_host)
    build_asset_regex()

    if args.target == "webflow-cloud":
        BASE = "/" + (args.base or "/app").strip("/")
        REMOTE_HOSTS.update(WEBFLOW_MEDIA_HOSTS)
        NO_AT = True
    else:
        BASE = "/" + args.base.strip("/") if args.base and args.base.strip("/") else ""
    if args.remote_media:
        REMOTE_HOSTS.update(WEBFLOW_MEDIA_HOSTS)
    OUT = os.path.abspath(os.path.expanduser(args.out or os.path.join(KIT, "out", f"{bare}-{args.target}")))
    os.makedirs(OUT, exist_ok=True)
    KEEP_EMBEDS = args.keep_embeds
    cfg = args.config or os.path.join(KIT, "sites", bare + ".json")
    if os.path.isfile(cfg):
        with open(cfg) as f:
            CONFIG = json.load(f)
        print(f"Config: {cfg}")
    elif args.config:
        sys.exit(f"Config not found: {args.config}")
    print(f"Site: {START}\nOutput: {OUT}\nTarget: {args.target}  base: {BASE or '/'}  "
          f"media on CDN: {bool(REMOTE_HOSTS)}")

    start_path = urlsplit(START).path or "/"
    start_norm = norm_page(start_path)
    queue, seen, src_path = [start_norm], {start_norm}, {start_norm: start_path}
    if start_norm != "/":
        # Started from a sub-page: still include the home page (sites don't
        # always link back to it), and open the installed app on the sub-page.
        queue.append("/")
        seen.add("/")
        src_path["/"] = "/"
    home_html = ""
    while queue:
        norm = queue.pop(0)
        url = urljoin(START, quote(unquote(src_path.get(norm, norm)), safe="/:@!$&'()*+,;="))
        try:
            data, ctype, final = fetch(url)
        except Exception as e:
            failed.append((url, str(e)))
            continue
        if "html" not in ctype or urlsplit(final).netloc not in SITE_HOSTS:
            continue
        print(f"PAGE {norm}")
        html = data.decode("utf-8", "replace")

        def on_page(n, original):
            if n not in seen and len(seen) < args.max_pages:
                seen.add(n)
                src_path[n] = original
                queue.append(n)
        html = strip_offline_incompatible(html)
        html = re.sub(r'\s(integrity|crossorigin)="[^"]*"', "", html)
        html = add_google_fonts_from_webfont_loader(html)
        html = rewrite_attrs(html, final, on_page)
        # Pages reached only from scripts, e.g. location.replace('/chapter1')
        for script in re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S):
            for q in SCRIPT_PAGE_RE.finditer(script):
                if is_page_path(q.group(2)) and norm_page(q.group(2)) != "/":
                    on_page(norm_page(q.group(2)), q.group(2))
        html = rewrite_abs_assets(html)
        html = re.sub(r"(<style[^>]*>)(.*?)(</style>)",
                      lambda m: m.group(1) + rewrite_css(m.group(2), final) + m.group(3), html, flags=re.S)
        html = re.sub(r'(\sstyle=")([^"]*)(")',
                      lambda m: m.group(1) + rewrite_css(m.group(2).replace("&quot;", '"'), final)
                      .replace('"', "&quot;") + m.group(3), html)
        save(page_local(norm), html.encode("utf-8"))
        if norm == start_norm:
            home_html = html

    if page_local(start_norm) not in produced:
        sys.exit(f"Couldn't load {START} as a web page, so nothing was built. "
                 "Check the address opens in a browser.")
    if "/index.html" not in produced:
        # No usable home page: the start page doubles as index.html. Links are
        # root-relative, so the copy works at the new location.
        shutil.copy(os.path.join(OUT, page_local(start_norm).lstrip("/")), os.path.join(OUT, "index.html"))
        produced.add("/index.html")
        print(f"No home page found; using {start_norm} as the home page.")
    rewrite_script_page_paths(seen)
    fetch_finsweet_chunks()
    noindex = not args.allow_indexing
    write_target_files(args.target, noindex)
    offline_for = args.offline_for or ("installed" if args.target == "webflow-cloud" else "all")
    n_files, n_remote, version = add_offline_support(args.name or site_name(home_html, bare), noindex,
                                                     start_page=page_local(start_norm),
                                                     offline_for=offline_for)
    pruned = prune_stale()

    saved_pages = sum(1 for p in produced if p.endswith(".html"))
    print(f"\nDone: {saved_pages} pages, {n_files} files for offline "
          f"({n_remote} on the CDN), version {version}.")
    if pruned:
        print(f"Removed {pruned} stale files from the previous build.")
    mb = compressed_mb()
    if args.target == "webflow-cloud":
        print(f"Deploy size: {mb:.1f} MB compressed (Webflow Cloud limit: 100 MB)"
              + ("  <-- TOO BIG" if mb > 100 else ""))
    if failed:
        print(f"{len(failed)} downloads failed (usually also broken on the live site):")
        for u, e in failed[:40]:
            print("  ", e, u[:150])
    print(f"\nOutput: {OUT}")

    # Build summary: read by the Offline Kit app (kit_app.py) to list and
    # rebuild builds. A dotfile, so it is never deployed or pruned.
    import datetime
    summary = {
        "url": START, "target": args.target, "base": BASE, "name": args.name or "",
        "offline_for": offline_for,
        "out": OUT, "pages": saved_pages, "files": n_files, "cdn_files": n_remote,
        "version": version, "compressed_mb": round(mb, 1), "failed": len(failed),
        "failures": [f"{e} {u}" for u, e in failed[:40]],
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    with open(os.path.join(OUT, ".offline-build.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print("RESULT " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
