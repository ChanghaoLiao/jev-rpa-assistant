import copy
import json
import tempfile
import unittest
from pathlib import Path

from jevtool.config import DEFAULTS, validate_config
from jevtool.profiles import LOCATOR_NAMES, add_profile, prepare_profiles, sync_active
from jevtool.recording import check_recording, bind_recording, backup_recording_config, RecordingError


def write_store(root, name, names=LOCATOR_NAMES, automation='Edge'):
    directory = root / '.locator'; directory.mkdir(exist_ok=True)
    path = directory / (name + '.cnstore')
    path.write_text(json.dumps({'name': name, 'nodes': [], 'locators': [
        {'name': n, 'content': {'automationType': automation, 'childControls': []}} for n in names]}), encoding='utf-8')
    return path


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.cfg = copy.deepcopy(DEFAULTS); prepare_profiles(self.cfg)
        add_profile(self.cfg, '闲鱼网页版', '店铺甲', 'web')

    def tearDown(self):
        self.temp.cleanup()

    def test_single_complete_unclaimed_store_can_connect(self):
        file = write_store(self.root, 'goofish'); original = file.read_bytes()
        checked = check_recording(self.cfg, self.root)
        self.assertFalse(checked.ready); self.assertEqual(checked.candidates, ('goofish',))
        result = bind_recording(self.cfg, self.root, 'goofish'); validate_config(result)
        self.assertTrue(check_recording(result, self.root).ready)
        self.assertEqual(file.read_bytes(), original)
        self.assertNotEqual(result['desktop']['locators'], self.cfg['desktop']['locators'])

    def test_mapping_change_only_invalidates_active_page_acceptance(self):
        write_store(self.root, 'goofish')
        self.cfg['onboarding'].update(jev_verified=True, vision_verified=True)
        self.cfg['desktop'].update(locators_reviewed=True, unread_locator_reviewed=True,
            test_send_verified=True, auto_send_enabled=True, data_upload_authorized=True)
        sync_active(self.cfg); before = copy.deepcopy(self.cfg)
        result = bind_recording(self.cfg, self.root, 'goofish')
        for key in ('jev', 'vision', 'knowledge', 'network', 'onboarding'):
            self.assertEqual(result[key], before[key])
        self.assertEqual(result['profiles']['default'], before['profiles']['default'])
        self.assertTrue(result['desktop']['data_upload_authorized'])
        for key in ('locators_reviewed', 'unread_locator_reviewed', 'test_send_verified', 'auto_send_enabled'):
            self.assertFalse(result['desktop'][key])
        self.assertEqual(self.cfg, before)

    def test_correct_existing_mapping_preserves_acceptance(self):
        write_store(self.root, 'goofish')
        self.cfg = bind_recording(self.cfg, self.root, 'goofish')
        self.cfg['desktop'].update(locators_reviewed=True, unread_locator_reviewed=True,
            test_send_verified=True, auto_send_enabled=True); sync_active(self.cfg)
        self.assertTrue(check_recording(self.cfg, self.root).ready)
        self.assertEqual(bind_recording(self.cfg, self.root, 'goofish'), self.cfg)

    def test_multiple_candidates_require_explicit_selection(self):
        write_store(self.root, 'store_a'); write_store(self.root, 'store_b')
        self.assertEqual(check_recording(self.cfg, self.root).candidates, ('store_a', 'store_b'))
        result = bind_recording(self.cfg, self.root, 'store_b')
        self.assertTrue(all(v.startswith('store_b.') for v in result['desktop']['locators'].values()))

    def test_other_account_store_is_not_a_candidate(self):
        write_store(self.root, 'goofish'); original = self.cfg['active_profile']
        self.cfg['desktop']['locators'] = {k: 'goofish.' + k for k in LOCATOR_NAMES}; sync_active(self.cfg)
        add_profile(self.cfg, '闲鱼网页版', '店铺乙', 'web')
        checked = check_recording(self.cfg, self.root)
        self.assertFalse(checked.candidates); self.assertIn('其他平台', checked.reason)
        self.assertTrue(all(v.startswith('goofish.') for v in self.cfg['profiles'][original]['locators'].values()))

    def test_incomplete_existing_store_is_not_replaced(self):
        name = self.cfg['desktop']['locators']['current_user'].split('.')[0]
        write_store(self.root, name, ('current_user',)); write_store(self.root, 'goofish')
        checked = check_recording(self.cfg, self.root)
        self.assertFalse(checked.candidates); self.assertIn('chat_window', checked.reason)
        with self.assertRaises(RecordingError): bind_recording(self.cfg, self.root, 'goofish')

    def test_incomplete_files_are_not_merged(self):
        write_store(self.root, 'one', LOCATOR_NAMES[:3]); write_store(self.root, 'two', LOCATOR_NAMES[3:])
        checked = check_recording(self.cfg, self.root)
        self.assertFalse(checked.candidates); self.assertIn('同一个 Store', checked.reason)

    def test_malformed_expected_store_does_not_fall_back(self):
        write_store(self.root, 'goofish')
        name = self.cfg['desktop']['locators']['current_user'].split('.')[0]
        (self.root / '.locator' / (name + '.cnstore')).write_text('{broken')
        checked = check_recording(self.cfg, self.root)
        self.assertFalse(checked.candidates); self.assertIn('无法读取', checked.reason)

    def test_inconsistent_file_and_store_names_are_rejected(self):
        path = write_store(self.root, 'one'); data = json.loads(path.read_text()); data['name'] = 'two'
        path.write_text(json.dumps(data)); checked = check_recording(self.cfg, self.root)
        self.assertFalse(checked.candidates); self.assertIn('one', checked.reason)

    def test_wrong_platform_type_not_selected(self):
        write_store(self.root, 'uia_store', automation='UIA')
        self.assertFalse(check_recording(self.cfg, self.root).candidates)

    def test_changed_or_deleted_store_is_rechecked_before_binding(self):
        path = write_store(self.root, 'goofish')
        self.assertEqual(check_recording(self.cfg, self.root).candidates, ('goofish',))
        path.unlink()
        with self.assertRaises(RecordingError): bind_recording(self.cfg, self.root, 'goofish')

    def test_nested_existing_locators_remain_valid(self):
        path = write_store(self.root, 'nested'); data = json.loads(path.read_text())
        data['nodes'] = [{'name': 'chat', 'locators': data['locators'], 'nodes': []}]; data['locators'] = []
        path.write_text(json.dumps(data))
        self.cfg['desktop']['locators'] = {k: 'locator.nested.chat.' + k for k in LOCATOR_NAMES}; sync_active(self.cfg)
        self.assertTrue(check_recording(self.cfg, self.root).ready)

    def test_backup_preserves_exact_original_and_is_unique(self):
        source = self.root / 'config.local.json'; source.write_bytes(b'{"private":"test-only"}')
        first = backup_recording_config(self.root); second = backup_recording_config(self.root)
        self.assertEqual(first.read_bytes(), source.read_bytes()); self.assertNotEqual(first, second)


if __name__ == '__main__': unittest.main()
