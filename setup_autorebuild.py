#!/usr/bin/env python3
"""Add automatic rebuilds to an offline app repo (e.g. a Webflow Cloud app).

    python3 setup_autorebuild.py ~/path/to/app-repo https://www.example.com

It copies this kit into <repo>/.kit/ and writes a GitHub Actions workflow,
.github/workflows/offline-rebuild.yml, that runs the kit on a schedule (and
on demand). The workflow commits and pushes only when the site actually
changed, and Webflow Cloud redeploys on that push. Dot folders like .kit and
.github aren't deployed by Webflow Cloud.

Run it again after updating the kit to refresh the copy in .kit/. Then
commit and push the repo.
"""
import argparse, os, shutil, sys

KIT = os.path.dirname(os.path.abspath(__file__))
KIT_FILES = ["offline_site.py", "runtime/sw.js", "runtime/offline.js"]

WORKFLOW = """\
# Rebuilds the offline app from the live site and deploys it when it changed.
# Written by site-offline-kit's setup_autorebuild.py.
name: Rebuild offline app

on:
  schedule:
    - cron: "{cron}"
  workflow_dispatch:          # "Run workflow" button in the Actions tab
  repository_dispatch:        # for a "site published" webhook relay
    types: [site-published]

permissions:
  contents: write

concurrency:
  group: offline-rebuild
  cancel-in-progress: false

jobs:
  rebuild:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4

      - name: Build from the live site
        # --strict: a temporary failure stops here, so nothing half-built is pushed.
        run: >
          python3 .kit/offline_site.py "{url}"
          --target {target} {base_arg} --out . --strict

      - name: Commit and push if the site changed
        run: |
          git add -A
          if git diff --cached --quiet; then
            echo "No changes on the site; nothing to deploy."
            exit 0
          fi
          git config user.name "offline-kit"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git commit -m "Rebuild from {host}: site changed"
          git push
"""

GITIGNORE_LINES = [".DS_Store", ".offline-build.json", "__pycache__/"]


def main():
    ap = argparse.ArgumentParser(description="Add scheduled rebuilds to an offline app repo.")
    ap.add_argument("repo", help="the app's git repo folder (the build output)")
    ap.add_argument("url", help="the live site, e.g. https://www.example.com")
    ap.add_argument("--target", choices=["webflow-cloud", "local"], default="webflow-cloud")
    ap.add_argument("--base", default="/app", help='mount path for webflow-cloud (default "/app")')
    ap.add_argument("--cron", default="17 * * * *",
                    help='when to check the site, in GitHub cron syntax (UTC). Default: hourly')
    args = ap.parse_args()

    repo = os.path.abspath(os.path.expanduser(args.repo))
    if not os.path.isdir(os.path.join(repo, ".git")):
        sys.exit(f"{repo} isn't a git repo. Build into it and push it to GitHub first.")
    url = args.url if "://" in args.url else "https://" + args.url
    host = url.split("://", 1)[1].split("/")[0]
    bare = host[4:] if host.startswith("www.") else host

    # 1. The kit, copied in so the workflow needs no access to other repos.
    dest = os.path.join(repo, ".kit")
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    for rel in KIT_FILES:
        os.makedirs(os.path.dirname(os.path.join(dest, rel)) or dest, exist_ok=True)
        shutil.copy(os.path.join(KIT, rel), os.path.join(dest, rel))
    site_cfg = os.path.join(KIT, "sites", bare + ".json")
    if os.path.isfile(site_cfg):
        os.makedirs(os.path.join(dest, "sites"), exist_ok=True)
        shutil.copy(site_cfg, os.path.join(dest, "sites", bare + ".json"))

    # 2. The workflow.
    wf_dir = os.path.join(repo, ".github", "workflows")
    os.makedirs(wf_dir, exist_ok=True)
    base_arg = f'--base "{args.base}"' if args.target == "webflow-cloud" else ""
    with open(os.path.join(wf_dir, "offline-rebuild.yml"), "w") as f:
        f.write(WORKFLOW.format(cron=args.cron, url=url, target=args.target,
                                base_arg=base_arg, host=bare))

    # 3. Keep local build info out of the repo.
    gi = os.path.join(repo, ".gitignore")
    lines = open(gi).read().splitlines() if os.path.isfile(gi) else []
    with open(gi, "a") as f:
        for line in GITIGNORE_LINES:
            if line not in lines:
                f.write(line + "\n")

    print(f"Added automatic rebuilds to {repo}")
    print(f"  kit copy:  .kit/  ({', '.join(KIT_FILES)}"
          + (f", sites/{bare}.json" if os.path.isfile(site_cfg) else "") + ")")
    print(f"  workflow:  .github/workflows/offline-rebuild.yml  (schedule: {args.cron} UTC)")
    print("Next: commit and push. Then trigger a first run from the repo's Actions tab "
          "(Rebuild offline app → Run workflow), or wait for the schedule.")


if __name__ == "__main__":
    main()
