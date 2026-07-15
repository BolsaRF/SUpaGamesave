from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

import customtkinter as ctk
from tkinter import simpledialog, messagebox

from ..hashing import parse_zip_name_for_fields
from ..storage_drive import (
    _drive_enabled,
    drive_get_credentials,
    drive_get_service,
    drive_get_or_create_app_folder,
    drive_get_or_create_profile_folder,
    drive_list_profiles,
    drive_list_profile_backups,
    drive_download_file,
)
from ..storage_local import (
    list_profiles as localfs_list_profiles,
    list_profile_backups as localfs_list_profile_backups,
    get_or_create_profile_folder as localfs_get_or_create_profile_folder,
)
from ..zip_manifest import GOOGLE_DRIVE_MANIFEST_NAME

if TYPE_CHECKING:  # pragma: no cover
    from ..gui_app import SaveFinderApp


def _pick_newest_by_modifiedTime(files: list[dict]) -> dict | None:
    if not files:
        return None

    def key_fn(x):
        return x.get("modifiedTime") or ""

    files_sorted = sorted(files, key=key_fn, reverse=True)
    return files_sorted[0]


@dataclass
class ProfilesCallbacks:
    on_restore_backup: callable
    on_download_backup: callable
    on_delete_backup: callable
    on_restore_game_root: callable
    on_open_in_explorer: callable


class ProfilesView:
    def __init__(self, app: "SaveFinderApp", callbacks: ProfilesCallbacks):
        self.app = app
        self.callbacks = callbacks

    def refresh_profiles_ui(self, prefer_local_results: bool = False):
        app = self.app
        if app._profiles_refreshing:
            return
        if app.storage_backend == "drive" and not _drive_enabled():
            self._clear_profiles_widgets()
            app.profiles_list_scroll._scrollbar.configure(height=0) if hasattr(app.profiles_list_scroll, "_scrollbar") else None
            app._append_log_text("\n[DRIVE] Profiles unavailable: Drive backend not enabled.\n")
            return

        app._profiles_refreshing = True

        def _worker():
            try:
                service = None
                app_folder_id = None
                if app.storage_backend == "drive":
                    app.after(0, lambda: app._show_upload_progress("Connecting to Drive...", 0.02))
                    app.after(0, lambda: app._append_log_text("\n[DRIVE] Connecting & fetching profiles...\n"))

                    creds = drive_get_credentials(log_callback=None)
                    app.after(0, lambda: app._show_upload_progress("Creating Drive service...", 0.10))
                    service = drive_get_service(creds, log_callback=None)

                    app.after(0, lambda: app._show_upload_progress("Ensuring app folder...", 0.20))
                    app_folder_id = drive_get_or_create_app_folder(service, log_callback=None)

                    app.after(0, lambda: app._show_upload_progress("Fetching profiles list...", 0.35))
                    profiles = drive_list_profiles(service, app_folder_id, log_callback=None)
                else:
                    profiles = localfs_list_profiles(app.local_backups_root, log_callback=None)

                profile_info = []
                for pr in profiles:
                    if app.storage_backend == "drive":
                        bks = drive_list_profile_backups(service, pr["id"], save_root=None, log_callback=None, limit=200)
                    else:
                        bks = localfs_list_profile_backups(pr["id"], save_root=None, log_callback=None, limit=200)
                    profile_info.append({"name": pr["name"], "id": pr["id"], "count": len(bks)})

                selected = app.selected_profile_name
                if not selected and profile_info:
                    non_empty = next((p for p in profile_info if p.get("count", 0) > 0), None)
                    if non_empty:
                        selected = non_empty["name"]
                        app.selected_profile_name = selected
                        app._save_selected_profile_name()
                    else:
                        selected = None

                managed: dict[str, list[dict]] = {}
                profile_result_paths: list[str] = []
                selected_profile_backups: list[dict] = []

                if selected:
                    sel = next((x for x in profile_info if x["name"] == selected), None)
                    if sel:
                        if app.storage_backend == "drive":
                            backups = drive_list_profile_backups(service, sel["id"], save_root=None, log_callback=None, limit=200)
                        else:
                            backups = localfs_list_profile_backups(sel["id"], save_root=None, log_callback=None, limit=200)
                        selected_profile_backups = backups or []

                        for b in backups:
                            parsed = parse_zip_name_for_fields(b.get("name", ""))
                            sr = parsed.get("save_root")
                            if not sr:
                                continue
                            managed.setdefault(sr, []).append(b)

                        for sr, files in managed.items():
                            newest = _pick_newest_by_modifiedTime(files)
                            if newest:
                                resolved_path = self._read_backup_manifest_path(service, newest.get("id"), fallback_save_root=sr)
                                if resolved_path:
                                    profile_result_paths.append(resolved_path)

                managed_games = []
                for sr, files in managed.items():
                    newest = _pick_newest_by_modifiedTime(files)
                    managed_games.append({"save_root": sr, "newest": newest, "count": len(files)})

                managed_games.sort(key=lambda x: x["save_root"].lower())
                profile_result_paths = list(dict.fromkeys(profile_result_paths))

                def _schedule_render():
                    to_pass = profile_result_paths
                    if prefer_local_results and not profile_result_paths:
                        to_pass = None
                    self._render_profiles_ui(profile_info, managed_games, to_pass, selected_profile_backups)
                    if app.storage_backend == "drive":
                        app._reset_upload_progress()

                app.after(0, _schedule_render)

            except Exception as e:
                err_msg = str(e)
                app.after(0, lambda: app._append_log_text(f"\n[ERROR] Profiles refresh failed: {err_msg}\n"))

            finally:
                app._profiles_refreshing = False

        threading.Thread(target=_worker, daemon=True).start()

    def _clear_profiles_widgets(self):
        app = self.app
        for w in getattr(app, "_profile_rows", []):
            try:
                w.destroy()
            except Exception:
                pass
        app._profile_rows = []

        for w in getattr(app.profiles_list_scroll, "winfo_children", lambda: [])():
            try:
                w.destroy()
            except Exception:
                pass

        for w in getattr(app.profile_backups_scroll, "winfo_children", lambda: [])():
            try:
                w.destroy()
            except Exception:
                pass

        for w in getattr(app.managed_games_scroll, "winfo_children", lambda: [])():
            try:
                w.destroy()
            except Exception:
                pass

    def _read_backup_manifest_path(self, service, backup_file_id: str, fallback_save_root: str | None = None) -> str | None:
        try:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp_path = tmp.name
            try:
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
        app = self.app
        self._clear_profiles_widgets()

        visible_profiles = profile_info or []
        app._known_profile_names = [pr["name"] for pr in visible_profiles if pr.get("name")]

        if not visible_profiles:
            label = ctk.CTkLabel(app.profiles_list_scroll, text="(no profiles found)", text_color="gray")
            label.pack(anchor="w", padx=10, pady=6)
        else:
            for pr in visible_profiles:
                name = pr["name"]
                count = pr.get("count", 0)

                row = ctk.CTkFrame(app.profiles_list_scroll, fg_color="transparent")
                row.pack(fill="x", padx=5, pady=4)

                btn = ctk.CTkButton(
                    row,
                    text=("✓ " if name == app.selected_profile_name else "") + f"{name} ({count})",
                    anchor="w",
                    command=lambda n=name: self._on_profile_selected(n),
                )
                btn.pack(fill="x", padx=5)
                app._profile_rows.append(row)

        try:
            try:
                app.profiles_hint_label.pack_forget()
                app.profiles_hint_label._is_packed = False
            except Exception:
                pass
        except Exception:
            pass

        if profile_result_paths is not None:
            if profile_result_paths:
                app._show_results_for_paths(profile_result_paths)
            else:
                if not app.discovered_paths:
                    app._show_empty_results()

        app._update_auto_backup_checkbox_visibility()

        if not selected_profile_backups:
            label = ctk.CTkLabel(app.profile_backups_scroll, text="(no backups in selected profile)", text_color="gray")
            label.pack(anchor="w", padx=10, pady=6)
        else:
            for backup in selected_profile_backups:
                name = backup.get("name", "<unknown>")
                row = ctk.CTkFrame(app.profile_backups_scroll, fg_color="transparent")
                row.pack(fill="x", padx=5, pady=4)

                lbl = ctk.CTkLabel(row, text=name, anchor="w", font=ctk.CTkFont(size=11))
                lbl.pack(side="left", fill="x", expand=True, padx=(8, 6))

                restore_btn = ctk.CTkButton(
                    row,
                    text="Restore",
                    width=80,
                    command=lambda b=backup: self.callbacks.on_restore_backup(b),
                )
                restore_btn.pack(side="right", padx=(0, 6))

                download_btn = ctk.CTkButton(
                    row,
                    text="Download",
                    width=80,
                    command=lambda b=backup: self.callbacks.on_download_backup(b),
                )
                download_btn.pack(side="right", padx=(0, 6))

                delete_btn = ctk.CTkButton(
                    row,
                    text="Delete",
                    width=80,
                    command=lambda b=backup: self.callbacks.on_delete_backup(b),
                )
                delete_btn.pack(side="right", padx=(0, 6))

                if app.storage_backend == "local":
                    open_btn = ctk.CTkButton(
                        row,
                        text="Open",
                        width=70,
                        command=lambda b=backup: self.callbacks.on_open_in_explorer(os.path.dirname(str(b.get("id", "")))),
                    )
                    open_btn.pack(side="right", padx=(0, 6))

        if not managed_games:
            label = ctk.CTkLabel(app.managed_games_scroll, text="(no managed games yet)", text_color="gray")
            label.pack(anchor="w", padx=10, pady=6)
            return

        for mg in managed_games:
            sr = mg["save_root"]
            newest = mg.get("newest")
            file_name = newest.get("name") if newest else "-"

            row = ctk.CTkFrame(app.managed_games_scroll, fg_color="transparent")
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
                command=lambda game_root=sr: self.callbacks.on_restore_game_root(game_root),
            )
            restore_btn.pack(side="right", padx=(0, 6))

            if newest:
                app._append_log_text(f"\n[DRIVE] Managed game: {sr} newest backup: {file_name}\n")

    def _on_profile_selected(self, profile_name: str):
        app = self.app
        app.selected_profile_name = profile_name
        app._save_selected_profile_name()
        self.refresh_profiles_ui()

    def delete_selected_profile_ui(self):
        app = self.app
        if not app.selected_profile_name:
            app._append_log_text("\n[ERROR] No profile selected.\n")
            return

        profile_name = str(app.selected_profile_name).strip()
        if not profile_name:
            return

        confirm = messagebox.askyesno(
            "Confirm Delete",
            f"Delete selected profile '{profile_name}'?\n\nThis will remove backups stored for this profile:\n- Local: delete the profile folder\n- Drive: delete the Drive profile folder\nThis cannot be undone.",
        )
        if not confirm:
            return

        if app.storage_backend == "local":
            profile_folder = os.path.join(app.local_backups_root, profile_name)

            def _worker_local():
                try:
                    if os.path.exists(profile_folder):
                        shutil.rmtree(profile_folder, ignore_errors=True)
                    app._append_log_text(f"\n[SUCCESS] Deleted local profile: {profile_folder}\n")
                except Exception as e:
                    err_msg = str(e)
                    app._append_log_text(f"\n[ERROR] Failed deleting local profile: {err_msg}\n")
                finally:
                    app.after(0, self.refresh_profiles_ui)

            threading.Thread(target=_worker_local, daemon=True).start()
            return

        if app.storage_backend != "drive":
            app._append_log_text("\n[ERROR] Unknown storage backend.\n")
            return

        if not app._ensure_drive_available_or_log():
            return

        def _worker_drive():
            try:
                creds = drive_get_credentials(log_callback=None)
                service = drive_get_service(creds, log_callback=None)
                app_folder_id = drive_get_or_create_app_folder(service, log_callback=None)

                # Find profile folder id by name
                q = (
                    f"'{app_folder_id}' in parents and "
                    f"mimeType = 'application/vnd.google-apps.folder' and "
                    f"name = '{profile_name}' and trashed = false"
                )
                resp = service.files().list(q=q, spaces="drive", fields="files(id)").execute()
                files = resp.get("files", [])
                if not files:
                    app.after(0, lambda: app._append_log_text(f"\n[WARN] Drive profile folder not found: {profile_name}\n"))
                    return

                profile_folder_id = str(files[0].get("id", ""))
                if not profile_folder_id:
                    app.after(0, lambda: app._append_log_text(f"\n[ERROR] Drive profile folder id missing for: {profile_name}\n"))
                    return

                # Deleting the folder should cascade-delete its contents in Drive API semantics.
                service.files().delete(fileId=profile_folder_id).execute()
                app.after(0, lambda: app._append_log_text(f"\n[SUCCESS] Deleted Drive profile: {profile_name}\n"))
            except Exception as e:
                err_msg = str(e)
                app.after(0, lambda: app._append_log_text(f"\n[ERROR] Failed deleting Drive profile: {err_msg}\n"))
            finally:
                app.after(0, self.refresh_profiles_ui)

        threading.Thread(target=_worker_drive, daemon=True).start()

    def add_profile_ui(self):
        app = self.app

        default_name = app._default_backup_profile_name(app.path_entry.get().strip() or None)
        name = simpledialog.askstring("Add Profile", "Profile name:", initialvalue=default_name)
        if not name or not str(name).strip():
            return

        app.selected_profile_name = str(name).strip()
        app._save_selected_profile_name()

        if not app._ensure_drive_available_or_log():
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
            app._queue_log(level, message)

        def _worker():
            try:
                if app.storage_backend == "drive":
                    creds = drive_get_credentials(log_callback=_log_worker)
                    service = drive_get_service(creds, log_callback=_log_worker)
                    app_folder_id = drive_get_or_create_app_folder(service, log_callback=_log_worker)
                    drive_get_or_create_profile_folder(service, app_folder_id, app.selected_profile_name, log_callback=_log_worker)
                else:
                    localfs_get_or_create_profile_folder(app.local_backups_root, app.selected_profile_name, log_callback=_log_worker)
                app.after(0, self.refresh_profiles_ui)
            except Exception as e:
                _log_worker(f"[ERROR] Add profile failed: {e}\n")

        threading.Thread(target=_worker, daemon=True).start()
