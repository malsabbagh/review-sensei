# PyInstaller one-folder specification for native release runners.
# The release workflow invokes this file with the pinned version from
# requirements.txt; no cross-compilation or runtime download is attempted.
# PyInstaller resolves Analysis script paths relative to this spec file.
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

datas = collect_data_files(
    "review_sensei",
    includes=["default_categories/*.json", "default_stages/*.json", "schemas/*.json"],
)
datas += copy_metadata("review-sensei")
datas += collect_data_files("certifi")

a = Analysis(
    ["entrypoint.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="review-sensei",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="review-sensei",
)
