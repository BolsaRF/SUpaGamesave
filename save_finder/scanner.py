import configparser
import json
import os
import re
import urllib.error
import urllib.request

from .hashing import compute_directory_tree_hash  # may be useful elsewhere


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

    # --- DEEP SCAN ---
    user_profile = os.environ.get("USERPROFILE", "")
    roots_to_scan = [
        os.path.join(user_profile, "Documents"),
        os.path.join(user_profile, "Documents", "My Games"),
        os.environ.get("LOCALAPPDATA", ""),
        os.path.join(user_profile, "AppData", "LocalLow"),
        os.environ.get("APPDATA", ""),
        os.path.join(user_profile, "Saved Games"),
        os.path.join(
            os.environ.get("PUBLIC", r"C:\\Users\\Public"),
            "Documents",
            "Steam",
            app_id if app_id else "UNKNOWN",
        ),
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
                        if os.path.isdir(sub_item_path) and any(
                            term in sub_item.lower() for term in all_search_terms
                        ):
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

