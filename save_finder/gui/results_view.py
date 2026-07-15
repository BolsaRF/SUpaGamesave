from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import customtkinter as ctk

if TYPE_CHECKING:  # pragma: no cover
    from ..gui_app import SaveFinderApp


@dataclass
class ResultsCallbacks:
    on_restore_clicked: callable
    on_backup_clicked: callable
    on_label_double_click_restore: callable | None = None


class ResultsView:
    def __init__(self, app: "SaveFinderApp", callbacks: ResultsCallbacks):
        self.app = app
        self.callbacks = callbacks

    def clear_results(self):
        for w in getattr(self.app, "_tree_sections", []):
            try:
                w.destroy()
            except Exception:
                pass
        self.app._tree_sections = []

    def show_empty_results(self):
        self.app.discovered_paths = []
        self.clear_results()
        self.app._update_auto_backup_checkbox_visibility()
        self.app._append_log_text("\n[INFO] No saved locations available for this profile yet.\n")

    def show_results_for_paths(self, paths: list[str]):
        self.app.discovered_paths = list(dict.fromkeys([p for p in (paths or []) if p]))
        self.clear_results()
        for p in self.app.discovered_paths:
            self._add_result_section(p)
        self.app._update_auto_backup_checkbox_visibility()

    def toggle_visibility(self):
        self.app.results_visible = not getattr(self.app, "results_visible", True)
        if self.app.results_visible:
            self.app.results_scroll.pack(fill="both", expand=True, padx=10, pady=10)
            self.app.results_toggle_btn.configure(text="Hide Results")
        else:
            self.app.results_scroll.forget()
            self.app.results_toggle_btn.configure(text="Show Results")

    def _copy_path(self, path: str):
        try:
            self.app.clipboard_clear()
            self.app.clipboard_append(path)
        except Exception:
            pass

    def _bind_dynamic_wraplength(self, label):
        # A fixed wraplength only works for a fixed-width panel. Now that
        # the results panel can be dragged narrower/wider (main_paned in
        # gui_app.py), the wrap point needs to track the label's actual
        # current width instead, or long paths clip again at narrow widths.
        def _on_configure(event, lbl=label):
            try:
                lbl.configure(wraplength=max(100, event.width - 4))
            except Exception:
                pass

        label.bind("<Configure>", _on_configure)

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

        subfolders = [
            os.path.join(root_path, e)
            for e in entries
            if os.path.isdir(os.path.join(root_path, e))
        ]
        subfolders.sort(key=lambda x: x.lower())
        if len(subfolders) > max_items:
            subfolders = subfolders[:max_items]

        if not subfolders:
            empty = ctk.CTkLabel(
                children_frame,
                text="(no subfolders)",
                anchor="w",
                font=ctk.CTkFont(size=11),
                text_color="gray",
            )
            empty.pack(fill="x", padx=30, pady=(4, 6))
            return

        for sp in subfolders:
            row = ctk.CTkFrame(children_frame, fg_color="transparent")
            row.pack(fill="x", padx=(18, 8), pady=2)

            name = os.path.basename(sp)
            lbl = ctk.CTkLabel(row, text=name, anchor="w", justify="left", font=ctk.CTkFont(size=11))
            lbl.pack(fill="x", padx=(10, 10), pady=(2, 0))
            self._bind_dynamic_wraplength(lbl)

            actions_row = ctk.CTkFrame(row, fg_color="transparent")
            actions_row.pack(fill="x", padx=(10, 0), pady=(2, 2))

            cbtn = ctk.CTkButton(actions_row, text="Copy", width=52, command=lambda p=sp: self._copy_path(p))
            cbtn.pack(side="left", padx=(0, 6))

            obtn = ctk.CTkButton(actions_row, text="Open", width=52, command=lambda p=sp: self.app._open_in_explorer(p))
            obtn.pack(side="left", padx=(0, 6))

            backup_btn = ctk.CTkButton(actions_row, text="Backup", width=70, command=lambda p=sp: self.callbacks.on_backup_clicked(p))
            backup_btn.pack(side="left", padx=(0, 6))

            restore_btn = ctk.CTkButton(actions_row, text="Restore", width=70, command=lambda p=sp: self.callbacks.on_restore_clicked(p))
            restore_btn.pack(side="left", padx=(0, 6))

    def _add_result_section(self, root_path: str):
        section = ctk.CTkFrame(self.app.results_scroll, fg_color="transparent")
        section.pack(fill="x", pady=6)

        header = ctk.CTkFrame(section, fg_color="transparent")
        header.pack(fill="x")

        # Path row: expander + full path, on its own row so it isn't
        # squeezed by the action buttons (which pushed them off-screen
        # at normal window widths). wraplength guarantees long paths
        # wrap onto extra lines instead of being clipped.
        path_row = ctk.CTkFrame(header, fg_color="transparent")
        path_row.pack(fill="x")

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
            path_row,
            text="▶",
            width=28,
            command=lambda: (toggle(), exp_btn.configure(text="▼" if not expander_state["expanded"] else "▶")),
        )
        exp_btn.pack(side="left", padx=(8, 4), pady=4)

        root_label = ctk.CTkLabel(
            path_row,
            text=root_path,
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
        )
        root_label.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=4)
        self._bind_dynamic_wraplength(root_label)

        if self.callbacks.on_label_double_click_restore:
            try:
                root_label.bind(
                    "<Double-Button-1>",
                    lambda e, p=root_path: self.callbacks.on_label_double_click_restore(p),
                )
            except Exception:
                pass

        # Actions row: buttons on their own row below the path.
        actions_row = ctk.CTkFrame(header, fg_color="transparent")
        actions_row.pack(fill="x", pady=(4, 0))

        copy_btn = ctk.CTkButton(actions_row, text="Copy", width=64, command=lambda p=root_path: self._copy_path(p))
        copy_btn.pack(side="left", padx=(36, 6))

        open_btn = ctk.CTkButton(actions_row, text="Open", width=64, command=lambda p=root_path: self.app._open_in_explorer(p))
        open_btn.pack(side="left", padx=(0, 6))

        backup_btn = ctk.CTkButton(actions_row, text="Backup", width=80, command=lambda p=root_path: self.callbacks.on_backup_clicked(p))
        backup_btn.pack(side="left", padx=(0, 6))

        restore_btn = ctk.CTkButton(actions_row, text="Restore", width=80, command=lambda p=root_path: self.callbacks.on_restore_clicked(p))
        restore_btn.pack(side="left", padx=(0, 6))

        self.app._tree_sections.append(section)

