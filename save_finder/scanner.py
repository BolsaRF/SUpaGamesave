import configparser
import json
import os
import re
import sys
import urllib.error
import urllib.request
try:
    import winreg
except ImportError:
    winreg = None  # type: ignore
 
# Allow running this file directly for testing, which is likely how the
# user encountered the ImportError.
if __package__ is None or __package__ == "":
    # Add project root to sys.path so `import save_finder` works
    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)
    from save_finder.hashing import compute_directory_tree_hash
else:
    from .hashing import compute_directory_tree_hash
 
def _get_steam_path(log_callback):
    """Tries to find Steam installation path from the registry."""
    if not winreg:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
            if steam_path and os.path.isdir(steam_path):
                return steam_path
    except Exception as e:
        log_callback(f"   [API] Could not read Steam path from registry: {e}\n")

    # Fallback to common locations
    for path in [
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
    ]:
        if path:
            steam_path_fallback = os.path.join(path, "Steam")
            if os.path.isdir(steam_path_fallback):
                return steam_path_fallback
    return None


def find_steam_userdata_folders(log_callback):
    """Tries to find Steam userdata folders."""
    steam_path = _get_steam_path(log_callback)
    if not steam_path:
        log_callback("   [API] Steam installation path not found.\n")
        return []

    userdata_path = os.path.join(steam_path, "userdata")
    if not os.path.isdir(userdata_path):
        return []

    user_folders = []
    try:
        for item in os.listdir(userdata_path):
            # User IDs are numbers. Ignore '0' and other non-numeric folders.
            if item.isdigit() and item != "0":
                full_path = os.path.join(userdata_path, item)
                if os.path.isdir(full_path):
                    user_folders.append(full_path)
    except Exception as e:
        log_callback(f"   [API] Could not list Steam userdata folders: {e}\n")

    return user_folders


def get_steam_api_data(app_id, log_callback):
    """Pings Steam Web API to extract developer and publisher token lists."""
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
        "the",
    ]
    devs = [w for w in devs if len(w) > 2 and w not in stopwords]
    pubs = [w for w in pubs if len(w) > 2 and w not in stopwords]
    return list(set(devs)), list(set(pubs))


def get_executable_keywords(game_directory):
    """Scans for game .exe names and extracts internal codenames."""
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
                clean_exe = re.sub(
                    r"(-win64-shipping|-win32-shipping|64|32)$", "", base_name
                )

                if "redist" not in clean_exe and "setup" not in clean_exe:
                    if clean_exe not in ignore_list and len(clean_exe) > 2:
                        keywords.append(clean_exe)

    return list(set(keywords))


def get_unreal_project_name(game_directory):
    """Detect Unreal-style directories and extract the internal Project folder name."""
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
    clean_name = re.sub(
        r"[._](v\d+|build|update|patch).*$", "", raw_game_name, flags=re.IGNORECASE
    )
    clean_name = re.sub(r"[._-]", " ", clean_name).strip()

    words = clean_name.split()
    stopwords = [
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "and",
        "for",
        "to",
        "build",
        "game",
        "edition",
    ]
    valid_words = [
        w
        for w in words
        if w.lower() not in stopwords and len(w) > 2 and not w.isdigit()
    ]

    base_keyword = (
        max(valid_words, key=len).lower()
        if valid_words
        else (words[0].lower() if words else raw_game_name.lower())
    )

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
        log_callback(
            "\n[SUCCESS] Found portable/local save folders inside the game directory:\n"
        )
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
                log_callback(
                    f"[SUCCESS] Found explicit Save Path in .ini:\n-> {save_path}\n"
                )
                final_return_paths = list(local_save_folders)
                final_return_paths.append(save_path)
                success_callback(list(set(final_return_paths)))
                return
            else:
                log_callback(
                    f"   Extracted AppID: {app_id} (No explicit SavePath inside .ini)\n"
                )
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

    verified_save_directories = []
    appdata = os.environ.get("APPDATA", "")
    public_docs = os.path.join(os.environ.get("PUBLIC", r"C:\\Users\\Public"), "Documents")

    # --- HIGH-CONFIDENCE PATHS ---
    log_callback("   [SCAN] Checking high-confidence emulator and user paths...\n")
    high_confidence_paths = []
    if app_id:
        # Steam/CODEX paths
        high_confidence_paths.append(os.path.join(public_docs, "Steam", "CODEX", app_id))
        high_confidence_paths.append(os.path.join(public_docs, "Steam", app_id))

        # Goldberg paths
        high_confidence_paths.append(os.path.join(appdata, "Goldberg SteamEmu Saves", app_id))
        high_confidence_paths.append(os.path.join(appdata, "Goldberg UplayEmu Saves", app_id))

        # Steam userdata paths
        steam_userdata_folders = find_steam_userdata_folders(log_callback)
        for user_folder in steam_userdata_folders:
            high_confidence_paths.append(os.path.join(user_folder, app_id))

    for path in high_confidence_paths:
        if path and os.path.isdir(path):
            log_callback(f"   [SCAN] Found high-confidence path: {path}\n")
            # Add with a very high score to ensure it's included.
            verified_save_directories.append((path, 100))

    # --- HIGH-CONFIDENCE PATHS (AppID UNKNOWN) ---
    # This block is crucial for when we can't find an AppID in an .ini file.
    # It scans common emulator roots for any numeric folder, assuming it's an AppID.
    # This is a heuristic, but a very effective one for this class of games.
    log_callback("   [SCAN] Checking common emulator roots for any numeric AppID folders...\n")
    emu_roots_to_scan_for_any_appid = [
        (os.path.join(appdata, "Goldberg SteamEmu Saves"), "Goldberg SteamEmu"),
        (os.path.join(appdata, "Goldberg UplayEmu Saves"), "Goldberg UplayEmu"),
        (os.path.join(public_docs, "Steam", "CODEX"), "CODEX"),
        # Some games place the AppID folder directly in Public Documents\Steam
        (os.path.join(public_docs, "Steam"), "Steam"),
    ]
    for root, emu_name in emu_roots_to_scan_for_any_appid:
        if not os.path.isdir(root):
            continue
        try:
            for item in os.listdir(root):
                # If a subfolder is just a number, it's an AppID folder.
                if item.isdigit() and os.path.isdir(os.path.join(root, item)):
                    path = os.path.join(root, item)
                    log_callback(f"   [SCAN] Found potential {emu_name} save folder: {path}\n")
                    # Add with a score high enough to pass, but lower than a confirmed AppID match.
                    verified_save_directories.append((path, 75))
        except Exception as e:
            log_callback(f"   [SCAN] Error scanning {root}: {e}\n")

    # --- DEEP SCAN ---
    user_profile = os.environ.get("USERPROFILE", "")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    roots_to_scan = [
        os.path.join(user_profile, "Documents"),
        os.path.join(user_profile, "Documents", "My Games"),
        localappdata,
        os.path.join(user_profile, "AppData", "LocalLow"),
        appdata,
        os.path.join(user_profile, "Saved Games"),
        public_docs,
    ]

    # Add base emulator folders for keyword scanning, as a fallback
    roots_to_scan.append(os.path.join(appdata, "Goldberg SteamEmu Saves"))
    roots_to_scan.append(os.path.join(appdata, "Goldberg UplayEmu Saves"))
    roots_to_scan.append(os.path.join(public_docs, "Steam"))

    # Add Steam userdata folders if no app_id is known
    if not app_id:
        steam_userdata_folders = find_steam_userdata_folders(log_callback)
        roots_to_scan.extend(steam_userdata_folders)

    roots_to_scan = sorted(list(set(p for p in roots_to_scan if p and os.path.isdir(p))))

    candidate_paths = []
    all_search_terms = list(set(high_priority_keywords + dev_keywords + pub_keywords))
    MAX_SEARCH_DEPTH = 4

    for root_dir in roots_to_scan:
        root_level = root_dir.count(os.sep)
        try:
            for dirpath, dirnames, _ in os.walk(root_dir, topdown=True):
                if dirpath.count(os.sep) - root_level >= MAX_SEARCH_DEPTH:
                    dirnames[:] = []
                    continue

                for d in dirnames:
                    if any(term in d.lower() for term in all_search_terms):
                        candidate_paths.append(os.path.join(dirpath, d))
        except PermissionError:
            log_callback(f"   [SCAN] Permission denied during scan of '{root_dir}', results may be incomplete.\n")
            pass

    candidate_paths = list(set(candidate_paths))

    # --- SCORING SYSTEM ROUTINE ---
    for path in candidate_paths:
        score = 0
        path_lower = path.lower()
        folder_name = os.path.basename(path).lower()

        if any(hp in folder_name for hp in high_priority_keywords):
            score += 50
        if any(
            s_term in path_lower for s_term in ["save", "savedgames", "saves", "remote"]
        ):
            score += 30
        if any(dev in path_lower for dev in dev_keywords):
            score += 25
        if any(pub in path_lower for pub in pub_keywords):
            score += 10

        if score >= 40:
            verified_save_directories.append((path, score))

    # --- CONSOLIDATE & SCORE ---
    # Consolidate paths from all search methods, keeping the highest score for each unique path.
    path_scores = {}
    for path, score in verified_save_directories:
        path_scores[path] = max(path_scores.get(path, 0), score)

    consolidated_saves = list(path_scores.items())
    consolidated_saves.sort(key=lambda x: x[1], reverse=True)
    log_callback("   [SCAN] Consolidating and sorting all found paths...\n")

    # --- SUBFOLDER FILTERING ---
    # Remove paths that are subdirectories of other, higher-scoring paths.
    final_roots = []
    for path, score in consolidated_saves:
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
                log_callback(
                    f"   [!] Verified Unreal Engine Directory Subpath:\n   {ue_save}\n"
                )
                final_return_paths.append(ue_save)

    if final_return_paths:
        success_callback(list(set(final_return_paths)))
    else:
        log_callback(
            "\n[FAILED] No legitimate game save data profiles cleared the verification criteria.\n"
        )
        success_callback([])


if __name__ == "__main__":
    # --- TEST RUNNER ---
    # Allows running the scanner directly from the command line for testing.
    # Usage: python save_finder/scanner.py "C:\Path\To\Your\Game"

    if len(sys.argv) < 2:
        print("Usage: python save_finder/scanner.py \"<path_to_game_directory>\"")
        sys.exit(1)

    game_dir_to_test = sys.argv[1]

    def _test_log_callback(message):
        """Simple logger that prints to the console."""
        print(message, end="")

    def _test_success_callback(found_paths):
        """Simple results printer."""
        print("\n--- SCAN COMPLETE ---")
        print(f"Found {len(found_paths)} potential save path(s):")
        for p in found_paths:
            print(f"  -> {p}")

    run_save_finder(game_dir_to_test, _test_log_callback, _test_success_callback)
