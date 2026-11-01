"""Source resources and writable per-user data are deliberately separate."""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

SOURCE_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
DATA_ROOT = Path(os.environ.get('JEVTOOL_DATA_DIR') or
                 (Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'JevReplyTool'))


def bootstrap_data() -> Path:
    """Copy editable examples once; never overwrite a merchant's data."""
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    for relative in ('knowledge/replies.csv', 'knowledge/categories.json',
                     'reference/VISION_PROMPT.txt', '.locator/README.md', 'FIRST_RUN.md'):
        target = DATA_ROOT / relative
        source = SOURCE_ROOT / relative
        if source.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    (DATA_ROOT / '.locator').mkdir(exist_ok=True)
    # Preserve the ledger from a previous source checkout when installing over it.
    old = SOURCE_ROOT / 'runtime/events.sqlite3'
    new = DATA_ROOT / 'profiles/default/events.sqlite3'
    if old.is_file() and not new.exists() and old.resolve() != new.resolve():
        new.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(old)) as source, sqlite3.connect(str(new)) as target:
            source.backup(target)
    return DATA_ROOT


def knowledge_file(relative: str) -> Path:
    user_file = DATA_ROOT / relative
    return user_file if user_file.exists() else SOURCE_ROOT / relative
