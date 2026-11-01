from __future__ import annotations
import copy
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from .paths import SOURCE_ROOT, DATA_ROOT, bootstrap_data
from .profiles import LOCATOR_NAMES, prepare_profiles, sync_active

ROOT = SOURCE_ROOT
MINIMAX_MODEL = 'MiniMax-M3'
MINIMAX_ENDPOINTS = {
    '中国站': 'https://api.minimax.cn/v1/chat/completions',
    '国际站': 'https://api.minimax.io/v1/chat/completions',
}
DEFAULTS = {
    'jev': {'endpoint': 'https://api.typesafe.ai/v1/systemone', 'model': 'jev-latest',
            'api_key': '', 'min_confidence': 0.80, 'min_probability': 0.80,
            'min_margin': 0.15, 'max_calls': 12},
    'vision': {'provider': 'minimax', 'endpoint': MINIMAX_ENDPOINTS['中国站'],
               'model': MINIMAX_MODEL, 'api_key': '',
               'dify_base_url': 'https://api.dify.ai/v1', 'dify_input_name': 'input',
               'dify_output_name': 'result', 'buyer_side': 'left'},
    'desktop': {'license_key': '', 'shop_id': 'my-test-shop',
                'poll_seconds': 2, 'settle_seconds': 1,
                'unread_mode': 'locator',
                'adapter': 'generic', 'unread_rule_hash': '',
                'locators_reviewed': False, 'unread_locator_reviewed': False,
                'data_upload_authorized': False, 'auto_send_enabled': False,
                'test_send_verified': False,
                'max_sends_per_minute': 3,
                'locators': {
                    'new_message': 'aliworkbench.new_message',
                    'current_user': 'aliworkbench.current_user',
                    'chat_window': 'aliworkbench.chat_window',
                    'reply_text': 'aliworkbench.reply_text',
                    'button_send': 'aliworkbench.button_send'}},
    'knowledge': {'replies_csv': 'knowledge/replies.csv', 'categories_json': 'knowledge/categories.json'},
    'network': {'connect_timeout': 5, 'read_timeout': 40, 'attempts': 3},
    'active_profile': 'default', 'profiles': {},
    'onboarding': {'jev_verified': False, 'vision_verified': False},
}

class ConfigError(ValueError):
    pass

def merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out

def validate_endpoint(url: str) -> str:
    p = urlparse(url)
    if not p.hostname or p.username or p.password or p.fragment:
        raise ConfigError('API 地址格式不正确；请填完整 HTTP(S) 地址，不能含用户名或密码。')
    if p.scheme != 'https' and not (p.scheme == 'http' and p.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ConfigError('非本机 API 必须使用 HTTPS，避免明文传输密钥和客户信息。')
    return url.rstrip('/')

def validate_config(c: dict) -> None:
    prepare_profiles(c)
    validate_endpoint(c['jev']['endpoint'])
    for key in ('min_confidence', 'min_probability', 'min_margin'):
        v = c['jev'][key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise ConfigError(f'jev.{key} 必须是 0 到 1 的数字。')
    if not isinstance(c['jev']['max_calls'], int) or not 1 <= c['jev']['max_calls'] <= 32:
        raise ConfigError('max_calls 必须为 1–32。')
    if c['vision']['provider'] not in ('compatible', 'dify', 'minimax'):
        raise ConfigError('识字接口只能选 minimax、compatible 或 dify。')
    if c['vision']['provider'] == 'minimax' and (
            c['vision']['endpoint'] not in MINIMAX_ENDPOINTS.values() or
            c['vision']['model'] != MINIMAX_MODEL):
        raise ConfigError('MiniMax 识字必须使用官方接口地址和 MiniMax-M3 模型。')
    if c['vision']['buyer_side'] not in ('left', 'right'):
        raise ConfigError('buyer_side 只能为 left 或 right。')
    for key in ('locators_reviewed', 'unread_locator_reviewed', 'data_upload_authorized', 'auto_send_enabled', 'test_send_verified'):
        if type(c['desktop'][key]) is not bool:
            raise ConfigError(f'desktop.{key} 必须为 true 或 false。')
    if c['desktop']['unread_mode'] not in ('locator', 'red_badge'):
        raise ConfigError('未读识别方式不合法。')
    for key, lo, hi in [('poll_seconds', 1, 600), ('settle_seconds', 0.5, 10), ('max_sends_per_minute', 1, 30)]:
        value = c['desktop'][key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
            raise ConfigError(f'desktop.{key} 必须在 {lo}–{hi} 之间。')
    for key, lo, hi in [('connect_timeout', 1, 30), ('read_timeout', 1, 120), ('attempts', 1, 5)]:
        value = c['network'][key]
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            raise ConfigError(f'network.{key} 必须是 {lo}–{hi} 的整数。')
    if not str(c['desktop']['shop_id']).strip():
        raise ConfigError('请填写店铺标识 shop_id。')
    if not isinstance(c.get('profiles'), dict) or c.get('active_profile') not in c['profiles']:
        raise ConfigError('当前平台配置不存在。')
    for profile_id, profile in c['profiles'].items():
        if not isinstance(profile_id, str) or not profile_id.replace('_', '').isalnum() or len(profile_id) > 40:
            raise ConfigError('平台配置编号不合法。')
        if not isinstance(profile, dict) or profile.get('kind') not in ('web', 'windows'):
            raise ConfigError('平台类型不合法。')
        if not str(profile.get('label', '')).strip() or not str(profile.get('shop_id', '')).strip():
            raise ConfigError('平台名称和账号标识不能为空。')
        if profile.get('unread_mode') not in ('locator', 'red_badge'):
            raise ConfigError('平台未读识别方式不合法。')
        if profile.get('adapter') not in ('generic', 'xianyu_web'):
            raise ConfigError('平台自动适配类型不合法。')
        fingerprint = profile.get('unread_rule_hash')
        if not isinstance(fingerprint, str) or (fingerprint and
                (len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint))):
            raise ConfigError('未读规则校验记录格式不正确。')
        if not isinstance(profile.get('locators'), dict) or set(profile['locators']) != set(LOCATOR_NAMES):
            raise ConfigError('平台缺少5个定位器。')
        for value in profile['locators'].values():
            if not isinstance(value, str) or not value or any(not p.isidentifier() for p in value.split('.')):
                raise ConfigError('定位器路径不合法。')
        for key in ('locators_reviewed', 'unread_locator_reviewed', 'test_send_verified',
                    'data_upload_authorized', 'auto_send_enabled'):
            if type(profile.get(key)) is not bool:
                raise ConfigError(f'平台 {key} 必须为 true 或 false。')
    for key in ('jev_verified', 'vision_verified'):
        if type(c.get('onboarding', {}).get(key)) is not bool:
            raise ConfigError(f'onboarding.{key} 必须为 true 或 false。')

def load_config(path: Path | None = None) -> dict:
    if path is None:
        bootstrap_data()
        path = DATA_ROOT / 'config.local.json'
        if not path.exists() and (ROOT / 'config.local.json').is_file():
            path.write_bytes((ROOT / 'config.local.json').read_bytes())
    c = copy.deepcopy(DEFAULTS)
    if path.exists():
        with path.open(encoding='utf-8-sig') as f:
            patch = json.load(f)
        if not isinstance(patch, dict):
            raise ConfigError('配置文件必须是 JSON 对象。')
        c = merge(c, patch)
    prepare_profiles(c)
    for group, env in [('jev', 'TYPESAFE_API_KEY'), ('vision', 'VISION_API_KEY')]:
        if os.getenv(env):
            c[group]['api_key'] = os.environ[env]
    if os.getenv('CLICKNIUM_LICENSE_KEY'):
        c['desktop']['license_key'] = os.environ['CLICKNIUM_LICENSE_KEY']
    validate_config(c)
    return c

def save_config(c: dict, path: Path | None = None) -> None:
    sync_active(c)
    validate_config(c)
    path = path or DATA_ROOT / 'config.local.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    temp.replace(path)
