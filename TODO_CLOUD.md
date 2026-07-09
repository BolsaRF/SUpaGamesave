# TODO - Google Drive ZIP backup/restore

- [ ] Add UI controls: Backup to Google Drive, Restore from Drive, per-result actions (Copy/Open remain).
- [ ] Add backend layer for Google Drive:
  - [ ] OAuth login (Google API)
  - [ ] Upload ZIP (resumable where possible)
  - [ ] List/search backups under an app folder
  - [ ] Download ZIP for restore
- [ ] ZIP format (Option B):
  - [ ] zip contains: manifest.json + contents/* (folder contents only)
  - [ ] manifest includes: original_save_path (best-effort), timestamp, game_root, and relative restore target hints
- [ ] Restore flow:
  - [ ] download ZIP
  - [ ] extract to temp
  - [ ] locate target directory (prefer detected result path; fallback to manifest/choose)
  - [ ] copy contents/* into target, with overwrite-safe behavior (skip or backup collisions)
- [ ] Thread-safe UI updates using existing queue log.
- [ ] Smoke test: backup one folder, then restore and verify files.

