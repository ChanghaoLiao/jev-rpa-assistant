"""Persistent step-five evidence; all methods run on the existing serial Worker."""
from __future__ import annotations
import copy
import hashlib
import io
import json
import time
import uuid
from pathlib import Path
from PIL import Image
from .config import save_config
from .paths import knowledge_file
from .vision import vision_signature
from .audit import clean


def route_signature(cfg):
    material = {'jev': cfg['jev'], 'knowledge': cfg['knowledge']}
    for key in ('replies_csv', 'categories_json'):
        path = knowledge_file(cfg['knowledge'][key])
        material[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


class AcceptanceFlow:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS acceptance_flows (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, updated REAL NOT NULL, data_json TEXT NOT NULL)''')
        self.store.db.commit()

    @property
    def scope(self):
        return self.service.customer_key('')

    def load(self, id_=None):
        row = self.store.db.execute(
            'SELECT * FROM acceptance_flows WHERE scope=?' + (' AND id=?' if id_ else '') +
            ' ORDER BY updated DESC LIMIT 1', (self.scope, id_) if id_ else (self.scope,)).fetchone()
        if id_ and row is None:
            raise ValueError('验收流程已变化，请重新打开本步骤。')
        cfg = self.service.cfg
        data = json.loads(row['data_json']) if row else dict(id=uuid.uuid4().hex, step=0)
        vision = vision_signature(cfg['vision']); route = route_signature(cfg)
        if data.get('vision_signature') != vision:
            for key in ('image_id', 'vision_id', 'route_id', 'vision_reviewed', 'route_reviewed', 'live', 'error'):
                data.pop(key, None)
            data['step'] = 0
        elif data.get('route_signature') != route:
            for key in ('route_id', 'route_reviewed', 'live', 'error'):
                data.pop(key, None)
            data['step'] = min(data.get('step', 0), 1)
        data.update(vision_signature=vision, route_signature=route)
        return data

    def save(self, data, action):
        self.store.record_action('acceptance.' + action, details={'flow_id': data['id'],
            'step': data.get('step', 0), 'vision_id': data.get('vision_id'), 'route_id': data.get('route_id'),
            'send_event': (data.get('live') or {}).get('event_id'), 'error': data.get('error')})
        self.store.db.execute('INSERT INTO acceptance_flows VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
            'updated=excluded.updated,data_json=excluded.data_json',
            (data['id'], self.scope, time.time(), json.dumps(clean(data), ensure_ascii=False)))
        self.store.db.commit()

    def metadata(self, values):
        """Only verification/consent metadata changes; no new Service or candidate reset."""
        allowed = {'onboarding': {'vision_verified', 'vision_signature'},
                   'desktop': {'data_upload_authorized', 'test_send_verified'}}
        cfg = copy.deepcopy(self.service.cfg)
        for group, fields in values.items():
            if group not in allowed or not set(fields).issubset(allowed[group]):
                raise ValueError('此操作只能保存验收结果。')
            cfg[group].update(fields)
        self.store.record_action('acceptance.metadata_save_intent', details=values)
        save_config(cfg)
        self.service.cfg = cfg
        self.store.record_action('acceptance.metadata_saved', details=values)

    def present(self, data, *, metadata=None):
        result = copy.deepcopy(data)
        result['scope'] = self.scope
        for key in ('image', 'vision', 'route'):
            id_ = data.get(key + '_id')
            result[key] = self.store.vision_record(id_) if id_ else None
        live = result.get('live')
        if live and live.get('id') not in self.service.pending:
            live['id'] = None
            if live.get('status') == 'candidate':
                live.update(status='expired', reason='候选已失效，请重新读取真实测试会话。')
        return {'kind': 'acceptance_result', 'flow_id': data['id'], 'data': result,
                'metadata': metadata or {}}

    def execute(self, request):
        action = request.get('action', 'load')
        data = self.load(request.get('flow_id'))
        if request.get('vision_signature') not in (None, data['vision_signature']):
            raise ValueError('视觉服务已变化，请重新测试。')
        metadata = {}
        data.pop('error', None)
        try:
            if action == 'pick':
                path = Path(request['path']); png = path.read_bytes()
                with Image.open(io.BytesIO(png)) as im:
                    if im.format != 'PNG': raise ValueError('请选择 PNG 聊天截图。')
                    im.verify()
                data.update(image_id=self.store.start_vision('验收：选图（尚未上传）', 'local-image-test', png),
                    filename=path.name, step=0, vision_reviewed=False, route_reviewed=False)
                for key in ('vision_id', 'route_id', 'live'): data.pop(key, None)
            elif action == 'authorize':
                metadata = {'desktop': {'data_upload_authorized': True}}
                self.metadata(metadata)
            elif action == 'ocr':
                image = self.store.vision_record(data.get('image_id'))
                if not image: raise ValueError('请先上传聊天截图。')
                self.service.require_upload_consent()
                data.update(vision_reviewed=False, route_reviewed=False)
                for key in ('vision_id', 'route_id', 'live'): data.pop(key, None)
                try:
                    transcript, audit_id = self.service.read_audited(image['image_png'], 'local-image-test', '验收：视觉识字')
                except Exception:
                    failed = self.store.latest_vision()
                    if failed and failed['source'] == '验收：视觉识字': data['vision_id'] = failed['id']
                    raise
                self.store.update_vision(audit_id, status='vision_test_passed')
                data['vision_id'] = audit_id
            elif action == 'vision_confirm':
                row = self.store.vision_record(data.get('vision_id'))
                if not row or row['status'] != 'vision_test_passed': raise ValueError('请先成功识字并核对结果。')
                metadata = {'onboarding': {'vision_verified': True, 'vision_signature': data['vision_signature']}}
                self.metadata(metadata); data.update(vision_reviewed=True, step=1)
            elif action == 'route':
                if not data.get('vision_reviewed'): raise ValueError('请先核对识字结果。')
                self.service.require_upload_consent()
                row = self.store.vision_record(data['vision_id'])
                audit_id = self.store.start_vision('验收：复用识字测试话术（不发送）', 'local-image-test', row['image_png'])
                state = json.loads(row['transcript_json'])
                self.store.update_vision(audit_id, transcript=state, status='routing')
                data.update(route_id=audit_id, route_reviewed=False)
                try:
                    decision = self.service.decide_audited(state, audit_id)
                except Exception as error:
                    self.store.update_vision(audit_id, status='routing_failed', error=str(error))
                    raise
                self.store.update_vision(audit_id, decision=decision.as_dict(), status='previewed')
            elif action == 'route_confirm':
                row = self.store.vision_record(data.get('route_id'))
                if not data.get('vision_reviewed') or not row or not row['decision_json']:
                    raise ValueError('请先测试选择话术并核对结果。')
                data.update(route_reviewed=True, step=2)
            elif action == 'read':
                if not data.get('route_reviewed') and not self.service.cfg['desktop']['test_send_verified']:
                    raise ValueError('请先完成图片识字和话术核对。')
                old_id = (data.get('live') or {}).get('id')
                self.service.pending.pop(old_id, None); self.service.pending_transcripts.pop(old_id, None)
                data['live'] = {'id': None, 'status': 'expired', 'reason': '读取尚未成功，请查看检查原因。'}
                captured = []; emit = self.service.emit
                self.service.emit = lambda item: (captured.append(item), emit(item))[-1]
                try:
                    self.service.unverified_preview_mode = False
                    self.service.preview_current(manual_preview=True, refresh_preview=True)
                finally:
                    self.service.emit = emit
                event = next((e for e in reversed(captured) if e['kind'] in ('decision', 'blocked', 'preview_skipped')), None)
                data['live'] = {'id': None, 'status': 'no_candidate', 'reason': '当前没有待答买家消息。'}
                if event:
                    d = event.get('decision') or {}
                    data['live'] = {'id': event.get('id'), 'event_id': event.get('event_id'),
                        'customer': event.get('customer'), 'input': event.get('input'), 'decision': d,
                        'status': 'candidate' if event.get('id') else 'blocked',
                        'reason': event.get('text') or d.get('reason') or event.get('source') or '此预览不能发送。'}
            elif action == 'send':
                live = data.get('live') or {}; id_ = request.get('candidate_id')
                if not id_ or id_ != live.get('id') or id_ not in self.service.pending:
                    raise ValueError('候选已失效，请重新读取真实测试会话。')
                try:
                    self.service.send(id_, manual=True)
                finally:
                    status = self.store.status(id_)
                    live.update(id=None, event_id=id_, status=status or 'expired')
                    live['reason'] = self.store.is_blocked(live.get('customer')) or '请重新读取真实测试会话。'
                    self.save(data, 'send_outcome')
                if status == 'sent_ui_verified':
                    metadata = {'desktop': {'test_send_verified': True}}
                    self.metadata(metadata)
            elif action == 'page':
                step = max(0, min(2, int(request['step'])))
                if step > 0 and not data.get('vision_reviewed'): raise ValueError('请先核对识字结果。')
                if step > 1 and not data.get('route_reviewed'): raise ValueError('请先核对话术结果。')
                data['step'] = step
            elif action != 'load':
                raise ValueError('未知验收操作。')
            if action == 'load' and not self.service.cfg['desktop']['test_send_verified']:
                live = data.get('live') or {}
                if live.get('status') == 'sent_ui_verified' and self.store.status(live.get('event_id')) == 'sent_ui_verified':
                    metadata = {'desktop': {'test_send_verified': True}}
                    self.metadata(metadata)
            self.save(data, action)
            return self.present(data, metadata=metadata)
        except Exception as error:
            data['error'] = clean(str(error))
            self.save(data, action + '_failed')
            raise
