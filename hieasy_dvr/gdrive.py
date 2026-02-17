"""
Google Drive upload module for DVR recordings.

Uses a Google Cloud service account for headless (no-browser) authentication.

Setup Guide:
  1. Go to https://console.cloud.google.com → create a project
  2. Enable "Google Drive API" (APIs & Services → Enable APIs)
  3. Create a Service Account (IAM & Admin → Service Accounts → Create)
  4. Create a JSON key (Service Account → Keys → Add Key → JSON)
  5. Save the JSON key file (e.g., /opt/dvr/gdrive-credentials.json)
  6. Create a folder in your Google Drive for DVR recordings
  7. Share that folder with the service account email
     (the email is in the JSON file under "client_email")
  8. Copy the folder ID from the Drive URL:
     https://drive.google.com/drive/folders/<FOLDER_ID>
  9. Configure in dvr.env:
       DVR_GDRIVE_ENABLED=true
       DVR_GDRIVE_CREDENTIALS=/opt/dvr/gdrive-credentials.json
       DVR_GDRIVE_FOLDER_ID=<your-folder-id>

Required packages:
  pip3 install google-api-python-client google-auth
"""

import os
import logging

log = logging.getLogger('dvr.gdrive')

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    HAS_GOOGLE_API = True
except ImportError:
    HAS_GOOGLE_API = False


class GDriveUploader:
    """Uploads files to Google Drive via a service account."""

    def __init__(self, credentials_file, folder_id=None):
        if not HAS_GOOGLE_API:
            raise RuntimeError(
                'Google Drive API not installed. Run:\n'
                '  pip3 install google-api-python-client google-auth'
            )
        if not os.path.isfile(credentials_file):
            raise FileNotFoundError(f'Credentials file not found: {credentials_file}')

        creds = service_account.Credentials.from_service_account_file(
            credentials_file,
            scopes=['https://www.googleapis.com/auth/drive.file'],
        )
        self.service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        self.folder_id = folder_id
        self._subfolder_cache = {}  # name → id
        self.email = creds.service_account_email
        log.info('Google Drive: authenticated as %s', self.email)

    # ── Public API ──────────────────────────────────────

    def upload(self, filepath, filename=None, folder_id=None):
        """Upload a local file to Drive. Returns the Drive file ID."""
        if filename is None:
            filename = os.path.basename(filepath)
        folder = folder_id or self.folder_id
        meta = {'name': filename}
        if folder:
            meta['parents'] = [folder]

        media = MediaFileUpload(filepath, resumable=True)
        result = self.service.files().create(
            body=meta, media_body=media, fields='id,name,size',
        ).execute()
        log.info('Uploaded %s → Drive (id=%s, %s bytes)',
                 filename, result['id'], result.get('size', '?'))
        return result['id']

    def ensure_subfolder(self, name, parent_id=None):
        """Get or create a subfolder inside the upload root. Cached."""
        if name in self._subfolder_cache:
            return self._subfolder_cache[name]
        parent = parent_id or self.folder_id
        if not parent:
            return None
        # Search for existing folder
        q = (f"name='{name}' and '{parent}' in parents "
             f"and mimeType='application/vnd.google-apps.folder' "
             f"and trashed=false")
        hits = self.service.files().list(q=q, fields='files(id)').execute()
        files = hits.get('files', [])
        if files:
            fid = files[0]['id']
        else:
            body = {
                'name': name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [parent],
            }
            fid = self.service.files().create(body=body, fields='id').execute()['id']
            log.info('Created Drive subfolder: %s (%s)', name, fid)
        self._subfolder_cache[name] = fid
        return fid

    def list_files(self, folder_id=None, limit=50):
        """List files in a Drive folder (newest first)."""
        fid = folder_id or self.folder_id
        q = f"'{fid}' in parents and trashed=false" if fid else 'trashed=false'
        resp = self.service.files().list(
            q=q, pageSize=limit,
            fields='files(id,name,size,createdTime)',
            orderBy='createdTime desc',
        ).execute()
        return resp.get('files', [])

    def delete(self, file_id):
        """Delete a file from Drive."""
        self.service.files().delete(fileId=file_id).execute()
        log.info('Deleted Drive file %s', file_id)
