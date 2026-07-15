@echo off
rem Quick rebuild script using existing PyInstaller spec for faster builds
rem Usage: run from repo root. Requires the venv to exist (optional: activates it).
if exist .venv\Scripts\activate (
  call .venv\Scripts\activate
)
pyinstaller --noconfirm --onedir SaveFinder.spec
echo Rebuild finished. See dist\SaveFinder\
pause
