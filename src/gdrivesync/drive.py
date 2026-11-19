"""Thin wrapper over the Google Drive v3 API."""

from __future__ import annotations

import random
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable, TypeVar

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_APPS_PREFIX = "application/vnd.google-apps."

FILE_FIELDS = (
    "id, name, mimeType, parents, md5Checksum, size, modifiedTime, "
    "headRevisionId, trashed, webViewLink, ownedByMe, capabilities/canDownload"
)

T = TypeVar("T")

_RETRY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}


def _should_retry(err: HttpError) -> bool:
    status = err.resp.status
    if status == 429 or status >= 500:
        return True
    if status == 403:
        try:
            reasons = {e.get("reason") for e in err.error_details or []}
        except Exception:
            reasons = set()
        return bool(reasons & _RETRY_REASONS)
    return False


def with_retry(fn: Callable[[], T], attempts: int = 6) -> T:
    """Call fn, retrying with exponential backoff + jitter on transient errors."""
    for i in range(attempts):
        try:
            return fn()
        except HttpError as err:
            if i == attempts - 1 or not _should_retry(err):
                raise
            time.sleep(min(2**i, 32) + random.random())
    raise AssertionError("unreachable")


def is_google_doc(mime: str) -> bool:
    return mime.startswith(GOOGLE_APPS_PREFIX) and mime not in (FOLDER_MIME, SHORTCUT_MIME)


class DriveClient:
    def __init__(self, credentials: Any):
        self._svc = build("drive", "v3", credentials=credentials, cache_discovery=False)

    # --- account -----------------------------------------------------------
    def about(self) -> dict:
        return with_retry(
            lambda: self._svc.about()
            .get(fields="user(displayName, emailAddress), storageQuota")
            .execute()
        )

    def root_id(self) -> str:
        return with_retry(lambda: self._svc.files().get(fileId="root", fields="id").execute())["id"]

    # --- listing -----------------------------------------------------------
    def list_children(self, folder_id: str) -> Iterator[dict]:
        q = f"'{folder_id}' in parents and trashed = false"
        yield from self._paged_files(q)

    def list_all(self) -> Iterator[dict]:
        """Every non-trashed item owned by me in My Drive."""
        yield from self._paged_files("trashed = false and 'me' in owners")

    def _paged_files(self, q: str) -> Iterator[dict]:
        token = None
        while True:
            resp = with_retry(
                lambda: self._svc.files()
                .list(
                    q=q,
                    spaces="drive",
                    pageSize=1000,
                    pageToken=token,
                    fields=f"nextPageToken, files({FILE_FIELDS})",
                )
                .execute()
            )
            yield from resp.get("files", [])
            token = resp.get("nextPageToken")
            if not token:
                return

    # --- changes -----------------------------------------------------------
    def start_page_token(self) -> str:
        return with_retry(lambda: self._svc.changes().getStartPageToken().execute())[
            "startPageToken"
        ]

    def changes_since(self, page_token: str) -> tuple[list[dict], str]:
        """Return (changes, new_start_page_token)."""
        changes: list[dict] = []
        token = page_token
        while True:
            resp = with_retry(
                lambda: self._svc.changes()
                .list(
                    pageToken=token,
                    spaces="drive",
                    pageSize=1000,
                    includeRemoved=True,
                    fields=f"nextPageToken, newStartPageToken, "
                    f"changes(fileId, removed, time, file({FILE_FIELDS}))",
                )
                .execute()
            )
            changes.extend(resp.get("changes", []))
            if "newStartPageToken" in resp:
                return changes, resp["newStartPageToken"]
            token = resp["nextPageToken"]

    # --- content -----------------------------------------------------------
    def download(self, file_id: str, dest: Path) -> None:
        """Download binary content to dest (caller handles atomic rename)."""
        request = self._svc.files().get_media(fileId=file_id)
        with open(dest, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = with_retry(downloader.next_chunk)
