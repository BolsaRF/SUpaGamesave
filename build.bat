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

nrem Build (one-folder) so the exe sits next to its data files in dist\SaveFinder\
pyinstaller --noconfirm --onedir --windowed --name SaveFinder --add-data "credentials.json;." --add-data "token.json;." save_finder.py

necho Build finished. See dist\SaveFinder\
endlocal
pause
