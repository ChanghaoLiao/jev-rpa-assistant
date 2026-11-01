import unittest
from unittest.mock import Mock
from jevtool.desktop import ClickniumDesktop, Snapshot, DesktopError

class ContentPrecheckTests(unittest.TestCase):
    def snap(self, pixels, text='客户：几号到货', top=0):
        return Snapshot('客户甲', pixels, pixels.hex(), True,
                        {'after': {'content': text, 'top': top, 'height': 200, 'full': 400}})
    def adapter(self, current):
        desktop=object.__new__(ClickniumDesktop)
        desktop.read_current=Mock(return_value=current)
        return desktop
    def test_render_change_passes_with_same_content(self):
        self.adapter(self.snap(b'new rendering')).ensure_unchanged(self.snap(b'old rendering'))
    def test_new_message_stops_and_preserves_both_snapshots(self):
        old=self.snap(b'old');new=self.snap(b'new','客户：地址变更')
        with self.assertRaises(DesktopError) as caught:
            self.adapter(new).ensure_unchanged(old)
        self.assertIs(caught.exception.baseline,old)
        self.assertIs(caught.exception.current,new)
    def test_scroll_change_stops(self):
        with self.assertRaises(DesktopError):
            self.adapter(self.snap(b'x',top=-50)).ensure_unchanged(self.snap(b'x'))
    def test_recipient_change_stops(self):
        new=Snapshot('客户乙',b'x','x')
        with self.assertRaises(DesktopError):
            self.adapter(new).ensure_unchanged(self.snap(b'x'))
    def test_legacy_capture_keeps_pixel_guard(self):
        with self.assertRaises(DesktopError):
            self.adapter(Snapshot('客户甲',b'b','b')).ensure_unchanged(Snapshot('客户甲',b'a','a'))

    def test_unverified_baseline_cannot_send_even_with_matching_content(self):
        old=Snapshot('客户甲',b'x','x',False,{'after':{'content':'same'}})
        desktop=self.adapter(old)
        with self.assertRaises(DesktopError):desktop.ensure_unchanged(old)
        desktop.read_current.assert_not_called()
