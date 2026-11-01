"""Presentation-only preferences never grant business acceptance or sending rights."""
from __future__ import annotations
import json
from pathlib import Path
from .paths import DATA_ROOT


class UIState:
    def __init__(self, root=DATA_ROOT):
        self.path = Path(root) / 'ui-state.json'
        self.data = {'wizard_step': 0, 'reduced_motion': False}
        try:
            self.data.update(json.loads(self.path.read_text(encoding='utf-8')))
        except (OSError, ValueError, TypeError):
            pass

    def update(self, **values):
        self.data.update(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)
