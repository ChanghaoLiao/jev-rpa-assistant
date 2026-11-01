"""Connect saved Clicknium stores without changing captured locator rules."""
from __future__ import annotations

import copy
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .profiles import LOCATOR_NAMES, sync_active

WEB_TYPES = {'Edge', 'Chrome', 'Firefox', 'Brave', 'Vivaldi'}


class RecordingError(ValueError):
    pass


@dataclass(frozen=True)
class SavedStore:
    name: str
    locations: dict


@dataclass(frozen=True)
class RecordingCheck:
    ready: bool
    candidates: tuple[str, ...] = ()
    reason: str = ''


def locator_path(value):
    return value.removeprefix('locator.')


def saved_stores(root: Path):
    directory = root / '.locator'
    stores, errors = {}, {}
    if not directory.is_dir():
        return stores, errors
    for file in sorted(directory.glob('*.cnstore')):
        try:
            if not file.resolve().is_relative_to(directory.resolve()):
                raise ValueError('录制文件位于定位器目录之外')
            data = json.loads(file.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict) or data.get('name') != file.stem or not file.stem.isidentifier():
                raise ValueError('Store 名称与文件名不一致或名称不合法')
            locations = {}

            def collect(node, prefix=''):
                locators, children = node.get('locators', []), node.get('nodes', [])
                if not isinstance(locators, list) or not isinstance(children, list):
                    raise ValueError('定位器列表格式不正确')
                for item in locators:
                    name = item.get('name') if isinstance(item, dict) else None
                    if not isinstance(name, str) or not name.isidentifier() or not isinstance(item.get('content'), dict):
                        raise ValueError('定位器名称或内容格式不正确')
                    path = prefix + name
                    if path in locations:
                        raise ValueError('存在重名定位器')
                    locations[path] = item['content']
                for child in children:
                    name = child.get('name') if isinstance(child, dict) else None
                    if not isinstance(name, str) or not name.isidentifier():
                        raise ValueError('定位器分组名称不合法')
                    collect(child, prefix + name + '.')

            collect(data)
            stores[file.stem] = SavedStore(file.stem, locations)
        except (OSError, ValueError, TypeError, RecursionError) as error:
            errors[file.stem] = str(error)
    return stores, errors


def check_recording(config: dict, root: Path) -> RecordingCheck:
    stores, errors = saved_stores(root)
    paths = {key: locator_path(value) for key, value in config['desktop']['locators'].items()}
    missing = []
    for key, path in paths.items():
        store, _, name = path.partition('.')
        if store not in stores or name not in stores[store].locations:
            missing.append(key)
    if not missing:
        return RecordingCheck(True)
    expected = {path.partition('.')[0] for path in paths.values()}
    # Never replace a partly usable/malformed store with a different account's capture.
    for name in sorted(expected):
        if name in errors:
            return RecordingCheck(False, reason=f'录制库 {name} 无法读取：{errors[name]}。请在录制器保存后重试。')
    if len(expected) != 1 or any(name in stores for name in expected):
        return RecordingCheck(False, reason='当前录制库缺少配置指定的位置：' + '、'.join(missing) + '。请按配置名称保存；未改动现有映射。')
    claimed = {locator_path(value).partition('.')[0]
               for key, profile in config['profiles'].items() if key != config['active_profile']
               for value in profile['locators'].values()}
    kind = config['profiles'][config['active_profile']]['kind']
    complete, candidates = [], []
    for store in stores.values():
        if not all(name in store.locations for name in LOCATOR_NAMES):
            continue
        types = {store.locations[name].get('automationType') for name in LOCATOR_NAMES}
        if kind == 'web' and not types.issubset(WEB_TYPES):
            continue
        if kind == 'windows' and types.intersection(WEB_TYPES):
            continue
        complete.append(store.name)
        if store.name not in claimed:
            candidates.append(store.name)
    if candidates:
        return RecordingCheck(False, tuple(candidates), '配置指定的录制库不存在，找到可接入的完整录制库。')
    if complete:
        reason = '完整录制库已被其他平台配置使用。请为当前账号单独保存录制库，避免串用。'
    else:
        reason = '未找到当前平台可接入的完整录制库。请保存到本程序的 .locator 目录，确认同一个 Store 内包含：' + '、'.join(LOCATOR_NAMES) + '。'
        if errors:
            reason += ' 无法读取的库：' + '、'.join(errors) + '。'
    return RecordingCheck(False, reason=reason)


def bind_recording(config: dict, root: Path, name: str) -> dict:
    checked = check_recording(config, root)
    if checked.ready:
        return copy.deepcopy(config)
    if name not in checked.candidates:
        raise RecordingError('录制库状态已变化或不能用于当前平台，请重新检查。')
    result = copy.deepcopy(config)
    result['desktop']['locators'] = {key: f'{name}.{key}' for key in LOCATOR_NAMES}
    result['desktop']['adapter'] = 'generic'
    result['desktop']['unread_rule_hash'] = ''
    for field in ('locators_reviewed', 'unread_locator_reviewed', 'test_send_verified', 'auto_send_enabled'):
        result['desktop'][field] = False
    sync_active(result)
    return result


def backup_recording_config(root: Path):
    source = root / 'config.local.json'
    if not source.exists():
        return None
    destination = root / 'backups' / ('recording-connect-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    destination.mkdir(parents=True, exist_ok=False)
    target = destination / source.name
    shutil.copy2(source, target)
    return target
