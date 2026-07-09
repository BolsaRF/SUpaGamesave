# TODO - UI improvements (log box + interactivity)

- [ ] Review `save_finder.py` current GUI structure and log mechanism.
- [ ] Add an interactive “Results” area (list of found save paths with copy/open actions).
- [ ] Improve log box UX:
  - [ ] Add log severity tags/colors (INFO/WARN/ERROR/SUCCESS).
  - [ ] Add timestamps.
  - [ ] Add “Clear” button and “Auto-scroll” toggle.
- [ ] Fix thread-safety: route backend log/result updates to Tk main thread (use `after()` or a queue).
- [ ] Add progress/phase indicator (even if approximate: [1/3], [2/3], [3/3]).
- [ ] Update backend callbacks to use new logging interface.
- [ ] Smoke test: run app, scan a folder, verify UI remains responsive and actions work.

