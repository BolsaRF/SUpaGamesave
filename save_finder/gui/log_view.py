from __future__ import annotations

import queue
from datetime import datetime
from typing import Callable


def _format_timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class LogView:
    """Console log rendering + queue processing.

    This class expects the CTkTextbox + queue + autoscroll flag to be owned by
    the main app.
    """

    def __init__(self, app):
        self.app = app

    def queue_log(self, level: str, message: str):
        ts = _format_timestamp()
        # app._log_queue stores (level, text)
        self.app._log_queue.put((level, f"[{ts}] {message}"))

    def _toggle_autoscroll(self):
        self.app._log_autoscroll = bool(self.app.autoscroll_var.get())

    def process_log_queue(self):
        try:
            while True:
                level, text = self.app._log_queue.get_nowait()
                tag = f"lvl_{level}"
                color_map = {
                    "INFO": "#d0d0d0",
                    "SUCCESS": "#44dd55",
                    "WARN": "#ffcc00",
                    "ERROR": "#ff4444",
                }

                # Ensure the tag exists before querying it (prevents: TclError)
                try:
                    if not self.app.console_output.tag_cget(tag, "foreground"):
                        self.app.console_output.tag_config(
                            tag, foreground=color_map.get(level, "#d0d0d0")
                        )
                except Exception:
                    self.app.console_output.tag_config(
                        tag, foreground=color_map.get(level, "#d0d0d0")
                    )

                self.app.console_output.configure(state="normal")
                self.app.console_output.insert("end", text, tag)
                if self.app._log_autoscroll:
                    self.app.console_output.see("end")
                self.app.console_output.configure(state="disabled")
        except queue.Empty:
            pass

        self.app.after(50, self.process_log_queue)

    def append_log_text(self, text: str):
        self.app.console_output.configure(state="normal")
        self.app.console_output.insert("end", text)
        if self.app._log_autoscroll:
            self.app.console_output.see("end")
        self.app.console_output.configure(state="disabled")

    def clear_console(self):
        self.app.console_output.configure(state="normal")
        self.app.console_output.delete("1.0", "end")
        self.app.console_output.configure(state="disabled")

