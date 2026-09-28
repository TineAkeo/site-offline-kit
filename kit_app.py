#!/usr/bin/env python3
"""Offline Kit app: a local web UI for offline_site.py.

    python3 kit_app.py          (or double-click "Offline Kit.app" on a Mac)

Opens http://localhost:<port>/ in your browser. Build a site, watch progress,
preview the result offline, open its folder, and push it to GitHub. Runs only
on this computer (127.0.0.1); every action needs a per-launch token that only
the app's own page has, so other websites can't drive it. Quits by itself a
few minutes after its page is closed. Standard library only.
"""
import json, os, queue, re, secrets, shutil, socket, subprocess, sys, threading, time, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler, SimpleHTTPRequestHandler
from urllib.parse import urlsplit, unquote

KIT = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(KIT, "out")
TOKEN = secrets.token_urlsafe(24)
IDLE_QUIT_SECONDS = 300

state_lock = threading.Lock()
job = None            # the running/last build
last_ping = time.time()
previews = {}         # build folder -> (server, url)


def free_port(preferred):
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("No free port found")


# --- Builds -------------------------------------------------------------------------
class Job:
    def __init__(self, cmd):
        self.lines, self.result, self.returncode = [], None, None
        self.listeners = []
        self.proc = subprocess.Popen(cmd, cwd=KIT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("RESULT "):
                try:
                    self.result = json.loads(line[7:])
                except ValueError:
                    pass
                continue
            self._emit({"type": "log", "line": line})
        self.returncode = self.proc.wait()
        self._emit({"type": "done", "ok": self.returncode == 0 and self.result is not None,
                    "result": self.result, "code": self.returncode})

    def _emit(self, event):
        with state_lock:
            if event["type"] == "log":
                self.lines.append(event["line"])
            self.events_done = event["type"] == "done"
            for q in list(self.listeners):
                q.put(event)

    @property
    def running(self):
        return self.returncode is None


def start_build(opts):
    global job
    url = (opts.get("url") or "").strip()
    if not re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}(/\S*)?$", url, re.I):
        return {"error": "That doesn't look like a website address."}
    target = "webflow-cloud" if opts.get("target") == "webflow-cloud" else "local"
    cmd = [sys.executable, "-u", os.path.join(KIT, "offline_site.py"), url, "--target", target]
    if opts.get("name"):
        cmd += ["--name", opts["name"].strip()]
    if target == "webflow-cloud" and opts.get("base"):
        cmd += ["--base", "/" + opts["base"].strip().strip("/")]
    if opts.get("out"):
        cmd += ["--out", os.path.expanduser(opts["out"].strip())]
    if opts.get("keep_embeds"):
        cmd.append("--keep-embeds")
    if opts.get("allow_indexing"):
        cmd.append("--allow-indexing")
    with state_lock:
        if job and job.running:
            return {"error": "A build is already running."}
        job = Job(cmd)
    return {"ok": True}


def build_folders():
    """Every folder with a build summary: under out/, plus any --out folders
    remembered in out/.builds.json."""
    known = set()
    if os.path.isdir(OUT_ROOT):
        known.update(os.path.join(OUT_ROOT, d) for d in os.listdir(OUT_ROOT))
    try:
        with open(os.path.join(OUT_ROOT, ".builds.json")) as f:
            known.update(json.load(f))
    except (OSError, ValueError):
        pass
    builds = []
    for d in known:
        p = os.path.join(d, ".offline-build.json")
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    b = json.load(f)
            except ValueError:
                continue
            b["out"] = d
            b["git"] = git_info(d)
            builds.append(b)
    return sorted(builds, key=lambda b: b.get("built_at", ""), reverse=True)


def remember_folder(path):
    os.makedirs(OUT_ROOT, exist_ok=True)
    p = os.path.join(OUT_ROOT, ".builds.json")
    try:
        with open(p) as f:
            folders = set(json.load(f))
    except (OSError, ValueError):
        folders = set()
    folders.add(path)
    with open(p, "w") as f:
        json.dump(sorted(folders), f, indent=1)


def is_build_folder(path):
    return bool(path) and os.path.isfile(os.path.join(path, ".offline-build.json"))


# --- Git / GitHub -------------------------------------------------------------------
def run(cmd, cwd):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
    return p.returncode, (p.stdout + p.stderr).strip()


def git_info(path):
    if not shutil.which("git") or not os.path.isdir(os.path.join(path, ".git")):
        return {"repo": False, "gh": bool(shutil.which("gh"))}
    code, remote = run(["git", "remote", "get-url", "origin"], path)
    return {"repo": True, "remote": remote if code == 0 else "", "gh": bool(shutil.which("gh"))}


def push(path, message, new_repo=None):
    log = []
    if not os.path.isdir(os.path.join(path, ".git")):
        if not new_repo:
            return {"error": "This folder isn't a git repo yet. Enter a new repo name to create one."}
        for cmd in (["git", "init", "-q", "-b", "main"],):
            code, out = run(cmd, path)
            log.append(out)
            if code:
                return {"error": out}
        with open(os.path.join(path, ".gitignore"), "a") as f:
            f.write(".DS_Store\n.offline-build.json\n")
    run(["git", "add", "-A"], path)
    code, out = run(["git", "commit", "-q", "-m", message or "Update offline build"], path)
    if code and "nothing to commit" not in out:
        return {"error": out}
    log.append(out or "Committed.")
    code, remote = run(["git", "remote", "get-url", "origin"], path)
    if code:  # no remote: create a private GitHub repo with gh
        if not new_repo:
            return {"error": "No GitHub remote. Enter a new repo name to create a private one."}
        if not shutil.which("gh"):
            return {"error": "The GitHub CLI (gh) isn't installed, so the repo can't be created from here."}
        code, out = run(["gh", "repo", "create", new_repo, "--private", "--source", ".", "--push"], path)
        log.append(out)
        return {"ok": code == 0, "log": "\n".join(log), **({} if code == 0 else {"error": out})}
    code, out = run(["git", "push", "-u", "origin", "HEAD"], path)
    log.append(out)
    return {"ok": code == 0, "log": "\n".join(log), **({} if code == 0 else {"error": out})}


# --- Preview servers ----------------------------------------------------------------
def preview(path):
    """Serve a build like its host would: at / for local builds, under the
    mount path (e.g. /app/) for Webflow Cloud builds."""
    if path in previews:
        return previews[path][1]
    with open(os.path.join(path, ".offline-build.json")) as f:
        base = json.load(f).get("base", "")

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def translate_path(self, p):
            p = unquote(urlsplit(p).path)
            if base:
                if not (p == base or p.startswith(base + "/")):
                    return os.path.join(path, "__outside_mount__")
                p = p[len(base):] or "/"
            full = os.path.normpath(os.path.join(path, p.lstrip("/")))
            return full if full.startswith(path) else os.path.join(path, "__outside__")

        def do_GET(self):
            if base and urlsplit(self.path).path in ("/", ""):
                self.send_response(302)
                self.send_header("Location", base + "/index.html")
                self.end_headers()
                return
            super().do_GET()

        def end_headers(self):
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

    port = free_port(8600)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://localhost:{port}{base}/index.html"
    previews[path] = (srv, url)
    return url


# --- The app's own server -----------------------------------------------------------
class App(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        host = self.headers.get("Host", "")
        return (self.headers.get("X-Kit-Token") == TOKEN
                and re.match(r"^(localhost|127\.0\.0\.1):\d+$", host) is not None)

    def do_GET(self):
        global last_ping
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(KIT, "ui", "index.html"), encoding="utf-8") as f:
                page = f.read().replace("__TOKEN__", TOKEN)
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/stream":
            # EventSource can't send headers, so the token comes as a query param.
            if f"token={TOKEN}" not in (urlsplit(self.path).query or ""):
                return self._json({"error": "forbidden"}, 403)
            return self._stream()
        if not self._authorized():
            return self._json({"error": "forbidden"}, 403)
        last_ping = time.time()
        if path == "/api/builds":
            return self._json({"builds": build_folders(),
                               "running": bool(job and job.running)})
        if path == "/api/ping":
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        global last_ping
        if not self._authorized():
            return self._json({"error": "forbidden"}, 403)
        last_ping = time.time()
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        path = urlsplit(self.path).path
        folder = data.get("path")
        if path == "/api/build":
            return self._json(start_build(data))
        if path == "/api/stop":
            if job and job.running:
                job.proc.terminate()
            return self._json({"ok": True})
        if path == "/api/remember":
            if is_build_folder(folder):
                remember_folder(folder)
            return self._json({"ok": True})
        if path == "/api/choose-folder":
            if sys.platform != "darwin":
                return self._json({"error": "Folder picker is Mac-only; type a path instead."})
            p = subprocess.run(["osascript", "-e",
                                'POSIX path of (choose folder with prompt "Build into which folder?")'],
                               capture_output=True, text=True)
            return self._json({"path": p.stdout.strip().rstrip("/")} if p.returncode == 0 else {"cancelled": True})
        # everything below acts on an existing build folder only
        if not is_build_folder(folder):
            return self._json({"error": "Not a build folder."}, 400)
        if path == "/api/open":
            opener = "open" if sys.platform == "darwin" else ("explorer" if os.name == "nt" else "xdg-open")
            subprocess.Popen([opener, folder])
            return self._json({"ok": True})
        if path == "/api/preview":
            url = preview(folder)
            webbrowser.open(url)
            return self._json({"url": url})
        if path == "/api/push":
            name = (data.get("new_repo") or "").strip()
            if name and not re.match(r"^[\w.-]+(/[\w.-]+)?$", name):
                return self._json({"error": "Repo names look like my-repo or owner/my-repo."})
            return self._json(push(folder, data.get("message"), name or None))
        self._json({"error": "not found"}, 404)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = queue.Queue()
        with state_lock:
            j = job
            backlog = list(j.lines) if j else []
            if j:
                j.listeners.append(q)
        try:
            for line in backlog:
                self.wfile.write(f"data: {json.dumps({'type': 'log', 'line': line})}\n\n".encode())
            if j and not j.running:
                self.wfile.write(f"data: {json.dumps({'type': 'done', 'ok': j.returncode == 0 and j.result is not None, 'result': j.result, 'code': j.returncode})}\n\n".encode())
                return
            self.wfile.flush()
            while j:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
                if ev["type"] == "done":
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if j:
                with state_lock:
                    if q in j.listeners:
                        j.listeners.remove(q)


def idle_watchdog(server):
    while True:
        time.sleep(30)
        busy = job is not None and job.running
        if not busy and time.time() - last_ping > IDLE_QUIT_SECONDS:
            print("No open Offline Kit page for a while; quitting.")
            server.shutdown()
            return


def main():
    port = free_port(8765)
    server = ThreadingHTTPServer(("127.0.0.1", port), App)
    url = f"http://localhost:{port}/"
    print(f"Offline Kit running at {url}  (Ctrl+C to quit)")
    threading.Thread(target=idle_watchdog, args=(server,), daemon=True).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
