# Fix: Google Drive backend disabled in built version

## Root cause
PyInstaller does not bundle Google's static discovery documents (`drive.v3.json`
etc.) because they are loaded via `open()` at runtime, not imported. The built
`SaveFinder/_internal/googleapiclient/discovery_cache/documents/` folder is empty,
so `build("drive", "v3")` with default `static_discovery=True` cannot find the
discovery doc and fails.

## Steps
- [x] Add discovery documents to `datas` in `SaveFinder.spec`
- [x] Add matching `--add-data` flag in `build.bat`
- [x] Rebuild the app
- [x] Verify `drive.v3.json` exists in the built output (586 documents bundled)
