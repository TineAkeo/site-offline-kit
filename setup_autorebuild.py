#!/usr/bin/env python3
"""Add automatic rebuilds to an offline app repo (e.g. a Webflow Cloud app).

    python3 setup_autorebuild.py ~/path/to/app-repo https://www.example.com
    python3 setup_autorebuild.py ~/path/to/template-repo --template

It copies this kit into <repo>/.kit/, writes the site settings to
.kit/site.json, and adds the GitHub Actions workflow
.github/workflows/offline-rebuild.yml. The workflow rebuilds from the live
site when Webflow's "Site publish" webhook arrives (through
offline-publish-relay), when run by hand, and once a day as a safety net.
It commits and pushes only real changes; Webflow Cloud redeploys on push.
Dot folders like .kit and .github aren't deployed by Webflow Cloud.

--template prepares a GitHub template repo instead: no site URL and no
webhook secret yet. Each repo made from it gets its own on its first run
(Actions → Rebuild offline app → Run workflow, with site_url).

Run it again after updating the kit to refresh .kit/, then commit and push.
"""
import argparse, json, os, re, secrets, shutil, subprocess, sys

KIT = os.path.dirname(os.path.abspath(__file__))
KIT_FILES = ["offline_site.py", "ci.py", "runtime/sw.js", "runtime/offline.js"]
# Where offline-publish-relay is deployed (its Webflow Cloud mount path).
DEFAULT_RELAY = "https://webflowcloudtest-105e9e.webflow.io/hooks"
GITIGNORE_LINES = [".DS_Store", ".offline-build.json", "__pycache__/"]


def repo_slug(repo):
    """owner/name from the repo's GitHub remote, if it has one."""
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo,
                             capture_output=True, text=True).stdout.strip()
    except OSError:
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser(description="Add rebuild-on-publish to an offline app repo.")
    ap.add_argument("repo", help="the app's git repo folder (the build output)")
    ap.add_argument("url", nargs="?", help="the live site, e.g. https://www.example.com")
    ap.add_argument("--template", action="store_true",
                    help="prepare a template repo: no site URL or webhook secret yet")
    ap.add_argument("--target", choices=["webflow-cloud", "local"], default="webflow-cloud")
    ap.add_argument("--base", default="/app", help='mount path for webflow-cloud (default "/app")')
    ap.add_argument("--relay", default=DEFAULT_RELAY, help="address of offline-publish-relay")
    ap.add_argument("--cron", default="23 3 * * *",
                    help="daily safety-net check, GitHub cron syntax in UTC (default 03:23)")
    args = ap.parse_args()

    repo = os.path.abspath(os.path.expanduser(args.repo))
    if not os.path.isdir(os.path.join(repo, ".git")):
        sys.exit(f"{repo} isn't a git repo (run `git init` in it first).")
    if not args.template and not args.url:
        sys.exit("Give the site's URL, or use --template.")
    url = "" if args.template else (args.url if "://" in args.url else "https://" + args.url)

    # 1. The kit, copied in so the workflow needs no access to other repos.
    dest = os.path.join(repo, ".kit")
    keep = {n: open(os.path.join(dest, n)).read() for n in ("hook.json",)
            if os.path.isfile(os.path.join(dest, n))}
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    for rel in KIT_FILES:
        os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
        shutil.copy(os.path.join(KIT, rel), os.path.join(dest, rel))
    shutil.copytree(os.path.join(KIT, "sites"), os.path.join(dest, "sites"))
    for n, text in keep.items():  # an existing webhook secret stays valid
        with open(os.path.join(dest, n), "w") as f:
            f.write(text)

    # 2. Site settings, read by .kit/ci.py.
    with open(os.path.join(dest, "site.json"), "w") as f:
        json.dump({"url": url, "target": args.target, "base": args.base,
                   "relay": args.relay.rstrip("/")}, f, indent=2)
        f.write("\n")

    # 3. The webhook secret (not for templates: each new repo makes its own).
    hook_path = os.path.join(dest, "hook.json")
    if not args.template and not os.path.isfile(hook_path):
        with open(hook_path, "w") as f:
            json.dump({"secret": secrets.token_urlsafe(24), "branch": "main"}, f, indent=2)
            f.write("\n")
    if args.template and os.path.isfile(hook_path):
        os.remove(hook_path)

    # 4. The workflow.
    wf_dir = os.path.join(repo, ".github", "workflows")
    os.makedirs(wf_dir, exist_ok=True)
    with open(os.path.join(KIT, "templates", "offline-rebuild.yml")) as f:
        workflow = f.read().replace("__CRON__", args.cron)
    with open(os.path.join(wf_dir, "offline-rebuild.yml"), "w") as f:
        f.write(workflow)

    # 5. Keep local build info out of the repo.
    gi = os.path.join(repo, ".gitignore")
    lines = open(gi).read().splitlines() if os.path.isfile(gi) else []
    with open(gi, "a") as f:
        for line in GITIGNORE_LINES:
            if line not in lines:
                f.write(line + "\n")

    print(f"Set up rebuild-on-publish in {repo}")
    print(f"  .kit/        kit copy + site.json (site: {url or '(set on first run)'})")
    print(f"  workflow     .github/workflows/offline-rebuild.yml (safety net: {args.cron} UTC)")
    if not args.template:
        slug = repo_slug(repo) or "<owner>/<repo>"
        secret = json.load(open(hook_path))["secret"]
        print("\nWebflow webhook (Site settings → Apps & integrations → Webhooks → Site publish):")
        print(f"  {args.relay.rstrip('/')}/hook/{slug}/{secret}")
        print("Keep that address private: anyone with it can start a rebuild.")
    print("\nNext: commit and push.")


if __name__ == "__main__":
    main()
