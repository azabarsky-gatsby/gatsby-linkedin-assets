#!/usr/bin/env python3
"""
resolve-notion-image.py

Given a Notion page ID from the "LinkedIn Posts" database:
  1. If the page already has "Post Image URL" set to an https URL, return it (cache hit).
  2. If "Post Image" is empty, print NO_IMAGE (text-only post).
  3. If "Post Image" is a Notion-hosted file, download the bytes, push to the
     gatsby-linkedin-assets repo, wait for Cloudflare Pages to deploy, then
     write the public URL to "Post Image URL" and return it.
  4. If "Post Image" is an external URL, copy it to "Post Image URL" and return it.

On success: prints the public https URL (or NO_IMAGE) to stdout and exits 0.
On failure: prints "ERROR: <reason>" to stderr and exits 1.

Credentials (env var wins; .secrets file is fallback):
  - NOTION_TOKEN                     or  <secrets-dir>/notion-integration-token
  - GITHUB_TOKEN                     or  <secrets-dir>/github-token

Default secrets dir:
  /sessions/pensive-sweet-feynman/mnt/Marketing/.secrets
  (override with --secrets-dir or SECRETS_DIR env var)

Usage:
  python3 resolve-notion-image.py <notion-page-id>
  python3 resolve-notion-image.py <notion-page-id> --verbose
  python3 resolve-notion-image.py <notion-page-id> --secrets-dir /path/to/.secrets
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
GH_REPO = "azabarsky-gatsby/gatsby-linkedin-assets"
PAGES_HOST = "gatsby-linkedin-assets.pages.dev"
PUBLIC_BASE = f"https://{PAGES_HOST}"
DEFAULT_SECRETS_DIR = "/sessions/pensive-sweet-feynman/mnt/Marketing/.secrets"


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def load_token(env_name, secrets_dir, file_name):
    """Env var wins; .secrets file is fallback."""
    if os.environ.get(env_name):
        return os.environ[env_name].strip()
    p = pathlib.Path(secrets_dir) / file_name
    if not p.exists():
        die(f"missing credential: set {env_name} env var or create {p}")
    return p.read_text().strip()


def notion_request(method, path, token, body=None):
    url = f"{NOTION_API}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        die(f"Notion API {method} {path} -> HTTP {e.code}: {body_text[:500]}")


def slugify(s, max_len=40):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:max_len] or "post"


def parse_post_image(files_prop):
    """Return (kind, url, ext): kind is 'file' | 'external' | None."""
    if not files_prop or files_prop.get("type") != "files":
        return (None, None, None)
    files = files_prop.get("files") or []
    if not files:
        return (None, None, None)
    first = files[0]
    kind = first.get("type")
    if kind == "file":
        url = first.get("file", {}).get("url")
    elif kind == "external":
        url = first.get("external", {}).get("url")
    else:
        return (None, None, None)
    if not url:
        return (None, None, None)

    name = first.get("name", "")
    ext = "png"
    for candidate in (url.split("?")[0], name):
        m = re.search(r"\.(png|jpe?g|gif|webp)$", candidate or "", re.IGNORECASE)
        if m:
            ext = m.group(1).lower()
            if ext == "jpeg":
                ext = "jpg"
            break
    return (kind, url, ext)


def get_page_title(page):
    props = page.get("properties", {})
    rich = props.get("Post Title", {}).get("title", [])
    return "".join(t.get("plain_text", "") for t in rich).strip() or "untitled"


def get_post_image_url(page):
    prop = page.get("properties", {}).get("Post Image URL", {})
    if prop.get("type") == "url":
        return prop.get("url")
    return None


def download(url, dest_path):
    req = urllib.request.Request(url, headers={"User-Agent": "gatsby-resolver/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if not data:
        die(f"downloaded 0 bytes from {url[:80]}...")
    pathlib.Path(dest_path).write_bytes(data)
    return len(data)


def run(cmd, cwd=None, check=True):
    result = subprocess.run(cmd, cwd=cwd, check=False, capture_output=True, text=True)
    if check and result.returncode != 0:
        die(
            "command failed: "
            + " ".join(cmd)
            + f"\nstderr: {result.stderr.strip()[:500]}"
        )
    return result


def push_to_pages(filename, local_path, github_token):
    """Clone repo, add file if new/changed, commit and push. Returns public URL."""
    tmpdir = tempfile.mkdtemp(prefix="gatsby-assets-")
    clone_url = f"https://x-access-token:{github_token}@github.com/{GH_REPO}.git"
    run(["git", "clone", "--depth", "1", clone_url, tmpdir])
    run(["git", "config", "user.email", "adam@gatsby.events"], cwd=tmpdir)
    run(["git", "config", "user.name", "Gatsby LinkedIn Resolver"], cwd=tmpdir)
    dest = pathlib.Path(tmpdir) / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(pathlib.Path(local_path).read_bytes())
    status = run(["git", "status", "--porcelain"], cwd=tmpdir, check=False)
    if status.stdout.strip():
        run(["git", "add", filename], cwd=tmpdir)
        run(["git", "commit", "-m", f"Add {filename}"], cwd=tmpdir)
        run(["git", "push", "origin", "main"], cwd=tmpdir)
    return f"{PUBLIC_BASE}/{filename}"


def wait_for_live(url, expected_size, timeout_s=180, poll_s=5):
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gatsby-resolver/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 200:
                    body = r.read()
                    if expected_size == 0 or len(body) == expected_size:
                        return True
        except urllib.error.HTTPError:
            pass
        except Exception:
            pass
        time.sleep(poll_s)
    return False


def write_image_url_back(page_id, public_url, token):
    body = {"properties": {"Post Image URL": {"url": public_url}}}
    notion_request("PATCH", f"/pages/{page_id}", token, body=body)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("page_id", help="Notion page ID (with or without hyphens)")
    ap.add_argument(
        "--secrets-dir",
        default=os.environ.get("SECRETS_DIR", DEFAULT_SECRETS_DIR),
    )
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    def log(m):
        if args.verbose:
            print(f"[resolver] {m}", file=sys.stderr)

    notion_token = load_token("NOTION_TOKEN", args.secrets_dir, "notion-integration-token")
    github_token = load_token("GITHUB_TOKEN", args.secrets_dir, "github-token")

    log(f"fetching page {args.page_id}")
    page = notion_request("GET", f"/pages/{args.page_id}", notion_token)

    # Cache hit
    existing = get_post_image_url(page)
    if existing and existing.startswith("https://"):
        log(f"cache hit: {existing}")
        print(existing)
        return

    # Parse Post Image
    files_prop = page.get("properties", {}).get("Post Image", {})
    kind, url, ext = parse_post_image(files_prop)
    if kind is None:
        print("NO_IMAGE")
        return

    # External URL passthrough
    if kind == "external":
        log(f"external URL, pass-through: {url}")
        write_image_url_back(args.page_id, url, notion_token)
        print(url)
        return

    # Notion-hosted file: download, push, deploy, writeback
    title = get_page_title(page)
    short_id = args.page_id.replace("-", "")[:8]
    filename = f"{short_id}-{slugify(title)}.{ext}"
    log(f"filename: {filename}")

    with tempfile.TemporaryDirectory() as td:
        local_path = pathlib.Path(td) / filename
        size = download(url, local_path)
        log(f"downloaded {size} bytes from Notion signed URL")

        public_url = push_to_pages(filename, local_path, github_token)
        log(f"pushed to {GH_REPO}; polling {public_url}")

        if not wait_for_live(public_url, size):
            die(f"timed out waiting for {public_url} (expected {size} bytes)")
        log("live on Cloudflare Pages")

    log("writing URL back to Notion Post Image URL property")
    write_image_url_back(args.page_id, public_url, notion_token)
    print(public_url)


if __name__ == "__main__":
    main()
