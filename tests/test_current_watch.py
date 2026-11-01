import hashlib
import unittest
from unittest.mock import Mock
import test_core
from jevtool.desktop import Snapshot, DesktopError
from jevtool.vision import Transcript
from jevtool.router import Decision

class CurrentWatchTests(unittest.TestCase):
    def setUp(self):
        test_core.ServiceTests.setUp(self)
        self.desktop.open_next_unread.return_value=False
        self.observation={'customer':'客户甲','content_key':self.digest('old'),'latest':True,'draft':False}
        self.desktop.observe_current.side_effect=lambda:dict(self.observation)
        self.current=Snapshot('客户甲',self.png,'new-frame',True,{'after':{'content':'new'}})
        self.desktop.read_current.return_value=self.current
    def tearDown(self):test_core.ServiceTests.tearDown(self)
    def digest(self,text):return hashlib.sha256(text.encode()).hexdigest()
    def changed(self):self.observation['content_key']=self.digest('new')
    def stable_change(self):
        self.s.scan_once();self.changed();self.s.scan_once();self.s.scan_once()
    def test_start_and_unchanged_page_never_call_models(self):
        for _ in range(5):self.s.scan_once()
        self.reader.read.assert_not_called();self.router.decide.assert_not_called()
        self.desktop.read_current.assert_not_called()
    def test_followup_without_red_dot_requires_two_stable_observations(self):
        self.s.scan_once();self.changed();self.s.scan_once()
        self.reader.read.assert_not_called()
        self.s.scan_once()
        self.reader.read.assert_called_once();self.router.decide.assert_called_once()
        self.desktop.send_reply.assert_not_called()
        self.assertTrue(self.s.pending)
    def test_own_or_system_message_does_not_route_or_repeat(self):
        self.reader.read.return_value=Transcript([{'role':'seller','text':'自己的回复'}])
        self.stable_change()
        for _ in range(3):self.s.scan_once()
        self.reader.read.assert_called_once();self.router.decide.assert_not_called()
    def test_draft_preserves_input_and_avoids_switching(self):
        self.s.scan_once();self.changed();self.observation['draft']=True
        self.desktop.open_next_unread.reset_mock()
        for _ in range(3):self.s.scan_once()
        self.reader.read.assert_not_called();self.desktop.open_next_unread.assert_not_called()
    def test_history_position_defers_current_ocr(self):
        self.s.scan_once();self.changed();self.observation['latest']=False
        for _ in range(3):self.s.scan_once()
        self.reader.read.assert_not_called()
    def test_historical_monitor_prepares_latest_then_builds_baseline_without_api(self):
        old=dict(self.observation,latest=False)
        self.desktop.observe_current.side_effect=[old,dict(self.observation)]
        self.s.scan_once()
        self.desktop.return_to_latest.assert_called_once_with('客户甲')
        self.reader.read.assert_not_called();self.router.decide.assert_not_called()
    def test_monitor_never_scrolls_customer_paused_for_human(self):
        self.observation['latest']=False
        self.store.block(self.s.customer_key('客户甲'),'交人工')
        self.s.scan_once()
        self.desktop.return_to_latest.assert_not_called();self.reader.read.assert_not_called()
    def test_monitor_with_draft_never_scrolls(self):
        self.observation.update(latest=False,draft=True)
        self.s.scan_once()
        self.desktop.return_to_latest.assert_not_called();self.desktop.open_next_unread.assert_not_called()
    def test_customer_switch_during_preparation_stops_before_model(self):
        old=dict(self.observation,latest=False)
        self.desktop.observe_current.side_effect=[old,dict(self.observation,customer='客户乙')]
        with self.assertRaisesRegex(DesktopError,'客户不一致'):self.s.scan_once()
        self.reader.read.assert_not_called();self.desktop.open_next_unread.assert_not_called()
    def test_human_customer_is_skipped_without_repeated_api_calls(self):
        self.router.decide.return_value=Decision('human',reason='交人工')
        self.stable_change();self.observation['content_key']=self.digest('another message')
        for _ in range(4):self.s.scan_once()
        self.reader.read.assert_called_once()
    def test_unread_and_watch_share_deduplication(self):
        self.s.scan_once();self.changed();self.desktop.open_next_unread.return_value=True
        self.s.scan_once();self.desktop.open_next_unread.return_value=False
        for _ in range(3):self.s.scan_once()
        self.reader.read.assert_called_once();self.router.decide.assert_called_once()
    def test_queued_followup_can_revisit_after_switch(self):
        key=self.s.customer_key('客户甲')
        self.s.observed[key]={'name':'客户甲','baseline':self.digest('old'),
            'candidate':self.digest('new'),'stable':1,'deferred':None}
        self.observation.update(customer='客户乙',content_key=self.digest('b'))
        def revisit(name):
            self.observation.update(customer=name,content_key=self.digest('new'))
            return True
        self.desktop.open_watched_customer.side_effect=revisit
        self.s.next_watch_turn=True
        self.s.scan_once()
        self.desktop.open_watched_customer.assert_called_once_with('客户甲')
        self.reader.read.assert_called_once()
    def test_revisit_nonunique_marks_only_that_customer(self):
        key=self.s.customer_key('客户甲')
        self.s.observed[key]={'name':'客户甲','baseline':'old','candidate':'new','stable':1,'deferred':None}
        self.observation['customer']='客户乙';self.desktop.open_watched_customer.return_value=False
        self.s.next_watch_turn=True;self.s.scan_once()
        self.assertTrue(self.store.is_blocked(key));self.reader.read.assert_not_called()
    def test_restart_reset_establishes_baseline_instead_of_replaying(self):
        self.s.scan_once();self.changed();self.s.reset_observation();self.s.scan_once()
        self.reader.read.assert_not_called()
    def test_watch_processing_consumes_poll_even_when_no_buyer_pending(self):
        self.reader.read.return_value=Transcript([{'role':'system','text':'系统消息'}])
        self.s.scan_once();self.changed();self.s.scan_once()
        self.s.next_watch_turn=True;self.desktop.open_next_unread.reset_mock()
        self.s.scan_once()
        self.reader.read.assert_called_once();self.desktop.open_next_unread.assert_not_called()
    def test_send_followed_immediately_by_buyer_is_confirmed_and_queued(self):
        self.s.preview_current();id_=next(e['id'] for e in self.events if e['kind']=='decision')
        before=self.s.pending_transcripts[id_]
        self.reader.read.return_value=Transcript(before.messages+[{'role':'seller','text':'固定原文'},
                                                                  {'role':'buyer','text':'还有个问题'}])
        self.s.send(id_,manual=True)
        self.assertEqual(self.store.status(id_),'sent_ui_verified')
        self.assertTrue(self.s.observed[self.s.customer_key('客户甲')]['candidate'])
        self.desktop.send_reply.assert_called_once()
    def test_historical_same_reply_does_not_confirm_new_send(self):
        self.s.preview_current();id_=next(e['id'] for e in self.events if e['kind']=='decision')
        self.reader.read.return_value=self.s.pending_transcripts[id_]
        self.s.send(id_,manual=True)
        self.assertEqual(self.store.status(id_),'send_unknown')
        with self.assertRaises(DesktopError):self.s.send(id_,manual=True)
        self.desktop.send_reply.assert_called_once()
    def test_alternating_priority_serves_unread_after_current(self):
        self.s.scan_current_change=Mock(return_value=True)
        self.s.next_watch_turn=True;self.s.scan_once()
        self.desktop.open_next_unread.assert_not_called()
        self.desktop.open_next_unread.return_value=True;self.s.scan_once()
        self.desktop.open_next_unread.assert_called_once()
    def test_same_question_in_new_dom_occurrence_is_not_old_message(self):
        self.s.preview_current()
        first=next(e['id'] for e in self.events if e['kind']=='decision')
        self.store.set_status(first,'sent_ui_verified')
        self.desktop.read_current.return_value=Snapshot('客户甲',self.png,'next-frame',True,
                                                        {'after':{'content':'new occurrence after seller reply'}})
        self.s.preview_current()
        ids=[e['id'] for e in self.events if e['kind']=='decision']
        self.assertEqual(len(ids),2)
        self.assertNotEqual(ids[0],ids[1])
    def test_same_dom_with_different_rendering_does_not_recall_ocr(self):
        self.s.preview_current()
        self.desktop.read_current.return_value=Snapshot('客户甲',self.png,'different rendering',True,
                                                        {'after':{'content':'new'}})
        self.s.preview_current()
        self.reader.read.assert_called_once()

    def test_continuous_changes_wait_for_stable_content(self):
        self.s.scan_once()
        for text in ('one','two','three'):
            self.observation['content_key']=self.digest(text)
            self.s.scan_once()
        self.reader.read.assert_not_called()
        self.observation['content_key']=self.digest('new')
        self.s.scan_once();self.s.scan_once()
        self.reader.read.assert_called_once()
    def test_unauthorized_auto_stops_before_probe(self):
        with self.assertRaises(DesktopError):self.s.scan_once(auto=True)
        self.desktop.observe_current.assert_not_called();self.reader.read.assert_not_called()
    def test_switching_to_unknown_customer_establishes_baseline(self):
        self.s.scan_once();self.observation.update(customer='客户乙',content_key=self.digest('different'))
        self.s.scan_once()
        self.reader.read.assert_not_called()
    def test_missing_dom_structure_stops_without_api(self):
        self.desktop.observe_current.side_effect=DesktopError('结构失效')
        with self.assertRaises(DesktopError):self.s.scan_once()
        self.reader.read.assert_not_called()


class ReplyOverlapTests(unittest.TestCase):
    def test_ambiguous_overlap_rejects(self):
        old=Transcript([{'role':'buyer','text':'同样问题'}])
        new=Transcript(old.messages+old.messages+[{'role':'seller','text':'回复'}])
        self.assertFalse(new.verifies_new_reply(old,'回复'))
    def test_cropped_history_uses_unique_tail(self):
        old=Transcript([{'role':'seller','text':'旧回复'},{'role':'buyer','text':'新问题'}])
        new=Transcript([old.messages[-1],{'role':'seller','text':'回复'},{'role':'buyer','text':'追问'}])
        self.assertTrue(new.verifies_new_reply(old,'回复'))
