import threading
import unittest
from unittest.mock import Mock, patch

from jevtool.desktop import ClickniumDesktop, DesktopError, Snapshot, CHAT_RETURN_LATEST_SCRIPT, physical_chat_bounds
from jevtool.network import Cancelled


class AutoLatestTests(unittest.TestCase):
    def state(self, top=0, **changes):
        return dict(top=top, height=220, full=2168, direction='column-reverse',
                    display='flex', overflow='auto', visible=True, content='stable', **changes)

    def desktop(self, states=None):
        d=ClickniumDesktop.__new__(ClickniumDesktop)
        d.cfg={'adapter':'xianyu_web','locators':{'chat_window':'goofish.chat_window'}}
        d.stop=Mock();d.stop.is_set.return_value=False;d.stop.wait.return_value=False
        d.customer=Mock(return_value='客户甲')
        d.chat_position=Mock(side_effect=states or [self.state(-1948),self.state(),self.state()])
        d.resolve=Mock(return_value='recorded-chat');d.cc=Mock();d.progress=Mock()
        d.input=Mock();d.input.get_text.return_value=''
        d.target=Mock();d.target.execute_js.return_value=True
        d.element=Mock(side_effect=lambda name:d.input if name=='reply_text' else d.target)
        d.tab=d.cc.edge.attach.return_value;d.tab.find_elements.return_value=[d.target]
        return d

    def test_history_returns_to_zero_and_verifies_two_stable_reads(self):
        d=self.desktop()
        with patch('jevtool.desktop.record') as log:
            self.assertTrue(d.return_to_latest('客户甲'))
        d.target.execute_js.assert_called_once_with(CHAT_RETURN_LATEST_SCRIPT,method='returnChatLatest()',timeout=3)
        self.assertEqual(d.chat_position.call_count,3)
        self.assertEqual(d.stop.wait.call_count,2)
        self.assertIn('scroll.latest.confirmed',[c.args[0] for c in log.call_args_list])
        d.target.click.assert_not_called();d.input.send_hotkey.assert_not_called()

    def test_already_latest_does_not_scroll(self):
        d=self.desktop([self.state()]);self.assertFalse(d.return_to_latest('客户甲'))
        d.target.execute_js.assert_not_called();d.cc.edge.attach.assert_not_called()

    def test_draft_blocks_scroll_without_reading_or_overwriting_input(self):
        d=self.desktop();d.input.get_text.return_value='人工草稿'
        with self.assertRaisesRegex(DesktopError,'人工草稿'):d.return_to_latest('客户甲')
        d.target.execute_js.assert_not_called();d.input.send_hotkey.assert_not_called()

    def test_unknown_position_preview_remains_nonmutating(self):
        d=self.desktop();d.chat_position.side_effect=DesktopError('无法读取滚动状态')
        self.assertFalse(d.return_to_latest('客户甲',allow_unknown_latest=True))
        d.target.execute_js.assert_not_called()

    def test_unknown_position_strict_read_stops(self):
        d=self.desktop();d.chat_position.side_effect=DesktopError('无法读取滚动状态')
        with self.assertRaises(DesktopError):d.return_to_latest('客户甲')
        d.target.execute_js.assert_not_called()

    def test_draft_keeps_manual_preview_scroll_unchanged(self):
        d=self.desktop();d.input.get_text.return_value='人工草稿'
        self.assertFalse(d.return_to_latest('客户甲',allow_unknown_latest=True))
        d.target.execute_js.assert_not_called()

    def test_unsupported_structure_never_scrolls(self):
        d=self.desktop([dict(self.state(-1948),direction='column')])
        with self.assertRaises(DesktopError):d.return_to_latest('客户甲')
        d.target.execute_js.assert_not_called()

    def test_other_platform_unchanged(self):
        d=self.desktop();d.cfg['locators']['chat_window']='other.chat_window'
        d.cfg['adapter']='generic'
        self.assertFalse(d.return_to_latest('客户甲'))
        d.customer.assert_not_called();d.chat_position.assert_not_called()

    def test_changed_customer_before_scroll_stops(self):
        d=self.desktop();d.customer.return_value='客户乙'
        with self.assertRaisesRegex(DesktopError,'客户已切换'):d.return_to_latest('客户甲')
        d.target.execute_js.assert_not_called()

    def test_nonunique_chat_stops(self):
        d=self.desktop();d.tab.find_elements.return_value=[d.target,d.target]
        with self.assertRaisesRegex(DesktopError,'不唯一'):d.return_to_latest('客户甲')
        d.target.execute_js.assert_not_called()

    def test_new_draft_before_scroll_stops(self):
        d=self.desktop();d.input.get_text.side_effect=['','新草稿']
        with self.assertRaisesRegex(DesktopError,'草稿'):d.return_to_latest('客户甲')
        d.target.execute_js.assert_not_called()

    def test_customer_switch_after_scroll_cannot_confirm(self):
        d=self.desktop();d.customer.side_effect=['客户甲','客户甲','客户甲','客户乙']
        with self.assertRaisesRegex(DesktopError,'客户已切换'):d.return_to_latest('客户甲')

    def test_stop_after_scroll_cancels_before_capture(self):
        d=self.desktop();d.stop.wait.return_value=True
        with self.assertRaises(Cancelled):d.return_to_latest('客户甲')
        self.assertEqual(d.chat_position.call_count,1)

    def test_scroll_command_failure_retains_before_and_stage(self):
        d=self.desktop();d.target.execute_js.side_effect=RuntimeError('browser failure')
        with self.assertRaises(RuntimeError) as raised:d.return_to_latest('客户甲')
        self.assertEqual(raised.exception.evidence['before']['top'],-1948)
        self.assertEqual(raised.exception.evidence['stage'],'scroll.latest')

    def test_continuous_changes_timeout_instead_of_capturing(self):
        d=self.desktop();d.chat_position.side_effect=[self.state(-1948),dict(self.state(),content='new1'),dict(self.state(),content='new2')]
        with patch('jevtool.desktop.time.monotonic',side_effect=range(10)):
            with self.assertRaisesRegex(DesktopError,'未确认稳定'):d.return_to_latest('客户甲')

    def test_send_precheck_never_prepares_or_scrolls_to_reuse_old_candidate(self):
        d=self.desktop();d.read_current=Mock(side_effect=DesktopError('聊天未在最新位置'))
        d.return_to_latest=Mock()
        with self.assertRaises(DesktopError):d.ensure_unchanged(Snapshot('客户甲',b'x','old',True,{'after':self.state()}))
        d.read_current.assert_called_once_with();d.return_to_latest.assert_not_called()


class CaptureCoordinateTests(unittest.TestCase):
    def test_css_viewport_is_projected_at_supported_scales_and_monitor_offsets(self):
        state={'capture_bounds':[200,100,800,400],'anchor_bounds':[250,40,450,60]}
        for scale in (1,1.25,1.5,2):
            for origin in ((0,0),(-1920,80)):
                anchor=tuple(origin[i%2]+v*scale for i,v in enumerate(state['anchor_bounds']))
                expected=tuple(round(origin[i%2]+v*scale) for i,v in enumerate(state['capture_bounds']))
                with self.subTest(scale=scale,origin=origin):self.assertEqual(physical_chat_bounds(state,anchor),expected)

    def test_missing_or_hidden_geometry_never_produces_a_screenshot_bound(self):
        for state in ({},{'capture_bounds':[0,0,100,100],'anchor_bounds':[0,0,0,1]},
                      {'capture_bounds':[0,0,100,float('nan')],'anchor_bounds':[0,0,20,10]}):
            with self.assertRaises(DesktopError):physical_chat_bounds(state,(0,0,20,10))

    def test_inconsistent_dpi_calibration_stops(self):
        with self.assertRaises(DesktopError):physical_chat_bounds(
            {'capture_bounds':[0,0,100,100],'anchor_bounds':[0,0,200,20]},(0,0,200,40))
