"""OAuth 2.0 (desktop loopback flow) using the user's own client ID."""

from __future__ import annotations

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from gdrivesync import config

SCOPES = ["https://www.googleapis.com/auth/drive"]


class AuthError(RuntimeError):
    pass


def _save(creds: Credentials) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(config.TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())


def login() -> Credentials:
    """Run the interactive browser login and persist the token."""
    if not config.CLIENT_SECRET_FILE.exists():
        raise AuthError(
            f"Missing OAuth client file: {config.CLIENT_SECRET_FILE}\n"
            "Create a 'Desktop app' OAuth client in Google Cloud Console "
            "(with the Drive API enabled) and save its JSON there."
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.CLIENT_SECRET_FILE), SCOPES
    )
    creds = flow.run_local_server(port=0, open_browser=True)
    _save(creds)
    return creds


def load_credentials() -> Credentials:
    """Load stored credentials, refreshing if expired."""
    if not config.TOKEN_FILE.exists():
        raise AuthError("Not logged in. Run `gdrivesync login`.")
    creds = Credentials.from_authorized_user_file(str(config.TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save(creds)
        else:
            raise AuthError("Stored token is invalid. Run `gdrivesync login`.")
    return creds


def logout() -> None:
    config.TOKEN_FILE.unlink(missing_ok=True)
