"""Exercise the real desktop adapter's sending logic with fake UI controls.
No Windows, Clicknium runtime, or real customer messages are involved.
"""
import threading
import unittest
from unittest.mock import Mock
from jevtool.desktop import ClickniumDesktop, DesktopError, Snapshot, UnreadScan, expanded_capture_bounds
from jevtool.network import Cancelled

class DesktopGuardTests(unittest.TestCase):
    def test_capture_extends_current_height_quarter_at_bottom(self):
        self.assertEqual(expanded_capture_bounds((500,400,1000,600), (500,900,1000,1000), (0,0,1920,1080)), (500,300,1000,800))

    def test_extra_bottom_stops_before_input(self):
        self.assertEqual(expanded_capture_bounds((500,400,1000,600), (500,750,1000,1000), (0,0,1920,1080)), (500,300,1000,750))

    def test_capture_excludes_draft_and_clips_virtual_screen(self):
        self.assertEqual(expanded_capture_bounds((-100,40,1000,640), (-100,650,1000,900), (-50,0,1920,1080)), (-50,0,1000,650))

    def test_invalid_capture_stops(self):
        with self.assertRaises(DesktopError):
            expanded_capture_bounds((500,400,500,600), (500,900,1000,1000), (0,0,1920,1080))

    def setUp(self):
        self.d=ClickniumDesktop.__new__(ClickniumDesktop)
        self.d.stop=threading.Event()
        self.d.round_robin=0
        self.d.clipboard=Mock()
        self.d.ensure_unchanged=Mock()
        self.input=Mock();self.button=Mock()
        self.text=''
        self.input.get_text.side_effect=lambda **kw:self.text
        self.input.send_hotkey.side_effect=self.paste
        self.d.element=lambda name: self.input if name=='reply_text' else self.button
        self.base=Snapshot('c',b'not-used','hash')
    def paste(self,hotkey):
        self.text=self.d.clipboard.copy.call_args.args[0]
    def test_exact_clipboard_paste_and_send(self):
        self.d.send_reply(self.base,'固定原文')
        self.d.clipboard.copy.assert_called_once_with('固定原文')
        self.input.send_hotkey.assert_called_once_with('^v')
        self.button.click.assert_called_once()
        self.assertEqual(self.d.ensure_unchanged.call_count,2)
    def test_existing_draft_never_overwritten(self):
        self.text='人工草稿'
        with self.assertRaises(DesktopError):self.d.send_reply(self.base,'固定原文')
        self.d.clipboard.copy.assert_not_called();self.button.click.assert_not_called()
    def test_failed_paste_never_sends(self):
        self.input.send_hotkey.side_effect=lambda _:None
        with self.assertRaises(DesktopError):self.d.send_reply(self.base,'固定原文')
        self.button.click.assert_not_called()
    def test_changed_chat_after_paste_never_sends(self):
        self.d.ensure_unchanged.side_effect=[None,DesktopError('changed')]
        with self.assertRaises(DesktopError):self.d.send_reply(self.base,'固定原文')
        self.button.click.assert_not_called()
    def test_stop_during_paste_never_sends(self):
        def paste_and_stop(hotkey):self.paste(hotkey);self.d.stop.set()
        self.input.send_hotkey.side_effect=paste_and_stop
        with self.assertRaises(Cancelled):self.d.send_reply(self.base,'固定原文')
        self.button.click.assert_not_called()
    def test_overlong_reply_rejected(self):
        with self.assertRaises(DesktopError):self.d.send_reply(self.base,'x'*3001)
        self.button.click.assert_not_called()

    def test_changed_red_badge_list_never_clicks(self):
        row = Mock()
        self.d.cfg = {'unread_mode': 'red_badge', 'unread_locator_reviewed': True,
                      'settle_seconds': .5}
        first = UnreadScan(b'first', (0, 0, 330, 513), (row,), (0,))
        changed = UnreadScan(b'changed', (0, 0, 330, 513), (row,), (0,))
        self.d._scan_red_badges = Mock(side_effect=(first, changed))
        with self.assertRaisesRegex(DesktopError, '发生变化'):
            self.d.open_next_unread()
        row.click.assert_not_called()

    def test_stable_red_badge_list_clicks_only_one_row(self):
        row = Mock()
        self.d.cfg = {'unread_mode': 'red_badge', 'unread_locator_reviewed': True,
                      'settle_seconds': .5}
        self.d.round_robin = 0
        scan = UnreadScan(b'same', (0, 0, 330, 513), (row,), (0,))
        self.d._scan_red_badges = Mock(return_value=scan)
        self.d.customer = Mock(return_value='测试买家')
        self.assertTrue(self.d.open_next_unread())
        row.click.assert_called_once()
        self.d.customer.assert_called_once()

    def test_unverified_preview_uses_red_badges_without_changing_config(self):
        self.d.cfg = {'unread_mode':'locator','unread_locator_reviewed':False,'settle_seconds':0}
        self.d._scan_red_badges = Mock(return_value=UnreadScan(b'list',(0,0,330,513),(),()))
        with self.assertRaises(DesktopError):
            self.d.open_next_unread()
        self.assertFalse(self.d.open_next_unread(preview_unverified=True))
        self.d._scan_red_badges.assert_called_once()
        self.assertFalse(self.d.cfg['unread_locator_reviewed'])
        self.assertEqual(self.d.cfg['unread_mode'],'locator')

if __name__=='__main__':unittest.main()
