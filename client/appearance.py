"""Цвета и оформление окна Qt."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import QApplication

LIGHT = {
    "canvas": "#F3F5FA",
    "surface": "#FFFFFF",
    "inset": "#F6F7FC",
    "line": "#DCE1ED",
    "text": "#172139",
    "muted": "#55637C",
    "primary": "#4838CD",
    "hover": "#3B2CB7",
    "tint": "#ECE9FC",
    "accent": "#4838BD",
    "disabled": "#E5E8F1",
    "notice": "#FFF8E8",
    "notice_line": "#E8C98E",
    "notice_text": "#704812",
    "plus": "#136840",
    "minus": "#A32949",
}
DARK = {
    "canvas": "#111521",
    "surface": "#1A2030",
    "inset": "#151A28",
    "line": "#354057",
    "text": "#EFF1F9",
    "muted": "#ABB6CE",
    "primary": "#5847DB",
    "hover": "#6654E8",
    "tint": "#302950",
    "accent": "#C6BCFF",
    "disabled": "#292F43",
    "notice": "#30271C",
    "notice_line": "#6B5232",
    "notice_text": "#F5CD87",
    "plus": "#8ADCB0",
    "minus": "#FFA3B9",
}

STYLE = """
QWidget {{ color: {text}; font-size: 14px; }}
#workspace {{ background: {canvas}; }}
#panel, #connection {{
    background: {surface}; border: 1px solid {line}; border-radius: 14px;
}}
#brand {{
    background: {primary}; color: white; border-radius: 11px;
    font-size: 16px; font-weight: 700;
}}
#title {{ font-size: 24px; font-weight: 700; }}
#section {{ font-size: 16px; font-weight: 700; }}
#caption {{ color: {muted}; font-size: 12px; }}
#fieldLabel {{ color: {muted}; font-size: 12px; font-weight: 600; }}
#badge {{
    background: {tint}; color: {accent}; border-radius: 10px;
    padding: 6px 10px; font-size: 12px; font-weight: 700;
}}
#approval {{
    background: {notice}; border: 1px solid {notice_line}; border-radius: 12px;
}}
#approvalTitle {{ color: {notice_text}; font-size: 13px; font-weight: 700; }}
#status {{ color: {muted}; font-size: 12px; }}
QLineEdit, #composer, #preview {{
    background: {inset}; border: 1px solid {line}; border-radius: 9px;
    selection-background-color: {primary}; selection-color: white;
}}
QLineEdit {{ padding: 7px 9px; }}
QLineEdit:disabled {{ color: {muted}; }}
QLineEdit:focus, #composer:focus, #preview:focus {{ border-color: {primary}; }}
#chat, #activity, #fileContent, QListWidget {{
    background: {surface}; border: 1px solid transparent;
    selection-background-color: {primary}; selection-color: white;
}}
#chat:focus, #activity:focus, #fileContent:focus, QListWidget:focus {{
    border-color: {line};
}}
#composer {{ background: {surface}; }}
QListWidget::item {{ padding: 6px; }}
QListWidget::item:selected {{ background: {tint}; color: {accent}; }}
QTabWidget::pane {{ border: 0; border-top: 1px solid {line}; }}
QTabBar::tab {{
    padding: 7px 12px; color: {muted}; border-bottom: 2px solid transparent;
}}
QTabBar::tab:hover {{ color: {text}; }}
QTabBar::tab:focus {{ background: {inset}; }}
QTabBar::tab:selected {{
    border-bottom-color: {primary}; color: {accent}; font-weight: 700;
}}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {line}; }}
QScrollBar {{ background: transparent; border: 0; }}
QScrollBar:vertical {{ width: 10px; }}
QScrollBar:horizontal {{ height: 10px; }}
QScrollBar::handle {{ background: {line}; border-radius: 4px; min-height: 22px; min-width: 22px; }}
QScrollBar::handle:hover {{ background: {muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QPushButton {{
    background: {surface}; border: 1px solid {line}; border-radius: 9px;
    padding: 7px 13px; min-height: 18px; font-weight: 600;
}}
QPushButton:hover {{ background: {inset}; }}
QPushButton:focus {{ border-color: {primary}; }}
QPushButton[primary="true"] {{
    background: {primary}; color: white; border-color: {primary};
}}
QPushButton[primary="true"]:hover {{ background: {hover}; }}
QPushButton[primary="true"]:focus {{ border-color: {accent}; }}
QPushButton:disabled, QPushButton[primary="true"]:disabled {{
    background: {disabled}; color: {muted}; border-color: {line};
}}
"""


def apply_theme(window, scheme, fields):
    colors = DARK if scheme == Qt.ColorScheme.Dark else LIGHT
    window.setStyleSheet(STYLE.format_map(colors))
    for field in fields:
        palette = QApplication.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(colors["muted"]))
        field.setPalette(palette)
    return colors


class DiffHighlight(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.colors = LIGHT
        self.enabled = False

    def highlightBlock(self, text):
        if not self.enabled:
            return
        if text.startswith(("+++", "---", "@@")):
            color = "muted"
        elif text.startswith("+"):
            color = "plus"
        elif text.startswith("-"):
            color = "minus"
        else:
            return
        style = QTextCharFormat()
        style.setForeground(QColor(self.colors[color]))
        self.setFormat(0, len(text), style)
