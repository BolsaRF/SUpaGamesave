# SaveFinder refactor (split monolith) — DONE

Goal: move backend/scanner/helpers out of `save_finder.py` into a new `save_finder/` package, leaving `save_finder.py` as a thin entrypoint.

## Steps
- [x] Create `save_finder/` package with `__init__.py`
- [x] Create module stubs + partial implementations: `hashing.py`, `zip_manifest.py`, `scanner.py`, `storage_local.py`, `storage_drive.py`, `app_config.py`, `gui_app.py`

- [x] Move non-GUI constants/helpers from `save_finder.py` into `app_config.py` / `hashing.py` / `zip_manifest.py`
- [x] Move Drive backend functions into `storage_drive.py`
- [x] Move Local backend functions into `storage_local.py`
- [x] Move scan/detection functions into `scanner.py`
- [x] Move `SaveFinderApp` class into `gui_app.py` and update imports

- [x] Convert `save_finder.py` into entrypoint that imports and runs `SaveFinderApp`
- [x] Run a quick import/execution sanity check (launch script)
- [x] PyInstaller spec / build scripts already target `save_finder/gui_app.py` — no changes needed
