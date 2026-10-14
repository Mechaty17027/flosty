"""A varied, calm workspace theme with consistent interaction states."""
from pathlib import Path
import sys

STYLESHEET = r'''
QWidget {
    color: #EFF0F9;
    background: #10131E;
    font-family: "Segoe UI", "Arial";
    font-size: 13px;
}
QLabel { background: transparent; }
QFrame#Sidebar { background: #171A28; border-right: 1px solid #2A2E42; }
QFrame#Card { background: #1B2030; border: 1px solid #32394F; border-radius: 16px; }
QFrame#Card[accent="purple"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #29233E, stop:1 #1C2032);
    border-color: #504366;
}
QFrame#Card[accent="cyan"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1C343E, stop:1 #1B2331);
    border-color: #32515A;
}
QFrame#Card[accent="amber"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #352D2B, stop:1 #212231);
    border-color: #584838;
}
QFrame#Card[accent="green"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1D3634, stop:1 #1C2530);
    border-color: #335951;
}
QFrame#Hero {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #493567, stop:0.55 #2B3654, stop:1 #233E42);
    border: 1px solid #655681;
    border-radius: 20px;
}
QFrame#WebHero {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1E494C, stop:1 #34345D);
    border: 1px solid #3D6B6B;
    border-radius: 20px;
}
QLabel#Brand { color: #FAF8FF; font-size: 25px; font-weight: 800; }
QLabel#Muted { color: #A6B1CB; }
QLabel#HeroText { color: #D5D9EF; }
QLabel#Eyebrow { color: #AEB6D2; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
QLabel#Metric { color: #F5F6FC; font-size: 29px; font-weight: 700; }
QFrame#Card[accent="purple"] QLabel#Metric { color: #C8B8FF; }
QFrame#Card[accent="cyan"] QLabel#Metric { color: #83E3E0; }
QFrame#Card[accent="amber"] QLabel#Metric { color: #F8C88F; }
QFrame#Card[accent="green"] QLabel#Metric { color: #8FE5C4; }
QLabel#Section { color: #F1F0FA; font-size: 17px; font-weight: 700; }
QLabel#HeroTitle { color: #FFFFFF; font-size: 29px; font-weight: 800; }
QLabel#Headline { color: #FAF8FF; font-size: 27px; font-weight: 700; }
QLabel#AccentTitle { color: #C1ADFF; font-size: 21px; font-weight: 700; }
QLabel#Pill { color: #B3ECD4; background: #203B37; border: 1px solid #365E51; border-radius: 11px; padding: 7px 11px; }
QPushButton {
    background: #282E45; border: 1px solid #414B67; border-radius: 10px;
    padding: 10px 14px; font-weight: 600; color: #EEF0FB;
}
QPushButton:hover { background: #353D59; border-color: #7D86B6; }
QPushButton:pressed { background: #202637; }
QPushButton:disabled { color: #748099; background: #1C2130; border-color: #30374B; }
QPushButton#Primary { color: #FFFFFF; background: #8065D6; border-color: #AC8EF7; }
QPushButton#Primary:hover { background: #9176E7; }
QPushButton#Subtle { background: transparent; border: none; color: #C7B6FA; padding: 5px 8px; }
QPushButton#Subtle:hover { background: #302B47; }
QPushButton#Nav {
    border: 0; border-radius: 11px; background: transparent; color: #A9B2CA;
    text-align: left; padding: 12px 14px; font-weight: 600;
}
QPushButton#Nav:hover { background: #242A3F; color: #F6F4FF; }
QPushButton#Nav[active="true"] { background: #37304F; color: #E3D6FF; border-left: 3px solid #AC8CF6; }
QToolButton#IconLaunch { background: #252D43; border: 1px solid #3B465D; border-radius: 16px; padding: 12px; }
QToolButton#IconLaunch[accent="purple"] { background: #302A46; border-color: #54456D; }
QToolButton#IconLaunch[accent="cyan"] { background: #203840; border-color: #385863; }
QToolButton#IconLaunch[accent="amber"] { background: #3A3029; border-color: #5E4B39; }
QToolButton#IconLaunch[accent="green"] { background: #223C37; border-color: #3A6050; }
QToolButton#IconLaunch:hover { background: #44415F; border: 1px solid #C5AEFF; }
QToolButton#IconLaunch:pressed { background: #232538; }
QPushButton#BrowserControl { padding: 0; }
QToolButton#BrowserAddTab { background: #282E45; color: #D6C8FF; border: 1px solid #414B67; border-radius: 8px; font-size: 19px; }
QToolButton#BrowserAddTab:hover { background: #413552; }
QPushButton#WebLink { min-height: 52px; background: #212A3B; border-color: #394B60; }
QTabWidget::pane { border: none; background: transparent; }
QTabBar { background: transparent; }
QTabBar::tab { background: #1E2434; color: #AAB4CD; padding: 10px 17px; border-bottom: 2px solid transparent; margin-right: 6px; }
QTabBar::tab:selected { color: #ECE1FF; background: #302A45; border-bottom-color: #B396F8; }
QTabBar::tab:hover { color: #FFFFFF; background: #2B3146; }
QProgressBar { background: #323A50; border: none; border-radius: 4px; min-height: 7px; max-height: 7px; }
QProgressBar::chunk { background: #A58AED; border-radius: 4px; }
QTableWidget, QListWidget {
    background: #1B2030; alternate-background-color: #22283A; border: 1px solid #333D56;
    border-radius: 14px; gridline-color: transparent; selection-background-color: #484066;
    selection-color: #FFFFFF; outline: 0;
}
QHeaderView::section { background: #272E42; color: #BCC7DE; border: none; padding: 11px; font-weight: 600; }
QTableWidget::item { padding: 8px; }
QTableWidget::item:selected { background: #494061; color: #FFFFFF; }
QListWidget#AppGrid { background: #141925; border: 1px solid #2D354B; padding: 10px; }
QListWidget#AppGrid::item { background: #252B40; border: 1px solid #3C435C; border-radius: 16px; padding: 12px; }
QListWidget#AppGrid::item:hover { background: #343C54; border-color: #8B87B7; }
QListWidget#AppGrid::item:selected { background: #403652; border: 2px solid #B496F7; }
QScrollArea { background: transparent; border: none; }
QDialog { background: #10131E; }
QScrollBar:vertical { background: #141925; width: 9px; margin: 0; }
QScrollBar::handle:vertical { background: #414D67; border-radius: 4px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #141925; height: 9px; }
QScrollBar::handle:horizontal { background: #414D67; border-radius: 4px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QLineEdit { background: #151B2B; border: 1px solid #414A65; border-radius: 10px; padding: 10px 12px; color: #F1F2FB; }
QLineEdit:focus { border-color: #B495F7; }
QComboBox, QDoubleSpinBox { background: #151B2B; border: 1px solid #414A65; border-radius: 9px; padding: 8px 12px; color: #F1F2FB; min-height: 22px; }
QComboBox QAbstractItemView { background: #242B40; selection-background-color: #494064; color: #F1F2FB; }
QSplitter::handle { background: #30394F; width: 5px; margin: 4px; }
QCheckBox { background: transparent; color: #CFD7EB; spacing: 8px; }
QCheckBox:disabled { color: #77839C; }
QCheckBox::indicator { width: 17px; height: 17px; background: #151B2B; border: 1px solid #7B89A6; border-radius: 4px; }
QCheckBox::indicator:hover { border-color: #C6B1FF; }
QCheckBox::indicator:checked { background: #8065D6; border-color: #C6B1FF; image: url("%CHECK_ICON%"); }
QCheckBox::indicator:disabled { background: #272E42; border-color: #4D5870; }
QTextEdit, QTextBrowser { background: #1B2030; color: #D4DCF0; border: 1px solid #343E57; border-radius: 12px; padding: 10px; }
QMenu { background: #242B40; color: #F0EEF8; border: 1px solid #49536E; padding: 6px; }
QMenu::item { padding: 10px 18px; }
QMenu::item:selected { background: #494064; }
QToolTip { color: #F1EFF9; background: #31364B; border: 1px solid #777B9A; padding: 7px; }
'''

_asset_root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
STYLESHEET = STYLESHEET.replace('%CHECK_ICON%', (_asset_root / 'assets' / 'check.svg').as_posix())
