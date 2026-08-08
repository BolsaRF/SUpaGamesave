# -*- mode: python ; coding: utf-8 -*-

import googleapiclient.discovery_cache
from cffi import __file__ as _cffi_pkg_file
import os

# Google API client loads its static discovery documents (e.g. drive.v3.json)
# via open() at runtime, not via import — PyInstaller cannot detect them
# automatically, so without bundling them the Google Drive backend silently
# fails in the frozen build. Resolve the documents dir from the installed
# package (the spec runs with the build venv's Python).
_DISCOVERY_DOCS_DIR = googleapiclient.discovery_cache.DISCOVERY_DOC_DIR

# cffi ships a compiled extension module (_cffi_backend) that cryptography,
# google-auth, and googleapiclient import at load time. The .pyd lives
# directly in site-packages (its parent dir), not inside the cffi package
# folder. PyInstaller does not automatically bundle it here, and without it
# the whole Google Drive import chain fails (ModuleNotFoundError:
# _cffi_backend), leaving the Drive backend disabled in the frozen build.
# Collect the .pyd explicitly from site-packages.
_site_packages = os.path.dirname(os.path.dirname(_cffi_pkg_file))
_cffi_backend = os.path.join(_site_packages, "_cffi_backend*.pyd")

a = Analysis(
    ['save_finder\\gui_app.py'],
    pathex=[],
    binaries=[
        (_cffi_backend, '.'),
    ],
    datas=[
        ('credentials.json', '.'),
        ('token.json', '.'),
        (_DISCOVERY_DOCS_DIR, 'googleapiclient/discovery_cache/documents'),
    ],
    hiddenimports=[
        'cffi',
        '_cffi_backend',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SaveFinder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SaveFinder',
)
