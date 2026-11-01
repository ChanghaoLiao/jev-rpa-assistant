"""One active Clicknium profile at a time; every profile owns its locator store."""
from __future__ import annotations

import copy
import uuid

PROFILE_FIELDS = ('shop_id', 'locators', 'locators_reviewed',
                  'unread_locator_reviewed', 'test_send_verified',
                  'data_upload_authorized', 'auto_send_enabled', 'unread_mode',
                  'adapter', 'unread_rule_hash')
LOCATOR_NAMES = ('new_message', 'current_user', 'chat_window',
                 'reply_text', 'button_send')


def profile_from_desktop(desktop: dict, *, label='旧配置', kind='windows') -> dict:
    profile = {'label': label, 'kind': kind}
    for key in PROFILE_FIELDS:
        default = ('generic' if key == 'adapter' else 'locator' if key == 'unread_mode' else
                   False if key.endswith(('verified', 'authorized', 'enabled', 'reviewed')) else '')
        profile[key] = copy.deepcopy(desktop.get(key, default))
    return profile


def prepare_profiles(config: dict) -> dict:
    profiles = config.setdefault('profiles', {})
    active = config.setdefault('active_profile', 'default')
    if not profiles:
        profiles['default'] = profile_from_desktop(config['desktop'])
    if active not in profiles:
        raise ValueError('当前平台配置不存在，请重新选择。')
    for profile in profiles.values():
        profile.setdefault('unread_mode', 'locator')
        profile.setdefault('adapter', 'generic')
        profile.setdefault('unread_rule_hash', '')
    for key in PROFILE_FIELDS:
        if key in profiles[active]:
            config['desktop'][key] = copy.deepcopy(profiles[active][key])
    return config


def sync_active(config: dict) -> None:
    active = config['active_profile']
    if active not in config['profiles']:
        raise ValueError('当前平台配置不存在。')
    for key in PROFILE_FIELDS:
        config['profiles'][active][key] = copy.deepcopy(config['desktop'][key])


def select_profile(config: dict, profile_id: str) -> dict:
    sync_active(config)
    if profile_id not in config['profiles']:
        raise ValueError('平台配置不存在。')
    config['active_profile'] = profile_id
    return prepare_profiles(config)


def add_profile(config: dict, label: str, account: str, kind: str) -> str:
    label, account = label.strip(), account.strip()
    if not label or not account or len(label) > 80 or len(account) > 120:
        raise ValueError('请填写平台名称和店铺/账号标识。')
    if kind not in ('web', 'windows'):
        raise ValueError('平台类型只能是网页版或 Windows 客户端。')
    sync_active(config)
    profile_id = uuid.uuid4().hex[:12]
    store = f'p_{profile_id}'
    profile = {'label': label, 'kind': kind, 'shop_id': account,
               'locators_reviewed': False, 'unread_locator_reviewed': False,
               'test_send_verified': False, 'data_upload_authorized': False,
               'auto_send_enabled': False, 'unread_mode': 'locator',
               'adapter': 'generic', 'unread_rule_hash': '',
               'locators': {name: f'{store}.{name}' for name in LOCATOR_NAMES}}
    config['profiles'][profile_id] = profile
    config['active_profile'] = profile_id
    prepare_profiles(config)
    return profile_id
