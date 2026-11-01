from __future__ import annotations
import queue
import threading
import time
from pathlib import Path
from .service import Service
from .desktop import ClickniumDesktop
from .network import Cancelled
from .demo import run_demo
from .onboarding import can_auto_send
from .costs import default_rates
from .audit import record, operation, clean, exception_details

class Worker(threading.Thread):
    def __init__(self, output):
        super().__init__(daemon=True)
        self.commands = queue.Queue()
        self.output = output
        self.stop_event = threading.Event()
        self.exit_event = threading.Event()
        self.service = None
        self.monitor = False
        self.auto = False
        self.last_poll = 0
        self.generation = 0
        self.acceptance_context = None

    def emit(self, value):
        cfg = self.service.cfg if self.service else {}
        secrets = [cfg.get('jev', {}).get('api_key', ''), cfg.get('vision', {}).get('api_key', ''),
                   cfg.get('desktop', {}).get('license_key', '')]
        def redact(item):
            if isinstance(item, str):
                for secret in secrets:
                    if secret:
                        item = item.replace(secret, '[REDACTED]')
                return item
            if isinstance(item, dict):
                return {k: redact(v) for k, v in item.items()}
            if isinstance(item, list):
                return [redact(v) for v in item]
            return item
        value = redact(value)
        value.setdefault('profile', cfg.get('active_profile'))
        value.setdefault('generation', self.generation)
        if self.acceptance_context is not None:
            value.setdefault('flow_id', self.acceptance_context)
        if self.service and value.get('kind') in ('log', 'status'):
            try:
                self.service.store.append_log(value['kind'], value.get('text', ''))
            except Exception as error:
                self.monitor = False
                self.stop_event.set()
                self.output.put({'kind':'operation_failed', 'text': clean('日志写入失败，已停止任务，不继续发送：' + str(error))})
        self.output.put(value)

    def configure(self, cfg):
        if self.service:
            self.service.close()
        self.service = None
        self.service = Service(cfg, self.stop_event, self.emit)
        self.monitor = False

    def run(self):
        while not self.exit_event.is_set():
            try:
                queued = self.commands.get(timeout=.2)
                command, value = queued[:2]
                parent_op = queued[2] if len(queued) > 2 else None
                if command != 'configure' and len(queued) > 3:
                    self.generation = queued[3]
            except queue.Empty:
                command, value = 'tick', None
                parent_op = None
            auditing = command != 'tick' or (self.monitor and self.service and time.monotonic() - self.last_poll >= self.service.cfg['desktop']['poll_seconds'])
            scope = operation('poll' if command == 'tick' else 'worker.' + command, parent=parent_op) if auditing else None
            audit_error = None
            scope_entered = False
            try:
                if scope:
                    scope.__enter__()
                    scope_entered = True
                if self.service and command not in ('tick', 'configure'):
                    self.service.store.record_action('command_started',
                        details={'command': command})
                if self.stop_event.is_set():
                    self.monitor = False
                if command == 'stop':
                    self.monitor = False
                    if self.service:
                        self.service.reset_observation()
                        if not (value or {}).get('preserve_candidate', False):
                            self.service.pending.clear()
                            self.service.pending_transcripts.clear()
                    self.emit({'kind': 'status', 'text': '已停止'})
                    self.emit({'kind': 'log', 'text': '已停止。正在进行的网络请求会在返回或超时后结束；不会继续自动发送。'})
                elif command == 'configure':
                    self.generation = queued[3] if len(queued) > 3 else self.generation + 1
                    self.stop_event.clear()
                    self.configure(value)
                    self.emit({'kind': 'status', 'text': '已就绪 · 预览模式'})
                    self.emit({'kind': 'log', 'text': '配置已加载。模式仍为预览，不会自动开启发送。'})
                elif command == 'demo':
                    d = run_demo()
                    self.emit({'kind': 'decision', 'source': '离线演示（模拟结果，不是Jev）', 'id': None,
                               'decision': d.as_dict(), 'input': '怎么查看预计发货时间？'})
                elif self.service is None:
                    if command != 'tick':
                        self.emit({'kind': 'log', 'text': '请先保存并加载有效配置。'})
                elif command == 'acceptance':
                    from .acceptance import AcceptanceFlow
                    self.monitor = False
                    self.stop_event.clear()
                    flow = AcceptanceFlow(self.service)
                    self.acceptance_context = value.get('flow_id')
                    if self.acceptance_context is None:
                        initial = flow.load()
                        flow.save(initial, 'opened')
                        self.acceptance_context = initial['id']
                    request = dict(value, flow_id=self.acceptance_context)
                    try:
                        self.emit(flow.execute(request))
                    except Exception:
                        self.emit(flow.present(flow.load(self.acceptance_context)))
                        raise
                elif command == 'ui_query':
                    from .history import query_history, blocked_details
                    request = value or {}
                    target = request.get('target', 'events')
                    if target == 'blocked':
                        result = {'rows': blocked_details(self.service.store)}
                    elif target == 'trace':
                        from .audit import get_audit
                        filters = dict(request.get('filters') or {})
                        filters['platform'] = self.service.customer_key('').rsplit(' :: ', 1)[0]
                        rows, more = get_audit().page_rows(filters, request.get('page', 0))
                        result = {'rows': rows, 'has_more': more, 'page': request.get('page', 0)}
                    else:
                        result = query_history(self.service.store, target, request.get('filters'), request.get('page', 0))
                    self.emit({'kind': 'ui_query', 'target': target, 'request': request, **result})
                elif command in ('library_list', 'library_import', 'library_preview'):
                    from .library import ReplyLibrary
                    from .paths import knowledge_file
                    library = ReplyLibrary()
                    try:
                        if command == 'library_import':
                            library.snapshot_current(self.service.cfg)
                            version = library.import_file(Path(value), knowledge_file(self.service.cfg['knowledge']['categories_json']))
                            version, rows = library.preview(version['id'])
                            self.emit({'kind': 'library_preview', 'version': version, 'rows': rows})
                        elif command == 'library_preview':
                            version, rows = library.preview(value)
                            self.emit({'kind': 'library_preview', 'version': version, 'rows': rows})
                        else:
                            current = library.snapshot_current(self.service.cfg)
                            self.emit({'kind': 'library_versions', 'rows': [dict(r, active=r['id'] == current['id']) for r in library.list()]})
                    finally:
                        library.close()
                elif command == 'manual_done':
                    self.service.store.record_action('manual_marked_done', customer=value, details={'does_not_resume': True})
                    self.emit({'kind': 'log', 'text': '已记录人工处理完成；该客户仍暂停，恢复后续接待需单独确认。'})
                elif command in ('text', 'jev_check', 'image', 'vision_test', 'current', 'doctor', 'monitor', 'monitor_unverified', 'send', 'reject', 'resume', 'ledger', 'blocked_list', 'vision_audit', 'operation_log', 'costs', 'rates_save'):
                    self.stop_event.clear()
                    if command == 'text':
                        self.service.text_test(value)
                    elif command == 'jev_check':
                        decision = self.service.text_test('怎么查看预计发货时间？')
                        if not decision.trace:
                            raise ValueError('Jev 没有返回有效选择，请核对 Key 和网络。')
                        self.emit({'kind': 'jev_verified'})
                    elif command == 'vision_test':
                        self.service.test_vision(Path(value))
                    elif command == 'image':
                        self.service.preview_image(Path(value))
                    elif command == 'current':
                        self.monitor = False
                        self.service.unverified_preview_mode = False
                        self.service.preview_current(manual_preview=True)
                    elif command == 'doctor':
                        from .unread_adapter import prepare_recording
                        from .paths import DATA_ROOT
                        self.monitor = False
                        self.emit({'kind': 'log', 'text': '录制库已接入，正在备份、整理并核对未读规则…'})
                        prepared = prepare_recording(self.service.cfg, DATA_ROOT, self.stop_event)
                        if prepared.config != self.service.cfg:
                            self.service.cfg = prepared.config
                            self.service.desktop = None
                            self.service.pending.clear()
                            self.service.pending_transcripts.clear()
                            self.service.reset_observation()
                        self.emit({'kind':'recording_prepared', 'status':prepared.status,
                                   'text':prepared.reason, 'changed':prepared.changed,
                                   'desktop':{key:prepared.config['desktop'][key] for key in
                                       ('adapter','unread_rule_hash','unread_locator_reviewed',
                                        'test_send_verified','auto_send_enabled')}})
                        diagnostic = ClickniumDesktop(self.service.cfg['desktop'], self.stop_event, allow_unreviewed=True,
                            progress=lambda text: self.emit({'kind':'log','text':text}))
                        result = diagnostic.doctor()
                        result['录制库'] = '已接入'
                        result['规则整理'] = prepared.reason
                        result['页面匹配'] = '通过' if prepared.status == 'prepared' else '待人工验证'
                        result['未读变化验收'] = ('已验收' if self.service.cfg['desktop']['unread_locator_reviewed']
                                                  else '待完成三步检查；页面匹配不代替变化验收')
                        self.service.store.record_action('locator_check', details={'stage': value, 'result': result})
                        self.emit({'kind': 'doctor', 'stage': value, 'result': result})
                    elif command in ('monitor', 'monitor_unverified'):
                        self.auto = bool(value) if command == 'monitor' else False
                        self.service.unverified_preview_mode = command == 'monitor_unverified'
                        if self.service.unverified_preview_mode:
                            self.service.pending.clear()
                            self.service.store.record_action('unverified_preview_started',
                                details={'send_allowed': False, 'detector': 'red_badge'})
                        if self.auto:
                            count = sum(r.approved and not r.manual_only for r in self.service.knowledge.replies.values())
                            if not can_auto_send(self.service.cfg, approved=count):
                                raise ValueError('首次配置尚未完成；请返回“开始配置”查看缺少的步骤。')
                        self.service.require_upload_consent()
                        self.service.get_desktop()
                        self.service.reset_observation()
                        self.monitor = True
                        self.emit({'kind': 'status', 'text': '自动接待中' if self.auto else '监测预览中'})
                        self.emit({'kind': 'log', 'text': '开始自动监测：左侧未读和当前会话变化；仅发送已批准话术。' if self.auto else '开始监测预览：左侧未读和当前会话变化；发现候选/人工项后暂停。'})
                        if self.service.unverified_preview_mode:
                            self.emit({'kind': 'log', 'text': '临时未验收预览：按闲鱼列表红点检测；候选禁止发送，未改变验收状态。'})
                    elif command == 'send':
                        self.monitor = False
                        self.service.send(value, manual=True)
                    elif command == 'reject':
                        self.service.reject(value)
                    elif command == 'resume':
                        self.service.resume(value)
                    elif command == 'ledger':
                        self.emit({'kind': 'ledger', 'rows': self.service.store.recent()})
                    elif command == 'vision_audit':
                        if value:
                            self.emit({'kind': 'vision_audit', 'row': self.service.store.vision_record(value)})
                        else:
                            self.emit({'kind': 'vision_history', 'rows': self.service.store.vision_history()})
                    elif command == 'operation_log':
                        page = max(0, int(value or 0))
                        rows = self.service.store.recent_actions(101, page * 100)
                        self.emit({'kind': 'operation_log', 'rows': rows[:100],
                                   'page': page, 'has_more': len(rows) > 100})
                    elif command in ('costs', 'rates_save'):
                        if command == 'rates_save':
                            self.service.store.set_rates(value['provider'], value['rates'])
                        page = max(0, int(value or 0)) if command == 'costs' else 0
                        report = self.service.store.cost_report(page)
                        report['rates'] = {}
                        for provider, section in [('minimax', 'vision'), ('jev', 'jev')]:
                            c = self.service.cfg[section]
                            report['rates'][provider] = self.service.store.get_rates(provider) or default_rates(
                                provider, c.get('endpoint', ''), c.get('model', ''),
                                c.get('api_key', '').startswith('sk-cp'))
                        self.emit({'kind': 'cost_report', **report})
                    elif command == 'blocked_list':
                        self.emit({'kind': 'blocked_list', 'rows': self.service.store.blocked_customers()})
                if self.monitor and not self.stop_event.is_set():
                    interval = self.service.cfg['desktop']['poll_seconds']
                    if time.monotonic() - self.last_poll >= interval:
                        processed = self.service.scan_once(auto=self.auto)
                        self.last_poll = time.monotonic()
                        if processed and not self.auto:
                            self.monitor = False
                            self.emit({'kind': 'log', 'text': '预览监测已暂停，请核对截图、客户和候选话术。'})
                if self.service and command != 'tick':
                    self.service.store.record_action('command_finished',
                        details={'command': command})
                    if command in ('vision_test', 'image', 'text', 'current', 'doctor'):
                        self.emit({'kind': 'log', 'text': '本次操作已完成，请查看检查结果。'})
            except Cancelled as e:
                audit_error = e
                self.monitor = False
                if self.service:
                    self.service.reset_observation()
                if self.service:
                    try:
                        self.service.store.record_action('command_cancelled',
                            details={'command': command, **exception_details(e)})
                    except Exception as logging_error:
                        self.output.put({'kind':'operation_failed','text':clean('日志写入失败：' + str(logging_error))})
                        self.stop_event.set()
                if command == 'send':
                    self.emit({'kind': 'send_failed', 'text': str(e)})
                self.emit({'kind': 'status', 'text': '已停止'})
                self.emit({'kind': 'log', 'text': str(e)})
            except Exception as e:
                audit_error = e
                self.monitor = False
                if self.service:
                    self.service.reset_observation()
                if self.service:
                    try:
                        self.service.store.record_action('command_failed',
                            details={'command': command, **exception_details(e)})
                    except Exception as logging_error:
                        self.output.put({'kind':'operation_failed','text':clean('日志写入失败：' + str(logging_error))})
                        self.stop_event.set()
                if command == 'send':
                    self.emit({'kind': 'send_failed', 'text': str(e)})
                self.emit({'kind': 'operation_failed', 'text': str(e)})
                self.emit({'kind': 'status', 'text': '需处理 · 监测已暂停'})
                self.emit({'kind': 'log', 'text': f'已停止当前任务：{type(e).__name__}: {e}'})
            finally:
                self.acceptance_context = None
                if scope_entered:
                    try:
                        scope.__exit__(type(audit_error) if audit_error else None, audit_error, audit_error.__traceback__ if audit_error else None)
                    except Exception as logging_error:
                        self.monitor = False
                        self.stop_event.set()
                        self.output.put({'kind':'operation_failed','text':clean('日志写入失败，已停止任务：' + str(logging_error))})
                if command != 'tick':
                    self.output.put({'kind': 'command_done', 'command': command,
                                     'ok': audit_error is None, 'generation': self.generation,
                                     'profile': self.service.cfg.get('active_profile') if self.service else None})
        if self.service:
            self.service.close()
        record('worker.closed')

