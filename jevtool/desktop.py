"""Clicknium UIA adapter. Clipboard paste/click flow adapted from qianniu_bot.py.
No official 1688/千牛 message API is used. Calibrate locators on the actual machine.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageGrab
from .network import Cancelled
from .audit import record, traced, exception_details
from .web_messages import MESSAGE_SCRIPT, MessageState, parse_messages, compare_reply
from .unread import RowBounds, find_red_badges, match_red_badges
from .vision import image_digest, normalize
from .unread_adapter import is_xianyu, probe_unread, ROW_SCRIPT, checked_row, rule_hash
from .recording import saved_stores, locator_path
from .paths import DATA_ROOT

class DesktopError(RuntimeError):
    pass

CHAT_SCROLL_SCRIPT = '''function chatScrollState(){
const containers=document.querySelectorAll('#message-list-scrollable');
if(containers.length!==1)throw new Error('Chat scroll container missing or ambiguous');
const e=containers[0],a=_context$.currentElement.getBoundingClientRect();
const s=getComputedStyle(e),r=e.getBoundingClientRect();
return JSON.stringify({top:e.scrollTop,height:e.clientHeight,full:e.scrollHeight,
direction:s.flexDirection,display:s.display,overflow:s.overflowY,
capture_bounds:[r.left,r.top,r.right,r.bottom],anchor_bounds:[a.left,a.top,a.right,a.bottom],
visible:r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none',
content:JSON.stringify({text:e.innerText,images:Array.from(e.querySelectorAll('img')).map(i=>({src:i.currentSrc||i.src,alt:i.alt})),
media:Array.from(e.querySelectorAll('video,audio')).map(i=>i.currentSrc||i.src)})});
}'''

CHAT_RETURN_LATEST_SCRIPT = '''function returnChatLatest(){
const containers=document.querySelectorAll('#message-list-scrollable');
if(containers.length!==1)throw new Error('Chat scroll container missing or ambiguous');
const e=containers[0];
if(!e.isConnected)
throw new Error('Chat scroll container missing or ambiguous');
const s=getComputedStyle(e),r=e.getBoundingClientRect();
if(s.display!=='flex'||s.flexDirection!=='column-reverse'||
!['auto','scroll'].includes(s.overflowY)||r.width<=0||r.height<=0||s.visibility==='hidden')
throw new Error('Unsupported chat scroll layout');
e.scrollTop=0;
return true;
}'''

def chat_is_latest(state):
    if not isinstance(state, dict) or state.get('visible') is not True:
        raise DesktopError('无法读取可见聊天区的滚动状态。')
    values = [state.get(k) for k in ('top', 'height', 'full')]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise DesktopError('聊天区滚动数据异常。')
    top, height, full = values
    if height <= 0 or full < height or state.get('overflow') not in ('auto', 'scroll'):
        raise DesktopError('聊天滚动容器尺寸或结构异常。')
    if full - height <= 2:
        return True
    if state.get('display') == 'flex' and state.get('direction') == 'column-reverse':
        return abs(top) <= 2
    # Only support layouts whose latest end has been established.
    raise DesktopError('闲鱼聊天滚动布局已变化，需重新核对最新位置。')

def expanded_capture_bounds(chat, input_box, screen):
    """Expand half a viewport, then extend the resulting bottom by 25%."""
    left, top, right, bottom = chat
    if right <= left or bottom <= top:
        raise DesktopError('聊天截图区域尺寸异常。')
    margin = (bottom - top + 1) // 2
    left, right = max(left, screen[0]), min(right, screen[2])
    top, bottom = max(top - margin, screen[1]), min(bottom + margin, screen[3])
    if input_box[0] < right and input_box[2] > left:
        bottom = min(bottom, input_box[1])
    if right <= left or bottom <= top:
        raise DesktopError('聊天截图区域不可见或与输入框重叠。')
    extra = (bottom - top + 3) // 4
    limit = screen[3]
    if input_box[0] < right and input_box[2] > left:
        limit = min(limit, input_box[1])
    bottom = min(bottom + extra, limit)
    return left, top, right, bottom

def physical_chat_bounds(state, anchor):
    """Map CSS viewport to SDK screen coordinates without touching the tall chat."""
    chat, css = state.get('capture_bounds'), state.get('anchor_bounds')
    for rect in (chat, css, anchor):
        if not isinstance(rect, (list, tuple)) or len(rect)!=4 or any(
                type(v) not in (int,float) or not math.isfinite(v) for v in rect):
            raise DesktopError('聊天截图坐标不可读取，未截图。')
        if rect[2]<=rect[0] or rect[3]<=rect[1]:
            raise DesktopError('聊天截图或标题位置不可见，未截图。')
    sx=(anchor[2]-anchor[0])/(css[2]-css[0]); sy=(anchor[3]-anchor[1])/(css[3]-css[1])
    if not .5<=sx<=4 or not .5<=sy<=4 or abs(sx-sy)>max(sx,sy)*.08:
        raise DesktopError('聊天截图缩放比例异常，未截图。')
    # The wider title gives a less noisy scale than its short, rounded text height.
    return tuple(round(anchor[i%2]+(chat[i]-css[i%2])*sx) for i in range(4))


@dataclass(frozen=True)
class Snapshot:
    customer: str
    png: bytes
    digest: str
    latest_position_verified: bool | None = None
    position_evidence: dict | None = None


@dataclass(frozen=True)
class UnreadScan:
    png: bytes
    position: tuple[int, int, int, int]
    rows: tuple[object, ...]
    matches: tuple[int, ...]


class ClickniumDesktop:
    def __init__(self, cfg: dict, stop: threading.Event, *, allow_unreviewed: bool = False, progress=None, project_root=None):
        if sys.platform != 'win32':
            raise DesktopError('桌面监测/发送只支持 Windows；文字测试可在其他系统运行。')
        if not cfg['locators_reviewed'] and not allow_unreviewed:
            raise DesktopError('请先在实际客户端录制并验证 .locator，然后勾选“定位器已实机校准”。')
        try:
            from clicknium import clicknium as cc, locator, ui
            import pyperclip
        except ImportError:
            raise DesktopError('缺少 Clicknium/pyperclip，请先运行 setup_windows.cmd。') from None
        from .clicknium_project import bind_locator_project
        self.project_root = Path(project_root or DATA_ROOT).resolve()
        runtime_root = bind_locator_project(self.project_root, fresh=cfg.get('adapter') == 'xianyu_web')
        record('desktop.locator_project', details={'project': str(runtime_root), 'source':str(self.project_root)})
        self.cc, self.locator, self.ui, self.clipboard = cc, locator, ui, pyperclip
        self.cfg, self.stop = cfg, stop
        self.progress = progress
        self.round_robin = 0
        if cfg.get('license_key'):
            cc.config.set_license(cfg['license_key'])

    def check_stop(self):
        if self.stop.is_set():
            raise Cancelled('操作已停止。')

    def resolve(self, name):
        obj = self.locator
        path = self.cfg['locators'][name]
        record('desktop.locator', details={'name':name, 'path':path})
        if path.startswith('locator.'):
            path = path[len('locator.'):]
        for part in path.split('.'):
            if not part or part.startswith('_'):
                raise DesktopError('定位器路径不合法。')
            obj = getattr(obj, part)
        return obj

    def element(self, name):
        self.check_stop()
        loc = self.resolve(name)
        items = self.cc.find_elements(loc, timeout=2)
        if len(items) != 1:
            raise DesktopError(f'{name} 匹配了 {len(items)} 个控件，要求恰好1个。请关闭其他店铺窗口或重录。')
        return items[0]

    def customer(self):
        value = self.element('current_user').get_text(timeout=2).strip()
        if len(value) < 2 or len(value) > 200 or value in ('[', ']', '客户', '未知', '接待中心'):
            raise DesktopError('不能可靠读取当前客户名称；不会生成临时客户ID。')
        return value

    @traced('desktop.capture')
    def read_current(self, *, allow_unknown_latest=False, prepare_latest=False) -> Snapshot:
        self.check_stop()
        before = self.customer()
        self.last_auto_scroll = False
        if prepare_latest and self.is_xianyu():
            self.last_auto_scroll = self.return_to_latest(before, allow_unknown_latest=allow_unknown_latest)
        fd, name = tempfile.mkstemp(suffix='.png', prefix='jev_capture_')
        os.close(fd)
        path = Path(name)
        position_before = position_after = None
        try:
            chat = self.element('chat_window')
            # Keep other platform adapters unchanged.
            if self.is_xianyu():
                def position():
                    if not allow_unknown_latest:
                        return self.chat_position()
                    try:
                        return self.chat_position(require_latest=False)
                    except DesktopError as error:
                        return {'error': str(error)}
                position_before = position()
                import ctypes
                def rect(element):
                    p = element.get_position(timeout=2)
                    return p.Left, p.Top, p.Right, p.Bottom
                metrics = ctypes.windll.user32.GetSystemMetrics
                x, y = metrics(76), metrics(77)
                screen = (x, y, x + metrics(78), y + metrics(79))
                if allow_unknown_latest and 'capture_bounds' not in position_before:
                    # An explicitly non-sendable unknown-position preview retains
                    # the previous locator-based screenshot fallback.
                    chat_bounds = rect(chat)
                    record('capture.geometry_fallback', 'preview_only', {'position':position_before}, customer=before)
                else:
                    chat_bounds = physical_chat_bounds(position_before, rect(self.element('current_user')))
                bounds = expanded_capture_bounds(chat_bounds, rect(self.element('reply_text')), screen)
                self.check_stop()
                ImageGrab.grab(bbox=bounds, all_screens=True).save(str(path), format='PNG')
                position_after = position()
                if position_before != position_after:
                    fields = [k for k in set(position_before) | set(position_after) if position_before.get(k) != position_after.get(k)]
                    error = DesktopError('截图期间状态发生变化：' + '、'.join(fields) + '；请重新读取。')
                    error.evidence = {'stage': 'capture.stability', 'before': position_before, 'after': position_after, 'changed_fields': fields}
                    raise error
            else:
                chat.save_to_image(str(path))
            png = path.read_bytes()
            record('capture.stability', details={'before': position_before, 'after': position_after}, customer=before, png=png)
        except Exception as error:
            if not hasattr(error, 'evidence'):
                error.evidence = {'stage': 'capture', 'before': position_before, 'after': position_after}
            record('capture.evidence', 'failed', exception_details(error), customer=before, png=path.read_bytes() if path.exists() and path.stat().st_size else None)
            raise
        finally:
            path.unlink(missing_ok=True)
        after = self.customer()
        self.check_stop()
        if before != after:
            error = DesktopError('截图时会话发生切换，已取消。')
            error.evidence = {'before_customer': before, 'after_customer': after}
            record('capture.identity', 'failed', error.evidence, customer=before, png=png)
            raise error
        record('capture.identity', 'passed', {'before_customer': before, 'after_customer': after}, customer=before)
        latest = None
        if position_after is not None:
            try:
                latest = chat_is_latest(position_after)
            except DesktopError:
                latest = None
        return Snapshot(before, png, image_digest(png),
                        latest,
                        {'before': position_before, 'after': position_after} if position_after is not None else None)

    def observe_current(self):
        """Cheap Clicknium DOM probe: no screenshot or model request."""
        if not self.is_xianyu():
            return None
        before = self.customer()
        state = self.chat_position(require_latest=False)
        content = state.get('content')
        if not isinstance(content, str) or not content:
            raise DesktopError('聊天区消息摘要不可读取，已停止监测。')
        draft = bool(normalize(self.element('reply_text').get_text(timeout=2)))
        if self.customer() != before:
            raise DesktopError('观察期间客户发生切换，已取消本次检查。')
        try:
            latest = chat_is_latest(state)
        except DesktopError:
            latest = False
        return {'customer': before, 'content_key': hashlib.sha256(content.encode()).hexdigest(),
                'latest': latest, 'draft': draft}

    def open_watched_customer(self, name):
        """Revisit a previously observed follow-up; never use fuzzy nickname matches."""
        if self.customer() == name:
            return True
        loc = self.resolve('chat_window')
        tab = self.cc.edge.attach(loc, is_maximize=False, timeout=3)
        element = tab.find_element(loc)
        # The ant-dropdown conversation rows were inspected on the current Xianyu page.
        script = """function revisit(){
          const name=NAME_JSON;
          const rows=Array.from(document.querySelectorAll('aside .ant-dropdown-trigger'));
          const matches=rows.filter(r=>Array.from(r.querySelectorAll('*')).some(
            n=>n.children.length===0&&n.innerText&&n.innerText.trim()===name));
          if(matches.length!==1)return false;
          matches[0].click();return true;
        }""".replace('NAME_JSON', json.dumps(name, ensure_ascii=True))
        self.check_stop()
        if element.execute_js(script, method='revisit()', timeout=3) not in (True, 'true'):
            return False
        if self.stop.wait(self.cfg['settle_seconds']):
            raise Cancelled('已停止。')
        return self.customer() == name

    def chat_position(self, *, require_latest=True):
        self.check_stop()
        try:
            # The SDK brings tall script targets into view. Use the recorded header
            # so reading a scroll state does not itself move the chat into history.
            loc = self.resolve('current_user')
            tab = self.cc.edge.attach(loc, is_maximize=False, timeout=3)
            element = tab.find_element(loc)
            state = json.loads(element.execute_js(CHAT_SCROLL_SCRIPT, method='chatScrollState()', timeout=3))
        except Exception as error:
            self.check_stop()
            raise DesktopError('无法读取闲鱼聊天滚动状态，请检查浏览器扩展和聊天窗口。') from error
        self.check_stop()
        if require_latest and not chat_is_latest(state):
            error = DesktopError('程序检查：聊天尚未到最新消息位置，请重新读取当前会话；未调用识字模型。')
            error.evidence = {'stage': 'position.latest', 'state': state}
            raise error
        return state

    @traced('desktop.return_latest')
    def return_to_latest(self, expected_customer, *, allow_unknown_latest=False):
        """Prepare a new read; never called by existing-candidate send validation."""
        self.check_stop()
        if not self.is_xianyu():
            return False
        if self.customer() != expected_customer:
            raise DesktopError('自动回到最新消息前客户已切换，已取消。')
        try:
            before = self.chat_position(require_latest=False)
            latest = chat_is_latest(before)
        except DesktopError as error:
            record('scroll.latest.unavailable', 'skipped', exception_details(error), customer=expected_customer)
            if allow_unknown_latest:
                return False  # Preserve explicitly non-sendable previews of unknown positions.
            raise
        if latest:
            return False
        draft = self.element('reply_text').get_text(timeout=2)
        if normalize(draft):
            record('scroll.latest.blocked', 'blocked', {'reason':'人工草稿', 'before':before}, customer=expected_customer)
            if allow_unknown_latest:
                return False
            raise DesktopError('输入框有人工草稿，未自动滚动、未覆盖；请处理草稿后再读取。')
        self.element('chat_window')  # Require the recorded target to remain unique.
        if self.customer() != expected_customer:
            raise DesktopError('自动回到最新消息前客户已切换，已取消。')
        self.check_stop()
        notice = getattr(self, 'progress', None)
        if callable(notice):
            notice('当前聊天未在最新位置，正在自动回到最新消息；不会发送。')
        record('scroll.latest.intent', details={'before':before, 'target_top':0}, customer=expected_customer)
        after = None
        try:
            loc = self.resolve('current_user')
            tab = self.cc.edge.attach(loc, is_maximize=False, timeout=3)
            elements = tab.find_elements(loc, timeout=2)
            if len(elements) != 1:
                raise DesktopError('自动滚动时客户标题不唯一，已取消。')
            # Recheck input and identity immediately before the reversible scroll.
            if normalize(self.element('reply_text').get_text(timeout=2)):
                raise DesktopError('自动滚动前出现人工草稿，已取消。')
            if self.customer() != expected_customer:
                raise DesktopError('自动滚动前客户已切换，已取消。')
            self.check_stop()
            returned = elements[0].execute_js(CHAT_RETURN_LATEST_SCRIPT, method='returnChatLatest()', timeout=3)
            record('scroll.latest.returned', details={'returned':returned}, customer=expected_customer)
            if returned not in (True, 'true'):
                raise DesktopError('聊天区未确认执行自动滚动。')
            deadline = time.monotonic() + 3
            attempts = 0
            previous = None
            # Check two matching post-scroll states, including content and dimensions.
            while time.monotonic() < deadline:
                if self.stop.wait(.15):
                    raise Cancelled('自动回到最新消息时已停止。')
                if self.customer() != expected_customer:
                    raise DesktopError('自动滚动期间客户已切换，已取消本次读取。')
                if normalize(self.element('reply_text').get_text(timeout=2)):
                    raise DesktopError('自动滚动期间出现人工草稿，已取消本次读取。')
                after = self.chat_position(require_latest=False)
                attempts += 1
                if chat_is_latest(after) and previous == after:
                    if self.customer() != expected_customer:
                        raise DesktopError('确认最新位置时客户已切换，已取消本次读取。')
                    record('scroll.latest.confirmed', 'passed', {'before':before,'after':after,'samples':attempts}, customer=expected_customer)
                    if callable(notice):
                        notice('已自动回到最新消息，并确认客户和聊天状态稳定。')
                    return True
                previous = after
            raise DesktopError('自动回到最新消息后未确认稳定，已停止读取；未调用模型。')
        except Exception as error:
            error.evidence = {**getattr(error,'evidence',{}), 'stage':'scroll.latest',
                              'before':before, 'after':after, 'expected_customer':expected_customer,
                              'samples':locals().get('attempts',0)}
            record('scroll.latest.evidence', 'failed', exception_details(error), customer=expected_customer)
            raise

    def is_xianyu(self):
        from .paths import DATA_ROOT
        return is_xianyu(getattr(self, 'cfg', {}), getattr(self, 'project_root', DATA_ROOT))

    def checked_unread_rows(self):
        from .paths import DATA_ROOT
        expected = self.cfg.get('unread_rule_hash')
        if expected:
            stores, _ = saved_stores(getattr(self, 'project_root', DATA_ROOT))
            store, _, name = locator_path(self.cfg['locators']['new_message']).partition('.')
            content = stores.get(store).locations.get(name) if store in stores else None
            if not content or rule_hash(content) != expected:
                raise DesktopError('未读录制规则已被重新保存，请返回检查5个位置；不会继续点击。')
        return probe_unread(self)['rows']

    def read_message_state(self, *, deadline=None):
        """Read messages without screenshots, focus, scrolling, or model calls."""
        if not self.is_xianyu():
            raise DesktopError('网页消息核对仅适用于已适配的闲鱼页面。')
        deadline = deadline if deadline is not None else time.monotonic() + 5
        def budget():
            self.check_stop()
            left = deadline - time.monotonic()
            if left <= 0:raise DesktopError('网页消息读取超过核对时限。')
            # Clicknium's .NET bridge requires whole seconds, including zero
            # for an immediate lookup near the end of the five-second budget.
            return min(1, int(left))
        def customer():
            values = self.cc.find_elements(self.resolve('current_user'), timeout=budget())
            if len(values)!=1:raise DesktopError('网页核对时客户标题不唯一。')
            name = values[0].get_text(timeout=budget()).strip()
            if not 2 <= len(name) <= 200 or name in ('[',']','客户','未知','接待中心'):
                raise DesktopError('网页核对时客户身份不可读取。')
            return name
        before = customer()
        try:
            loc = self.resolve('current_user')
            tab = self.cc.edge.attach(loc, is_maximize=False, timeout=budget())
            elements = tab.find_elements(loc, timeout=budget())
            if len(elements)!=1:raise DesktopError('网页核对时客户标题不唯一。')
            element = elements[0]
            raw = json.loads(element.execute_js(CHAT_SCROLL_SCRIPT + MESSAGE_SCRIPT, method='chatMessages()', timeout=budget()))
            messages = parse_messages(raw['messages'])
            position = raw['position']
        except Cancelled:
            raise
        except Exception as error:
            failure = DesktopError('网页消息结构无法可靠读取，不能确认新增回复。')
            failure.evidence = {'stage':'web_messages.read', 'cause':exception_details(error)}
            raise failure from error
        after = customer()
        budget()
        if before != after:
            error = DesktopError('读取网页消息期间客户发生切换。')
            error.evidence = {'stage':'web_messages.identity','before_customer':before,'after_customer':after}
            raise error
        if not chat_is_latest(position):
            error = DesktopError('网页核对时聊天未处于最新位置。')
            error.evidence = {'stage':'web_messages.position','position':position,'messages':messages}
            raise error
        return MessageState(before,messages,position)

    def verify_web_reply(self, baseline, text):
        deadline = time.monotonic() + 5
        previous = None
        stable = attempts = 0
        reason = 'unstable_messages'
        current = None
        while time.monotonic() < deadline:
            started = time.monotonic()
            self.check_stop()
            try:
                current = self.read_message_state(deadline=deadline)
            except Exception as error:
                error.evidence = {**getattr(error,'evidence',{}),'before':baseline.messages,
                    'last_after':current.messages if current else None,'attempts':attempts,
                    'last_match_reason':reason}
                raise
            attempts += 1
            if current.customer != baseline.customer:
                error = DesktopError('发送后客户发生切换，不能确认发送结果。')
                error.evidence = {'before_customer':baseline.customer,'after_customer':current.customer}
                raise error
            stable = stable + 1 if previous is not None and current.messages == previous.messages else 1
            verified, reason, added = compare_reply(baseline,current,text)
            record('send.web.sample', details={'attempt':attempts,'stable_count':stable,
                   'before':baseline.messages,'after':current.messages,'added':added,'reason':reason},customer=current.customer)
            if stable >= 2 and verified:
                record('send.web.verified','passed',{'before':baseline.messages,'after':current.messages,
                       'added':added,'reason':reason,'samples':attempts},customer=current.customer)
                return current, added
            previous = current
            delay = min(max(0, .25-(time.monotonic()-started)),max(0,deadline-time.monotonic()))
            if self.stop.wait(delay):raise Cancelled('发送后核对已停止，结果需人工检查。')
        error = DesktopError('网页新增回复核对未通过：' + reason + '；发送结果需人工核查。')
        error.evidence = {'stage':'web_messages.verify','before':baseline.messages,
                          'after':current.messages if current else None,'reason':reason,'stable_count':stable,'attempts':attempts}
        raise error

    def _scan_red_badges(self) -> UnreadScan:
        """Read Xianyu's recorded sidebar without changing the active chat."""
        self.check_stop()
        aside = self.element('new_message')
        pos = aside.get_position(timeout=2)
        position = (pos.Left, pos.Top, pos.Right, pos.Bottom)
        width, height = pos.Right - pos.Left, pos.Bottom - pos.Top
        if width < 160 or height < 100:
            raise DesktopError('消息列表不可见或尺寸异常，未点击会话。')
        outer = aside.children
        if len(outer) != 1 or len(outer[0].children) != 2:
            raise DesktopError('闲鱼消息列表结构已变化，无法区分列表头与客户行。')
        header, body = outer[0].children
        header_pos, body_pos = header.get_position(timeout=2), body.get_position(timeout=2)
        header_bottom = header_pos.Bottom - pos.Top
        if (not 20 <= header_bottom <= height * .3 or
                abs(body_pos.Top - header_pos.Bottom) > 3):
            raise DesktopError('闲鱼列表头位置不确定，未识别红点。')

        fd, filename = tempfile.mkstemp(suffix='.png', prefix='jev_unread_')
        os.close(fd)
        path = Path(filename)
        try:
            aside.save_to_image(str(path))
            png = path.read_bytes()
        finally:
            path.unlink(missing_ok=True)
        with Image.open(BytesIO(png)) as image:
            image_size = image.size
        if image_size != (width, height):
            raise DesktopError('列表截图与屏幕坐标尺寸不一致，未点击会话。')

        candidates = []
        frontier = [body]
        for _ in range(6):
            next_frontier = []
            for parent in frontier[:40]:
                children = parent.children[:100]
                rows = []
                for child in children:
                    p = child.get_position(timeout=1)
                    child_width, child_height = p.Right - p.Left, p.Bottom - p.Top
                    if (child_width >= width * .88 and 40 <= child_height <= 140 and
                            body_pos.Top <= p.Top < body_pos.Bottom):
                        bounds = RowBounds(p.Top - pos.Top, p.Bottom - pos.Top)
                        if bounds not in [row[0] for row in rows]:
                            rows.append((bounds, child))
                    if (child_width >= width * .75 and child_height > 140 and
                            p.Bottom > body_pos.Top):
                        next_frontier.append(child)
                if len(rows) >= 2:
                    candidates.append(tuple(sorted(rows, key=lambda row: row[0].top)))
            frontier = next_frontier[:40]
            if not frontier:
                break
        if not candidates:
            raise DesktopError('无法唯一找到闲鱼会话行，未点击。')
        largest = max(len(candidate) for candidate in candidates)
        groups = [group for group in candidates if len(group) == largest]
        bounds = tuple(row[0] for row in groups[0])
        if any(tuple(row[0] for row in group) != bounds for group in groups[1:]):
            raise DesktopError('闲鱼会话行结构存在歧义，未点击。')
        try:
            matches = match_red_badges(find_red_badges(png, header_bottom=header_bottom),
                                       bounds, list_width=width)
        except ValueError as exc:
            raise DesktopError(str(exc)) from exc
        return UnreadScan(png, position, tuple(row[1] for row in groups[0]), matches)

    @traced('desktop.unread')
    def open_next_unread(self, *, preview_unverified=False):
        if not self.cfg['unread_locator_reviewed'] and not preview_unverified:
            raise DesktopError('未验证“未读客户行”定位器；不能把普通客户列表当新消息。')
        self.check_stop()
        if preview_unverified or self.cfg.get('unread_mode', 'locator') == 'red_badge':
            before = self._scan_red_badges()
            record('unread.scan', details={'mode': 'red_badge', 'count': len(before.matches), 'matches': before.matches})
            if not before.matches:
                return False
            row_index = before.matches[self.round_robin % len(before.matches)]
            current = self._scan_red_badges()
            if (before.png != current.png or before.position != current.position or
                    before.matches != current.matches):
                raise DesktopError('点击前消息列表发生变化，已暂停本次读取。')
            self.check_stop()
            record('unread.open.intent', details={'row_index': row_index, 'position': current.position}, png=current.png)
            current.rows[row_index].click()
            record('unread.open.returned')
            self.round_robin += 1
            if self.stop.wait(self.cfg['settle_seconds']):
                raise Cancelled('操作已停止。')
            self.customer()
            return True
        rows = (self.checked_unread_rows() if self.is_xianyu() else
                self.cc.find_elements(self.resolve('new_message'), timeout=1))
        record('unread.scan', details={'mode': 'locator', 'count': len(rows)})
        if not rows:
            return False
        row = rows[self.round_robin % len(rows)]
        if self.is_xianyu():
            self.check_stop()
            state = json.loads(row.execute_js(ROW_SCRIPT, method='unreadRowState()', timeout=2))
            if not checked_row(state):
                record('unread.changed', 'skipped', {'reason': '点击前未读标记消失或客户行已失效'})
                return False
        self.round_robin += 1
        record('unread.open.intent', details={'row_index': (self.round_robin - 1) % len(rows)})
        row.click()
        record('unread.open.returned')
        if self.stop.wait(self.cfg['settle_seconds']):
            raise Cancelled('操作已停止。')
        return True

    @traced('send.unchanged_check')
    def ensure_unchanged(self, baseline: Snapshot):
        if baseline.position_evidence is not None and baseline.latest_position_verified is not True:
            raise DesktopError('最新位置尚未通过程序确认，此预览禁止发送。')
        current = self.read_current()
        if current.customer != baseline.customer:
            error = DesktopError('客户窗口已切换，已禁止发送。')
            error.baseline, error.current = baseline, current
            raise error
        old = (baseline.position_evidence or {}).get('after', {})
        new = (current.position_evidence or {}).get('after', {})
        if isinstance(old.get('content'), str) and isinstance(new.get('content'), str):
            unchanged = (old['content'] == new['content'] and
                         all(old.get(k) == new.get(k) for k in ('top', 'height', 'full')))
        else:
            unchanged = current.digest == baseline.digest
        if not unchanged:
            error = DesktopError('聊天消息内容或滚动位置已变化，需重新读取，禁止使用旧回复。')
            error.baseline, error.current = baseline, current
            error.evidence = {'before': old, 'after': new, 'changed_fields': [k for k in set(old) | set(new) if old.get(k) != new.get(k)]}
            raise error
        record('send.unchanged_check.passed', 'passed', {'before': old, 'after': new,
               'before_customer': baseline.customer, 'after_customer': current.customer}, customer=baseline.customer)

    @traced('send.reply')
    def send_reply(self, baseline: Snapshot, text: str):
        """No automatic retries. After click, the caller verifies the visible seller bubble."""
        self.send_phase = 'precheck'
        self.click_intent = False
        self.web_send_baseline = None
        record('send.precheck.started', customer=baseline.customer)
        if not text or len(text) > 3000:
            raise DesktopError('回复为空或超过长度上限。')
        self.ensure_unchanged(baseline)
        record('send.precheck.passed', customer=baseline.customer)
        self.send_phase = 'input_check'
        input_box = self.element('reply_text')
        draft = input_box.get_text(timeout=2)
        record('send.input_check', 'blocked' if normalize(draft) else 'passed', {'draft': draft}, customer=baseline.customer)
        if normalize(draft):
            raise DesktopError('输入框已有人工草稿，未覆盖、未发送。')
        # Clipboard restoration is deliberately not attempted: it could overwrite a user's newer clipboard.
        self.check_stop()
        self.send_phase = 'paste'
        record('send.paste.intent', details={'text': text}, customer=baseline.customer)
        self.clipboard.copy(text)
        input_box.click()
        self.check_stop()
        input_box.send_hotkey('^v')
        record('send.paste.returned', customer=baseline.customer)
        if self.stop.wait(0.25):
            raise Cancelled('已停止；输入框可能留有未发送草稿，请人工检查。')
        self.send_phase = 'readback'
        readback = input_box.get_text(timeout=2)
        record('send.readback', 'passed' if normalize(readback) == normalize(text) else 'failed', {'actual': readback, 'expected': text, 'matches': normalize(readback) == normalize(text)}, customer=baseline.customer)
        if normalize(readback) != normalize(text):
            raise DesktopError('粘贴内容未能核对，未点击发送；请人工检查草稿。')
        self.ensure_unchanged(baseline)
        # Validate actual input and recipient again immediately before the irreversible click.
        self.send_phase = 'final_input_check'
        final_text = self.element('reply_text').get_text(timeout=2)
        record('send.final_input_check', 'passed' if normalize(final_text) == normalize(text) else 'failed', {'actual': final_text, 'expected': text}, customer=baseline.customer)
        if normalize(final_text) != normalize(text):
            raise DesktopError('输入框内容在发送前被改动，已停止。')
        self.check_stop()
        if self.is_xianyu():
            self.send_phase = 'web_baseline'
            browser_before = self.read_message_state()
            old_content = (baseline.position_evidence or {}).get('after',{}).get('content')
            if browser_before.customer != baseline.customer or old_content != browser_before.position.get('content'):
                error = DesktopError('发送前网页消息或客户已变化，请重新读取。')
                error.evidence = {'stage':'web_messages.baseline','before_customer':baseline.customer,
                    'after_customer':browser_before.customer,'before_content':old_content,
                    'after_content':browser_before.position.get('content')}
                raise error
            self.web_send_baseline = browser_before
            record('send.web.baseline',details={'messages':browser_before.messages},customer=baseline.customer)
        button = self.element('button_send')
        self.send_phase = 'click_intent'
        record('send.click.intent', details={'text': text}, customer=baseline.customer)
        # Only after the FULL synchronous transaction commits may a click occur.
        self.click_intent = True
        button.click()
        self.send_phase = 'click_returned'
        record('send.click.returned', customer=baseline.customer)
        # A click is NOT a delivery acknowledgement.

    def doctor(self):
        result = {}
        for name in ('current_user', 'chat_window', 'reply_text', 'button_send'):
            try:
                self.element(name)
                result[name] = '唯一匹配'
            except Exception as e:
                result[name] = str(e)
        try:
            self.customer()
            result['customer_readable'] = True
        except Exception as e:
            result['customer_readable'] = False
            result['customer_error'] = str(e)
        try:
            value = self.element('reply_text').get_text(timeout=2)
            result['input_readable'] = isinstance(value, str)
        except Exception as e:
            result['input_readable'] = False
            result['input_error'] = str(e)
        try:
            self.read_current(prepare_latest=True)
            result['chat_capture_valid'] = True
            result['聊天最新位置'] = '已自动回到最新消息并确认' if self.last_auto_scroll else '已确认，无需滚动'
        except Exception as e:
            result['chat_capture_valid'] = False
            result['chat_capture_error'] = str(e)
        try:
            count = (len(self._scan_red_badges().matches) if self.cfg.get('unread_mode', 'locator') == 'red_badge'
                     else len(self.checked_unread_rows()) if self.is_xianyu()
                     else len(self.cc.find_elements(self.resolve('new_message'), timeout=1)))
            result['new_message_count'] = count
            result['new_message'] = (f'当前可见 {count} 条红点会话（列表外未读需另行检查）'
                                     if self.cfg.get('unread_mode', 'locator') == 'red_badge' else
                                     f'当前匹配 {count} 行（需用测试消息核对是否仅匹配未读）')
        except Exception as e:
            result['new_message'] = str(e)
        return result
