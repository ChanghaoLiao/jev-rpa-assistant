"""One-shot Xianyu trial: unread row -> MiniMax -> Jev -> paste draft.

This script has no send path. It never calls Service.send or the send button.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


class TrialError(RuntimeError):
    pass


class _NicknameParser(HTMLParser):
    """Read the name field of one Xianyu conversation row, never its preview."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.names: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.depth:
            self.depth += 1
            return
        style = dict(attrs).get('style', '')
        if (tag == 'div' and 'overflow: hidden' in style and
                'text-overflow: ellipsis' in style and 'white-space: nowrap' in style and
                'max-width:' in style and 'max-height:' not in style):
            self.depth = 1
            self.parts = []

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1
            if not self.depth:
                self.names.append(''.join(self.parts).strip())


def buyer_name_from_row(html: str) -> str:
    parser = _NicknameParser()
    parser.feed(html)
    if len(parser.names) != 1 or not 2 <= len(parser.names[0]) <= 200:
        raise TrialError('未读行的买家昵称不能唯一读取，停止测试。')
    return parser.names[0]


@dataclass(frozen=True)
class RowInfo:
    name: str
    box: tuple[int, int, int, int]
    html_digest: str
    element: object

    @property
    def signature(self):
        return self.name, self.box, self.html_digest


def scan_unread(desktop) -> tuple[RowInfo, ...]:
    rows = desktop.cc.find_elements(desktop.resolve('new_message'), timeout=2)
    result = []
    for row in rows:
        html = row.get_property('outerHTML')
        if not isinstance(html, str) or 'ant-badge-count' not in html:
            raise TrialError('未读标记无法核对，停止测试。')
        pos = row.get_position(timeout=2)
        box = pos.Left, pos.Top, pos.Right, pos.Bottom
        if box[2] - box[0] < 160 or not 40 <= box[3] - box[1] <= 140:
            raise TrialError('未读行位置或尺寸异常，停止测试。')
        result.append(RowInfo(buyer_name_from_row(html), box,
                              hashlib.sha256(html.encode('utf-8')).hexdigest(), row))
    return tuple(result)


def open_first_unread(desktop, watch_seconds: int = 300) -> str:
    deadline = time.monotonic() + watch_seconds
    first = scan_unread(desktop)
    while not first:
        if time.monotonic() >= deadline:
            raise TrialError('等待超时：没有看到未读会话，未点击。')
        desktop.check_stop()
        if desktop.stop.wait(desktop.cfg['poll_seconds']):
            raise TrialError('监测已停止，未点击。')
        first = scan_unread(desktop)
    target_first = min(first, key=lambda row: row.box[1])
    if sum(row.box[1] == target_first.box[1] for row in first) != 1:
        raise TrialError('最上方未读行位置不唯一，未点击。')
    second = scan_unread(desktop)
    if tuple(row.signature for row in first) != tuple(row.signature for row in second):
        raise TrialError('点击前未读列表发生变化，已停止。')
    target = min(second, key=lambda row: row.box[1])
    desktop.check_stop()
    target.element.click()
    if desktop.stop.wait(desktop.cfg['settle_seconds']):
        raise TrialError('操作已停止；请检查页面。')
    if desktop.customer() != target.name:
        raise TrialError('打开后当前买家与未读行昵称不一致；没有识字或粘贴。')
    return target.name


def paste_draft(desktop, snapshot, text: str):
    """Paste only; this function cannot resolve or click button_send."""
    from jevtool.vision import normalize

    if not text or len(text) > 3000:
        raise TrialError('候选回复为空或过长，未粘贴。')
    desktop.ensure_unchanged(snapshot)
    input_box = desktop.element('reply_text')
    html = input_box.get_property('outerHTML')
    if not isinstance(html, str) or not html.lstrip().lower().startswith('<textarea'):
        raise TrialError('定位器没有指向闲鱼文本输入框，未粘贴。')
    if normalize(input_box.get_text(timeout=2)):
        raise TrialError('输入框已有草稿，未覆盖。')
    desktop.check_stop()
    desktop.clipboard.copy(text)
    input_box.click()
    desktop.check_stop()
    input_box.send_hotkey('^v')
    if desktop.stop.wait(.25):
        raise TrialError('已停止；输入框可能留有测试草稿，请人工检查。')
    if normalize(input_box.get_text(timeout=2)) != normalize(text):
        raise TrialError('粘贴后的文字与固定话术不一致；未发送，请人工检查草稿。')
    desktop.ensure_unchanged(snapshot)
    if desktop.customer() != snapshot.customer:
        raise TrialError('粘贴后当前买家变化；未发送，请人工检查草稿。')


def run_trial(desktop, service, watch_seconds: int = 300):
    buyer = open_first_unread(desktop, watch_seconds)
    if not service.preview_current(auto=False):
        raise TrialError('当前会话没有可处理的新买家消息，未粘贴。')
    pending = list(service.pending.values())
    if len(pending) != 1:
        raise TrialError('Jev 未选出唯一固定回复，未粘贴；请人工查看。')
    snapshot, decision, _ = pending[0]
    if snapshot.customer != buyer or decision.action != 'candidate':
        raise TrialError('买家或候选回复不符合测试条件，未粘贴。')
    paste_draft(desktop, snapshot, decision.text)


def _installed_app_running() -> bool:
    if sys.platform != 'win32':
        return False
    try:
        result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq JevReplyTool.exe',
                                 '/FO', 'CSV', '/NH'], capture_output=True, text=True,
                                errors='replace', timeout=8, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise TrialError('无法确认客服工具是否仍在运行；请关闭原程序后再运行测试。') from error
    return any(fields and fields[0].casefold() == 'jevreplytool.exe'
               for fields in csv.reader(result.stdout.splitlines()))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='闲鱼未读监测：识字、Jev选话术、只粘贴不发送')
    parser.add_argument('--watch-seconds', type=int, default=300, help='等待未读会话的最长秒数，默认300')
    args = parser.parse_args(argv)
    if not 1 <= args.watch_seconds <= 3600:
        raise TrialError('监测时间必须在1到3600秒之间。')
    if sys.platform != 'win32':
        raise TrialError('只支持 Windows 闲鱼网页版。')
    if _installed_app_running():
        raise TrialError('请先关闭当前客服工具，避免两个程序同时操作闲鱼页面。测试结束后可重新打开原程序。')

    data_root = Path(os.environ.get('JEVTOOL_DATA_DIR') or
                     Path(os.environ['LOCALAPPDATA']) / 'JevReplyTool')
    sys.path[0] = str(data_root)  # Clicknium finds the existing .locator here.
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(1, str(PROJECT_ROOT))
    os.chdir(data_root)
    from jevtool.config import load_config
    from jevtool.desktop import ClickniumDesktop, DesktopError
    from jevtool.service import Service
    from jevtool.store import Store

    class NoSendDesktop(ClickniumDesktop):
        def element(self, name):
            if name == 'button_send':
                raise TrialError('测试脚本禁止访问发送按钮。')
            return super().element(name)

        def send_reply(self, *args, **kwargs):
            raise TrialError('测试脚本禁止执行发送流程。')

    cfg = load_config(data_root / 'config.local.json')
    profile = cfg['profiles'][cfg['active_profile']]
    if profile.get('label') != '闲鱼网页版' or cfg['desktop']['locators']['new_message'] != 'goofish.new_message':
        raise TrialError('当前配置不是已验证的闲鱼未读行定位器，未操作页面。')
    if cfg['desktop']['auto_send_enabled']:
        raise TrialError('当前配置开启了自动发送，停止测试。')
    if not cfg['desktop']['locators_reviewed']:
        raise TrialError('其余聊天定位器尚未验收，停止测试。')
    if not cfg['jev']['api_key'] or not cfg['vision']['api_key']:
        raise TrialError('Jev 或 MiniMax Key 尚未填写。')
    # The user authorized this one-shot screenshot upload in this chat. Keep disk config untouched.
    cfg['desktop']['data_upload_authorized'] = True
    stop = threading.Event()
    desktop = NoSendDesktop(cfg['desktop'], stop)
    with tempfile.TemporaryDirectory(prefix='jev_xianyu_trial_') as folder:
        service = Service(cfg, stop, lambda _: None, desktop=desktop,
                          store=Store(Path(folder) / 'events.sqlite3'))
        try:
            print('监测闲鱼可见未读会话；出现未读后只处理最上方一条。按 Ctrl+C 可停止。', flush=True)
            run_trial(desktop, service, args.watch_seconds)
            print('测试完成：买家已核对；MiniMax 已识字；Jev 已选择固定话术；草稿已粘贴并核对。')
            print('没有点击发送。请在闲鱼输入框人工核对草稿；不需要时请手动清空。')
            return 0
        finally:
            service.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (TrialError, KeyboardInterrupt) as error:
        print(f'测试停止：{error}', file=sys.stderr)
        raise SystemExit(1)
