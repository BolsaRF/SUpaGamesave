# GUI layer
# This file is the refactored GUI entry for SaveFinder.
# It uses extracted backends/utilities from the `save_finder/` package.

from __future__ import annotations

import os
import threading
import queue
import configparser
import re
import urllib.request
import urllib.error
import tempfile
import subprocess
import webbrowser
import sys
from datetime import datetime

# Optional tray dependencies
try:
    import pystray  # type: ignore
    from PIL import Image, ImageDraw  # type: ignore
except Exception:
    pystray = None

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog
from tkinter import simpledialog, messagebox

try:
    # When executed as part of the `save_finder` package
    from .gui.log_view import LogView
    from .gui.results_view import ResultsView, ResultsCallbacks
    from .gui.profiles_view import ProfilesView, ProfilesCallbacks, _pick_newest_by_modifiedTime
except ImportError:  # pragma: no cover
    # When executed directly (e.g., `python save_finder/gui_app.py`)
    LogView = None  # type: ignore
    ResultsView = None  # type: ignore
    ResultsCallbacks = None  # type: ignore
    ProfilesView = None  # type: ignore
    ProfilesCallbacks = None  # type: ignore
    _pick_newest_by_modifiedTime = None  # type: ignore



# ---- Import extracted backends/utilities (new package) ----

# Allow running this file directly (python save_finder/gui_app.py)
if __package__ is None or __package__ == "":
    # Add project root to sys.path so `import save_finder` works
    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)

from save_finder.app_config import (

    APP_SETTINGS_FILE,
    APP_SETTINGS_SECTION,
    APP_SETTINGS_SELECTED_PROFILE,
    APP_SETTINGS_STORAGE_BACKEND,
    APP_SETTINGS_LOCAL_ROOT,
    APP_SETTINGS_AUTO_BACKUP,
    APP_SETTINGS_START_AT_LOGIN,
    APP_SETTINGS_WINDOW_GEOMETRY,
    APP_SETTINGS_PANEL_SPLIT,
    load_setting,
    save_setting,
    set_start_at_login,
)

# Import extracted hashing helpers.
if __package__ is None or __package__ == "":
    from save_finder.hashing import (
        ZIP_SHA256_PREFIX_LEN,
        DRIVE_ZIP_NAME_DELIM,
        compute_directory_tree_hash,
        drive_safe_filename_fragment,
    )
else:
    from .hashing import (
        ZIP_SHA256_PREFIX_LEN,
        DRIVE_ZIP_NAME_DELIM,
        compute_directory_tree_hash,
        drive_safe_filename_fragment,
    )

if __package__ is None or __package__ == "":
    from save_finder.scanner import run_save_finder
else:
    from .scanner import run_save_finder

if __package__ is None or __package__ == "":
    from save_finder.storage_drive import (
        _drive_enabled,
        drive_get_credentials,
        drive_get_service,
        drive_get_or_create_app_folder,
        drive_get_or_create_profile_folder,
        drive_list_profiles,
        drive_list_profile_backups,
        drive_has_sha_dedupe_match,
        drive_upload_backup_zip,
        drive_cleanup_old_backups,
        drive_download_file,
        drive_restore_backup_zip,
    )
else:
    from .storage_drive import (
        _drive_enabled,
        drive_get_credentials,
        drive_get_service,
        drive_get_or_create_app_folder,
        drive_get_or_create_profile_folder,
        drive_list_profiles,
        drive_list_profile_backups,
        drive_has_sha_dedupe_match,
        drive_upload_backup_zip,
        drive_cleanup_old_backups,
        drive_download_file,
        drive_restore_backup_zip,
    )


if __package__ is None or __package__ == "":
    from save_finder.storage_local import (
        localfs_backups_root as _localfs_backups_root,
        list_profiles as localfs_list_profiles,
        get_or_create_profile_folder as localfs_get_or_create_profile_folder,
        list_profile_backups as localfs_list_profile_backups,
        has_sha_dedupe_match as localfs_has_sha_dedupe_match,
        upload_backup_zip as localfs_upload_backup_zip,
        cleanup_old_backups as localfs_cleanup_old_backups,
        download_file as localfs_download_file,
        restore_backup_zip as localfs_restore_backup_zip,
    )
else:
    from .storage_local import (
        localfs_backups_root as _localfs_backups_root,
        list_profiles as localfs_list_profiles,
        get_or_create_profile_folder as localfs_get_or_create_profile_folder,
        list_profile_backups as localfs_list_profile_backups,
        has_sha_dedupe_match as localfs_has_sha_dedupe_match,
        upload_backup_zip as localfs_upload_backup_zip,
        cleanup_old_backups as localfs_cleanup_old_backups,
        download_file as localfs_download_file,
        restore_backup_zip as localfs_restore_backup_zip,
    )


if __package__ is None or __package__ == "":
    from save_finder.zip_manifest import (
        create_zip_with_manifest as _create_zip_with_manifest,
        extract_zip_contents as _extract_zip_contents,
        copy_contents_into_target as _copy_contents_into_target,
    )
else:
    from .zip_manifest import (
        create_zip_with_manifest as _create_zip_with_manifest,
        extract_zip_contents as _extract_zip_contents,
        copy_contents_into_target as _copy_contents_into_target,
    )



def _debug_mark(message: str):
    try:
        p = os.path.dirname(os.path.abspath(__file__))
        f = os.path.join(p, "save_finder_debug.log")
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().isoformat()}] {message}\n")
    except Exception:
        pass


class SaveFinderApp(ctk.CTk):

    def __init__(self):
        super().__init__()

        _debug_mark("SaveFinderApp.__init__ start")

        self._log_queue = queue.Queue()
        self._log_autoscroll = True

        self.discovered_paths: list[str] = []
        self._tree_sections: list[ctk.CTkFrame] = []

        # Profiles UI state
        self._profiles_refreshing = False
        self.selected_profile_name: str | None = None
        self.profiles_panel_widgets = {}
        self._profile_rows = []

        self._settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), APP_SETTINGS_FILE)

        self._auto_backup_state: dict[str, dict[str, object]] = {}
        self._auto_backup_in_progress: set[str] = set()
        self._auto_backup_interval_ms = 30000
        self._auto_backup_enabled = load_setting(self._settings_path, APP_SETTINGS_AUTO_BACKUP, "0") == "1"

        # Window
        self.title("Universal Game Save Finder & Backup")
        saved_geometry = self._load_app_setting(APP_SETTINGS_WINDOW_GEOMETRY, None)
        try:
            self.geometry(saved_geometry or "1280x760")
        except Exception:
            self.geometry("1280x760")
        # Wide enough that the window itself can't be shrunk below what the
        # two panes need (a pane's minsize only stops the sash being
        # dragged past it, not the whole window being resized smaller).
        self.minsize(1360, 760)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # Header
        self.title_label = ctk.CTkLabel(
            self,
            text="Universal Game Save Finder",
            font=ctk.CTkFont(size=24, weight="bold"),
        )
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

        self.browse_btn = ctk.CTkButton(
            self.selection_frame,
            text="Browse Folder",
            width=150,
            command=self.browse_folder,
        )
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

        main_frame.grid_rowconfigure(4, weight=1)
        main_frame.grid_columnconfigure(0, weight=1, minsize=560)
        main_frame.grid_columnconfigure(1, weight=1, minsize=520)

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

        self.upload_progress_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        self.upload_progress_frame.grid(row=3, column=0, columnspan=2, sticky="ew", padx=(0, 0), pady=(0, 10))
        self.upload_progress_label = ctk.CTkLabel(
            self.upload_progress_frame,
            text="",
            text_color="gray",
            font=ctk.CTkFont(size=11),
        )
        self.upload_progress_label.pack(anchor="w", padx=10, pady=(0, 4))
        self.upload_progress_bar = ctk.CTkProgressBar(self.upload_progress_frame, width=1040)
        self.upload_progress_bar.pack(fill="x", padx=10, pady=(0, 4))
        self.upload_progress_frame.grid_remove()

        # Draggable split between the results panel and the profiles panel,
        # so the two panes can be resized instead of being fixed-width.
        self.main_paned = tk.PanedWindow(
            main_frame,
            orient="horizontal",
            sashwidth=6,
            sashrelief="raised",
            bg="#242424",
            bd=0,
            # Only resize the panes once on release, not on every pixel of
            # drag movement — with this many nested widgets inside each
            # pane, live-resizing during drag is visibly laggy.
            opaqueresize=False,
        )
        self.main_paned.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(0, 10))

        # Results panel (left)
        self.results_frame = ctk.CTkFrame(self.main_paned)
        self.main_paned.add(self.results_frame, minsize=520, stretch="always")
        self.results_frame.grid_rowconfigure(0, weight=0)
        self.results_frame.grid_rowconfigure(1, weight=1)
        self.results_frame.grid_columnconfigure(0, weight=1)

        self.results_label = ctk.CTkLabel(self.results_frame, text="Detected Save Locations", font=ctk.CTkFont(size=14, weight="bold"))
        self.results_label.pack(anchor="w", padx=10, pady=(10, 0))

        self.results_controls = ctk.CTkFrame(self.results_frame, fg_color="transparent")
        self.results_controls.pack(fill="x", padx=10, pady=(0, 6))

        self.results_toggle_btn = ctk.CTkButton(
            self.results_controls,
            text="Hide Results",
            width=120,
            command=self._toggle_results_visibility,
        )
        self.results_toggle_btn.pack(side="left", padx=(0, 4))

        self.results_scroll = ctk.CTkScrollableFrame(self.results_frame, fg_color="transparent")
        self.results_scroll.pack(fill="both", expand=True, padx=10, pady=10)
        self.results_visible = True

        # Profiles panel (right)
        self.profiles_frame = ctk.CTkFrame(self.main_paned)
        self.main_paned.add(self.profiles_frame, minsize=700, stretch="always")
        self.profiles_frame.grid_rowconfigure(5, weight=1)
        self.profiles_frame.grid_columnconfigure(0, weight=1)

        self.profiles_label = ctk.CTkLabel(self.profiles_frame, text="Profiles", font=ctk.CTkFont(size=14, weight="bold"))
        self.profiles_label.pack(anchor="w", padx=10, pady=(10, 0))

        self.profiles_controls = ctk.CTkFrame(self.profiles_frame, fg_color="transparent")
        self.profiles_controls.pack(fill="x", padx=10, pady=(5, 8))

        # Row 1: profile actions. Row 2: storage backend + local path.
        # Row 3: folder shortcuts + start-at-login. Split across three rows
        # (rather than one wide guess at a minimum panel width) so no single
        # row needs more than ~450px, leaving real margin for DPI scaling.
        self.profiles_controls_row1 = ctk.CTkFrame(self.profiles_controls, fg_color="transparent")
        self.profiles_controls_row1.pack(fill="x")

        self.profiles_controls_row2 = ctk.CTkFrame(self.profiles_controls, fg_color="transparent")
        self.profiles_controls_row2.pack(fill="x", pady=(6, 0))

        self.profiles_controls_row3 = ctk.CTkFrame(self.profiles_controls, fg_color="transparent")
        self.profiles_controls_row3.pack(fill="x", pady=(6, 0))

        self.profiles_refresh_btn = ctk.CTkButton(self.profiles_controls_row1, text="Refresh", width=100, command=self.refresh_profiles_ui)
        self.profiles_refresh_btn.pack(side="left", padx=(0, 8))

        self.profiles_add_btn = ctk.CTkButton(self.profiles_controls_row1, text="Add", width=80, command=self.add_profile_ui)
        self.profiles_add_btn.pack(side="left")

        self.profiles_delete_btn = ctk.CTkButton(
            self.profiles_controls_row1,
            text="Delete Selected Profile",
            width=240,
            command=self.delete_selected_profile_ui,
        )
        self.profiles_delete_btn.pack(side="left", padx=(10, 0))


        self.auto_backup_var = ctk.BooleanVar(value=self._auto_backup_enabled)
        self.auto_backup_checkbox = ctk.CTkCheckBox(
            self.profiles_controls_row1,
            text="Auto backup saves",
            variable=self.auto_backup_var,
            command=self._on_auto_backup_toggled,
        )
        self.auto_backup_checkbox._is_packed = False

        # Storage backend selector + local backups path
        stored_backend = load_setting(self._settings_path, APP_SETTINGS_STORAGE_BACKEND, "Drive" if _drive_enabled() else "Local") or (
            "Drive" if _drive_enabled() else "Local"
        )
        if str(stored_backend).lower() == "drive" and not _drive_enabled():
            stored_backend = "Local"

        self.storage_backend_var = ctk.StringVar(value=str(stored_backend).title())
        self.storage_selector = ctk.CTkOptionMenu(
            self.profiles_controls_row2,
            values=["Drive", "Local"],
            variable=self.storage_backend_var,
            command=lambda v: self._on_storage_backend_changed(v),
        )
        self.storage_selector.pack(side="left", padx=(0, 6))

        self.local_root_entry = ctk.CTkEntry(self.profiles_controls_row2, width=160, placeholder_text="Local backups root")
        self.local_root_entry.pack(side="left", padx=(6, 6))
        stored_local_root = load_setting(self._settings_path, APP_SETTINGS_LOCAL_ROOT)
        if stored_local_root and str(stored_local_root).strip():
            self.local_root_entry.insert(0, os.path.normpath(stored_local_root))
        else:
            self.local_root_entry.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups"))

        self.local_root_browse = ctk.CTkButton(self.profiles_controls_row2, text="Browse", width=60, command=self._choose_local_root)
        self.local_root_browse.pack(side="left", padx=(4, 0))

        # Start at login checkbox
        self._start_at_login = load_setting(self._settings_path, APP_SETTINGS_START_AT_LOGIN, "0") == "1"
        self.start_at_login_var = ctk.BooleanVar(value=self._start_at_login)
        self.start_at_login_checkbox = ctk.CTkCheckBox(
            self.profiles_controls_row3,
            text="Start at login",
            variable=self.start_at_login_var,
            command=self._on_start_at_login_toggled,
        )
        self.start_at_login_checkbox.pack(side="left", padx=(0, 0))

        self.open_profile_folder_btn = ctk.CTkButton(self.profiles_controls_row3, text="Open Profile Folder", width=140, command=self._open_selected_profile_folder)
        self.open_profile_folder_btn.pack(side="left", padx=(8, 0))

        self.view_storage_root_btn = ctk.CTkButton(self.profiles_controls_row3, text="View Storage Root", width=130, command=self._view_storage_root)
        self.view_storage_root_btn.pack(side="left", padx=(8, 0))

        self.storage_backend = "drive" if self.storage_backend_var.get().lower() == "drive" else "local"
        self.local_backups_root = _localfs_backups_root(self.local_root_entry.get().strip(), script_dir=os.path.dirname(os.path.abspath(__file__)))
        self._update_auto_backup_checkbox_visibility()

        self.profiles_list_scroll = ctk.CTkScrollableFrame(self.profiles_frame, fg_color="transparent")
        self.profiles_list_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 8))


        self.profile_backups_label = ctk.CTkLabel(self.profiles_frame, text="Backups in selected profile", font=ctk.CTkFont(size=12, weight="bold"))
        self.profile_backups_label.pack(anchor="w", padx=10, pady=(0, 0))

        self.profile_backups_scroll = ctk.CTkScrollableFrame(self.profiles_frame, fg_color="transparent")
        self.profile_backups_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 8))


        self.profiles_hint_label = ctk.CTkLabel(self.profiles_frame, text="", text_color="gray", font=ctk.CTkFont(size=10))

        self.managed_games_label = ctk.CTkLabel(self.profiles_frame, text="Games we manage (in current profile)", font=ctk.CTkFont(size=12, weight="bold"))
        self.managed_games_label.pack(anchor="w", padx=10, pady=(0, 6))

        self.managed_games_scroll = ctk.CTkScrollableFrame(self.profiles_frame, fg_color="transparent")
        self.managed_games_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.profiles_view = ProfilesView(self, ProfilesCallbacks(
            on_restore_backup=self._restore_backup_to_folder,
            on_download_backup=self._download_backup_file,
            on_delete_backup=self._delete_backup_file,
            on_restore_game_root=self.restore_game_root_from_profile,
            on_open_in_explorer=self._open_in_explorer,
        ))

        self.process_log_queue()
        self.selected_profile_name = self._load_selected_profile_name()
        self.after(200, self.refresh_profiles_ui)
        self.after(5000, self._auto_backup_poll)
        self.after(50, self._restore_panel_split)

        # Tray
        self._tray_icon = None
        self._tray_thread = None
        try:
            self._last_window_state = self.state()
            self.after(1000, self._watch_window_state)
        except Exception:
            self._last_window_state = None

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        try:
            if getattr(self, "start_at_login_var", None) and bool(self.start_at_login_var.get()):
                set_start_at_login(True)
        except Exception:
            pass

        self._append_log_text("\n[READY] App started. Backup/Restore enabled when Drive deps are installed.\n")

    # ---- settings persistence helpers ----
    def _load_app_setting(self, key: str, fallback: str | None = None) -> str | None:
        return load_setting(self._settings_path, key, fallback=fallback)

    def _save_app_setting(self, key: str, value: str | None):
        return save_setting(self._settings_path, key, value=value)


    def _load_selected_profile_name(self) -> str | None:
        return self._load_app_setting(APP_SETTINGS_SELECTED_PROFILE)

    def _save_selected_profile_name(self):
        self._save_app_setting(APP_SETTINGS_SELECTED_PROFILE, self.selected_profile_name or "")

    # -------- logging helpers --------
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

    def _show_upload_progress(self, label: str, fraction: float = 0.0):
        try:
            self.upload_progress_label.configure(text=label or "Uploading backup...")
            self.upload_progress_bar.set(max(0.0, min(1.0, fraction)))
            if not getattr(self.upload_progress_frame, "_visible", False):
                self.upload_progress_frame.grid()
                self.upload_progress_frame._visible = True
            try:
                if getattr(self, "_tray_icon", None):
                    pct = int(max(0.0, min(1.0, fraction)) * 100)
                    self._tray_icon.title = f"SaveFinder — {pct}% uploading"
            except Exception:
                pass
        except Exception:
            pass

    def _update_upload_progress(self, label: str, fraction: float = 0.0):
        self.after(0, lambda: self._show_upload_progress(label, fraction))

    def _reset_upload_progress(self):
        try:
            self.upload_progress_bar.set(0.0)
            self.upload_progress_label.configure(text="")
            if getattr(self.upload_progress_frame, "_visible", False):
                self.upload_progress_frame.grid_remove()
                self.upload_progress_frame._visible = False
        except Exception:
            pass

    # --- Tray / Background helpers ---
    def _on_start_at_login_toggled(self):
        enabled = bool(self.start_at_login_var.get())
        self._save_app_setting(APP_SETTINGS_START_AT_LOGIN, "1" if enabled else "0")
        ok = set_start_at_login(enabled)
        if not ok:
            self._append_log_text(f"\n[WARN] Could not {'enable' if enabled else 'disable'} start-at-login (platform or permission issue).\n")
        try:
            self._update_tray_state()
        except Exception:
            pass

    def _create_tray_icon(self):
        if pystray is None:
            return None

        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tray_icon.png")
        img = None
        try:
            if os.path.isfile(icon_path):
                img = Image.open(icon_path).convert("RGBA")
        except Exception:
            img = None

        if img is None:
            try:
                img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                draw.ellipse((4, 4, 60, 60), fill=(30, 144, 255, 255))
                draw.ellipse((12, 12, 52, 52), fill=(255, 255, 255, 255))
                try:
                    from PIL import ImageFont

                    fnt = ImageFont.load_default()
                    draw.text((18, 18), "SF", font=fnt, fill=(30, 144, 255, 255))
                except Exception:
                    draw.rectangle((18, 18, 22, 44), fill=(30, 144, 255, 255))
                    draw.rectangle((26, 18, 38, 24), fill=(30, 144, 255, 255))
                    draw.rectangle((26, 30, 38, 36), fill=(30, 144, 255, 255))
                try:
                    img.save(icon_path)
                except Exception:
                    pass
            except Exception:
                img = Image.new("RGBA", (64, 64), (30, 144, 255, 255))

        def on_open(icon, item):
            try:
                self.after(0, lambda: (self.deiconify(), self.lift()))
            except Exception:
                pass

        def on_quit(icon, item):
            try:
                icon.stop()
            except Exception:
                pass
            try:
                self.after(0, lambda: self.destroy())
            except Exception:
                pass

        def tray_toggle_auto(icon, item):
            try:
                def _do():
                    cur = bool(self.auto_backup_var.get())
                    self.auto_backup_var.set(not cur)
                    try:
                        self._on_auto_backup_toggled()
                    except Exception:
                        pass

                self.after(0, _do)
            except Exception:
                pass

        def tray_toggle_start(icon, item):
            try:
                def _do():
                    cur = bool(self.start_at_login_var.get())
                    self.start_at_login_var.set(not cur)
                    try:
                        self._on_start_at_login_toggled()
                    except Exception:
                        pass

                self.after(0, _do)
            except Exception:
                pass

        def tray_status(icon, item):
            try:
                def _show():
                    backend = getattr(self, "storage_backend_var", None)
                    backend_val = backend.get() if backend else getattr(self, "storage_backend", "unknown")
                    selected = getattr(self, "selected_profile_name", None) or "<none>"
                    autos = "On" if bool(self.auto_backup_var.get()) else "Off"
                    startup = "On" if bool(self.start_at_login_var.get()) else "Off"
                    messagebox.showinfo(
                        "SaveFinder Status",
                        f"Profile: {selected}\nStorage: {backend_val}\nAuto backup: {autos}\nStart at login: {startup}",
                    )

                self.after(0, _show)
            except Exception:
                pass

        menu = pystray.Menu(
            pystray.MenuItem("Open", on_open),
            pystray.MenuItem(
                "Auto backup",
                tray_toggle_auto,
                checked=lambda item: bool(self.auto_backup_var.get()),
            ),
            pystray.MenuItem(
                "Start at login",
                tray_toggle_start,
                checked=lambda item: bool(self.start_at_login_var.get()),
            ),
            pystray.MenuItem("Status", tray_status),
            pystray.MenuItem("Exit", on_quit),
        )

        try:
            return pystray.Icon("SaveFinder", img, "SaveFinder", menu)
        except Exception:
            return None

    def _start_tray(self):
        if pystray is None:
            self._append_log_text("\n[WARN] Tray not available (pystray missing).\n")
            return
        if getattr(self, "_tray_icon", None) is not None:
            return
        try:
            self._tray_icon = self._create_tray_icon()
            if self._tray_icon:
                def _run_icon():
                    try:
                        self._tray_icon.run()
                    except Exception:
                        pass

                self._tray_thread = threading.Thread(target=_run_icon, daemon=True)
                self._tray_thread.start()
        except Exception:
            pass

    def _update_tray_state(self):
        try:
            if getattr(self, "_tray_icon", None):
                title_parts = ["SaveFinder"]
                try:
                    title_parts.append("Auto:On" if bool(self.auto_backup_var.get()) else "Auto:Off")
                except Exception:
                    pass
                try:
                    title_parts.append(
                        "StartAtLogin:On" if bool(self.start_at_login_var.get()) else "StartAtLogin:Off"
                    )
                except Exception:
                    pass
                self._tray_icon.title = " — ".join(title_parts)
        except Exception:
            pass

    def _stop_tray(self):
        try:
            if getattr(self, "_tray_icon", None):
                try:
                    self._tray_icon.stop()
                except Exception:
                    pass
                self._tray_icon = None
            self._tray_thread = None
        except Exception:
            pass

    def _minimize_to_tray(self):
        try:
            self.withdraw()
            self._start_tray()
            self._append_log_text("\n[INFO] App minimized to tray.\n")
        except Exception as e:
            self._append_log_text(f"\n[ERROR] Minimize to tray failed: {e}\n")

    def _restore_panel_split(self):
        try:
            saved = self._load_app_setting(APP_SETTINGS_PANEL_SPLIT, None)
            if saved:
                self.main_paned.sash_place(0, int(saved), 1)
        except Exception:
            pass

    def _on_close(self):
        try:
            self._save_app_setting(APP_SETTINGS_WINDOW_GEOMETRY, self.geometry())
            self._save_app_setting(APP_SETTINGS_PANEL_SPLIT, str(self.main_paned.sash_coord(0)[0]))
        except Exception:
            pass

        try:
            resp = messagebox.askyesno(
                "Exit SaveFinder",
                "Leave the app running in background (minimize to tray)?\nClick Yes to keep running in background, No to exit completely.",
            )
            if resp:
                self._minimize_to_tray()
            else:
                try:
                    self._stop_tray()
                except Exception:
                    pass
                self.destroy()
        except Exception:
            try:
                self.destroy()
            except Exception:
                pass

    def _watch_window_state(self):
        try:
            cur = self.state()
            if cur != getattr(self, "_last_window_state", None):
                if cur == "iconic":
                    self._minimize_to_tray()
                elif cur in ("normal", "zoomed"):
                    self._stop_tray()
                self._last_window_state = cur
        except Exception:
            pass
        finally:
            try:
                self.after(1000, self._watch_window_state)
            except Exception:
                pass

    # -------- results UI --------
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

    def _toggle_results_visibility(self):
        self.results_visible = not getattr(self, "results_visible", True)
        if self.results_visible:
            self.results_scroll.pack(fill="both", expand=True, padx=10, pady=10)
            self.results_toggle_btn.configure(text="Hide Results")
        else:
            self.results_scroll.forget()
            self.results_toggle_btn.configure(text="Show Results")

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

        try:
            root_label.bind("<Double-Button-1>", lambda e, p=root_path: self._on_label_double_click_restore(p))
        except Exception:
            pass

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

    # -------- backend selection --------
    def _ensure_drive_available_or_log(self) -> bool:
        if not _drive_enabled():
            self._append_log_text("\n[DRIVE] Google Drive backend not available (install google-api-python-client + deps).\n")
            return False
        return True

    def _on_storage_backend_changed(self, value: str):
        v = (value or "").lower()
        self.storage_backend = "drive" if v == "drive" else "local"
        self._save_app_setting(APP_SETTINGS_STORAGE_BACKEND, self.storage_backend)
        self.local_backups_root = _localfs_backups_root(
            self.local_root_entry.get().strip(),
            script_dir=os.path.dirname(os.path.abspath(__file__)),
        )
        self.refresh_profiles_ui()

    def _on_auto_backup_toggled(self):
        self._auto_backup_enabled = bool(self.auto_backup_var.get())
        self._save_app_setting(APP_SETTINGS_AUTO_BACKUP, "1" if self._auto_backup_enabled else "0")
        if self._auto_backup_enabled:
            self._reset_auto_backup_state()
        try:
            self._update_tray_state()
        except Exception:
            pass

    def _choose_local_root(self):
        sel = filedialog.askdirectory()
        if sel:
            self.local_root_entry.delete(0, "end")
            self.local_root_entry.insert(0, os.path.normpath(sel))
            self.local_backups_root = _localfs_backups_root(
                self.local_root_entry.get().strip(),
                script_dir=os.path.dirname(os.path.abspath(__file__)),
            )
            self._save_app_setting(APP_SETTINGS_LOCAL_ROOT, self.local_backups_root)
            if self.storage_backend == "local":
                self.refresh_profiles_ui()

    # -------- auto backup --------
    def _reset_auto_backup_state(self):
        self._auto_backup_state = {}
        for path in list(self.discovered_paths):
            if os.path.isdir(path):
                self._auto_backup_state[path] = {
                    "latest_mtime": self._get_directory_latest_mtime(path),
                    "hash": compute_directory_tree_hash(path) if os.path.isdir(path) else None,
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

                    current_hash = compute_directory_tree_hash(path)
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

                    # Global auto-backup: determine the target profile for each save root
                    # without mutating the UI-selected profile.
                    profile_name = self._default_backup_profile_name(path)

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

    # -------- scan flow --------
    def _detected_save_root_name(self, root_path: str) -> str:
        return os.path.basename(os.path.normpath(root_path))

    # Whole-folder-name matches (not just suffixes) that are generic
    # save-data containers, not the game's own name — e.g. Unity's
    # "<Game>_Data" sibling folder, or common save-subfolder conventions.
    _GENERIC_SAVE_CONTAINER_NAMES = {
        "save", "saves", "savefile", "savefiles", "save data", "savedata",
        "savegame", "savegames", "saved games", "savedgames", "remote",
        "storage", "userdata", "cloud", "cloudsaves", "cloud saves", "data",
    }

    def _derive_profile_name_from_path(self, path: str | None) -> str | None:
        candidate = (path or "").strip()
        if not candidate:
            return None
        candidate = os.path.basename(os.path.normpath(candidate))
        cleaned = re.sub(r"[\\/]+", " ", candidate).strip(" ._- ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            cleaned = re.sub(
                r"(?i)\s*[-_.]?(v\d+(?:\.\d+)*|build|update|patch|p2p|repack|cracked|steam|epic|gog|demo|data)(?:[-_.]?\w+)?$",
                "",
                cleaned,
            ).strip()
            cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._- ")
        if cleaned and cleaned.lower() in self._GENERIC_SAVE_CONTAINER_NAMES:
            return None
        return cleaned or None

    @staticmethod
    def _normalize_profile_name_for_match(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", name.lower())

    def _match_existing_profile_name(self, name: str) -> str:
        """If a profile with this name is already in the sidebar, return its
        exact existing name instead of a fresh guess. The comparison ignores
        case and punctuation/spacing (e.g. "octopath traveler 0" vs
        "Octopath.Traveler.0" both match), since folder-name-derived guesses
        rarely match a hand-typed profile name character-for-character.

        Also prevents near-duplicate profiles, since Drive folder names are
        case-sensitive even though this comparison isn't.
        """
        target = self._normalize_profile_name_for_match(name)
        for existing in getattr(self, "_known_profile_names", []) or []:
            if self._normalize_profile_name_for_match(existing) == target:
                return existing
        return name

    def _default_backup_profile_name(self, root_path: str | None = None) -> str:
        for candidate in [self.path_entry.get().strip(), root_path]:
            name = self._derive_profile_name_from_path(candidate)
            if name:
                return self._match_existing_profile_name(name)

        # The save folder's own name might just be a generic launcher/
        # platform tag (e.g. "steam", "epic", "gog") that gets cleaned to
        # nothing above. Walk up to parent folders looking for the actual
        # game name instead of falling back to that raw, uncleaned name.
        if root_path:
            current = os.path.dirname(os.path.normpath(root_path))
            for _ in range(4):
                if not current:
                    break
                name = self._derive_profile_name_from_path(current)
                if name:
                    return self._match_existing_profile_name(name)
                parent = os.path.dirname(current)
                if not parent or parent == current:
                    break
                current = parent

        fallback = self._detected_save_root_name(root_path) if root_path else "Default"
        return self._match_existing_profile_name(fallback)

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
                                    drive_get_or_create_profile_folder(
                                        service,
                                        app_folder_id,
                                        profile_to_ensure,
                                        log_callback=None,
                                    )
                                else:
                                    localfs_get_or_create_profile_folder(
                                        self.local_backups_root,
                                        profile_to_ensure,
                                        log_callback=None,
                                    )
                                self.after(0, lambda: self.refresh_profiles_ui(prefer_local_results=True))
                            except Exception as e:
                                err_msg = str(e)
                                self.after(0, lambda: self._append_log_text(f"\n[ERROR] Auto-create profile failed: {err_msg}\n"))

                        threading.Thread(target=_ensure_profile_remote, daemon=True).start()
                except Exception:
                    pass

            self.refresh_profiles_ui(prefer_local_results=True)

        self.after(0, _apply_ui)

    # --- Profiles UI ---

    def refresh_profiles_ui(self, prefer_local_results: bool = False):
        self.profiles_view.refresh_profiles_ui(prefer_local_results=prefer_local_results)


    def start_backup(self, root_path: str):
        if not root_path or not os.path.isdir(root_path):
            self._append_log_text("\n[ERROR] Backup target is not a valid directory.\n")
            return
        if self.storage_backend == "drive" and not self._ensure_drive_available_or_log():
            return

        save_root = self._detected_save_root_name(root_path)
        # Prefer whatever profile is currently selected in the sidebar over
        # guessing from the folder path — if you've already got a profile
        # selected, that's almost always the one this backup belongs to.
        default_profile = self.selected_profile_name or self._default_backup_profile_name(root_path)

        self._append_log_text(f"\n[STORAGE] Backup clicked for: {root_path} (backend={self.storage_backend})\n")

        profile_name = simpledialog.askstring(
            "Backup Profile",
            f"Profile name for '{save_root}':",
            initialvalue=default_profile,
        )
        if not profile_name or not str(profile_name).strip():
            self._append_log_text("[DRIVE] Backup cancelled (no profile name provided).\n")
            return

        profile_name = str(profile_name).strip()
        self.selected_profile_name = profile_name
        self._save_selected_profile_name()
        self.refresh_profiles_ui()

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
                profile_folder_id = localfs_get_or_create_profile_folder(self.local_backups_root, profile_name, log_callback=log_worker)

            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            content_hash = compute_directory_tree_hash(root_path)
            sha_manifest = {
                "timestamp": timestamp,
                "game_root": save_root,
                "original_save_path": root_path,
                "restore_target_hint": "original_save_path",
                "content_hash": content_hash,
            }

            with tempfile.TemporaryDirectory(prefix="savefinder_backup_") as tmp:
                zip_path = os.path.join(tmp, "backup.zip")
                _create_zip_with_manifest(zip_path=zip_path, manifest=sha_manifest, folder_to_backup=root_path, log_callback=log_worker)

                sha12 = content_hash[:ZIP_SHA256_PREFIX_LEN]

                if self.storage_backend == "drive":
                    if drive_has_sha_dedupe_match(service, profile_folder_id, computed_sha12=sha12, log_callback=log_worker):
                        log_worker(f"[SUCCESS] Backup skipped (save contents unchanged; SHA suffix: {sha12}).\n")
                        return

                    self._update_upload_progress("Uploading backup to Drive...", 0.0)
                    file_id = drive_upload_backup_zip(
                        service,
                        profile_folder_id=profile_folder_id,
                        zip_path=zip_path,
                        manifest=sha_manifest,
                        sha256_hex=content_hash,
                        log_callback=log_worker,
                        progress_callback=self._update_upload_progress,
                    )
                    self._reset_upload_progress()
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
        for p in self.discovered_paths:
            if self._detected_save_root_name(p) == save_root:
                return p
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

            profile_name = self.selected_profile_name or save_root

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

            self.refresh_profiles_ui()
        except Exception as e:
            log_worker(f"[ERROR] Restore failed: {e}\n")

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

    def _download_backup_file(self, backup_entry: dict):
        if not backup_entry:
            return

        name = backup_entry.get("name", "backup.zip")
        dest = filedialog.asksaveasfilename(
            defaultextension=".zip",
            initialfile=name,
            filetypes=[("ZIP archive", "*.zip")],
        )
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
            os.makedirs(target_dir, exist_ok=True)

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
        confirm = messagebox.askyesno(
            "Confirm Delete",
            f"Delete backup '{name}' from the selected profile? This cannot be undone.",
        )
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

        self._append_log_text(
            f"\n[WARN] No local directory available for '{game_root}'. Please choose a target folder to restore into.\n"
        )
        chosen_dir = filedialog.askdirectory(title=f"Choose restore folder for {game_root}")
        if chosen_dir:
            self.start_restore(chosen_dir)
        else:
            self._append_log_text("\n[INFO] Restore cancelled. No target directory selected.\n")

    def delete_selected_profile_ui(self):
        self.profiles_view.delete_selected_profile_ui()

    def add_profile_ui(self):
        self.profiles_view.add_profile_ui()



if __name__ == "__main__":
    import traceback

    try:
        _debug_mark("__main__: starting")
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "save_finder.log"), "a", encoding="utf-8") as _lf:
            _lf.write(f"\n--- Start {datetime.now().isoformat()} ---\n")
        app = SaveFinderApp()
        _debug_mark("__main__: SaveFinderApp created")
        app.mainloop()
    except Exception:
        tb = traceback.format_exc()
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "save_finder.log"), "a", encoding="utf-8") as _lf:
            _lf.write(tb)
        raise
