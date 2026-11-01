"""Trial gates, profile isolation and restart behavior without external services."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from jevtool.config import DEFAULTS, load_config, save_config
from jevtool.onboarding import can_auto_send, checks
from jevtool.profiles import add_profile, select_profile
from jevtool.router import Decision
from jevtool.store import Store
from jevtool.vision import vision_signature


class TrialReadinessTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(DEFAULTS)
        self.config['profiles'] = {}
        from jevtool.profiles import prepare_profiles
        prepare_profiles(self.config)

    def test_auto_requires_every_real_check(self):
        c = self.config
        c['jev']['api_key'] = 'local-test-key'
        c['vision'].update(provider='minimax', api_key='local-test-key')
        c['onboarding'].update(jev_verified=True, vision_verified=True)
        c['desktop'].update(locators_reviewed=True, unread_locator_reviewed=True,
                            test_send_verified=True, data_upload_authorized=True,
                            auto_send_enabled=True)
        c['onboarding']['vision_signature'] = vision_signature(c['vision'])
        self.assertTrue(can_auto_send(c, approved=1))
        self.assertFalse(can_auto_send(c, approved=0))
        for field in ('locators_reviewed', 'unread_locator_reviewed',
                      'test_send_verified', 'data_upload_authorized', 'auto_send_enabled'):
            c['desktop'][field] = False
            self.assertFalse(can_auto_send(c, approved=1), field)
            c['desktop'][field] = True
        c['onboarding']['vision_verified'] = False
        self.assertFalse(can_auto_send(c, approved=1))
        self.assertFalse(checks(c, approved=1)['vision'])

    def test_minimax_vision_requires_verified_supported_endpoint(self):
        c = self.config
        c['vision'].update(provider='minimax', api_key='test', model='MiniMax-M3',
                           endpoint='https://api.minimax.cn/v1/chat/completions')
        c['onboarding']['vision_verified'] = True
        c['onboarding']['vision_signature'] = vision_signature(c['vision'])
        self.assertTrue(checks(c, approved=1)['vision'])
        c['vision']['endpoint'] = 'https://example.com/v1/chat/completions'
        self.assertFalse(checks(c, approved=1)['vision'])
        c['vision'].update(provider='dify', endpoint='', api_key='test')
        self.assertFalse(checks(c, approved=1)['vision'])

    def test_switching_platform_restores_each_profile_flags_and_locator_store(self):
        c = self.config
        c['desktop']['locators_reviewed'] = True
        first_store = c['desktop']['locators']['current_user']
        new_id = add_profile(c, '闲鱼网页版', 'shop-xianyu', 'web')
        self.assertFalse(c['desktop']['locators_reviewed'])
        self.assertTrue(c['desktop']['locators']['current_user'].startswith('p_'))
        self.assertNotEqual(c['desktop']['locators']['current_user'], first_store)
        c['desktop']['test_send_verified'] = True
        select_profile(c, 'default')
        self.assertTrue(c['desktop']['locators_reviewed'])
        self.assertFalse(c['desktop']['test_send_verified'])
        select_profile(c, new_id)
        self.assertTrue(c['desktop']['test_send_verified'])

    def test_red_badge_mode_is_profile_scoped_and_cannot_auto_send_yet(self):
        c = self.config
        new_id = add_profile(c, '闲鱼网页版', '1', 'web')
        c['desktop'].update(unread_mode='red_badge', locators_reviewed=True,
                            unread_locator_reviewed=True, test_send_verified=True,
                            data_upload_authorized=True, auto_send_enabled=True)
        c['jev']['api_key'] = c['vision']['api_key'] = 'test-key'
        c['onboarding'].update(jev_verified=True, vision_verified=True)
        self.assertFalse(can_auto_send(c, approved=1))
        select_profile(c, 'default')
        self.assertEqual(c['desktop']['unread_mode'], 'locator')
        select_profile(c, new_id)
        self.assertEqual(c['desktop']['unread_mode'], 'red_badge')

    def test_old_profile_without_unread_mode_migrates_to_locator(self):
        c = self.config
        del c['profiles']['default']['unread_mode']
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps(c), encoding='utf-8')
            restored = load_config(path)
        self.assertEqual(restored['desktop']['unread_mode'], 'locator')

    def test_profile_flags_survive_config_restart(self):
        c = self.config
        new_id = add_profile(c, '闲鱼网页版', 'shop-xianyu', 'web')
        c['desktop'].update(locators_reviewed=True, unread_locator_reviewed=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            save_config(c, path)
            restored = load_config(path)
            self.assertEqual(restored['active_profile'], new_id)
            self.assertTrue(restored['desktop']['locators_reviewed'])
            select_profile(restored, 'default')
            self.assertFalse(restored['desktop']['locators_reviewed'])

    def test_platform_ledgers_are_separate_and_crashed_send_stays_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / 'profiles'
            a = Store(base / 'shop_a' / 'events.sqlite3')
            b = Store(base / 'shop_b' / 'events.sqlite3')
            try:
                event = a.add('same-message', 'customer', Decision('candidate', text='fixed'))
                self.assertTrue(a.begin_send(event))
                self.assertFalse(b.seen('same-message'))
            finally:
                a.close()
                b.close()
            a = Store(base / 'shop_a' / 'events.sqlite3')
            try:
                self.assertTrue(a.seen('same-message'))
                self.assertTrue(a.is_blocked('customer'))
                self.assertEqual(a.status(event), 'sending')
            finally:
                a.close()


if __name__ == '__main__':
    unittest.main()
