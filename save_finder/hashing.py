import hashlib
import os
import re

ZIP_SHA256_PREFIX_LEN = 12
DRIVE_ZIP_NAME_DELIM = "__sha256_"


def compute_file_hash(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def compute_directory_tree_hash(root_path: str, chunk_size: int = 1024 * 1024) -> str:
    """Hash the file tree contents deterministically, ignoring timestamps."""
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
            # Length-prefix the content so a crafted file's trailing bytes
            # can't be mistaken for the next entry's name+terminator (two
            # genuinely different trees could otherwise hash identically).
            h.update(str(os.path.getsize(abs_fp)).encode("utf-8"))
            h.update(b"\0")
            with open(abs_fp, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    h.update(chunk)

    return h.hexdigest()


def drive_safe_filename_fragment(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(s)).strip("._- ")
    if not s:
        return "save"
    return s[:max_len]


def parse_zip_name_for_fields(filename: str) -> dict:
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

