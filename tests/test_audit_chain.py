import copy
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
from PIL import Image
import requests
from jevtool import audit
from jevtool.audit import Audit, operation, record, clean
from jevtool.config import DEFAULTS
from jevtool.desktop import ClickniumDesktop, DesktopError, Snapshot
from jevtool.network import JsonHttp, ApiError, Cancelled
from jevtool.store import Store
from jevtool.gui import App
import test_core

class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'audit.sqlite3'
        self.a = Audit(self.path)
        self.replace = patch('jevtool.audit._instance',self.a); self.replace.start()
    def tearDown(self):
        self.replace.stop(); self.a.db.close(); self.tmp.cleanup()
    def test_operation_correlates_cost_vision_actions_and_steps(self):
        store = Store(Path(self.tmp.name)/'events.sqlite3')
        with operation('preview') as op:
            record('capture',customer='客户')
            store.record_action('decision',customer='客户')
            id_ = store.start_vision('test','客户',b'image')
            call = store.start_api_call({'provider':'jev','model':'test','attempt':1},
                {'currency':'USD','billing':'subscription','input_per_million':'0','output_per_million':'0'})
        self.assertEqual(store.vision_record(id_)['operation_id'],op)
        self.assertEqual(store.db.execute('SELECT operation_id FROM api_calls WHERE id=?',(call,)).fetchone()[0],op)
        self.assertEqual(store.db.execute('SELECT operation_id FROM operation_log').fetchone()[0],op)
        store.close()
    def test_filter_customer_expands_whole_operation(self):
        with operation('preview'):
            record('capture',customer='客户甲'); record('api.request')
        with operation('other'): record('capture',customer='客户乙')
        rows=self.a.rows({'customer':'客户甲'})
        self.assertIn('api.request',[r['stage'] for r in rows])
        self.assertNotIn('客户乙',[r['customer'] for r in rows])
    def test_error_is_complete_and_secret_redacted(self):
        audit.register_secrets({'vision':{'api_key':'fake-private-key'}})
        with self.assertRaises(ValueError):
            with operation('failure'): raise ValueError('fake-private-key')
        text=json.dumps(self.a.rows())
        self.assertNotIn('fake-private-key',text)
        self.assertIn('traceback',text)
        self.assertIn('ValueError',text)
        self.assertEqual(clean({'Authorization':'Bearer secret','nested':{'api_key':'unknown'}})['nested']['api_key'],'[REDACTED]')
    def test_export_preserves_actual_attachment_and_no_secret(self):
        with operation('capture'):
            record('capture.evidence','failed',{'api_key':'unknown','before':{'top':0},'after':{'top':-10}},png=b'actual-image')
        path=Path(self.tmp.name)/'diagnostic.zip';self.a.export(path)
        with zipfile.ZipFile(path) as z:
            data=z.read('trace.json').decode()
            self.assertNotIn('unknown',data)
            self.assertIn('before',data)
            self.assertEqual(z.read(next(n for n in z.namelist() if n.startswith('attachments/'))),b'actual-image')
    def test_restart_preserves_last_stage_and_marks_interruption(self):
        token=audit.operation_id.set('interrupted-op')
        record('operation.started',details={'name':'send'})
        event_token=audit.event_id.set('event')
        record('send.click.intent');audit.event_id.reset(event_token)
        audit.operation_id.reset(token)
        reopened=Audit(self.path)
        try:
            self.assertEqual(reopened.last_send_stage('event')['stage'],'send.click.intent')
            recovery=next(r for r in reopened.rows() if r['stage']=='recovery.interrupted')
            self.assertIn('send.click.intent',recovery['details'])
        finally: reopened.db.close()
    def test_all_user_action_methods_have_scoped_wrapper(self):
        for name in ('save','persist_config','setup_select_profile','confirm_vision','confirm_locators','enable_auto','approve','reject','resume','choose_image','command','stop','close'):
            self.assertTrue(hasattr(getattr(App,name),'__wrapped__'),name)
    def test_history_paging_retains_complete_operations(self):
        for _ in range(102):
            with operation('poll'):record('poll.observation')
        first,more=self.a.page_rows(page=0)
        self.assertTrue(more)
        self.assertEqual(len({r['operation'] for r in first}),100)
        second,_=self.a.page_rows(page=1)
        self.assertFalse({r['operation'] for r in first}.intersection({r['operation'] for r in second}))
        self.assertEqual(len([r for r in first if r['stage']=='operation.finished']),100)

class SendEvidenceTests(AuditTests):
    def adapter(self):
        d=object.__new__(ClickniumDesktop);d.stop=threading.Event();d.cfg={}
        d.clipboard=Mock();d.ensure_unchanged=Mock()
        input_box=Mock();button=Mock();text=['']
        input_box.get_text.side_effect=lambda **kw:text[0]
        input_box.send_hotkey.side_effect=lambda _hotkey:text.__setitem__(0,d.clipboard.copy.call_args.args[0])
        d.element=lambda name:input_box if name=='reply_text' else button
        return d,input_box,button
    def stages(self): return [r['stage'] for r in reversed(self.a.rows())]
    def test_click_intent_is_durable_before_call(self):
        d,input_box,button=self.adapter()
        def click():
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM trace WHERE stage='send.click.intent'").fetchone()[0],1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM trace WHERE stage='send.click.returned'").fetchone()[0],0)
        button.click.side_effect=click
        with operation('send'):d.send_reply(Snapshot('客户',b'x','x'),'回复')
        self.assertIn('send.click.returned',self.stages())
        self.assertNotIn('send_ui_verified',self.stages())
    def test_logging_failure_forbids_click(self):
        d,input_box,button=self.adapter()
        def fail(stage,*args,**kwargs):
            if stage=='send.click.intent':raise sqlite3.OperationalError('disk full')
            return record(stage,*args,**kwargs)
        with patch('jevtool.desktop.record',side_effect=fail):
            with self.assertRaises(sqlite3.OperationalError):d.send_reply(Snapshot('客户',b'x','x'),'回复')
        button.click.assert_not_called();self.assertFalse(d.click_intent)
    def test_paste_failure_has_no_click_boundary(self):
        d,input_box,button=self.adapter();input_box.send_hotkey.side_effect=RuntimeError('paste failed')
        with self.assertRaises(RuntimeError):d.send_reply(Snapshot('客户',b'x','x'),'回复')
        self.assertEqual(d.send_phase,'paste');button.click.assert_not_called()
        self.assertIn('send.reply.failed',self.stages());self.assertNotIn('send.click.intent',self.stages())
    def test_readback_failure_records_mismatch(self):
        d,input_box,button=self.adapter();input_box.send_hotkey.side_effect=lambda _:None
        with self.assertRaises(DesktopError):d.send_reply(Snapshot('客户',b'x','x'),'回复')
        row=next(r for r in self.a.rows() if r['stage']=='send.readback')
        self.assertFalse(json.loads(row['details'])['matches']);button.click.assert_not_called()
    def test_click_failure_retains_unknown_boundary(self):
        d,input_box,button=self.adapter();button.click.side_effect=RuntimeError('click lost')
        with self.assertRaises(RuntimeError):d.send_reply(Snapshot('客户',b'x','x'),'回复')
        self.assertTrue(d.click_intent);self.assertIn('send.click.intent',self.stages())
        self.assertNotIn('send.click.returned',self.stages())
    def test_capture_failure_keeps_png_and_exact_changed_fields(self):
        d=object.__new__(ClickniumDesktop);d.stop=threading.Event()
        d.cfg={'adapter':'xianyu_web','locators':{'chat_window':'goofish.chat_window'}}
        d.customer=Mock(return_value='客户')
        rect=Mock();rect.Left=10;rect.Top=40;rect.Right=90;rect.Bottom=100
        element=Mock();element.get_position.return_value=rect;d.element=Mock(return_value=element)
        position={'top':0,'capture_bounds':[10,40,90,100],'anchor_bounds':[10,40,90,100]}
        d.chat_position=Mock(side_effect=[{**position,'content':'before'},{**position,'content':'after'}])
        with patch('jevtool.desktop.ImageGrab.grab',return_value=Image.new('RGB',(20,20))):
            with self.assertRaises(DesktopError) as error:d.read_current()
        self.assertEqual(error.exception.evidence['changed_fields'],['content'])
        row=next(r for r in self.a.rows() if r['stage']=='capture.evidence')
        image=self.a.attachments(row['seq'])[0]['data']
        self.assertTrue(image.startswith(b'\x89PNG'))
        self.assertIn('after',row['details'])

class ApiEvidenceTests(AuditTests):
    def http(self,responses):
        session=Mock();session.post.side_effect=responses
        cfg=copy.deepcopy(DEFAULTS['network']);cfg['attempts']=1
        return JsonHttp(cfg,session=session)
    def test_invalid_json_raw_response_retained(self):
        response=Mock(status_code=200,text='bad response');response.json.side_effect=ValueError('bad')
        with operation('ocr'):
            with self.assertRaises(ApiError):self.http([response]).post('https://api.minimax.io/v1/chat/completions','private',payload={'model':'test'})
        rows=self.a.rows();row=next(r for r in rows if r['stage']=='api.response')
        self.assertIn('bad response',row['details']);self.assertEqual(row['result'],'invalid_json')
    def test_http_error_redacts_credentials_inside_raw_body(self):
        response=Mock(status_code=401,text='{"api_key":"unregistered-secret"}')
        with self.assertRaises(ApiError):self.http([response]).post('https://api.minimax.io/v1/chat/completions','configured-secret',payload={'model':'test'})
        self.assertNotIn('unregistered-secret',json.dumps(self.a.rows()))
        self.assertNotIn('configured-secret',json.dumps(self.a.rows()))
    def test_timeout_contains_full_exception_without_false_send_claim(self):
        with self.assertRaises(ApiError) as error:self.http([requests.Timeout('connection timeout')]).post('https://api.minimax.io/v1/chat/completions','private',payload={'model':'test'})
        self.assertNotIn('没有发送',str(error.exception))
        row=next(r for r in self.a.rows() if r['stage']=='api.response')
        self.assertIn('Timeout',row['details']);self.assertIn('traceback',row['details'])
    def test_cancel_before_request_is_audited_and_does_not_call_network(self):
        http=self.http([]);http.stop.set()
        with self.assertRaises(Cancelled):http.post('https://api.minimax.io/v1/chat/completions','private',payload={'model':'test'})
        http.session.post.assert_not_called();self.assertIn('api.call.failed',[r['stage'] for r in self.a.rows()])

class ServiceBoundaryTests(AuditTests):
    def setUp(self):
        super().setUp()
        self.fixture=test_core.ServiceTests();self.fixture.setUp()
        self.fixture.s.preview_current()
        self.id_=next(e['id'] for e in self.fixture.events if e['kind']=='decision')
    def tearDown(self):
        self.fixture.tearDown();super().tearDown()
    def real_adapter(self):
        d=object.__new__(ClickniumDesktop);d.cfg={};d.stop=threading.Event()
        d.ensure_unchanged=Mock();d.clipboard=Mock()
        d.read_current=Mock(return_value=self.fixture.desktop.read_current.return_value)
        input_box=Mock();button=Mock();text=['']
        input_box.get_text.side_effect=lambda **kw:text[0]
        input_box.send_hotkey.side_effect=lambda _:text.__setitem__(0,d.clipboard.copy.call_args.args[0])
        d.element=lambda name:input_box if name=='reply_text' else button
        self.fixture.s.desktop=d
        return d,input_box,button
    def test_before_click_error_is_aborted_and_client_stays_paused(self):
        d,input_box,button=self.real_adapter();input_box.get_text.side_effect=RuntimeError('input failed')
        with operation('send'):self.fixture.s.send(self.id_,manual=True)
        self.assertEqual(self.fixture.store.status(self.id_),'send_aborted_before_click')
        button.click.assert_not_called()
        row=next(r for r in self.a.rows() if r['stage']=='send.failure')
        self.assertEqual(json.loads(row['details'])['event_id'],self.id_)
        self.assertEqual(json.loads(row['details'])['last_stage'],'input_check')
        with self.assertRaises(DesktopError):self.fixture.s.send(self.id_,manual=True)
    def test_post_click_capture_failure_is_unknown_and_never_retries(self):
        d,input_box,button=self.real_adapter();d.read_current.side_effect=DesktopError('capture failed')
        with operation('send'):self.fixture.s.send(self.id_,manual=True)
        self.assertEqual(self.fixture.store.status(self.id_),'send_unknown')
        button.click.assert_called_once()
        row=next(r for r in self.a.rows() if r['stage']=='send.failure')
        self.assertEqual(json.loads(row['details'])['last_stage'],'post_capture')
        self.assertEqual(self.a.last_send_stage(self.id_)['stage'],'send.failure')
        with self.assertRaises(DesktopError):self.fixture.s.send(self.id_,manual=True)
        button.click.assert_called_once()
    def test_legacy_database_migration_preserves_rows(self):
        path=Path(self.tmp.name)/'old.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.executescript("CREATE TABLE operation_log(id INTEGER PRIMARY KEY AUTOINCREMENT,created REAL NOT NULL,action TEXT NOT NULL,customer TEXT,event_id TEXT,details_json TEXT NOT NULL); INSERT INTO operation_log(created,action,customer,details_json) VALUES (1,'old','客户','{}');")
            db.commit()
        store=Store(path)
        try:
            row=store.recent_actions()[0]
            self.assertEqual(row['action'],'old');self.assertEqual(row['details_json'],'{}')
            self.assertIn('operation_id',{r[1] for r in store.db.execute('PRAGMA table_info(operation_log)')})
        finally:store.close()

if __name__=='__main__':unittest.main()
