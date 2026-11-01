import unittest, copy
from pathlib import Path
from unittest.mock import Mock, patch
import test_core
from jevtool.gui import App
from jevtool.vision import vision_signature
from jevtool.onboarding import checks

class PurposeFlowTests(unittest.TestCase):
    def setUp(self): test_core.ServiceTests.setUp(self)
    def tearDown(self): test_core.ServiceTests.tearDown(self)
    def test_vision_only_does_not_call_jev_or_desktop(self):
        path=Path(self.tmp.name)/'test.png';path.write_bytes(self.png)
        self.s.test_vision(path)
        self.router.decide.assert_not_called();self.desktop.read_current.assert_not_called()
        event=next(e for e in self.events if e['kind']=='vision_test_result')
        self.assertIsNone(self.store.vision_record(event['audit_id'])['decision_json'])
        self.assertFalse(self.s.pending)
    def test_unknown_position_preview_has_no_sendable_candidate(self):
        self.s.preview_current(manual_preview=True)
        self.reader.read.assert_called_once();self.router.decide.assert_called_once()
        self.assertFalse(self.s.pending)
        self.assertIsNone(next(e for e in self.events if e['kind']=='decision')['id'])
        self.desktop.send_reply.assert_not_called()
    def test_manual_preview_prepares_latest_without_enabling_send(self):
        self.s.preview_current(manual_preview=True)
        self.desktop.read_current.assert_called_once_with(allow_unknown_latest=True,prepare_latest=True)
        self.desktop.send_reply.assert_not_called()
    def test_changed_vision_settings_invalidate_acceptance(self):
        cfg=copy.deepcopy(self.cfg);cfg['vision']['api_key']='test'
        cfg['onboarding']['vision_verified']=True
        cfg['onboarding']['vision_signature']=vision_signature(cfg['vision'])
        self.assertTrue(checks(cfg,approved=1)['vision'])
        cfg['vision']['api_key']='different'
        self.assertFalse(checks(cfg,approved=1)['vision'])
    def test_prompt_change_invalidates_acceptance(self):
        old=vision_signature(self.cfg['vision'])
        with patch('jevtool.vision.EXTRACTION_PROMPT','changed prompt'):
            self.assertNotEqual(old,vision_signature(self.cfg['vision']))
    def test_cancel_image_selection_does_not_save_or_test(self):
        app=object.__new__(App);app.command=Mock();app.write=Mock();app.log=Mock()
        with patch('jevtool.gui.filedialog.askopenfilename',return_value=''):
            app.choose_image(vision_only=True)
        app.command.assert_not_called()
    def test_setup_save_does_not_select_image_or_call_service(self):
        app=object.__new__(App);app.cfg=copy.deepcopy(self.cfg)
        app.api_jev_var=Mock();app.api_jev_var.get.return_value=app.cfg['jev']['api_key']
        app.api_vision_var=Mock();app.api_vision_var.get.return_value=app.cfg['vision']['api_key']
        app.api_region_var=Mock();app.api_region_var.get.return_value='中国站'
        app.consent_var=Mock();app.consent_var.get.return_value=True
        app.persist_config=Mock();app.write=Mock();app.log=Mock();app.choose_image=Mock();app.command=Mock()
        app.setup_save_api()
        app.persist_config.assert_called_once();app.command.assert_not_called();app.choose_image.assert_not_called()
