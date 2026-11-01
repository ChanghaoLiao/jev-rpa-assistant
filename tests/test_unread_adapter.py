import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jevtool.config import DEFAULTS, save_config, load_config
from jevtool.profiles import prepare_profiles, add_profile, sync_active, LOCATOR_NAMES
from jevtool.unread_adapter import (SELECTOR, normalize_content, prepare_recording,
    recorded_adapter, locator_project, probe_unread, checked_row, rule_hash)
from jevtool.recording import RecordingError
from jevtool.desktop import ClickniumDesktop, DesktopError
from jevtool.network import Cancelled


def content(**attrs):
    identifier = {'controlMetaType':'Web', 'tag':{'value':'DIV'},
        'ancestorId':{'value':'conv-list-scrollable'},
        'class':{'value':'conversation-item-hover--temporary'},
        'innerText':{'value':'虚构买家甲 1'}, 'index':{'value':'1'},
        'style':{'isCustom':True,'value':'background:blue','operator':'equals'}}
    identifier.update(attrs)
    return {'automationType':'Edge', 'processName':{'value':'msedge'},
        'filePath':{'value':'msedge.exe'},'version':{'value':'1.3'},
        'childControls':[{'identifier':{'controlMetaType':'Tab', 'role':{'value':'window'},
            'title':{'value':'聊天_闲鱼'}, 'url':{'value':'https://www.goofish.com/im?example=1','excluded':True}},
            'childControls':[{'identifier':identifier,'childControls':[],'excluded':False}], 'excluded':False}]}


def write_fixture(root, name='p_newmerchant'):
    (root/'.locator').mkdir(exist_ok=True)
    data={'name':name,'id':'fictional-store','nodes':[], 'locators':[
        {'name':n,'id':'fictional-'+n,'content':content()} for n in LOCATOR_NAMES]}
    file=root/'.locator'/(name+'.cnstore')
    file.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    return file,data


class UnreadPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.file,self.data=write_fixture(self.root)
        self.cfg=copy.deepcopy(DEFAULTS);prepare_profiles(self.cfg)
        add_profile(self.cfg,'闲鱼网页版','虚构店铺','web')
        self.cfg['desktop'].update(locators={n:'p_newmerchant.'+n for n in LOCATOR_NAMES},
            locators_reviewed=True,unread_locator_reviewed=True,test_send_verified=True,auto_send_enabled=True)
        self.cfg['onboarding'].update(jev_verified=True,vision_verified=True)
        sync_active(self.cfg);save_config(self.cfg,self.root/'config.local.json')
        self.stop=threading.Event()
        self.validator=Mock(return_value={'matched_count':2,'page':{'unread_indices':[1,3]}})
        self.audit=patch('jevtool.unread_adapter.record');self.log=self.audit.start()

    def tearDown(self):self.audit.stop();self.tmp.cleanup()

    def prepare(self,**kw):return prepare_recording(self.cfg,self.root,self.stop,validator=kw.get('validator',self.validator))

    def test_only_unread_target_changes_and_browser_scope_is_preserved(self):
        result=self.prepare();after=json.loads(self.file.read_text(encoding='utf-8'))
        self.assertTrue(result.changed);self.assertEqual(result.status,'prepared')
        for before,new in zip(self.data['locators'],after['locators']):
            if before['name']!='new_message':self.assertEqual(before,new)
        old=self.data['locators'][0]['content'];new=after['locators'][0]['content']
        self.assertEqual(old['processName'],new['processName'])
        self.assertEqual(old['childControls'][0]['identifier'],new['childControls'][0]['identifier'])
        ident=new['childControls'][0]['childControls'][0]['identifier']
        self.assertEqual(ident['cssSelector']['value'],SELECTOR)
        for name in ('class','innerText','index','style'):self.assertTrue(ident[name]['excluded'])
        self.assertTrue(result.config['desktop']['locators_reviewed'])
        self.assertEqual(result.config['onboarding'],self.cfg['onboarding'])
        for flag in ('unread_locator_reviewed','test_send_verified','auto_send_enabled'):
            self.assertFalse(result.config['desktop'][flag])
        self.assertEqual(self.cfg['desktop']['adapter'],'generic')
        self.assertEqual(result.config['desktop']['adapter'],'xianyu_web')
        self.assertEqual(self.validator.call_args.args[1].parent.parent.name,'backups')

    def test_existing_good_rule_is_idempotent_and_preserves_acceptance(self):
        first=self.prepare();self.cfg=first.config
        self.cfg['desktop'].update(unread_locator_reviewed=True,test_send_verified=True,auto_send_enabled=True)
        sync_active(self.cfg);save_config(self.cfg,self.root/'config.local.json')
        before=self.file.read_bytes();result=self.prepare()
        self.assertFalse(result.changed);self.assertEqual(self.file.read_bytes(),before)
        self.assertTrue(result.config['desktop']['test_send_verified'])
        self.assertEqual(result.config,self.cfg)

    def test_no_unread_is_waiting_for_validation_not_locator_failure(self):
        self.validator.return_value={'matched_count':0,'page':{'unread_indices':[]}}
        result=self.prepare();self.assertIn('没有未读',result.reason)
        self.assertFalse(result.config['desktop']['unread_locator_reviewed'])

    def test_failed_page_check_preserves_exact_originals(self):
        before=self.file.read_bytes();cfg=(self.root/'config.local.json').read_bytes()
        self.validator.side_effect=RecordingError('wrong rows')
        with self.assertRaises(RecordingError):self.prepare()
        self.assertEqual(self.file.read_bytes(),before);self.assertEqual((self.root/'config.local.json').read_bytes(),cfg)
        self.assertEqual(len(list((self.root/'backups').glob('unread-adapt-*'))),1)

    def test_cancel_during_check_never_commits(self):
        before=self.file.read_bytes()
        def cancel(*args):self.stop.set();return {'matched_count':0}
        with self.assertRaises(Cancelled):self.prepare(validator=cancel)
        self.assertEqual(before,self.file.read_bytes())

    def test_recorder_saved_during_check_is_not_overwritten(self):
        saved=b'{"newer":"recording saved externally"}'
        def changed(*args):self.file.write_bytes(saved);return {'matched_count':0}
        with self.assertRaisesRegex(RecordingError,'重新保存'):self.prepare(validator=changed)
        self.assertEqual(self.file.read_bytes(),saved)

    def test_config_changed_during_check_is_not_overwritten(self):
        original=self.file.read_bytes();saved=b'{"newer":"config"}'
        def changed(*args):(self.root/'config.local.json').write_bytes(saved);return {'matched_count':0}
        with self.assertRaisesRegex(RecordingError,'重新保存'):self.prepare(validator=changed)
        self.assertEqual(self.file.read_bytes(),original);self.assertEqual((self.root/'config.local.json').read_bytes(),saved)

    def test_file_in_use_leaves_both_originals_intact(self):
        before=self.file.read_bytes();cfg=(self.root/'config.local.json').read_bytes()
        with patch('jevtool.unread_adapter.os.replace',side_effect=PermissionError('file in use')):
            with self.assertRaises(PermissionError):self.prepare()
        self.assertEqual(self.file.read_bytes(),before);self.assertEqual((self.root/'config.local.json').read_bytes(),cfg)

    def test_config_save_failure_restores_original_locator(self):
        before=self.file.read_bytes()
        with patch('jevtool.config.save_config',side_effect=OSError('cannot save')):
            with self.assertRaises(OSError):self.prepare()
        self.assertEqual(self.file.read_bytes(),before)

    def test_shared_store_is_not_modified(self):
        self.cfg['profiles']['other']=copy.deepcopy(self.cfg['profiles'][self.cfg['active_profile']])
        before=self.file.read_bytes()
        with self.assertRaisesRegex(RecordingError,'其他账号'):self.prepare()
        self.assertEqual(self.file.read_bytes(),before);self.validator.assert_not_called()

    def test_other_platform_and_chrome_are_explicitly_manual(self):
        for case in ('chrome','wrong-url'):
            with self.subTest(case=case):
                data=copy.deepcopy(self.data)
                for item in data['locators']:
                    c=item['content']
                    if case=='chrome':c['automationType']='Chrome'
                    else:c['childControls'][0]['identifier']['url']['value']='https://example.com/im'
                self.file.write_text(json.dumps(data))
                before=self.file.read_bytes();r=self.prepare()
                self.assertEqual(r.status,'manual');self.assertEqual(r.config,self.cfg)
                self.assertEqual(before,self.file.read_bytes())
        self.validator.assert_not_called()

    def test_entire_list_is_rejected_with_specific_instruction(self):
        data=copy.deepcopy(self.data)
        data['locators'][0]['content']['childControls'][0]['childControls'][0]['identifier']['tag']['value']='ASIDE'
        self.file.write_text(json.dumps(data));before=self.file.read_bytes()
        with self.assertRaisesRegex(RecordingError,'完整客户行'):self.prepare()
        self.assertEqual(self.file.read_bytes(),before)

    def test_store_name_and_group_names_do_not_determine_adapter(self):
        data=copy.deepcopy(self.data);data['locators']=[]
        data['nodes']=[{'name':'chat','nodes':[],'locators':self.data['locators']}]
        self.file.write_text(json.dumps(data))
        self.cfg['desktop']['locators']={n:'p_newmerchant.chat.'+n for n in LOCATOR_NAMES}
        sync_active(self.cfg);save_config(self.cfg,self.root/'config.local.json')
        self.assertEqual(recorded_adapter(self.cfg['desktop'],self.root),'xianyu_web')
        self.assertTrue(self.prepare().changed)

    def test_sdk_loads_normalized_rules_and_sees_replace(self):
        from clicknium.core.service.invokerservice import LocatorService
        from jevtool.clicknium_project import bind_locator_project
        with locator_project(self.root):
            before=str(LocatorService.get_locator('p_newmerchant.new_message').Locator)
            candidate=copy.deepcopy(self.data)
            candidate['locators'][0]['content']=normalize_content(candidate['locators'][0]['content'])
            self.file.write_text(json.dumps(candidate),encoding='utf-8')
            bind_locator_project(self.root, fresh=True)
            after=str(LocatorService.get_locator('p_newmerchant.new_message').Locator)
        self.assertNotEqual(before,after);self.assertIn('ant-badge-count',after)

    def test_native_sdk_transaction_uses_fresh_committed_copy(self):
        import hashlib
        from clicknium.core.service.invokerservice import LocatorService
        with locator_project(self.root):
            LocatorService.get_locator('p_newmerchant.new_message')
        def validate(cfg, stage, stop):
            with locator_project(stage):
                value=str(LocatorService.get_locator('p_newmerchant.new_message').Locator)
                self.assertIn('ant-badge-count', value)
                return {'matched_count':0,'sdk_locator_hash':hashlib.sha256(value.encode()).hexdigest()}
        self.assertTrue(self.prepare(validator=validate).changed)

    def test_runtime_snapshot_reused_only_for_same_file_contents(self):
        from jevtool.clicknium_project import runtime_project
        first=runtime_project(self.root)
        self.assertEqual(first,runtime_project(self.root))
        candidate=copy.deepcopy(self.data)
        candidate['locators'][0]['content']=normalize_content(candidate['locators'][0]['content'])
        self.file.write_text(json.dumps(candidate),encoding='utf-8')
        second=runtime_project(self.root)
        self.assertNotEqual(first,second)
        self.assertEqual((first/'.locator'/self.file.name).read_bytes(),json.dumps(self.data,ensure_ascii=False).encode('utf-8'))


class UnreadPageTests(unittest.TestCase):
    def test_default_constructor_uses_user_data_root_without_explicit_override(self):
        from jevtool.paths import DATA_ROOT
        cfg=copy.deepcopy(DEFAULTS['desktop'])
        with patch('jevtool.clicknium_project.bind_locator_project',return_value=DATA_ROOT.resolve()) as bind, \
             patch('jevtool.desktop.record'):
            desktop=ClickniumDesktop(cfg,threading.Event(),allow_unreviewed=True)
        self.assertEqual(desktop.project_root,DATA_ROOT.resolve())
        bind.assert_called_once_with(DATA_ROOT.resolve(),fresh=False)

    def desktop(self,indices=(0,2)):
        d=ClickniumDesktop.__new__(ClickniumDesktop);d.stop=threading.Event();d.cc=Mock()
        d.cfg={'adapter':'xianyu_web','locators':{'new_message':'any.new_message'},'unread_locator_reviewed':True,'settle_seconds':.5}
        d.resolve=Mock(side_effect=lambda n:n);d.round_robin=0
        d.cc.find_elements.return_value=[Mock()]
        tab=d.cc.edge.attach.return_value;header=Mock()
        header.execute_js.return_value=json.dumps({'host':'www.goofish.com','path':'/im','total_rows':3,'unread_indices':list(indices)})
        rows=[]
        for i in indices:
            row=Mock();row.execute_js.return_value=json.dumps({'host':'www.goofish.com','path':'/im','row_index':i,'belongs':True,'unread':True});rows.append(row)
        tab.find_elements.side_effect=lambda loc,**kw:[header] if loc=='current_user' else rows
        return d,header,rows

    def test_zero_and_multiple_unread_are_checked_without_actions(self):
        for indices in ((),(0,2)):
            d,h,rows=self.desktop(indices);r=probe_unread(d)
            self.assertEqual(r['matched_count'],len(indices));h.click.assert_not_called()
            for row in rows:row.click.assert_not_called();row.scroll.assert_not_called()

    def test_ambiguous_tabs_are_rejected_before_attach(self):
        d,_,_=self.desktop();d.cc.find_elements.return_value=[Mock(),Mock()]
        with self.assertRaisesRegex(RecordingError,'不唯一'):probe_unread(d)
        d.cc.edge.attach.assert_not_called()

    def test_plain_customer_is_rejected(self):
        d,_,rows=self.desktop();rows[0].execute_js.return_value=json.dumps({'belongs':True,'unread':False})
        with self.assertRaises(RecordingError):probe_unread(d)
        for row in rows:row.click.assert_not_called()

    def test_changing_list_and_duplicate_results_are_rejected(self):
        d,h,rows=self.desktop()
        first={'host':'www.goofish.com','path':'/im','total_rows':3,'unread_indices':[0,2]}
        h.execute_js.side_effect=[json.dumps(first),json.dumps({**first,'unread_indices':[0]})]
        with self.assertRaisesRegex(RecordingError,'发生变化'):probe_unread(d)
        d,h,rows=self.desktop();rows[1].execute_js.return_value=rows[0].execute_js.return_value
        with self.assertRaisesRegex(RecordingError,'不一致'):probe_unread(d)

    def test_badge_disappearing_immediately_before_click_is_skipped(self):
        d,_,rows=self.desktop((0,));d.checked_unread_rows=Mock(return_value=rows)
        rows[0].execute_js.return_value=json.dumps({'belongs':True,'unread':False})
        self.assertFalse(d.open_next_unread());rows[0].click.assert_not_called()

    def test_only_one_verified_row_clicked(self):
        d,_,rows=self.desktop((0,2));d.checked_unread_rows=Mock(return_value=rows)
        d.stop=Mock();d.stop.is_set.return_value=False;d.stop.wait.return_value=False
        self.assertTrue(d.open_next_unread());rows[0].click.assert_called_once();rows[1].click.assert_not_called()

    def test_re_saved_rule_stops_clicks(self):
        d,_,rows=self.desktop();d.cfg['unread_rule_hash']='0'*64
        d.project_root=Path(tempfile.gettempdir())/'nonexistent-jev-fictional-project'
        with self.assertRaisesRegex(DesktopError,'重新保存'):d.open_next_unread()
        for row in rows:row.click.assert_not_called()
