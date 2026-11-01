import json
import threading
import unittest
from unittest.mock import Mock

from jevtool.desktop import ClickniumDesktop, DesktopError, chat_is_latest
from jevtool.network import Cancelled


class ChatPositionTests(unittest.TestCase):
    def state(self, **changes):
        return dict(top=0, height=344, full=426, direction='column-reverse',
                    display='flex', overflow='auto', visible=True, **changes)

    def test_reversed_latest_and_history_and_small_rounding(self):
        s=self.state()
        self.assertTrue(chat_is_latest(s))
        for top in (-82, 82):
            self.assertFalse(chat_is_latest({**s,'top':top}))
        self.assertTrue(chat_is_latest({**s,'top':-.5}))

    def test_no_overflow_has_no_hidden_history(self):
        self.assertTrue(chat_is_latest({**self.state(),'full':344}))

    def test_invalid_or_changed_structure_stops(self):
        for change in ({'top':float('nan')},{'height':0},{'full':100},
                       {'visible':False},{'direction':'column'},{'overflow':'hidden'}):
            with self.subTest(change=change), self.assertRaises(DesktopError):
                chat_is_latest({**self.state(),**change})

    def desktop(self):
        d=ClickniumDesktop.__new__(ClickniumDesktop)
        d.stop=threading.Event();d.cc=Mock();d.resolve=Mock(return_value='recorded-chat')
        d.cc.edge.attach.return_value.find_element.return_value.execute_js.return_value=json.dumps(self.state())
        return d

    def test_position_read_uses_recorded_tab_and_no_scroll(self):
        d=self.desktop()
        self.assertEqual(d.chat_position(),self.state())
        d.cc.edge.attach.assert_called_once_with('recorded-chat',is_maximize=False,timeout=3)
        d.resolve.assert_called_once_with('current_user')
        e=d.cc.edge.attach.return_value.find_element.return_value
        e.scroll.assert_not_called();e.click.assert_not_called()

    def test_historical_position_stops(self):
        d=self.desktop()
        d.cc.edge.attach.return_value.find_element.return_value.execute_js.return_value=json.dumps({**self.state(),'top':-40})
        with self.assertRaisesRegex(DesktopError,'尚未到最新'):
            d.chat_position()

    def test_unreadable_state_stops_without_assuming_latest(self):
        d=self.desktop()
        d.cc.edge.attach.return_value.find_element.return_value.execute_js.side_effect=RuntimeError('tab missing')
        with self.assertRaisesRegex(DesktopError,'无法读取'):
            d.chat_position()

    def test_stopped_position_check_does_not_attach(self):
        d=self.desktop();d.stop.set()
        with self.assertRaises(Cancelled):d.chat_position()
        d.cc.edge.attach.assert_not_called()
