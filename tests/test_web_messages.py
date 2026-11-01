import threading
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch
import test_core as core
import test_desktop_guards as guards
from jevtool.desktop import ClickniumDesktop, DesktopError
from jevtool.network import Cancelled
from jevtool.web_messages import MessageState, parse_messages, compare_reply

def msg(role, text, type='text'):
    return {'role':role,'type':type,'text':text,'media':[]}

def state(*messages, customer='客户甲', content='summary'):
    return MessageState(customer,parse_messages(list(messages)),{'content':content})

class MessageComparisonTests(unittest.TestCase):
    def setUp(self):
        self.old=state(msg('seller','固定原文'),msg('buyer','几号到货'))
    def check(self, added, reason, passed=False, old=None, customer='客户甲'):
        after=state(*(self.old.messages if old is None else old),*added,customer=customer)
        result=compare_reply(self.old,after,'固定原文')
        self.assertEqual(result[:2],(passed,reason))
    def test_old_same_reply_is_not_success(self):
        self.check([],'no_new_message')
    def test_new_same_reply_is_success(self):
        self.check([msg('seller','固定原文')],'new_seller_reply_verified',True)
    def test_multiple_buyer_followups_allowed(self):
        self.check([msg('seller','固定原文'),msg('buyer','谢谢'),msg('buyer','还有呢')],'new_seller_reply_verified',True)
    def test_duplicate_new_seller_replies_rejected(self):
        self.check([msg('seller','固定原文'),msg('seller','固定原文')],'seller_reply_not_unique')
    def test_similar_text_is_not_exact(self):
        self.check([msg('seller','固定 原文')],'reply_text_mismatch')
    def test_buyer_echo_is_not_seller_reply(self):
        self.check([msg('buyer','固定原文')],'unexpected_message_before_reply')
    def test_loaded_older_history_rejected(self):
        self.check([msg('seller','固定原文')],'history_changed',old=[msg('buyer','更早'),*self.old.messages])
    def test_replaced_history_rejected(self):
        self.check([msg('seller','固定原文')],'history_changed',old=[msg('buyer','不同')])
    def test_customer_switch_rejected(self):
        self.check([msg('seller','固定原文')],'customer_changed',customer='另一位')
    def test_media_reply_does_not_count_as_text(self):
        self.check([msg('seller','固定原文','card')],'reply_text_mismatch')
    def test_unknown_role_and_empty_body_rejected(self):
        for item in (msg('unknown','a'),msg('buyer','')):
            with self.assertRaises(ValueError):parse_messages([item])
    def test_read_receipts_styles_and_coordinates_are_excluded(self):
        self.assertEqual(parse_messages([{**msg('seller','固定原文'),'read':'已读','x':1,'class':'hover'}]),parse_messages([msg('seller','固定原文')]))

class PollingTests(unittest.TestCase):
    def setUp(self):
        self.clock=0
        self.d=ClickniumDesktop.__new__(ClickniumDesktop)
        self.d.stop=Mock();self.d.stop.is_set.return_value=False
        def wait(delay):self.clock+=delay;return False
        self.d.stop.wait.side_effect=wait
        self.before=state(msg('buyer','问题'))
        self.after=state(*self.before.messages,msg('seller','回复'))
    def run_poll(self, states):
        self.d.read_message_state=Mock(side_effect=states if isinstance(states,list) else None)
        if not isinstance(states,list):self.d.read_message_state.return_value=states
        with patch('jevtool.desktop.time.monotonic',side_effect=lambda:self.clock),patch('jevtool.desktop.record'):
            return self.d.verify_web_reply(self.before,'回复')
    def test_two_matching_reads_required(self):
        self.assertEqual(self.run_poll([self.after,self.after])[0],self.after)
        self.assertEqual(self.d.read_message_state.call_count,2)
        self.assertEqual(self.clock,.25)
    def test_receipt_and_scroll_dimensions_do_not_affect_message_stability(self):
        second=MessageState(self.after.customer,self.after.messages,{'content':'已读','height':1975})
        self.assertEqual(self.run_poll([self.after,second])[0],second)
    def test_pending_then_stable_reply(self):
        self.run_poll([self.before,self.after,self.after])
        self.assertEqual(self.d.read_message_state.call_count,3)
    def test_no_new_reply_times_out_without_retry_click(self):
        self.d.send_reply=Mock()
        with self.assertRaises(DesktopError) as caught:self.run_poll(self.before)
        self.assertEqual(caught.exception.evidence['reason'],'no_new_message')
        self.assertEqual(self.clock,5)
        self.assertEqual(self.d.read_message_state.call_count,20)
        self.d.send_reply.assert_not_called()
    def test_customer_switch_stops_immediately(self):
        changed=MessageState('另一位',self.after.messages,{})
        with self.assertRaises(DesktopError):self.run_poll(changed)
        self.assertEqual(self.d.read_message_state.call_count,1)
    def test_structure_error_stops(self):
        with self.assertRaises(DesktopError):self.run_poll([DesktopError('结构异常')])
    def test_continuous_followups_do_not_confirm_unstable_sequence(self):
        samples=[state(*self.before.messages,msg('seller','回复'),*[msg('buyer',str(j)) for j in range(i)]) for i in range(20)]
        with self.assertRaises(DesktopError) as caught:self.run_poll(samples)
        self.assertEqual(caught.exception.evidence['stable_count'],1)
        self.assertEqual(self.clock,5)
    def test_stop_during_wait_cancels(self):
        self.d.stop.wait.side_effect=None;self.d.stop.wait.return_value=True
        with self.assertRaises(Cancelled):self.run_poll(self.after)

class ServiceWebVerificationTests(unittest.TestCase):
    def setUp(self):
        core.ServiceTests.setUp(self)
        self.id=core.ServiceTests.make_preview(self)
        self.customer=self.s.customer_key(self.snap.customer)
        d=ClickniumDesktop.__new__(ClickniumDesktop)
        d.cfg={'adapter':'xianyu_web','locators':{'chat_window':'goofish.message'}}
        d.ensure_unchanged=Mock();d.send_reply=Mock()
        d.web_send_baseline=state(msg('buyer','问题'))
        d.verify_web_reply=Mock(return_value=(state(msg('buyer','问题'),msg('seller','固定原文')), (msg('seller','固定原文'),)))
        def click(*args):d.click_intent=True;d.send_phase='click_returned'
        d.send_reply.side_effect=click
        self.s.desktop=d;self.desktop=d
        self.s.read_audited=Mock(side_effect=AssertionError('发送核对禁止额外调用模型'))
    def tearDown(self):core.ServiceTests.tearDown(self)
    def test_web_success_uses_no_model_and_syncs_baseline(self):
        self.s.send(self.id,manual=True)
        self.assertEqual(self.store.status(self.id),'sent_ui_verified')
        self.s.read_audited.assert_not_called()
        self.assertIn(self.customer,self.s.observed)
    def test_immediate_buyer_followup_is_pending_next_round(self):
        added=(msg('seller','固定原文'),msg('buyer','追问'))
        self.desktop.verify_web_reply.return_value=(state(msg('buyer','问题'),*added),added)
        self.s.send(self.id,manual=True)
        self.assertEqual(self.store.status(self.id),'sent_ui_verified')
        self.assertEqual(self.s.observed[self.customer]['baseline'],'pending-followup')
    def test_uncertain_reply_pauses_without_resend(self):
        self.desktop.verify_web_reply.side_effect=DesktopError('历史被替换')
        self.s.send(self.id,manual=True)
        self.assertEqual(self.store.status(self.id),'send_unknown')
        self.desktop.send_reply.assert_called_once()
        self.assertTrue(self.store.is_blocked(self.customer))
    def test_click_exception_is_unknown_not_retried(self):
        def uncertain(*args):
            self.desktop.click_intent=True;raise DesktopError('点击调用中断')
        self.desktop.send_reply.side_effect=uncertain
        self.s.send(self.id,manual=True)
        self.assertEqual(self.store.status(self.id),'send_unknown')
        self.desktop.verify_web_reply.assert_not_called()
        self.desktop.send_reply.assert_called_once()

class WebBaselineGuardTests(unittest.TestCase):
    def setUp(self):
        guards.DesktopGuardTests.setUp(self)
        self.d.cfg={'adapter':'xianyu_web','locators':{'chat_window':'goofish.message'}}
        self.base=replace(self.base,position_evidence={'after':{'content':'summary'}})
        self.d.read_message_state=Mock(return_value=state(msg('buyer','问题'),customer='c'))
    paste=guards.DesktopGuardTests.paste
    def test_baseline_saved_before_click(self):
        self.d.send_reply(self.base,'固定原文')
        self.assertIsNotNone(self.d.web_send_baseline)
        self.button.click.assert_called_once()
    def test_customer_changed_never_clicks(self):
        self.d.read_message_state.return_value=state(msg('buyer','问题'),customer='另一位')
        with self.assertRaises(DesktopError):self.d.send_reply(self.base,'固定原文')
        self.button.click.assert_not_called()
    def test_content_changed_never_clicks(self):
        self.d.read_message_state.return_value=state(msg('buyer','问题'),customer='c',content='changed')
        with self.assertRaises(DesktopError):self.d.send_reply(self.base,'固定原文')
        self.button.click.assert_not_called()
    def test_unreadable_structure_never_clicks(self):
        self.d.read_message_state.side_effect=DesktopError('结构异常')
        with self.assertRaises(DesktopError):self.d.send_reply(self.base,'固定原文')
        self.button.click.assert_not_called()
