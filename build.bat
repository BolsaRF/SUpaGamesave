@echo off
rem Build script for SaveFinder (Windows)
rem Creates (if needed) a venv, installs deps and runs PyInstaller in onedir mode.
setlocal
if not exist .venv (
    python -m venv .venv
)
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller

rem Build (one-folder) so the exe sits next to its data files in dist\SaveFinder\.
rem The spec bundles credentials.json, token.json, and Google's static discovery
rem documents (required for the Drive backend in the frozen build).
pyinstaller --noconfirm SaveFinder.spec

echo Build finished. See dist\SaveFinder\
endlocal
pause
