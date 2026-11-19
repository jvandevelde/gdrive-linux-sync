# gdrive-linux-sync

Dropbox-style Google Drive sync for Ubuntu, with Nautilus integration.
Personal project — see [docs/DESIGN.md](docs/DESIGN.md).

**Status:** milestone 1 done — one-way `pull` of My Drive. Two-way sync is next.

## Setup

1. In [Google Cloud Console](https://console.cloud.google.com/): create a project,
   enable the **Google Drive API**, configure the OAuth consent screen
   (External, add yourself as a test user), and create an OAuth client of type
   **Desktop app**. Download the JSON to `~/.config/gdrivesync/client_secret.json`.
2. Install:
   ```sh
   pipx install -e .        # or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
   ```
3. Use:
   ```sh
   gdrivesync login
   gdrivesync whoami
   gdrivesync ls            # list My Drive root
   gdrivesync config --sync-root ~/GoogleDrive
   gdrivesync pull --dry-run   # preview
   gdrivesync pull             # mirror My Drive locally (safe to re-run)
   ```

> Note: with the consent screen in "Testing" mode, refresh tokens expire after
> 7 days. Publishing the app to "In production" (unverified is fine for
> personal use) avoids that; you'll just see an "unverified app" warning once.

## What `pull` does

- Mirrors My Drive into the sync folder (default `~/Google Drive`), 4 downloads in parallel,
  verifying each file's MD5 and setting its modified time to Drive's.
- Google Docs/Sheets/Slides become small `.gdoc`/`.gsheet`/`.gslides` JSON files with the link.
- Re-running is cheap: unchanged files are skipped, renames/moves on Drive become local renames.
- A local file that differs from Drive is **never overwritten** — it's renamed to
  `name (conflicted copy <date>).ext` and the Drive version is downloaded.
- Not yet: uploading, deleting local files removed from Drive, shortcuts, Shared Drives.

## Tests

```sh
pytest
```
