# gdrive-linux-sync

Dropbox-style Google Drive sync for Ubuntu, with Nautilus integration.
Personal project — see [docs/DESIGN.md](docs/DESIGN.md).

**Status:** milestone 2 done — two-way sync of one or more folder pairs as a background
service. Nautilus integration is next.

## Setup

1. In [Google Cloud Console](https://console.cloud.google.com/): create a project,
   enable the **Google Drive API**, configure the OAuth consent screen
   (External, add yourself as a test user), and create an OAuth client of type
   **Desktop app**. Download the JSON to `~/.config/gdrivesync/client_secret.json`.
2. Install:
   ```sh
   pipx install -e .        # or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
   ```
3. Choose what to sync — one or more **pairs** of local folder ⇄ Drive folder:
   ```sh
   gdrivesync login
   gdrivesync ls /                              # browse Drive (ls /Work, ls <id>, …)
   gdrivesync pair add ~/'Google Drive' /       # all of My Drive…
   # …or separate folders instead (pairs can't overlap):
   gdrivesync pair add ~/Work /Work
   gdrivesync pair add ~/Pictures/Phone /Photos/Phone --create
   gdrivesync pair list
   gdrivesync pull --dry-run                    # preview the first download
   gdrivesync sync                              # one two-way pass for every pair, then exit
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

- Mirrors each pair's Drive folder into its local folder, 4 downloads in parallel,
  verifying each file's MD5 and setting its modified time to Drive's.
- Google Docs/Sheets/Slides become small `.gdoc`/`.gsheet`/`.gslides` JSON files with the link.
- Re-running is cheap: unchanged files are skipped, renames/moves on Drive become local renames.
- A local file that differs from Drive is **never overwritten** — it's renamed to
  `name (conflicted copy <date>).ext` and the Drive version is downloaded.
- `pull` itself is download-only; `sync` / the service do both directions.

## Configuration

| What | How |
|---|---|
| Folders to sync | `gdrivesync pair add/remove/list` |
| Skip a subfolder (selective sync) | `gdrivesync exclude add ~/Google\ Drive/Videos` — removes the local copy on the next sync (it stays on Drive; waits for any not-yet-uploaded changes inside first). `exclude remove <path>` brings it back, `exclude list` shows them. |
| Ignore files by pattern | `~/.config/gdrivesync/ignore` (gitignore-style) |
| How often to check Drive | `gdrivesync config --poll-interval 30` (seconds, default 45) |
| Parallel downloads in `pull` | `gdrivesync config --max-transfers 8` |
| Full comparison with Drive | `gdrivesync config --full-check-hours 12` (default 24, `0` = off). Run one now: `gdrivesync sync --full` (add `--dry-run` to preview) |

Everything lives in `~/.config/gdrivesync/settings.json`; restart the service after
changes (`systemctl --user restart gdrivesync`). Older single-folder configs are
migrated automatically to a pair called "My Drive".

## Other devices and missed changes

Your phone, the web and other computers all change the same Drive; their changes arrive
through Drive's change feed within one poll interval. Two safety nets cover the feed itself:

- **Rejected feed position** (e.g. after restoring an old backup of the sync state): detected
  automatically. The service compares the whole folder with Drive, applies the differences
  with the usual rules (local edits kept, conflicts → conflicted copies, nothing re-downloaded
  that hasn't changed), and continues from a fresh position. If Drive rejects it again within
  10 minutes the service stops retrying and logs an error instead of re-listing Drive in a loop.
- **Daily full check**: every 24 h each pair is compared with Drive in full. If that finds
  anything the feed missed, it's applied and a warning is logged
  (`journalctl --user -u gdrivesync | grep "full check"`). `gdrivesync pair list` shows when
  each pair was last checked.

## Dry run and operation log

```sh
gdrivesync sync --dry-run           # every change it *would* make, both directions; changes nothing
gdrivesync sync --dry-run --pair Work
gdrivesync pull --dry-run           # same, for the initial download only
gdrivesync log                      # last 50 operations actually performed
gdrivesync log -f                   # follow live (like tail -f)
gdrivesync log -n 0 --pair Work     # everything for one pair
gdrivesync log --json | jq .        # raw records
```

The dry run uses the real sync logic on a throwaway copy of the sync state: Drive is
only read and your local files aren't touched. Example output:

```
== My Drive: /home/you/Google Drive ⇄ Drive /  (dry run)
  would download Docs/report.pdf (1.2 MB)
  would move local Old -> Renamed — moved on Drive
  would upload new file Notes/todo.txt (312 B)
  would trash on Drive old-draft.txt — deleted locally
4 operation(s) would be performed. Nothing was changed.
```

Every change made by `pull`, `sync`, `doctor --fix` or the service is appended as a JSON
line to `~/.local/state/gdrivesync/operations.log` (rotated at 10 MB, 5 old files kept)
and also appears in the service's journal.

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

Interrupted uploads (crash, power loss, network drop) are resumed under the same
Drive file id, so they never leave duplicates. To check for and clean up any
duplicates from other causes: `gdrivesync doctor` (add `--fix` to trash them).

Ignored by default: `*.part *.swp *.tmp *~ ~$* .~lock.*# .#* .goutputstream-* .directory`.
Add your own patterns (gitignore-style) to `~/.config/gdrivesync/ignore`.

## Tests

```sh
pytest
```
