import json
import os
import shutil
import tempfile

from .hashing import (
    ZIP_SHA256_PREFIX_LEN,
    DRIVE_ZIP_NAME_DELIM,
    drive_safe_filename_fragment,
)
from .zip_manifest import (
    create_zip_with_manifest,
    extract_zip_contents,
    copy_contents_into_target,
    restore_zip_to_target,
)


def _safe_makedirs(p: str):
    os.makedirs(p, exist_ok=True)


def localfs_backups_root(default_root: str | None = None, script_dir: str | None = None) -> str:
    if default_root:
        root = default_root
    else:
        if script_dir is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        root = os.path.join(script_dir, "..", "backups")
        root = os.path.abspath(root)
    _safe_makedirs(root)
    return root


def list_profiles(backups_root: str, log_callback=None):
    _safe_makedirs(backups_root)
    items = []
    try:
        for name in os.listdir(backups_root):
            p = os.path.join(backups_root, name)
            if os.path.isdir(p):
                items.append({"id": p, "name": name})
    except Exception as e:
        if log_callback:
            log_callback(f"[LOCAL] Failed to list profiles in {backups_root}: {e}\n")
    return items


def get_or_create_profile_folder(backups_root: str, profile_name: str, log_callback=None) -> str:
    if not profile_name or not str(profile_name).strip():
        profile_name = "Default"
    profile_name = str(profile_name).strip()
    dest = os.path.join(backups_root, profile_name)
    _safe_makedirs(dest)
    if log_callback:
        log_callback(f"[LOCAL] Using profile folder: {dest}\n")
    return dest


def list_profile_backups(profile_folder_path: str, save_root: str | None = None, log_callback=None, limit: int = 200):
    out = []
    try:
        for fn in os.listdir(profile_folder_path):
            if not fn.lower().endswith(".zip"):
                continue
            if save_root:
                safe_root = drive_safe_filename_fragment(save_root)
                if f"{safe_root}_" not in fn:
                    continue
            fp = os.path.join(profile_folder_path, fn)
            mtime = os.path.getmtime(fp)
            from datetime import datetime

            out.append(
                {
                    "id": fp,
                    "name": fn,
                    "modifiedTime": datetime.utcfromtimestamp(mtime).isoformat() + "Z",
                }
            )
    except Exception as e:
        if log_callback:
            log_callback(f"[LOCAL] Failed to list backups in {profile_folder_path}: {e}\n")

    out.sort(key=lambda x: x.get("modifiedTime", ""), reverse=True)
    return out[:limit]


def has_sha_dedupe_match(profile_folder_path: str, computed_sha12: str) -> bool:
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


def upload_backup_zip(profile_folder_path: str, zip_path: str, manifest: dict, sha256_hex: str, log_callback=None) -> str:
    if not sha256_hex:
        raise RuntimeError("Missing sha256 for dedupe/naming")

    timestamp = manifest.get("timestamp", "unknown")
    game_root = manifest.get("game_root", "")
    save_root = drive_safe_filename_fragment(game_root)
    sha12 = sha256_hex[:ZIP_SHA256_PREFIX_LEN]
    filename = f"{save_root}_{timestamp}{DRIVE_ZIP_NAME_DELIM}{sha12}.zip"
    dest = os.path.join(profile_folder_path, filename)
    _safe_makedirs(os.path.dirname(dest) or ".")
    shutil.copy2(zip_path, dest)
    if log_callback:
        log_callback(f"[LOCAL] Saved backup: {dest}\n")
    return dest


def cleanup_old_backups(profile_folder_path: str, save_root: str, keep_path: str | None = None, log_callback=None):
    if not save_root:
        return
    try:
        # Cleanup needs to see every backup for this save_root, not just the
        # UI-display-sized page, or backups beyond the limit could never be
        # cleaned up.
        backups = list_profile_backups(profile_folder_path, save_root=save_root, log_callback=log_callback, limit=2000)
        to_remove = [b for b in backups if str(b.get("id", "")) and str(b.get("id", "")) != (keep_path or "")]
        if log_callback:
            log_callback(
                f"[LOCAL] Cleanup: {len(backups)} backup(s) found for '{save_root}', "
                f"removing {len(to_remove)}, keeping {keep_path}\n"
            )
        for backup in to_remove:
            path = str(backup.get("id", ""))
            try:
                os.remove(path)
                if log_callback:
                    log_callback(f"[LOCAL] Removed old backup: {path}\n")
            except Exception as e:
                if log_callback:
                    log_callback(f"[WARN] Could not delete old backup {path}: {e}\n")
    except Exception as e:
        if log_callback:
            log_callback(f"[WARN] Backup cleanup failed for '{save_root}': {e}\n")


def download_file(file_path: str, dest_path: str, log_callback=None):
    if log_callback:
        log_callback(f"[LOCAL] Copying file {file_path} -> {dest_path}\n")
    _safe_makedirs(os.path.dirname(dest_path) or ".")
    shutil.copy2(file_path, dest_path)


def restore_backup_zip(file_path: str, target_dir: str, log_callback=None) -> dict:
    # Backwards-compat wrapper over zip_manifest helpers
    return restore_zip_to_target(file_path, target_dir, log_callback=log_callback)


def list_profiles_storage_root(default_root: str | None = None):
    """Backward-compatible helper if needed by GUI refactor."""
    return localfs_backups_root(default_root)


