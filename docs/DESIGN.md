# gdrive-linux-sync — Design

A Dropbox-style Google Drive sync client for Ubuntu/GNOME, with Nautilus
integration. Personal-use tool.

## Scope (v1)

| In scope | Out of scope (for now) |
|---|---|
| **My Drive** only | Shared Drives, "Shared with me" |
| **Full mirror** of My Drive (minus excluded folders) | On-demand / placeholder files (would need FUSE) |
| Two-way sync with conflict copies | Real-time collaboration, Office conversion |
| Nautilus status icons + context menu | Flatpak/Snap packaging |
| Tray icon + small settings window | Public distribution / Google app verification |
| All Python | |

Because this is personal use, each install uses **the user's own OAuth client ID**
(created in Google Cloud Console as a "Desktop app"). That avoids Google's
restricted-scope verification for the full `drive` scope.

## Architecture

```
┌──────────────────────┐   D-Bus    ┌──────────────────────────────┐
│ Nautilus extension   │◄──────────►│  gdrivesyncd (systemd --user)│
│ (nautilus-python)    │            │  ┌────────────┐ ┌──────────┐ │
└──────────────────────┘            │  │ Local      │ │ Remote   │ │
┌──────────────────────┐            │  │ watcher    │ │ poller   │ │
│ Tray + settings      │◄──────────►│  │ (inotify)  │ │(changes) │ │
│ (GTK4 / libadwaita)  │            │  └─────┬──────┘ └────┬─────┘ │
└──────────────────────┘            │        ▼             ▼       │
                                    │   Reconciler ◄─► SQLite DB   │
                                    │        ▼                     │
                                    │   Transfer queue (up/down)   │
                                    └──────────────┬───────────────┘
                                                   ▼
                                           Google Drive API v3
```

### Python environments

- **Daemon + CLI** (`src/gdrivesync`): installed in its own venv (e.g. `pipx`),
  free to use PyPI deps (`google-api-python-client`, `google-auth-oauthlib`,
  `watchdog`, ...).
- **Nautilus extension** (`nautilus/`): runs *inside Nautilus' process* with the
  system Python. It must only use `gi` (PyGObject) from Ubuntu's packages —
  no PyPI deps. It talks to the daemon purely over D-Bus.
- **Tray/settings UI**: system Python + PyGObject (GTK4, libadwaita,
  AyatanaAppIndicator3). Also D-Bus only.

### Daemon components

1. **Auth** (`auth.py`) — `InstalledAppFlow` loopback flow. Client secret at
   `~/.config/gdrivesync/client_secret.json`; token at
   `~/.local/share/gdrivesync/token.json` (mode 0600). Could move to libsecret
   later.
2. **Drive client** (`drive.py`) — thin wrapper over Drive v3: listing,
   `changes.list` paging, downloads, resumable uploads, retry with exponential
   backoff on 403 `rateLimitExceeded`/`userRateLimitExceeded`, 429, 5xx.
3. **State DB** (`state.py`) — SQLite at `~/.local/share/gdrivesync/state.db`.
   One row per synced item recording the *last agreed state* ("base") of both
   sides. This is what makes three-way reconciliation possible.
4. **Remote poller** — every 30–60 s calls `changes.list(pageToken)`; persists
   the new `startPageToken` only after changes are applied.
5. **Local watcher** — `watchdog` (inotify) for live events, debounced; plus a
   periodic full rescan (inotify can overflow / hit `max_user_watches`).
6. **Reconciler** — for each item compare *local*, *remote* and *base*:

   | Local vs base | Remote vs base | Action |
   |---|---|---|
   | same | same | nothing |
   | changed | same | upload |
   | same | changed | download |
   | changed | changed | if contents equal (md5) → just update base; else **conflict** |
   | deleted | same | trash remotely |
   | same | deleted/trashed | delete locally (move to local trash) |
   | deleted | changed | re-download (edit wins over delete) |
   | changed | deleted | re-upload (edit wins over delete) |

   Conflict: keep remote as the canonical name, rename local to
   `name (conflicted copy YYYY-MM-DD HHMM).ext` and upload it.
7. **Transfer queue** — bounded worker pool (default 4); downloads to a temp
   file in `<sync root>/.gdrivesync-tmp/` then atomic `rename`; uploads use
   resumable sessions for files > 5 MB.

### Change detection details

- Remote "changed" = `md5Checksum` (or `headRevisionId`) differs from base;
  rename/move = `name`/`parents` differ.
- Local "changed" = size or mtime differ from base → then hash to confirm
  (avoids spurious uploads after `touch`).
- Local renames: inotify `MOVED_FROM`/`MOVED_TO` pairs → Drive `files.update`
  with new name/parent (no re-upload). On rescan, unmatched delete+create with
  equal md5 is treated as a move.

### Drive quirks & how we handle them

| Quirk | Handling |
|---|---|
| Google Docs/Sheets/Slides/etc. have no binary content | Write a small `.gdoc`/`.gsheet`/`.gslides` JSON file with the URL; Nautilus "Open" goes to browser. Never uploaded back. |
| Duplicate names in the same folder | Second+ copies get `name (2).ext` locally; mapping is by `fileId`, not name. |
| `/` and other odd characters in names | `/` → `∕` (U+2215) locally, mapped back on upload. |
| Shortcuts (`application/vnd.google-apps.shortcut`) | Skipped in v1 (optionally `.desktop` link later). |
| Multiple parents | Legacy only; use first parent. |
| Files we can't download (abuse flag, no permission) | Mark as error, surface in UI, don't retry forever. |
| Trash | Remote deletes from us go to Drive trash; local deletes from remote go to the freedesktop trash (`gio trash`). |

### Ignore rules

Built-in: `.gdrivesync-tmp/`, `*.part`, `*.swp`, `~$*`, `.~lock.*#`,
`.goutputstream-*`. User patterns in `~/.config/gdrivesync/ignore` (gitignore
syntax). Excluded remote folders (selective sync) stored in config.

## D-Bus API (session bus)

Name: `io.github.jvandevelde.GDriveSync`, object `/io/github/jvandevelde/GDriveSync`

| Member | Signature | Purpose |
|---|---|---|
| `GetFileStatus(path)` | `s → s` | `synced` / `syncing` / `error` / `conflict` / `ignored` / `unknown` |
| `GetFileStatuses(paths)` | `as → a{ss}` | Batched, for directory views |
| `GetShareLink(path)` | `s → s` | `webViewLink` |
| `Pause()` / `Resume()` | | |
| `GetState()` | `→ a{sv}` | overall state, queue sizes, last error |
| signal `StatusChanged(paths)` | `as` | Nautilus calls `invalidate_extension_info()` on affected files |

## Nautilus extension

`nautilus/gdrivesync_nautilus.py`, installed to
`~/.local/share/nautilus-python/extensions/` (needs `python3-nautilus`).

- **InfoProvider** — emblem per file under the sync root + a "Sync status"
  column (ColumnProvider) as fallback, since emblem display in GTK4-era Nautilus
  is limited. **To verify on the target Ubuntu release.**
- **MenuProvider** — Copy share link, Open in Google Drive, Exclude from sync.
- Must never block the UI: D-Bus calls are async with a short timeout; unknown
  status is shown as nothing.

## Tray / settings

- AyatanaAppIndicator (Ubuntu ships the AppIndicator GNOME extension).
  Menu: status line, Open folder, Pause/Resume, Settings, Quit.
- libadwaita settings window: account, sync folder, excluded folders, ignore
  patterns, bandwidth limits, activity log.
- Notifications via `Gio.Notification` for conflicts and errors.

## Layout

```
src/gdrivesync/     daemon + CLI package
  cli.py            `gdrivesync` command
  config.py         paths & settings
  auth.py           OAuth
  drive.py          Drive API wrapper
  state.py          SQLite state DB
nautilus/           Nautilus extension (system Python, gi only)
packaging/systemd/  user service unit
tests/
docs/
```

## Implementation notes (milestone 2)

- `engine.py` is **state-based**: `remote_pass()` applies the Drive change feed,
  `local_pass()` scans the tree and diffs against the DB. Both are idempotent, so
  our own writes echoing back (inotify events, Drive changes) are no-ops.
- `daemon.py` runs everything on one thread; the watchdog thread only enqueues.
  Local events are debounced (2 s quiet, max 15 s). Watcher rename events are
  applied directly so renaming a big folder is one API call; a full scan runs
  every 10 min as a safety net (every 60 s if inotify is unavailable). Remote
  polling backs off exponentially on errors.
- Remote items that fail to apply are stored in `kv.remote_retry_ids` and
  re-fetched next pass, so the change-feed position can always advance.
- Offline renames are recovered by matching vanished files to new ones by
  size + MD5 (or by the id inside a `.gdoc` stub).

### Idempotent creates (no duplicates after crashes/retries)

Drive's `files.create` isn't idempotent, so a crash or a retried request after
Drive committed — but before we recorded it — used to leave a duplicate. Now:

1. Get a file id up front from `files.generateIds` (pooled, 20 at a time).
2. Journal `(rel_path, file_id, parent_id)` in `pending_creates` **before** calling Drive.
3. Create with that id. If the call errors but `files.get(id)` finds the file,
   an earlier attempt succeeded — use it (`DriveClient._create_idempotent`).
4. Record the item, then delete the journal row.

At the start of every local pass, leftover journal rows are settled: if the item
was recorded, the row is dropped; if the local file is still there and new, the
pass resumes with the same id (Drive rejects a second file with that id, and if
the local content changed meanwhile we push an update); if the local file is gone
(deleted/renamed while we were down), any orphan on Drive is trashed.

`gdrivesync doctor [--fix]` finds pre-existing duplicates (same folder + name +
MD5, or same-named empty folders) and trashes the extras.

### Known limitations / later hardening

- Transfers run one at a time in the service (only the initial pull is parallel).
- Deleting a folder locally trashes the whole Drive folder, even if something in
  it was edited on Drive meanwhile (recoverable from Drive's trash).
- A tiny race window remains between "local file unchanged?" and overwriting it.

## Milestones

1. ✅ **Pull-only PoC** — `gdrivesync login`, `gdrivesync pull`: full download of
   My Drive into the sync folder, populate state DB, `.gdoc` stubs.
2. ✅ **Two-way engine** — local watcher, changes poller, reconciler, conflicts,
   systemd user service. Test against a scratch Google account.
3. **D-Bus + Nautilus** — status emblems/column, context menu.
4. **Tray + settings UI.**
5. **Hardening** — selective sync, large trees (100k+ files), offline/resume,
   rate limits, `.deb` packaging.
