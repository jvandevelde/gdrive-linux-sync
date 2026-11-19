# gdrive-linux-sync

Dropbox-style Google Drive sync for Ubuntu, with Nautilus integration.
Personal project — see [docs/DESIGN.md](docs/DESIGN.md).

**Status:** milestone 1 in progress (login, account info, remote listing).

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
   ```

> Note: with the consent screen in "Testing" mode, refresh tokens expire after
> 7 days. Publishing the app to "In production" (unverified is fine for
> personal use) avoids that; you'll just see an "unverified app" warning once.

## Tests

```sh
pytest
```
