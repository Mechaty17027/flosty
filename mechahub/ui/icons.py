"""Vector action icons and cached native file icons for the GUI thread.

Call ``icon(name, color)`` for navigation or ``app_icon(path)`` for a program,
shortcut, or folder. Both return independent QIcon handles. A QApplication
must already exist; native shell icon extraction and pixmaps stay on its thread.
"""
from __future__ import annotations

from functools import lru_cache
import os

from PySide6.QtCore import QByteArray, QFileInfo, QPoint, QRect, QRectF, QSize, QThread, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QFileIconProvider


APP_ICON_CACHE_LIMIT = 192
VECTOR_ICON_CACHE_LIMIT = 128
DEFAULT_COLOR = '#A8B8D6'

# All artwork is local SVG geometry; no fonts, downloads, or external resources.
_SHAPES = {
    'cash': '<path d="M4 10h16v11H4Z M6 10V3h12v7M8 6h8M8 14h2M14 14h2M8 17h8"/>',
    'code': '<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18"/>',
    'study': '<path d="M3 4h6c2 0 3 1 3 2 0-1 1-2 3-2h6v15h-6c-2 0-3 1-3 2-1-1-2-2-4-2H3Z"/><path d="M12 6v15M6 8h3M15 8h3"/>',
    'currency': '<circle cx="12" cy="12" r="9"/><path d="M15.5 7.5h-5a2.5 2.5 0 0 0 0 5h3a2.5 2.5 0 0 1 0 5h-5M12 5v14"/>',
    'chart': '<path d="M3 3v18h18M6 16l4-6 4 3 6-8"/>',
    'home': '<path d="M3 10.5 12 3l9 7.5M5.5 9v11h5v-6h3v6h5V9"/>',
    'apps': '<rect x="3" y="3" width="7" height="7" rx="1.6"/>'
            '<rect x="14" y="3" width="7" height="7" rx="1.6"/>'
            '<rect x="3" y="14" width="7" height="7" rx="1.6"/>'
            '<rect x="14" y="14" width="7" height="7" rx="1.6"/>',
    'files': '<path d="M8 3h7l5 5v12a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"/>'
             '<path d="M15 3v5h5M4 7v11M10 12h7M10 16h5"/>',
    'network': '<rect x="8" y="3" width="8" height="5" rx="1.3"/>'
               '<rect x="2" y="16" width="6" height="5" rx="1.3"/>'
               '<rect x="16" y="16" width="6" height="5" rx="1.3"/>'
               '<path d="M12 8v4M5 16v-4h14v4M12 12v5"/>',
    'globe': '<circle cx="12" cy="12" r="9"/>'
             '<ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18M5 7h14M5 17h14"/>',
    'updates': '<path d="M20 9a8.3 8.3 0 0 0-14-3L3 9M3 4v5h5"/>'
               '<path d="M4 15a8.3 8.3 0 0 0 14 3l3-3M21 20v-5h-5"/>',
    'info': '<circle cx="12" cy="12" r="9"/><path d="M12 11v6"/>'
            '<circle cx="12" cy="7.5" r=".75" fill="currentColor" stroke="none"/>',
    'settings': '<path d="m9.5 3-.8 2.5-2 .9-2.5-.5-1.5 2.6 1.7 2v3l-1.7 2 1.5 2.6 2.5-.5 2 .9.8 2.5h3l.8-2.5 2-.9 2.5.5 1.5-2.6-1.7-2v-3l1.7-2-1.5-2.6-2.5.5-2-.9L12.5 3Z"/>'
                '<circle cx="11" cy="12" r="3"/>',
    'folder': '<path d="M3 6a1.5 1.5 0 0 1 1.5-1.5H10l2 2h7.5A1.5 1.5 0 0 1 21 8v10a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18Z"/>'
              '<path d="M3 10h18"/>',
    'taskmgr': '<rect x="3" y="3" width="18" height="18" rx="2.5"/>'
               '<path d="M7 17v-5M12 17V7M17 17v-8"/>',
    'search': '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/>',
    'plus': '<path d="M12 4v16M4 12h16"/>',
    'arrow': '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    'back': '<path d="M20 12H4m6-6-6 6 6 6"/>',
    'stop': '<path d="m6 6 12 12M6 18 18 6"/>',
    'download': '<path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4"/>',
    'upload': '<path d="M12 15V3m-5 5 5-5 5 5M4 16v4h16v-4"/>',
    'cpu': '<rect x="6" y="6" width="12" height="12" rx="2"/>'
           '<rect x="9" y="9" width="6" height="6" rx="1"/>'
           '<path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
    'memory': '<rect x="2" y="6" width="20" height="11" rx="1.5"/>'
              '<path d="M5 9v5M9 9v5M13 9v5M17 9v5M5 17v3M9 17v3M15 17v3M19 17v3"/>',
    'disk': '<rect x="3" y="4" width="18" height="16" rx="2.5"/>'
            '<path d="M3 15h18M7 17.5h.1M11 17.5h.1"/>'
            '<circle cx="12" cy="9.5" r="3"/><path d="m12 9.5 4 2.5"/>',
}

_FALLBACK_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="tile" x2="1" y2="1">
<stop stop-color="#263B61"/><stop offset="1" stop-color="#15213B"/>
</linearGradient><linearGradient id="accent" x2="1" y2="1">
<stop stop-color="#95AEFF"/><stop offset="1" stop-color="#55D7E4"/>
</linearGradient></defs>
<rect x="3" y="3" width="58" height="58" rx="16" fill="url(#tile)" stroke="#5B75A6"/>
<g fill="url(#accent)"><rect x="16" y="16" width="13" height="13" rx="4"/>
<rect x="35" y="16" width="13" height="13" rx="4"/>
<rect x="16" y="35" width="13" height="13" rx="4"/>
<rect x="35" y="35" width="13" height="13" rx="4"/></g></svg>'''


def _require_gui_thread() -> None:
    application = QApplication.instance()
    if not isinstance(application, QApplication):
        raise RuntimeError('Create QApplication before requesting MechaHub icons.')
    if QThread.currentThread() != application.thread():
        raise RuntimeError('MechaHub icons must be requested on the GUI thread.')


class _SvgIconEngine(QIconEngine):
    """Keep SVG geometry so Qt can request crisp pixmaps at any display scale."""

    def __init__(self, svg: bytes):
        super().__init__()
        self._svg = svg
        self._renderer = QSvgRenderer(QByteArray(svg))

    def clone(self) -> QIconEngine:
        return _SvgIconEngine(self._svg)

    def key(self) -> str:
        return 'MechaHubSvg'

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        painter.save()
        if mode == QIcon.Mode.Disabled:
            painter.setOpacity(painter.opacity() * 0.4)
        self._renderer.render(painter, QRectF(rect))
        painter.restore()

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            self.paint(painter, QRect(QPoint(0, 0), size), mode, state)
        finally:
            painter.end()
        return pixmap


@lru_cache(maxsize=VECTOR_ICON_CACHE_LIMIT)
def _cached_vector_icon(name: str, color: str) -> QIcon:
    shape = _SHAPES.get(name, _SHAPES['apps'])
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'color="{color}" fill="none" stroke="{color}" stroke-width="1.7" '
        f'stroke-linecap="round" stroke-linejoin="round">{shape}</svg>'
    ).encode('ascii')
    return QIcon(_SvgIconEngine(svg))


@lru_cache(maxsize=1)
def _fallback_icon() -> QIcon:
    return QIcon(_SvgIconEngine(_FALLBACK_SVG))


@lru_cache(maxsize=1)
def _file_icon_provider() -> QFileIconProvider:
    return QFileIconProvider()


@lru_cache(maxsize=APP_ICON_CACHE_LIMIT)
def _cached_app_icon(path: str, modified: int, size: int, directory: bool) -> QIcon:
    try:
        result = _file_icon_provider().icon(QFileInfo(path))
    except (OSError, RuntimeError):
        return _fallback_icon()
    return result if not result.isNull() else _fallback_icon()


def icon(name: str, color: str = DEFAULT_COLOR) -> QIcon:
    """Return a scalable local SVG icon; unknown names use the app-grid symbol."""
    _require_gui_thread()
    tint = QColor(color)
    if not tint.isValid():
        raise ValueError(f'Invalid icon color: {color!r}')
    return QIcon(_cached_vector_icon(name.strip().casefold(), tint.name()))


def app_icon(path: str) -> QIcon:
    """Return the shell icon for exe/lnk/files/folders, or a visible app tile.

    QFileIconProvider follows the Windows shell's file associations and shortcut
    handling. A changed file timestamp or size refreshes the cached icon. Native
    shell extraction is synchronous on first use, so request icons on the GUI
    thread after populating the list, and reuse this bounded cache on redraws.
    """
    _require_gui_thread()
    clean_path = os.path.expandvars(path.strip().strip('"').strip())
    if not clean_path:
        return QIcon(_fallback_icon())
    info = QFileInfo(os.path.expanduser(clean_path))
    if not info.exists():
        return QIcon(_fallback_icon())
    absolute = os.path.normcase(os.path.normpath(info.absoluteFilePath()))
    result = _cached_app_icon(absolute, info.lastModified().toMSecsSinceEpoch(), info.size(), info.isDir())
    return QIcon(result)


def clear_icon_cache() -> None:
    """Refresh shell icons after a user explicitly reloads the programs list."""
    _require_gui_thread()
    _cached_app_icon.cache_clear()
    _file_icon_provider.cache_clear()
