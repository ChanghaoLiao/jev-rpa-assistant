import threading
import unittest
from unittest.mock import Mock
from unittest.mock import patch
import subprocess

from jevtool.desktop import Snapshot
from jevtool.router import Decision
from xianyu_draft_test import (TrialError, _installed_app_running, buyer_name_from_row,
                               main, open_first_unread, paste_draft, run_trial)


def row_html(name, badge=True, width=176):
    marker = '<sup class="ant-badge-count">1</sup>' if badge else ''
    return ('<div class="conversation-item--abc">' + marker +
            '<div style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap; '
            f'max-width: {width}px;">' + name + '</div>'
            '<div>消息预览不能用于匹配买家</div></div>')


class FakeRow:
    def __init__(self, name, top):
        self.name, self.top = name, top
        self.click = Mock()

    def get_property(self, name):
        assert name == 'outerHTML'
        return row_html(self.name)

    def get_position(self, **_):
        return type('Position', (), dict(Left=0, Top=self.top, Right=330, Bottom=self.top + 68))()


class FakeDesktop:
    def __init__(self, rows):
        self.rows = rows
        self.cc = Mock()
        self.cc.find_elements.return_value = rows
        self.cfg = {'settle_seconds': .5, 'poll_seconds': 1}
        self.stop = threading.Event()
        self.check_stop = Mock()
        self.customer = Mock(return_value='测试买家')

    def resolve(self, name):
        assert name == 'new_message'
        return 'mock.new_message'


class DraftTrialTests(unittest.TestCase):
    def test_name_is_exact_field_not_message_preview(self):
        self.assertEqual(buyer_name_from_row(row_html('测试买家')), '测试买家')
        self.assertEqual(buyer_name_from_row(row_html('测试买家', width=180)), '测试买家')
        with self.assertRaises(TrialError):
            buyer_name_from_row('<div>测试买家</div>')

    def test_only_top_unread_row_is_clicked(self):
        target, other = FakeRow('测试买家', 100), FakeRow('其他买家', 168)
        desktop = FakeDesktop([other, target])
        self.assertEqual(open_first_unread(desktop), '测试买家')
        target.click.assert_called_once_with()
        other.click.assert_not_called()

    def test_ambiguous_or_changed_list_never_clicks(self):
        rows = [FakeRow('测试买家', 100), FakeRow('其他买家', 100)]
        desktop = FakeDesktop(rows)
        with self.assertRaises(TrialError):
            open_first_unread(desktop)
        for row in rows:
            row.click.assert_not_called()

        target = FakeRow('测试买家', 100)
        desktop = FakeDesktop([target])
        desktop.cc.find_elements.side_effect = [[target], [FakeRow('测试买家', 101)]]
        with self.assertRaises(TrialError):
            open_first_unread(desktop)
        target.click.assert_not_called()

    def test_waits_for_new_unread_then_opens_one(self):
        target = FakeRow('测试买家', 100)
        desktop = FakeDesktop([])
        desktop.stop.wait = Mock(return_value=False)
        desktop.cc.find_elements.side_effect = [[], [target], [target]]
        self.assertEqual(open_first_unread(desktop, watch_seconds=10), '测试买家')
        target.click.assert_called_once_with()
        desktop.stop.wait.assert_any_call(1)

    def test_paste_draft_leaves_text_and_never_accesses_send(self):
        desktop = FakeDesktop([])
        desktop.ensure_unchanged = Mock()
        desktop.clipboard = Mock()
        draft = {'text': ''}
        input_box = Mock()
        input_box.get_property.return_value = '<textarea></textarea>'
        input_box.get_text.side_effect = lambda **_: draft['text']
        input_box.send_hotkey.side_effect = lambda _: draft.update(text='固定回复')
        desktop.element = Mock(side_effect=lambda name: input_box if name == 'reply_text' else
                               (_ for _ in ()).throw(AssertionError('send button accessed')))
        snapshot = Snapshot('测试买家', b'png', 'digest')
        paste_draft(desktop, snapshot, '固定回复')
        self.assertEqual(draft['text'], '固定回复')
        input_box.send_hotkey.assert_called_once_with('^v')
        desktop.element.assert_called_once_with('reply_text')
        desktop.ensure_unchanged.assert_called()

    def test_existing_draft_or_changed_chat_stops_before_paste(self):
        desktop = FakeDesktop([])
        desktop.ensure_unchanged = Mock()
        desktop.clipboard = Mock()
        input_box = Mock()
        input_box.get_property.return_value = '<textarea></textarea>'
        input_box.get_text.return_value = '人工草稿'
        desktop.element = Mock(return_value=input_box)
        with self.assertRaises(TrialError):
            paste_draft(desktop, Snapshot('测试买家', b'png', 'digest'), '固定回复')
        input_box.send_hotkey.assert_not_called()

        desktop.ensure_unchanged.side_effect = RuntimeError('chat changed')
        with self.assertRaises(RuntimeError):
            paste_draft(desktop, Snapshot('测试买家', b'png', 'digest'), '固定回复')
        input_box.send_hotkey.assert_not_called()

    def test_refuses_concurrent_app_and_unknown_process_state(self):
        with patch('xianyu_draft_test._installed_app_running', return_value=True):
            with self.assertRaisesRegex(TrialError, '关闭当前客服工具'):
                main([])
        with patch('xianyu_draft_test.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'tasklist')):
            with self.assertRaisesRegex(TrialError, '无法确认客服工具'):
                _installed_app_running()
        with patch('xianyu_draft_test.subprocess.run', return_value=Mock(stdout='"JevReplyTool.exe","41296"\n')):
            self.assertTrue(_installed_app_running())
        with patch('xianyu_draft_test.subprocess.run', return_value=Mock(stdout='没有匹配的任务\n')):
            self.assertFalse(_installed_app_running())

    def test_whole_trial_pastes_without_calling_send(self):
        row = FakeRow('测试买家', 100)
        desktop = FakeDesktop([row])
        service = Mock()
        service.preview_current.return_value = True
        service.pending = {'candidate': (Snapshot('测试买家', b'png', 'digest'),
                                         Decision('candidate', text='固定回复'), 'test-key')}
        with patch('xianyu_draft_test.paste_draft') as paste:
            run_trial(desktop, service)
        row.click.assert_called_once_with()
        service.preview_current.assert_called_once_with(auto=False)
        paste.assert_called_once()
        service.send.assert_not_called()

        service.preview_current.return_value = False
        with patch('xianyu_draft_test.paste_draft') as paste:
            with self.assertRaises(TrialError):
                run_trial(desktop, service)
            paste.assert_not_called()

if __name__ == '__main__':
    unittest.main()
