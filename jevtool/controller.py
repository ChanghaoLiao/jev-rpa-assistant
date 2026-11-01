"""Qt presentation controller; chat actions stay in the serial Worker."""
from __future__ import annotations
import copy
import functools
import os
import queue
import shutil
import subprocess
import sys
import time
import webbrowser
import json
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QInputDialog
from .worker import Worker
from .config import load_config, save_config, MINIMAX_MODEL, MINIMAX_ENDPOINTS, validate_config
from .paths import DATA_ROOT, knowledge_file
from .profiles import add_profile, select_profile, sync_active
from .recording import check_recording, bind_recording, backup_recording_config, RecordingError
from .onboarding import approved_replies, checks, can_auto_send
from .vision import vision_signature
from .library import ReplyLibrary
from .ui_state import UIState
from .audit import record, operation, operation_id, platform_id, register_secrets, get_audit, clean, exception_details

VERSION = '0.3.5'
CHECK_NAMES = dict(jev='Jev 接口测试', vision='视觉识字核对', platform='平台账号', locators='聊天位置验证',
    unread='未读验证', conversation_identity='未读行客户身份', knowledge='话术审核', test_send='测试发送', consent='截图上传授权')
BACKGROUND = {'ui_query', 'vision_audit', 'library_list', 'costs', 'ledger', 'blocked_list', 'operation_log'}


class FileDialogs:
    def askopenfilename(self, **kwargs):
        types = kwargs.get('filetypes', [('文件', '*')])
        value = QFileDialog.getOpenFileName(None, kwargs.get('title', '选择文件'), '',
            ';;'.join(f'{name} ({pattern})' for name, pattern in types))[0]
        record('dialog.open_file', 'success' if value else 'cancelled', {'path': value})
        return value

    def asksaveasfilename(self, **kwargs):
        value = QFileDialog.getSaveFileName(None, kwargs.get('title', '保存文件'),
            kwargs.get('initialfile', ''), kwargs.get('filter', '所有文件 (*)'))[0]
        record('dialog.save_file', 'success' if value else 'cancelled', {'path': value})
        return value


filedialog = FileDialogs()


class App:
    def __init__(self, root=None, *, cfg=None, worker=None, autostart=True):
        self.cfg = copy.deepcopy(cfg) if cfg is not None else load_config()
        register_secrets(self.cfg)
        self.ui_state = UIState()
        self.output = queue.Queue()
        self.worker = worker or Worker(self.output)
        self.current_id = self.current_customer = None
        self.send_block_reason = '请先读取当前测试会话，并核对候选回复。'
        self.can_confirm_vision = self.expect_test_send = self.doctor_passed = False
        self.vision_test_signature = None
        self.unread_samples = {}
        self.recording_check_pending = None
        self.connection_checked = False
        self.busy = None
        self.generation = 1
        self.closing = self.closed = False
        self.last_result = None
        self.cost_rates = {}
        self.live_logs = []
        self.dialogs = []
        self.acceptance = {}
        self.acceptance_action_name = None
        from .views import Window
        self.window = Window(self)
        self.root = self.window
        self.entries = self.window.entries
        for name in ('api_jev_var', 'api_vision_var', 'api_region_var', 'consent_var'):
            setattr(self, name, getattr(self.window, name))
        self.log = self.window.feedback
        self.refresh_setup()
        self.timer = QTimer(self.window)
        self.timer.timeout.connect(self.drain)
        self.timer.start(100)
        if autostart:
            self.worker.start()
            self.busy = 'configure'
            self.worker.commands.put(('configure', copy.deepcopy(self.cfg), operation_id.get(), self.generation))
        else:
            self.window.finish_startup()

    def notify(self, text, state='info'):
        self.window.set_feedback(str(clean(text)), state)

    def write(self, widget, text, replace=False):
        self.notify(text)

    def call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as error:
            record('ui.error', 'failed', exception_details(error))
            self.notify(str(error), 'error')

    def confirm(self, title, text):
        answer = QMessageBox.question(self.window, title, text, QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No) == QMessageBox.Yes
        record('dialog.confirm', 'success' if answer else 'cancelled', {'title': title, 'answer': answer})
        return answer

    def readiness(self):
        try:
            return checks(self.cfg, approved=approved_replies(self.cfg))
        except Exception:
            return {key: False for key in CHECK_NAMES}

    def missing(self):
        return [CHECK_NAMES[k] for k, passed in self.readiness().items() if not passed]

    def first_missing_step(self):
        r = self.readiness()
        if not (self.cfg['jev']['api_key'] and self.cfg['vision']['api_key'] and r['jev']): return 0
        if not r['platform']: return 1
        if not r['locators'] or not r['unread']: return 2
        if not r['knowledge']: return 3
        if not r['vision'] or not r['test_send'] or not r['consent'] or not r['conversation_identity']: return 4
        return 5

    def persist_config(self, config):
        validated = copy.deepcopy(config)
        sync_active(validated)
        validate_config(validated)
        register_secrets(config)
        record('config.save.intent', details={'config': config})
        self.stop()
        save_config(config)
        self.cfg = config
        self.current_id = None
        self.expect_test_send = False
        self.send_block_reason = '配置已变化，请重新读取当前会话。'
        self.connection_checked = False
        self.busy = 'configure'
        self.generation += 1
        self.worker.commands.put(('configure', copy.deepcopy(config), operation_id.get(), self.generation))
        self.window.update_fields(config)
        record('config.saved')
        self.refresh_setup()

    def setup_save_api(self):
        config = copy.deepcopy(self.cfg)
        jev_key = self.api_jev_var.get().strip()
        vision_key = self.api_vision_var.get().strip()
        endpoint = MINIMAX_ENDPOINTS[self.api_region_var.get()]
        if jev_key != config['jev']['api_key']:
            config['onboarding']['jev_verified'] = False
        if (vision_key != config['vision']['api_key'] or config['vision']['provider'] != 'minimax' or
                endpoint != config['vision']['endpoint'] or config['vision']['model'] != MINIMAX_MODEL):
            config['onboarding']['vision_verified'] = False
            self.can_confirm_vision = False
        config['jev']['api_key'] = jev_key
        config['vision'].update(provider='minimax', endpoint=endpoint, model=MINIMAX_MODEL, api_key=vision_key)
        config['desktop']['data_upload_authorized'] = self.consent_var.get()
        self.persist_config(config)
        self.write(self.log, '设置已保存；未调用 API，未开启监测或发送。')

    def setup_save_only(self):
        self.setup_save_api()

    def setup_test_jev(self):
        self.setup_save_api()
        self.command('jev_check', queued=True)

    def setup_test_image(self):
        self.choose_image(vision_only=True)

    def acceptance_pick(self):
        name = filedialog.askopenfilename(filetypes=[('PNG 聊天截图', '*.png')])
        if name:
            self.acceptance_action('pick', path=name)
        else:
            self.notify('已取消选择；当前图片和验收结果保留。')

    def acceptance_action(self, action, **values):
        if self.busy or self.closing:
            self.notify('请等待当前操作完成，或先停止任务。', 'warning'); return
        if action in ('read', 'pick', 'ocr', 'route'):
            self.stop()
            self.current_id = None
        payload = dict(action=action, flow_id=self.acceptance.get('id'), **values)
        if self.acceptance.get('vision_signature'):
            payload['vision_signature'] = self.acceptance['vision_signature']
        self.acceptance_action_name = action
        self.busy = 'acceptance'
        self.worker.commands.put(('acceptance', payload, operation_id.get(), self.generation))
        names = dict(load='读取已保存的验收进度', pick='加载所选图片', authorize='保存上传授权',
            ocr='识别图片文字', vision_confirm='保存识字核对', route='选择固定话术',
            route_confirm='保存话术核对', read='读取真实测试会话', send='发送并核对一次', page='读取已完成结果')
        self.notify(names.get(action, action) + '…')
        self.refresh_setup()

    def acceptance_primary(self):
        data = self.acceptance; step = data.get('step', 0)
        if step == 0:
            if not data.get('image_id'): self.acceptance_pick()
            elif (data.get('vision') or {}).get('status') != 'vision_test_passed': self.acceptance_action('ocr')
            else: self.acceptance_action('vision_confirm')
        elif step == 1:
            if not (data.get('route') or {}).get('decision_json'): self.acceptance_action('route')
            else: self.acceptance_action('route_confirm')
        elif self.cfg['desktop']['test_send_verified']:
            self.window.next_step()
        elif (data.get('live') or {}).get('id') and (data.get('live') or {}).get('id') == self.current_id:
            self.wizard_send()
        else:
            self.acceptance_action('read')

    def confirm_vision(self):
        if not self.can_confirm_vision:
            raise ValueError('请先成功测试图片，并核对截图与识字文字。')
        if self.vision_test_signature != vision_signature(self.cfg['vision']):
            self.can_confirm_vision = False
            raise ValueError('视觉设置或提示词已变化，请重新测试。')
        config = copy.deepcopy(self.cfg)
        config['onboarding'].update(vision_verified=True, vision_signature=self.vision_test_signature)
        self.can_confirm_vision = False
        self.persist_config(config)
        self.notify('视觉识字已核对并保存。话术决策与发送验收分别记录。', 'success')

    def choose_image(self, *, vision_only=False):
        name = filedialog.askopenfilename(filetypes=[('PNG 截图', '*.png')])
        if name:
            self.can_confirm_vision = False
            self.command('vision_test' if vision_only else 'image', name)
        else:
            record('image.selection', 'cancelled')
            self.write(self.log, '已取消图片选择；保存的设置保持不变。')

    def open_minimax_key_page(self):
        host = 'platform.minimax.io' if self.api_region_var.get() == '国际站' else 'platform.minimax.cn'
        webbrowser.open(f'https://{host}/subscribe/token-plan')

    def setup_add_profile(self, label=None, account=None, kind=None):
        config = copy.deepcopy(self.cfg)
        add_profile(config, label or self.window.new_label.text(), account or self.window.new_account.text(),
            kind or self.window.new_kind.currentData())
        self.unread_samples = {}; self.doctor_passed = False
        self.persist_config(config)

    def setup_select_profile(self):
        config = copy.deepcopy(self.cfg)
        select_profile(config, self.window.profile_choice.currentData())
        self.unread_samples = {}; self.doctor_passed = False
        self.persist_config(config)
        self.query('home'); self.query('blocked')

    def setup_save_unread_mode(self):
        mode = self.window.unread_mode.currentData()
        if mode != self.cfg['desktop']['unread_mode']:
            config = copy.deepcopy(self.cfg)
            config['desktop'].update(unread_mode=mode, unread_locator_reviewed=False, auto_send_enabled=False)
            self.unread_samples = {}
            self.persist_config(config)

    def open_recorder(self):
        code = shutil.which('code')
        if not code:
            candidate = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/Microsoft VS Code/bin/code.cmd'
            code = str(candidate) if candidate.is_file() else None
        if not code:
            webbrowser.open('https://code.visualstudio.com/download')
            self.notify('已打开 VS Code 官方下载页。安装后在扩展中安装 Clicknium，再回到这里打开录制项目。', 'warning')
            return
        args = [code, str(DATA_ROOT)]
        if Path(code).suffix.lower() in ('.cmd', '.bat'):
            args = [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', code, str(DATA_ROOT)]
        subprocess.Popen(args, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.notify('录制项目已打开。按图文指引录制 5 个位置；录完回到这里验证。')

    def connect_recording(self):
        if self.busy or self.closing:
            raise RecordingError('请等待当前任务完成或停止后再接入录制位置。')
        checked = check_recording(self.cfg, DATA_ROOT)
        record('recording.inspect', 'success' if checked.ready else 'pending',
               {'candidates': checked.candidates, 'reason': checked.reason})
        if checked.ready:
            self.notify('录制文件与配置路径一致；仍需检查真实页面匹配结果。', 'success')
            return False
        if not checked.candidates:
            raise RecordingError(checked.reason)
        name = checked.candidates[0]
        if len(checked.candidates) > 1:
            name, accepted = QInputDialog.getItem(self.window, '选择本次录制库',
                '发现多个完整录制库，请选择你刚录制的 Store：', list(checked.candidates), 0, False)
            if not accepted:
                record('recording.selection', 'cancelled')
                self.notify('已取消接入录制库；原配置保持不变。', 'warning')
                return None
        config = bind_recording(self.cfg, DATA_ROOT, name)
        backup = backup_recording_config(DATA_ROOT)
        record('recording.bind.intent', details={'store': name, 'backup': str(backup) if backup else None,
            'previous': self.cfg['desktop']['locators'], 'locators': config['desktop']['locators']})
        self.persist_config(config)
        self.unread_samples = {}; self.doctor_passed = False
        record('recording.bound', details={'store': name, 'locators': config['desktop']['locators']})
        self.notify(f'已接入录制库 {name}；未改录制规则，尚未通过页面验收。', 'success')
        return True

    def doctor(self, stage=None):
        changed = self.connect_recording()
        if changed is None:
            return
        if stage == 'empty': self.unread_samples = {}
        if changed:
            # Wait for reconfiguration; command() would otherwise discard the queued configure.
            self.recording_check_pending = (stage, self.generation, operation_id.get())
            self.notify('已接入录制库，正在加载配置；随后自动检查 5 个位置…')
            return
        self.command('doctor', stage)

    def confirm_locators(self):
        if not self.doctor_passed:
            raise ValueError('请先检查买家名、聊天区、输入框、发送按钮均唯一匹配且可读取。')
        config = copy.deepcopy(self.cfg)
        config['desktop']['locators_reviewed'] = True
        baseline = self.unread_samples.get('empty')
        passed = (type(baseline) is int and baseline >= 0 and self.unread_samples.get('unread', -1) > baseline
            and self.unread_samples.get('read') == baseline) if (config['desktop']['unread_mode'] == 'red_badge'
            or config['desktop'].get('adapter') == 'xianyu_web') else (
            baseline == 0 and self.unread_samples.get('unread', 0) > 0 and self.unread_samples.get('read') == 0)
        config['desktop']['unread_locator_reviewed'] = passed
        self.persist_config(config)
        self.window.validation_result.appendPlainText('未读变化验收：' + ('已通过并保存。' if passed else '尚未通过，需完成增加并回到基线的检查。'))
        record('recording.unread_review', 'success' if passed else 'pending',
               {'adapter':config['desktop'].get('adapter'),'counts':self.unread_samples,'passed':passed})
        self.notify('聊天位置验证已保存。' + ('未读检查通过。' if passed else '未读变化尚未通过；仍可手动预览。'),
            'success' if passed else 'warning')

    def reload_config(self):
        self.persist_config(copy.deepcopy(self.cfg))

    def save(self):
        config = copy.deepcopy(self.cfg)
        for (group, key), var in self.entries.items():
            old = config[group][key]; value = var.get().strip()
            config[group][key] = int(value) if type(old) is int else float(value) if type(old) is float else value
        if any(config['jev'][k] != self.cfg['jev'][k] for k in ('api_key', 'endpoint', 'model')): config['onboarding']['jev_verified'] = False
        if any(config['vision'][k] != self.cfg['vision'][k] for k in ('provider', 'endpoint', 'model', 'api_key', 'buyer_side')):
            config['onboarding']['vision_verified'] = False; self.can_confirm_vision = False
        self.persist_config(config)
        self.notify('高级设置已保存。监测未启动。', 'success')

    def command(self, name, value=None, *, queued=False):
        if self.closing: return
        if self.busy and name not in BACKGROUND and not queued:
            self.notify('正在执行上一个操作，请等待完成或先停止。', 'warning'); return
        if name not in BACKGROUND:
            if name in ('current', 'image', 'text', 'vision_test', 'doctor', 'send', 'library_import', 'library_preview'):
                self.stop(preserve_candidate=name == 'send')
            self.busy = name
            if name in ('current', 'text', 'image', 'vision_test', 'monitor', 'monitor_unverified'):
                self.current_id = None; self.send_block_reason = '正在处理，尚未生成可发送候选。'
            self.notify('正在执行：' + self.window.command_name(name) + '…')
        self.worker.commands.put((name, value, operation_id.get(), self.generation))
        self.refresh_setup()

    def stop(self, *, preserve_candidate=False):
        self.recording_check_pending = None
        self.generation += 1
        self.worker.stop_event.set()
        while True:
            try:
                discarded = self.worker.commands.get_nowait()
                record('command.discarded', 'cancelled', {'command': discarded[0]})
            except queue.Empty: break
        self.worker.commands.put(('stop', {'preserve_candidate': preserve_candidate}, operation_id.get(), self.generation))
        self.busy = 'stop'
        self.current_id = None; self.expect_test_send = False
        self.send_block_reason = '任务已停止，请重新读取当前会话。'
        if hasattr(self, 'window'): self.notify('正在停止；不会继续自动发送。')

    def start_auto(self):
        if not can_auto_send(self.cfg):
            self.notify('暂不能开始：' + '、'.join(self.missing() or ['尚未允许自动发送']), 'warning'); return
        if self.confirm('开始真实接待', '将向当前平台真实客户发送已批准的固定话术。请保持聊天页可见，不同时操作同一窗口。现在开始？'):
            self.command('monitor', True)

    def enable_auto(self):
        if self.missing(): raise ValueError('请先完成：' + '、'.join(self.missing()))
        config = copy.deepcopy(self.cfg); config['desktop']['auto_send_enabled'] = True
        self.persist_config(config)
        self.notify('自动发送权限已保存。进入首页后仍需手动点击开始接待。', 'success')

    def start_unverified_preview(self):
        self.command('monitor_unverified')

    def approve(self):
        if not self.current_id: self.notify(self.send_block_reason, 'warning'); return
        if self.confirm('确认发送一次', '已核对当前客户、客户问题及固定回复？窗口或内容变化会阻止发送。'):
            self.command('send', self.current_id)

    def wizard_send(self):
        if not self.current_id: self.notify(self.send_block_reason, 'warning'); return
        live = self.acceptance.get('live') or {}
        if live.get('id') == self.current_id:
            from .history import nickname
            text = (live.get('decision') or {}).get('text', '')
            if not text: self.notify('候选回复为空，请重新读取。', 'warning'); return
            if self.confirm('确认测试发送一次', '请确认这是你控制的测试买家。\n\n收件人：' +
                    nickname(live.get('customer')) + '\n\n回复全文：\n' + text + '\n\n现在实际发送一次？'):
                self.acceptance_action('send', candidate_id=self.current_id)
            else: self.notify('已取消发送；没有点击发送按钮。')
            return
        if self.confirm('测试买家发送验收', '确认当前客户是你控制的测试买家，并已核对固定回复。现在实际发送一次？'):
            self.command('send', self.current_id)
            self.expect_test_send = True

    def reject(self):
        if self.current_id: self.command('reject', self.current_id)
        self.current_id = None

    def resume(self, customer=None):
        customer = customer or self.current_customer
        if not customer: raise ValueError('请先选择待人工处理客户。')
        if self.confirm('恢复后续接待', f'确认已核查 {customer.split(" :: ")[-1]} 的聊天页面？恢复后只处理后续消息，旧回复不会重发。'):
            self.command('resume', customer)

    def manual_done(self, customer): self.command('manual_done', customer)

    def import_replies(self):
        name = filedialog.askopenfilename(title='导入话术', filetypes=[('话术表', '*.xlsx *.csv')])
        if name: self.command('library_import', name)
        else: self.notify('已取消导入；当前话术版本保持不变。')

    def activate_version(self, version_id):
        library = ReplyLibrary()
        try: config = library.activate_config(self.cfg, version_id)
        finally: library.close()
        self.persist_config(config)
        record('knowledge.version.activated', details={'version': version_id})
        self.notify('话术版本已启用。回复正文与审批标记按审核结果保存。', 'success')
        self.command('library_list')

    def template(self):
        name = filedialog.asksaveasfilename(title='下载话术模板', initialfile='Jev-话术模板.xlsx', filter='Excel 工作簿 (*.xlsx)')
        if name:
            ReplyLibrary.export_template(Path(name))
            self.notify('模板已保存；示例话术未批准自动发送。', 'success')

    def export_replies(self):
        source = knowledge_file(self.cfg['knowledge']['replies_csv'])
        name = filedialog.asksaveasfilename(title='导出话术供编辑', initialfile='Jev-当前话术.csv', filter='CSV 话术 (*.csv)')
        if name:
            destination = Path(name).resolve()
            if destination == source.resolve():
                raise ValueError('请选择另一个文件名，不能覆盖当前启用的话术。')
            shutil.copyfile(source, destination)
            record('knowledge.exported', details={'path': str(destination)})
            self.notify('当前话术已导出；修改后重新导入审核，原版本保持不变。', 'success')

    def query(self, target, filters=None, page=0):
        self.command('ui_query', {'target': 'events' if target == 'home' else target,
            'slot': target, 'filters': filters or {}, 'page': page})

    def show_trace_history(self):
        self.window.navigate('history'); self.window.history_tabs.setCurrentIndex(2)

    def export_trace(self):
        name = filedialog.asksaveasfilename(title='导出脱敏诊断包', initialfile='Jev-诊断记录.zip', filter='ZIP 诊断包 (*.zip)')
        if name:
            filters = self.window.history_filters()
            filters['platform'] = self.cfg['active_profile'] + ' :: ' + self.cfg['desktop']['shop_id']
            get_audit().export(Path(name), filters)
            self.notify('诊断包已导出，认证密钥已脱敏。')

    def edit_cost_rates(self): self.window.show_rates(self.cost_rates)

    def open_file(self, path):
        record('file.open', details={'path': str(path)})
        os.startfile(str(Path(path).resolve()))

    def refresh_setup(self):
        if hasattr(self, 'window'): self.window.refresh_state()

    def drain(self):
        try:
            while True: self.handle_event(self.output.get_nowait())
        except queue.Empty: pass
        except Exception as error:
            self.current_id = None
            self.stop()
            record('ui.result.failed', 'failed', exception_details(error))
            self.notify('界面结果显示失败，任务已停止：' + str(error), 'error')
        self.window.refresh_running()
        if self.closing and not self.worker.is_alive():
            self.closed = True; self.timer.stop(); self.window.close()

    def handle_event(self, item):
        if item.get('generation') is not None and item['generation'] != self.generation:
            record('ui.stale_configuration_event', 'skipped', {'kind': item['kind']}); return
        if item.get('profile') and item['profile'] != self.cfg['active_profile']:
            record('ui.stale_profile_event', 'skipped', {'kind': item['kind']}); return
        kind = item['kind']
        if kind == 'recording_prepared':
            self.cfg['desktop'].update(item['desktop'])
            sync_active(self.cfg)
            if item.get('changed'):
                self.unread_samples = {}
                self.current_id = None
                self.send_block_reason = '未读规则已更新，请重新读取测试会话。'
            self.notify(item['text'], 'success' if item['status'] == 'prepared' else 'warning')
            self.window.validation_result.setPlainText(item['text'])
            self.refresh_setup(); return
        if kind == 'acceptance_result':
            if self.acceptance.get('id') and item['flow_id'] != self.acceptance['id']:
                record('acceptance.stale_result', 'skipped'); return
            self.acceptance = item['data']
            for group, values in item.get('metadata', {}).items():
                self.cfg[group].update(values)
            sync_active(self.cfg)
            self.consent_var.set(self.cfg['desktop']['data_upload_authorized'])
            live = self.acceptance.get('live') or {}
            self.current_id = live.get('id')
            self.current_customer = live.get('customer')
            self.send_block_reason = live.get('reason', '请读取真实测试会话。')
            if live.get('status') == 'sent_ui_verified':
                self.send_block_reason = '网页新增回复已确认；没有自动开启接待。'
            self.window.acceptance_panel.render(self.acceptance)
            notices = dict(load='验收进度已加载；已保存的结果可回看。', pick='图片已加载，尚未调用识字服务。',
                authorize='上传授权已保存。', ocr='识字完成，请对照原图核对文字和身份。',
                vision_confirm='识字核对已保存，下一步测试选择话术。', route='话术选择完成，请核对回复或人工处理原因。',
                route_confirm='图片话术核对已保存，下一步读取真实测试会话。', page='正在查看已保存结果，没有重新调用 API。',
                read='候选已生成，尚未发送。' if live.get('id') else self.send_block_reason,
                send='网页新增回复已确认；此记录不是服务器送达回执。' if live.get('status') == 'sent_ui_verified' else self.send_block_reason)
            self.notify(self.acceptance.get('error') or notices.get(self.acceptance_action_name, '结果已保存，尚未开启接待。'),
                'error' if self.acceptance.get('error') else 'info' if self.acceptance_action_name in ('load', 'page') else 'success')
            self.refresh_setup(); return
        if item.get('flow_id'):
            if kind in ('operation_failed', 'send_failed', 'send_unknown'):
                self.send_block_reason = item.get('text', '操作失败，请查看记录。')
                self.notify(self.send_block_reason, 'error')
            elif item.get('text'): self.notify(item['text'])
            return
        if kind == 'command_done':
            if self.busy == item['command']: self.busy = None
            if item['command'] == 'configure':
                self.acceptance = {}
                self.window.finish_startup(item.get('ok', True))
                if item.get('ok'):
                    self.query('home'); self.query('blocked')
                    self.notify('配置与本地记录已加载；尚未开始接待。', 'success')
                pending, self.recording_check_pending = self.recording_check_pending, None
                if pending and pending[1] == self.generation and item.get('ok'):
                    token = operation_id.set(pending[2])
                    try:
                        self.doctor(pending[0])
                    finally:
                        operation_id.reset(token)
                elif item.get('ok') and self.window.wizard_pages.currentIndex() == 4:
                    self.acceptance_action('load')
            if item['command'] == 'acceptance':
                self.acceptance_action_name = None
                self.query('blocked'); self.query('home')
            if item['command'] == 'stop' and self.busy is None:
                if not self.window.initialized and getattr(self.worker, 'service', None):
                    self.window.finish_startup()
                self.notify('已停止；不会继续自动发送。')
            if item['command'] in ('resume', 'manual_done', 'send', 'current'):
                self.query('blocked'); self.query('home')
            self.refresh_setup(); return
        if kind == 'decision':
            self.current_id = item.get('id'); self.current_customer = item.get('customer')
            d = item['decision']; self.last_result = item
            self.send_block_reason = d.get('reason', '') if d['action'] == 'human' else (
                '此结果仅用于不发送预览：' + item.get('source', '') if not self.current_id else '')
            self.window.show_decision(item)
            self.notify('已生成候选回复，尚未发送。' if self.current_id else self.send_block_reason,
                'success' if self.current_id else 'warning')
            self.query('home'); self.query('blocked')
        elif kind == 'vision_test_result':
            self.vision_test_signature = item['signature']
            self.can_confirm_vision = item['signature'] == vision_signature(self.cfg['vision'])
            self.notify('识字成功。请核对实际截图和文字后确认；未调用 Jev，未检查最新位置。', 'success')
            self.command('vision_audit', item['audit_id'])
        elif kind == 'jev_verified':
            config = copy.deepcopy(self.cfg); config['onboarding']['jev_verified'] = True
            self.persist_config(config)
            self.notify('Jev 接口已真实测试通过。', 'success')
        elif kind == 'doctor':
            r = item['result']
            self.doctor_passed = all(r.get(n) == '唯一匹配' for n in ('current_user', 'chat_window', 'reply_text', 'button_send')) and all(
                r.get(n) is True for n in ('customer_readable', 'input_readable', 'chat_capture_valid'))
            self.connection_checked = self.doctor_passed
            stage = item.get('stage')
            if self.doctor_passed and isinstance(r.get('new_message_count'), int) and stage in ('empty', 'unread', 'read'):
                self.unread_samples[stage] = r['new_message_count']
            self.window.show_doctor(r, stage)
            self.notify('检查完成：' + ('4 个聊天位置可读取。' if self.doctor_passed else '位置检查未通过，请按具体结果调整。'),
                'success' if self.doctor_passed else 'warning')
        elif kind in ('operation_failed', 'preview_skipped', 'blocked', 'send_failed', 'send_unknown'):
            self.current_id = None; self.current_customer = item.get('customer', self.current_customer)
            self.send_block_reason = item.get('text', '发送结果未知，请人工核查。'); self.expect_test_send = False
            self.notify(self.send_block_reason, 'error' if kind in ('operation_failed', 'send_unknown', 'send_failed') else 'warning')
            if kind == 'operation_failed': self.connection_checked = False
            self.window.show_result_text(self.send_block_reason); self.query('blocked')
        elif kind == 'sent':
            self.current_id = None
            if self.expect_test_send:
                self.expect_test_send = False
                config = copy.deepcopy(self.cfg); config['desktop']['test_send_verified'] = True
                self.persist_config(config)
            self.notify('网页新增回复气泡已确认。此记录不代表服务器送达回执。', 'success')
            self.query('home'); self.query('blocked')
        elif kind == 'ui_query': self.window.show_query(item)
        elif kind == 'library_versions': self.window.show_versions(item['rows'])
        elif kind == 'library_preview': self.window.show_library_preview(item['version'], item['rows'])
        elif kind == 'vision_audit': self.window.show_vision(item['row'])
        elif kind == 'cost_report':
            self.cost_rates = item['rates']; self.window.show_costs(item)
        elif kind == 'watch_status': self.window.watch_label.setText('左侧未读 ＋ 当前会话 · ' + item.get('customer', ''))
        elif kind == 'status': self.window.runtime_label.setText(item.get('text', ''))
        elif item.get('text'):
            self.live_logs.append((time.time(), item['text'])); self.live_logs = self.live_logs[-100:]
            self.window.show_live_log(self.live_logs)
        self.refresh_setup()

    def close(self):
        if self.closed: return
        self.closing = True; self.stop(); self.worker.exit_event.set()
        self.notify('正在关闭，等待后台任务安全结束。')
        record('app.close.requested')
        if not self.worker.is_alive(): self.closed = True; self.window.close()


def audit_ui(fn):
    @functools.wraps(fn)
    def invoke(self, *args, **kwargs):
        cfg = getattr(self, 'cfg', {}); register_secrets(cfg)
        token = platform_id.set(cfg.get('active_profile', 'default') + ' :: ' + cfg.get('desktop', {}).get('shop_id', ''))
        try:
            with operation('ui.' + fn.__name__, parent=operation_id.get()): return fn(self, *args, **kwargs)
        finally: platform_id.reset(token)
    return invoke


for _name in ('persist_config', 'setup_save_only', 'setup_save_api', 'setup_test_jev', 'setup_test_image', 'confirm_vision',
    'acceptance_pick', 'acceptance_action', 'acceptance_primary',
    'open_minimax_key_page', 'setup_add_profile', 'setup_select_profile', 'setup_save_unread_mode', 'open_recorder', 'connect_recording', 'doctor',
    'confirm_locators', 'reload_config', 'wizard_send', 'enable_auto', 'save', 'choose_image', 'start_auto', 'approve', 'reject',
    'resume', 'stop', 'close', 'command', 'start_unverified_preview', 'edit_cost_rates', 'show_trace_history', 'open_file',
    'import_replies', 'activate_version', 'template', 'export_replies', 'manual_done', 'export_trace'):
    setattr(App, _name, audit_ui(getattr(App, _name)))


def ui_check():
    if not os.environ.get('JEVTOOL_DATA_DIR'):
        raise ValueError('界面检查必须显式使用隔离的 JEVTOOL_DATA_DIR，不能写真实商家目录。')
    application = QApplication.instance() or QApplication([])
    from .presentation import install_style
    install_style(application)
    app = App(); app.window.show()
    result = {'version': VERSION, 'ok': False}
    def finish():
        if app.busy == 'configure':
            result['error'] = '后台初始化未完成'
        else:
            app.window.navigate('home')
            application.processEvents()
            app.window.grab().save(str(DATA_ROOT / 'ui-check.png'))
            result.update(ok=app.initialized if hasattr(app, 'initialized') else app.window.initialized,
                monitor=app.worker.monitor, page=app.window.route, qt_font=application.font().family(),
                width=app.window.width(), height=app.window.height(),
                stop_visible=app.window.top_stop.isVisible() and app.window.rect().contains(
                    app.window.top_stop.mapTo(app.window, app.window.top_stop.rect().bottomRight())))
        (DATA_ROOT / 'ui-check.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        app.close()
    QTimer.singleShot(1500, finish)
    QTimer.singleShot(7000, application.quit)
    application.exec()
    return 0 if result['ok'] and result.get('stop_visible') and not result.get('monitor') else 1


def launch():
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName('Jev 客服工具'); application.setApplicationVersion(VERSION)
    from .presentation import install_style
    install_style(application)
    try: app = App()
    except Exception as error:
        record('app.startup', 'failed', {'error': error})
        from PySide6.QtWidgets import QDialog, QVBoxLayout
        from .presentation import label, button, note
        dialog = QDialog(); dialog.setWindowTitle('Jev · 启动需要处理'); dialog.resize(680, 440)
        layout = QVBoxLayout(dialog); layout.setContentsMargins(40, 40, 40, 40); layout.setSpacing(24)
        layout.addWidget(label('工作台暂时无法加载', 'h1'))
        reason = label(str(clean(error)), 'small'); layout.addWidget(reason)
        layout.addWidget(note('原配置未重置', '请核对用户目录中的配置与话术文件，修正后重试。无需重新录制定位器。', 'warning'))
        def retry():
            try:
                with operation('ui.startup.retry'):
                    next_app = App(); application._jev_app = next_app
                    next_app.window.show(); dialog.close()
            except Exception as failure:
                reason.setText(str(clean(failure)))
        layout.addWidget(button('重新加载', retry, 'primary'))
        layout.addWidget(button('打开用户数据目录', lambda: os.startfile(str(DATA_ROOT))))
        dialog.show(); application.exec(); return
    app.window.show()
    application.exec()
