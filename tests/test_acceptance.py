import copy
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import test_core
from jevtool.acceptance import AcceptanceFlow
from jevtool.config import load_config
from jevtool.desktop import Snapshot, DesktopError
from jevtool.vision import image_digest, vision_signature
from jevtool.router import Decision


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        test_core.ServiceTests.setUp(self)
        from jevtool.profiles import prepare_profiles
        prepare_profiles(self.cfg)
        self.cfg['desktop']['auto_send_enabled'] = False
        self.file = Path(self.tmp.name) / 'chat.png'; self.file.write_bytes(self.png)
        self.config_file = Path(self.tmp.name) / 'config.json'
        from jevtool.config import save_config
        self.saver = patch('jevtool.acceptance.save_config', side_effect=lambda cfg: save_config(cfg, self.config_file))
        self.saver.start()
        self.flow = AcceptanceFlow(self.s); self.data = self.flow.load(); self.flow.save(self.data, 'opened')
        self.id = self.data['id']
        self.desktop.read_current.return_value = Snapshot('客户甲', self.png, image_digest(self.png), True)

    def tearDown(self):
        self.saver.stop(); test_core.ServiceTests.tearDown(self)

    def run_action(self, action, **args):
        result = self.flow.execute(dict(action=action, flow_id=self.id, **args))
        self.data = result['data']; return result

    def image_ready(self):
        self.run_action('pick', path=str(self.file)); self.run_action('ocr'); self.run_action('vision_confirm')

    def send_ready(self):
        self.image_ready(); self.run_action('route'); self.run_action('route_confirm'); self.run_action('read')
        return self.data['live']['id']

    def test_pick_is_local_and_ocr_only_calls_reader(self):
        self.run_action('pick', path=str(self.file))
        self.reader.read.assert_not_called(); self.router.decide.assert_not_called()
        self.run_action('ocr')
        self.reader.read.assert_called_once(); self.router.decide.assert_not_called()
        self.assertEqual(self.data['vision']['image_png'], self.png)
        self.assertFalse(self.s.pending)

    def test_route_reuses_exact_saved_transcript_without_reader(self):
        self.image_ready(); self.reader.read.reset_mock(); self.run_action('route')
        self.reader.read.assert_not_called()
        self.router.decide.assert_called_once_with(json.loads(self.data['vision']['transcript_json']))
        self.assertFalse(self.s.pending); self.assertNotIn('live', self.data)

    def test_consent_is_inline_and_required_before_ocr(self):
        self.s.cfg['desktop']['data_upload_authorized'] = False
        self.run_action('pick', path=str(self.file))
        with self.assertRaisesRegex(DesktopError, '未授权'): self.run_action('ocr')
        self.reader.read.assert_not_called()
        self.run_action('authorize'); self.run_action('ocr')
        self.assertTrue(load_config(self.config_file)['desktop']['data_upload_authorized'])

    def test_save_acceptance_retains_pending_and_unrelated_configuration(self):
        self.image_ready()
        self.s.preview_current(manual_preview=True); before = copy.deepcopy(self.s.cfg)
        pending = dict(self.s.pending); reader = self.s.reader; router = self.s.router
        self.run_action('vision_confirm')
        self.assertEqual(self.s.pending, pending); self.assertIs(self.s.reader, reader); self.assertIs(self.s.router, router)
        self.assertEqual(before['knowledge'], self.s.cfg['knowledge'])
        self.assertEqual(before['desktop'], self.s.cfg['desktop'])

    def test_return_and_restart_use_saved_results_without_api(self):
        self.image_ready(); self.run_action('route'); self.run_action('route_confirm')
        self.reader.read.reset_mock(); self.router.decide.reset_mock()
        self.run_action('page', step=0); self.run_action('page', step=2)
        result = AcceptanceFlow(self.s).execute({'action': 'load'})
        self.assertEqual(result['data']['id'], self.id); self.assertEqual(result['data']['step'], 2)
        self.reader.read.assert_not_called(); self.router.decide.assert_not_called()

    def test_human_can_be_reviewed_but_never_creates_send_candidate(self):
        self.image_ready(); self.router.decide.return_value = Decision('human', reason='本话术库没有对应回复')
        self.run_action('route'); self.run_action('route_confirm')
        self.assertEqual(self.data['step'], 2); self.assertFalse(self.s.pending)
        with self.assertRaises(ValueError): self.run_action('send', candidate_id='invented')
        self.desktop.send_reply.assert_not_called()

    def test_repeat_read_refreshes_unsent_candidate_without_deleting_evidence(self):
        id_ = self.send_ready(); count = self.store.db.execute('SELECT COUNT(*) FROM vision_audit').fetchone()[0]
        self.run_action('read')
        self.assertEqual(self.data['live']['id'], id_)
        self.assertEqual(self.store.status(id_), 'preview')
        self.assertGreater(self.store.db.execute('SELECT COUNT(*) FROM vision_audit').fetchone()[0], count)
        self.desktop.send_reply.assert_not_called()

    def test_restart_candidate_is_display_only_until_fresh_read(self):
        id_ = self.send_ready(); self.s.pending.clear(); self.s.pending_transcripts.clear()
        self.run_action('load'); self.assertIsNone(self.data['live']['id'])
        with self.assertRaises(ValueError): self.run_action('send', candidate_id=id_)
        self.run_action('read'); self.assertEqual(self.data['live']['id'], id_)

    def test_sent_record_cannot_be_recreated_after_manual_refresh(self):
        id_ = self.send_ready(); self.store.set_status(id_, 'sent_ui_verified'); self.s.pending.clear()
        self.run_action('read'); self.assertIsNone(self.data['live']['id'])
        self.assertEqual(self.store.status(id_), 'sent_ui_verified')

    def test_seen_frame_with_changed_ocr_cannot_create_repeat_candidate(self):
        id_ = self.send_ready(); self.store.set_status(id_, 'sent_ui_verified'); self.s.pending.clear()
        self.reader.read.return_value = test_core.transcript(text='识字发生细微变化')
        self.run_action('read'); self.assertIsNone(self.data['live']['id'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)

    def test_blocked_and_whitelist_and_unknown_position_stay_unsendable(self):
        self.send_ready(); customer = self.s.customer_key('客户甲')
        self.store.block(customer, '发送结果未知')
        self.run_action('read'); self.assertIsNone(self.data['live']['id'])
        self.assertEqual(self.store.is_blocked(customer), '发送结果未知')
        self.store.db.execute('INSERT INTO preview_whitelist VALUES (?)', (customer,)); self.store.db.commit()
        self.run_action('read'); self.assertIsNone(self.data['live']['id'])
        self.store.resume(customer); self.store.db.execute('DELETE FROM preview_whitelist'); self.store.db.commit()
        self.desktop.read_current.return_value = self.snap
        self.run_action('read'); self.assertIsNone(self.data['live']['id'])

    def test_success_requires_actual_event_confirmation(self):
        id_ = self.send_ready()
        self.reader.read.return_value = test_core.parse_transcript({'readable': True, 'at_bottom': True,
            'has_unsupported_content': False, 'messages': [*test_core.transcript().messages,
                {'role': 'seller', 'text': '固定原文', 'timestamp': ''}]})
        self.run_action('send', candidate_id=id_)
        self.desktop.send_reply.assert_called_once()
        self.assertEqual(self.data['live']['status'], 'sent_ui_verified')
        self.assertTrue(self.s.cfg['desktop']['test_send_verified'])
        self.assertFalse(self.s.cfg['desktop']['auto_send_enabled'])
        with self.assertRaises(ValueError): self.run_action('send', candidate_id=id_)
        self.assertEqual(self.desktop.send_reply.call_count, 1)

    def test_unknown_send_does_not_pass_and_remains_paused(self):
        id_ = self.send_ready(); self.run_action('send', candidate_id=id_)
        self.assertEqual(self.data['live']['status'], 'send_unknown')
        self.assertFalse(self.s.cfg['desktop']['test_send_verified'])
        self.assertTrue(self.store.is_blocked(self.s.customer_key('客户甲')))

    def test_metadata_write_failure_does_not_grant_acceptance(self):
        self.run_action('pick', path=str(self.file)); self.run_action('ocr')
        with patch('jevtool.acceptance.save_config', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.run_action('vision_confirm')
        self.assertFalse(self.s.cfg['onboarding']['vision_verified'])

    def test_service_change_invalidates_affected_result(self):
        self.image_ready(); self.run_action('route'); self.run_action('route_confirm')
        self.s.cfg['jev']['api_key'] = 'different-test-key'
        self.run_action('load')
        self.assertTrue(self.data['vision_reviewed']); self.assertNotIn('route_id', self.data)
        self.s.cfg['vision']['api_key'] = 'new-vision-key'; self.run_action('load')
        self.assertNotIn('vision_id', self.data); self.assertEqual(self.data['step'], 0)

    def test_platform_and_account_cannot_reuse_flow(self):
        self.image_ready(); self.s.cfg['desktop']['shop_id'] = 'other-account'
        with self.assertRaises(ValueError): self.run_action('load')
        self.assertNotEqual(self.flow.load()['id'], self.id)

    def test_failed_reread_cannot_restore_old_candidate(self):
        id_ = self.send_ready(); self.desktop.read_current.side_effect = DesktopError('买家名无法读取')
        with self.assertRaises(DesktopError): self.run_action('read')
        self.assertNotIn(id_, self.s.pending)
        self.assertIsNone(self.flow.present(self.flow.load(self.id))['data']['live']['id'])

    def test_failed_ocr_retains_its_evidence_but_cannot_be_approved(self):
        self.run_action('pick', path=str(self.file)); self.reader.read.side_effect = ValueError('无法识字')
        with self.assertRaises(ValueError): self.run_action('ocr')
        result = self.flow.present(self.flow.load(self.id))['data']
        self.assertEqual(result['vision']['status'], 'vision_failed')
        self.assertEqual(result['vision']['image_png'], self.png)
        with self.assertRaises(ValueError): self.run_action('vision_confirm')

    def test_confirmed_send_metadata_retry_never_sends_again(self):
        id_ = self.send_ready()
        self.reader.read.return_value = test_core.parse_transcript({'readable': True, 'at_bottom': True,
            'has_unsupported_content': False, 'messages': [*test_core.transcript().messages,
                {'role': 'seller', 'text': '固定原文', 'timestamp': ''}]})
        with patch('jevtool.acceptance.save_config', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.run_action('send', candidate_id=id_)
        self.assertFalse(self.s.cfg['desktop']['test_send_verified'])
        self.run_action('load'); self.assertTrue(self.s.cfg['desktop']['test_send_verified'])
        self.desktop.send_reply.assert_called_once()

    def test_failure_text_is_redacted_in_saved_flow(self):
        from jevtool.audit import register_secrets
        self.s.cfg['vision']['api_key'] = 'unit-private-key'; register_secrets(self.s.cfg)
        self.run_action('pick', path=str(self.file)); self.reader.read.side_effect = ValueError('echo unit-private-key')
        with self.assertRaises(ValueError): self.run_action('ocr')
        saved = self.store.db.execute('SELECT data_json FROM acceptance_flows WHERE id=?', (self.id,)).fetchone()[0]
        self.assertNotIn('unit-private-key', saved); self.assertIn('[REDACTED]', saved)


class AcceptanceGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from jevtool.presentation import install_style
        cls.qt = QApplication.instance() or QApplication([]); install_style(cls.qt)

    def setUp(self):
        from test_qt_frontend import config, IdleWorker
        from jevtool.controller import App
        from jevtool.ui_state import UIState
        self.tmp = tempfile.TemporaryDirectory()
        with patch('jevtool.controller.UIState', return_value=UIState(Path(self.tmp.name))):
            self.app = App(cfg=config(), worker=IdleWorker(), autostart=False)
        self.app.timer.stop()

    def tearDown(self):
        self.app.closed = True; self.app.window.close(); self.qt.processEvents(); self.tmp.cleanup()

    def test_cancel_pick_preserves_current_results(self):
        self.app.acceptance = {'id': 'existing', 'image_id': 'image'}
        with patch('jevtool.controller.filedialog.askopenfilename', return_value=''): self.app.acceptance_pick()
        self.assertEqual(self.app.acceptance['image_id'], 'image'); self.assertTrue(self.app.worker.commands.empty())

    def test_initial_upload_page_hides_future_results_and_never_claims_acceptance(self):
        self.app.window.set_step(4); self.app.acceptance_action_name = 'load'
        self.app.handle_event({'kind': 'acceptance_result', 'flow_id': 'new', 'data': {'id': 'new', 'step': 0}})
        self.assertFalse(self.app.window.acceptance_panel.upload_box.isHidden())
        self.assertTrue(self.app.window.acceptance_panel.result_card.isHidden())
        self.assertIn('验收进度已加载', self.app.window.feedback.text())
        self.assertNotIn('验收操作已完成', self.app.window.feedback.text())
        self.assertFalse(self.app.cfg['onboarding']['vision_verified'])

    def test_flow_and_generation_filter_reject_late_results(self):
        self.app.acceptance = {'id': 'current'}
        self.app.handle_event({'kind': 'acceptance_result', 'flow_id': 'old', 'data': {'id': 'old'}})
        self.assertEqual(self.app.acceptance['id'], 'current')

    def test_image_result_never_enables_send_or_next_step(self):
        self.app.window.set_step(4); self.app.busy = None
        self.app.handle_event({'kind': 'acceptance_result', 'flow_id': 'test', 'data':
            {'id': 'test', 'step': 1, 'vision_reviewed': True, 'route': {'decision_json': '{"action":"candidate","text":"固定回复"}'}}})
        self.assertIsNone(self.app.current_id); self.app.window.next_step()
        self.assertEqual(self.app.window.wizard_pages.currentIndex(), 4)

    def test_confirmation_contains_actual_recipient_and_complete_reply(self):
        live = {'id': 'candidate', 'customer': 'default :: shop :: 模拟买家', 'decision': {'text': '完整固定回复正文'}}
        self.app.acceptance = {'id': 'flow', 'step': 2, 'live': live}; self.app.current_id = 'candidate'
        with patch.object(self.app, 'confirm', return_value=False) as confirm:
            self.app.wizard_send()
        self.assertIn('模拟买家', confirm.call_args.args[1]); self.assertIn('完整固定回复正文', confirm.call_args.args[1])
        self.assertTrue(self.app.worker.commands.empty())

    def test_metadata_result_keeps_current_candidate(self):
        self.app.acceptance = {'id': 'flow'}
        live = {'id': 'candidate', 'status': 'candidate', 'decision': {'text': '固定回复'}}
        self.app.handle_event({'kind': 'acceptance_result', 'flow_id': 'flow', 'data': {'id': 'flow', 'step': 2, 'live': live},
            'metadata': {'onboarding': {'vision_verified': True, 'vision_signature': 'verified'}}})
        self.assertEqual(self.app.current_id, 'candidate')
        self.assertFalse(any(item[0] == 'configure' for item in list(self.app.worker.commands.queue)))

    def test_serial_worker_full_three_page_flow_without_reconfiguration(self):
        from test_qt_frontend import config
        from jevtool.controller import App
        from jevtool.ui_state import UIState
        from jevtool.service import Service
        from jevtool.store import Store
        from jevtool.config import save_config
        cfg = config(); cfg['desktop'].update(data_upload_authorized=True, settle_seconds=.5)
        desktop = Mock(); png = test_core.png_bytes()
        desktop.read_current.return_value = Snapshot('模拟买家', png, image_digest(png), True)
        before = test_core.transcript(text='在哪里查看物流')
        after = test_core.parse_transcript({'readable': True, 'at_bottom': True, 'has_unsupported_content': False,
            'messages': [*before.messages, {'role': 'seller', 'text': '请查看订单物流。', 'timestamp': ''}]})
        reader = Mock(); reader.read.side_effect = [before, before, before, after]
        router = Mock(); router.decide.return_value = Decision('candidate', 'r', '请查看订单物流。', '匹配', ['物流'], [], True)
        path = Path(self.tmp.name); image = path / 'chat.png'; image.write_bytes(png)
        factory = Mock(side_effect=lambda c, s, e: Service(c, s, e, desktop=desktop,
            router=router, reader=reader, store=Store(path / 'flow.db')))
        with patch('jevtool.worker.Service', factory), patch('jevtool.controller.UIState', return_value=UIState(path)), \
                patch('jevtool.acceptance.save_config', side_effect=lambda c: save_config(c, path / 'cfg.json')):
            running = App(cfg=cfg); running.timer.stop()
            def wait():
                deadline = time.monotonic()+5
                while time.monotonic() < deadline:
                    running.drain(); self.qt.processEvents()
                    if running.busy is None: return
                    time.sleep(.01)
                self.fail('Worker did not finish: ' + running.window.feedback.text())
            try:
                wait(); running.acceptance_action('load'); wait()
                for action, extra in [('pick', {'path': str(image)}), ('ocr', {}), ('vision_confirm', {}), ('route', {}), ('route_confirm', {}), ('read', {})]:
                    running.acceptance_action(action, **extra); wait()
                self.assertIsNotNone(running.current_id)
                running.stop(); wait()
                running.acceptance_action('page', step=2); wait()
                self.assertIsNone(running.current_id)
                self.assertIsNone(running.acceptance['live']['id'])
                running.acceptance_action('read'); wait()
                self.assertIsNotNone(running.current_id)
                with patch.object(running, 'confirm', return_value=True): running.wizard_send()
                wait()
                self.assertTrue(running.cfg['desktop']['test_send_verified'])
                self.assertEqual(running.acceptance['live']['status'], 'sent_ui_verified')
                self.assertEqual(factory.call_count, 1); self.assertEqual(reader.read.call_count, 4)
                self.assertEqual(router.decide.call_count, 3); desktop.send_reply.assert_called_once()
                self.assertFalse(running.worker.monitor)
            finally:
                running.close(); running.worker.join(5); running.closed = True; running.window.close()


if __name__ == '__main__': unittest.main()
