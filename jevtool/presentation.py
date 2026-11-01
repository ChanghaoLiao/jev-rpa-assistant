"""Native Qt components using the approved Figma tokens and downloaded SVGs."""
from __future__ import annotations
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont, QFontDatabase, QIcon, QPixmap
from PySide6.QtSvg import QSvgRenderer  # Include Qt's SVG provider in the frozen build.
from PySide6.QtWidgets import (QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
                              QLineEdit, QScrollArea, QWidget, QTableWidget, QHeaderView,
                              QAbstractItemView)
from .paths import SOURCE_ROOT

COLORS = dict(blue='#0145F2', cloud='#EDF1F5', white='#FFFFFF', ink='#172033',
              secondary='#526078', border='#D5DEE8', blueSoft='#EAF0FF', green='#13734C',
              greenSoft='#EAF7F0', amber='#8A5100', amberSoft='#FFF4DE', red='#B42332', redSoft='#FFF0F1')
ASSETS = SOURCE_ROOT / 'assets'
ICONS = {
    'brand': '5-72-imgFrame.svg', 'chat': '5-72-imgIconChat.svg',
    'clock': '5-72-imgIconClock.svg', 'list': '5-72-imgIconList.svg',
    'settings': '5-72-imgIconSettings.svg', 'shield': '5-72-imgIconShield.svg',
    'check': '4-169-imgIconCheck.svg', 'alert': '5-250-imgIconAlert.svg',
    'upload': '4-249-imgIconUpload.svg', 'file': '6-340-imgIconFile.svg',
    'play': '4-94-imgIconPlay.svg', 'info': '6-261-imgIconShield.svg',
}

STYLE = '''
QWidget { font-family: "Noto Sans SC", "Microsoft YaHei UI"; font-size: 16px; color: #172033; }
QMainWindow, QWidget#shell, QWidget#page { background: #EDF1F5; }
QFrame#card, QFrame#header, QFrame#sidebar, QDialog { background: #FFFFFF; border-radius: 12px; }
QFrame#header, QFrame#sidebar { border-radius: 0; }
QLabel { background: transparent; border: none; }
QLabel[role="h1"] { font-size: 32px; font-weight: 700; }
QLabel[role="h2"] { font-size: 24px; font-weight: 700; }
QLabel[role="h3"] { font-size: 20px; font-weight: 500; }
QLabel[role="small"] { font-size: 14px; color: #526078; }
QLabel[role="caption"] { font-size: 12px; color: #526078; }
QLabel[role="blue"] { color: #0145F2; font-size: 14px; }
QPushButton { min-height: 46px; padding: 0 22px; border-radius: 8px; background: #FFFFFF; border: 1px solid #D5DEE8; font-weight: 500; }
QPushButton:hover { background: #EDF1F5; }
QPushButton:pressed { background: #E3E8EF; }
QPushButton:focus { border: 3px solid #0145F2; }
QPushButton[role="primary"] { background: #0145F2; color: white; border-color: #0145F2; }
QPushButton[role="primary"]:hover { background: #003BCF; }
QPushButton[role="primary"]:pressed { background: #002B9D; }
QPushButton[role="danger"] { background: #FFF0F1; color: #B42332; border-color: #FFF0F1; }
QPushButton[role="danger"]:hover { background: #FFE0E3; }
QPushButton[role="nav"] { border: none; text-align: left; padding-left: 18px; color: #526078; background: transparent; }
QPushButton[role="nav"][active="true"] { background: #EAF0FF; color: #0145F2; }
QPushButton:disabled { background: #E3E8EF; color: #526078; border-color: #E3E8EF; }
QLineEdit, QComboBox, QPlainTextEdit, QTextEdit, QDateEdit, QSpinBox, QDoubleSpinBox { background: white; border: 1px solid #D5DEE8; border-radius: 8px; padding: 10px 14px; selection-background-color: #0145F2; }
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus { border: 2px solid #0145F2; }
QComboBox { min-height: 26px; }
QCheckBox { spacing: 10px; font-size: 14px; }
QCheckBox::indicator { width: 18px; height: 18px; }
QTableWidget { background: white; border: none; gridline-color: #D5DEE8; font-size: 14px; }
QTableWidget::item { padding: 12px; border-bottom: 1px solid #D5DEE8; }
QTableWidget::item:selected { background: #EAF0FF; color: #172033; }
QHeaderView::section { background: #EDF1F5; color: #526078; padding: 14px; border: none; font-size: 14px; }
QScrollArea, QScrollArea > QWidget > QWidget { border: none; background: #FFFFFF; }
QScrollBar:vertical { width: 8px; background: transparent; }
QScrollBar::handle:vertical { background: #D5DEE8; border-radius: 4px; min-height: 40px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { padding: 8px; background: #172033; color: white; border: none; }
'''


def install_style(application):
    font = ASSETS / 'fonts' / 'NotoSansSC.ttf'
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
    application.setStyle('Fusion')
    application.setFont(QFont('Noto Sans SC', 11))
    application.setStyleSheet(STYLE)


def icon(name):
    return QIcon(str(ASSETS / 'icons' / ICONS[name]))


def label(text='', role=None, wrap=True):
    w = QLabel(str(text))
    if role:
        w.setProperty('role', role)
    w.setWordWrap(wrap)
    w.setTextInteractionFlags(Qt.TextSelectableByMouse)
    w.setTextFormat(Qt.PlainText)
    return w


def button(text, callback=None, role='secondary', width=None):
    w = QPushButton(text)
    w.setProperty('role', role)
    w.setCursor(Qt.PointingHandCursor)
    w.setAutoDefault(False)
    if width:
        w.setMinimumWidth(width)
    if callback:
        w.clicked.connect(lambda checked=False: callback())
    return w


def card(margins=24, spacing=16):
    w = QFrame()
    w.setObjectName('card')
    layout = QVBoxLayout(w)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    return w, layout


def horizontal(*widgets, spacing=16):
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for w in widgets:
        if isinstance(w, int):
            layout.addStretch(w)
        else:
            layout.addWidget(w)
    return layout


def badge(text, state='neutral'):
    colors = {'success': ('green', 'greenSoft'), 'warning': ('amber', 'amberSoft'),
              'error': ('red', 'redSoft'), 'info': ('blue', 'blueSoft'), 'neutral': ('secondary', 'cloud')}
    fg, bg = colors[state]
    w = label(text, wrap=False)
    w.setStyleSheet(f'font-size:12px; color:{COLORS[fg]}; background:{COLORS[bg]}; padding:4px 12px; border-radius:4px;')
    w.setMaximumWidth(w.sizeHint().width() + 12)
    return w


def icon_label(name, size=24):
    w = QLabel()
    w.setPixmap(icon(name).pixmap(size, size))
    w.setFixedSize(size, size)
    if name == 'brand':
        w.setStyleSheet('background:#0145F2; border-radius:8px; padding:0;')
    return w


def note(title, body, state='info'):
    fg, bg = {'info': ('blue', 'blueSoft'), 'success': ('green', 'greenSoft'),
              'warning': ('amber', 'amberSoft'), 'error': ('red', 'redSoft')}[state]
    w, layout = card(16, 6)
    w.setStyleSheet(f'QFrame#card {{ background: {COLORS[bg]}; border-radius:8px; }}')
    content = QVBoxLayout()
    content.addWidget(label(title))
    content.addWidget(label(body, 'small'))
    layout.addLayout(horizontal(icon_label('check' if state == 'success' else 'alert' if state in ('warning', 'error') else 'info'), 0), 0)
    # Place text alongside the icon without drawing custom symbols.
    first = layout.takeAt(0).layout()
    first.addLayout(content, 1)
    layout.addLayout(first)
    return w


def scroll(widget):
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(widget)
    return area


def table(headers):
    w = QTableWidget(0, len(headers))
    w.setHorizontalHeaderLabels(headers)
    w.verticalHeader().hide()
    w.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    w.setSelectionBehavior(QAbstractItemView.SelectRows)
    w.setSelectionMode(QAbstractItemView.SingleSelection)
    w.setEditTriggers(QAbstractItemView.NoEditTriggers)
    w.setShowGrid(False)
    w.setWordWrap(True)
    return w


class Value:
    """Small compatibility boundary for the existing configuration semantics."""
    def __init__(self, widget):
        self.widget = widget

    def get(self):
        if hasattr(self.widget, 'isChecked'):
            return self.widget.isChecked()
        return self.widget.currentText() if hasattr(self.widget, 'currentText') else self.widget.text()

    def set(self, value):
        if hasattr(self.widget, 'setChecked'):
            self.widget.setChecked(bool(value))
        elif hasattr(self.widget, 'setCurrentText'):
            self.widget.setCurrentText(str(value))
        else:
            self.widget.setText(str(value))


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            clear_layout(item.layout())
