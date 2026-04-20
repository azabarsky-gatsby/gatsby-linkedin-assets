# LinkedIn image resolver

`resolve-notion-image.py` — takes a Notion LinkedIn-post page ID and guarantees
its image is hosted at a public URL so Zapier's `linkedin_create_company_update`
integration can fetch it.

## What it does

For a given page in the **LinkedIn Posts** Notion database (`7dbf1573-6191-42dc-92eb-5fecbe5e9bc7`):

1. If `Post Image URL` (URL property) is already set to an https URL → print it and exit (cache hit).
2. If `Post Image` (Files & media property) is empty → print `NO_IMAGE` and exit.
3. If `Post Image` is an external URL → copy it to `Post Image URL` and exit.
4. If `Post Image` is a Notion-hosted attachment:
   a. Read the signed S3 URL from Notion's `pages.retrieve` response.
   b. Download the bytes to a tempfile.
   c. Clone this repo to `/tmp`, copy the file to the repo root with a deterministic name
      (`<first-8-chars-of-page-id>-<slugified-title>.<ext>`), commit, and push.
   d. Poll `https://gatsby-linkedin-assets.pages.dev/<filename>` until it returns 200
      with the expected byte count (up to 3 minutes).
   e. PATCH the Notion page, setting `Post Image URL` to the public URL.
   f. Print the public URL.

The **original attachment in `Post Image` is never modified** — the resolver only writes
to the separate `Post Image URL` property.

## Why two properties

- `Post Image` stays a normal Notion Files & media field so humans can drag in images
  during authoring, review, and CEO edits.
- `Post Image URL` is what the Zapier LinkedIn integration actually reads. Notion's
  internal `file://` references can't be fetched by Zapier; a public https URL can.
- Separating them means the resolver is idempotent and cache-friendly: once a URL is
  written, subsequent runs are a no-op lookup.

## Credentials

Loaded from env var first, then from a secrets directory:

| Env var        | Fallback file                                                    |
| -------------- | ---------------------------------------------------------------- |
| `NOTION_TOKEN` | `<secrets-dir>/notion-integration-token`                         |
| `GITHUB_TOKEN` | `<secrets-dir>/github-token`                                     |

Default secrets dir: `/sessions/pensive-sweet-feynman/mnt/Marketing/.secrets/`
(override with `--secrets-dir` or `SECRETS_DIR` env var).

The Notion integration needs **read + update** access to the LinkedIn Posts database.
The GitHub token needs **Contents: read/write** on `azabarsky-gatsby/gatsby-linkedin-assets`.

## Usage

```bash
# One-shot resolve
python3 scripts/resolve-notion-image.py 3435f11c-1e6e-8180-9dca-ee6432634957

# With verbose logging to stderr
python3 scripts/resolve-notion-image.py <page-id> --verbose
```

Exit codes:
- `0` — URL written and printed to stdout (or `NO_IMAGE` for text-only posts)
- `1` — `ERROR: <reason>` printed to stderr

## How the approval monitor uses it

In the `linkedin-approval-monitor` scheduled task, between the "find approved posts
scheduled for today" step and the "call Zapier" step:

```bash
# Clone the latest resolver on each run (so script updates propagate without
# redeploying the task prompt):
git clone --depth 1 https://github.com/azabarsky-gatsby/gatsby-linkedin-assets.git /tmp/gla

# For each page:
RESULT=$(python3 /tmp/gla/scripts/resolve-notion-image.py "$PAGE_ID" 2>&1)
case "$RESULT" in
  NO_IMAGE)     # text-only post, omit Post Image from Zapier ;;
  ERROR:*)      # mark Failed, post to Slack ;;
  https://*)    # use this as the Post Image argument to Zapier ;;
esac
```

## Filename convention

`<8-hex-chars>-<slugified-title>.<ext>` — e.g.
`3435f11c-one-guest-list-always-current.png`

Deterministic so re-runs hit the same URL. The first 8 chars of the page UUID prevent
collisions between posts that share a title prefix.

## Edge cases

- **Signed S3 URL expired (~1h):** we fetch the page and download in one shot, so this
  never happens in practice. If it does, re-run the resolver.
- **GitHub push of identical content:** `git status --porcelain` short-circuits. No
  commit is created; the existing public URL is returned.
- **Cloudflare Pages deploy delay:** we poll up to 180s. If the deploy exceeds that
  (typical is 20-60s), the resolver errors out and the monitor should mark the post
  Failed with a retry hint.
- **Multiple files in `Post Image`:** LinkedIn company updates take one image, so we
  take the first entry. If you need a carousel, use LinkedIn's native carousel feature
  (not supported here).
