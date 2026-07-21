# SaveFinder (Universal Game Save Finder & Backup)

SaveFinder scans a game root folder to discover save locations, then backs up the latest saves to a storage backend (Google Drive when enabled, otherwise local disk). It can also restore the newest backup back into a selected target folder.

## Features

- **Scan** a game directory to detect save locations
- **Profile management** (per-game/per-root profiles)
- **Back up** detected save subfolders
- **Restore** backups (newest or selected)
- **Auto-backup polling** (checks for changes and uploads when content changes)
- **Google Drive support** (when dependencies/credentials are available) yes

## Requirements

### Python

- Python 3.10+

### Dependencies

Install dependencies from `requirements.txt`:

```bash
pip install -r requirements.txt
```

### Google Drive backend

Drive backend requires the Google API dependencies already included in `requirements.txt`.

You also need a Drive OAuth credentials file:

- `credentials.json` (project root)

## Configuration

Edit `save_finder.ini` (or use the UI) for basic settings.

Profiles are stored in:

- **Local**: under the configured local backups root (default: `backups/`)
- **Drive**: under the app folder you authorize

## Running

### GUI

Run the GUI directly:

```bash
python save_finder/gui_app.py
```

Or run the convenience launcher (if present in your setup):

```bash
python save_finder.py
```

## Credentials / Secrets

- Do **not** commit `credentials.json` or OAuth tokens to public repositories.
- If you publish this repo, add these files to `.gitignore`.

## Troubleshooting

### Tkinter callback / GUI errors

If you see an error coming from a Tkinter callback, check the console log output.

## Build / Packaging

This project may be packaged into an executable via build scripts (e.g. PyInstaller). Check `build.bat` / `rebuild.bat`.
