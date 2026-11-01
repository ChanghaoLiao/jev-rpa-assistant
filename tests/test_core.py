import copy
import csv
import io
import json
import math
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
from PIL import Image
from jevtool.config import DEFAULTS, ROOT, validate_config, validate_endpoint, ConfigError
from jevtool.network import JsonHttp, ApiError, Cancelled
from jevtool.knowledge import Knowledge, Reply, KnowledgeError
from jevtool.router import JevClient, Router, ChoiceResult, Decision
from jevtool.vision import Transcript, EXTRACTION_PROMPT, parse_transcript, ExtractionError, image_digest, VisionReader
from jevtool.store import Store
from jevtool.desktop import Snapshot, DesktopError
from jevtool.service import Service
from jevtool.demo import run_demo


def transcript(role='buyer', text='怎么查看预计发货时间？', timestamp='10:20'):
    return parse_transcript({'readable':True,'at_bottom':True,'has_unsupported_content':False,
       'messages':[{'role':role,'text':text,'timestamp':timestamp}]})

def png_bytes():
    im=Image.new('RGB',(20,20)); b=io.BytesIO();im.save(b,format='PNG');return b.getvalue()

class FakeClient:
    def __init__(self, human_at=-1, confidence=.96, fail_at=-1):
        self.calls=[];self.human_at=human_at;self.confidence=confidence;self.fail_at=fail_at
    def choose(self,state,instructions,criteria):
        n=len(self.calls); self.calls.append((state,instructions,criteria))
        if n==self.fail_at:raise ApiError('测试超时')
        key='HUMAN' if n==self.human_at else next(k for k in criteria if k!='HUMAN')
        return ChoiceResult(key,self.confidence,{k:1.0 if k==key else 0.0 for k in criteria},'MOCK',{})

class ConfigTests(unittest.TestCase):
    def test_defaults_valid(self):validate_config(copy.deepcopy(DEFAULTS))
    def test_remote_http_rejected(self):
        with self.assertRaises(ConfigError):validate_endpoint('http://example.com/api')
    def test_local_http_allowed(self):self.assertEqual(validate_endpoint('http://127.0.0.1:8000/'),'http://127.0.0.1:8000')
    def test_embedded_credentials_rejected(self):
        with self.assertRaises(ConfigError):validate_endpoint('https://secret@example.com/api')
    def test_string_bool_rejected(self):
        c=copy.deepcopy(DEFAULTS);c['desktop']['auto_send_enabled']='false'
        with self.assertRaises(ConfigError):validate_config(c)
    def test_invalid_threshold_rejected(self):
        c=copy.deepcopy(DEFAULTS);c['jev']['min_confidence']=float('nan')
        with self.assertRaises(ConfigError):validate_config(c)

class KnowledgeTests(unittest.TestCase):
    def test_sample_library_safe(self):
        k=Knowledge.load(ROOT/'knowledge/replies.csv',ROOT/'knowledge/categories.json')
        self.assertEqual(len(k.replies),16)
        self.assertFalse(any(r.approved for r in k.replies.values()))
    def test_duplicate_id_rejected(self):
        r=Reply('a','x','when','','text',False,False)
        with self.assertRaises(KnowledgeError):Knowledge([r,r])
    def test_too_many_choices_rejected(self):
        with self.assertRaises(KnowledgeError):Knowledge([Reply(str(i),'x','when','','text',False,False) for i in range(255)])
    def test_254_plus_human_allowed(self):
        k=Knowledge([Reply(str(i),'x','when','','text',False,False) for i in range(254)])
        self.assertEqual(len(k.options(k.root.children['x'])[0]),255)
    def test_empty_library_rejected(self):
        with self.assertRaises(KnowledgeError):Knowledge([])
    def test_empty_path_rejected(self):
        with self.assertRaises(KnowledgeError):Knowledge([Reply('a','x//y','w','','t',False,False)])
    def test_10000_replies_have_bounded_choices(self):
        rows=[Reply(str(i),f'大类{i//1000}/小类{(i//100)%10}','适用条件','','固定回复',False,False) for i in range(10000)]
        k=Knowledge(rows)
        self.assertEqual(len(k.replies),10000)
        self.assertEqual(len(k.options(k.root)[0]),11)
        leaf=k.root.children['大类0'].children['小类0']
        self.assertEqual(len(k.options(leaf)[0]),101)

class RouterTests(unittest.TestCase):
    def setUp(self):
        self.k=Knowledge([Reply('r1','物流/方法','一般查询入口','','固定回复原文',False,False)])
        self.state={'pending_buyer_messages':[{'role':'buyer','text':'问题'}],'history':[]}
    def test_hierarchy_and_exact_text(self):
        client=FakeClient();d=Router(self.k,client,DEFAULTS['jev']).decide(self.state)
        self.assertEqual(d.action,'candidate');self.assertEqual(d.text,'固定回复原文')
        self.assertEqual(d.path,['物流','方法']);self.assertEqual(len(client.calls),4)
        self.assertFalse(d.approved)
        self.assertIn('candidate',client.calls[-1][0])
        self.assertEqual(client.calls[-1][0]['pending_buyer_messages'],self.state['pending_buyer_messages'])
    def test_unknown_at_root_stops(self):
        c=FakeClient(human_at=0);d=Router(self.k,c,DEFAULTS['jev']).decide(self.state)
        self.assertEqual(d.action,'human');self.assertEqual(len(c.calls),1)
    def test_low_confidence_does_not_override_jev_choice(self):
        d=Router(self.k,FakeClient(confidence=.1),DEFAULTS['jev']).decide(self.state)
        self.assertEqual(d.action,'candidate')
        self.assertEqual(d.trace[-1]['confidence'],.1)
    def test_all_numeric_thresholds_are_ignored_including_final_review(self):
        class LowScores(FakeClient):
            def choose(self,*args,**kwargs):
                r=super().choose(*args,**kwargs)
                scores={k:(.01 if k==r.choice else .99/(len(r.probabilities)-1)) for k in r.probabilities}
                r.probabilities.clear();r.probabilities.update(scores)
                return r
        cfg={**DEFAULTS['jev'],'min_confidence':1,'min_probability':1,'min_margin':1}
        d=Router(self.k,LowScores(confidence=.01),cfg).decide(self.state)
        self.assertEqual(d.action,'candidate')
        self.assertEqual(len(d.trace),4)
        self.assertEqual(d.trace[-1]['probabilities']['SEND'],.01)
    def test_final_guard_can_reject(self):
        d=Router(self.k,FakeClient(human_at=3),DEFAULTS['jev']).decide(self.state)
        self.assertEqual(d.action,'human');self.assertIn('复核',d.reason)
    def test_timeout_is_human(self):
        d=Router(self.k,FakeClient(fail_at=1),DEFAULTS['jev']).decide(self.state)
        self.assertEqual(d.action,'human')
    def test_manual_only_never_candidate(self):
        k=Knowledge([Reply('r1','物流','when','','text',True,True)])
        d=Router(k,FakeClient(),DEFAULTS['jev']).decide(self.state)
        self.assertEqual(d.action,'human')
    def test_call_budget_enforced(self):
        cfg={**DEFAULTS['jev'],'max_calls':2};c=FakeClient()
        d=Router(self.k,c,cfg).decide(self.state)
        self.assertEqual(d.action,'human');self.assertEqual(len(c.calls),2)
    def test_missing_input_no_call(self):
        c=FakeClient();d=Router(self.k,c,DEFAULTS['jev']).decide({})
        self.assertEqual(d.action,'human');self.assertFalse(c.calls)
    def test_offline_demo_marked_mock(self):
        self.assertTrue(all(t['model']=='MOCK-NOT-JEV' for t in run_demo().trace))

class JevProtocolTests(unittest.TestCase):
    def setUp(self):
        self.answer={'model':'jev-test','answers':{'route':{'type':'choice','choice':'A','confidence':.9,
                     'probabilities':{'A':.95,'HUMAN':.05}}},'usage':{'input_tokens':20}}
        self.http=Mock();self.http.post.return_value=self.answer
        self.client=JevClient({**DEFAULTS['jev'],'api_key':'test-not-real'},self.http)
    def call(self):return self.client.choose({'message':'test'},'pick',{'A':'option','HUMAN':'other'})
    def test_current_http_schema(self):
        r=self.call();self.assertEqual(r.choice,'A')
        p=self.http.post.call_args.kwargs['payload']
        self.assertEqual(p['model'],'jev-latest');self.assertEqual(p['questions']['route']['type'],'choice')
    def test_old_choices_shape_is_rejected(self):
        self.http.post.return_value={'choices':self.answer['answers']}
        with self.assertRaises(ApiError):self.call()
    def test_unknown_option_rejected(self):
        self.answer['answers']['route']['choice']='invented'
        with self.assertRaises(ApiError):self.call()
    def test_nan_confidence_rejected(self):
        self.answer['answers']['route']['confidence']=float('nan')
        with self.assertRaises(ApiError):self.call()
    def test_missing_probability_rejected(self):
        self.answer['answers']['route']['probabilities'].pop('HUMAN')
        with self.assertRaises(ApiError):self.call()
    def test_invalid_probability_sum_rejected(self):
        self.answer['answers']['route']['probabilities']['HUMAN']=.9
        with self.assertRaises(ApiError):self.call()

class HttpTests(unittest.TestCase):
    def response(self,status,payload=None):
        r=Mock();r.status_code=status;r.headers={};r.json.return_value=payload or {'ok':True};return r
    def test_auth_payload_and_no_redirect(self):
        s=Mock();s.post.return_value=self.response(200)
        h=JsonHttp(DEFAULTS['network'],session=s)
        h.post('https://api.typesafe.ai/v1/systemone','unit-test-key',payload={'model':'test'})
        kw=s.post.call_args.kwargs
        self.assertFalse(kw['allow_redirects']);self.assertEqual(kw['headers']['Authorization'],'Bearer unit-test-key')
    def test_401_no_retry(self):
        s=Mock();s.post.return_value=self.response(401)
        with self.assertRaises(ApiError):JsonHttp(DEFAULTS['network'],session=s).post('https://example.com','x',payload={})
        self.assertEqual(s.post.call_count,1)
    def test_429_retries_then_returns(self):
        s=Mock();s.post.side_effect=[self.response(429),self.response(200)]
        stop=Mock();stop.is_set.return_value=False;stop.wait.return_value=False
        h=JsonHttp(DEFAULTS['network'],stop=stop,session=s)
        self.assertEqual(h.post('https://example.com','x',payload={}),{'ok':True})
        self.assertEqual(s.post.call_count,2)
    def test_no_key_no_network(self):
        s=Mock()
        with self.assertRaises(ApiError):JsonHttp(DEFAULTS['network'],session=s).post('https://example.com','',payload={})
        s.post.assert_not_called()
    def test_stop_before_request(self):
        e=threading.Event();e.set();s=Mock()
        with self.assertRaises(Cancelled):JsonHttp(DEFAULTS['network'],stop=e,session=s).post('https://example.com','x',payload={})
        s.post.assert_not_called()

class VisionTests(unittest.TestCase):
    def test_jev_state_keeps_messages_without_extra_order_note(self):
        t=parse_transcript(self.raw())
        state=t.state()
        self.assertNotIn('note',state)
        self.assertEqual(state['history'],t.messages)
        self.assertEqual(state['pending_buyer_messages'],t.pending())
        self.assertEqual(state['verified_order_facts'],{})

    def test_verified_program_position_overrides_model_uncertainty(self):
        r=self.raw();r['at_bottom']=False
        self.assertTrue(parse_transcript(r,latest_position_verified=True).pending())
        r.pop('at_bottom')
        self.assertTrue(parse_transcript(r,latest_position_verified=True).pending())
        with self.assertRaises(ExtractionError):parse_transcript(r)

    def test_program_history_cannot_be_overridden_by_model(self):
        with self.assertRaises(ExtractionError):
            parse_transcript(self.raw(),latest_position_verified=False)

    def test_program_position_still_checks_readability_and_identity(self):
        r=self.raw();r['readable']=False
        with self.assertRaises(ExtractionError):parse_transcript(r,latest_position_verified=True)
        r=self.raw();r['messages'][0]['role']='unknown'
        with self.assertRaises(ExtractionError):parse_transcript(r,latest_position_verified=True)

    def test_verified_capture_does_not_ask_model_to_judge_scroll(self):
        r=self.raw();r.pop('at_bottom')
        h=Mock();h.post.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(r)}}]}
        reader=VisionReader({**DEFAULTS['vision'],'api_key':'test'},h)
        self.assertTrue(reader.read(png_bytes(),'客户甲',latest_position_verified=True).pending())
        prompt=h.post.call_args.kwargs['payload']['messages'][0]['content']
        self.assertNotIn('at_bottom',prompt)
        self.assertIn('最新位置由程序独立核对',prompt)

    def test_unverified_position_still_allows_transcription_without_scroll_prompt(self):
        h=Mock();r=self.raw();r.pop('at_bottom')
        h.post.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(r)}}]}
        reader=VisionReader({**DEFAULTS['vision'],'api_key':'test'},h)
        self.assertTrue(reader.read(png_bytes(),'客户甲',latest_position_verified=False).pending())
        self.assertNotIn('at_bottom',h.post.call_args.kwargs['payload']['messages'][0]['content'])

    def test_system_card_is_retained_without_pending_question(self):
        r=self.raw();r['messages']=[{'role':'system','text':'留资卡开启方案','timestamp':'','type':'card'}]
        t=parse_transcript(r)
        self.assertEqual(t.messages[0]['type'],'card')
        self.assertFalse(t.pending())

    def test_unknown_identity_is_not_treated_as_buyer(self):
        r=self.raw();r['messages'][0]['role']='unknown'
        with self.assertRaises(ExtractionError):parse_transcript(r)

    def test_media_type_blocks_pending_buyer_even_if_flag_incorrect(self):
        r=self.raw();r['messages'][0]['type']='image'
        with self.assertRaises(ExtractionError):parse_transcript(r)

    def test_historical_media_does_not_block_latest_text(self):
        r=self.raw();r['messages']=[{'role':'buyer','text':'[图片]','type':'image'}, {'role':'seller','text':'收到'}, {'role':'buyer','text':'什么时候发货','type':'text'}]
        self.assertEqual(parse_transcript(r).pending()[0]['text'],'什么时候发货')

    def raw(self):
        return {'readable':True,'at_bottom':True,'has_unsupported_content':False,'messages':[{'role':'buyer','text':'你好','timestamp':''}]}
    def test_packaged_prompt_matches_active_minimax_prompt(self):
        reference=(ROOT/'reference/VISION_PROMPT.txt').read_text(encoding='utf-8').strip()
        self.assertEqual(EXTRACTION_PROMPT.format(buyer_side='左'),reference)
        self.assertIn('不回答客户',reference)
        self.assertNotIn('at_bottom',reference)
    def test_parse_json(self):self.assertEqual(parse_transcript(json.dumps(self.raw())).pending()[0]['text'],'你好')
    def test_parse_fence(self):self.assertTrue(parse_transcript('```json\n'+json.dumps(self.raw())+'\n```').pending())
    def test_unreadable_stops(self):
        r=self.raw();r['readable']=False
        with self.assertRaisesRegex(ExtractionError,'readable 未通过'):parse_transcript(r)
    def test_not_at_bottom_stops(self):
        r=self.raw();r['at_bottom']=False
        with self.assertRaisesRegex(ExtractionError,'at_bottom 未通过'):parse_transcript(r)
    def test_both_uncertain_reasons_shown(self):
        r=self.raw();r['readable']=False;r['at_bottom']=False
        with self.assertRaises(ExtractionError) as caught:parse_transcript(r)
        self.assertIn('readable 未通过',str(caught.exception))
        self.assertIn('at_bottom 未通过',str(caught.exception))
    def test_unsupported_stops(self):
        r=self.raw();r['has_unsupported_content']=True
        with self.assertRaises(ExtractionError):parse_transcript(r)
    def test_invalid_role_stops(self):
        r=self.raw();r['messages'][0]['role']='assistant'
        with self.assertRaises(ExtractionError):parse_transcript(r)
    def test_seller_last_means_no_pending(self):self.assertFalse(transcript('seller').pending())
    def test_system_after_buyer_means_no_pending(self):
        r=self.raw();r['messages'].append({'role':'system','text':'退款成功','timestamp':'09-27 14:56'})
        self.assertFalse(parse_transcript(r).pending())
    def test_pending_groups_consecutive_buyer_messages(self):
        r=self.raw();r['messages']=[{'role':x,'text':str(i)} for i,x in enumerate(['buyer','seller','buyer','buyer'])]
        self.assertEqual([m['text'] for m in parse_transcript(r).pending()],['2','3'])
    def test_fingerprint_stable_and_customer_isolated(self):
        t=transcript();self.assertEqual(t.fingerprint('shop','a'),t.fingerprint('shop','a'))
        self.assertNotEqual(t.fingerprint('shop','a'),t.fingerprint('shop','b'))
        self.assertNotEqual(t.fingerprint('shop','a'),t.fingerprint('other','a'))
    def test_visible_echo_required(self):
        self.assertTrue(transcript('seller','固定原文').verifies_reply('固定原文'))
        self.assertFalse(transcript('buyer','固定原文').verifies_reply('固定原文'))
    def test_system_after_seller_cannot_verify_send(self):
        r=self.raw();r['messages']=[{'role':'seller','text':'固定原文'},
                                     {'role':'system','text':'订单状态变化'}]
        self.assertFalse(parse_transcript(r).verifies_reply('固定原文'))
    def test_image_hash_decoded_pixels(self):self.assertEqual(image_digest(png_bytes()),image_digest(png_bytes()))
    def test_compatible_payload(self):
        h=Mock();h.post.return_value={'choices':[{'message':{'content':json.dumps(self.raw())}}]}
        c={**DEFAULTS['vision'],'provider':'compatible',
           'endpoint':'https://example.com/chat/completions','model':'vision','api_key':'test'}
        VisionReader(c,h).read(png_bytes(),'customer')
        p=h.post.call_args.kwargs['payload']
        self.assertTrue(p['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
    def test_minimax_vision_payload(self):
        h=Mock();h.post.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(self.raw())}}]}
        c={**DEFAULTS['vision'],'provider':'minimax',
           'endpoint':'https://api.minimax.cn/v1/chat/completions',
           'model':'MiniMax-M3','api_key':'test'}
        self.assertEqual(VisionReader(c,h).read(png_bytes(),'customer').pending()[0]['text'],'你好')
        p=h.post.call_args.kwargs['payload']
        self.assertEqual(p['thinking'],{'type':'disabled'})
        self.assertEqual(p['model'],'MiniMax-M3')
        self.assertEqual(p['messages'][0]['role'],'system')
        self.assertEqual(p['messages'][0]['content'],EXTRACTION_PROMPT.format(buyer_side='左'))
        self.assertEqual(p['messages'][1]['content'][1]['image_url']['detail'],'high')
        self.assertTrue(p['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
    def test_minimax_http_request_to_transcript(self):
        response=Mock(status_code=200, headers={})
        response.json.return_value={'base_resp':{'status_code':0},'choices':[
            {'finish_reason':'stop','message':{'content':json.dumps(self.raw(),ensure_ascii=False)}}]}
        session=Mock();session.post.return_value=response
        c={**DEFAULTS['vision'],'api_key':'unit-test-subscription-key'}
        reader=VisionReader(c,JsonHttp(DEFAULTS['network'],session=session))
        self.assertEqual(reader.read(png_bytes(),'customer').pending()[0]['text'],'你好')
        args,kwargs=session.post.call_args
        self.assertEqual(args[0],c['endpoint'])
        self.assertEqual(kwargs['headers']['Authorization'],'Bearer unit-test-subscription-key')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['json']['model'],'MiniMax-M3')
    def test_customer_identity_context_uses_nickname_not_profile_key(self):
        h=Mock();h.post.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(self.raw())}}]}
        c={**DEFAULTS['vision'],'api_key':'test','buyer_side':'right'}
        VisionReader(c,h).read(png_bytes(),'profile123 :: 1 :: demo-customer')
        p=h.post.call_args.kwargs['payload']
        context=json.loads(p['messages'][1]['content'][0]['text'].split('\n',1)[1])
        self.assertEqual(context,{'current_customer_name':'demo-customer','customer_bubble_side':'right','account_role':'seller'})
        self.assertIn('客户气泡在 右 侧',p['messages'][0]['content'])

    def test_imported_image_has_no_invented_customer_name(self):
        h=Mock();h.post.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(self.raw())}}]}
        VisionReader({**DEFAULTS['vision'],'api_key':'test'},h).read(png_bytes(),'local-image-test')
        text=h.post.call_args.kwargs['payload']['messages'][1]['content'][0]['text']
        self.assertIsNone(json.loads(text.split('\n',1)[1])['current_customer_name'])
    def test_minimax_incomplete_or_filtered_response_stops(self):
        c={**DEFAULTS['vision'],'api_key':'test'}
        for response in (
            {'choices':[{'finish_reason':'length','message':{'content':json.dumps(self.raw())}}]},
            {'output_sensitive':True,'choices':[{'finish_reason':'stop','message':{'content':json.dumps(self.raw())}}]},
            {'base_resp':{'status_code':2013},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(self.raw())}}]},
        ):
            with self.subTest(response=response):
                h=Mock();h.post.return_value=response
                with self.assertRaises(ExtractionError):VisionReader(c,h).read(png_bytes(),'customer')
                captured=[]
                with self.assertRaises(ExtractionError):
                    VisionReader(c,h).read(png_bytes(),'customer',audit=captured.append)
                self.assertEqual(captured, [response])
    def test_minimax_rejects_unofficial_endpoint_before_upload(self):
        h=Mock()
        c={**DEFAULTS['vision'],'api_key':'test','endpoint':'https://example.com/v1/chat/completions'}
        with self.assertRaises(ApiError):VisionReader(c,h).read(png_bytes(),'customer')
        h.post.assert_not_called()
    def test_dify_exact_output_field(self):
        h=Mock();h.post.side_effect=[{'id':'file-test'},{'data':{'status':'succeeded','outputs':{'result':self.raw()}}}]
        c={**DEFAULTS['vision'],'provider':'dify','api_key':'test'}
        self.assertTrue(VisionReader(c,h).read(png_bytes(),'customer').pending())
    def test_dify_missing_field_not_silently_accepted(self):
        h=Mock();h.post.side_effect=[{'id':'file-test'},{'data':{'status':'succeeded','outputs':{'text':'wrong'}}}]
        c={**DEFAULTS['vision'],'provider':'dify','api_key':'test'}
        with self.assertRaises(ExtractionError):VisionReader(c,h).read(png_bytes(),'customer')

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'events.db';self.s=Store(self.path)
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def test_persistent_deduplication(self):
        d=Decision('candidate',text='x');a=self.s.add('key','customer',d)
        self.assertTrue(a);self.assertIsNone(self.s.add('key','customer',d))
        self.s.close();self.s=Store(self.path);self.assertTrue(self.s.seen('key'))
    def test_sending_claim_is_once_only(self):
        i=self.s.add('key','c',Decision('candidate',text='x'))
        self.assertTrue(self.s.begin_send(i));self.assertFalse(self.s.begin_send(i))
    def test_crashed_send_blocks_customer(self):
        i=self.s.add('key','c',Decision('candidate',text='x'));self.s.begin_send(i)
        self.s.close();self.s=Store(self.path);self.assertTrue(self.s.is_blocked('c'))
    def test_resume_does_not_erase_old_event(self):
        self.s.add('key','c',Decision('human'));self.s.block('c','reason');self.s.resume('c')
        self.assertFalse(self.s.is_blocked('c'));self.assertTrue(self.s.seen('key'))
    def test_vision_and_operation_history_survive_restart(self):
        first = self.s.start_vision('当前会话预览', '客户甲', png_bytes())
        self.s.update_vision(first, response={'choices': []}, status='vision_failed', error='无法识字')
        second = self.s.start_vision('导入截图测试', '客户乙', png_bytes())
        self.s.record_action('vision_capture', customer='客户乙', details={'audit_id': second})
        self.s.close(); self.s = Store(self.path)
        self.assertEqual([r['id'] for r in self.s.vision_history()], [second, first])
        self.assertEqual(self.s.vision_record(first)['image_png'], png_bytes())
        self.assertEqual(self.s.vision_record(first)['error'], '无法识字')
        self.assertEqual(self.s.recent_actions()[0]['action'], 'vision_capture')

class ServiceTests(unittest.TestCase):
    def test_program_position_reaches_parser_and_audit(self):
        evidence={'before':{'top':0},'after':{'top':0}}
        snap=Snapshot('客户甲',self.png,image_digest(self.png),True,evidence)
        self.desktop.read_current.return_value=snap
        h=Mock()
        h.post.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
            'readable':True,'has_unsupported_content':False,
            'messages':[{'role':'buyer','text':'什么时候发货','timestamp':'','type':'text'}]})}}]}
        self.s.reader=VisionReader({**DEFAULTS['vision'],'api_key':'test'},h)
        self.s.preview_current()
        self.assertEqual(self.store.latest_vision()['status'],'previewed')
        logs=self.store.recent_actions()
        checked=next(r for r in logs if r['action']=='chat_position_verified')
        self.assertEqual(json.loads(checked['details_json'])['evidence'],evidence)
        self.desktop.send_reply.assert_not_called()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'e.db')
        self.cfg=copy.deepcopy(DEFAULTS);self.cfg['desktop'].update(data_upload_authorized=True,auto_send_enabled=True,locators_reviewed=True,unread_locator_reviewed=True,settle_seconds=.5)
        self.stop=threading.Event();self.events=[]
        self.png=png_bytes();self.snap=Snapshot('客户甲',self.png,image_digest(self.png))
        self.desktop=Mock();self.desktop.read_current.return_value=self.snap
        self.reader=Mock();self.reader.read.return_value=transcript()
        self.router=Mock();self.router.decide.return_value=Decision('candidate','r','固定原文','ok',['物流'],[],True)
        self.s=Service(self.cfg,self.stop,self.events.append,desktop=self.desktop,router=self.router,reader=self.reader,store=self.store)
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def make_preview(self):
        self.s.preview_current();return next(e['id'] for e in self.events if e['kind']=='decision')
    def test_whitelist_manual_preview_repeats_without_sendable_candidate(self):
        customer=self.s.customer_key(self.snap.customer)
        self.store.db.execute('INSERT INTO preview_whitelist VALUES (?)',(customer,));self.store.db.commit()
        self.store.block(customer,'old failure')
        for _ in range(2):self.s.preview_current(manual_preview=True)
        self.assertEqual(self.reader.read.call_count,2)
        self.assertEqual(self.router.decide.call_count,2)
        self.assertFalse(self.s.pending)
        self.assertTrue(all(e.get('id') is None for e in self.events if e['kind']=='decision'))
        self.desktop.send.assert_not_called()
        self.s.preview_current()
        self.assertEqual(self.reader.read.call_count,2)

    def test_preview_never_sends(self):
        self.make_preview();self.desktop.send_reply.assert_not_called()
        record = self.store.latest_vision()
        self.assertEqual(record['image_png'], self.png)
        self.assertEqual(record['status'], 'previewed')
        self.assertIn('固定原文', record['decision_json'])
    def test_unverified_preview_never_claims_or_sends_message(self):
        self.s.unverified_preview_mode = True
        self.s.preview_current()
        self.assertFalse(self.s.pending)
        self.assertEqual(self.store.recent(), [])
        self.assertIsNone(self.events[-1]['id'])
        with self.assertRaises(DesktopError):
            self.s.send('anything',manual=True)
        self.desktop.send_reply.assert_not_called()
        self.s.unverified_preview_mode = False
        self.make_preview()
        self.assertTrue(self.s.pending)
    def test_unverified_preview_blocks_auto_before_reading(self):
        self.s.unverified_preview_mode = True
        with self.assertRaises(DesktopError):
            self.s.scan_once(auto=True)
        self.desktop.open_next_unread.assert_not_called()
    def test_duplicate_frame_skips_api(self):
        self.s.preview_current();self.s.preview_current();self.assertEqual(self.reader.read.call_count,1)
        self.assertEqual(self.events[-1]['kind'],'preview_skipped')
        self.assertIn('未重新调用',self.events[-1]['text'])
        self.desktop.send_reply.assert_not_called()
    def test_duplicate_message_reports_completed_ocr_without_sending(self):
        self.s.preview_current()
        self.desktop.read_current.return_value=Snapshot('客户甲',self.png,'different-frame')
        self.s.preview_current()
        self.assertEqual(self.reader.read.call_count,2)
        self.assertEqual(self.router.decide.call_count,1)
        self.assertEqual(self.events[-1]['kind'],'preview_skipped')
        self.assertIn('已完成本次识字',self.events[-1]['text'])
        self.assertEqual(self.store.latest_vision()['status'],'duplicate_message')
        self.desktop.send_reply.assert_not_called()
    def test_upload_consent_required(self):
        self.cfg['desktop']['data_upload_authorized']=False
        with self.assertRaises(DesktopError):self.s.preview_current()
        self.reader.read.assert_not_called()
    def test_seller_last_never_routes(self):
        self.reader.read.return_value=transcript('seller');self.s.preview_current()
        self.router.decide.assert_not_called()
    def test_system_notice_after_buyer_never_routes(self):
        self.reader.read.return_value=parse_transcript({
            'readable':True,'at_bottom':True,'has_unsupported_content':False,
            'messages':[{'role':'buyer','text':'你要干啥'},
                        {'role':'system','text':'退款成功，钱款已原路退还'}]})
        self.s.preview_current(auto=False)
        self.router.decide.assert_not_called()
        self.desktop.send_reply.assert_not_called()
    def test_exact_reply_sent_and_visually_verified(self):
        i=self.make_preview();self.reader.read.return_value=Transcript(transcript().messages + transcript('seller','固定原文').messages)
        self.s.send(i,manual=True)
        self.desktop.send_reply.assert_called_once_with(self.snap,'固定原文')
        self.assertEqual(self.store.status(i),'sent_ui_verified')
    def test_unknown_send_does_not_retry(self):
        i=self.make_preview();self.desktop.send_reply.side_effect=DesktopError('test failure')
        self.s.send(i,manual=True)
        self.assertEqual(self.store.status(i),'send_unknown');self.assertTrue(self.store.is_blocked(self.s.customer_key('客户甲')))
        with self.assertRaises(DesktopError):self.s.send(i,manual=True)
        self.assertEqual(self.desktop.send_reply.call_count,1)
    def test_changed_window_blocks_before_send(self):
        i=self.make_preview();self.desktop.ensure_unchanged.side_effect=DesktopError('changed')
        with self.assertRaises(DesktopError):self.s.send(i,manual=True)
        self.desktop.send_reply.assert_not_called()
    def test_unapproved_template_not_auto_sent(self):
        self.router.decide.return_value.approved=False
        with self.assertRaises(DesktopError):
            self.s.preview_current(auto=True)
        self.desktop.send_reply.assert_not_called()
    def test_human_decision_blocks_customer(self):
        self.router.decide.return_value=Decision('human',reason='unknown');self.s.preview_current()
        self.assertTrue(self.store.is_blocked(self.s.customer_key('客户甲')))
        self.desktop.send_reply.assert_not_called()
    def test_blocked_customer_can_be_selected_for_resume(self):
        customer=self.s.customer_key('客户甲');self.store.block(customer,'manual')
        self.s.preview_current()
        self.assertEqual(self.events[-1]['kind'],'blocked')
        self.assertEqual(self.events[-1]['customer'],customer)
        self.reader.read.assert_not_called()
    def test_stop_before_send(self):
        i=self.make_preview();self.stop.set()
        with self.assertRaises(Cancelled):self.s.send(i,manual=True)
        self.desktop.send_reply.assert_not_called()
    def test_rejected_draft_not_sendable(self):
        i=self.make_preview();self.s.reject(i)
        with self.assertRaises(DesktopError):self.s.send(i,manual=True)
    def test_limit_blocks_further_send(self):
        i=self.make_preview()
        for _ in range(3):self.store.record_attempt()
        with self.assertRaises(DesktopError):self.s.send(i,manual=True)
        self.desktop.send_reply.assert_not_called()

if __name__=='__main__':unittest.main()
