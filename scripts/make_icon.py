"""Generate WinFirst's application icon.

The icon is drawn with Qt so the build does not depend on external artwork.
Run from the repository root:

    python scripts/make_icon.py

Outputs:
    src/winfirst/resources/icons/winfirst.ico
    src/winfirst/resources/icons/winfirst.png (256 px preview)
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "src" / "winfirst" / "resources" / "icons"


def _render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    scale = float(size)
    margin = scale * 0.03
    body = QRectF(margin, margin, scale - margin * 2, scale - margin * 2)

    # Rounded dark tile.
    background = QLinearGradient(body.topLeft(), body.bottomRight())
    background.setColorAt(0.0, QColor("#1b3a52"))
    background.setColorAt(1.0, QColor("#0b141d"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(background)
    painter.drawRoundedRect(body, scale * 0.22, scale * 0.22)

    # Shield.
    shield = QPainterPath()
    shield.moveTo(scale * 0.50, scale * 0.17)
    shield.lineTo(scale * 0.77, scale * 0.28)
    shield.lineTo(scale * 0.75, scale * 0.56)
    shield.quadTo(QPointF(scale * 0.72, scale * 0.74), QPointF(scale * 0.50, scale * 0.84))
    shield.quadTo(QPointF(scale * 0.28, scale * 0.74), QPointF(scale * 0.25, scale * 0.56))
    shield.lineTo(scale * 0.23, scale * 0.28)
    shield.closeSubpath()

    shield_fill = QLinearGradient(
        QPointF(scale * 0.5, scale * 0.17), QPointF(scale * 0.5, scale * 0.84)
    )
    shield_fill.setColorAt(0.0, QColor("#67b5f2"))
    shield_fill.setColorAt(1.0, QColor("#2f7df0"))
    painter.setBrush(shield_fill)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawPath(shield)

    # Check mark.
    check = QPainterPath()
    check.moveTo(scale * 0.35, scale * 0.51)
    check.lineTo(scale * 0.46, scale * 0.62)
    check.lineTo(scale * 0.67, scale * 0.39)
    pen = QPen(QColor("#ffffff"))
    pen.setWidthF(max(1.6, scale * 0.075))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(check)

    painter.end()
    return image


def _png_bytes(image: QImage) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return data


def _write_ico(path: Path, images: list[QImage]) -> None:
    payloads = [_png_bytes(image) for image in images]
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    offset = 6 + 16 * len(images)
    for image, payload in zip(images, payloads):
        dimension = 0 if image.width() >= 256 else image.width()
        entries += struct.pack(
            "<BBBBHHII",
            dimension,
            dimension,
            0,
            0,
            1,
            32,
            len(payload),
            offset,
        )
        offset += len(payload)
    path.write_bytes(header + entries + b"".join(payloads))


def main() -> int:
    # A Qt application is needed for font/painter backends even though the
    # icon uses vector shapes only.
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    images = [_render(size) for size in SIZES]
    _write_ico(OUT_DIR / "winfirst.ico", images)
    images[-1].save(str(OUT_DIR / "winfirst.png"), "PNG")
    app.quit()
    print(f"Wrote {OUT_DIR / 'winfirst.ico'} ({', '.join(str(s) for s in SIZES)} px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
