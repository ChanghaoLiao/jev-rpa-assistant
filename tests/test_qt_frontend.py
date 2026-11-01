import copy
import csv
import io
import json
import queue
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from jevtool.config import DEFAULTS
from jevtool.profiles import prepare_profiles, add_profile
from jevtool.gui import App
from jevtool.library import ReplyLibrary
from jevtool.knowledge import Knowledge, KnowledgeError
from jevtool.ui_state import UIState
from jevtool.presentation import install_style
from jevtool.vision import vision_signature
from jevtool.paths import SOURCE_ROOT


def config():
    cfg = copy.deepcopy(DEFAULTS); prepare_profiles(cfg)
    return cfg


class ReplyVersionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name); self.library = ReplyLibrary(self.root)
        self.categories = self.root / 'categories.json'; self.categories.write_text('{}')

    def tearDown(self):
        self.library.close(); self.tmp.cleanup()

    def csv(self, **changes):
        row = dict(zip(Knowledge.FIELDS, ['物流/配送', 'r1', '客户问到货', '', '一号到货。\n  原文保留', 'false', 'false']))
        row.update(changes); file = self.root / 'merchant.csv'
        with file.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, Knowledge.FIELDS); writer.writeheader(); writer.writerow(row)
        return file

    def test_import_preserves_body_and_unapproved_flags(self):
        version = self.library.import_file(self.csv(), self.categories)
        _, rows = self.library.preview(version['id'])
        self.assertEqual(rows[0]['reply_text'], '一号到货。\n  原文保留')
        self.assertFalse(rows[0]['approved_for_auto']); self.assertFalse(rows[0]['requires_human'])

    def test_activate_does_not_write_config_or_alter_sending_permissions(self):
        cfg = config(); before = copy.deepcopy(cfg)
        version = self.library.import_file(self.csv(approved_for_auto='true'), self.categories)
        result = self.library.activate_config(cfg, version['id'])
        self.assertEqual(cfg, before); self.assertEqual(result['desktop'], before['desktop'])
        self.assertEqual(result['onboarding'], before['onboarding'])
        self.assertEqual(result['knowledge']['replies_csv'], version['replies_path'])

    def test_restore_keeps_all_versions(self):
        first = self.library.import_file(self.csv(), self.categories)
        second = self.library.import_file(self.csv(reply_text='另一条固定原文'), self.categories)
        cfg = self.library.activate_config(config(), second['id'])
        restored = self.library.activate_config(cfg, first['id'])
        self.assertEqual(restored['knowledge']['replies_csv'], first['replies_path']); self.assertEqual(len(self.library.list()), 2)

    def test_external_modification_detected(self):
        v = self.library.import_file(self.csv(), self.categories)
        (self.root / v['replies_path']).write_text('tampered')
        with self.assertRaises(KnowledgeError): self.library.activate_config(config(), v['id'])

    def test_failed_import_leaves_previous_library_intact(self):
        v = self.library.import_file(self.csv(), self.categories)
        with self.assertRaises(KnowledgeError): self.library.import_file(self.csv(approved_for_auto='maybe'), self.categories)
        self.assertEqual([r['id'] for r in self.library.list()], [v['id']])
        self.assertEqual(len(list(self.library.directory.iterdir())), 1)

    def test_excel_first_sheet_boolean_and_unicode(self):
        from openpyxl import Workbook
        w = Workbook(); w.active.append(Knowledge.FIELDS)
        w.active.append(['物流/配送', 'r1', '什么时候到', '', '固定正文 \n保留空格', True, False])
        w.create_sheet('忽略的第二页').append(['bad'])
        p = self.root / 'merchant.xlsx'; w.save(p)
        v = self.library.import_file(p, self.categories); _, rows = self.library.preview(v['id'])
        self.assertTrue(rows[0]['approved_for_auto']); self.assertEqual(rows[0]['reply_text'], '固定正文 \n保留空格')

    def test_excel_formula_rejected_with_source_row(self):
        from openpyxl import Workbook
        w = Workbook(); w.active.append(Knowledge.FIELDS)
        w.active.append(['物流', 'r1', '问物流', '', '=1+1', False, False]); p = self.root / 'formula.xlsx'; w.save(p)
        with self.assertRaisesRegex(KnowledgeError, '第 2 行'): self.library.import_file(p, self.categories)

    def test_duplicate_id_does_not_create_version(self):
        p = self.csv(); p.write_bytes(p.read_bytes() + p.read_bytes().splitlines(keepends=True)[1])
        with self.assertRaises(KnowledgeError): self.library.import_file(p, self.categories)
        self.assertFalse(self.library.list())

    def test_review_creates_new_version_and_preserves_original(self):
        v = self.library.import_file(self.csv(), self.categories)
        before = (self.root / v['replies_path']).read_bytes()
        reviewed = self.library.store_review(before.replace(b'false,false', b'true,false'), v)
        self.assertNotEqual(v['id'], reviewed['id']); self.assertEqual((self.root / v['replies_path']).read_bytes(), before)

    def test_template_has_seven_headers_and_unapproved_example(self):
        p = self.root / 'template.xlsx'; self.library.export_template(p)
        rows = self.library.parse(p)
        self.assertEqual(set(rows[0]), set(Knowledge.FIELDS)); self.assertEqual(rows[0]['approved_for_auto'], 'false')

    def test_ui_progress_is_not_business_acceptance(self):
        cfg = config(); state = UIState(self.root); state.update(wizard_step=5, reduced_motion=True)
        self.assertEqual(UIState(self.root).data['wizard_step'], 5)
        self.assertFalse(cfg['desktop']['test_send_verified']); self.assertFalse(cfg['onboarding']['vision_verified'])


class IdleWorker:
    def __init__(self):
        self.commands = queue.Queue(); self.stop_event = threading.Event(); self.exit_event = threading.Event(); self.monitor = False
    def is_alive(self): return False


class NativeInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([]); install_style(cls.qt)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.state = UIState(Path(self.tmp.name))
        with patch('jevtool.controller.UIState', return_value=self.state):
            self.app = App(cfg=config(), worker=IdleWorker(), autostart=False)
        self.app.timer.stop(); self.app.window.show(); self.qt.processEvents()

    def tearDown(self):
        self.app.closed = True
        for d in list(self.app.dialogs): d.close()
        self.app.window.close(); self.qt.processEvents(); self.tmp.cleanup()

    def commands(self):
        out = []
        while not self.app.worker.commands.empty(): out.append(self.app.worker.commands.get_nowait())
        return out

    def test_opening_home_never_enqueues_monitor_or_send(self):
        self.app.window.navigate('home'); self.assertFalse(any(c[0] in ('monitor', 'send') for c in self.commands()))

    def test_save_button_does_not_pick_image_or_call_api(self):
        self.app.window.set_step(0)
        with patch.object(self.app, 'persist_config') as save, patch.object(self.app, 'choose_image') as choose:
            buttons = self.app.window.guarded_buttons
            target = next(b for b in buttons if b.text() == '保存设置')
            QTest.mouseClick(target, Qt.LeftButton)
        save.assert_called_once(); choose.assert_not_called()
        self.assertFalse(any(c[0] in ('vision_test', 'jev_check') for c in self.commands()))

    def test_cancel_import_preserves_current_path(self):
        before = copy.deepcopy(self.app.cfg)
        with patch('jevtool.gui.filedialog.askopenfilename', return_value=''): self.app.import_replies()
        self.assertEqual(self.app.cfg, before); self.assertFalse(any(c[0] == 'library_import' for c in self.commands()))

    def test_stale_same_profile_result_cannot_restore_send_candidate(self):
        self.app.handle_event({'kind': 'decision', 'profile': self.app.cfg['active_profile'], 'generation': 0,
            'id': 'old', 'decision': {'action': 'candidate'}})
        self.assertIsNone(self.app.current_id)

    def test_stale_other_platform_result_ignored(self):
        self.app.handle_event({'kind': 'decision', 'profile': 'different', 'id': 'old', 'decision': {'action': 'candidate'}})
        self.assertIsNone(self.app.current_id)

    def test_human_reason_is_visible_not_false_unread_prompt(self):
        self.app.handle_event({'kind': 'decision', 'id': None, 'customer': 'test :: 1 :: 买家甲', 'source': '当前会话',
            'decision': {'action': 'human', 'path': ['物流'], 'text': '', 'reason': 'Jev 主动选择人工', 'trace': []}})
        self.app.wizard_send()
        self.assertIn('Jev 主动选择人工', self.app.window.feedback.text()); self.assertFalse(any(c[0] == 'send' for c in self.commands()))

    def test_preview_only_reason_remains_visible(self):
        self.app.handle_event({'kind': 'decision', 'id': None, 'customer': '买家甲', 'source': '白名单不发送预览',
            'decision': {'action': 'candidate', 'path': ['物流'], 'text': '固定回复', 'reason': '', 'trace': []}})
        self.assertFalse(self.app.window.test_send_button.isEnabled()); self.assertIn('不发送预览', self.app.window.send_reason.text())

    def test_stop_discards_queued_send(self):
        self.app.worker.commands.put(('send', 'old', None)); self.app.current_id = 'old'; self.app.stop()
        self.assertTrue(self.app.worker.stop_event.is_set()); self.assertIsNone(self.app.current_id)
        self.assertEqual([c[0] for c in self.commands()], ['stop'])

    def test_stop_rejects_late_candidate_from_previous_operation(self):
        before = self.app.generation
        self.app.stop()
        self.app.handle_event({'kind': 'decision', 'profile': self.app.cfg['active_profile'],
            'generation': before, 'id': 'late', 'decision': {'action': 'candidate'}})
        self.assertIsNone(self.app.current_id)
        self.assertFalse(self.app.window.test_send_button.isEnabled())

    def test_stop_completion_updates_visible_feedback(self):
        self.app.stop()
        self.app.handle_event({'kind': 'command_done', 'command': 'stop', 'generation': self.app.generation, 'ok': True})
        self.assertIsNone(self.app.busy)
        self.assertTrue(self.app.window.feedback.text().startswith('已停止'))

    def test_next_step_does_not_grant_acceptance(self):
        self.app.window.set_step(4); before = copy.deepcopy(self.app.cfg); self.app.window.next_step()
        self.assertEqual(self.app.window.wizard_pages.currentIndex(), 4); self.assertEqual(self.app.cfg, before)

    def test_failed_test_send_does_not_pass_acceptance(self):
        self.app.expect_test_send = True
        self.app.handle_event({'kind': 'send_unknown', 'text': '发送结果未知。'})
        self.assertFalse(self.app.expect_test_send); self.assertFalse(self.app.cfg['desktop']['test_send_verified'])

    def test_live_connection_is_not_installed_state(self):
        self.assertIn('尚未检查', self.app.window.connection_label.text())
        self.app.handle_event({'kind': 'doctor', 'result': {**dict.fromkeys(('current_user', 'chat_window', 'reply_text', 'button_send'), '唯一匹配'),
            'customer_readable': True, 'input_readable': True, 'chat_capture_valid': True}})
        self.assertIn('本次检查通过', self.app.window.connection_label.text())

    def test_new_validation_round_clears_old_samples(self):
        self.app.unread_samples = dict(empty=0, unread=1, read=0)
        with patch.object(self.app, 'connect_recording', return_value=False): self.app.doctor('empty')
        self.assertEqual(self.app.unread_samples, {})

    def recording_root(self, *names):
        from test_recording import write_store
        root = Path(self.tmp.name)
        self.app.cfg['profiles'][self.app.cfg['active_profile']]['kind'] = 'web'
        for name in names: write_store(root, name)
        (root / 'config.local.json').write_text(json.dumps(self.app.cfg), encoding='utf-8')
        return root

    def test_recording_binding_waits_for_configure_then_checks_without_sending(self):
        root = self.recording_root('goofish')
        with patch('jevtool.controller.DATA_ROOT', root):
            self.app.doctor('empty')
            self.assertEqual([c[0] for c in self.commands()], ['stop', 'configure'])
            self.assertIsNotNone(self.app.recording_check_pending)
            self.app.handle_event({'kind': 'command_done', 'command': 'configure', 'ok': True,
                'generation': self.app.generation, 'profile': self.app.cfg['active_profile']})
        commands = self.commands()
        self.assertEqual([c[0] for c in commands], ['stop', 'doctor'])
        self.assertEqual(commands[-1][1], 'empty'); self.assertIsNone(self.app.recording_check_pending)
        self.assertFalse(self.app.cfg['desktop']['locators_reviewed'])
        self.assertFalse(self.app.worker.monitor); self.assertIsNone(self.app.current_id)
        self.assertEqual(len(list((root / 'backups').rglob('config.local.json'))), 1)

    def test_cancel_recording_choice_preserves_config_and_queues_nothing(self):
        root = self.recording_root('one', 'two'); before = copy.deepcopy(self.app.cfg)
        original = (root / 'config.local.json').read_bytes()
        with patch('jevtool.controller.DATA_ROOT', root), patch('jevtool.controller.QInputDialog.getItem', return_value=('', False)):
            self.app.doctor()
        self.assertEqual(self.app.cfg, before); self.assertEqual((root / 'config.local.json').read_bytes(), original)
        self.assertEqual(self.commands(), []); self.assertIn('已取消', self.app.window.feedback.text())

    def test_stop_cancels_delayed_recording_check(self):
        root = self.recording_root('goofish')
        with patch('jevtool.controller.DATA_ROOT', root): self.app.doctor()
        previous = self.app.generation; self.commands(); self.app.stop()
        self.app.handle_event({'kind': 'command_done', 'command': 'configure', 'ok': True,
            'generation': previous, 'profile': self.app.cfg['active_profile']})
        self.assertEqual([c[0] for c in self.commands()], ['stop']); self.assertIsNone(self.app.recording_check_pending)

    def test_failed_reconfiguration_does_not_schedule_page_check(self):
        root = self.recording_root('goofish')
        with patch('jevtool.controller.DATA_ROOT', root): self.app.doctor()
        self.commands()
        self.app.handle_event({'kind': 'command_done', 'command': 'configure', 'ok': False,
            'generation': self.app.generation, 'profile': self.app.cfg['active_profile']})
        self.assertFalse(any(c[0] == 'doctor' for c in self.commands()))
        self.assertIsNone(self.app.recording_check_pending)

    def test_existing_recording_binding_does_not_save_or_reset_flags(self):
        root = self.recording_root('aliworkbench')
        before = copy.deepcopy(self.app.cfg)
        with patch('jevtool.controller.DATA_ROOT', root), patch.object(self.app, 'persist_config') as persist:
            self.app.doctor()
        persist.assert_not_called(); self.assertEqual(self.app.cfg, before)
        self.assertEqual([c[0] for c in self.commands()], ['stop', 'doctor'])

    def test_xianyu_existing_unread_baseline_can_pass_validation(self):
        self.app.cfg['desktop']['adapter'] = 'xianyu_web'
        self.app.doctor_passed = True
        self.app.unread_samples = dict(empty=3, unread=4, read=3)
        with patch.object(self.app, 'persist_config') as persist:
            self.app.confirm_locators()
        self.assertTrue(persist.call_args.args[0]['desktop']['unread_locator_reviewed'])

    def test_unchanged_or_unreturned_unread_counts_do_not_pass(self):
        self.app.cfg['desktop']['adapter'] = 'xianyu_web'
        self.app.doctor_passed = True
        for samples in (dict(empty=3,unread=3,read=3), dict(empty=3,unread=4,read=4)):
            self.app.unread_samples = samples
            with patch.object(self.app, 'persist_config') as persist:
                self.app.confirm_locators()
            self.assertFalse(persist.call_args.args[0]['desktop']['unread_locator_reviewed'])

    def test_normalization_feedback_invalidates_only_current_preview(self):
        self.app.unread_samples = dict(empty=0, unread=1, read=0)
        self.app.current_id = 123
        self.app.handle_event({'kind':'recording_prepared','status':'prepared','text':'规则已整理',
            'changed':True,'desktop':{'adapter':'xianyu_web','unread_rule_hash':'a'*64,
                'unread_locator_reviewed':False,'test_send_verified':False,'auto_send_enabled':False}})
        self.assertEqual(self.app.unread_samples,{}); self.assertIsNone(self.app.current_id)
        self.assertEqual(self.app.cfg['profiles'][self.app.cfg['active_profile']]['adapter'],'xianyu_web')

    def test_recording_completed_queues_preparation_and_page_check(self):
        self.app.window.set_step(1)
        with patch.object(self.app, 'connect_recording', return_value=False):
            button=next(b for b in self.app.window.guarded_buttons if b.text()=='录制完成，自动整理并检查')
            QTest.mouseClick(button, Qt.LeftButton)
        self.assertEqual([c[0] for c in self.commands()], ['stop','doctor'])

    def test_missing_recordings_explain_failure_before_browser_access(self):
        root = self.recording_root()
        with patch('jevtool.controller.DATA_ROOT', root): self.app.call(self.app.doctor)
        self.assertIn('未找到', self.app.window.feedback.text()); self.assertFalse(self.commands())

    def test_manual_done_does_not_call_resume(self):
        self.app.manual_done('test :: 1 :: 买家甲')
        self.assertEqual([c[0] for c in self.commands()], ['manual_done'])

    def test_reduced_motion_survives_restart(self):
        self.app.window.motion_check.setChecked(True)
        self.assertTrue(UIState(Path(self.tmp.name)).data['reduced_motion'])

    def test_narrow_window_keeps_stop_visible_and_folds_pending(self):
        self.app.window.navigate('home'); self.app.window.resize(1024, 720); self.qt.processEvents()
        self.assertTrue(self.app.window.top_stop.isVisible()); self.assertFalse(self.app.window.pending_panel.isVisible())
        self.assertTrue(self.app.window.pending_compact.isVisible())

    def test_keyboard_focus_reaches_stop(self):
        self.app.window.top_stop.setFocus(); self.assertTrue(self.app.window.top_stop.hasFocus())

    def test_large_history_dialog_fits_narrow_window_and_keeps_stop(self):
        self.app.window.resize(1024, 720); self.qt.processEvents()
        self.app.window.show_vision({'customer': 'test :: 1 :: 买家甲', 'created': time.time(),
            'source': '模拟预览', 'status': 'ok', 'image_png': None})
        dialog = self.app.dialogs[-1]; self.qt.processEvents()
        from PySide6.QtWidgets import QPushButton
        stop = dialog.findChild(QPushButton, 'dialogStop')
        self.assertLessEqual(dialog.width(), self.app.window.width())
        self.assertLessEqual(dialog.height(), self.app.window.height())
        self.assertTrue(stop.isVisible()); self.assertTrue(dialog.rect().contains(stop.mapTo(dialog, stop.rect().bottomRight())))

    def test_long_price_form_fits_narrow_window(self):
        self.app.window.resize(1024, 720); self.qt.processEvents()
        self.app.window.show_rates({}); self.qt.processEvents()
        dialog = self.app.dialogs[-1]
        self.assertLessEqual(dialog.height(), self.app.window.height())

    def test_zero_price_remains_explicit_zero(self):
        self.app.window.show_rates({'minimax': {'input': 0, 'output': None, 'cached': 0}})
        from PySide6.QtWidgets import QLineEdit
        fields = self.app.dialogs[-1].findChildren(QLineEdit)
        self.assertEqual([x.text() for x in fields[:3]], ['0', '', '0'])

    def test_reply_export_preserves_current_version_and_configuration(self):
        source = Path(self.tmp.name) / 'current.csv'; source.write_bytes(b'original fixed replies')
        destination = Path(self.tmp.name) / 'editing.csv'; before = copy.deepcopy(self.app.cfg)
        with patch('jevtool.controller.knowledge_file', return_value=source), patch('jevtool.gui.filedialog.asksaveasfilename', return_value=str(destination)):
            self.app.export_replies()
        self.assertEqual(destination.read_bytes(), source.read_bytes()); self.assertEqual(before, self.app.cfg)
        self.assertFalse(any(c[0] in ('monitor', 'send', 'configure') for c in self.commands()))

    def test_vision_confirmation_requires_matching_signature(self):
        self.app.can_confirm_vision = True; self.app.vision_test_signature = 'old'
        with self.assertRaises(ValueError): self.app.confirm_vision()
        self.assertFalse(self.app.cfg['onboarding']['vision_verified'])

    def test_page_change_retains_user_flags(self):
        before = copy.deepcopy(self.app.cfg)
        for key in ('home', 'history', 'cost', 'settings', 'wizard'): self.app.window.navigate(key)
        self.assertEqual(before, self.app.cfg)

    def test_stop_completion_unlocks_buttons_after_discarded_task(self):
        self.app.busy = 'current'; self.app.worker.commands.put(('current', None, None)); self.app.stop()
        self.app.handle_event({'kind': 'command_done', 'command': 'stop', 'ok': True})
        self.assertIsNone(self.app.busy)

    def test_startup_resumes_saved_step_without_granting_checks(self):
        cfg = config(); cfg['jev']['api_key'] = 'fake-key'; cfg['vision']['api_key'] = 'fake-vision'
        cfg['onboarding']['jev_verified'] = True; cfg['desktop'].update(locators_reviewed=True, unread_locator_reviewed=True)
        self.state.update(wizard_step=3)
        with patch('jevtool.controller.UIState', return_value=self.state):
            other = App(cfg=cfg, worker=IdleWorker(), autostart=False)
        self.assertEqual(other.window.wizard_pages.currentIndex(), 3)
        self.assertFalse(other.cfg['desktop']['test_send_verified'])
        other.timer.stop(); other.closed = True; other.window.close()

    def test_closed_pending_dialog_does_not_leave_dead_controls(self):
        before = len(self.app.window.guarded_buttons)
        self.app.window.pending_details({'customer': 'test :: 1 :: 买家甲', 'reason': '测试', 'possibly_sent': False})
        dialog = self.app.dialogs[-1]; dialog.close()
        from PySide6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.refresh_setup()
        self.assertEqual(before, len(self.app.window.guarded_buttons))

    def test_serial_worker_end_to_end_preview_confirm_send_and_history(self):
        from jevtool.service import Service
        from jevtool.store import Store
        from jevtool.desktop import Snapshot
        from jevtool.router import Decision
        from jevtool.vision import parse_transcript, image_digest
        import test_core
        png = test_core.png_bytes(); snapshot = Snapshot('模拟买家', png, image_digest(png), True)
        desktop = Mock(); desktop.read_current.return_value = snapshot
        reader = Mock()
        before = parse_transcript({'readable': True, 'at_bottom': True, 'has_unsupported_content': False, 'messages': [{'role': 'buyer', 'text': '在哪里看物流', 'timestamp': ''}]})
        after = parse_transcript({'readable': True, 'at_bottom': True, 'has_unsupported_content': False, 'messages': [*before.messages, {'role': 'seller', 'text': '请查看订单物流。', 'timestamp': ''}]})
        reader.read.side_effect = [before, after]
        router = Mock(); router.decide.return_value = Decision('candidate', 'r1', '请查看订单物流。', '匹配', ['物流'], [], True)
        ledger = Path(self.tmp.name) / 'worker-events.sqlite3'
        cfg = config(); cfg['desktop'].update(data_upload_authorized=True, locators_reviewed=True, unread_locator_reviewed=True, settle_seconds=.5)
        def service_factory(configuration, stop, emit):
            return Service(configuration, stop, emit, desktop=desktop, router=router, reader=reader, store=Store(ledger))
        with patch('jevtool.worker.Service', side_effect=service_factory), patch('jevtool.controller.UIState', return_value=self.state):
            running = App(cfg=cfg)
            running.timer.stop()
            def until(predicate):
                deadline = time.monotonic()+5
                while time.monotonic() < deadline:
                    running.drain(); self.qt.processEvents()
                    if predicate(): return
                    time.sleep(.01)
                self.fail('worker did not complete simulated flow: ' + running.window.feedback.text())
            try:
                until(lambda: running.busy is None)
                running.command('current'); until(lambda: running.busy is None and running.current_id is not None)
                self.assertIn('请查看订单物流。', running.window.preview_result.toPlainText())
                with patch.object(running, 'confirm', return_value=True): running.wizard_send()
                until(lambda: running.busy is None and running.cfg['desktop']['test_send_verified'])
                desktop.send_reply.assert_called_once()
                self.assertEqual(desktop.send_reply.call_args.args[1], '请查看订单物流。')
                self.assertFalse(running.worker.monitor)
                self.assertTrue(running.window.test_send_button.isEnabled() is False)
            finally:
                running.close(); running.worker.join(5); running.closed = True; running.window.close()
        import sqlite3
        with closing(sqlite3.connect(ledger)) as connection:
            self.assertEqual(connection.execute('SELECT status FROM events').fetchone()[0], 'sent_ui_verified')


if __name__ == '__main__': unittest.main()
