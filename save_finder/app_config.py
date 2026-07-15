import configparser
import os
import sys
from datetime import datetime

# App settings file
APP_SETTINGS_FILE = "save_finder.ini"
APP_SETTINGS_SECTION = "app"
APP_SETTINGS_SELECTED_PROFILE = "selected_profile"
APP_SETTINGS_STORAGE_BACKEND = "storage_backend"
APP_SETTINGS_LOCAL_ROOT = "local_backups_root"
APP_SETTINGS_AUTO_BACKUP = "auto_backup_enabled"
APP_SETTINGS_START_AT_LOGIN = "start_at_login"
APP_SETTINGS_WINDOW_GEOMETRY = "window_geometry"
APP_SETTINGS_PANEL_SPLIT = "panel_split_x"


def _get_script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def load_setting(settings_path: str, key: str, fallback: str | None = None) -> str | None:
    try:
        if not os.path.exists(settings_path):
            return fallback
        cfg = configparser.ConfigParser()
        cfg.read(settings_path, encoding="utf-8")
        value = cfg.get(APP_SETTINGS_SECTION, key, fallback=fallback or "")
        value = (value or "").strip()
        return value or fallback
    except Exception:
        return fallback


def save_setting(settings_path: str, key: str, value: str | None):
    try:
        cfg = configparser.ConfigParser()
        if os.path.exists(settings_path):
            cfg.read(settings_path, encoding="utf-8")
        if not cfg.has_section(APP_SETTINGS_SECTION):
            cfg.add_section(APP_SETTINGS_SECTION)
        cfg.set(APP_SETTINGS_SECTION, key, value or "")
        os.makedirs(os.path.dirname(settings_path) or ".", exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as f:
            cfg.write(f)
    except Exception:
        pass


def set_start_at_login(enabled: bool) -> bool:
    """Enable/disable start-at-login for current user (Windows HKCU Run).

    Returns True if operation succeeded or not applicable on this platform.
    """
    if os.name != "nt":
        return False

    try:
        import winreg as reg

        key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "SaveFinder"

        if enabled:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}"'
            else:
                # Run Python with the script path (top-level save_finder.py)
                repo_root = os.path.abspath(os.path.join(_get_script_dir(), ".."))
                script_path = os.path.join(repo_root, "save_finder.py")
                cmd = f'"{sys.executable}" "{script_path}"'

            with reg.OpenKey(reg.HKEY_CURRENT_USER, key, 0, reg.KEY_SET_VALUE) as rk:
                reg.SetValueEx(rk, app_name, 0, reg.REG_SZ, cmd)
        else:
            try:
                with reg.OpenKey(reg.HKEY_CURRENT_USER, key, 0, reg.KEY_SET_VALUE) as rk:
                    reg.DeleteValue(rk, app_name)
            except FileNotFoundError:
                pass

        return True
    except Exception:
        return False

