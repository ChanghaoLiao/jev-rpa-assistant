"""Whole receive/preview/send path against simulated Clicknium UI controls."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from jevtool.config import DEFAULTS
from jevtool.desktop import ClickniumDesktop
from jevtool.router import Decision
from jevtool.service import Service
from jevtool.store import Store
from jevtool.vision import parse_transcript


class ChatControl:
    def __init__(self, chat, name):
        self.chat, self.name = chat, name

    def get_text(self, **_):
        return self.chat.customer if self.name == 'current_user' else self.chat.draft

    def save_to_image(self, path):
        # A different visible chat state yields a different screenshot digest.
        color = (70, len(self.chat.messages) * 40, 90)
        Image.new('RGB', (80, 40), color).save(path, format='PNG')

    def click(self):
        if self.name == 'new_message':
            self.chat.unread = False
        elif self.name == 'button_send':
            self.chat.messages.append(('seller', self.chat.draft))
            self.chat.draft = ''

    def send_hotkey(self, hotkey):
        assert hotkey == '^v'
        self.chat.draft = self.chat.clipboard.value


class ChatUI:
    def __init__(self):
        self.customer = '测试买家'
        self.unread = True
        self.draft = ''
        self.messages = [('buyer', '怎么查看预计发货时间？')]
        self.clipboard = Mock()
        self.clipboard.value = ''
        self.clipboard.copy.side_effect = lambda value: setattr(self.clipboard, 'value', value)

    def find_elements(self, locator, timeout=0):
        name = locator.rsplit('.', 1)[-1]
        return [ChatControl(self, name)] if name != 'new_message' or self.unread else []


class SimulatedChatTests(unittest.TestCase):
    def test_unread_to_verified_reply_then_restart_does_not_repeat(self):
        chat = ChatUI()
        desktop = ClickniumDesktop.__new__(ClickniumDesktop)
        cfg = copy.deepcopy(DEFAULTS)
        cfg['desktop'].update(shop_id='trial-shop', data_upload_authorized=True,
                              locators_reviewed=True, unread_locator_reviewed=True,
                              settle_seconds=.5,
                              locators={name: 'mock.' + name for name in cfg['desktop']['locators']})
        desktop.cfg = cfg['desktop']
        desktop.stop = threading.Event()
        desktop.cc = chat
        desktop.locator = type('Locator', (), {'mock': type('MockStore', (), {
            name: 'mock.' + name for name in cfg['desktop']['locators']})()})()
        desktop.clipboard = chat.clipboard
        desktop.round_robin = 0
        reader = Mock()
        reader.read.side_effect = lambda png, customer: parse_transcript({
            'readable': True, 'at_bottom': True, 'has_unsupported_content': False,
            'messages': [{'role': role, 'text': text, 'timestamp': '10:00'}
                         for role, text in chat.messages]})
        router = Mock()
        router.decide.return_value = Decision('candidate', 'shipping_where',
                                              '请查看订单页的预计发货时间。', '可回复', ['物流'], [], True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.sqlite3'
            events = []
            service = Service(cfg, desktop.stop, events.append, desktop=desktop,
                              router=router, reader=reader, store=Store(path))
            try:
                self.assertTrue(service.scan_once())
                candidate = next(e for e in events if e['kind'] == 'decision')
                self.assertEqual(candidate['decision']['text'], '请查看订单页的预计发货时间。')
                self.assertEqual(chat.messages, [('buyer', '怎么查看预计发货时间？')])
                service.send(candidate['id'], manual=True)
                self.assertEqual(chat.messages[-1], ('seller', candidate['decision']['text']))
                self.assertEqual(service.store.status(candidate['id']), 'sent_ui_verified')
                self.assertFalse(service.scan_once())
            finally:
                service.close()
            reopened = Store(path)
            try:
                self.assertEqual(len(reopened.recent()), 1)
                self.assertEqual(reopened.status(candidate['id']), 'sent_ui_verified')
            finally:
                reopened.close()


if __name__ == '__main__':
    unittest.main()
