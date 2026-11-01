from __future__ import annotations
import hashlib
import json
import threading
import time
from pathlib import Path
from .paths import DATA_ROOT, knowledge_file
from .desktop import ClickniumDesktop, Snapshot, DesktopError
from .knowledge import Knowledge
from .network import JsonHttp, Cancelled
from .router import JevClient, Router, Decision
from .store import Store
from .vision import VisionReader, Transcript, vision_signature
from .onboarding import can_auto_send
from .costs import default_rates
from .audit import record, traced, exception_details, register_secrets, platform_id

class Service:
    """One worker owns all UIA objects, API requests, and the SQLite connection."""
    def __init__(self, cfg: dict, stop: threading.Event, emit, *, desktop=None, router=None, reader=None, store=None):
        register_secrets(cfg)
        platform_id.set(cfg.get('active_profile', 'default') + ' :: ' + cfg['desktop']['shop_id'])
        self.cfg, self.stop, self.emit = cfg, stop, emit
        self.http = JsonHttp(cfg['network'], stop)
        self.knowledge = Knowledge.load(knowledge_file(cfg['knowledge']['replies_csv']),
                                        knowledge_file(cfg['knowledge']['categories_json']))
        self.router = router or Router(self.knowledge, JevClient(cfg['jev'], self.http), cfg['jev'])
        self.reader = reader or VisionReader(cfg['vision'], self.http)
        self.store = store or Store(DATA_ROOT / 'profiles' / cfg.get('active_profile', 'default') / 'events.sqlite3')
        self.desktop = desktop
        self.pending: dict[str, tuple[Snapshot, Decision, str]] = {}
        self.unverified_preview_mode = False
        self.observed = {}
        self.next_watch_turn = False
        self.pending_transcripts = {}
        self.scan_consumed = False
        self.current_draft = False
        self.watched_name = None
        self.http.observer = self.observe_api

    def observe_api(self, phase, item):
        if phase == 'start':
            rates = self.store.get_rates(item['provider']) or default_rates(
                item['provider'], item['endpoint'], item['model'], item['subscription'])
            return self.store.start_api_call(item, rates)
        self.store.finish_api_call(item['id'], item)

    @traced('service.decide_audited')
    def decide_audited(self, state, audit_id=None):
        previous = self.http.context
        self.http.context = {'purpose': 'Jev固定话术选择', 'audit_id': audit_id}
        try:
            return self.router.decide(state)
        finally:
            self.http.context = previous

    def get_desktop(self):
        if self.desktop is None:
            self.desktop = ClickniumDesktop(self.cfg['desktop'], self.stop,
                                            progress=lambda text: self.emit({'kind':'log','text':text}))
        return self.desktop

    def customer_key(self, customer):
        return self.cfg.get('active_profile', 'default') + ' :: ' + self.cfg['desktop']['shop_id'] + ' :: ' + customer

    def require_upload_consent(self):
        if not self.cfg['desktop']['data_upload_authorized']:
            raise DesktopError('未授权上传客户聊天截图/文字；请先阅读数据说明并在设置页确认。')

    @traced('service.text_test')
    def text_test(self, message: str):
        if not message.strip() or len(message) > 5000:
            raise ValueError('请输入1–5000字的测试问题。')
        state = {'pending_buyer_messages': [{'role': 'buyer', 'text': message, 'timestamp': ''}],
                 'history': [], 'verified_order_facts': {}}
        decision = self.decide_audited(state)
        self.store.record_action('text_test_decision',
                                 details={'input': message, 'decision': decision.as_dict()})
        self.emit({'kind': 'decision', 'source': '文字测试（不会发送）', 'id': None,
                   'decision': decision.as_dict(), 'input': message})
        return decision

    @traced('service.test_vision')
    def test_vision(self, path: Path):
        self.require_upload_consent()
        transcript, audit_id = self.read_audited(path.read_bytes(), 'local-image-test', '视觉识字测试')
        self.store.update_vision(audit_id, status='vision_test_passed')
        self.store.record_action('vision_test_passed', details={'audit_id': audit_id})
        self.emit({'kind': 'vision_test_result', 'audit_id': audit_id, 'input': transcript.state(), 'signature': vision_signature(self.cfg['vision'])})
        return transcript

    @traced('service.preview_image')
    def preview_image(self, path: Path):
        self.require_upload_consent()
        transcript, audit_id = self.read_audited(path.read_bytes(), 'local-image-test', '导入截图测试')
        try:
            decision = self.decide_audited(transcript.state(), audit_id)
        except Exception as e:
            self.store.update_vision(audit_id, status='routing_failed', error=str(e))
            raise
        self.store.update_vision(audit_id, decision=decision.as_dict(), status='previewed')
        self.store.record_action('image_preview', details={'audit_id': audit_id, 'decision': decision.as_dict()})
        self.emit({'kind': 'decision', 'source': '导入截图测试（不会发送）', 'id': None,
                   'decision': decision.as_dict(), 'input': transcript.state()})
        return decision

    @traced('service.read_audited')
    def read_audited(self, png: bytes, customer: str, source: str, *, snapshot=None):
        audit_id = self.store.start_vision(source, customer, png)
        self.store.record_action('vision_capture', customer=customer,
                                 details={'audit_id': audit_id, 'source': source, 'bytes': len(png)})
        if snapshot is not None and snapshot.position_evidence is not None:
            self.store.record_action('chat_position_verified', customer=customer,
                details={'audit_id': audit_id, 'verified': snapshot.latest_position_verified,
                         'evidence': snapshot.position_evidence})
        def save_response(obj):
            # The API key is sent in the header, but scrub any echoed secret defensively.
            raw = json.dumps(obj, ensure_ascii=False)
            for secret in (self.cfg['vision'].get('api_key'), self.cfg['jev'].get('api_key')):
                if secret:
                    raw = raw.replace(secret, '[REDACTED]')
            self.store.update_vision(audit_id, response=json.loads(raw), status='responded')
            self.store.record_action('vision_response', customer=customer, details={'audit_id': audit_id})
        previous = self.http.context
        self.http.context = {'purpose': source, 'audit_id': audit_id}
        try:
            if isinstance(self.reader, VisionReader):
                transcript = self.reader.read(png, customer, audit=save_response,
                    latest_position_verified=snapshot.latest_position_verified if snapshot is not None else None)
            else:
                transcript = self.reader.read(png, customer)
            self.store.update_vision(audit_id, transcript=transcript.state(), status='transcribed')
            self.store.record_action('vision_parsed', customer=customer, details={'audit_id': audit_id})
            return transcript, audit_id
        except Exception as e:
            self.store.update_vision(audit_id, status='vision_failed', error=str(e))
            self.store.record_action('vision_failed', customer=customer,
                                     details={'audit_id': audit_id, 'error_type': type(e).__name__})
            raise
        finally:
            self.http.context = previous

    @traced('service.preview_current')
    def preview_current(self, auto: bool = False, *, manual_preview: bool = False, refresh_preview: bool = False):
        self.require_upload_consent()
        if auto and self.unverified_preview_mode:
            raise DesktopError('未验收预览禁止自动发送。')
        if auto:
            d = self.cfg['desktop']
            if not can_auto_send(self.cfg, approved=sum(r.approved and not r.manual_only for r in self.knowledge.replies.values())):
                raise DesktopError('首次配置或测试发送未验收；自动发送尚未开放。')
        desktop = self.get_desktop()
        snapshot = desktop.read_current(allow_unknown_latest=True, prepare_latest=True) if manual_preview and not auto else desktop.read_current(prepare_latest=True)
        return self.process_snapshot(snapshot, auto=auto, manual_preview=manual_preview, refresh_preview=refresh_preview)

    def process_snapshot(self, snapshot, *, auto=False, manual_preview=False, source='当前会话预览', refresh_preview=False):
        self.sync_observation(snapshot)
        customer = self.customer_key(snapshot.customer)
        self.store.record_action('current_chat_read', customer=customer)
        if manual_preview:
            self.emit({'kind': 'log', 'text': '最新位置检查：' + ('程序已确认' if snapshot.latest_position_verified is True else '未确认，本次仅预览')})
        if manual_preview and not auto and self.store.db.execute(
                'SELECT 1 FROM preview_whitelist WHERE customer=?', (customer,)).fetchone():
            self.store.record_action('whitelisted_manual_preview', customer=customer,
                                     details={'send_allowed': False})
            self.emit({'kind': 'log', 'text': '测试白名单：重新识字和选话术，本次结果禁止发送。'})
            return self.preview_unverified(snapshot, customer, source='白名单手动预览', label='白名单预览，禁止发送')
        if manual_preview and snapshot.latest_position_verified is not True:
            self.emit({'kind': 'log', 'text': '最新位置未确认：继续识字和选话术，本次禁止发送。'})
            return self.preview_unverified(snapshot, customer, source='位置未确认的手动预览', label='最新位置未确认，禁止发送')
        if self.unverified_preview_mode:
            return self.preview_unverified(snapshot, customer)
        block = self.store.is_blocked(customer)
        if block:
            self.store.record_action('customer_blocked_skip', customer=customer, details={'reason': block})
            self.emit({'kind': 'blocked', 'customer': customer, 'text': f'{snapshot.customer} 已暂停：{block}'})
            return False
        frame_key = hashlib.sha256((customer + snapshot.digest).encode()).hexdigest()
        content = (snapshot.position_evidence or {}).get('after', {}).get('content')
        content_key = hashlib.sha256((customer + 'DOM:' + content).encode()).hexdigest() if isinstance(content, str) else None
        refresh_preview = bool(refresh_preview and manual_preview and not auto)
        seen_frame = self.store.frame_seen(frame_key) or bool(content_key and self.store.frame_seen(content_key))
        if not refresh_preview and seen_frame:
            self.store.record_action('duplicate_frame_skip', customer=customer)
            self.emit({'kind': 'preview_skipped', 'customer': customer,
                       'text': f'{snapshot.customer}：本次截图与已处理画面相同，已跳过。未重新调用识字模型或选话术，也未发送；识字历史没有新增记录。'})
            return False
        try:
            transcript, audit_id = self.read_audited(snapshot.png, customer, source, snapshot=snapshot)
        except Cancelled:
            raise
        except Exception as e:
            decision = Decision('human', reason=f'截图读取失败：{e}')
            id_ = self.store.add('frame:' + frame_key, customer, decision)
            self.store.block(customer, decision.reason)
            self.store.record_action('vision_human', customer=customer, event_id=id_,
                                     details={'reason': decision.reason})
            self.emit({'kind': 'decision', 'source': snapshot.customer, 'id': None, 'event_id': id_,
                       'customer': customer, 'decision': decision.as_dict(), 'input': '截图读取失败；人工核查。'})
            return True
        if not transcript.pending():
            self.store.update_vision(audit_id, status='no_pending_buyer')
            self.store.mark_frame(frame_key)
            if content_key:
                self.store.mark_frame(content_key)
            self.store.record_action('no_pending_buyer', customer=customer)
            self.emit({'kind': 'log', 'text': f'{snapshot.customer}：可见最后一条不是客户待答消息，跳过。'})
            return False
        fingerprint = transcript.fingerprint(self.cfg['desktop']['shop_id'], snapshot.customer)
        if isinstance(content, str):
            legacy = self.store.db.execute('SELECT status FROM events WHERE fingerprint=?', (fingerprint,)).fetchone()
            if legacy is None or legacy[0] == 'sent_ui_verified':
                fingerprint = hashlib.sha256((customer + fingerprint + content).encode()).hexdigest()
        prior = self.store.db.execute('SELECT id,status FROM events WHERE fingerprint=? AND customer=?',
                                     (fingerprint, customer)).fetchone()
        reusable = refresh_preview and prior is not None and prior['status'] == 'preview'
        if (self.store.seen(fingerprint) or refresh_preview and seen_frame) and not reusable:
            self.store.update_vision(audit_id, status='duplicate_message')
            self.store.record_action('duplicate_message_skip', customer=customer,
                                     details={'audit_id': audit_id})
            self.emit({'kind': 'preview_skipped', 'customer': customer,
                       'text': f'{snapshot.customer}：已完成本次识字，但这条客户消息已有处理记录，已跳过选话术及发送。可在识字历史查看本次结果。'})
            return False
        try:
            decision = self.decide_audited(transcript.state(), audit_id)
        except Exception as e:
            self.store.update_vision(audit_id, status='routing_failed', error=str(e))
            raise
        self.store.update_vision(audit_id, decision=decision.as_dict(), status='previewed')
        if reusable:
            id_ = prior['id']
            self.pending.pop(id_, None); self.pending_transcripts.pop(id_, None)
            self.store.db.execute('UPDATE events SET decision=?,status=? WHERE id=? AND status=\'preview\'',
                (json.dumps(decision.as_dict(), ensure_ascii=False), 'preview' if decision.action == 'candidate' else 'human', id_))
            self.store.db.commit()
            self.store.record_action('manual_preview_refreshed', customer=customer, event_id=id_, details={'audit_id': audit_id})
        else:
            id_ = self.store.add(fingerprint, customer, decision)
        self.store.mark_frame(frame_key)
        if content_key:
            self.store.mark_frame(content_key)
        if not id_:
            return False
        if decision.action == 'human':
            self.store.block(customer, decision.reason)
        elif not self.unverified_preview_mode:
            # Sendable candidates are memory-only; the separate audit retains evidence.
            self.pending[id_] = (snapshot, decision, customer)
            self.pending_transcripts[id_] = transcript
        self.store.record_action('decision', customer=customer, event_id=id_,
                                 details={'audit_id': audit_id, 'decision': decision.as_dict()})
        self.emit({'kind': 'decision', 'source': snapshot.customer + ('（未验收预览，禁止发送）' if self.unverified_preview_mode else ''),
                   'id': id_ if decision.action == 'candidate' and not self.unverified_preview_mode else None,
                   'event_id': id_, 'customer': customer, 'decision': decision.as_dict(), 'input': transcript.state()})
        if auto and decision.action == 'candidate':
            if decision.approved:
                self.send(id_, manual=False)
            else:
                self.store.block(customer, '话术未批准自动发送，等待人工。')
                self.emit({'kind': 'log', 'text': '该话术未批准自动发送，已暂停此客户。'})
        return True

    def preview_unverified(self, snapshot, customer, *, source='临时未验收监测预览', label='未验收预览，禁止发送'):
        """Audit only: never claim messages, block customers, or create sendable candidates."""
        audit_id = None
        try:
            transcript, audit_id = self.read_audited(snapshot.png, customer, source, snapshot=snapshot)
            if not transcript.pending():
                self.store.update_vision(audit_id, status='no_pending_buyer')
                self.emit({'kind': 'log', 'text': '未验收预览：当前最后一条不是待答买家消息。'})
                return True
            decision = self.decide_audited(transcript.state(), audit_id)
            self.store.update_vision(audit_id, decision=decision.as_dict(), status='preview_only')
            state = transcript.state()
        except Exception as error:
            if isinstance(error, Cancelled):
                raise
            if audit_id:
                self.store.update_vision(audit_id, status='routing_failed', error=str(error))
            decision = Decision('human', reason=f'未验收预览失败：{error}')
            state = '请查看识字历史中的截图与失败原因。'
        self.store.record_action('unverified_preview_result', customer=customer,
                                 details={'decision': decision.as_dict(), 'send_allowed': False})
        self.emit({'kind': 'decision', 'source': snapshot.customer + '（' + label + '）',
                   'id': None, 'customer': customer, 'decision': decision.as_dict(), 'input': state})
        return True

    def reset_observation(self):
        self.observed.clear()
        self.current_draft = False
        self.watched_name = None
        self.next_watch_turn = False

    def sync_observation(self, snapshot):
        content = (snapshot.position_evidence or {}).get('after', {}).get('content')
        if isinstance(content, str) and content:
            key = self.customer_key(snapshot.customer)
            self.observed[key] = {'name': snapshot.customer,
                'baseline': hashlib.sha256(content.encode()).hexdigest(),
                'candidate': None, 'stable': 0, 'deferred': None}

    def observe_active(self):
        observation = self.get_desktop().observe_current()
        if not isinstance(observation, dict):
            return None
        key = self.customer_key(observation['customer'])
        if not observation['latest'] and not observation['draft'] and not self.store.is_blocked(key):
            self.emit({'kind':'log','text':observation['customer'] + '：准备自动回到最新消息。'})
            self.get_desktop().return_to_latest(observation['customer'])
            refreshed = self.get_desktop().observe_current()
            if not isinstance(refreshed, dict) or refreshed['customer'] != observation['customer']:
                raise DesktopError('自动回到最新消息后客户不一致，已停止本次监测。')
            observation = refreshed
            self.store.record_action('watch_position_prepared', customer=key,
                                     details={'latest':observation['latest'],'draft':observation['draft']})
        self.current_draft = observation['draft']
        if observation['customer'] != self.watched_name:
            self.watched_name = observation['customer']
            self.emit({'kind': 'watch_status', 'customer': self.watched_name})
        key = self.customer_key(observation['customer'])
        digest = observation['content_key']
        if key not in self.observed:
            self.observed[key] = {'name': observation['customer'], 'baseline': digest,
                                 'candidate': None, 'stable': 0, 'deferred': None}
            self.store.record_action('watch_baseline', customer=key)
        state = self.observed[key]
        if digest == state['baseline']:
            state.update(candidate=None, stable=0)
        elif digest == state['candidate']:
            state['stable'] += 1
        else:
            state.update(candidate=digest, stable=1)
            self.store.record_action('watch_content_changed', customer=key)
        reason = ('客户已暂停' if self.store.is_blocked(key) else
                  '输入框有人工草稿' if observation['draft'] else
                  '最新位置未确认' if not observation['latest'] else None)
        record('poll.observation', details={'content_digest': digest, 'baseline_digest': state['baseline'],
               'stable_count': state['stable'], 'latest': observation['latest'], 'draft': observation['draft'],
               'deferred': reason}, customer=key)
        if reason != state['deferred']:
            state['deferred'] = reason
            self.store.record_action('watch_deferred' if reason else 'watch_ready',
                                     customer=key, details={'reason': reason})
            if reason:
                self.emit({'kind': 'log', 'text': observation['customer'] + '：当前会话暂缓，' + reason})
        return key

    def scan_current_change(self, active, *, auto=False):
        desktop = self.get_desktop()
        if self.current_draft:
            return False
        queued = [k for k,v in self.observed.items() if v['candidate'] and not v['deferred'] and not self.store.is_blocked(k)]
        if active in queued:
            queued.remove(active); queued.insert(0, active)
        if not queued:
            return False
        key = queued[0]
        state = self.observed[key]
        if self.store.is_blocked(key):
            return False
        if key != active:
            if not desktop.open_watched_customer(state['name']):
                self.store.block(key, '待处理追问会话无法唯一重新打开，请人工处理。')
                self.store.record_action('watch_revisit_failed', customer=key)
                self.emit({'kind': 'log', 'text': state['name'] + '：无法唯一重新打开追问会话，已交人工；继续处理其他客户。'})
                return False
            active = self.observe_active()
            if active != key:
                raise DesktopError('重新打开追问会话后客户身份不一致，已停止。')
            state = self.observed[key]
        if state['deferred'] or state['stable'] < 2:
            return False
        snapshot = desktop.read_current()
        if snapshot.customer != state['name']:
            raise DesktopError('追问截图的客户与观察目标不一致，已停止本次处理。')
        content = (snapshot.position_evidence or {}).get('after', {}).get('content')
        if not isinstance(content, str) or hashlib.sha256(content.encode()).hexdigest() != state['candidate']:
            self.observe_active()
            return False
        self.store.record_action('watch_change_stable', customer=key)
        self.emit({'kind': 'log', 'text': state['name'] + '：当前会话有稳定的新变化，开始识字。'})
        self.scan_consumed = True
        return self.process_snapshot(snapshot, auto=auto, source='当前会话变化监测')

    @traced('service.scan_once')
    def scan_once(self, auto: bool = False):
        self.require_upload_consent()
        if auto and self.unverified_preview_mode:
            raise DesktopError('未验收预览禁止自动发送。')
        if auto and self.cfg['desktop'].get('unread_mode') == 'red_badge':
            raise DesktopError('闲鱼红点模式仅开放监测预览；唯一会话身份和真实发送尚未验收。')
        if auto and not can_auto_send(self.cfg, approved=sum(r.approved and not r.manual_only for r in self.knowledge.replies.values())):
            raise DesktopError('自动发送尚未获准，不能启动自动监测。')
        self.scan_consumed = False
        active = self.observe_active() if not self.unverified_preview_mode else None
        if self.current_draft:
            return False
        watch_first = self.next_watch_turn
        self.next_watch_turn = not self.next_watch_turn
        if watch_first:
            processed = self.scan_current_change(active, auto=auto)
            if processed or self.scan_consumed:
                return processed
        desktop = self.get_desktop()
        opened = (desktop.open_next_unread(preview_unverified=True) if self.unverified_preview_mode
                  else desktop.open_next_unread())
        if opened:
            self.store.record_action('unread_opened')
            return self.preview_current(auto=auto)
        if not watch_first and not self.unverified_preview_mode:
            return self.scan_current_change(active, auto=auto)
        return False

    def record_precheck_failure(self, error, customer):
        for label in ('baseline', 'current'):
            snapshot = getattr(error, label, None)
            if snapshot is not None:
                record('send.precheck.evidence', 'failed', {'side': label, 'position': snapshot.position_evidence,
                       'error': exception_details(error)}, customer=customer, png=snapshot.png)
                audit_id = self.store.start_vision('发送前检查失败：' + label, customer, snapshot.png)
                self.store.update_vision(audit_id, status='precheck_failed', error=str(error))
                self.store.record_action('send_precheck_evidence', customer=customer,
                    details={'audit_id': audit_id, 'side': label,
                             'position': snapshot.position_evidence})

    @traced('service.send')
    def send(self, id_: str, manual: bool = False):
        self.http.check_stop()
        if self.unverified_preview_mode:
            raise DesktopError('未验收预览禁止发送；请完成正式验收后重新读取。')
        if id_ not in self.pending:
            raise DesktopError('候选记录已失效（重启/重新配置后不能继续发送旧候选）。')
        snapshot, decision, customer = self.pending[id_]
        if decision.action != 'candidate':
            raise DesktopError('人工处理项不能通过候选发送。')
        if not manual and (not decision.approved or not can_auto_send(self.cfg,
                approved=sum(r.approved and not r.manual_only for r in self.knowledge.replies.values()))):
            raise DesktopError('此话术未获自动发送授权。')
        if self.store.attempted_last_minute() >= self.cfg['desktop']['max_sends_per_minute']:
            self.store.block(customer, '达到每分钟发送上限，人工确认后恢复。')
            raise DesktopError('达到每分钟发送上限；不会延后自动补发。')
        desktop = self.get_desktop()
        # Fail before claiming a send when a stale preview is detected.
        try:
            desktop.ensure_unchanged(snapshot)
        except DesktopError as error:
            self.record_precheck_failure(error, customer)
            raise
        self.store.record_action('send_precheck_passed', customer=customer, event_id=id_,
                                 details={'manual': manual})
        if not self.store.begin_send(id_):
            raise DesktopError('该消息已处理或发送过；不允许重复发送。')
        self.store.record_attempt()
        self.store.record_action('send_attempt', customer=customer, event_id=id_)
        desktop.click_intent = False
        desktop.send_phase = 'claimed'
        try:
            desktop.send_reply(snapshot, decision.text)
            web = isinstance(desktop, ClickniumDesktop) and desktop.is_xianyu()
            if web:
                desktop.send_phase = 'web_verify'
                if desktop.web_send_baseline is None:
                    raise DesktopError('缺少发送前网页消息证据，不能确认发送结果。')
                current_messages, added = desktop.verify_web_reply(desktop.web_send_baseline, decision.text)
                content = current_messages.position.get('content')
                if not isinstance(content,str) or not content:
                    raise DesktopError('发送后网页摘要缺失，不能同步监测基线。')
                self.observed[customer] = {'name':current_messages.customer,
                    'baseline':hashlib.sha256(content.encode()).hexdigest(),
                    'candidate':None,'stable':0,'deferred':None}
                has_followup = any(m['role']=='buyer' for m in added)
            else:
                # Keep the previously verified flow for other platform adapters.
                desktop.send_phase = 'settle'
                if self.stop.wait(self.cfg['desktop']['settle_seconds']):
                    raise Cancelled('发送后已停止；结果需人工核查。')
                desktop.send_phase = 'post_capture'
                record('send.post_capture.started', customer=customer)
                current = desktop.read_current()
                if current.customer != snapshot.customer:
                    raise DesktopError('发送后会话切换，不能确认发送结果。')
                check, _verification_audit = self.read_audited(current.png, customer, '发送后界面核对', snapshot=current)
                desktop.send_phase = 'bubble_check'
                record('send.bubble_check.started', customer=customer)
                previous = self.pending_transcripts.get(id_)
                verified = (check.verifies_new_reply(previous, decision.text) if previous is not None else False)
                if not verified:
                    raise DesktopError('无法唯一确认新增卖家回复气泡，发送结果需人工核查。')
                record('send.bubble_check.verified', details={'verification_audit': _verification_audit}, customer=customer)
                self.sync_observation(current)
                has_followup = bool(check.pending())
            if has_followup:
                # Preserve the new buyer tail even if switching to another unread next.
                state = self.observed.get(customer)
                if state:
                    state.update(baseline='pending-followup', candidate=state['baseline'], stable=0)
                self.store.record_action('followup_after_send', customer=customer)
            self.store.set_status(id_, 'sent_ui_verified')
            self.store.record_action('send_ui_verified', customer=customer, event_id=id_)
            self.emit({'kind': 'log', 'text': '已在界面确认卖家回复气泡（不是服务器送达回执）。'})
        except Exception as e:
            self.record_precheck_failure(e, customer)
            known_before_click = isinstance(desktop, ClickniumDesktop) and not desktop.click_intent
            status = 'send_aborted_before_click' if known_before_click else 'send_unknown'
            self.store.set_status(id_, status)
            self.store.block(customer, '发送点击前已中止；请检查未发送草稿后恢复，不自动重试。' if known_before_click else '发送结果未知；检查客户端后再恢复，不会自动重发。')
            record('send.failure', status, {**exception_details(e), 'last_stage': desktop.send_phase, 'click_intent_committed': desktop.click_intent}, customer=customer)
            self.store.record_action(status, customer=customer, event_id=id_,
                                     details=exception_details(e))
            self.emit({'kind': 'log', 'text': f'{"点击前已中止" if known_before_click else "发送结果未知"}：{e}。此客户已暂停，不自动重试。'})
            self.emit({'kind': 'send_failed' if known_before_click else 'send_unknown', 'id': id_, 'customer': customer,
                       'text': '点击前已中止；请检查未发送草稿。' if known_before_click else '发送结果未知。'})
            if isinstance(e, Cancelled):
                raise
        finally:
            self.pending.pop(id_, None)
            self.pending_transcripts.pop(id_, None)
        if self.store.status(id_) == 'sent_ui_verified':
            self.emit({'kind': 'sent', 'id': id_})

    @traced('service.reject')
    def reject(self, id_: str):
        self.pending_transcripts.pop(id_, None)
        item = self.pending.pop(id_, None)
        if item:
            self.store.set_status(id_, 'rejected')
            self.store.block(item[2], '用户拒绝候选，交人工处理。')
            self.store.record_action('candidate_rejected', customer=item[2], event_id=id_)
            self.emit({'kind': 'log', 'text': '已拒绝候选，没有发送；该客户暂停自动处理。'})

    @traced('service.resume')
    def resume(self, customer: str):
        self.store.resume(customer)
        self.store.record_action('customer_resumed', customer=customer)
        self.emit({'kind': 'log', 'text': f'已恢复 {customer} 的后续新消息。旧消息不会重发。'})

    def close(self):
        self.store.close()
        self.http.session.close()
