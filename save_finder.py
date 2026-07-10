import os
import configparser
import re
import json
import urllib.request
import urllib.error
import threading
import queue

from datetime import datetime
import subprocess
import tempfile
import shutil
import hashlib
import webbrowser

import customtkinter as ctk
from tkinter import filedialog
from tkinter import simpledialog, messagebox


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


# ============================================================
# Google Drive backup/restore (OPTIONAL)
# TODO_CLOUD.md requirements:
# - UI controls: Backup / Restore (per-result actions)
# - Backend layer: OAuth, Upload ZIP, List/Search backups, Download ZIP
# - ZIP format (Option B): manifest.json + contents/* only
# - Restore flow: download -> extract -> locate target -> copy contents/* into target
# - Thread-safe UI updates via existing queue log
# ============================================================

GOOGLE_DRIVE_APP_FOLDER_NAME = "SaveFinderBackups"
GOOGLE_DRIVE_ZIP_MIME = "application/zip"
GOOGLE_DRIVE_MANIFEST_NAME = "manifest.json"
GOOGLE_DRIVE_PROFILE_PARENT_MIME = "application/vnd.google-apps.folder"

GOOGLE_OAUTH_CLIENT_SECRETS_FILE = "credentials.json"
GOOGLE_OAUTH_TOKEN_FILE = "token.json"

GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

APP_SETTINGS_FILE = "save_finder.ini"
APP_SETTINGS_SECTION = "app"
APP_SETTINGS_SELECTED_PROFILE = "selected_profile"
APP_SETTINGS_STORAGE_BACKEND = "storage_backend"
APP_SETTINGS_LOCAL_ROOT = "local_backups_root"
APP_SETTINGS_AUTO_BACKUP = "auto_backup_enabled"

# Filename format (required):
#   <save_root>_<timestamp>__sha256_<sha12>.zip
ZIP_SHA256_PREFIX_LEN = 12
DRIVE_ZIP_NAME_DELIM = "__sha256_"


def _drive_enabled() -> bool:
    return build is not None


def _get_script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _safe_makedirs(p: str):
    os.makedirs(p, exist_ok=True)


def _compute_file_hash(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _compute_directory_tree_hash(root_path: str, chunk_size: int = 1024 * 1024) -> str:
    """Hash the file tree contents deterministically, ignoring timestamps in the archive manifest."""
    if not os.path.isdir(root_path):
        raise FileNotFoundError(f"Directory not found: {root_path}")

    h = hashlib.sha256()
    for current_root, dirs, files in os.walk(root_path):
        dirs.sort()
        files.sort()

        rel_root = os.path.relpath(current_root, root_path)
        rel_root = "." if rel_root == "." else rel_root.replace("\\", "/")
        if rel_root != ".":
            h.update(rel_root.encode("utf-8"))
            h.update(b"\0")

        for fn in files:
            abs_fp = os.path.join(current_root, fn)
            rel_fp = os.path.join(rel_root, fn) if rel_root != "." else fn
            rel_fp = rel_fp.replace("\\", "/")
            h.update(rel_fp.encode("utf-8"))
            h.update(b"\0")
            with open(abs_fp, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    h.update(chunk)
    return h.hexdigest()


def _drive_safe_filename_fragment(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(s)).strip("._-")
    if not s:
        return "save"
    return s[:max_len]


def _parse_zip_name_for_fields(filename: str) -> dict:
    """Parse <save_root>_<timestamp>__sha256_<sha12>.zip."""
    base = filename
    if base.lower().endswith(".zip"):
        base = base[:-4]

    sha_idx = base.find(DRIVE_ZIP_NAME_DELIM)
    if sha_idx == -1:
        return {"save_root": None, "timestamp": None, "sha12": None}

    left = base[:sha_idx]
    right = base[sha_idx + len(DRIVE_ZIP_NAME_DELIM) :]

    sha12 = None
    if right:
        m = re.match(r"^([a-zA-Z0-9]+)", right)
        sha12 = (m.group(1) if m else right)[:ZIP_SHA256_PREFIX_LEN]

    m = re.match(r"^(?P<save_root>.+)_(?P<timestamp>[^_]+)$", left)
    if not m:
        return {"save_root": None, "timestamp": None, "sha12": sha12}

    return {"save_root": m.group("save_root"), "timestamp": m.group("timestamp"), "sha12": sha12}


def _create_zip_with_manifest(zip_path: str, manifest: dict, folder_to_backup: str, log_callback=None):
    """ZIP option B: zip contains manifest.json + contents/* (folder contents only)."""
    import zipfile

    if log_callback:
        log_callback(f"[ZIP] Creating zip: {zip_path}\n")

    _safe_makedirs(os.path.dirname(zip_path) or ".")

    folder_to_backup = os.path.abspath(folder_to_backup)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(GOOGLE_DRIVE_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))

        for root, dirs, files in os.walk(folder_to_backup):
            rel_root = os.path.relpath(root, folder_to_backup)
            for fn in files:
                abs_fp = os.path.join(root, fn)
                rel_fp = os.path.join(rel_root, fn) if rel_root != "." else fn
                arcname = os.path.join("contents", rel_fp).replace("\\", "/")
                zf.write(abs_fp, arcname=arcname)


def _extract_zip_contents(zip_path: str, extract_dir: str, log_callback=None) -> str:
    """Extract zip into extract_dir and return path to manifest.json."""
    import zipfile

    if log_callback:
        log_callback(f"[ZIP] Extracting zip: {zip_path}\n")

    _safe_makedirs(extract_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    return os.path.join(extract_dir, GOOGLE_DRIVE_MANIFEST_NAME)


def _copy_contents_into_target(zip_extract_dir: str, target_dir: str, log_callback=None) -> dict:
    """Copy contents/* into target_dir. Overwrite-safe: skip if destination exists."""
    contents_dir = os.path.join(zip_extract_dir, "contents")
    if not os.path.exists(contents_dir):
        raise FileNotFoundError(f"Missing contents directory inside extracted zip: {contents_dir}")

    copied = 0
    skipped = 0
    total = 0

    for root, dirs, files in os.walk(contents_dir):
        rel_root = os.path.relpath(root, contents_dir)
        for fn in files:
            total += 1
            src_fp = os.path.join(root, fn)
            rel_fp = os.path.join(rel_root, fn) if rel_root != "." else fn
            dst_fp = os.path.join(target_dir, rel_fp)

            _safe_makedirs(os.path.dirname(dst_fp))

            if os.path.exists(dst_fp):
                skipped += 1
                continue

            shutil.copy2(src_fp, dst_fp)
            copied += 1

    if log_callback:
        log_callback(f"[RESTORE] Copied={copied}, skipped(existing)={skipped}, total={total}\n")

    return {"copied": copied, "skipped": skipped, "total": total}


def drive_get_credentials(log_callback=None):
    if not _drive_enabled():
        raise RuntimeError("Google Drive backend unavailable (missing google-api-python-client deps).")

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
            log_callback(f"[DRIVE] Using existing app folder: {GOOGLE_DRIVE_APP_FOLDER_NAME} (id={fid})\n")
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


# -----------------------
# Local filesystem backend
# -----------------------

def _localfs_backups_root(default_root: str | None = None) -> str:
    if default_root:
        root = default_root
    else:
        root = os.path.join(_get_script_dir(), "backups")
    _safe_makedirs(root)
    return root


def localfs_list_profiles(backups_root: str, log_callback=None):
    _safe_makedirs(backups_root)
    items = []
    try:
        for name in os.listdir(backups_root):
            p = os.path.join(backups_root, name)
            if os.path.isdir(p):
                items.append({"id": p, "name": name})
    except Exception:
        pass
    return items


def localfs_get_or_create_profile_folder(backups_root: str, profile_name: str, log_callback=None) -> str:
    if not profile_name or not str(profile_name).strip():
        profile_name = "Default"
    profile_name = str(profile_name).strip()
    dest = os.path.join(backups_root, profile_name)
    _safe_makedirs(dest)
    if log_callback:
        log_callback(f"[LOCAL] Using profile folder: {dest}\n")
    return dest


def localfs_list_profile_backups(profile_folder_path: str, save_root: str | None = None, log_callback=None, limit: int = 200):
    out = []
    try:
        for fn in os.listdir(profile_folder_path):
            if not fn.lower().endswith(".zip"):
                continue
            if save_root:
                safe_root = _drive_safe_filename_fragment(save_root)
                if f"{safe_root}_" not in fn:
                    continue
            fp = os.path.join(profile_folder_path, fn)
            mtime = os.path.getmtime(fp)
            out.append({"id": fp, "name": fn, "modifiedTime": datetime.utcfromtimestamp(mtime).isoformat() + "Z"})
    except Exception:
        pass
    out.sort(key=lambda x: x.get("modifiedTime", ""), reverse=True)
    return out[:limit]


def localfs_has_sha_dedupe_match(profile_folder_path: str, computed_sha12: str, log_callback=None) -> bool:
    if not computed_sha12:
        return False
    needle = f"{DRIVE_ZIP_NAME_DELIM}{computed_sha12}"
    try:
        for fn in os.listdir(profile_folder_path):
            if needle in fn:
                return True
    except Exception:
        pass
    return False


def localfs_upload_backup_zip(profile_folder_path: str, zip_path: str, manifest: dict, sha256_hex: str, log_callback=None) -> str:
    if not sha256_hex:
        raise RuntimeError("Missing sha256 for dedupe/naming")
    timestamp = manifest.get("timestamp", "unknown")
    game_root = manifest.get("game_root", "")
    save_root = _drive_safe_filename_fragment(game_root)
    sha12 = sha256_hex[:ZIP_SHA256_PREFIX_LEN]
    filename = f"{save_root}_{timestamp}{DRIVE_ZIP_NAME_DELIM}{sha12}.zip"
    dest = os.path.join(profile_folder_path, filename)
    _safe_makedirs(os.path.dirname(dest) or ".")
    shutil.copy2(zip_path, dest)
    if log_callback:
        log_callback(f"[LOCAL] Saved backup: {dest}\n")
    return dest


def localfs_cleanup_old_backups(profile_folder_path: str, save_root: str, keep_path: str | None = None, log_callback=None):
    if not save_root:
        return
    try:
        backups = localfs_list_profile_backups(profile_folder_path, save_root=save_root, log_callback=log_callback, limit=200)
        for backup in backups:
            path = str(backup.get("id", ""))
            if not path or path == keep_path:
                continue
            try:
                os.remove(path)
                if log_callback:
                    log_callback(f"[LOCAL] Removed old backup: {path}\n")
            except Exception:
                pass
    except Exception:
        pass


def localfs_download_file(file_path: str, dest_path: str, log_callback=None):
    if log_callback:
        log_callback(f"[LOCAL] Copying file {file_path} -> {dest_path}\n")
    _safe_makedirs(os.path.dirname(dest_path) or ".")
    shutil.copy2(file_path, dest_path)


def localfs_restore_backup_zip(file_path: str, target_dir: str, log_callback=None) -> dict:
    # reuse existing restore helper: extract and copy
    if log_callback:
        log_callback("[RESTORE] Starting local restore...\n")
    with tempfile.TemporaryDirectory(prefix="savefinder_restore_") as tmp:
        tmp_zip = os.path.join(tmp, "backup.zip")
        shutil.copy2(file_path, tmp_zip)
        manifest_path = _extract_zip_contents(tmp_zip, tmp, log_callback=log_callback)
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        final_target = target_dir or manifest.get("original_save_path")
        if not final_target:
            raise RuntimeError("Restore target directory not provided and manifest has no original_save_path.")
        final_target = os.path.abspath(final_target)
        _safe_makedirs(final_target)
        stats = _copy_contents_into_target(tmp, final_target, log_callback=log_callback)
        return {"manifest": manifest, "target": final_target, "stats": stats}


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

    created = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    fid = created.get("id")
    if log_callback:
        log_callback(f"[DRIVE] Upload complete (id={fid})\n")
    return fid


def drive_cleanup_old_backups(service, profile_folder_id: str, save_root: str, keep_file_id: str | None = None, log_callback=None):
    if not save_root:
        return
    try:
        backups = drive_list_profile_backups(service, profile_folder_id, save_root=save_root, log_callback=log_callback, limit=200)
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

        manifest_path = _extract_zip_contents(zip_path, tmp, log_callback=log_callback)
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        final_target = target_dir
        if not final_target and manifest.get("original_save_path"):
            final_target = manifest.get("original_save_path")
        if not final_target:
            raise RuntimeError("Restore target directory not provided and manifest has no original_save_path.")

        final_target = os.path.abspath(final_target)
        _safe_makedirs(final_target)

        stats = _copy_contents_into_target(tmp, final_target, log_callback=log_callback)
        return {"manifest": manifest, "target": final_target, "stats": stats}


def _pick_newest_by_modifiedTime(files: list[dict]) -> dict | None:
    if not files:
        return None

    def key_fn(x):
        return x.get("modifiedTime") or ""

    files_sorted = sorted(files, key=key_fn, reverse=True)
    return files_sorted[0]


# --- BACKEND LOGIC (scan/save finder) ---

def get_steam_api_data(app_id, log_callback):
    """Pings the public Steam API to extract separate developer and publisher lists."""
    log_callback(f"   [API] Querying Steam Web API for AppID {app_id}...\n")
    url = f"https://store.steampowered.com/api/appdetails?appids={app_id}"
    devs, pubs = [], []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))

            if str(app_id) in data and data[str(app_id)]["success"]:
                app_data = data[str(app_id)]["data"]

                for d in app_data.get("developers", []):
                    clean = re.sub(r"[^a-zA-Z0-9 ]", "", d).strip().lower()
                    if clean:
                        devs.extend(clean.split())

                for p in app_data.get("publishers", []):
                    clean = re.sub(r"[^a-zA-Z0-9 ]", "", p).strip().lower()
                    if clean:
                        pubs.extend(clean.split())
    except Exception as e:
        log_callback(f"   [API] Could not fetch data from Steam: {e}\n")

    stopwords = [
        "the",
        "inc",
        "ltd",
        "studio",
        "studios",
        "entertainment",
        "software",
        "games",
        "game",
    ]
    devs = [w for w in devs if len(w) > 2 and w not in stopwords]
    pubs = [w for w in pubs if len(w) > 2 and w not in stopwords]
    return list(set(devs)), list(set(pubs))


def get_executable_keywords(game_directory):
    """Scans for game executables to find internal project codenames."""
    ignore_list = [
        "unins000",
        "uninstaller",
        "crashreportclient",
        "unitycrashhandler32",
        "unitycrashhandler64",
        "gameassembly",
        "epicwebhelper",
    ]
    keywords = []

    for root, dirs, files in os.walk(game_directory):
        for file in files:
            if file.lower().endswith(".exe"):
                base_name = os.path.splitext(file)[0].lower()
                clean_exe = re.sub(r"(-win64-shipping|-win32-shipping|64|32)$", "", base_name)

                if "redist" not in clean_exe and "setup" not in clean_exe:
                    if clean_exe not in ignore_list and len(clean_exe) > 2:
                        keywords.append(clean_exe)

    return list(set(keywords))


def get_unreal_project_name(game_directory):
    """Detects Unreal Engine structure and extracts the internal Project folder name."""
    keywords = []
    for root, dirs, files in os.walk(game_directory):
        dirs_lower = [d.lower() for d in dirs]
        if "engine" in dirs_lower:
            for d in dirs:
                ignore_dirs = [
                    "engine",
                    "binaries",
                    "content",
                    "saved",
                    "config",
                    "plugins",
                    "extras",
                    "build",
                    "intermediate",
                ]
                if d.lower() not in ignore_dirs:
                    keywords.append(d.lower())
    return list(set(keywords))


def run_save_finder(game_directory, log_callback, success_callback):
    """Executes the deep scan logic and routes output back to the GUI."""

    ini_files_to_check = [
        "steam_emu.ini",
        "steam_api.ini",
        "steam_api64.ini",
        "flt.ini",
        "forcebind.ini",
        "tenoke.ini",
        "rune.ini",
        "epic_emu.ini",
    ]

    log_callback(f"[1/3] Scanning '{game_directory}' for emulators...\n")

    if not os.path.exists(game_directory):
        log_callback("Error: The specified directory does not exist.\n")
        success_callback([])
        return

    # --- BASE KEYWORD EXTRACTION ---
    raw_game_name = os.path.basename(os.path.normpath(game_directory))
    clean_name = re.sub(r"[._](v\d+|build|update|patch).*$", "", raw_game_name, flags=re.IGNORECASE)
    clean_name = re.sub(r"[._-]", " ", clean_name).strip()

    words = clean_name.split()
    stopwords = ["the", "a", "an", "of", "in", "on", "and", "for", "to", "build", "game", "edition"]
    valid_words = [w for w in words if w.lower() not in stopwords and len(w) > 2 and not w.isdigit()]

    base_keyword = max(valid_words, key=len).lower() if valid_words else (words[0].lower() if words else raw_game_name.lower())

    found_ini_files = []
    local_save_folders = []

    for root, dirs, files in os.walk(game_directory):
        for file in files:
            if file.lower() in ini_files_to_check:
                found_ini_files.append(os.path.join(root, file))
        for d in dirs:
            if "save" in d.lower() or d.lower() == "remote":
                local_save_folders.append(os.path.join(root, d))

    if local_save_folders:
        log_callback("\n[SUCCESS] Found portable/local save folders inside the game directory:\n")
        for folder in local_save_folders:
            log_callback(f"-> {folder}\n")
        log_callback("---------------------------------------------------\n")

    # --- EMULATOR PARSING ---
    app_id = None
    if found_ini_files:
        log_callback("\n[2/3] Analyzing emulator configuration...\n")
        config = configparser.ConfigParser(strict=False)
        target_ini = found_ini_files[0]
        try:
            config.read(target_ini, encoding="utf-8")
            save_path = None
            for section in config.sections():
                for key in config[section]:
                    if key.lower() in ["savepath", "storage"]:
                        save_path = config[section][key]
                    elif key.lower() == "appid":
                        app_id = config[section][key]

            if save_path:
                log_callback(f"[SUCCESS] Found explicit Save Path in .ini:\n-> {save_path}\n")
                final_return_paths = list(local_save_folders)
                final_return_paths.append(save_path)
                success_callback(list(set(final_return_paths)))
                return
            else:
                log_callback(f"   Extracted AppID: {app_id} (No explicit SavePath inside .ini)\n")
        except Exception:
            pass

    # --- ARSENAL DEFINITIONS & SCORES ---
    log_callback("\n[3/3] Initiating Deep Scan of Windows user directories...\n")

    dev_keywords, pub_keywords = [], []
    if app_id:
        dev_keywords, pub_keywords = get_steam_api_data(app_id, log_callback)

    exe_keywords = get_executable_keywords(game_directory)
    ue_keywords = get_unreal_project_name(game_directory)

    high_priority_keywords = list(set([base_keyword] + exe_keywords + ue_keywords))

    # --- DEEP SCAN ---
    user_profile = os.environ.get("USERPROFILE", "")
    roots_to_scan = [
        os.path.join(user_profile, "Documents"),
        os.path.join(user_profile, "Documents", "My Games"),
        os.environ.get("LOCALAPPDATA", ""),
        os.path.join(user_profile, "AppData", "LocalLow"),
        os.environ.get("APPDATA", ""),
        os.path.join(user_profile, "Saved Games"),
        os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Documents", "Steam", app_id if app_id else "UNKNOWN"),
    ]

    candidate_paths = []
    all_search_terms = list(set(high_priority_keywords + dev_keywords + pub_keywords))

    for root_dir in roots_to_scan:
        if not root_dir or not os.path.exists(root_dir):
            continue

        try:
            for item in os.listdir(root_dir):
                item_path = os.path.join(root_dir, item)
                if not os.path.isdir(item_path):
                    continue

                item_lower = item.lower()
                if any(term in item_lower for term in all_search_terms):
                    candidate_paths.append(item_path)

                try:
                    for sub_item in os.listdir(item_path):
                        sub_item_path = os.path.join(item_path, sub_item)
                        if os.path.isdir(sub_item_path) and any(term in sub_item.lower() for term in all_search_terms):
                            candidate_paths.append(sub_item_path)
                except PermissionError:
                    pass
        except PermissionError:
            pass

    candidate_paths = list(set(candidate_paths))
    verified_save_directories = []

    # --- SCORING SYSTEM ROUTINE ---
    for path in candidate_paths:
        score = 0
        path_lower = path.lower()
        folder_name = os.path.basename(path).lower()

        if any(hp in folder_name for hp in high_priority_keywords):
            score += 50
        if any(s_term in path_lower for s_term in ["save", "savedgames", "saves", "remote"]):
            score += 30
        if any(dev in path_lower for dev in dev_keywords):
            score += 25
        if any(pub in path_lower for pub in pub_keywords):
            score += 10

        if score >= 40:
            verified_save_directories.append((path, score))

    verified_save_directories.sort(key=lambda x: x[1], reverse=True)

    # --- SUBFOLDER FILTERING ---
    final_roots = []
    for path, score in verified_save_directories:
        is_subpath = False
        for root_path, _ in final_roots:
            if path.startswith(root_path + os.sep):
                is_subpath = True
                break
        if not is_subpath:
            final_roots.append((path, score))

    # --- FINAL OUTPUT ---
    final_return_paths = list(local_save_folders)

    if final_roots:
        log_callback("\n[SUCCESS] Confirmed true save directories identified:\n")
        for path, score in final_roots:
            log_callback(f"-> {path} (Score: {score})\n")
            final_return_paths.append(path)

            ue_save = os.path.join(path, "Saved", "SaveGames")
            if os.path.exists(ue_save):
                log_callback(f"   [!] Verified Unreal Engine Directory Subpath:\n   {ue_save}\n")
                final_return_paths.append(ue_save)

    if final_return_paths:
        success_callback(list(set(final_return_paths)))
    else:
        log_callback("\n[FAILED] No legitimate game save data profiles cleared the verification criteria.\n")
        success_callback([])


# --- GUI INTERFACE CLASS ---

class SaveFinderApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self._log_queue = queue.Queue()
        self._log_autoscroll = True

        self.discovered_paths: list[str] = []
        self._tree_sections: list[ctk.CTkFrame] = []

        # Profiles UI state
        self._profiles_refreshing = False
        self.selected_profile_name: str | None = None
        self.profiles_panel_widgets = {}
        self._profile_rows = []
        self._settings_path = os.path.join(_get_script_dir(), APP_SETTINGS_FILE)
        self._auto_backup_state: dict[str, dict[str, object]] = {}
        self._auto_backup_in_progress: set[str] = set()
        self._auto_backup_interval_ms = 30000
        self._auto_backup_enabled = self._load_app_setting(APP_SETTINGS_AUTO_BACKUP, "0") == "1"

        # Window
        self.title("Universal Game Save Finder & Backup")
        self.geometry("1280x760")
        self.minsize(1200, 760)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # Header
        self.title_label = ctk.CTkLabel(self, text="Universal Game Save Finder", font=ctk.CTkFont(size=24, weight="bold"))
        self.title_label.pack(pady=(20, 5))

        self.subtitle_label = ctk.CTkLabel(
            self,
            text="Scan a game root folder and manage save backups (Google Drive).",
            font=ctk.CTkFont(size=14),
            text_color="gray",
        )
        self.subtitle_label.pack(pady=(0, 20))

        # Input frame
        self.selection_frame = ctk.CTkFrame(self)
        self.selection_frame.pack(fill="x", padx=30, pady=10)

        self.path_entry = ctk.CTkEntry(
            self.selection_frame,
            placeholder_text="C:\\Path\\To\\Game\\Folder",
            width=520,
            font=ctk.CTkFont(size=13),
        )
        self.path_entry.pack(side="left", padx=(15, 10), pady=15, expand=True, fill="x")

        self.browse_btn = ctk.CTkButton(self.selection_frame, text="Browse Folder", width=150, command=self.browse_folder)
        self.browse_btn.pack(side="right", padx=(0, 15), pady=15)

        # Scan button
        self.scan_btn = ctk.CTkButton(
            self,
            text="Scan for Saves",
            font=ctk.CTkFont(size=16, weight="bold"),
            height=45,
            command=self.start_scan,
        )
        self.scan_btn.pack(fill="x", padx=30, pady=15)

        # Top layout: left results, right profiles
        main_frame = ctk.CTkFrame(self)
        main_frame.pack(fill="both", expand=True, padx=30, pady=(5, 0))

        main_frame.grid_rowconfigure(1, weight=1)
        main_frame.grid_columnconfigure(0, weight=1, minsize=560)
        main_frame.grid_columnconfigure(1, weight=0, minsize=520)

        # Logs
        self.log_label = ctk.CTkLabel(main_frame, text="Console Log Output", font=ctk.CTkFont(size=14, weight="bold"))
        self.log_label.grid(row=0, column=0, columnspan=2, sticky="w", padx=0, pady=(0, 6))

        self.log_controls_frame = ctk.CTkFrame(main_frame)
        self.log_controls_frame.grid(row=1, column=0, columnspan=2, sticky="w")

        self.clear_log_btn = ctk.CTkButton(self.log_controls_frame, text="Clear Log", width=120, command=self.clear_console)
        self.clear_log_btn.pack(side="left", padx=(0, 10), pady=5)

        self.autoscroll_var = ctk.BooleanVar(value=True)
        self.autoscroll_checkbox = ctk.CTkCheckBox(
            self.log_controls_frame,
            text="Auto-scroll",
            variable=self.autoscroll_var,
            command=self._toggle_autoscroll,
        )
        self.autoscroll_checkbox.pack(side="left", pady=5)

        # Smaller log box
        self.console_output = ctk.CTkTextbox(main_frame, height=95, font=ctk.CTkFont(family="Consolas", size=12))
        self.console_output.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=(0, 0), pady=(5, 10))
        self.console_output.configure(state="disabled")

        # Results panel (left)
        self.results_frame = ctk.CTkFrame(main_frame)
        self.results_frame.grid(row=3, column=0, sticky="nsew", padx=(0, 10), pady=(0, 10))
        self.results_frame.grid_rowconfigure(0, weight=0)
        self.results_frame.grid_rowconfigure(1, weight=1)
        self.results_frame.grid_columnconfigure(0, weight=1)

        self.results_label = ctk.CTkLabel(self.results_frame, text="Detected Save Locations", font=ctk.CTkFont(size=14, weight="bold"))
        self.results_label.pack(anchor="w", padx=10, pady=(10, 0))

        self.results_controls = ctk.CTkFrame(self.results_frame, fg_color="transparent")
        self.results_controls.pack(fill="x", padx=10, pady=(0, 6))

        self.results_toggle_btn = ctk.CTkButton(self.results_controls, text="Hide Results", width=120, command=self._toggle_results_visibility)
        self.results_toggle_btn.pack(side="left", padx=(0, 4))

        self.results_scroll = ctk.CTkScrollableFrame(self.results_frame, fg_color="transparent")
        self.results_scroll.pack(fill="both", expand=True, padx=10, pady=10)
        self.results_visible = True

        # Profiles panel (right)
        self.profiles_frame = ctk.CTkFrame(main_frame)
        self.profiles_frame.grid(row=3, column=1, sticky="nsew", padx=(10, 0), pady=(0, 10))
        self.profiles_frame.grid_rowconfigure(5, weight=1)
        self.profiles_frame.grid_columnconfigure(0, weight=1)

        self.profiles_label = ctk.CTkLabel(self.profiles_frame, text="Profiles", font=ctk.CTkFont(size=14, weight="bold"))
        self.profiles_label.pack(anchor="w", padx=10, pady=(10, 0))

        self.profiles_controls = ctk.CTkFrame(self.profiles_frame, fg_color="transparent")
        self.profiles_controls.pack(fill="x", padx=10, pady=(5, 8))

        self.profiles_refresh_btn = ctk.CTkButton(self.profiles_controls, text="Refresh", width=100, command=self.refresh_profiles_ui)
        self.profiles_refresh_btn.pack(side="left", padx=(0, 8))

        self.profiles_add_btn = ctk.CTkButton(self.profiles_controls, text="Add", width=80, command=self.add_profile_ui)
        self.profiles_add_btn.pack(side="left")

        self.auto_backup_var = ctk.BooleanVar(value=self._auto_backup_enabled)
        self.auto_backup_checkbox = ctk.CTkCheckBox(
            self.profiles_controls,
            text="Auto backup saves",
            variable=self.auto_backup_var,
            command=self._on_auto_backup_toggled,
        )
        self.auto_backup_checkbox._is_packed = False

        # Storage backend selector + local backups path
        stored_backend = self._load_app_setting(APP_SETTINGS_STORAGE_BACKEND, "Drive" if _drive_enabled() else "Local") or ("Drive" if _drive_enabled() else "Local")
        if stored_backend.lower() == "drive" and not _drive_enabled():
            stored_backend = "Local"

        self.storage_backend_var = ctk.StringVar(value=stored_backend.title())
        self.storage_selector = ctk.CTkOptionMenu(self.profiles_controls, values=["Drive", "Local"], variable=self.storage_backend_var, command=lambda v: self._on_storage_backend_changed(v))
        self.storage_selector.pack(side="left", padx=(8, 6))

        self.local_root_entry = ctk.CTkEntry(self.profiles_controls, width=160, placeholder_text="Local backups root")
        self.local_root_entry.pack(side="left", padx=(6, 6))
        stored_local_root = self._load_app_setting(APP_SETTINGS_LOCAL_ROOT)
        if stored_local_root and str(stored_local_root).strip():
            self.local_root_entry.insert(0, os.path.normpath(stored_local_root))
        else:
            self.local_root_entry.insert(0, os.path.join(_get_script_dir(), "backups"))

        self.local_root_browse = ctk.CTkButton(self.profiles_controls, text="Browse", width=60, command=self._choose_local_root)
        self.local_root_browse.pack(side="left", padx=(4, 0))

        self.open_profile_folder_btn = ctk.CTkButton(self.profiles_controls, text="Open Profile Folder", width=140, command=self._open_selected_profile_folder)
        self.open_profile_folder_btn.pack(side="left", padx=(8, 0))

        self.view_storage_root_btn = ctk.CTkButton(self.profiles_controls, text="View Storage Root", width=130, command=self._view_storage_root)
        self.view_storage_root_btn.pack(side="left", padx=(8, 0))

        self.storage_backend = "drive" if self.storage_backend_var.get().lower() == "drive" else "local"
        self.local_backups_root = _localfs_backups_root(self.local_root_entry.get().strip())
        self._update_auto_backup_checkbox_visibility()

        self.profiles_list_scroll = ctk.CTkScrollableFrame(self.profiles_frame, fg_color="transparent", height=250)
        self.profiles_list_scroll.pack(fill="x", expand=False, padx=10, pady=(0, 8))

        self.profile_backups_label = ctk.CTkLabel(self.profiles_frame, text="Backups in selected profile", font=ctk.CTkFont(size=12, weight="bold"))
        self.profile_backups_label.pack(anchor="w", padx=10, pady=(0, 0))

        self.profile_backups_scroll = ctk.CTkScrollableFrame(self.profiles_frame, fg_color="transparent", height=160)
        self.profile_backups_scroll.pack(fill="x", expand=False, padx=10, pady=(0, 8))

        # Hint label shown when some Drive profiles are hidden by the filter
        self.profiles_hint_label = ctk.CTkLabel(self.profiles_frame, text="", text_color="gray", font=ctk.CTkFont(size=10))

        # Managed games
        self.managed_games_label = ctk.CTkLabel(self.profiles_frame, text="Games we manage (in current profile)", font=ctk.CTkFont(size=12, weight="bold"))
        self.managed_games_label.pack(anchor="w", padx=10, pady=(0, 6))

        self.managed_games_scroll = ctk.CTkScrollableFrame(self.profiles_frame, fg_color="transparent")
        self.managed_games_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.process_log_queue()
        self.selected_profile_name = self._load_selected_profile_name()
        self.after(200, self.refresh_profiles_ui)
        self.after(5000, self._auto_backup_poll)

        # Initialize drive availability hint
        self._append_log_text("\n[READY] App started. Backup/Restore enabled when Drive deps are installed.\n")

    def _toggle_autoscroll(self):
        self._log_autoscroll = bool(self.autoscroll_var.get())

    def browse_folder(self):
        selected_dir = filedialog.askdirectory()
        if selected_dir:
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, os.path.normpath(selected_dir))

    def clear_console(self):
        self.console_output.configure(state="normal")
        self.console_output.delete("1.0", "end")
        self.console_output.configure(state="disabled")

    def _format_timestamp(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _queue_log(self, level: str, message: str):
        ts = self._format_timestamp()
        self._log_queue.put((level, f"[{ts}] {message}"))

    def process_log_queue(self):
        try:
            while True:
                level, text = self._log_queue.get_nowait()
                tag = f"lvl_{level}"
                color_map = {
                    "INFO": "#d0d0d0",
                    "SUCCESS": "#44dd55",
                    "WARN": "#ffcc00",
                    "ERROR": "#ff4444",
                }

                # Ensure the tag exists before querying it (prevents: TclError)
                try:
                    if not self.console_output.tag_cget(tag, "foreground"):
                        self.console_output.tag_config(tag, foreground=color_map.get(level, "#d0d0d0"))
                except Exception:
                    self.console_output.tag_config(tag, foreground=color_map.get(level, "#d0d0d0"))

                self.console_output.configure(state="normal")
                self.console_output.insert("end", text, tag)
                if self._log_autoscroll:
                    self.console_output.see("end")
                self.console_output.configure(state="disabled")
        except queue.Empty:
            pass

        self.after(50, self.process_log_queue)

    def _append_log_text(self, text: str):
        self.console_output.configure(state="normal")
        self.console_output.insert("end", text)
        if self._log_autoscroll:
            self.console_output.see("end")
        self.console_output.configure(state="disabled")

    def _clear_results(self):
        for w in getattr(self, "_tree_sections", []):
            try:
                w.destroy()
            except Exception:
                pass
        self._tree_sections = []

    def _show_results_for_paths(self, paths: list[str]):
        self.discovered_paths = list(dict.fromkeys([p for p in (paths or []) if p]))
        self._clear_results()
        for p in self.discovered_paths:
            self._add_result_section(p)
        self._update_auto_backup_checkbox_visibility()

    def _show_empty_results(self):
        self.discovered_paths = []
        self._clear_results()
        self._update_auto_backup_checkbox_visibility()
        self._append_log_text("\n[INFO] No saved locations available for this profile yet.\n")

    def _update_auto_backup_checkbox_visibility(self):
        has_valid_auto_backup_target = bool(self.selected_profile_name and self.discovered_paths)
        if has_valid_auto_backup_target:
            if not getattr(self.auto_backup_checkbox, "_is_packed", False):
                self.auto_backup_checkbox.pack(side="left", padx=(10, 6))
                self.auto_backup_checkbox._is_packed = True
            self.auto_backup_checkbox.configure(state="normal")
        else:
            try:
                if getattr(self.auto_backup_checkbox, "_is_packed", False):
                    self.auto_backup_checkbox.pack_forget()
                    self.auto_backup_checkbox._is_packed = False
            except Exception:
                pass

    def _load_app_setting(self, key: str, fallback: str | None = None) -> str | None:
        try:
            if not os.path.exists(self._settings_path):
                return fallback
            cfg = configparser.ConfigParser()
            cfg.read(self._settings_path, encoding="utf-8")
            value = cfg.get(APP_SETTINGS_SECTION, key, fallback=fallback or "")
            value = (value or "").strip()
            return value or fallback
        except Exception:
            return fallback

    def _save_app_setting(self, key: str, value: str | None):
        try:
            cfg = configparser.ConfigParser()
            if os.path.exists(self._settings_path):
                cfg.read(self._settings_path, encoding="utf-8")
            if not cfg.has_section(APP_SETTINGS_SECTION):
                cfg.add_section(APP_SETTINGS_SECTION)
            cfg.set(APP_SETTINGS_SECTION, key, value or "")
            with open(self._settings_path, "w", encoding="utf-8") as f:
                cfg.write(f)
        except Exception:
            pass

    def _load_selected_profile_name(self) -> str | None:
        return self._load_app_setting(APP_SETTINGS_SELECTED_PROFILE)

    def _save_selected_profile_name(self):
        self._save_app_setting(APP_SETTINGS_SELECTED_PROFILE, self.selected_profile_name or "")

    def _copy_path(self, path: str):
        try:
            self.clipboard_clear()
            self.clipboard_append(path)
        except Exception:
            pass

    def _open_in_explorer(self, path: str):
        try:
            subprocess.Popen(["explorer", path])
        except Exception:
            pass

    def _add_result_section(self, root_path: str):
        section = ctk.CTkFrame(self.results_scroll, fg_color="transparent")
        section.pack(fill="x", pady=6)

        header = ctk.CTkFrame(section, fg_color="transparent")
        header.pack(fill="x")
        header.grid_columnconfigure(1, weight=1)

        expander_state = {"expanded": False}
        children_frame = ctk.CTkFrame(section, fg_color="transparent")
        children_frame.pack(fill="x")
        children_frame.forget()

        def toggle():
            if expander_state["expanded"]:
                children_frame.forget()
                expander_state["expanded"] = False
            else:
                self._populate_children(children_frame, root_path)
                children_frame.pack(fill="x", pady=(4, 0))
                expander_state["expanded"] = True

        exp_btn = ctk.CTkButton(
            header,
            text="▶",
            width=28,
            command=lambda: (toggle(), exp_btn.configure(text="▼" if not expander_state["expanded"] else "▶")),
        )
        exp_btn.grid(row=0, column=0, padx=(8, 4), pady=4)

        root_label = ctk.CTkLabel(header, text=root_path, anchor="w", font=ctk.CTkFont(size=12))
        root_label.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=4)

        # Double-click the path label to quickly restore to this location (with confirmation)
        try:
            root_label.bind("<Double-Button-1>", lambda e, p=root_path: self._on_label_double_click_restore(p))
        except Exception:
            pass

        # Action buttons (fixed small widths so label doesn't push them off-screen)
        restore_btn = ctk.CTkButton(header, text="Restore", width=80, command=lambda p=root_path: self.start_restore(p))
        restore_btn.grid(row=0, column=5, padx=(6, 8))

        backup_btn = ctk.CTkButton(header, text="Backup", width=80, command=lambda p=root_path: self.start_backup(p))
        backup_btn.grid(row=0, column=4, padx=(6, 6))

        open_btn = ctk.CTkButton(header, text="Open", width=64, command=lambda p=root_path: self._open_in_explorer(p))
        open_btn.grid(row=0, column=3, padx=(6, 6))

        copy_btn = ctk.CTkButton(header, text="Copy", width=64, command=lambda p=root_path: self._copy_path(p))
        copy_btn.grid(row=0, column=2, padx=(6, 6))

        self._tree_sections.append(section)

    def _populate_children(self, children_frame, root_path: str, max_items: int = 200):
        for w in getattr(children_frame, "winfo_children", lambda: [])():
            try:
                w.destroy()
            except Exception:
                pass

        try:
            entries = os.listdir(root_path)
        except Exception:
            entries = []

        subfolders = [os.path.join(root_path, e) for e in entries if os.path.isdir(os.path.join(root_path, e))]
        subfolders.sort(key=lambda x: x.lower())
        if len(subfolders) > max_items:
            subfolders = subfolders[:max_items]

        if not subfolders:
            empty = ctk.CTkLabel(children_frame, text="(no subfolders)", anchor="w", font=ctk.CTkFont(size=11), text_color="gray")
            empty.pack(fill="x", padx=30, pady=(4, 6))
            return

        for sp in subfolders:
            row = ctk.CTkFrame(children_frame, fg_color="transparent")
            row.pack(fill="x", padx=(18, 8), pady=2)

            name = os.path.basename(sp)
            lbl = ctk.CTkLabel(row, text=name, anchor="w", font=ctk.CTkFont(size=11))
            lbl.pack(side="left", fill="x", expand=True, padx=(10, 10))

            cbtn = ctk.CTkButton(row, text="Copy", width=52, command=lambda p=sp: self._copy_path(p))
            cbtn.pack(side="right", padx=(0, 6))

            obtn = ctk.CTkButton(row, text="Open", width=52, command=lambda p=sp: self._open_in_explorer(p))
            obtn.pack(side="right", padx=(0, 6))

            backup_btn = ctk.CTkButton(row, text="Backup", width=70, command=lambda p=sp: self.start_backup(p))
            backup_btn.pack(side="right", padx=(0, 6))

            restore_btn = ctk.CTkButton(row, text="Restore", width=70, command=lambda p=sp: self.start_restore(p))
            restore_btn.pack(side="right", padx=(0, 6))

    def _ensure_drive_available_or_log(self) -> bool:
        if not _drive_enabled():
            self._append_log_text("\n[DRIVE] Google Drive backend not available (install google-api-python-client + deps).\n")
            return False
        return True

    def _on_storage_backend_changed(self, value: str):
        v = (value or "").lower()
        if v == "drive":
            self.storage_backend = "drive"
        else:
            self.storage_backend = "local"
        self._save_app_setting(APP_SETTINGS_STORAGE_BACKEND, self.storage_backend)
        # update local root
        self.local_backups_root = _localfs_backups_root(self.local_root_entry.get().strip())
        self.refresh_profiles_ui()

    def _on_auto_backup_toggled(self):
        self._auto_backup_enabled = bool(self.auto_backup_var.get())
        self._save_app_setting(APP_SETTINGS_AUTO_BACKUP, "1" if self._auto_backup_enabled else "0")
        if self._auto_backup_enabled:
            self._reset_auto_backup_state()

    def _choose_local_root(self):
        sel = filedialog.askdirectory()
        if sel:
            self.local_root_entry.delete(0, "end")
            self.local_root_entry.insert(0, os.path.normpath(sel))
            self.local_backups_root = _localfs_backups_root(self.local_root_entry.get().strip())
            self._save_app_setting(APP_SETTINGS_LOCAL_ROOT, self.local_backups_root)
            if self.storage_backend == "local":
                self.refresh_profiles_ui()

    def _reset_auto_backup_state(self):
        self._auto_backup_state = {}
        for path in list(self.discovered_paths):
            if os.path.isdir(path):
                self._auto_backup_state[path] = {
                    "latest_mtime": self._get_directory_latest_mtime(path),
                    "hash": _compute_directory_tree_hash(path) if os.path.isdir(path) else None,
                }

    def _get_directory_latest_mtime(self, root_path: str) -> float:
        latest = 0.0
        for current_root, dirs, files in os.walk(root_path):
            for fn in files:
                try:
                    fp = os.path.join(current_root, fn)
                    mtime = os.path.getmtime(fp)
                    if mtime > latest:
                        latest = mtime
                except Exception:
                    pass
        return latest

    def _auto_backup_poll(self):
        try:
            if self._auto_backup_enabled and self.discovered_paths:
                for path in list(self.discovered_paths):
                    if path in self._auto_backup_in_progress:
                        continue
                    if not os.path.isdir(path):
                        continue
                    latest_mtime = self._get_directory_latest_mtime(path)
                    previous = self._auto_backup_state.get(path)
                    if previous is None:
                        self._auto_backup_state[path] = {"latest_mtime": latest_mtime, "hash": None}
                        continue
                    if latest_mtime <= previous.get("latest_mtime", 0.0):
                        continue
                    current_hash = _compute_directory_tree_hash(path)
                    if current_hash == previous.get("hash"):
                        self._auto_backup_state[path]["latest_mtime"] = latest_mtime
                        continue
                    self._auto_backup_state[path] = {"latest_mtime": latest_mtime, "hash": current_hash}
                    self._auto_backup_in_progress.add(path)

                    def log_worker(message: str):
                        msg = (message or "").strip()
                        upper = msg.upper()
                        level = "INFO"
                        if msg.startswith("[SUCCESS]") or "[SUCCESS]" in msg or "SUCCESS" in upper:
                            level = "SUCCESS"
                        elif msg.startswith("[FAILED]") or "FAILED" in upper or "ERROR" in upper or msg.startswith("[ERROR]"):
                            level = "ERROR"
                        elif "WARN" in upper:
                            level = "WARN"
                        self._queue_log(level, message)

                    profile_name = self.selected_profile_name or self._default_backup_profile_name(path)
                    if profile_name != self.selected_profile_name:
                        self.selected_profile_name = profile_name
                        self._save_selected_profile_name()
                        self.after(0, self.refresh_profiles_ui)

                    threading.Thread(
                        target=self._auto_backup_worker,
                        args=(path, profile_name, self._detected_save_root_name(path), log_worker),
                        daemon=True,
                    ).start()
        except Exception as e:
            self._append_log_text(f"\n[ERROR] Auto backup poll failed: {e}\n")
        finally:
            self.after(self._auto_backup_interval_ms, self._auto_backup_poll)

    def _auto_backup_worker(self, root_path: str, profile_name: str, save_root: str, log_worker):
        try:
            self._backup_to_drive_worker(root_path, profile_name, save_root, log_worker)
        finally:
            try:
                self._auto_backup_in_progress.discard(root_path)
            except Exception:
                pass

    def _toggle_results_visibility(self):
        self.results_visible = not getattr(self, "results_visible", True)
        if self.results_visible:
            self.results_scroll.pack(fill="both", expand=True, padx=10, pady=10)
            self.results_toggle_btn.configure(text="Hide Results")
        else:
            self.results_scroll.forget()
            self.results_toggle_btn.configure(text="Show Results")

    def _open_selected_profile_folder(self):
        if not self.selected_profile_name:
            self._append_log_text("\n[ERROR] No profile selected.\n")
            return

        if self.storage_backend == "local":
            folder = os.path.join(self.local_backups_root, self.selected_profile_name)
            if os.path.exists(folder):
                self._open_in_explorer(folder)
            else:
                self._append_log_text(f"\n[ERROR] Local profile folder not found: {folder}\n")
            return

        if self.storage_backend == "drive":
            if not _drive_enabled():
                self._append_log_text("\n[DRIVE] Drive backend not available.\n")
                return
            try:
                creds = drive_get_credentials(log_callback=None)
                service = drive_get_service(creds, log_callback=None)
                app_folder_id = drive_get_or_create_app_folder(service, log_callback=None)
                profile_folder_id = drive_get_or_create_profile_folder(service, app_folder_id, self.selected_profile_name, log_callback=None)
                url = f"https://drive.google.com/drive/folders/{profile_folder_id}"
                webbrowser.open(url)
                self._append_log_text(f"\n[DRIVE] Opened profile folder in browser: {url}\n")
            except Exception as e:
                self._append_log_text(f"\n[ERROR] Could not open Drive profile folder: {e}\n")
            return

    def _view_storage_root(self):
        if self.storage_backend == "local":
            root = self.local_backups_root
            if os.path.exists(root):
                self._open_in_explorer(root)
            else:
                self._append_log_text(f"\n[ERROR] Local root does not exist: {root}\n")
            return

        if self.storage_backend == "drive":
            if not _drive_enabled():
                self._append_log_text("\n[DRIVE] Drive backend not available.\n")
                return
            try:
                creds = drive_get_credentials(log_callback=None)
                service = drive_get_service(creds, log_callback=None)
                app_folder_id = drive_get_or_create_app_folder(service, log_callback=None)
                url = f"https://drive.google.com/drive/folders/{app_folder_id}"
                webbrowser.open(url)
                self._append_log_text(f"\n[DRIVE] Opened Drive folder in browser: {url}\n")
            except Exception as e:
                self._append_log_text(f"\n[ERROR] Could not open Drive folder: {e}\n")

    def _derive_profile_name_from_path(self, path: str | None) -> str | None:
        candidate = (path or "").strip()
        if not candidate:
            return None

        candidate = os.path.basename(os.path.normpath(candidate))
        cleaned = re.sub(r"[\\/]+", " ", candidate).strip(" ._-")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        if cleaned:
            cleaned = re.sub(r"(?i)\s*[-_.]?(v\d+(?:\.\d+)*|build|update|patch|p2p|repack|cracked|steam|epic|gog|demo)(?:[-_.]?\w+)?$", "", cleaned).strip()
            cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._-")

        return cleaned or None

    def _default_backup_profile_name(self, root_path: str | None = None) -> str:
        for candidate in [self.path_entry.get().strip(), root_path]:
            name = self._derive_profile_name_from_path(candidate)
            if name:
                return name
        return self._detected_save_root_name(root_path) if root_path else "Default"

    def _detected_save_root_name(self, root_path: str) -> str:
        return os.path.basename(os.path.normpath(root_path))

    def start_backup(self, root_path: str):
        if not root_path or not os.path.isdir(root_path):
            self._append_log_text("\n[ERROR] Backup target is not a valid directory.\n")
            return
        if self.storage_backend == "drive" and not self._ensure_drive_available_or_log():
            return

        save_root = self._detected_save_root_name(root_path)
        default_profile = self._default_backup_profile_name(root_path)

        # Immediate UX feedback (main thread) before long work
        self._append_log_text(f"\n[STORAGE] Backup clicked for: {root_path} (backend={self.storage_backend})\n")

        profile_name = simpledialog.askstring("Backup Profile", f"Profile name for '{save_root}':", initialvalue=default_profile)
        if not profile_name or not str(profile_name).strip():
            self._append_log_text("[DRIVE] Backup cancelled (no profile name provided).\n")
            return

        profile_name = str(profile_name).strip()
        self.selected_profile_name = profile_name
        self._save_selected_profile_name()
        self.refresh_profiles_ui()  # best-effort to keep UI in sync

        def _log_worker(message: str):
            msg = (message or "").strip()
            upper = msg.upper()
            level = "INFO"
            if msg.startswith("[SUCCESS]") or "[SUCCESS]" in msg or "SUCCESS" in upper:
                level = "SUCCESS"
            elif msg.startswith("[FAILED]") or "FAILED" in upper or "ERROR" in upper or msg.startswith("[ERROR]"):
                level = "ERROR"
            elif "WARN" in upper:
                level = "WARN"
            self._queue_log(level, message)

        threading.Thread(
            target=self._backup_to_drive_worker,
            args=(root_path, profile_name, save_root, _log_worker),
            daemon=True,
        ).start()

    def _backup_to_drive_worker(self, root_path: str, profile_name: str, save_root: str, log_worker):
        try:
            log_worker(f"[STORAGE] Backup started... (backend={self.storage_backend})\n")

            if self.storage_backend == "drive":
                creds = drive_get_credentials(log_callback=log_worker)
                service = drive_get_service(creds, log_callback=log_worker)
                app_folder_id = drive_get_or_create_app_folder(service, log_callback=log_worker)
                profile_folder_id = drive_get_or_create_profile_folder(service, app_folder_id, profile_name, log_callback=log_worker)
            else:
                # Local filesystem backend
                profile_folder_id = localfs_get_or_create_profile_folder(self.local_backups_root, profile_name, log_callback=log_worker)

            # Create zip in temp
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            content_hash = _compute_directory_tree_hash(root_path)
            sha_manifest = {
                "timestamp": timestamp,
                "game_root": save_root,
                "original_save_path": root_path,
                "restore_target_hint": "original_save_path",
                "content_hash": content_hash,
            }

            with tempfile.TemporaryDirectory(prefix="savefinder_backup_") as tmp:
                zip_path = os.path.join(tmp, "backup.zip")
                _create_zip_with_manifest(zip_path, manifest=sha_manifest, folder_to_backup=root_path, log_callback=log_worker)

                sha12 = content_hash[:ZIP_SHA256_PREFIX_LEN]

                # Stable content-based dedupe
                if self.storage_backend == "drive":
                    if drive_has_sha_dedupe_match(service, profile_folder_id, computed_sha12=sha12, log_callback=log_worker):
                        log_worker(f"[SUCCESS] Backup skipped (save contents unchanged; SHA suffix: {sha12}).\n")
                        return
                    file_id = drive_upload_backup_zip(
                        service,
                        profile_folder_id=profile_folder_id,
                        zip_path=zip_path,
                        manifest=sha_manifest,
                        sha256_hex=content_hash,
                        log_callback=log_worker,
                    )
                    drive_cleanup_old_backups(service, profile_folder_id, save_root, keep_file_id=file_id, log_callback=log_worker)
                    log_worker(f"[SUCCESS] Backup uploaded to Drive (file id={file_id}).\n")
                else:
                    if localfs_has_sha_dedupe_match(profile_folder_id, computed_sha12=sha12, log_callback=log_worker):
                        log_worker(f"[SUCCESS] Backup skipped (save contents unchanged; SHA suffix: {sha12}).\n")
                        return
                    file_id = localfs_upload_backup_zip(
                        profile_folder_id,
                        zip_path=zip_path,
                        manifest=sha_manifest,
                        sha256_hex=content_hash,
                        log_callback=log_worker,
                    )
                    localfs_cleanup_old_backups(profile_folder_id, save_root, keep_path=file_id, log_callback=log_worker)
                    log_worker(f"[SUCCESS] Backup saved to local folder (path={file_id}).\n")
        except Exception as e:
            log_worker(f"[ERROR] Backup failed: {e}\n")

    def start_restore(self, root_path: str):
        if not root_path or not os.path.isdir(root_path):
            self._append_log_text("\n[ERROR] Restore target is not a valid directory.\n")
            return
        if self.storage_backend == "drive" and not self._ensure_drive_available_or_log():
            return

        save_root = self._detected_save_root_name(root_path)

        # Immediate UX feedback
        self._append_log_text(f"\n[DRIVE] Restore clicked for: {root_path}\n")

        def _log_worker(message: str):
            msg = (message or "").strip()
            upper = msg.upper()
            level = "INFO"
            if msg.startswith("[SUCCESS]") or "[SUCCESS]" in msg or "SUCCESS" in upper:
                level = "SUCCESS"
            elif msg.startswith("[FAILED]") or "FAILED" in upper or "ERROR" in upper or msg.startswith("[ERROR]"):
                level = "ERROR"
            elif "WARN" in upper:
                level = "WARN"
            self._queue_log(level, message)

        threading.Thread(
            target=self._restore_from_drive_worker,
            args=(root_path, save_root, _log_worker),
            daemon=True,
        ).start()

    def _choose_default_restore_target_dir(self, save_root: str) -> str | None:
        """Required behavior: default to first currently detected location with that save root."""
        for p in self.discovered_paths:
            if self._detected_save_root_name(p) == save_root:
                return p
        # fallback: current root_path passed to restore worker
        return None

    def _restore_from_drive_worker(self, root_path: str, save_root: str, log_worker):
        try:
            log_worker(f"[STORAGE] Restore started... (backend={self.storage_backend})\n")
            service = None
            app_folder_id = None
            if self.storage_backend == "drive":
                creds = drive_get_credentials(log_callback=log_worker)
                service = drive_get_service(creds, log_callback=log_worker)
                app_folder_id = drive_get_or_create_app_folder(service, log_callback=log_worker)

            # Default profile behavior:
            profile_name = self.selected_profile_name or save_root
            # If selected profile doesn't contain backups, we will still try to find ones across profiles.

            def _try_profile(profile_to_try: str):
                if self.storage_backend == "drive":
                    prof_id = drive_get_or_create_profile_folder(service, app_folder_id, profile_to_try, log_callback=log_worker)
                    backups = drive_list_profile_backups(service, prof_id, save_root=save_root, log_callback=log_worker, limit=200)
                else:
                    prof_id = localfs_get_or_create_profile_folder(self.local_backups_root, profile_to_try, log_callback=log_worker)
                    backups = localfs_list_profile_backups(prof_id, save_root=save_root, log_callback=log_worker, limit=200)
                newest = _pick_newest_by_modifiedTime(backups)
                return prof_id, backups, newest

            target_dir = self._choose_default_restore_target_dir(save_root) or root_path

            # First try the chosen profile
            _, backups, chosen_newest = _try_profile(profile_name)

            if not chosen_newest:
                log_worker(f"[STORAGE] No backups found in profile '{profile_name}' for '{save_root}'. Searching profiles...\n")
                if self.storage_backend == "drive":
                    profiles = drive_list_profiles(service, app_folder_id, log_callback=log_worker)
                else:
                    profiles = localfs_list_profiles(self.local_backups_root, log_callback=log_worker)
                profile_hits = []
                for pr in profiles:
                    prof_id = pr["id"]
                    pr_name = pr["name"]
                    if self.storage_backend == "drive":
                        bl = drive_list_profile_backups(service, prof_id, save_root=save_root, log_callback=log_worker, limit=200)
                    else:
                        bl = localfs_list_profile_backups(prof_id, save_root=save_root, log_callback=log_worker, limit=200)
                    newest = _pick_newest_by_modifiedTime(bl)
                    if newest:
                        profile_hits.append((pr_name, newest, bl))

                if not profile_hits:
                    log_worker(f"[ERROR] No backups found on Drive for '{save_root}'.\n")
                    return

                # pick newest among all profiles by modifiedTime
                profile_hits.sort(key=lambda t: t[1].get("modifiedTime", ""), reverse=True)
                profile_name, chosen_newest, _ = profile_hits[0]
                self.selected_profile_name = profile_name
                self._save_selected_profile_name()
                log_worker(f"[DRIVE] Using profile '{profile_name}' (newest backup across profiles).\n")

            chosen_file_id = chosen_newest["id"]
            log_worker(f"[STORAGE] Restoring newest backup: {chosen_newest.get('name','')}\n")

            if self.storage_backend == "drive":
                result = drive_restore_backup_zip(service, file_id=chosen_file_id, target_dir=target_dir, log_callback=log_worker)
            else:
                result = localfs_restore_backup_zip(chosen_file_id, target_dir, log_callback=log_worker)
            stats = result.get("stats", {})
            log_worker(
                f"[SUCCESS] Restore complete. copied={stats.get('copied')}, skipped={stats.get('skipped')}, total={stats.get('total')}.\n"
            )

            # Refresh managed games for UI
            self.refresh_profiles_ui()
        except Exception as e:
            log_worker(f"[ERROR] Restore failed: {e}\n")

    def start_scan(self):
        target_dir = self.path_entry.get().strip()
        if not target_dir:
            self.clear_console()
            self._append_log_text("[ERROR] Please provide a valid game directory path first.\n")
            return

        self.clear_console()
        self.scan_btn.configure(state="disabled", text="Searching...")

        def _log_worker(message: str):
            msg = (message or "").strip()
            upper = msg.upper()

            level = "INFO"
            if msg.startswith("[SUCCESS]") or "[SUCCESS]" in msg or "SUCCESS" in upper:
                level = "SUCCESS"
            elif msg.startswith("[FAILED]") or "FAILED" in upper or "ERROR" in upper or msg.startswith("[ERROR]"):
                level = "ERROR"
            elif "WARN" in upper:
                level = "WARN"

            self._queue_log(level, message)

        threading.Thread(
            target=run_save_finder,
            args=(target_dir, _log_worker, self.on_scan_complete),
            daemon=True,
        ).start()

    def on_scan_complete(self, paths):
        def _apply_ui():
            self.discovered_paths = paths or []
            self.scan_btn.configure(state="normal", text="Scan for Saves")

            self._clear_results()
            for p in self.discovered_paths:
                self._add_result_section(p)

            if self.discovered_paths:
                self._append_log_text("\n[READY] Scan finished. Save locations verified and locked.\n")
            else:
                self._append_log_text("\n[FINISHED] Scan completed with no locations found.\n")

            # If we found new save locations, create/select a sensible default profile
            # derived from the first detected path. Always ensure the selected profile
            # folder exists in the current storage backend.
            if self.discovered_paths:
                try:
                    candidate = self._default_backup_profile_name(self.discovered_paths[0])
                    if candidate:
                        if candidate != (self.selected_profile_name or ""):
                            self.selected_profile_name = candidate
                            self._save_selected_profile_name()
                            try:
                                self._append_log_text(f"\n[INFO] Auto-selected profile: {candidate}\n")
                            except Exception:
                                pass

                        profile_to_ensure = self.selected_profile_name or candidate

                        def _ensure_profile_remote():
                            try:
                                if self.storage_backend == "drive" and _drive_enabled():
                                    creds = drive_get_credentials(log_callback=None)
                                    service = drive_get_service(creds, log_callback=None)
                                    app_folder_id = drive_get_or_create_app_folder(service, log_callback=None)
                                    drive_get_or_create_profile_folder(service, app_folder_id, profile_to_ensure, log_callback=None)
                                else:
                                    localfs_get_or_create_profile_folder(self.local_backups_root, profile_to_ensure, log_callback=None)
                                self.after(0, lambda: self.refresh_profiles_ui(prefer_local_results=True))
                            except Exception as e:
                                self.after(0, lambda: self._append_log_text(f"\n[ERROR] Auto-create profile failed: {e}\n"))

                        threading.Thread(target=_ensure_profile_remote, daemon=True).start()
                except Exception:
                    pass

            # Profiles panel refresh best-effort
            # Refresh profiles but prefer showing freshly scanned local results
            self.refresh_profiles_ui(prefer_local_results=True)

        self.after(0, _apply_ui)

    # --- Profiles UI ---

    def refresh_profiles_ui(self, prefer_local_results: bool = False):
        if self._profiles_refreshing:
            return
        if self.storage_backend == "drive" and not _drive_enabled():
            # Show placeholder
            self._clear_profiles_widgets()
            self.profiles_list_scroll._scrollbar.configure(height=0) if hasattr(self.profiles_list_scroll, "_scrollbar") else None
            self._append_log_text("\n[DRIVE] Profiles unavailable: Drive backend not enabled.\n")
            return

        self._profiles_refreshing = True

        def _worker():
            try:
                service = None
                app_folder_id = None
                if self.storage_backend == "drive":
                    creds = drive_get_credentials(log_callback=None)
                    service = drive_get_service(creds, log_callback=None)
                    app_folder_id = drive_get_or_create_app_folder(service, log_callback=None)
                    profiles = drive_list_profiles(service, app_folder_id, log_callback=None)
                else:
                    profiles = localfs_list_profiles(self.local_backups_root, log_callback=None)

                # Count backups per profile (best-effort: only zips)
                profile_info = []
                for pr in profiles:
                    # list a bit (limit) to compute count best-effort by listing; may be inaccurate without count query
                    if self.storage_backend == "drive":
                        bks = drive_list_profile_backups(service, pr["id"], save_root=None, log_callback=None, limit=200)
                    else:
                        bks = localfs_list_profile_backups(pr["id"], save_root=None, log_callback=None, limit=200)
                    profile_info.append({"name": pr["name"], "id": pr["id"], "count": len(bks)})

                # Determine managed games from detected save roots that match backup filenames in selected/default profile
                selected = self.selected_profile_name
                if not selected and profile_info:
                    # Prefer a profile that actually contains backups; avoid auto-selecting empty profiles
                    non_empty = next((p for p in profile_info if p.get("count", 0) > 0), None)
                    if non_empty:
                        selected = non_empty["name"]
                        self.selected_profile_name = selected
                        self._save_selected_profile_name()
                    else:
                        # Do not auto-select a profile when none contain backups
                        selected = None

                managed = {}
                profile_result_paths = []
                selected_profile_backups = []
                if selected:
                    sel = next((x for x in profile_info if x["name"] == selected), None)
                    if sel:
                        if self.storage_backend == "drive":
                            backups = drive_list_profile_backups(service, sel["id"], save_root=None, log_callback=None, limit=200)
                        else:
                            backups = localfs_list_profile_backups(sel["id"], save_root=None, log_callback=None, limit=200)
                        selected_profile_backups = backups or []
                        for b in backups:
                            parsed = _parse_zip_name_for_fields(b.get("name", ""))
                            sr = parsed.get("save_root")
                            if not sr:
                                continue
                            managed.setdefault(sr, []).append(b)

                        # Build result list from the newest backup per save_root.
                        for sr, files in managed.items():
                            newest = _pick_newest_by_modifiedTime(files)
                            if newest:
                                resolved_path = self._read_backup_manifest_path(service, newest.get("id"), fallback_save_root=sr)
                                if resolved_path:
                                    profile_result_paths.append(resolved_path)

                # Reduce to newest per game
                managed_games = []
                for sr, files in managed.items():
                    newest = _pick_newest_by_modifiedTime(files)
                    managed_games.append({"save_root": sr, "newest": newest, "count": len(files)})

                # Sort for UI
                managed_games.sort(key=lambda x: x["save_root"].lower())
                profile_result_paths = list(dict.fromkeys(profile_result_paths))

                # When prefer_local_results is True, avoid overwriting freshly scanned local results
                def _schedule_render():
                    to_pass = profile_result_paths
                    if prefer_local_results and not profile_result_paths:
                        to_pass = None
                    self._render_profiles_ui(profile_info, managed_games, to_pass, selected_profile_backups)

                # Schedule UI update
                self.after(0, _schedule_render)

            except Exception as e:
                self.after(0, lambda: self._append_log_text(f"\n[ERROR] Profiles refresh failed: {e}\n"))
                self.after(0, lambda: self._profiles_refreshing.__setattr__("__bool__", False))
            finally:
                self._profiles_refreshing = False

        threading.Thread(target=_worker, daemon=True).start()

    def _clear_profiles_widgets(self):
        for w in getattr(self, "_profile_rows", []):
            try:
                w.destroy()
            except Exception:
                pass
        self._profile_rows = []

        # Clear profiles list scroll children (prevents duplicated '(no profiles found)' labels)
        for w in getattr(self.profiles_list_scroll, "winfo_children", lambda: [])():
            try:
                w.destroy()
            except Exception:
                pass

        for w in getattr(self.profile_backups_scroll, "winfo_children", lambda: [])():
            try:
                w.destroy()
            except Exception:
                pass

        for w in getattr(self.managed_games_scroll, "winfo_children", lambda: [])():
            try:
                w.destroy()
            except Exception:
                pass

    def _read_backup_manifest_path(self, service, backup_file_id: str, fallback_save_root: str | None = None) -> str | None:
        try:
            import tempfile
            import zipfile
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                # If backup_file_id is a local path, copy it directly; otherwise use Drive download helper
                if backup_file_id and os.path.exists(str(backup_file_id)):
                    shutil.copy2(str(backup_file_id), tmp_path)
                else:
                    drive_download_file(service, file_id=backup_file_id, dest_path=tmp_path, log_callback=None)

                with zipfile.ZipFile(tmp_path, "r") as zf:
                    manifest_bytes = zf.read(GOOGLE_DRIVE_MANIFEST_NAME)
                    manifest = json.loads(manifest_bytes.decode("utf-8"))
                original_path = manifest.get("original_save_path")
                if original_path:
                    return str(original_path)
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
        except Exception:
            pass

        return fallback_save_root

    def _render_profiles_ui(
        self,
        profile_info: list[dict],
        managed_games: list[dict],
        profile_result_paths: list[str] | None = None,
        selected_profile_backups: list[dict] | None = None,
    ):
        self._clear_profiles_widgets()

        # Profiles list - show all profiles (including empty). Only show placeholder when no profiles exist at all.
        visible_profiles = profile_info or []

        if not visible_profiles:
            label = ctk.CTkLabel(self.profiles_list_scroll, text="(no profiles found)", text_color="gray")
            label.pack(anchor="w", padx=10, pady=6)
        else:
            for pr in visible_profiles:
                name = pr["name"]
                count = pr.get("count", 0)

                row = ctk.CTkFrame(self.profiles_list_scroll, fg_color="transparent")
                row.pack(fill="x", padx=5, pady=4)

                btn = ctk.CTkButton(
                    row,
                    text=("✓ " if name == self.selected_profile_name else "") + f"{name} ({count})",
                    anchor="w",
                    command=lambda n=name: self._on_profile_selected(n),
                )
                btn.pack(fill="x", padx=5)
                self._profile_rows.append(row)

        # No filtering applied now; hide the hint label
        try:
            try:
                self.profiles_hint_label.pack_forget()
                self.profiles_hint_label._is_packed = False
            except Exception:
                pass
        except Exception:
            pass

        if profile_result_paths is not None:
            if profile_result_paths:
                self._show_results_for_paths(profile_result_paths)
            else:
                if not self.discovered_paths:
                    self._show_empty_results()

        self._update_auto_backup_checkbox_visibility()

        # Selected profile backups list
        if not selected_profile_backups:
            label = ctk.CTkLabel(self.profile_backups_scroll, text="(no backups in selected profile)", text_color="gray")
            label.pack(anchor="w", padx=10, pady=6)
        else:
            for backup in selected_profile_backups:
                name = backup.get("name", "<unknown>")
                row = ctk.CTkFrame(self.profile_backups_scroll, fg_color="transparent")
                row.pack(fill="x", padx=5, pady=4)

                lbl = ctk.CTkLabel(row, text=name, anchor="w", font=ctk.CTkFont(size=11))
                lbl.pack(side="left", fill="x", expand=True, padx=(8, 6))

                restore_btn = ctk.CTkButton(
                    row,
                    text="Restore",
                    width=80,
                    command=lambda b=backup: self._restore_backup_to_folder(b),
                )
                restore_btn.pack(side="right", padx=(0, 6))

                download_btn = ctk.CTkButton(
                    row,
                    text="Download",
                    width=80,
                    command=lambda b=backup: self._download_backup_file(b),
                )
                download_btn.pack(side="right", padx=(0, 6))

                delete_btn = ctk.CTkButton(
                    row,
                    text="Delete",
                    width=80,
                    command=lambda b=backup: self._delete_backup_file(b),
                )
                delete_btn.pack(side="right", padx=(0, 6))

                if self.storage_backend == "local":
                    open_btn = ctk.CTkButton(
                        row,
                        text="Open",
                        width=70,
                        command=lambda b=backup: self._open_in_explorer(os.path.dirname(str(b.get("id", "")))),
                    )
                    open_btn.pack(side="right", padx=(0, 6))

        # Managed games list
        if not managed_games:
            label = ctk.CTkLabel(self.managed_games_scroll, text="(no managed games yet)", text_color="gray")
            label.pack(anchor="w", padx=10, pady=6)
            return

        for mg in managed_games:
            sr = mg["save_root"]
            newest = mg.get("newest")
            file_name = newest.get("name") if newest else "-"

            row = ctk.CTkFrame(self.managed_games_scroll, fg_color="transparent")
            row.pack(fill="x", padx=5, pady=4)

            lbl = ctk.CTkLabel(row, text=sr, anchor="w", font=ctk.CTkFont(size=11, weight="bold"))
            lbl.pack(side="left", fill="x", expand=True, padx=(8, 6))

            if newest:
                backup_name = newest.get("name") or "-"
                detail_lbl = ctk.CTkLabel(row, text=backup_name, anchor="e", font=ctk.CTkFont(size=10), text_color="gray")
                detail_lbl.pack(side="right", padx=(0, 8))

            restore_btn = ctk.CTkButton(
                row,
                text="Restore",
                width=90,
                command=lambda game_root=sr: self.restore_game_root_from_profile(game_root),
            )
            restore_btn.pack(side="right", padx=(0, 6))

            if newest:
                self._append_log_text(f"\n[DRIVE] Managed game: {sr} newest backup: {file_name}\n")

    def _on_profile_selected(self, profile_name: str):
        self.selected_profile_name = profile_name
        self._save_selected_profile_name()
        self.refresh_profiles_ui()

    def _download_backup_file(self, backup_entry: dict):
        if not backup_entry:
            return

        name = backup_entry.get("name", "backup.zip")
        dest = filedialog.asksaveasfilename(defaultextension=".zip", initialfile=name, filetypes=[("ZIP archive", "*.zip")])
        if not dest:
            return

        def _worker():
            try:
                if self.storage_backend == "drive":
                    if not self._ensure_drive_available_or_log():
                        return
                    creds = drive_get_credentials(log_callback=None)
                    service = drive_get_service(creds, log_callback=None)
                    drive_download_file(service, file_id=backup_entry.get("id"), dest_path=dest, log_callback=None)
                else:
                    localfs_download_file(str(backup_entry.get("id", "")), dest_path=dest, log_callback=None)
                self._queue_log("SUCCESS", f"[STORAGE] Backup downloaded to {dest}\n")
            except Exception as e:
                self._queue_log("ERROR", f"[STORAGE] Download failed: {e}\n")

        threading.Thread(target=_worker, daemon=True).start()

    def _restore_backup_to_folder(self, backup_entry: dict):
        if not backup_entry:
            return

        target_dir = filedialog.askdirectory(title="Choose restore target folder")
        if not target_dir:
            self._append_log_text("\n[INFO] Restore cancelled. No folder selected.\n")
            return

        if not os.path.exists(target_dir):
            _safe_makedirs(target_dir)

        def _worker():
            try:
                if self.storage_backend == "drive":
                    if not self._ensure_drive_available_or_log():
                        return
                    creds = drive_get_credentials(log_callback=None)
                    service = drive_get_service(creds, log_callback=None)
                    result = drive_restore_backup_zip(service, file_id=backup_entry.get("id"), target_dir=target_dir, log_callback=None)
                else:
                    result = localfs_restore_backup_zip(str(backup_entry.get("id", "")), target_dir=target_dir, log_callback=None)

                stats = result.get("stats", {})
                self._queue_log("SUCCESS", f"[STORAGE] Restore complete. copied={stats.get('copied')}, skipped={stats.get('skipped')}, total={stats.get('total')}\n")
            except Exception as e:
                self._queue_log("ERROR", f"[STORAGE] Restore failed: {e}\n")

        threading.Thread(target=_worker, daemon=True).start()

    def _delete_backup_file(self, backup_entry: dict):
        if not backup_entry:
            return

        name = backup_entry.get("name", "backup")
        confirm = messagebox.askyesno("Confirm Delete", f"Delete backup '{name}' from the selected profile? This cannot be undone.")
        if not confirm:
            return

        def _worker():
            try:
                if self.storage_backend == "drive":
                    if not self._ensure_drive_available_or_log():
                        return
                    creds = drive_get_credentials(log_callback=None)
                    service = drive_get_service(creds, log_callback=None)
                    service.files().delete(fileId=backup_entry.get("id")).execute()
                else:
                    path = str(backup_entry.get("id", ""))
                    if os.path.exists(path):
                        os.remove(path)
                self._queue_log("SUCCESS", f"[STORAGE] Backup deleted: {name}\n")
                self.after(0, self.refresh_profiles_ui)
            except Exception as e:
                self._queue_log("ERROR", f"[STORAGE] Delete failed: {e}\n")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_label_double_click_restore(self, path: str):
        """Confirm and start restore for a detected save location when the label is double-clicked."""
        if not path:
            return
        try:
            resp = messagebox.askyesno(
                "Confirm Restore",
                f"Restore saves into '{path}'?\nThis will copy files from Drive into this folder. Continue?",
            )
            if resp:
                self.start_restore(path)
        except Exception:
            pass

    def restore_game_root_from_profile(self, game_root: str):
        if not game_root:
            self._append_log_text("\n[ERROR] Missing game root for restore.\n")
            return

        matched_path = None
        for p in self.discovered_paths:
            if self._detected_save_root_name(p) == game_root:
                matched_path = p
                break

        target_path = matched_path or game_root
        if os.path.isdir(target_path):
            self.start_restore(target_path)
            return

        self._append_log_text(f"\n[WARN] No local directory available for '{game_root}'. Please choose a target folder to restore into.\n")
        chosen_dir = filedialog.askdirectory(title=f"Choose restore folder for {game_root}")
        if chosen_dir:
            self.start_restore(chosen_dir)
        else:
            self._append_log_text("\n[INFO] Restore cancelled. No target directory selected.\n")

    def add_profile_ui(self):
        default_name = self._default_backup_profile_name(self.path_entry.get().strip() or None)
        name = simpledialog.askstring("Add Profile", "Profile name:", initialvalue=default_name)
        if not name or not str(name).strip():
            return
        self.selected_profile_name = str(name).strip()
        self._save_selected_profile_name()

        if not self._ensure_drive_available_or_log():
            return

        def _log_worker(message: str):
            msg = (message or "").strip()
            upper = msg.upper()
            level = "INFO"
            if msg.startswith("[SUCCESS]") or "[SUCCESS]" in msg or "SUCCESS" in upper:
                level = "SUCCESS"
            elif msg.startswith("[FAILED]") or "FAILED" in upper or "ERROR" in upper or msg.startswith("[ERROR]"):
                level = "ERROR"
            elif "WARN" in upper:
                level = "WARN"
            self._queue_log(level, message)

        def _worker():
            try:
                if self.storage_backend == "drive":
                    creds = drive_get_credentials(log_callback=_log_worker)
                    service = drive_get_service(creds, log_callback=_log_worker)
                    app_folder_id = drive_get_or_create_app_folder(service, log_callback=_log_worker)
                    drive_get_or_create_profile_folder(service, app_folder_id, self.selected_profile_name, log_callback=_log_worker)
                else:
                    localfs_get_or_create_profile_folder(self.local_backups_root, self.selected_profile_name, log_callback=_log_worker)
                self.after(0, self.refresh_profiles_ui)
            except Exception as e:
                _log_worker(f"[ERROR] Add profile failed: {e}\n")

        threading.Thread(target=_worker, daemon=True).start()


if __name__ == "__main__":
    app = SaveFinderApp()
    app.mainloop()

