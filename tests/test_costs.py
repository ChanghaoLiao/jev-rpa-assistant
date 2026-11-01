import json
import copy
import io
from decimal import Decimal
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from jevtool.config import DEFAULTS
from jevtool.costs import calculate, default_rates, validate_rates
from jevtool.network import JsonHttp, ApiError, Cancelled
from jevtool.store import Store
from jevtool.service import Service
from jevtool.vision import ExtractionError
from PIL import Image

class CostTests(unittest.TestCase):
    def test_jev_output_is_free_but_input_is_metered(self):
        rates = default_rates('jev','https://api.typesafe.ai/v1/systemone','jev-latest')
        self.assertEqual(calculate({'input_tokens':1000,'output_tokens':60},rates)['estimated'],'0.000042')

    def test_failed_extraction_still_records_usage_and_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = copy.deepcopy(DEFAULTS)
            cfg['vision']['api_key'] = 'sk-cp-private-test-key'
            store = Store(Path(directory)/'e.db')
            service = Service(cfg,threading.Event(),Mock(),store=store)
            response = Mock(status_code=200)
            response.json.return_value = {'usage':{'prompt_tokens':1000,'completion_tokens':100},
                'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'readable':False,
                    'at_bottom':False,'has_unsupported_content':False,'messages':[]})}}]}
            service.http.session = Mock()
            service.http.session.post.return_value = response
            buf = io.BytesIO()
            Image.new('RGB',(20,20),'white').save(buf,format='PNG')
            with self.assertRaises(ExtractionError):
                service.read_audited(buf.getvalue(),'customer','识字测试')
            report = store.cost_report()
            self.assertEqual(len(report['rows']),1)
            self.assertEqual(report['rows'][0]['estimated'],'0.00294')
            self.assertEqual(report['rows'][0]['billing'],'subscription')
            self.assertEqual(store.latest_vision()['status'],'vision_failed')
            self.assertNotIn(cfg['vision']['api_key'],repr(report))
            service.close()

    def test_cached_input_and_output_are_not_double_counted(self):
        rates = default_rates('minimax','https://api.minimax.cn/v1/chat/completions','MiniMax-M3',True)
        result = calculate({'prompt_tokens':1000,'completion_tokens':100,
                            'prompt_tokens_details':{'cached_tokens':200}}, rates)
        self.assertEqual(result['estimated'],'0.002604')
        self.assertEqual(rates['billing'],'subscription')

    def test_unknown_usage_and_rates_are_never_zero(self):
        rates = default_rates('other','','unknown')
        for usage in (None, {}, {'input_tokens':100,'output_tokens':20},
                      {'input_tokens':True,'output_tokens':20},
                      {'input_tokens':10,'output_tokens':20,'cache_read_input_tokens':11}):
            self.assertIsNone(calculate(usage,rates)['estimated'])

    def test_long_context_and_priority(self):
        rates = default_rates('minimax','https://api.minimax.io/v1/chat/completions','MiniMax-M3')
        self.assertEqual(Decimal(calculate({'prompt_tokens':600000,'completion_tokens':0},rates,
                                  model='MiniMax-M3',priority=True)['estimated']),Decimal('0.54'))

    def test_invalid_rates_rejected(self):
        for value in ('NaN','Infinity','-1','bad'):
            with self.assertRaises(ValueError):
                validate_rates({'currency':'USD','billing':'payg','input':value})

    def test_history_snapshot_and_currencies_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'events.db'
            store = Store(path)
            rates = default_rates('minimax','https://api.minimax.cn/v1/chat/completions','MiniMax-M3',True)
            item = {'provider':'minimax','model':'MiniMax-M3','attempt':1}
            first = store.start_api_call(item,rates)
            store.finish_api_call(first,{'status':'response_received','usage':{'prompt_tokens':1000,'completion_tokens':100}})
            store.set_rates('minimax',{'currency':'USD','billing':'payg','input':'99','output':'99','cached':'99'})
            second = store.start_api_call(item,store.get_rates('minimax'))
            store.finish_api_call(second,{'status':'network_unknown','usage':None})
            store.close()
            store = Store(path)
            report = store.cost_report()
            self.assertEqual(len(report['summary']),2)
            self.assertEqual(len(report['rows']),2)
            original = next(row for row in report['rows'] if row['id']==first)
            self.assertEqual(original['estimated'],'0.00294')
            self.assertEqual(original['currency'],'CNY')
            self.assertTrue(next(row for row in report['rows'] if row['id']==second)['unknown_reason'])
            store.close()

class NetworkMeterTests(unittest.TestCase):
    def test_retries_record_every_attempt_without_keys_or_prompt(self):
        observer = Mock(return_value='call')
        error = Mock(status_code=429,headers={})
        success = Mock(status_code=200,headers={})
        success.json.return_value = {'usage':{'input_tokens':10,'output_tokens':2}}
        session = Mock()
        session.post.side_effect = [error,success]
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        http = JsonHttp({**DEFAULTS['network'],'attempts':2},stop,session,observer)
        http.post('https://api.typesafe.ai/v1/systemone','secret-key',payload={'model':'jev-latest','prompt':'private-text'})
        phases = [call.args[0] for call in observer.call_args_list]
        self.assertEqual(phases,['start','finish','start','finish'])
        metadata = repr(observer.call_args_list)
        self.assertNotIn('secret-key',metadata)
        self.assertNotIn('private-text',metadata)
        self.assertEqual(observer.call_args_list[-1].args[1]['usage']['input_tokens'],10)

    def test_usage_is_recorded_before_cancellation(self):
        observer = Mock(return_value='call')
        session = Mock()
        response = Mock(status_code=200)
        response.json.return_value = {'usage':{'input_tokens':10,'output_tokens':2}}
        session.post.return_value = response
        stop = Mock()
        stop.is_set.side_effect = [False,True]
        http = JsonHttp(DEFAULTS['network'],stop,session,observer)
        with self.assertRaises(Cancelled):
            http.post('https://api.typesafe.ai/v1/systemone','key',payload={})
        self.assertEqual(observer.call_args_list[-1].args[0],'finish')

if __name__ == '__main__':
    unittest.main()
