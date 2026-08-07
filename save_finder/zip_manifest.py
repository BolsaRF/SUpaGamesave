import json
import os
import shutil
import tempfile
import zipfile

GOOGLE_DRIVE_MANIFEST_NAME = "manifest.json"


def safe_makedirs(p: str):
    os.makedirs(p, exist_ok=True)


def normalize_save_path_for_current_user(path: str) -> str:
    """Map an original Windows user profile path to the current user's equivalent.

    Example: C:\\Users\\fabio\\Saved Games -> C:\\Users\\Legion5\\Saved Games
    """
    if not path:
        return path

    path = os.path.abspath(os.path.expandvars(path))
    if os.path.exists(path):
        return path

    drive, tail = os.path.splitdrive(path)
    if not tail:
        return path

    parts = [p for p in tail.strip("\\/").split(os.sep) if p]
    if len(parts) < 3 or parts[0].lower() != "users":
        return path

    current_home = os.path.abspath(os.path.expanduser("~"))
    current_drive, current_tail = os.path.splitdrive(current_home)
    current_parts = [p for p in current_tail.strip("\\/").split(os.sep) if p]
    if len(current_parts) < 2 or current_parts[0].lower() != "users":
        return path

    remainder = parts[2:]
    current_user = current_parts[1]
    mapped_parts = ["Users", current_user] + remainder
    mapped_drive = current_drive or drive
    mapped_path = os.path.join(mapped_drive + os.sep if mapped_drive else "", *mapped_parts)
    return os.path.normpath(mapped_path)


def create_zip_with_manifest(zip_path: str, manifest: dict, folder_to_backup: str, log_callback=None):
    """ZIP option B: zip contains manifest.json + contents/* (folder contents only)."""
    if log_callback:
        log_callback(f"[ZIP] Creating zip: {zip_path}\n")

    safe_makedirs(os.path.dirname(zip_path) or ".")
    folder_to_backup = os.path.abspath(folder_to_backup)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            GOOGLE_DRIVE_MANIFEST_NAME,
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )

        for root, dirs, files in os.walk(folder_to_backup):
            rel_root = os.path.relpath(root, folder_to_backup)
            for fn in files:
                abs_fp = os.path.join(root, fn)
                rel_fp = os.path.join(rel_root, fn) if rel_root != "." else fn
                arcname = os.path.join("contents", rel_fp).replace("\\", "/")
                zf.write(abs_fp, arcname=arcname)


def extract_zip_contents(zip_path: str, extract_dir: str, log_callback=None) -> str:
    """Extract zip into extract_dir and return path to manifest.json."""
    if log_callback:
        log_callback(f"[ZIP] Extracting zip: {zip_path}\n")

    safe_makedirs(extract_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    return os.path.join(extract_dir, GOOGLE_DRIVE_MANIFEST_NAME)


def copy_contents_into_target(zip_extract_dir: str, target_dir: str, log_callback=None) -> dict:
    """Copy contents/* into target_dir, overwriting any existing files.

    Restoring is meant to replace the current save state with the backup's,
    so this always overwrites — callers wanting a safety net should back up
    target_dir before calling this (see SaveFinderApp's restore workers).
    """
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

            safe_makedirs(os.path.dirname(dst_fp))

            shutil.copy2(src_fp, dst_fp)
            copied += 1

    if log_callback:
        log_callback(f"[RESTORE] Copied={copied}, skipped(existing)={skipped}, total={total}\n")

    return {"copied": copied, "skipped": skipped, "total": total}


def restore_zip_to_target(zip_path: str, target_dir: str, log_callback=None, temp_dir: str | None = None) -> dict:
    """Convenience helper for local restore."""
    if log_callback:
        log_callback("[RESTORE] Starting local restore...\n")

    with tempfile.TemporaryDirectory(prefix="savefinder_restore_", dir=temp_dir) as tmp:
        tmp_zip = os.path.join(tmp, "backup.zip")
        shutil.copy2(zip_path, tmp_zip)

        manifest_path = extract_zip_contents(tmp_zip, tmp, log_callback=log_callback)
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        final_target = target_dir or manifest.get("original_save_path")
        if not final_target:
            raise RuntimeError("Restore target directory not provided and manifest has no original_save_path.")

        final_target = normalize_save_path_for_current_user(str(final_target))
        final_target = os.path.abspath(final_target)
        safe_makedirs(final_target)

        stats = copy_contents_into_target(tmp, final_target, log_callback=log_callback)
        return {"manifest": manifest, "target": final_target, "stats": stats}

