"""Google Drive backend.

This file is extracted from the original monolithic `save_finder.py`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request

from datetime import datetime

# Optional cloud dependencies (Google Drive). The app can run without them.
try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.http import MediaIoBaseDownload
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
except Exception:
    build = None

from .hashing import DRIVE_ZIP_NAME_DELIM, ZIP_SHA256_PREFIX_LEN
from .zip_manifest import GOOGLE_DRIVE_MANIFEST_NAME, extract_zip_contents, copy_contents_into_target

GOOGLE_DRIVE_APP_FOLDER_NAME = "SaveFinderBackups"
GOOGLE_DRIVE_ZIP_MIME = "application/zip"
GOOGLE_DRIVE_PROFILE_PARENT_MIME = "application/vnd.google-apps.folder"

GOOGLE_OAUTH_CLIENT_SECRETS_FILE = "credentials.json"
GOOGLE_OAUTH_TOKEN_FILE = "token.json"

GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _drive_enabled() -> bool:
    return build is not None


def _debug_mark(message: str, log_callback=None):
    if log_callback:
        try:
            log_callback(message)
            return
        except Exception:
            pass

    # fallback: silent


def _get_script_dir() -> str:
    # When frozen (PyInstaller), compiled modules live under an internal
    # module folder, not next to the produced .exe — resolve relative to
    # the executable itself so credentials/token are found where a user
    # would naturally place them (and where --add-data drops them).
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # Matches gui_app.py's own settings-path computation: same directory as
    # this package (save_finder/), which is where credentials/token actually live.
    return os.path.dirname(os.path.abspath(__file__))


def _safe_makedirs(p: str):
    os.makedirs(p, exist_ok=True)


def _drive_safe_filename_fragment(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(s)).strip("._-" )
    if not s:
        return "save"
    return s[:max_len]


def drive_get_credentials(log_callback=None):
    if not _drive_enabled():
        raise RuntimeError(
            "Google Drive backend unavailable (missing google-api-python-client deps)."
        )

    script_dir = _get_script_dir()
    creds_path = os.path.join(script_dir, GOOGLE_OAUTH_CLIENT_SECRETS_FILE)
    token_path = os.path.join(script_dir, GOOGLE_OAUTH_TOKEN_FILE)

    if log_callback:
        log_callback("[DRIVE] Loading/creating Google credentials...\n")

    creds = None
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, GOOGLE_DRIVE_SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            if log_callback:
                log_callback("[DRIVE] Refreshing token...\n")
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds or not creds.valid:
            if not os.path.exists(creds_path):
                raise FileNotFoundError(
                    f"Missing OAuth client secrets file: {creds_path}. Provide 'credentials.json' in the same folder as this script."
                )
            if log_callback:
                log_callback("[DRIVE] Starting OAuth flow...\n")
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, GOOGLE_DRIVE_SCOPES)
            creds = flow.run_local_server(port=0)

        _safe_makedirs(os.path.dirname(token_path) or script_dir)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def drive_get_service(creds, log_callback=None):
    if not _drive_enabled():
        raise RuntimeError("Google Drive backend unavailable.")
    if log_callback:
        log_callback("[DRIVE] Building Drive service...\n")
    return build("drive", "v3", credentials=creds)


def drive_get_or_create_app_folder(service, log_callback=None) -> str:
    q = (
        f"name = '{GOOGLE_DRIVE_APP_FOLDER_NAME}' and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    resp = service.files().list(q=q, spaces="drive", fields="files(id, name)").execute()
    files = resp.get("files", [])
    if files:
        fid = files[0]["id"]
        if log_callback:
            log_callback(
                f"[DRIVE] Using existing app folder: {GOOGLE_DRIVE_APP_FOLDER_NAME} (id={fid})\n"
            )
        return fid

    metadata = {"name": GOOGLE_DRIVE_APP_FOLDER_NAME, "mimeType": GOOGLE_DRIVE_PROFILE_PARENT_MIME}
    created = service.files().create(body=metadata, fields="id").execute()
    fid = created["id"]
    if log_callback:
        log_callback(f"[DRIVE] Created app folder: {GOOGLE_DRIVE_APP_FOLDER_NAME} (id={fid})\n")
    return fid


def drive_get_or_create_profile_folder(service, app_folder_id: str, profile_name: str, log_callback=None) -> str:
    if not profile_name or not str(profile_name).strip():
        profile_name = "Default"

    profile_name = str(profile_name).strip()

    q = (
        f"'{app_folder_id}' in parents and "
        f"mimeType = '{GOOGLE_DRIVE_PROFILE_PARENT_MIME}' and "
        f"name = '{profile_name}' and trashed = false"
    )

    resp = service.files().list(q=q, spaces="drive", fields="files(id, name)").execute()
    files = resp.get("files", [])
    if files:
        fid = files[0]["id"]
        if log_callback:
            log_callback(f"[DRIVE] Using existing profile folder: {profile_name} (id={fid})\n")
        return fid

    metadata = {
        "name": profile_name,
        "mimeType": GOOGLE_DRIVE_PROFILE_PARENT_MIME,
        "parents": [app_folder_id],
    }
    created = service.files().create(body=metadata, fields="id").execute()
    fid = created["id"]

    if log_callback:
        log_callback(f"[DRIVE] Created profile folder: {profile_name} (id={fid})\n")

    return fid


def drive_list_profiles(service, app_folder_id: str, log_callback=None, limit: int = 200):
    q = (
        f"'{app_folder_id}' in parents and "
        f"mimeType = '{GOOGLE_DRIVE_PROFILE_PARENT_MIME}' and trashed = false"
    )
    resp = service.files().list(q=q, spaces="drive", fields="files(id, name)", pageSize=limit).execute()
    return resp.get("files", [])


def drive_list_profile_backups(service, profile_folder_id: str, save_root: str | None = None, log_callback=None, limit: int = 200):
    q = (
        f"'{profile_folder_id}' in parents and trashed = false and mimeType = '{GOOGLE_DRIVE_ZIP_MIME}'"
    )
    if save_root:
        safe_root = _drive_safe_filename_fragment(save_root)
        q += f" and name contains '{safe_root}_'"  # best-effort

    q += " and not name contains 'manifest'"

    resp = service.files().list(
        q=q,
        spaces="drive",
        fields="files(id, name, modifiedTime)",
        pageSize=limit,
    ).execute()
    return resp.get("files", [])


def drive_has_sha_dedupe_match(service, profile_folder_id: str, computed_sha12: str, log_callback=None) -> bool:
    if not computed_sha12:
        return False
    needle = f"{DRIVE_ZIP_NAME_DELIM}{computed_sha12}"  # contains '__sha256_<sha12>'
    q = (
        f"'{profile_folder_id}' in parents and trashed = false and mimeType = '{GOOGLE_DRIVE_ZIP_MIME}' "
        f"and name contains '{needle}'"
    )
    resp = service.files().list(q=q, spaces="drive", fields="files(id)", pageSize=5).execute()
    return len(resp.get("files", [])) > 0


def drive_upload_backup_zip(
    service,
    profile_folder_id: str,
    zip_path: str,
    manifest: dict,
    sha256_hex: str,
    log_callback=None,
    progress_callback=None,
) -> str:
    if not sha256_hex:
        raise RuntimeError("Missing sha256 for dedupe/naming")

    timestamp = manifest.get("timestamp", "unknown")
    game_root = manifest.get("game_root", "")
    save_root = _drive_safe_filename_fragment(game_root)

    sha12 = sha256_hex[:ZIP_SHA256_PREFIX_LEN]
    filename = f"{save_root}_{timestamp}{DRIVE_ZIP_NAME_DELIM}{sha12}.zip"

    if log_callback:
        log_callback(f"[DRIVE] Upload name: {filename}\n")

    file_metadata = {
        "name": filename,
        "parents": [profile_folder_id],
        "mimeType": GOOGLE_DRIVE_ZIP_MIME,
    }
    media = MediaFileUpload(zip_path, mimetype=GOOGLE_DRIVE_ZIP_MIME, resumable=True)

    request = service.files().create(body=file_metadata, media_body=media, fields="id")
    response = None

    # MediaFileUpload already manages its own file handle for reading
    # chunks — no need to hold zip_path open here too.
    total_size = os.path.getsize(zip_path)
    uploaded_bytes = 0
    while response is None:
        status, response = request.next_chunk(num_retries=3)
        if status is not None:
            uploaded_bytes = int(status.resumable_progress or 0)
            percent = uploaded_bytes / total_size if total_size > 0 else 0.0
            if log_callback:
                log_callback(f"[DRIVE] Upload progress: {percent * 100:.1f}%\n")
            try:
                if progress_callback:
                    progress_callback("Uploading backup to Drive...", percent)
            except Exception:
                pass

    fid = response.get("id")
    if log_callback:
        log_callback(f"[DRIVE] Upload complete (id={fid})\n")

    # Store the resolved local path as file metadata so future profile
    # refreshes can read it back with a single metadata-only call instead
    # of downloading and extracting the whole zip. Best-effort and set
    # *after* the upload succeeds — never let this jeopardize the backup
    # itself. appProperties has a small per-property size limit, so skip
    # it entirely for unusually long paths rather than risk a truncated
    # (silently wrong) path — the zip-manifest fallback always works
    # regardless of length.
    original_save_path = manifest.get("original_save_path")
    if original_save_path and len(original_save_path) <= 100:
        try:
            service.files().update(
                fileId=fid,
                body={"appProperties": {"original_save_path": original_save_path}},
            ).execute()
        except Exception as e:
            if log_callback:
                log_callback(f"[DRIVE] Could not set fast-path metadata (non-fatal): {e}\n")

    return fid


def drive_cleanup_old_backups(
    service,
    profile_folder_id: str,
    save_root: str,
    keep_file_id: str | None = None,
    log_callback=None,
):
    if not save_root:
        return

    try:
        # Cleanup needs to see every backup for this save_root, not just the
        # UI-display-sized page, or backups beyond the limit could never be
        # cleaned up.
        backups = drive_list_profile_backups(
            service,
            profile_folder_id,
            save_root=save_root,
            log_callback=log_callback,
            limit=2000,
        )
        for backup in backups:
            fid = str(backup.get("id", ""))
            if not fid or fid == (keep_file_id or ""):
                continue
            try:
                service.files().delete(fileId=fid).execute()
                if log_callback:
                    log_callback(f"[DRIVE] Removed old backup id={fid}\n")
            except Exception:
                pass
    except Exception:
        pass


def drive_get_app_property(service, file_id: str, key: str, log_callback=None) -> str | None:
    """Read a single appProperty via a metadata-only call (no content download)."""
    try:
        meta = service.files().get(fileId=file_id, fields="appProperties").execute()
        return (meta.get("appProperties") or {}).get(key)
    except Exception as e:
        if log_callback:
            log_callback(f"[DRIVE] Could not read metadata for file id={file_id}: {e}\n")
        return None


def drive_download_file(service, file_id: str, dest_path: str, log_callback=None):
    if log_callback:
        log_callback(f"[DRIVE] Downloading file id={file_id} -> {dest_path}\n")

    _safe_makedirs(os.path.dirname(dest_path) or ".")

    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    if log_callback:
        log_callback("[DRIVE] Download complete\n")


def drive_restore_backup_zip(service, file_id: str, target_dir: str, log_callback=None) -> dict:
    """Downloads zip, extracts, then copies contents/* into target_dir."""
    if log_callback:
        log_callback("[RESTORE] Starting restore...\n")

    with tempfile.TemporaryDirectory(prefix="savefinder_restore_") as tmp:
        zip_path = os.path.join(tmp, "backup.zip")
        drive_download_file(service, file_id=file_id, dest_path=zip_path, log_callback=log_callback)

        manifest_path = extract_zip_contents(zip_path, tmp, log_callback=log_callback)
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        final_target = target_dir
        if not final_target and manifest.get("original_save_path"):
            final_target = manifest.get("original_save_path")
        if not final_target:
            raise RuntimeError(
                "Restore target directory not provided and manifest has no original_save_path."
            )

        final_target = os.path.abspath(final_target)
        _safe_makedirs(final_target)

        stats = copy_contents_into_target(tmp, final_target, log_callback=log_callback)
        return {"manifest": manifest, "target": final_target, "stats": stats}



