"""Thin wrapper over the Google Drive v3 API."""

from __future__ import annotations

import mimetypes
import random
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_APPS_PREFIX = "application/vnd.google-apps."

FILE_FIELDS = (
    "id, name, mimeType, parents, md5Checksum, size, createdTime, modifiedTime, "
    "headRevisionId, trashed, webViewLink, ownedByMe, capabilities/canDownload"
)

RESUMABLE_THRESHOLD = 5 * 1024 * 1024
CHUNK_SIZE = 8 * 1024 * 1024

T = TypeVar("T")

# HTTP statuses changes.list answers with when the page token itself is bad
# (Google documents tokens as non-expiring, but a token from a restored backup,
# another account, or corruption is rejected; reports cite 400 and 410).
INVALID_TOKEN_STATUSES = {400, 404, 410}


class InvalidPageToken(RuntimeError):
    """Drive rejected our saved change-feed position."""


def _q(s: str) -> str:
    """Escape a value for a Drive query string literal."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

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
    """Drive API client. Safe to share across threads: the underlying
    googleapiclient service (httplib2) is not thread-safe, so each thread
    lazily builds its own."""

    def __init__(self, credentials: Any):
        self._creds = credentials
        self._local = threading.local()
        self._ids: list[str] = []
        self._ids_lock = threading.Lock()

    @property
    def _svc(self) -> Any:
        svc = getattr(self._local, "svc", None)
        if svc is None:
            svc = build("drive", "v3", credentials=self._creds, cache_discovery=False)
            self._local.svc = svc
        return svc

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
    def get_file(self, file_id: str) -> dict | None:
        """File metadata, or None if it no longer exists / isn't accessible."""
        try:
            return with_retry(
                lambda: self._svc.files().get(fileId=file_id, fields=FILE_FIELDS).execute()
            )
        except HttpError as err:
            if err.resp.status == 404:
                return None
            raise

    def list_children(self, folder_id: str) -> Iterator[dict]:
        q = f"'{_q(folder_id)}' in parents and trashed = false"
        yield from self._paged_files(q)

    def find_children(self, parent_id: str, name: str) -> list[dict]:
        q = f"'{_q(parent_id)}' in parents and name = '{_q(name)}' and trashed = false"
        return list(self._paged_files(q))

    def list_all(self) -> Iterator[dict]:
        """Every non-trashed item visible to me. Callers build the My Drive
        tree from the root; items not reachable from it (e.g. "Shared with
        me") are ignored. Includes others' files inside my folders."""
        yield from self._paged_files("trashed = false")

    def list_trashed(self) -> Iterator[dict]:
        """Everything currently in Drive's trash (kept there ~30 days)."""
        yield from self._paged_files("trashed = true")

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
            try:
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
            except HttpError as err:
                if err.resp.status in INVALID_TOKEN_STATUSES:
                    raise InvalidPageToken(
                        f"Drive rejected change-feed position {token!r} (HTTP {err.resp.status})"
                    ) from err
                raise
            changes.extend(resp.get("changes", []))
            if "newStartPageToken" in resp:
                return changes, resp["newStartPageToken"]
            token = resp["nextPageToken"]

    # --- content -----------------------------------------------------------
    def download(self, file_id: str, dest: Path) -> None:
        """Download binary content to dest (caller handles atomic rename)."""
        request = self._svc.files().get_media(fileId=file_id)
        with open(dest, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=CHUNK_SIZE)
            done = False
            while not done:
                _, done = with_retry(downloader.next_chunk)

    # --- writes ------------------------------------------------------------
    def generate_id(self) -> str:
        """A fresh Drive file id to create a file with (makes creates idempotent)."""
        with self._ids_lock:
            if not self._ids:
                resp = with_retry(
                    lambda: self._svc.files()
                    .generateIds(count=20, space="drive", type="files")
                    .execute()
                )
                self._ids.extend(resp["ids"])
            return self._ids.pop()

    def _create_idempotent(self, file_id: str | None, do: Callable[[], dict]) -> dict:
        """Run a create. If it fails but a file with our pre-generated id exists,
        an earlier attempt (retry after a timeout, or before a crash) already
        succeeded — return that file instead of failing or duplicating."""
        try:
            return do()
        except HttpError:
            if file_id:
                existing = self.get_file(file_id)
                if existing is not None:
                    return existing
            raise

    def create_folder(self, name: str, parent_id: str, file_id: str | None = None) -> dict:
        body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        if file_id:
            body["id"] = file_id
        return self._create_idempotent(
            file_id,
            lambda: with_retry(
                lambda: self._svc.files().create(body=body, fields=FILE_FIELDS).execute()
            ),
        )

    def upload_new(
        self,
        path: Path,
        name: str,
        parent_id: str,
        mtime: float | None = None,
        file_id: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"name": name, "parents": [parent_id]}
        if mtime is not None:
            body["modifiedTime"] = _rfc3339(mtime)
        if file_id:
            body["id"] = file_id
        return self._create_idempotent(
            file_id,
            lambda: self._upload(
                lambda media: self._svc.files().create(
                    body=body, media_body=media, fields=FILE_FIELDS
                ),
                path,
            ),
        )

    def update_content(self, file_id: str, path: Path, mtime: float | None = None) -> dict:
        body: dict[str, Any] = {}
        if mtime is not None:
            body["modifiedTime"] = _rfc3339(mtime)
        return self._upload(
            lambda media: self._svc.files().update(
                fileId=file_id, body=body, media_body=media, fields=FILE_FIELDS
            ),
            path,
        )

    def update_metadata(
        self,
        file_id: str,
        *,
        name: str | None = None,
        add_parent: str | None = None,
        remove_parent: str | None = None,
    ) -> dict:
        body = {"name": name} if name else {}
        kw = {}
        if add_parent:
            kw["addParents"] = add_parent
        if remove_parent:
            kw["removeParents"] = remove_parent
        return with_retry(
            lambda: self._svc.files()
            .update(fileId=file_id, body=body, fields=FILE_FIELDS, **kw)
            .execute()
        )

    def trash(self, file_id: str) -> None:
        try:
            with_retry(
                lambda: self._svc.files()
                .update(fileId=file_id, body={"trashed": True}, fields="id")
                .execute()
            )
        except HttpError as err:
            if err.resp.status != 404:
                raise

    def _upload(self, make_request: Callable[[MediaFileUpload], Any], path: Path) -> dict:
        size = path.stat().st_size
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        resumable = size > RESUMABLE_THRESHOLD
        media = MediaFileUpload(str(path), mimetype=mime, resumable=resumable, chunksize=CHUNK_SIZE)
        request = make_request(media)
        if not resumable:
            return with_retry(request.execute)
        resp = None
        while resp is None:
            _, resp = with_retry(request.next_chunk)
        return resp
