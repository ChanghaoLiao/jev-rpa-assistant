"""Native, editable Qt widgets translated from the approved Figma layouts."""
from __future__ import annotations
import copy
import functools
import csv
import io
import json
import time
from pathlib import Path
from PySide6.QtCore import Qt, QDate, QSize, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QMainWindow, QWidget, QFrame, QVBoxLayout, QHBoxLayout, QStackedWidget,
    QLineEdit, QComboBox, QCheckBox, QPlainTextEdit, QTabWidget, QTableWidgetItem, QDialog,
    QDialogButtonBox, QDateEdit, QGraphicsOpacityEffect, QSizePolicy)
from .presentation import label, button, card, horizontal, badge, icon, icon_label, note, scroll, table, Value, clear_layout, COLORS
from .paths import DATA_ROOT, SOURCE_ROOT, knowledge_file
from .knowledge import Knowledge
from .library import ReplyLibrary
from .onboarding import can_auto_send
from .costs import validate_rates
from .audit import record, clean, get_audit, operation, operation_id, platform_id
from .history import nickname

STEPS = ['连接服务', '平台与录制', '验证聊天窗口', '导入与审核话术', '验收接待流程', '准备完成']
STATUS = dict(sent_ui_verified='界面已确认', send_aborted_before_click='点击前中止', sent='旧版已发送', human='需人工处理', candidate='候选 · 尚未发送', sending='正在发送',
    send_unknown='发送结果未知', rejected='已放弃', preview='候选 · 尚未发送', preview_only='仅预览', previewed='已选话术', vision_failed='识字失败', extracted='识字成功', failed='失败',
    success='成功', cancelled='已取消', skipped='已跳过', started='执行中')


def stamp(value, full=False):
    return time.strftime('%Y-%m-%d %H:%M:%S' if full else '%m-%d %H:%M', time.localtime(value))


def text_json(value):
    return json.dumps(clean(value), ensure_ascii=False, indent=2, default=str)


class Window(QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle('Jev 客服工具 · 0.3.5')
        self.setWindowIcon(icon('brand'))
        self.setMinimumSize(860, 500)
        available = self.screen().availableGeometry()
        self.resize(min(1440, max(860, available.width()-32)), min(900, max(500, available.height()-32)))
        self.entries = {}
        self.guarded_buttons = []
        self.initialized = False
        self.history_page = self.cost_page = 0
        self.history_rows = []; self.version_rows = []; self.blocked_rows = []; self.cost_rows = []
        self.route = 'startup'
        shell = QWidget(); shell.setObjectName('shell'); self.setCentralWidget(shell)
        outer = QVBoxLayout(shell); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)
        header = QFrame(); header.setObjectName('header'); header.setFixedHeight(88)
        h = QHBoxLayout(header); h.setContentsMargins(40, 16, 40, 16); h.setSpacing(12)
        h.addWidget(icon_label('brand', 36))
        brand = QVBoxLayout(); brand.setSpacing(0); brand.addWidget(label('Jev 客服助手', 'h3')); brand.addWidget(label('商家接待工作台', 'caption')); h.addLayout(brand)
        h.addStretch(); h.addWidget(label('所有操作记录保存在本机', 'small', wrap=False))
        self.return_home = button('返回接待首页', lambda: self.navigate('home')); h.addWidget(self.return_home); self.return_home.hide()
        self.top_stop = button('停止任务', lambda: app.call(app.stop), 'danger'); self.top_stop.setObjectName('alwaysStop'); h.addWidget(self.top_stop)
        outer.addWidget(header)
        body = QHBoxLayout(); body.setContentsMargins(0, 0, 0, 0); body.setSpacing(0); outer.addLayout(body, 1)
        self.sidebar = QFrame(); self.sidebar.setObjectName('sidebar'); self.sidebar.setFixedWidth(208)
        side = QVBoxLayout(self.sidebar); side.setContentsMargins(16, 32, 16, 32); side.setSpacing(16)
        self.nav = {}
        for key, title, asset in [('home', '接待', 'chat'), ('history', '历史', 'clock'), ('cost', '费用', 'list'), ('settings', '设置', 'settings')]:
            b = button(title, lambda k=key: self.navigate(k), 'nav'); b.setIcon(icon(asset)); b.setIconSize(QSize(20, 20)); side.addWidget(b); self.nav[key] = b
        side.addStretch()
        self.profile_label = label('', 'small'); self.connection_label = label('浏览器 · 尚未检查', 'caption')
        side.addWidget(self.profile_label); side.addWidget(self.connection_label); side.addWidget(label('版本 0.3.5', 'caption'))
        body.addWidget(self.sidebar)
        content = QWidget(); self.content_layout = QVBoxLayout(content); self.content_layout.setContentsMargins(40, 24, 40, 16); self.content_layout.setSpacing(16)
        self.feedback = label('', 'small'); self.feedback.setMinimumWidth(0); self.feedback.hide(); self.content_layout.addWidget(self.feedback)
        self.pages = QStackedWidget(); self.content_layout.addWidget(self.pages, 1); body.addWidget(content, 1)
        self.page_map = {}
        self.build_startup(); self.build_home(); self.build_wizard(); self.build_history(); self.build_costs(); self.build_settings()
        self.update_fields(app.cfg)
        self.navigate('startup')

    def action(self, title, fn, role='secondary', guarded=True):
        b = button(title, lambda: self.app.call(fn), role)
        if guarded:
            self.guarded_buttons.append(b)
            b.destroyed.connect(lambda: self.guarded_buttons.remove(b) if b in self.guarded_buttons else None)
        return b

    def register_page(self, key, widget):
        self.page_map[key] = widget; self.pages.addWidget(widget)

    def page(self, title, eyebrow='', subtitle=''):
        w = QWidget(); w.setObjectName('page'); lay = QVBoxLayout(w); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(24)
        if eyebrow: lay.addWidget(label(eyebrow, 'blue'))
        lay.addWidget(label(title, 'h1'))
        if subtitle: lay.addWidget(label(subtitle, 'small'))
        return w, lay

    def field(self, layout, title, value='', *, secret=False):
        layout.addWidget(label(title, 'small'))
        edit = QLineEdit(str(value)); edit.setMinimumWidth(0)
        if secret: edit.setEchoMode(QLineEdit.Password)
        layout.addWidget(edit)
        return edit

    def build_startup(self):
        w, lay = self.page('准备你的接待工作台', 'JEV / STARTUP')
        lay.addStretch()
        c, cl = card(40, 24); cl.addWidget(icon_label('brand', 48)); cl.addWidget(label('正在加载', 'h2'))
        self.loading_stage = label('配置已读取 · 正在打开本地记录与后台任务队列', 'small'); cl.addWidget(self.loading_stage)
        cl.addWidget(note('真实加载阶段', '本地记录、话术库与后台服务准备完成后进入工作台。此时不会连接聊天页或自动发送。'))
        cl.addWidget(self.action('重新加载', lambda: self.app.call(self.app.reload_config)))
        lay.addWidget(c); lay.addStretch(); self.register_page('startup', w)

    def finish_startup(self, ok=True):
        if not ok:
            self.loading_stage.setText('后台初始化失败，请查看上方原因。修正后可重新加载；原配置未重置。'); return
        if self.initialized: return
        self.initialized = True
        step = self.app.first_missing_step()
        if step == 5:
            self.navigate('home')
        else:
            saved = int(self.app.ui_state.data.get('wizard_step', step))
            self.set_step(min(step, max(0, saved))); self.navigate('wizard')

    def navigate(self, route):
        if route not in self.page_map: return
        self.route = route; self.pages.setCurrentWidget(self.page_map[route])
        for key, b in self.nav.items():
            b.setProperty('active', key == route); b.style().unpolish(b); b.style().polish(b)
        self.sidebar.setVisible(route not in ('startup', 'wizard'))
        self.return_home.setVisible(route == 'wizard')
        self.adapt_layout()
        if route == 'home' and self.initialized: self.app.query('home'); self.app.query('blocked')
        if route == 'history' and self.initialized: self.refresh_history()
        if route == 'cost' and self.initialized: self.app.command('costs', self.cost_page)
        if route == 'settings' and self.initialized: self.app.command('library_list')
        if self.initialized and not self.app.ui_state.data.get('reduced_motion'):
            effect = QGraphicsOpacityEffect(self.pages); self.pages.setGraphicsEffect(effect)
            self.animation = QPropertyAnimation(effect, b'opacity', self); self.animation.setDuration(180)
            self.animation.setStartValue(.65); self.animation.setEndValue(1.0); self.animation.setEasingCurve(QEasingCurve.OutCubic); self.animation.start()
        record('ui.page', details={'page': route})

    def set_feedback(self, text, state):
        self.feedback.setText(text)
        color = COLORS['red' if state == 'error' else 'amber' if state == 'warning' else 'green' if state == 'success' else 'secondary']
        self.feedback.setStyleSheet(f'color:{color};font-size:14px;padding:8px 0;')
        if hasattr(self, 'acceptance_panel') and self.route == 'wizard' and self.wizard_pages.currentIndex() == 4:
            self.feedback.hide()
            self.acceptance_panel.execution.setText(text)
            self.acceptance_panel.execution.setStyleSheet(f'color:{color};font-size:14px;')
            self.acceptance_panel.execution.show()
        else: self.feedback.show()

    def build_home(self):
        w, lay = self.page('', '接待 / LIVE WORKSPACE'); title = lay.takeAt(1).widget(); title.deleteLater()
        self.start_button = self.action('开始接待', self.app.start_auto, 'primary')
        lay.addLayout(horizontal(label('每条消息，都有着落。', 'h1'), 1, self.start_button))
        self.runtime_panel, rt = card(24, 8)
        self.runtime_label = label('尚未启动接待', 'h3'); self.watch_label = label('左侧未读与当前会话变化轮流检查。', 'small')
        content = QVBoxLayout(); content.addWidget(self.runtime_label); content.addWidget(self.watch_label)
        row = horizontal(icon_label('shield', 28)); row.addLayout(content, 1)
        self.runtime_badge = label('未运行', 'small'); row.addWidget(self.runtime_badge); rt.addLayout(row); lay.addWidget(self.runtime_panel)
        columns = QHBoxLayout(); columns.setSpacing(24)
        timeline, tl = card(24); tl.addLayout(horizontal(label('接待记录', 'h2'), 1, self.action('刷新', lambda: self.app.query('home'), guarded=False)))
        self.timeline_content = QWidget(); self.timeline_layout = QVBoxLayout(self.timeline_content); self.timeline_layout.setContentsMargins(0, 0, 0, 0); self.timeline_layout.setSpacing(20)
        tl.addWidget(scroll(self.timeline_content), 1)
        tl.addWidget(self.action('查看完整历史', lambda: self.navigate('history'), guarded=False))
        columns.addWidget(timeline, 1)
        self.pending_panel, pl = card(24); self.pending_panel.setFixedWidth(344)
        self.pending_title = label('待人工处理', 'h2'); pl.addWidget(self.pending_title)
        self.pending_content = QWidget(); self.pending_layout = QVBoxLayout(self.pending_content); self.pending_layout.setContentsMargins(0, 0, 0, 0); self.pending_layout.setSpacing(20)
        pl.addWidget(scroll(self.pending_content), 1)
        pl.addWidget(self.action('查看人工待办', self.show_pending_dialog, guarded=False))
        columns.addWidget(self.pending_panel); lay.addLayout(columns, 1)
        self.pending_compact = self.action('查看人工待办', self.show_pending_dialog, guarded=False); lay.addWidget(self.pending_compact); self.pending_compact.hide()
        self.home_footer = label('接待尚未启动。进入首页不会自动发送。', 'caption'); lay.addWidget(self.home_footer)
        self.render_timeline([]); self.render_pending([]); self.register_page('home', w)

    def render_timeline(self, rows):
        clear_layout(self.timeline_layout)
        if not rows:
            self.timeline_layout.addWidget(note('接待记录会显示在这里', '首次配置完成后手动开始接待。每条问题、固定回复和处理结果都可以回看。'))
        for r in rows[:30]:
            d = r.get('decision') or {}; status = r.get('status', 'preview')
            item = QWidget(); il = QVBoxLayout(item); il.setContentsMargins(0, 12, 0, 16); il.setSpacing(10)
            il.addLayout(horizontal(label(stamp(r['created']), 'caption'), label(nickname(r.get('customer')) or '话术测试'), 1,
                badge(STATUS.get(status, status), 'success' if status == 'sent_ui_verified' else 'warning' if status in ('human', 'send_unknown', 'send_aborted_before_click') else 'info')))
            il.addWidget(label(r.get('question') or '原问题见识字或操作详情', 'small'))
            reply = label(d.get('text') or d.get('reason') or '此操作未生成固定回复', 'small')
            reply.setStyleSheet('background:#EDF1F5; padding:10px 12px; border-radius:4px; color:#526078;'); il.addWidget(reply)
            il.addWidget(self.action('查看决策与证据', lambda row=r: self.event_details(row), guarded=False))
            self.timeline_layout.addWidget(item)
        self.timeline_layout.addStretch()

    def render_pending(self, rows):
        self.blocked_rows = rows; self.pending_title.setText(f'待人工处理  {len(rows)}')
        self.pending_compact.setText(f'查看人工待办 · {len(rows)} 位客户')
        clear_layout(self.pending_layout)
        if not rows:
            self.pending_layout.addWidget(note('暂时没有待办', '遇到无法判断或发送结果未知时，只暂停对应客户，并保留具体原因。'))
        for r in rows[:20]:
            item = QWidget(); il = QVBoxLayout(item); il.setContentsMargins(0, 8, 0, 16); il.setSpacing(12)
            il.addWidget(label(nickname(r['customer']), 'h3'))
            il.addWidget(badge('可能已发送 · 需核查' if r['possibly_sent'] else '未确认发送 · 待处理', 'warning'))
            il.addWidget(label(r['reason'], 'small'))
            if r.get('manual_done'): il.addWidget(label('已标记人工处理，尚未恢复', 'caption'))
            il.addWidget(self.action('查看与处理', lambda row=r: self.pending_details(row), guarded=False))
            self.pending_layout.addWidget(item)
        self.pending_layout.addStretch()

    def build_wizard(self):
        w = QWidget(); lay = QHBoxLayout(w); lay.setContentsMargins(0, 8, 0, 8); lay.setSpacing(24)
        self.rail, rl = card(24, 20); self.rail.setFixedWidth(256)
        rl.addWidget(label('首次配置', 'h2')); rl.addWidget(label('一次只做一件事。\n保存进度，下次继续。', 'small'))
        self.step_buttons = []
        for i, text in enumerate(STEPS):
            b = button(f'{i+1:02d}  {text}', lambda n=i: self.set_step(n), 'nav'); self.step_buttons.append(b); rl.addWidget(b)
        rl.addStretch(); rl.addWidget(label('页面前进不会替代真实验收。\n已通过的配置会保留。', 'caption')); lay.addWidget(self.rail)
        container, cl = card(32, 20)
        self.wizard_title = label('', 'h1'); self.wizard_subtitle = label('', 'small'); cl.addWidget(self.wizard_title); cl.addWidget(self.wizard_subtitle)
        self.wizard_pages = QStackedWidget(); cl.addWidget(self.wizard_pages, 1)
        self.build_services(); self.build_recording(); self.build_validation(); self.build_library(); self.build_acceptance(); self.build_complete()
        self.back_button = self.action('上一步', self.wizard_back, guarded=False)
        self.next_button = self.action('下一步', self.wizard_primary, 'primary', guarded=False)
        self.acceptance_exit = self.action('保存并退出', lambda: self.navigate('home'), guarded=False); self.acceptance_exit.hide()
        cl.addLayout(horizontal(self.back_button, self.acceptance_exit, 1, self.next_button)); lay.addWidget(container, 1)
        self.register_page('wizard', w); self.set_step(0, persist=False)

    def wizard_body(self):
        w = QWidget(); lay = QVBoxLayout(w); lay.setContentsMargins(0, 8, 8, 16); lay.setSpacing(16)
        self.wizard_pages.addWidget(scroll(w)); return lay

    def build_services(self):
        lay = self.wizard_body()
        lay.addWidget(note('两项服务，各司其职', 'Jev 从你的话术库中选择固定回复；MiniMax 识别聊天截图。密钥只保存在当前用户目录，不进入日志。'))
        jev = self.field(lay, 'Jev API Key', secret=True); self.api_jev_var = Value(jev)
        vision = self.field(lay, 'MiniMax API Key', secret=True); self.api_vision_var = Value(vision)
        lay.addWidget(label('MiniMax 服务区域', 'small')); region = QComboBox(); region.addItems(['中国站', '国际站']); lay.addWidget(region); self.api_region_var = Value(region)
        self.consent = QCheckBox('我同意将聊天截图发送给 MiniMax、识字文字发送给 Jev，用于客服处理。'); self.consent.setChecked(False); lay.addWidget(self.consent); self.consent_var = Value(self.consent)
        lay.addLayout(horizontal(self.action('保存设置', self.app.setup_save_only), self.action('测试 Jev 连接', self.app.setup_test_jev)))
        lay.addWidget(self.action('获取 MiniMax 密钥', self.app.open_minimax_key_page, guarded=False))
        self.service_result = label('', 'small'); lay.addWidget(self.service_result); lay.addStretch()

    def build_recording(self):
        lay = self.wizard_body()
        lay.addWidget(label('选择已有平台，或为新平台创建独立配置。', 'small'))
        self.profile_choice = QComboBox(); lay.addWidget(self.profile_choice)
        lay.addWidget(self.action('切换到所选平台', self.app.setup_select_profile))
        self.new_label = self.field(lay, '新平台名称', '闲鱼网页版'); self.new_account = self.field(lay, '店铺 / 账号标识')
        self.new_kind = QComboBox(); self.new_kind.addItem('网页版', 'web'); self.new_kind.addItem('Windows 客户端', 'windows'); lay.addWidget(self.new_kind)
        lay.addWidget(self.action('创建并选中', self.app.setup_add_profile))
        self.recorder_hint = label('', 'small'); lay.addWidget(self.recorder_hint)
        lay.addWidget(note('一次性录制 5 个位置', '① 带红点的完整客户行\n② 当前买家名（不要固定某人的昵称）\n③ 显示消息的完整聊天区\n④ 回复输入框\n⑤ 发送按钮\n闲鱼＋Edge：正常录制并保存，回到程序自动整理未读规则，无需手改属性。'))
        lay.addLayout(horizontal(self.action('去录制', self.app.open_recorder, 'primary'), self.action('查看图文指导', self.recording_help, guarded=False)))
        lay.addWidget(self.action('录制完成，自动整理并检查', self.app.doctor))
        lay.addWidget(label('保存后接入录制库；只有一个完整且未被其他平台占用的库时会自动接入。多个库需选择。接入后仍须验证真实页面；没有教学视频时使用图文指引。', 'caption')); lay.addStretch()

    def recording_help(self):
        import webbrowser
        record('help.recorder.opened')
        webbrowser.open('https://clicknium.com/documents/clicknium-recorder/capture/')
        self.text_dialog('录制图文指引', '在打开的 VS Code 里选择 Clicknium → LOCATORS → Capture。\n' + self.recorder_hint.text() +
            '\n录制并依次命名 new_message、current_user、chat_window、reply_text、button_send。\n买家名用通用元素定位，去掉固定昵称；聊天区应包含消息正文、身份和底部。\n闲鱼＋Edge：new_message 保存后无需手改属性，也不要求先通过 Validate；回程序点击“录制完成，自动整理并检查”。\n不要为通过检查操作不愿用于测试的真实客户。')

    def build_validation(self):
        lay = self.wizard_body(); lay.addWidget(note('只检查，不回复', '保持聊天页打开。检查不会点客户或发送。未读验证需要一位你能控制的测试买家。'))
        lay.addWidget(self.action('检查 5 个位置', lambda: self.app.doctor()))
        self.unread_mode = QComboBox(); self.unread_mode.addItem('录制的未读会话行', 'locator'); self.unread_mode.addItem('闲鱼左侧列表红点（仅预览）', 'red_badge')
        lay.addWidget(label('以下是识别方式选项，不是检查结果；录制整条未读客户行请选择第一项。', 'caption'))
        lay.addWidget(self.unread_mode); lay.addWidget(self.action('保存识别方式', self.app.setup_save_unread_mode))
        for stage, text, description in [('empty', '① 记录当前未读数量', '闲鱼＋Edge 可保留其他未读，以当前数量作基线；其他手动定位平台仍需无未读。'),
            ('unread', '② 收到未读后检查', '选择一位原本已读的测试买家，让其发一条新消息，先不要打开。'), ('read', '③ 手动读完后检查', '手动读完测试消息，确认红点消失、数量回到基线后检查。')]:
            lay.addWidget(label(description, 'small')); lay.addWidget(self.action(text, lambda s=stage: self.app.doctor(s)))
        self.validation_result = QPlainTextEdit(); self.validation_result.setReadOnly(True); self.validation_result.setMinimumHeight(160); lay.addWidget(self.validation_result)
        lay.addWidget(self.action('保存已通过的界面配置', self.app.confirm_locators, 'primary')); lay.addStretch()

    def build_library(self):
        lay = self.wizard_body()
        lay.addWidget(note('你的话术，你来决定', '支持 Excel 第一张工作表或 CSV。沿用现有 7 列；检查后先预览，确认启用才切换版本。不会自动改为已批准。'))
        lay.addLayout(horizontal(self.action('导入 Excel / CSV', self.app.import_replies, 'primary'), self.action('下载模板', self.app.template, guarded=False)))
        self.library_current = label('', 'small'); lay.addWidget(self.library_current)
        self.library_table = table(['上传时间', '文件', '条数', '可自动发送', '版本状态']); self.library_table.setMinimumHeight(210); lay.addWidget(self.library_table, 1)
        self.library_table.cellDoubleClicked.connect(lambda r, c: self.preview_version(r))
        lay.addLayout(horizontal(self.action('预览 / 恢复所选版本', lambda: self.preview_version(self.library_table.currentRow())), self.action('刷新版本历史', lambda: self.app.command('library_list'), guarded=False)))
        lay.addWidget(label('双击版本查看正文、适用条件和审批标记。修改审核结果会生成新版本，历史不会覆盖。', 'caption'))
        lay.addLayout(horizontal(self.action('导出当前话术供编辑', self.app.export_replies), self.action('查看分类说明', self.show_categories, guarded=False)))

    def show_categories(self):
        path = knowledge_file(self.app.cfg['knowledge']['categories_json'])
        self.text_dialog('当前分类说明', path.read_text(encoding='utf-8-sig'))

    def build_acceptance(self):
        from .acceptance_view import AcceptancePanel
        self.acceptance_panel = AcceptancePanel(self)
        self.wizard_pages.addWidget(self.acceptance_panel)
        self.vision_state = self.acceptance_panel.vision_state
        self.preview_result = self.acceptance_panel.preview_result
        self.send_reason = self.acceptance_panel.send_reason
        self.acceptance_state = self.acceptance_panel.acceptance_state
        # Compatibility controls used by the existing diagnostics/tests, not duplicate visible actions.
        self.vision_confirm = self.action('截图与识字正确，确认验收', self.app.confirm_vision); self.vision_confirm.setParent(self.acceptance_panel); self.vision_confirm.hide()
        self.test_send_button = self.action('人工确认测试发送一次', self.app.wizard_send, 'primary'); self.test_send_button.setParent(self.acceptance_panel); self.test_send_button.hide()

    def wizard_primary(self):
        if self.wizard_pages.currentIndex() == 4: self.app.acceptance_primary()
        else: self.next_step()

    def wizard_back(self):
        if self.wizard_pages.currentIndex() == 4 and self.app.acceptance.get('step', 0) > 0:
            self.app.acceptance_action('page', step=self.app.acceptance['step']-1)
        else: self.set_step(self.wizard_pages.currentIndex()-1)

    def build_complete(self):
        lay = self.wizard_body(); lay.addWidget(icon_label('check', 40)); lay.addWidget(label('准备就绪，再由你开始。', 'h2'))
        self.complete_state = label('', 'small'); lay.addWidget(self.complete_state)
        self.allow_button = self.action('允许自动发送已批准话术', self.app.enable_auto, 'primary'); lay.addWidget(self.allow_button)
        lay.addWidget(note('首页不会自动接待', '保存权限后进入首页，点击“开始接待”并确认才开始。保留停止、发送核对和客户暂停保护。')); lay.addStretch()

    def set_step(self, index, persist=True):
        index = max(0, min(5, index)); self.wizard_pages.setCurrentIndex(index)
        self.wizard_title.setText(STEPS[index]); self.wizard_subtitle.setText(f'第 {index+1} / 6 步 · 只完成这一步所需的操作')
        self.back_button.setEnabled(index > 0)
        self.next_button.setText('进入接待首页' if index == 5 else '下一步')
        self.next_button.setEnabled(True)
        self.acceptance_exit.setVisible(index == 4)
        for i, b in enumerate(self.step_buttons):
            b.setProperty('active', i == index); b.style().unpolish(b); b.style().polish(b)
        if persist: self.app.ui_state.update(wizard_step=index)
        if index == 3 and self.initialized: self.app.command('library_list')
        if index == 4:
            self.feedback.hide()
            if self.initialized and not self.app.busy and not self.app.acceptance: self.app.acceptance_action('load')
            self.acceptance_panel.refresh()

    def next_step(self):
        index = self.wizard_pages.currentIndex(); r = self.app.readiness()
        required = [('jev',), ('platform',), ('locators', 'unread'), ('knowledge',), ('vision', 'test_send', 'consent', 'conversation_identity'), ()][index]
        from .controller import CHECK_NAMES
        missing = [CHECK_NAMES[k] for k in required if not r[k]]
        if index == 0 and not self.app.cfg['vision']['api_key'].strip(): missing.append('MiniMax Key')
        if missing: self.app.notify('这一步还需完成：' + '、'.join(missing), 'warning'); return
        if index == 5: self.navigate('home')
        else: self.set_step(index + 1)

    def build_history(self):
        w, lay = self.page('每一步，都可回看。', '历史 / HISTORY')
        filters = QHBoxLayout(); filters.setSpacing(12)
        self.customer_filter = QLineEdit(); self.customer_filter.setPlaceholderText('搜索客户昵称'); self.customer_filter.setMinimumWidth(0)
        self.time_filter = QComboBox(); self.time_filter.addItems(['全部时间', '今天', '近 7 天', '近 30 天'])
        self.result_filter = QComboBox(); self.result_filter.addItem('全部结果', '')
        filters.addWidget(self.customer_filter, 1); filters.addWidget(self.time_filter); filters.addWidget(self.result_filter)
        filters.addWidget(self.action('查询 / 刷新', lambda: self.refresh_history(reset=True), guarded=False)); lay.addLayout(filters)
        panel, pl = card(24)
        self.history_tabs = QTabWidget(); self.history_tabs.addTab(QWidget(), '接待记录'); self.history_tabs.addTab(QWidget(), '识字与截图'); self.history_tabs.addTab(QWidget(), '完整操作追踪')
        self.history_tabs.setMaximumHeight(52); pl.addWidget(self.history_tabs)
        self.history_table = table(['时间', '客户', '问题 / 操作', '结果']); pl.addWidget(self.history_table, 1)
        self.history_table.cellDoubleClicked.connect(lambda r, c: self.history_detail(r))
        self.history_previous = self.action('上一页', lambda: self.turn_history(-1), guarded=False)
        self.history_next = self.action('下一页', lambda: self.turn_history(1), guarded=False)
        self.history_caption = label('', 'caption')
        pl.addLayout(horizontal(self.history_previous, self.history_caption, 1, self.history_next))
        pl.addLayout(horizontal(self.action('查看所选记录', lambda: self.history_detail(self.history_table.currentRow()), guarded=False),
            self.action('导出脱敏诊断包', self.app.export_trace, guarded=False)))
        lay.addWidget(panel, 1); self.register_page('history', w)
        self.history_tabs.currentChanged.connect(lambda i: self.history_kind_changed())
        self.history_kind_changed()

    def history_kind_changed(self):
        self.result_filter.clear(); self.result_filter.addItem('全部结果', '')
        states = [('sent_ui_verified', '界面已确认'), ('human', '需人工'), ('preview', '候选未发'), ('send_unknown', '发送未知'), ('send_aborted_before_click', '点击前中止'), ('rejected', '已放弃')]
        if self.history_tabs.currentIndex() == 1:
            states = [('extracted', '识字成功'), ('previewed', '已选话术'), ('vision_failed', '识字失败'), ('captured', '已截图')]
        if self.history_tabs.currentIndex() == 2:
            states = [('success', '成功'), ('failed', '失败'), ('send_unknown', '发送未知'), ('cancelled', '取消'), ('skipped', '跳过')]
        for key, name in states: self.result_filter.addItem(name, key)
        if self.initialized: self.refresh_history(reset=True)

    def history_filters(self):
        result = {'customer': self.customer_filter.text().strip()}
        hours = {1: 'today', 2: 7*24, 3: 30*24}.get(self.time_filter.currentIndex())
        if hours == 'today':
            now = time.localtime(); result['from'] = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
        elif hours: result['from'] = time.time() - hours * 3600
        status = self.result_filter.currentData()
        if status: result['result' if self.history_tabs.currentIndex() == 2 else 'status'] = status
        return result

    def refresh_history(self, reset=False):
        if reset: self.history_page = 0
        target = ['events', 'vision', 'trace'][self.history_tabs.currentIndex()]
        self.app.query(target, self.history_filters(), self.history_page)

    def turn_history(self, direction):
        self.history_page = max(0, self.history_page + direction); self.refresh_history()

    def show_query(self, item):
        slot = item['request'].get('slot', item['target'])
        if slot == 'home': self.render_timeline(item['rows']); return
        if slot == 'blocked': self.render_pending(item['rows']); return
        current = ['events', 'vision', 'trace'][self.history_tabs.currentIndex()]
        if slot != current or item['request'].get('page', 0) != self.history_page: return
        rows = item['rows']
        if slot == 'trace':
            grouped = {}
            for row in rows:
                key = row.get('operation') or str(row['seq'])
                grouped.setdefault(key, []).append(row)
            rows = [dict(group[0], group=group) for group in grouped.values()]
        self.history_rows = rows; self.history_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            if slot == 'events': values = [stamp(r['created'], True), nickname(r['customer']), r.get('question') or '查看识字或操作证据', STATUS.get(r['status'], r['status'])]
            elif slot == 'vision': values = [stamp(r['created'], True), nickname(r['customer']), r['source'], STATUS.get(r['status'], r['status'])]
            else: values = [stamp(r['created'], True), nickname(r.get('customer')), r['stage'], STATUS.get(r['result'], r['result'])]
            self.fill_row(self.history_table, i, values)
        self.history_previous.setEnabled(self.history_page > 0); self.history_next.setEnabled(item.get('has_more', False))
        self.history_caption.setText(f'第 {self.history_page+1} 页 · {len(rows)} ' + ('组操作（每页最多100个步骤）' if slot == 'trace' else '条记录') + (' · 暂无符合条件的记录' if not rows else ''))

    def history_detail(self, index):
        if index < 0 or index >= len(self.history_rows): self.app.notify('请先选择一条记录。'); return
        row = self.history_rows[index]; target = self.history_tabs.currentIndex()
        if target == 0: self.event_details(row)
        elif target == 1: self.app.command('vision_audit', row['id'])
        else:
            dialog, layout = self.dialog('操作追踪与故障证据', 1080, 740)
            filters = {'operation': row.get('operation')}
            steps = get_audit().rows(filters) if row.get('operation') else row['group']
            tabs = QTabWidget(); layout.addWidget(tabs, 1)
            details = QPlainTextEdit(text_json(steps)); details.setReadOnly(True); tabs.addTab(details, '有序步骤与输入结果')
            proofs = QWidget(); proof_layout = QVBoxLayout(proofs)
            for step in steps:
                for attachment in get_audit().attachments(step['seq']):
                    proof_layout.addWidget(label(stamp(step['created'], True) + ' · ' + step['stage'], 'small'))
                    proof_layout.addWidget(self.picture(attachment['data'], 950))
            proof_layout.addStretch(); tabs.addTab(scroll(proofs), '截图与差异证据')
            layout.addWidget(label('点击意图、点击调用返回、界面气泡确认分别记录。缺失的历史证据无法补造。', 'caption'))
            self.open_dialog(dialog)

    @staticmethod
    def fill_row(widget, row, values):
        for col, value in enumerate(values):
            item = QTableWidgetItem(str(value)); item.setToolTip(str(value)); widget.setItem(row, col, item)
        widget.setRowHeight(row, 60)

    def event_details(self, row):
        d = row.get('decision', {}); dialog, lay = self.dialog('接待记录 · ' + nickname(row.get('customer')), 820, 660)
        content = QWidget(); content_lay = QVBoxLayout(content); content_lay.setSpacing(16)
        content_lay.addWidget(badge(STATUS.get(row.get('status'), row.get('status', '预览')), 'info'))
        content_lay.addWidget(label(row.get('question') or '原问题请查看对应识字记录', 'h3'))
        content_lay.addWidget(note('固定回复', d.get('text') or d.get('reason') or '没有候选回复'))
        content_lay.addWidget(label('分类：' + ' → '.join(d.get('path', [])), 'small'))
        for index, step in enumerate(d.get('trace', [])):
            choice = step.get('choice')
            selected = '转人工' if choice == 'HUMAN' else '复核通过' if choice == 'SEND' else (
                d['path'][index] if index < len(d.get('path', [])) else '选择本条固定回复')
            content_lay.addWidget(label(str(step.get('node', '')) + '  →  ' + selected, 'h3'))
            probability = step.get('probabilities') or {}
            ratios = [('所选选项', probability.get(choice)), ('转人工选项', probability.get('HUMAN'))]
            content_lay.addWidget(label('　'.join(f'{key}：{value:.0%}' for key, value in ratios if isinstance(value, (int, float))), 'small'))
        content_lay.addWidget(label('模型选择比例是本次决策记录，不代表回复正确率。', 'caption'))
        raw = QPlainTextEdit(text_json(d)); raw.setReadOnly(True); raw.setMinimumHeight(180)
        content_lay.addWidget(raw); content_lay.addStretch(); lay.addWidget(scroll(content), 1)
        self.open_dialog(dialog)

    def show_decision(self, item):
        d = item['decision']; content = ('客户：' + nickname(item.get('customer')) + '\n来源：' + item.get('source', '') +
            '\n结果：' + ('候选回复，尚未发送' if d['action'] == 'candidate' else '人工处理') + '\n分类：' + ' → '.join(d.get('path', [])) +
            '\n固定回复：' + d.get('text', '') + '\n说明：' + d.get('reason', '') + '\n\n识字 / 输入：\n' + text_json(item.get('input')))
        self.preview_result.setPlainText(content); self.tool_result.setPlainText(content)

    def show_result_text(self, text):
        self.preview_result.setPlainText(text); self.tool_result.setPlainText(text)

    def show_doctor(self, result, stage):
        names = dict(current_user='买家名称', chat_window='聊天记录', reply_text='输入框', button_send='发送按钮', new_message='未读列表')
        lines = [stamp(time.time(), True) + ' · 本次检查完成'] + [names.get(k, k) + '：' + str(v) for k, v in result.items()]
        lines.append('本轮未读会话数：' + ' / '.join(f'{n}={self.app.unread_samples.get(k, "未检查")}' for k, n in [('empty', '基线'), ('unread', '收到新消息'), ('read', '手动读完')]))
        self.validation_result.setPlainText('\n'.join(lines))

    def dialog(self, title, width=900, height=650):
        screen = self.screen().availableGeometry()
        width = min(width, self.width()-48, screen.width()-48)
        height = min(height, self.height()-48, screen.height()-48)
        dialog = QDialog(self); dialog.setWindowTitle(title); dialog.resize(width, height); dialog.setAttribute(Qt.WA_DeleteOnClose)
        outer = QVBoxLayout(dialog); outer.setContentsMargins(24, 24, 24, 24); outer.setSpacing(16)
        stop = self.action('停止任务', self.app.stop, 'danger', guarded=False); stop.setObjectName('dialogStop')
        header = QHBoxLayout(); header.addWidget(label(title, 'h2'), 1); header.addWidget(stop)
        outer.addLayout(header)
        body = QWidget(); body.setObjectName('dialogBody')
        layout = QVBoxLayout(body); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(16)
        outer.addWidget(scroll(body), 1)
        return dialog, layout

    def open_dialog(self, dialog):
        self.app.dialogs.append(dialog)
        dialog.destroyed.connect(lambda: self.app.dialogs.remove(dialog) if dialog in self.app.dialogs else None)
        dialog.show()

    def text_dialog(self, title, text):
        dialog, lay = self.dialog(title); editor = QPlainTextEdit(str(clean(text))); editor.setReadOnly(True); lay.addWidget(editor, 1)
        self.open_dialog(dialog)

    def picture(self, data, width=850):
        image = label('截图未保存，无法还原'); pixmap = QPixmap()
        if data and pixmap.loadFromData(bytes(data)):
            image.setText(''); image.setPixmap(pixmap.scaledToWidth(min(width, pixmap.width()), Qt.SmoothTransformation))
        return image

    def show_vision(self, row):
        if not row: self.app.notify('这条识字记录不存在。', 'warning'); return
        dialog, lay = self.dialog('实际截图与模型返回 · ' + nickname(row['customer']), 1100, 780)
        tabs = QTabWidget(); lay.addWidget(tabs, 1)
        tabs.addTab(scroll(self.picture(row.get('image_png'), 1010)), '实际上传截图')
        for name, field in [('MiniMax 原始返回', 'response_json'), ('解析后的识字', 'transcript_json'), ('Jev 决策', 'decision_json')]:
            raw = row.get(field)
            try: raw = text_json(json.loads(raw)) if raw else '本次未产生该项记录。'
            except (ValueError, TypeError): raw = str(clean(raw))
            editor = QPlainTextEdit(raw); editor.setReadOnly(True); tabs.addTab(editor, name)
        lay.addWidget(label(stamp(row['created'], True) + ' · ' + row['source'] + ' · ' + row['status'] + ('\n' + row['error'] if row.get('error') else ''), 'small'))
        self.open_dialog(dialog)

    def show_pending_dialog(self):
        dialog, lay = self.dialog('待人工处理客户', 800, 640)
        content = QWidget(); cl = QVBoxLayout(content)
        if not self.blocked_rows: cl.addWidget(label('暂时没有待人工处理客户。'))
        for row in self.blocked_rows:
            cl.addWidget(self.action(nickname(row['customer']) + ' · 查看与处理', lambda r=row: self.pending_details(r), guarded=False))
            cl.addWidget(label(row['reason'], 'small'))
        cl.addStretch(); lay.addWidget(scroll(content)); self.open_dialog(dialog)

    def pending_details(self, row):
        dialog, lay = self.dialog('人工待办 · ' + nickname(row['customer']), 760, 580)
        lay.addWidget(note('可能已经发送，请先核查聊天页面' if row['possibly_sent'] else '该客户已暂停自动接待', row['reason'], 'warning'))
        if row.get('last_stage'): lay.addWidget(label('最后发送证据：' + text_json(row['last_stage']), 'small'))
        lay.addWidget(self.action('标记人工已处理（仍保持暂停）', lambda: self.app.manual_done(row['customer'])))
        lay.addWidget(self.action('恢复后续接待（不补发旧回复）', lambda: self.app.resume(row['customer']), 'primary'))
        lay.addWidget(self.action('重新预览当前会话 · 不发送', lambda: self.app.command('current')))
        lay.addWidget(label('重新预览读取的是浏览器当前会话，请先手动打开这位客户；不会替你切换客户。', 'caption')); lay.addStretch()
        self.open_dialog(dialog)

    def show_versions(self, rows):
        self.version_rows = rows; self.library_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            active = row.get('active') or self.app.cfg['knowledge']['replies_csv'] == row['replies_path']
            self.fill_row(self.library_table, i, [stamp(row['created'], True), row['filename'], row['count'], row['approved'], '当前使用' if active else '可预览 / 恢复'])

    def preview_version(self, row):
        if row < 0 or row >= len(self.version_rows): self.app.notify('请先选择一个版本。'); return
        self.app.command('library_preview', self.version_rows[row]['id'])

    def show_library_preview(self, version, rows):
        dialog, lay = self.dialog('审核话术 · ' + version['filename'], 1160, 720)
        lay.addWidget(label(f'{len(rows)} 条话术 · 请逐条核对。启用前不会替换当前话术库。', 'small'))
        grid = table(['分类 / 编号', '适用条件', '排除条件', '固定回复正文', '允许自动发送', '必须人工'])
        grid.setRowCount(len(rows)); lay.addWidget(grid, 1)
        for i, r in enumerate(rows):
            self.fill_row(grid, i, [r['category_path'] + '\n' + r['reply_id'], r['when_to_use'], r['when_not_to_use'], r['reply_text'], '', ''])
            for j, name in [(4, 'approved_for_auto'), (5, 'requires_human')]:
                check = QCheckBox('是' if r[name] else '否'); check.setChecked(r[name]); check.setProperty('field', name)
                check.toggled.connect(lambda checked, c=check: c.setText('是' if checked else '否')); grid.setCellWidget(i, j, check)
            grid.setRowHeight(i, 112)
        def detail(row):
            if row < 0 or row >= len(rows):
                self.app.notify('请先选择一条话术。'); return
            reply = rows[row]
            self.text_dialog('话术全文 · ' + reply['category_path'],
                '适用条件\n' + reply['when_to_use'] + '\n\n排除条件\n' + (reply['when_not_to_use'] or '无') +
                '\n\n固定回复原文\n' + reply['reply_text'])
        grid.cellDoubleClicked.connect(lambda row, column: detail(row))
        lay.addWidget(self.action('查看所选话术全文', lambda: detail(grid.currentRow()), guarded=False))
        confirm = QCheckBox('我已审核本版本正文、适用条件和自动发送审批。'); lay.addWidget(confirm)
        enable = button('确认启用此版本', role='primary'); enable.setEnabled(False); confirm.toggled.connect(enable.setEnabled)
        def activate():
            edited = copy.deepcopy(rows)
            for i, row in enumerate(edited):
                for j, name in [(4, 'approved_for_auto'), (5, 'requires_human')]: row[name] = grid.cellWidget(i, j).isChecked()
            id_ = version['id']
            if edited != rows:
                # Approval edits become a new immutable version; the original stays intact.
                library = ReplyLibrary()
                try:
                    output = io.StringIO(newline=''); writer = csv.DictWriter(output, Knowledge.FIELDS); writer.writeheader()
                    for row in edited:
                        writer.writerow({k: ('true' if v else 'false') if type(v) is bool else v for k, v in row.items()})
                    id_ = library.store_review(output.getvalue().encode('utf-8-sig'), version)['id']
                finally: library.close()
            self.app.activate_version(id_); dialog.close()
        enable.clicked.connect(lambda: self.app.call(activate)); lay.addWidget(enable); self.open_dialog(dialog)

    def build_costs(self):
        w, lay = self.page('成本有据可查。', '费用 / COSTS', '服务实际返回的用量 × 对应单价。参考金额不是账户账单；订阅额度与按量费用分别呈现。')
        self.cost_summary = label('尚未读取实际费用记录。', 'small'); lay.addWidget(self.cost_summary)
        p, pl = card(24); pl.addLayout(horizontal(label('每次 API 调用', 'h2'), 1, self.action('刷新', lambda: self.app.command('costs', self.cost_page), guarded=False)))
        self.cost_table = table(['时间', '服务 / 模型', '结果', '输入 / 输出 tokens', '估算金额']); pl.addWidget(self.cost_table, 1)
        self.cost_table.cellDoubleClicked.connect(lambda r, c: self.cost_detail(r))
        self.cost_previous = self.action('上一页', lambda: self.turn_cost(-1), guarded=False); self.cost_next = self.action('下一页', lambda: self.turn_cost(1), guarded=False)
        pl.addLayout(horizontal(self.cost_previous, 1, self.cost_next)); pl.addLayout(horizontal(self.action('查看计价明细', lambda: self.cost_detail(self.cost_table.currentRow()), guarded=False),
            self.action('单价与计费方式', self.app.edit_cost_rates, guarded=False)))
        lay.addWidget(p, 1); self.register_page('cost', w)

    def turn_cost(self, direction):
        self.cost_page = max(0, self.cost_page + direction); self.app.command('costs', self.cost_page)

    def show_costs(self, item):
        self.cost_page = item['page']; self.cost_rows = item['rows']
        lines = []
        for r in item['summary']:
            kind = dict(subscription='订阅额度参考', payg='按量估算', unknown='计费方式未知')[r['billing']]
            amount = '未知' if r['estimated'] is None else f"{r['estimated']:.6f} {r['currency']}"
            lines.append(f"{r['provider']} · {kind} · {r['attempts']} 次尝试 · {amount} · {r['attempts']-r['priced']} 次费用未知")
        self.cost_summary.setText('\n'.join(lines) or '暂无 API 调用记录。未知费用不会计为零。')
        self.cost_table.setRowCount(len(self.cost_rows))
        for i, r in enumerate(self.cost_rows):
            amount = str(r['estimated']) + ' ' + r['currency'] if r['estimated'] is not None else '未知'
            inp, out = r['input_tokens'], r['output_tokens']
            self.fill_row(self.cost_table, i, [stamp(r['created'], True), r['provider'] + '\n' + r['model'], STATUS.get(r['status'], r['status']),
                f"{inp if inp is not None else '未知'} / {out if out is not None else '未知'}", amount])
        self.cost_previous.setEnabled(self.cost_page > 0); self.cost_next.setEnabled(item['has_more'])

    def cost_detail(self, row):
        if 0 <= row < len(self.cost_rows): self.text_dialog('API 用量与计价依据', text_json(self.cost_rows[row]))
        else: self.app.notify('请先选择一次 API 调用。')

    def show_rates(self, rates):
        dialog, lay = self.dialog('单价与计费方式', 780, 700)
        lay.addWidget(label('每百万 tokens 单价。留空代表未知。修改只用于之后的调用，旧记录仍保留当时的价格。', 'small'))
        provider = QComboBox(); provider.addItems(['minimax', 'jev']); lay.addWidget(provider)
        editors = {key: self.field(lay, title) for key, title in [('input', '输入单价'), ('output', '输出单价'), ('cached', '缓存输入单价')]}
        currency = QComboBox(); currency.addItems(['CNY', 'USD']); lay.addWidget(currency)
        billing = QComboBox(); billing.addItem('按量计费', 'payg'); billing.addItem('订阅额度参考', 'subscription'); billing.addItem('未知', 'unknown'); lay.addWidget(billing)
        source = self.field(lay, '价格来源 / 账户约定')
        def load():
            r = rates.get(provider.currentText(), {})
            for k, e in editors.items(): e.setText('' if r.get(k) is None else str(r[k]))
            currency.setCurrentText(r.get('currency', 'USD')); billing.setCurrentIndex(billing.findData(r.get('billing', 'unknown'))); source.setText(r.get('source', ''))
        def save():
            r = validate_rates({**{k: e.text() for k, e in editors.items()}, 'currency': currency.currentText(), 'billing': billing.currentData(), 'source': source.text()})
            self.app.command('rates_save', {'provider': provider.currentText(), 'rates': r}); dialog.close()
        provider.currentIndexChanged.connect(lambda i: load()); load()
        lay.addWidget(self.action('保存之后调用的价格', save, 'primary')); self.open_dialog(dialog)

    def build_settings(self):
        w, lay = self.page('让工具适合你的生意。', '设置 / SETTINGS')
        body = QWidget(); bl = QVBoxLayout(body); bl.setContentsMargins(0, 0, 0, 0); bl.setSpacing(24)
        p, pl = card(24); pl.addWidget(label('基础配置', 'h2')); pl.addWidget(label('服务、平台、录制、话术、验收分别保存，不重复配置已通过的步骤。', 'small'))
        for i, title in [(0, '服务密钥'), (1, '平台与录制'), (2, '界面验证'), (3, '话术版本'), (4, '识字与发送验收')]:
            pl.addWidget(self.action(title + ' →', lambda n=i: (self.set_step(n), self.navigate('wizard')), guarded=False))
        self.motion_check = QCheckBox('减少界面动效'); self.motion_check.toggled.connect(self.set_motion); pl.addWidget(self.motion_check); bl.addWidget(p)
        pl.addWidget(self.action('简短操作指引', lambda: self.app.open_file(SOURCE_ROOT / 'FIRST_RUN.md'), guarded=False))
        p, pl = card(24); pl.addWidget(label('测试与诊断工具', 'h2')); pl.addWidget(label('测试结果与真实发送权限分别呈现。临时未验收监测只生成不能发送的预览。', 'small'))
        self.tool_input = QPlainTextEdit(); self.tool_input.setPlaceholderText('输入客户问题，测试 Jev 选择固定话术'); self.tool_input.setMaximumHeight(100); pl.addWidget(self.tool_input)
        pl.addLayout(horizontal(self.action('文字选话术', lambda: self.app.command('text', self.tool_input.toPlainText())), self.action('图片选话术 · 不发送', self.app.choose_image)))
        pl.addWidget(self.action('读取当前会话并预览 · 不发送', lambda: self.app.command('current')))
        pl.addLayout(horizontal(self.action('开始监测预览', lambda: self.app.command('monitor', False)), self.action('临时跳过未读验收 · 禁止发送', self.app.start_unverified_preview)))
        self.tool_result = QPlainTextEdit(); self.tool_result.setReadOnly(True); self.tool_result.setMinimumHeight(180); pl.addWidget(self.tool_result)
        pl.addLayout(horizontal(self.action('确认发送候选一次', self.app.approve), self.action('放弃候选', self.app.reject)))
        pl.addWidget(self.action('离线演示（模拟结果）', lambda: self.app.command('demo')))
        pl.addWidget(self.action('查看完整操作追踪', self.app.show_trace_history, guarded=False))
        self.live_log = QPlainTextEdit(); self.live_log.setReadOnly(True); self.live_log.setMinimumHeight(170); pl.addWidget(self.live_log); bl.addWidget(p)
        p, pl = card(24); pl.addWidget(label('高级连接与运行参数', 'h2'))
        for group, key, title, secret in [('jev', 'endpoint', 'Jev API 地址', False), ('jev', 'model', 'Jev 模型', False),
            ('desktop', 'license_key', 'Clicknium 授权 Key（如需）', True), ('desktop', 'poll_seconds', '轮询间隔（秒）', False),
            ('desktop', 'settle_seconds', '打开会话后的等待（秒）', False), ('desktop', 'max_sends_per_minute', '每分钟最多发送', False),
            ('vision', 'buyer_side', '买家气泡方向（left / right）', False), ('network', 'read_timeout', 'API 响应超时（秒）', False),
            ('network', 'attempts', '网络尝试次数', False)]:
            self.entries[(group, key)] = Value(self.field(pl, title, self.app.cfg[group][key], secret=secret))
        pl.addWidget(self.action('保存高级设置', self.app.save, 'primary')); pl.addWidget(self.action('打开用户数据目录', lambda: self.app.open_file(DATA_ROOT), guarded=False)); bl.addWidget(p)
        bl.addStretch(); lay.addWidget(scroll(body), 1); self.register_page('settings', w)

    def show_live_log(self, items):
        self.live_log.setPlainText('\n'.join(stamp(at, True) + ' ' + text for at, text in items))

    def set_motion(self, checked):
        self.app.ui_state.update(reduced_motion=checked)
        record('ui.motion.preference', details={'reduced_motion': checked})

    def update_fields(self, cfg):
        self.api_jev_var.set(cfg['jev']['api_key']); self.api_vision_var.set(cfg['vision']['api_key'])
        self.api_region_var.set('国际站' if cfg['vision']['endpoint'].endswith('.io/v1/chat/completions') else '中国站')
        self.consent_var.set(cfg['desktop']['data_upload_authorized'])
        for (group, key), value in self.entries.items(): value.set(cfg[group][key])
        self.profile_choice.blockSignals(True); self.profile_choice.clear()
        for key, p in cfg['profiles'].items(): self.profile_choice.addItem(p['label'] + ' / ' + p['shop_id'], key)
        self.profile_choice.setCurrentIndex(self.profile_choice.findData(cfg['active_profile'])); self.profile_choice.blockSignals(False)
        self.unread_mode.setCurrentIndex(self.unread_mode.findData(cfg['desktop']['unread_mode']))
        self.motion_check.setChecked(cfg is not None and self.app.ui_state.data.get('reduced_motion', False))

    def refresh_state(self):
        cfg = self.app.cfg; r = self.app.readiness(); p = cfg['profiles'][cfg['active_profile']]
        for b in self.guarded_buttons: b.setEnabled(not bool(self.app.busy) and not self.app.closing)
        self.profile_label.setText(p['label'] + '\n账号 ' + p['shop_id'])
        self.connection_label.setText('浏览器 · 本次检查通过' if self.app.connection_checked else '浏览器 · 尚未检查')
        self.service_result.setText(('Jev：接口已验收' if r['jev'] else 'Jev：待接口测试') + '　｜　MiniMax：' + ('识字已核对' if r['vision'] else '待图片验收'))
        paths = cfg['desktop']['locators']; store = paths['current_user'].split('.')[0]
        self.recorder_hint.setText(f'定位器目录：{DATA_ROOT / ".locator"}\nStore：{store}\n' + '\n'.join(f'{k} → {v}' for k, v in paths.items()))
        self.library_current.setText('当前使用：' + Path(cfg['knowledge']['replies_csv']).name + '\n' + ('已有可自动发送话术' if r['knowledge'] else '尚无已批准且不需人工的话术'))
        if not self.app.acceptance:
            self.vision_state.setText('视觉识字：' + ('已验收' if r['vision'] else '待测试和人工核对'))
        self.vision_confirm.setEnabled(self.app.can_confirm_vision and not self.app.busy)
        self.test_send_button.setEnabled(bool(self.app.current_id) and not self.app.busy)
        self.send_reason.setText(self.app.send_block_reason if not self.app.current_id else '候选已生成；请先核对问题与回复，再确认测试发送。')
        self.acceptance_state.setText('测试发送：' + ('界面已确认' if r['test_send'] else '尚未完成'))
        self.complete_state.setText('全部验收已完成。' if all(r.values()) else '尚需完成：' + '、'.join(self.app.missing()))
        self.allow_button.setEnabled(all(r.values()) and not cfg['desktop']['auto_send_enabled'] and not self.app.busy)
        self.allow_button.setText('已允许自动发送' if cfg['desktop']['auto_send_enabled'] else '允许自动发送已批准话术')
        allowed = can_auto_send(cfg) if all(r.values()) else False
        self.start_button.setEnabled(allowed and not self.app.busy and not self.app.worker.monitor)
        self.start_button.setToolTip('开始真实接待' if allowed else '请先完成：' + '、'.join(self.app.missing() or ['允许自动发送']))
        self.home_footer.setText('配置已就绪，可手动开始。' if allowed else '尚需完成配置。前往设置 → 验收接待流程，查看缺失项目。')
        self.acceptance_panel.refresh()
        self.acceptance_exit.setEnabled(not self.app.busy and not self.app.closing)

    def refresh_running(self):
        self.top_stop.setEnabled(not self.app.closed)
        running = self.app.worker.monitor and not self.app.worker.stop_event.is_set()
        self.runtime_badge.setText('监测中' if running else '未运行')
        if not running and not self.app.busy: self.runtime_label.setText('尚未启动接待 · ' + self.app.cfg['profiles'][self.app.cfg['active_profile']]['label'])
        if self.start_button.isEnabled() == running: self.refresh_state()

    @staticmethod
    def command_name(name):
        return dict(configure='加载配置', current='读取当前会话', doctor='验证聊天窗口', monitor='开始监测',
            monitor_unverified='临时不发送监测', vision_test='视觉识字测试', text='文字选话术', image='图片选话术',
            send='发送并核对', library_import='导入并检查话术', library_preview='读取话术版本', jev_check='测试 Jev 连接').get(name, name)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, 'pending_panel'): return
        self.adapt_layout()

    def adapt_layout(self):
        narrow = self.width() < 1200; self.sidebar.setFixedWidth(176 if narrow else 208)
        self.rail.setFixedWidth(208 if narrow else 248 if self.width() < 1400 else 280)
        self.pending_panel.setVisible(not narrow); self.pending_compact.setVisible(narrow)
        margin = (24 if narrow else 48 if self.width() < 1400 else 80) if self.route == 'wizard' else (24 if narrow else 40)
        self.content_layout.setContentsMargins(margin, 24, margin, 16)

    def closeEvent(self, event):
        if self.app.closed: event.accept()
        else: event.ignore(); self.app.call(self.app.close)


def audit_view(fn):
    @functools.wraps(fn)
    def invoke(self, *args, **kwargs):
        token = platform_id.set(self.app.cfg['active_profile'] + ' :: ' + self.app.cfg['desktop']['shop_id'])
        try:
            with operation('ui.' + fn.__name__, parent=operation_id.get()):
                return fn(self, *args, **kwargs)
        except Exception as error:
            self.app.notify(str(clean(error)), 'error')
        finally: platform_id.reset(token)
    return invoke


for _name in ('navigate', 'set_step', 'next_step', 'recording_help', 'refresh_history', 'history_detail',
    'show_pending_dialog', 'pending_details', 'preview_version', 'cost_detail', 'set_motion'):
    setattr(Window, _name, audit_view(getattr(Window, _name)))
