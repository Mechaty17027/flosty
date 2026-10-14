"""Render the existing SVG into a Windows ICO without extra image libraries."""
from __future__ import annotations

import argparse
from pathlib import Path
import struct
import sys

from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def make_icon(source: Path, output: Path) -> None:
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise ValueError('Invalid SVG icon.')
    pictures = []
    for size in (16, 32, 48, 64, 128, 256):
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, 'PNG'):
            raise ValueError('Cannot encode icon.')
        pictures.append((size, bytes(data)))
    offset = 6 + 16 * len(pictures)
    directory = bytearray(struct.pack('<HHH', 0, 1, len(pictures)))
    for size, data in pictures:
        directory.extend(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(bytes(directory) + b''.join(data for _, data in pictures))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('build/MechaHub.ico'))
    args = parser.parse_args()
    application = QGuiApplication.instance() or QGuiApplication(sys.argv)
    make_icon(Path(__file__).resolve().parents[1] / 'assets' / 'icon.svg', args.output)
