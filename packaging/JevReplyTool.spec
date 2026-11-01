# -*- mode: python ; coding: utf-8 -*-
"""One-folder Windows build. No local keys, runtime DB, or merchant locators."""
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

project = Path.cwd()
clicknium_datas, clicknium_binaries, clicknium_hidden = collect_all('clicknium')
datas = clicknium_datas + [
    (str(project / 'knowledge'), 'knowledge'),
    (str(project / 'reference' / 'VISION_PROMPT.txt'), 'reference'),
    (str(project / '.locator' / 'README.md'), '.locator'),
    (str(project / 'FIRST_RUN.md'), '.'),
    (str(project / 'README.md'), '.'),
    (str(project / 'assets' / 'icons'), 'assets/icons'),
    (str(project / 'assets' / 'fonts'), 'assets/fonts'),
    (str(project / 'THIRD_PARTY_NOTICES.txt'), '.'),
    (str(project / 'assets' / 'licenses'), 'assets/licenses'),
]

a = Analysis(
    [str(project / 'app.py')], pathex=[str(project)], binaries=clicknium_binaries,
    datas=datas, hiddenimports=clicknium_hidden + ['PySide6.QtSvg'],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
# Qt 6.11 uses Windows' system ICU. Codex's Poppler PATH also has an ICU DLL
# with the same basename but version-suffixed exports; bundling it shadows the
# system DLL and makes QtCore fail with ERROR_PROC_NOT_FOUND. Never ship that
# unrelated unversioned ICU library. Windows resolves its own ICU normally.
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in
              ('icuuc.dll', 'icuin.dll', 'icu.dll', 'icudt78.dll')]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='JevReplyTool',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='JevReplyTool')
