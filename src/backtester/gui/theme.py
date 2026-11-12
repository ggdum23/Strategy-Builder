"""Dark and light themes. Fusion plus a stylesheet, so Windows and Linux match."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette


@dataclass(frozen=True)
class Theme:
    name: str
    bg: str
    surface: str
    elevated: str
    border: str
    text: str
    muted: str
    accent: str
    accent_text: str
    positive: str
    negative: str
    warning: str
    warning_bg: str
    danger_bg: str
    ok_bg: str
    info_bg: str
    chart_bg: str
    chart_fg: str
    chart_grid: str
    strategy: str
    bench: str
    bench2: str
    drawdown: str
    exposure: str
    input_bg: str
    selection: str
    ui: str = "Sans Serif"
    mono: str = "Monospace"

    def with_fonts(self, ui: str, mono: str) -> "Theme":
        return Theme(**{**self.__dict__, "ui": ui, "mono": mono})


DARK = Theme(
    name="dark",
    bg="#0c1016",
    surface="#141a22",
    elevated="#1b232e",
    border="#2c3848",
    text="#e7edf4",
    muted="#93a0b0",
    accent="#3c7dd9",
    accent_text="#f7f9fc",
    positive="#3cba8b",
    negative="#e06c75",
    warning="#d7b15e",
    warning_bg="#2a2416",
    danger_bg="#2c181c",
    ok_bg="#13241c",
    info_bg="#15202c",
    chart_bg="#10161e",
    chart_fg="#c5d0dc",
    chart_grid="#243140",
    strategy="#8eb7f0",
    bench="#b7aea2",
    bench2="#d7b15e",
    drawdown="#e06c75",
    exposure="#6ec8a8",
    input_bg="#0f141b",
    selection="#24344a",
)

LIGHT = Theme(
    name="light",
    bg="#f3f5f8",
    surface="#ffffff",
    elevated="#e8edf3",
    border="#d3dbe4",
    text="#1a2330",
    muted="#5d6b7c",
    accent="#1d4e89",
    accent_text="#ffffff",
    positive="#0e7a45",
    negative="#a12622",
    warning="#8a5a00",
    warning_bg="#fbf3df",
    danger_bg="#fdecea",
    ok_bg="#e7f6ee",
    info_bg="#e8f0f8",
    chart_bg="#ffffff",
    chart_fg="#1a2330",
    chart_grid="#e1e7ee",
    strategy="#1d4e89",
    bench="#6d645c",
    bench2="#8a5a00",
    drawdown="#a12622",
    exposure="#0e7a45",
    input_bg="#ffffff",
    selection="#d9e6f5",
)

THEMES = {"dark": DARK, "light": LIGHT}


def build_palette(theme: Theme) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(theme.bg))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Base, QColor(theme.input_bg))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(theme.elevated))
    palette.setColor(QPalette.ColorRole.Text, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Button, QColor(theme.surface))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(theme.accent))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(theme.accent_text))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(theme.elevated))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(theme.muted))
    palette.setColor(QPalette.ColorRole.Mid, QColor(theme.border))
    return palette


def build_qss(theme: Theme) -> str:
    return f"""
    QMainWindow, QDialog, QWidget#appRoot, QWidget#page, QStackedWidget, QScrollArea, QTabWidget::pane {{
        background: {theme.bg};
        color: {theme.text};
        font-family: "{theme.ui}";
        font-size: 10.5pt;
    }}
    QLabel {{
        background: transparent;
        color: {theme.text};
    }}
    QLabel#muted, QLabel#kicker {{
        color: {theme.muted};
    }}
    QLabel#kicker {{
        font-size: 8.5pt;
        letter-spacing: 0.12em;
    }}
    QLabel#pageTitle {{
        font-size: 18pt;
        font-weight: 600;
    }}
    QLabel#wordmark {{
        font-size: 13pt;
        font-weight: 650;
        letter-spacing: 0.08em;
    }}
    QLabel#metricValue {{
        font-family: "{theme.mono}";
        font-size: 15pt;
        font-weight: 600;
    }}
    QLabel#section {{
        font-size: 11.5pt;
        font-weight: 600;
    }}
    QFrame#topbar, QFrame#rail, QFrame#card, QFrame#panel {{
        background: {theme.surface};
        border: 1px solid {theme.border};
    }}
    QFrame#topbar {{
        border-left: none;
        border-right: none;
        border-top: none;
    }}
    QFrame#rail {{
        border-top: none;
        border-bottom: none;
        border-left: none;
    }}
    QFrame#card, QFrame#panel {{
        border-radius: 2px;
    }}
    QFrame#banner-error {{
        background: {theme.danger_bg};
        border: 1px solid {theme.negative};
        border-radius: 2px;
    }}
    QFrame#banner-warn {{
        background: {theme.warning_bg};
        border: 1px solid {theme.warning};
        border-radius: 2px;
    }}
    QFrame#banner-ok {{
        background: {theme.ok_bg};
        border: 1px solid {theme.positive};
        border-radius: 2px;
    }}
    QFrame#banner-info {{
        background: {theme.info_bg};
        border: 1px solid {theme.accent};
        border-radius: 2px;
    }}
    QPushButton {{
        background: {theme.elevated};
        color: {theme.text};
        border: 1px solid {theme.border};
        border-radius: 2px;
        padding: 6px 12px;
        text-align: left;
    }}
    QPushButton:hover {{
        border-color: {theme.accent};
    }}
    QPushButton:disabled {{
        color: {theme.muted};
    }}
    QPushButton#primary, QPushButton#runButton {{
        background: {theme.accent};
        color: {theme.accent_text};
        border: 1px solid {theme.accent};
        font-weight: 600;
    }}
    QPushButton#primary:disabled, QPushButton#runButton:disabled {{
        background: {theme.elevated};
        color: {theme.muted};
        border: 1px solid {theme.border};
        font-weight: 500;
    }}
    QPushButton#danger {{
        background: transparent;
        color: {theme.negative};
        border: 1px solid {theme.negative};
    }}
    QPushButton[navRole="true"] {{
        background: transparent;
        border: none;
        border-left: 3px solid transparent;
        border-radius: 0;
        padding: 9px 14px;
        color: {theme.muted};
        text-align: left;
    }}
    QPushButton[navRole="true"]:checked {{
        background: {theme.elevated};
        color: {theme.text};
        border-left: 3px solid {theme.accent};
        font-weight: 600;
    }}
    QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
        background: {theme.input_bg};
        color: {theme.text};
        border: 1px solid {theme.border};
        border-radius: 2px;
        padding: 5px 8px;
        selection-background-color: {theme.selection};
    }}
    QPlainTextEdit#transcript, QPlainTextEdit#mono {{
        font-family: "{theme.mono}";
        font-size: 9.5pt;
    }}
    QComboBox::drop-down {{
        border: none;
        width: 18px;
    }}
    QTableView, QTableWidget, QListWidget, QTreeWidget {{
        background: {theme.input_bg};
        color: {theme.text};
        alternate-background-color: {theme.surface};
        border: 1px solid {theme.border};
        gridline-color: {theme.border};
        selection-background-color: {theme.selection};
        selection-color: {theme.text};
    }}
    QHeaderView::section {{
        background: {theme.elevated};
        color: {theme.muted};
        border: none;
        border-bottom: 1px solid {theme.border};
        padding: 6px 8px;
        font-size: 9pt;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {theme.muted};
        padding: 8px 14px;
        border: none;
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:selected {{
        color: {theme.text};
        border-bottom: 2px solid {theme.accent};
    }}
    QTabWidget::pane {{
        border: none;
        background: {theme.bg};
    }}
    QScrollBar:vertical {{
        background: {theme.bg};
        width: 10px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {theme.border};
        min-height: 24px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        height: 0;
    }}
    QStatusBar {{
        background: {theme.surface};
        color: {theme.muted};
        border-top: 1px solid {theme.border};
    }}
    QToolTip {{
        background: {theme.elevated};
        color: {theme.text};
        border: 1px solid {theme.border};
        padding: 6px;
    }}
    QRadioButton, QCheckBox {{
        background: transparent;
        spacing: 8px;
        padding: 1px 12px 1px 2px;
        min-height: 22px;
    }}
    QRadioButton::indicator {{
        width: 14px;
        height: 14px;
        border-radius: 8px;
        border: 1.5px solid {theme.text};
        background: transparent;
        subcontrol-origin: padding;
        subcontrol-position: left center;
    }}
    QRadioButton::indicator:checked {{
        border: 1.5px solid {theme.accent};
        background: {theme.accent};
    }}
    QCheckBox::indicator {{
        width: 14px;
        height: 14px;
        border-radius: 2px;
        border: 1.5px solid {theme.text};
        background: transparent;
        subcontrol-origin: padding;
        subcontrol-position: left center;
    }}
    QCheckBox::indicator:checked {{
        border: 1.5px solid {theme.accent};
        background: {theme.accent};
    }}
    """
