"""Keep SDK locator lookup in the merchant project, including frozen applications."""
from __future__ import annotations

from pathlib import Path
import hashlib
import os
import shutil
import tempfile
from .paths import DATA_ROOT


def runtime_project(root: Path):
    """Immutable content-keyed copy: the SDK caches stores by project directory."""
    directory = root / '.locator'
    def fingerprint(base):
        digest = hashlib.sha256()
        for file in sorted((base / '.locator').rglob('*')):
            if file.is_symlink() or not file.resolve().is_relative_to((base / '.locator').resolve()):
                raise ValueError('定位器目录包含外部链接，请检查录制目录。')
            if file.is_file():
                digest.update(str(file.relative_to(base)).encode('utf-8'))
                digest.update(hashlib.sha256(file.read_bytes()).digest())
        yaml = base / 'clicknium.yaml'
        if yaml.exists():
            if yaml.is_symlink():
                raise ValueError('录制项目配置包含外部链接。')
            digest.update(yaml.read_bytes())
        return digest.hexdigest()
    signature = fingerprint(root)
    cache = root / 'cache' / 'locator-runtime'
    cache.mkdir(parents=True, exist_ok=True)
    if cache.is_symlink() or not cache.resolve().is_relative_to(root):
        raise ValueError('定位器运行缓存目录不安全。')
    # Keep recorded-image paths below Windows' legacy path limit. Full hash is
    # still verified, so even a shortened directory-name collision fails closed.
    target = cache / signature[:24]
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix='preparing-', dir=cache) as staging:
            stage = Path(staging) / 'project'
            shutil.copytree(directory, stage / '.locator')
            if (root / 'clicknium.yaml').exists():
                shutil.copy2(root / 'clicknium.yaml', stage / 'clicknium.yaml')
            if fingerprint(stage) != signature or fingerprint(root) != signature:
                raise ValueError('录制文件正在保存，请完成保存后重新检查。')
            os.replace(stage, target)
    if fingerprint(target) != signature or fingerprint(root) != signature:
        raise ValueError('定位器运行副本或录制文件发生变化，请重新检查。')
    return target


def bind_locator_project(root: Path | None = None, *, fresh=False):
    from clicknium.common.constants import _Constants
    from clicknium.common.utils import Utils

    root = (root or DATA_ROOT).resolve()
    if not (root / _Constants.LocatorFolder).is_dir():
        raise ValueError('用户录制目录不存在，请从程序打开录制项目并保存定位器。')
    if fresh:
        root = runtime_project(root)
    identifiers = {_Constants.ClickniumYaml, _Constants.LocatorFolder}
    lookup = Utils.get_project_folder
    original = getattr(lookup, '_jev_original_lookup', lookup)

    def project_folder(identifier):
        if identifier in identifiers:
            # Do not fall back to bundled examples or another merchant's parent folder.
            return str(root) if (root / identifier).exists() else ''
        return original(identifier)

    project_folder._jev_original_lookup = original
    # SDK 0.2.12 has no public project setter. Adapt its lookup only in this process;
    # never edit the vendor installation or add user files to Python's import path.
    Utils.get_project_folder = staticmethod(project_folder)
    return root


def check_locator_runtime(config: dict):
    """Load the actual SDK locator data; no browser, screenshot, or model calls."""
    from clicknium.core.service.invokerservice import LocatorService
    root = bind_locator_project()
    result = {'project': str(root), 'locations': {}, 'ok': True}
    for name, path in config['desktop']['locators'].items():
        try:
            item = LocatorService.get_locator(path.removeprefix('locator.'))
            if not item or not item.Locator:
                raise ValueError('定位器内容为空')
            result['locations'][name] = {'loaded': True}
        except Exception as error:
            result['ok'] = False
            result['locations'][name] = {'loaded': False, 'reason': str(error)}
    return result
