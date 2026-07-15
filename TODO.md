# TODO

- [ ] Add Drive profile-loading progress indicators (logs + existing progress bar)
  - [ ] Show progress bar when `refresh_profiles_ui()` runs with Drive backend
  - [ ] Update progress by phases: Connecting → Fetching app folder → Fetching profiles → Counting backups → Rendering UI
  - [ ] Hide progress bar when finished or on error
  - [ ] Ensure UI updates via Tk `after()` and does not break existing upload progress

