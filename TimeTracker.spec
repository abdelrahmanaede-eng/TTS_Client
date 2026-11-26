# -*- mode: python ; coding: utf-8 -*-

import os
import re

# Java JDKs may ship copies of Windows API-set DLLs in their bin directory.
# PyInstaller can mistake those shims for app dependencies and bundle them.
os.environ["PATH"] = os.pathsep.join(
    entry
    for entry in os.environ.get("PATH", "").split(os.pathsep)
    if not re.search(r"(?:^|[\\/])jdk-[^\\/]+[\\/]bin[\\/]?$", entry, re.IGNORECASE)
)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets'), ('scripts/post_update_prompt.ps1', 'scripts')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'pandas', 'scipy', 'matplotlib'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TimeTracker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # Avoid UPX-packed binaries, which can trigger heuristic antivirus alerts.
    # This does not replace Authenticode signing or guarantee Smart App Control approval.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/app.ico'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='TimeTracker',
)
