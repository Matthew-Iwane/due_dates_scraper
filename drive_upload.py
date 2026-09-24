"""Upload generated week PDFs to a Google Drive folder.

Point NotebookLM at that Drive folder once and new weeks show up there
without re-uploading by hand.

One-time setup:
  1. console.cloud.google.com -> new project -> enable "Google Drive API"
  2. Credentials -> Create credentials -> OAuth client ID -> Desktop app
  3. Download the JSON, save it here as `google_credentials.json`
  4. Put the destination folder's ID in .env as GDRIVE_FOLDER_ID
     (it's the last part of the folder URL in Drive)

First run opens a browser for consent and caches `google_token.json`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

HERE = Path(__file__).parent
CREDENTIALS_FILE = HERE / "google_credentials.json"
TOKEN_FILE = HERE / "google_token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not CREDENTIALS_FILE.exists():
            raise SystemExit(
                f"Missing {CREDENTIALS_FILE.name}. See the setup steps at the top "
                "of drive_upload.py."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("drive", "v3", credentials=creds)


def upload(paths: list[Path], folder_id: str) -> None:
    """Upload each file, replacing any same-named file already in the folder."""
    service = _service()
    for path in paths:
        query = (
            f"name = '{path.name}' and '{folder_id}' in parents and trashed = false"
        )
        existing = (
            service.files()
            .list(q=query, fields="files(id)", pageSize=1)
            .execute()
            .get("files", [])
        )
        media = MediaFileUpload(str(path), resumable=True)

        if existing:
            service.files().update(fileId=existing[0]["id"], media_body=media).execute()
            print(f"  updated in Drive: {path.name}", file=sys.stderr)
        else:
            service.files().create(
                body={"name": path.name, "parents": [folder_id]},
                media_body=media,
                fields="id",
            ).execute()
            print(f"  uploaded to Drive: {path.name}", file=sys.stderr)