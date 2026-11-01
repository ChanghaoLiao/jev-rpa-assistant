import copy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jevtool.clicknium_project import bind_locator_project, check_locator_runtime
from jevtool.config import DEFAULTS


class ClickniumProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / '.locator').mkdir()
        self.original = Mock(spec=['__call__'], return_value='bundled-example-project')
        self.utils = type('Utils', (), {'get_project_folder': staticmethod(self.original)})
        constants = types.ModuleType('clicknium.common.constants')
        constants._Constants = types.SimpleNamespace(LocatorFolder='.locator', ClickniumYaml='clicknium.yaml')
        utils = types.ModuleType('clicknium.common.utils'); utils.Utils = self.utils
        service = types.ModuleType('clicknium.core.service.invokerservice')
        self.load = Mock(return_value=types.SimpleNamespace(Locator='actual definition'))
        service.LocatorService = types.SimpleNamespace(get_locator=self.load)
        self.modules = patch.dict(sys.modules, {
            'clicknium': types.ModuleType('clicknium'),
            'clicknium.common': types.ModuleType('clicknium.common'),
            'clicknium.common.constants': constants,
            'clicknium.common.utils': utils,
            'clicknium.core': types.ModuleType('clicknium.core'),
            'clicknium.core.service': types.ModuleType('clicknium.core.service'),
            'clicknium.core.service.invokerservice': service,
        })
        self.modules.start()

    def tearDown(self):
        self.modules.stop(); self.temp.cleanup()

    def test_frozen_path_never_selects_bundled_or_parent_stores(self):
        before = sys.path[:]
        bind_locator_project(self.root)
        self.assertEqual(self.utils.get_project_folder('.locator'), str(self.root.resolve()))
        self.assertEqual(self.utils.get_project_folder('clicknium.yaml'), '')
        self.original.assert_not_called()
        self.assertEqual(sys.path, before)

    def test_project_yaml_uses_same_merchant_directory(self):
        (self.root / 'clicknium.yaml').write_text('')
        bind_locator_project(self.root)
        self.assertEqual(self.utils.get_project_folder('clicknium.yaml'), str(self.root.resolve()))

    def test_unrelated_sdk_lookup_keeps_original_behavior(self):
        bind_locator_project(self.root)
        self.assertEqual(self.utils.get_project_folder('unrelated-resource'), 'bundled-example-project')
        self.original.assert_called_once_with('unrelated-resource')

    def test_missing_directory_fails_without_changing_sdk_lookup(self):
        with self.assertRaisesRegex(ValueError, '用户录制目录不存在'):
            bind_locator_project(self.root / 'missing')
        self.assertIs(self.utils.get_project_folder, self.original)

    def test_rebinding_does_not_reuse_previous_merchant_or_wrap_recursively(self):
        other = self.root / 'other'; (other / '.locator').mkdir(parents=True)
        bind_locator_project(self.root); bind_locator_project(other)
        self.assertEqual(self.utils.get_project_folder('.locator'), str(other.resolve()))
        self.utils.get_project_folder('unrelated-resource')
        self.original.assert_called_once_with('unrelated-resource')

    def test_runtime_check_loads_all_five_definitions_without_browser_calls(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg['desktop']['locators'] = {name: 'locator.goofish.' + name for name in cfg['desktop']['locators']}
        before = copy.deepcopy(cfg)
        with patch('jevtool.clicknium_project.DATA_ROOT', self.root):
            result = check_locator_runtime(cfg)
        self.assertTrue(result['ok']); self.assertEqual(len(result['locations']), 5)
        self.assertEqual(self.load.call_count, 5)
        self.assertEqual({c.args[0] for c in self.load.call_args_list},
                         {'goofish.' + n for n in cfg['desktop']['locators']})
        self.assertEqual(cfg, before)

    def test_partial_sdk_load_failure_reports_item_and_checks_remaining(self):
        cfg = copy.deepcopy(DEFAULTS)
        self.load.side_effect = [RuntimeError('missing locator'), *[types.SimpleNamespace(Locator='ok')] * 4]
        with patch('jevtool.clicknium_project.DATA_ROOT', self.root):
            result = check_locator_runtime(cfg)
        self.assertFalse(result['ok']); self.assertEqual(self.load.call_count, 5)
        self.assertEqual(sum(row['loaded'] for row in result['locations'].values()), 4)
        self.assertIn('missing locator', next(iter(result['locations'].values()))['reason'])

    def test_empty_sdk_definition_does_not_count_as_loaded(self):
        self.load.return_value = types.SimpleNamespace(Locator='')
        with patch('jevtool.clicknium_project.DATA_ROOT', self.root):
            result = check_locator_runtime(copy.deepcopy(DEFAULTS))
        self.assertFalse(result['ok'])
        self.assertTrue(all(not row['loaded'] for row in result['locations'].values()))
