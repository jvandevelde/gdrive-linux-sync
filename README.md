# gdrive-linux-sync

Dropbox-style Google Drive sync for Ubuntu, with Nautilus integration.
Personal project — see [docs/DESIGN.md](docs/DESIGN.md).

**Status:** milestone 2 done — two-way sync as a background service. Nautilus integration is next.

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
   gdrivesync sync             # one two-way pass, then exit
   ```
4. Run it in the background:
   ```sh
   mkdir -p ~/.config/systemd/user
   cp packaging/systemd/gdrivesync.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now gdrivesync
   journalctl --user -u gdrivesync -f      # watch what it's doing
   ```
   The service runs the initial `pull` itself if you haven't yet.

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
- `pull` itself is download-only; `sync` / the service do both directions.

## How two-way sync behaves

| You do | Result |
|---|---|
| Add / edit / delete a file locally | Uploaded / updated / moved to Drive's trash |
| Rename or move a file or folder locally | Renamed/moved on Drive (no re-upload) |
| Change something on drive.google.com | Applied locally within ~45 s (configurable `poll_interval_s`) |
| Edit the same file on both sides | Drive's version wins the name; yours is kept as `… (conflicted copy …)` and uploaded too |
| Edit on one side, delete on the other | The edit wins; the file comes back |
| Rename/delete a `.gdoc` link file | Renames / trashes the Google Doc |

Safety nets: deleted files go to the desktop trash or Drive's trash, never straight to
oblivion; the service refuses to sync deletions if the sync folder is missing, or if more
than half (and over 100) of your files vanish at once — run
`gdrivesync sync --allow-mass-delete` if that was intentional. Files still being written
(modified in the last few seconds) are uploaded once they settle.

Ignored by default: `*.part *.swp *.tmp *~ ~$* .~lock.*# .#* .goutputstream-* .directory`.
Add your own patterns (gitignore-style) to `~/.config/gdrivesync/ignore`.

## Tests

```sh
pytest
```
