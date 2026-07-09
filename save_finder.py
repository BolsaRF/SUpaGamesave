import os
import configparser
import re
import json
import urllib.request
import urllib.error
import threading
import queue

from datetime import datetime

import customtkinter as ctk
from tkinter import filedialog
import subprocess


# --- BACKEND LOGIC ---

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

    base_keyword = (
        max(valid_words, key=len).lower() if valid_words else (words[0].lower() if words else raw_game_name.lower())
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
                
                # Combine local saves with the explicit path
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
    
    # We now combine both lists so they don't overwrite each other!
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
        self.discovered_paths = []
        self._result_row_widgets = []

        # Window
        self.title("Universal Game Save Finder & Backup")
        self.geometry("850x700")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # Header
        self.title_label = ctk.CTkLabel(self, text="Universal Game Save Finder", font=ctk.CTkFont(size=24, weight="bold"))
        self.title_label.pack(pady=(20, 5))

        self.subtitle_label = ctk.CTkLabel(
            self,
            text="Select a game root folder to analyze and map configuration profiles.",
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

        # Logs
        self.log_label = ctk.CTkLabel(self, text="Console Log Output", font=ctk.CTkFont(size=14, weight="bold"))
        self.log_label.pack(anchor="w", padx=30, pady=(10, 0))

        self.log_controls_frame = ctk.CTkFrame(self)
        self.log_controls_frame.pack(fill="x", padx=30, pady=(5, 0))

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

        # Smaller log box: backend phase messages only (height-limited)
        self.console_output = ctk.CTkTextbox(self, height=95, font=ctk.CTkFont(family="Consolas", size=12))
        self.console_output.pack(fill="x", expand=False, padx=30, pady=(5, 10))

        self.console_output.configure(state="disabled")

        # Results panel
        self.results_label = ctk.CTkLabel(self, text="Detected Save Locations", font=ctk.CTkFont(size=14, weight="bold"))
        self.results_label.pack(anchor="w", padx=30, pady=(5, 0))

        self.results_frame = ctk.CTkFrame(self)
        self.results_frame.pack(fill="both", expand=True, padx=30, pady=(5, 15))

        self.results_scroll = ctk.CTkScrollableFrame(self.results_frame, fg_color="transparent")
        self.results_scroll.pack(fill="both", expand=True, padx=10, pady=10)

        # Tree-like selector
        # - show detected roots as expandable sections
        # - inside each section show immediate subfolders with Copy/Open
        self._tree_sections = []  # list of section frames


        self.process_log_queue()


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
                if not self.console_output.tag_cget(tag, "foreground"):
                    color_map = {
                        "INFO": "#d0d0d0",
                        "SUCCESS": "#44dd55",
                        "WARN": "#ffcc00",
                        "ERROR": "#ff4444",
                    }
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
        # single-shot UI write (UI thread)
        self.console_output.configure(state="normal")
        self.console_output.insert("end", text)
        if self._log_autoscroll:
            self.console_output.see("end")
        self.console_output.configure(state="disabled")

    def _clear_results(self):
        # Clear previous flat rows and tree sections
        for w in getattr(self, "_result_row_widgets", []):
            try:
                w.destroy()
            except Exception:
                pass
        self._result_row_widgets = []

        for w in getattr(self, "_tree_sections", []):
            try:
                w.destroy()
            except Exception:
                pass
        self._tree_sections = []


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
        """Create expandable section for a detected root save path."""
        section = ctk.CTkFrame(self.results_scroll, fg_color="transparent")
        section.pack(fill="x", pady=6)

        # Header row
        header = ctk.CTkFrame(section, fg_color="transparent")
        header.pack(fill="x")

        expander_state = {"expanded": False}
        children_frame = ctk.CTkFrame(section, fg_color="transparent")
        children_frame.pack(fill="x")
        children_frame.forget()  # hidden until expanded

        def toggle():
            if expander_state["expanded"]:
                children_frame.forget()
                expander_state["expanded"] = False
            else:
                # populate once
                self._populate_children(children_frame, root_path)
                children_frame.pack(fill="x", pady=(4, 0))
                expander_state["expanded"] = True

        # Left: expander button + root label
        exp_btn = ctk.CTkButton(
            header,
            text="▶",
            width=28,
            command=lambda: (toggle(), exp_btn.configure(text="▼" if not expander_state["expanded"] else "▶")),
        )
        exp_btn.pack(side="left", padx=(8, 4), pady=4)

        root_label = ctk.CTkLabel(header, text=root_path, anchor="w", font=ctk.CTkFont(size=12))
        root_label.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=4)

        copy_btn = ctk.CTkButton(header, text="Copy", width=70, command=lambda p=root_path: self._copy_path(p))
        copy_btn.pack(side="right", padx=(0, 8))

        open_btn = ctk.CTkButton(header, text="Open", width=70, command=lambda p=root_path: self._open_in_explorer(p))
        open_btn.pack(side="right", padx=(0, 10))

        self._tree_sections.append(section)

    def _populate_children(self, children_frame, root_path: str, max_items: int = 200):
        """List immediate subfolders under root_path (tree children)."""
        # clear old children
        for w in getattr(children_frame, "winfo_children", lambda: [])():
            try:
                w.destroy()
            except Exception:
                pass

        try:
            entries = os.listdir(root_path)
        except Exception:
            entries = []

        subfolders = []
        for e in entries:
            p = os.path.join(root_path, e)
            if os.path.isdir(p):
                subfolders.append(p)

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

            # Allow selecting leaf subfolder: Copy/Open
            cbtn = ctk.CTkButton(row, text="Copy", width=60, command=lambda p=sp: self._copy_path(p))
            cbtn.pack(side="right", padx=(0, 6))

            obtn = ctk.CTkButton(row, text="Open", width=60, command=lambda p=sp: self._open_in_explorer(p))
            obtn.pack(side="right", padx=(0, 6))


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
        self.discovered_paths = paths or []
        self.scan_btn.configure(state="normal", text="Scan for Saves")

        # Populate results panel (tree sections)
        self._clear_results()
        for p in self.discovered_paths:
            self._add_result_section(p)


        if self.discovered_paths:
            self._append_log_text("\n[READY] Scan finished. Save locations verified and locked.\n")
        else:
            self._append_log_text("\n[FINISHED] Scan completed with no locations found.\n")


if __name__ == "__main__":
    app = SaveFinderApp()
    app.mainloop()