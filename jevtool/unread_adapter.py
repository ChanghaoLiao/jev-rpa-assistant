"""Backed-up, page-checked Xianyu/Edge unread locator preparation."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .audit import record, exception_details
from .network import Cancelled
from .paths import DATA_ROOT
from .profiles import sync_active
from .recording import RecordingError, locator_path, saved_stores

SELECTOR = 'aside [class*="conversation-item--"]:has(.ant-badge-count)'
ADAPTER = 'xianyu_web'
HOSTS = {'www.goofish.com', 'goofish.com'}


def is_chat_url(url):
    value = urlparse(str(url))
    return value.scheme == 'https' and value.hostname in HOSTS and (
        value.path == '/im' or value.path.startswith('/im/'))


def tab_node(content):
    controls = content.get('childControls', [])
    if len(controls) != 1 or controls[0].get('identifier', {}).get('controlMetaType') != 'Tab':
        raise RecordingError('录制的浏览器标签页结构不明确，请重录未读客户行。')
    return controls[0]


def recorded_adapter(desktop, root=DATA_ROOT):
    """Identify the recorded application, never the chosen Store name/label."""
    stores, _ = saved_stores(Path(root))
    for path in desktop.get('locators', {}).values():
        store, _, location = locator_path(path).partition('.')
        content = stores.get(store).locations.get(location) if store in stores else None
        if not content or content.get('automationType') != 'Edge':
            return 'generic'
        try:
            if not is_chat_url(tab_node(content)['identifier'].get('url', {}).get('value', '')):
                return 'generic'
        except (RecordingError, AttributeError, TypeError):
            return 'generic'
    return ADAPTER if len(desktop.get('locators', {})) == 5 else 'generic'


def is_xianyu(desktop, root=DATA_ROOT):
    return desktop.get('adapter') == ADAPTER or recorded_adapter(desktop, root) == ADAPTER


def find_location(data, path):
    parts = path.split('.')
    node = data
    for part in parts[:-1]:
        values = [n for n in node.get('nodes', []) if n.get('name') == part]
        if len(values) != 1:
            raise RecordingError('录制分组不明确。')
        node = values[0]
    values = [n for n in node.get('locators', []) if n.get('name') == parts[-1]]
    if len(values) != 1:
        raise RecordingError('未读定位器缺失或重名。')
    return values[0]


def normalize_content(content):
    result = copy.deepcopy(content)
    tab = tab_node(result)
    controls = tab.get('childControls', [])
    while len(controls) == 1 and controls[0].get('childControls'):
        controls = controls[0]['childControls']
    if len(controls) != 1 or controls[0].get('identifier', {}).get('controlMetaType') != 'Web':
        raise RecordingError('请录制带红点的完整客户行；当前不是可适配的网页元素。')
    leaf = controls[0]
    ident = leaf['identifier']
    if ident.get('tag', {}).get('value', '').upper() in ('ASIDE', 'BODY', 'HTML'):
        raise RecordingError('录到了整个列表或页面，请重录带红点的完整客户行。')
    # Keep the complete Application/Tab constraints. Only normalize the web target.
    for name, attribute in ident.items():
        if name not in ('tag', 'ancestorId', 'cssSelector') and isinstance(attribute, dict):
            attribute['excluded'] = True
    for name, value in dict(tag='DIV', ancestorId='conv-list-scrollable', cssSelector=SELECTOR).items():
        attribute = ident.setdefault(name, {})
        attribute['value'] = value
        if attribute.get('excluded') is True:
            attribute['excluded'] = False
        if attribute.get('operator', 'equals') != 'equals':
            attribute['operator'] = 'equals'
    leaf['excluded'] = False
    tab['childControls'] = [leaf]
    return result


def rule_hash(content):
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


PAGE_SCRIPT = '''function unreadPageState(){
const host=location.hostname,path=location.pathname;
if(!['www.goofish.com','goofish.com'].includes(host)||!(path==='/im'||path.startsWith('/im/')))
throw new Error('Wrong platform URL');
const lists=document.querySelectorAll('#conv-list-scrollable');
if(lists.length!==1||!lists[0].closest('aside'))throw new Error('Conversation list missing or ambiguous');
const list=lists[0],rect=list.getBoundingClientRect(),style=getComputedStyle(list);
if(rect.width<=0||rect.height<=0||style.display==='none'||style.visibility==='hidden')
throw new Error('Conversation list is not visible');
const rows=Array.from(list.querySelectorAll('[class*="conversation-item--"]'));
const unread=rows.map((r,i)=>{const b=r.querySelector('.ant-badge-count');
if(!b)return -1;const s=getComputedStyle(b),p=b.getBoundingClientRect();
return s.display!=='none'&&s.visibility!=='hidden'&&p.width>0&&p.height>0&&b.textContent.trim()!=='0'?i:-1;
}).filter(i=>i>=0);
return JSON.stringify({host,path,total_rows:rows.length,unread_indices:unread});
}'''

ROW_SCRIPT = '''function unreadRowState(){
const e=_context$.currentElement,lists=document.querySelectorAll('#conv-list-scrollable');
if(lists.length!==1)return JSON.stringify({belongs:false,unread:false});
const list=lists[0],rows=Array.from(list.querySelectorAll('[class*="conversation-item--"]')),
i=rows.indexOf(e),b=e.querySelector('.ant-badge-count');
const s=b?getComputedStyle(b):null,p=b?b.getBoundingClientRect():null;
return JSON.stringify({host:location.hostname,path:location.pathname,row_index:i,
belongs:i>=0&&e.closest('#conv-list-scrollable')===list&&!!e.closest('aside'),
unread:!!b&&s.display!=='none'&&s.visibility!=='hidden'&&p.width>0&&p.height>0&&b.textContent.trim()!=='0'});
}'''


def checked_row(value):
    return (isinstance(value, dict) and value.get('belongs') is True and
            value.get('unread') is True and value.get('host') in HOSTS and
            (value.get('path') == '/im' or str(value.get('path', '')).startswith('/im/')) and
            type(value.get('row_index')) is int and value['row_index'] >= 0)


def probe_unread(desktop):
    """Read from the unique recorded header; do not click, scroll, or screenshot."""
    desktop.check_stop()
    anchor = desktop.resolve('current_user')
    # Uniqueness across tabs is required before attaching to one of them.
    if len(desktop.cc.find_elements(anchor, timeout=2)) != 1:
        raise RecordingError('闲鱼标签页或客户标题不唯一，请仅保留当前测试聊天页。')
    tab = desktop.cc.edge.attach(anchor, is_maximize=False, timeout=3)
    headers = tab.find_elements(anchor, timeout=2)
    if len(headers) != 1:
        raise RecordingError('请先打开一条闲鱼会话，客户标题必须唯一可读取。')
    before = json.loads(headers[0].execute_js(PAGE_SCRIPT, method='unreadPageState()', timeout=3))
    rows = tab.find_elements(desktop.resolve('new_message'), timeout=2)
    indices = []
    for row in rows:
        desktop.check_stop()
        state = json.loads(row.execute_js(ROW_SCRIPT, method='unreadRowState()', timeout=2))
        if not checked_row(state):
            raise RecordingError('未读规则匹配到了普通客户、列表头或其他页面；未启用。')
        indices.append(state['row_index'])
    after = json.loads(headers[0].execute_js(PAGE_SCRIPT, method='unreadPageState()', timeout=3))
    desktop.check_stop()
    if before != after:
        raise RecordingError('检查期间未读列表发生变化，请重试；未改录制文件。')
    if sorted(indices) != sorted(before['unread_indices']) or len(indices) != len(set(indices)):
        raise RecordingError('定位器匹配与网页未读行不一致，请检查录制的标签页约束。')
    return {'matched_count': len(rows), 'page': before, 'rows': rows}


@contextmanager
def locator_project(root, *, fresh=False):
    from clicknium.common.utils import Utils
    from .clicknium_project import bind_locator_project
    previous = Utils.get_project_folder
    bind_locator_project(root, fresh=fresh)
    try:
        yield
    finally:
        Utils.get_project_folder = staticmethod(previous)


def sdk_validate(config, root, stop):
    from clicknium.core.service.invokerservice import LocatorService
    from .desktop import ClickniumDesktop
    with locator_project(root):
        for path in config['desktop']['locators'].values():
            if not LocatorService.get_locator(locator_path(path)).Locator:
                raise RecordingError('Clicknium 无法加载整理后的定位器。')
        desktop = ClickniumDesktop(config['desktop'], stop, allow_unreviewed=True, project_root=root)
        result = probe_unread(desktop)
        digest = hashlib.sha256(str(LocatorService.get_locator(
            locator_path(config['desktop']['locators']['new_message'])).Locator).encode()).hexdigest()
        return {'matched_count': result['matched_count'], 'page': result['page'], 'sdk_locator_hash': digest}


@dataclass
class PreparedRecording:
    config: dict
    status: str
    reason: str
    changed: bool = False
    evidence: dict | None = None


def prepare_recording(config, root, stop, *, validator=sdk_validate):
    from .config import save_config
    root = Path(root).resolve()
    if stop.is_set():
        raise Cancelled('已取消整理录制规则。')
    adapter = recorded_adapter(config['desktop'], root)
    if adapter != ADAPTER or config['desktop'].get('unread_mode', 'locator') != 'locator':
        if config['desktop'].get('adapter') == ADAPTER and adapter != ADAPTER:
            raise RecordingError('当前录制文件不再属于闲鱼 Edge，请重新验证平台；未改规则。')
        return PreparedRecording(config, 'manual', '当前平台或识别方式尚未支持自动整理；保留原规则，使用原有人工验证。')
    store, _, location = locator_path(config['desktop']['locators']['new_message']).partition('.')
    for profile, values in config['profiles'].items():
        if profile != config['active_profile'] and any(
                locator_path(v).partition('.')[0] == store for v in values['locators'].values()):
            raise RecordingError('此录制库还被其他账号使用，请为当前账号单独录制；未改共享文件。')
    source = root / '.locator' / (store + '.cnstore')
    if source.is_symlink() or not source.resolve().is_relative_to((root / '.locator').resolve()):
        raise RecordingError('录制文件位于用户定位器目录之外。')
    original = source.read_bytes()
    original_config = (root / 'config.local.json').read_bytes() if (root / 'config.local.json').exists() else None
    data = json.loads(original.decode('utf-8-sig'))
    target = find_location(data, location)
    before = copy.deepcopy(target['content'])
    target['content'] = normalize_content(before)
    changed = before != target['content']
    result = copy.deepcopy(config)
    result['desktop']['adapter'] = ADAPTER
    result['desktop']['unread_rule_hash'] = rule_hash(target['content'])
    if changed:
        for key in ('unread_locator_reviewed', 'test_send_verified', 'auto_send_enabled'):
            result['desktop'][key] = False
    sync_active(result)
    backup = root / 'backups' / ('unread-adapt-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, exist_ok=False)
    (backup / source.name).write_bytes(original)
    if original_config is not None:
        (backup / 'config.local.json').write_bytes(original_config)
    stage = backup / 'check-project'
    directory = root / '.locator'
    for item in directory.rglob('*'):
        if item.is_symlink() or not item.resolve().is_relative_to(directory.resolve()):
            raise RecordingError('定位器目录含外部链接，未整理规则。')
    shutil.copytree(directory, stage / '.locator')
    candidate = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8') if changed else original
    (stage / '.locator' / source.name).write_bytes(candidate)
    record('recording.adapt.intent', details={'store':store,'backup':str(backup),'changed':changed,
           'before':before,'after':target['content'],'adapter':ADAPTER})
    committed = config_saved = False
    temp = source.with_name(source.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        evidence = validator(result, stage, stop)
        if stop.is_set():
            raise Cancelled('检查已停止；未提交整理结果。')
        if source.read_bytes() != original or ((root / 'config.local.json').read_bytes()
                if (root / 'config.local.json').exists() else None) != original_config:
            raise RecordingError('检查期间录制文件或配置被重新保存，请重试；未覆盖新内容。')
        if changed:
            temp.write_bytes(candidate)
            record('recording.adapt.commit_intent', details={'store':store,'backup':str(backup)})
            if stop.is_set():
                raise Cancelled('已停止；未提交整理结果。')
            os.replace(temp, source)
            committed = True
            # Check that the live SDK reads the new file, rather than cached content.
            if evidence.get('sdk_locator_hash'):
                from clicknium.core.service.invokerservice import LocatorService
                with locator_project(root, fresh=True):
                    actual = hashlib.sha256(str(LocatorService.get_locator(
                        locator_path(result['desktop']['locators']['new_message'])).Locator).encode()).hexdigest()
                if actual != evidence['sdk_locator_hash']:
                    raise RecordingError('Clicknium 未加载最新规则；已恢复录制备份，请重新打开程序检查。')
        if result != config:
            save_config(result, root / 'config.local.json')
            config_saved = True
        if stop.is_set():
            raise Cancelled('已停止；恢复未完成的适配操作。')
        reason = ('规则已整理，页面匹配通过。' if changed else '规则无需修改，页面匹配通过。') + (
            '当前没有未读，等待测试消息完成未读变化验收。' if evidence['matched_count'] == 0 else
            f"匹配 {evidence['matched_count']} 条未读客户行；仍需完成未读变化验收。")
        record('recording.adapt.completed', 'success', {'changed':changed,'backup':str(backup),'checks':evidence})
        return PreparedRecording(result, 'prepared', reason, changed, evidence)
    except Exception as error:
        rollback = []
        if committed:
            if source.read_bytes() == candidate:
                temp.write_bytes(original)
                os.replace(temp, source)
                rollback.append('locator_restored')
            else:
                rollback.append('newer_locator_preserved')
        if config_saved:
            # save_config has a deterministic representation; preserve external edits.
            expected = json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8')
            current_path = root / 'config.local.json'
            if current_path.read_bytes() == expected:
                if original_config is None:
                    current_path.unlink()
                else:
                    config_temp = current_path.with_suffix('.restore.tmp')
                    config_temp.write_bytes(original_config)
                    os.replace(config_temp, current_path)
                rollback.append('config_restored')
        record('recording.adapt.failed', 'cancelled' if isinstance(error, Cancelled) else 'failed',
               {**exception_details(error),'backup':str(backup),'rollback':rollback})
        raise
    finally:
        temp.unlink(missing_ok=True)
