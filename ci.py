#!/usr/bin/env python3
"""Build step of an offline app repo's GitHub Actions workflow.

Runs as .kit/ci.py from .github/workflows/offline-rebuild.yml. It reads the
settings in .kit/site.json:

    {"url": "https://www.example.com", "target": "webflow-cloud", "base": "/app",
     "relay": "https://<site-with-the-relay>/hooks"}

Environment (from the workflow):
    SITE_URL  set on a first "Run workflow": stored as "url" and forces a build
    FORCE     "true" to rebuild even if the site wasn't republished

On the first run it also creates .kit/hook.json, the secret for this repo's
"Site publish" webhook address (checked by offline-publish-relay), and shows
that address in the run summary.
"""
import json, os, secrets, subprocess, sys

KIT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(KIT)
CFG = os.path.join(KIT, "site.json")
HOOK = os.path.join(KIT, "hook.json")
DEFAULTS = {"url": "", "target": "webflow-cloud", "base": "/app", "relay": ""}


def summary(md):
    """Show text on the workflow run's page (and in the log)."""
    print(md)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(md + "\n")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main():
    cfg = {**DEFAULTS, **load(CFG, {})}
    first = False
    site_url = os.environ.get("SITE_URL", "").strip()
    if site_url:
        cfg["url"] = site_url if "://" in site_url else "https://" + site_url
        save(CFG, cfg)
        first = True
    if not cfg["url"]:
        message = ("## Setup needed\n\nRun this workflow from the **Actions** tab "
                   "(*Rebuild offline app → Run workflow*) and fill in **site_url** "
                   "with the live site, e.g. `https://www.example.com`.")
        if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
            # The template repo itself, or a new repo before its first run:
            # nothing to check yet, so the daily safety net isn't a failure.
            summary(message.replace("## Setup needed", "## No site set yet: skipped"))
            return
        summary(message)
        sys.exit(1)
    if not os.path.isfile(HOOK):
        save(HOOK, {"secret": secrets.token_urlsafe(24),
                    "branch": os.environ.get("GITHUB_REF_NAME") or "main"})
        first = True

    cmd = [sys.executable, os.path.join(KIT, "offline_site.py"), cfg["url"],
           "--target", cfg["target"], "--out", REPO, "--strict"]
    if cfg["target"] == "webflow-cloud":
        cmd += ["--base", cfg["base"]]
    if not first and os.environ.get("FORCE") != "true":
        cmd.append("--skip-unchanged")
    code = subprocess.call(cmd)
    if code:
        sys.exit(code)

    if first:
        hook = load(HOOK, {})
        repo = os.environ.get("GITHUB_REPOSITORY", "<owner>/<repo>")
        relay = cfg["relay"].rstrip("/")
        address = (f"{relay}/hook/{repo}/{hook.get('secret')}" if relay
                   else "(set \"relay\" in .kit/site.json to the publish relay's address first)")
        summary(
            f"## Offline app built from {cfg['url']}\n\n"
            "**Next, once per site:**\n\n"
            "1. **Webflow Cloud:** Apps → Webflow Cloud → Create new app → pick this repo → "
            f"**Existing site** → mount path `{cfg['base']}` → Deploy.\n"
            "2. **Rebuild on Publish:** in the site's Webflow **Site settings → Apps & "
            "integrations → Webhooks → Add webhook**, choose **Site publish** and use this URL:\n\n"
            f"   `{address}`\n\n"
            "Keep that address private: anyone with it can start a rebuild.")


if __name__ == "__main__":
    main()
