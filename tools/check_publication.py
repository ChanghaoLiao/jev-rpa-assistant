"""Check tracked source boundaries and relative documentation links, offline."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
MEDIA = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.mp4', '.mov', '.avi', '.mkv', '.webm'}
PRIVATE = {'.env', 'config.local.json', 'config.local.tmp'}
GENERATED = {'__pycache__', '.git', '.venv', '.local-data', 'runtime', 'build', 'dist', 'release', 'backups', 'outputs'}


def source_files():
    proc = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True)
    if proc.returncode == 0 and proc.stdout:
        return [ROOT / name for name in proc.stdout.decode('utf-8').split('\0') if name]
    return [p for p in ROOT.rglob('*') if p.is_file() and not any(part in GENERATED for part in p.relative_to(ROOT).parts)]


def main():
    issues = []
    files = source_files()
    for path in files:
        relative = path.relative_to(ROOT)
        if path.suffix.lower() in MEDIA | {'.cnstore', '.sqlite3', '.sqlite', '.db', '.exe', '.dll', '.pfx', '.pem', '.key'}:
            issues.append(f'Forbidden publication file: {relative}')
        if path.name in PRIVATE or any(part in GENERATED for part in relative.parts):
            issues.append(f'Private/generated path: {relative}')
        if path.suffix.lower() == '.md':
            text = path.read_text(encoding='utf-8-sig')
            if re.search(r'!\[[^\]]*\]\(', text):
                issues.append(f'Image embed in text-only documentation: {relative}')
            for target in re.findall(r'(?<!!)\[[^\]]+\]\(([^\s)]+)\)', text):
                if target.startswith(('#', 'https://', 'http://', 'mailto:')):
                    continue
                target_path = unquote(target.split('#', 1)[0].strip('<>'))
                if target_path and not (path.parent / target_path).exists():
                    issues.append(f'Broken relative link: {relative} -> {target}')
    config = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8-sig'))
    for section, key in [('jev', 'api_key'), ('vision', 'api_key'), ('desktop', 'license_key')]:
        if config[section][key]:
            issues.append(f'Example credential must be empty: {section}.{key}')
    for key in ('auto_send_enabled', 'locators_reviewed', 'unread_locator_reviewed', 'data_upload_authorized'):
        if config['desktop'][key] is not False:
            issues.append(f'Example permission must be false: desktop.{key}')
    if issues:
        raise SystemExit('\n'.join(issues))
    print(f'Publication boundaries and documentation links passed: {len(files)} source files.')


if __name__ == '__main__':
    main()
