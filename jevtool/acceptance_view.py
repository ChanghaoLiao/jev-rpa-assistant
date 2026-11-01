"""Three native, editable views of the approved step-five Figma design."""
from __future__ import annotations
import json
from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QWidget, QVBoxLayout, QBoxLayout, QStackedWidget, QCheckBox, QPlainTextEdit, QGraphicsOpacityEffect, QSizePolicy
from .presentation import label, button, card, horizontal, note, scroll, badge, icon_label, COLORS
from .history import nickname


class AcceptancePanel(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window; self.app = window.app; self.data = {}; self.png = None
        self.setMinimumWidth(0)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(16)
        self.progress = []
        row = horizontal(spacing=8)
        for i, title in enumerate(('识字核对', '话术核对', '测试发送')):
            b = button(f'{i+1}  {title}', lambda n=i: self.app.call(self.app.acceptance_action, 'page', step=n), 'nav')
            b.setMinimumWidth(0); row.addWidget(b, 1); self.progress.append(b)
            b.setStyleSheet('QPushButton[role="nav"]:disabled { background:transparent; border:none; color:#526078; }')
        lay.addLayout(row)
        self.pages = QStackedWidget(); lay.addWidget(self.pages, 1)
        self.build_ocr(); self.build_route(); self.build_send()
        self.error = label('', 'small'); self.error.setStyleSheet('color:' + COLORS['red']); lay.addWidget(self.error)
        self.execution = label('', 'small'); self.execution.hide(); lay.addWidget(self.execution)
        self.render({})

    def link(self, text, fn):
        b = self.window.action(text, fn, guarded=False)
        b.setStyleSheet('QPushButton { min-height:24px; padding:0 4px; border:none; color:#0145F2; font-size:14px; text-align:left; } QPushButton:focus { border:2px solid #0145F2; }')
        b.setMaximumHeight(32)
        return b

    def page(self):
        w = QWidget(); lay = QVBoxLayout(w); lay.setContentsMargins(0, 8, 8, 8); lay.setSpacing(16)
        self.pages.addWidget(scroll(w)); return lay

    def build_ocr(self):
        lay = self.page()
        self.ocr_intro = label('图片测试不操作聊天页面，也不会发送消息。', 'small'); lay.addWidget(self.ocr_intro)
        self.upload_box, ul = card(32, 16)
        self.upload_box.setStyleSheet('QFrame#card { background:#EDF1F5; border:1px solid #D5DEE8; border-radius:12px; }')
        self.upload_box.setMinimumHeight(268)
        ul.addStretch(); ul.addWidget(icon_label('upload', 28), 0, Qt.AlignHCenter)
        upload_title = label('先上传一张聊天截图', 'h3'); upload_title.setAlignment(Qt.AlignCenter); ul.addWidget(upload_title)
        upload_help = label('PNG 格式 · 建议包含清晰的消息正文与买卖家身份', 'small'); upload_help.setAlignment(Qt.AlignCenter); ul.addWidget(upload_help); ul.addStretch()
        lay.addWidget(self.upload_box)
        self.file_label = label('尚未选择聊天截图', 'small')
        self.select_button = self.link('上传聊天截图', self.app.acceptance_pick)
        self.zoom_button = self.link('放大查看', self.zoom)
        lay.addLayout(horizontal(self.file_label, 1, self.select_button, self.zoom_button, spacing=8))
        self.columns = QBoxLayout(QBoxLayout.LeftToRight); self.columns.setSpacing(24)
        self.image_card, il = card(16, 12)
        self.image_card.setStyleSheet('QFrame#card { background:#EDF1F5; border-radius:8px; }')
        self.image = label('上传一张能清楚看到买卖家消息的 PNG 聊天截图。', 'small')
        self.image.setAlignment(Qt.AlignCenter); self.image.setMinimumSize(0, 180)
        self.image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        il.addWidget(self.image); self.columns.addWidget(self.image_card, 1)
        self.result_card, rl = card(24, 12)
        self.result_card.setStyleSheet('QFrame#card { background:#EDF1F5; border-radius:8px; }')
        self.vision_state = label('等待识字', 'small'); rl.addWidget(self.vision_state)
        rl.addWidget(label('识别文字', 'h3'))
        self.transcript = label('开始识字后，按聊天顺序显示文字与买卖家身份。'); self.transcript.setMinimumWidth(0); rl.addWidget(self.transcript)
        self.raw_button = self.link('查看模型原始返回', self.raw_response); rl.addWidget(self.raw_button)
        self.retry_ocr = self.link('重新识字', lambda: self.app.acceptance_action('ocr')); rl.addWidget(self.retry_ocr)
        rl.addWidget(label('请对照左侧原图，核对文字与买卖家身份。', 'small')); rl.addStretch()
        self.columns.addWidget(self.result_card, 1); lay.addLayout(self.columns)
        self.authorization, al = card(16, 8)
        al.addWidget(label('图片上传说明', 'h3'))
        al.addWidget(label('开始识字会将此图发送给配置的 MiniMax 服务；后续话术测试会把识别文字发送给 Jev。截图与结果保存在本机，可在历史查看。', 'small'))
        self.consent = QCheckBox('我已阅读并同意上述数据使用说明'); al.addWidget(self.consent)
        self.authorize = self.window.action('同意并保存授权', lambda: self.app.acceptance_action('authorize'))
        self.consent.toggled.connect(lambda _: self.refresh()); al.addWidget(self.authorize); lay.addWidget(self.authorization)
        lay.addStretch()

    def build_route(self):
        lay = self.page(); lay.addWidget(badge('模拟测试 · 不发送', 'info'))
        lay.addWidget(label('复用已核对的识字结果，仅调用 Jev。', 'small'))
        self.question = label(''); self.question.setMinimumWidth(0)
        self.reply = label('点击测试选择话术，查看固定回复。'); self.reply.setMinimumWidth(0)
        c, cl = card(24, 12); cl.addWidget(label('客户问题', 'small')); cl.addWidget(self.question)
        c.setStyleSheet('QFrame#card { background:#EDF1F5; border-radius:8px; }')
        cl.addWidget(label('选择结果', 'small')); cl.addWidget(self.reply); lay.addWidget(c)
        self.route_retry = self.link('重新测试选择话术', lambda: self.app.acceptance_action('route')); lay.addWidget(self.route_retry)
        self.trace_button = self.link('展开选择路径与分数', self.toggle_trace); lay.addWidget(self.trace_button)
        self.trace = QPlainTextEdit(); self.trace.setReadOnly(True); self.trace.setMinimumHeight(170); lay.addWidget(self.trace); self.trace.hide()
        lay.addWidget(label('分数是模型选择记录，不代表回复正确率。选择人工也可核对，但不会生成可发送候选。', 'small')); lay.addStretch()

    def build_send(self):
        lay = self.page(); lay.addWidget(badge('真实会话 · 仅测试发送一次', 'info'))
        self.send_intro = label('请在聊天页打开你控制的测试买家。图片测试结果不能作为发送候选。', 'small'); lay.addWidget(self.send_intro)
        self.customer = label('尚未读取测试会话', 'h3'); lay.addWidget(self.customer)
        self.preview_result = QPlainTextEdit(self); self.preview_result.setReadOnly(True); self.preview_result.hide()
        c, cl = card(16, 8); c.setStyleSheet('QFrame#card { background:#EDF1F5; border-radius:8px; }')
        cl.addWidget(label('客户问题', 'small')); self.live_question = label('尚未读取'); cl.addWidget(self.live_question)
        cl.addWidget(label('候选回复', 'small')); self.live_reply = label('尚未生成'); cl.addWidget(self.live_reply); lay.addWidget(c)
        self.read_button = self.link('重新读取测试会话 · 不发送', lambda: self.app.acceptance_action('read')); lay.addWidget(self.read_button)
        self.send_reason = label('', 'small'); lay.addWidget(self.send_reason)
        self.acceptance_state = label('', 'h3'); lay.addWidget(self.acceptance_state)
        self.unknown_help = note('请在聊天页人工核查', '可能已经发送。请核对回复气泡与草稿，确认后到人工待办恢复后续接待；此处不会直接重发。', 'warning'); lay.addWidget(self.unknown_help)
        lay.addStretch()

    def zoom(self):
        if not self.png: return
        dialog, lay = self.window.dialog('原始聊天截图', 1080, 780)
        lay.addWidget(scroll(self.window.picture(self.png, 1000)), 1); self.window.open_dialog(dialog)

    def raw_response(self):
        row = self.data.get('vision') or {}
        raw = row.get('response_json')
        self.window.text_dialog('MiniMax 原始返回', raw or '尚未调用识字服务。')

    def toggle_trace(self):
        visible = not self.trace.isVisible(); self.trace.setVisible(visible)
        self.trace_button.setText('收起选择路径与分数' if visible else '展开选择路径与分数')

    @staticmethod
    def parse(row, key):
        try: return json.loads((row or {}).get(key) or '{}')
        except (TypeError, ValueError): return {}

    @staticmethod
    def decision_trace(decision):
        lines = ['选择路径：' + (' → '.join(decision.get('path', [])) or '尚未完成分类')]
        for index, item in enumerate(decision.get('trace', [])):
            choice = item.get('choice'); path = decision.get('path', [])
            selected = '转人工' if choice == 'HUMAN' else '复核通过' if choice == 'SEND' else (
                path[index] if index < len(path) else '选择本条固定回复')
            probs = item.get('probabilities') or {}
            scores = [('所选选项', probs.get(choice, item.get('confidence'))), ('转人工选项', probs.get('HUMAN'))]
            ratios = '　'.join(f'{name}：{value:.0%}' for name, value in scores if isinstance(value, (float, int)))
            lines.append(f'第 {index+1} 步：{selected}' + ('\n' + ratios if ratios else ''))
        return '\n\n'.join(lines)

    def render(self, data):
        previous = self.pages.currentIndex(); self.data = data
        step = data.get('step', 0); self.pages.setCurrentIndex(step)
        row = data.get('vision') or data.get('image') or {}
        self.png = row.get('image_png'); self.file_label.setText(data.get('filename') or '尚未选择聊天截图')
        self.select_button.setText('重新选图' if self.png else '上传聊天截图')
        self.upload_box.setVisible(not bool(self.png)); self.image_card.setVisible(bool(self.png))
        self.result_card.setVisible(bool(data.get('vision_id')))
        self.file_label.setVisible(bool(self.png)); self.select_button.setVisible(bool(self.png)); self.zoom_button.setVisible(bool(self.png))
        self.ocr_intro.setText('对照原图确认正确，再继续测试话术选择。' if data.get('vision_id') else '图片测试不操作聊天页面，也不会发送消息。')
        state = self.parse(data.get('vision'), 'transcript_json')
        roles = {'seller': '卖家', 'buyer': '买家', 'system': '系统'}
        self.transcript.setText('\n\n'.join(roles.get(m['role'], m['role']) + '：' + m['text'] for m in state.get('history', [])) or '开始识字后，按聊天顺序显示文字与买卖家身份。')
        passed = (data.get('vision') or {}).get('status') == 'vision_test_passed'
        self.vision_state.setText('识字已核对' if data.get('vision_reviewed') else '识字完成 · 待核对' if passed else '识字失败 · 可查看原始返回并重试' if data.get('vision_id') else '等待识字')
        route = self.parse(data.get('route'), 'decision_json')
        self.question.setText('\n'.join(m['text'] for m in state.get('pending_buyer_messages', [])) or '识字结果中没有待答买家消息。')
        self.reply.setText(('转人工：' + route.get('reason', '')) if route.get('action') == 'human' else route.get('text') or '点击测试选择话术，查看固定回复。')
        self.trace.setPlainText(self.decision_trace(route))
        live = data.get('live') or {}; decision = live.get('decision') or {}
        self.customer.setText(nickname(live.get('customer')) or '尚未读取测试会话')
        live_input = live.get('input')
        question = '\n'.join(m['text'] for m in live_input.get('pending_buyer_messages', [])) if isinstance(live_input, dict) else str(live_input or '')
        self.preview_result.setPlainText('客户问题：\n' + (question or '尚未读取') + '\n\n候选回复：\n' + (decision.get('text') or decision.get('reason') or '尚未生成'))
        self.live_question.setText(question or '尚未读取'); self.live_reply.setText(decision.get('text') or decision.get('reason') or '尚未生成')
        self.send_intro.setVisible(not bool(live))
        self.send_reason.setText(live.get('reason') or '请读取真实测试会话，并核对问题与回复。')
        names = {'sent_ui_verified': '网页新增回复已确认', 'send_unknown': '发送结果未知 · 已暂停', 'send_aborted_before_click': '点击前已中止 · 未自动重试', 'candidate': '候选已生成 · 尚未发送', 'blocked': '暂不能发送', 'expired': '候选已失效'}
        self.acceptance_state.setText(names.get(live.get('status'), '尚未完成测试发送'))
        self.unknown_help.setVisible(live.get('status') == 'send_unknown')
        self.error.setText(data.get('error') or (live.get('reason') if step == 2 and live and not live.get('id') and live.get('status') != 'sent_ui_verified' else '') or '')
        if previous != step and not self.app.ui_state.data.get('reduced_motion'):
            effect = QGraphicsOpacityEffect(self.pages); self.pages.setGraphicsEffect(effect)
            self.animation = QPropertyAnimation(effect, b'opacity', self); self.animation.setDuration(180)
            self.animation.setStartValue(.65); self.animation.setEndValue(1); self.animation.setEasingCurve(QEasingCurve.OutCubic); self.animation.start()
        self.refresh(); self.fit_image()
        self.pages.currentWidget().verticalScrollBar().setValue(0)

    def refresh(self):
        busy = bool(self.app.busy) or self.app.closing; d = self.data; step = d.get('step', 0)
        authorized = self.app.cfg['desktop']['data_upload_authorized']
        self.authorization.setVisible(not authorized)
        self.authorize.setEnabled(self.consent.isChecked() and not busy)
        self.select_button.setEnabled(not busy); self.retry_ocr.setEnabled(not busy and authorized)
        self.route_retry.setEnabled(not busy); self.read_button.setEnabled(not busy)
        self.zoom_button.setEnabled(bool(self.png)); self.raw_button.setEnabled(bool(d.get('vision_id')))
        self.retry_ocr.setVisible(bool(d.get('vision_id'))); self.route_retry.setVisible(bool((d.get('route') or {}).get('decision_json')))
        self.trace_button.setVisible(bool((d.get('route') or {}).get('decision_json')))
        for i, b in enumerate(self.progress):
            b.setEnabled(bool(not busy and (i == 0 or i == 1 and d.get('vision_reviewed') or i == 2 and d.get('route_reviewed'))))
            b.setProperty('active', i == step); b.style().unpolish(b); b.style().polish(b)
        if self.window.wizard_pages.currentIndex() != 4: return
        live = d.get('live') or {}
        passed = (d.get('vision') or {}).get('status') == 'vision_test_passed'
        text = ('上传聊天截图' if not d.get('image_id') else '开始识字' if not passed else '识字正确，继续') if step == 0 else (
            '测试选择话术' if not (d.get('route') or {}).get('decision_json') else '确认结果，继续') if step == 1 else (
            '完成验收' if self.app.cfg['desktop']['test_send_verified'] else '核对无误，测试发送一次' if live.get('id') and live.get('id') == self.app.current_id else '读取测试会话')
        self.window.next_button.setText('执行中…' if busy else text)
        allowed = not busy and bool(d.get('id'))
        if step == 0 and d.get('image_id') and not passed and not authorized: allowed = False
        self.window.next_button.setEnabled(allowed)
        self.window.back_button.setEnabled(not busy)
        self.window.wizard_title.setText(('文字和身份，逐一核对。' if d.get('vision_id') else '先确认图片读得正确。', '合适的话术，由你核对。', '确认一次真实接待。')[step])
        self.window.wizard_subtitle.setText(f'首次配置 / 05 · {step+1} / 3')

    def fit_image(self):
        pix = QPixmap()
        if self.png and pix.loadFromData(bytes(self.png)):
            self.image.setText(''); self.image.setPixmap(pix.scaled(max(140, self.image_card.width()-32), 300, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.image.setPixmap(QPixmap()); self.image.setText('上传一张能清楚看到买卖家消息的 PNG 聊天截图。')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = self.width() < 760
        self.columns.setDirection(QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight)
        self.upload_box.setMinimumHeight(208 if narrow else 268)
        self.fit_image()
