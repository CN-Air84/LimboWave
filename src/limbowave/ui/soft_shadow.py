"""柔和投影：逐层外扩的圆角矩形叠出由深到浅的晕染。

输入框与高级栏共用同一套观感参数：投影整体下移、左右略收，所以下沿
最浓最长，两侧淡，上沿几乎没有。范围要罩在宿主留白之内——输入框行的
上下边距（10 / 14）、高级栏与输入框的间距及对话区左右边距（各 16）。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath

SHADOW_OFFSET = 4  # 投影矩形整体向下移多少
SHADOW_SPREAD = 10  # 从投影矩形向外逐层叠多少层
SHADOW_ALPHA = 5  # 每层的不透明度（0–255），叠满也只有两成左右的黑
SHADOW_INSET = 3  # 投影矩形左右内收，两侧因此比上下更收敛


def shadow_bounds(body: QRect) -> QRect:
    """投影可能落到的范围；左右按未内收的宽度算，重画区域取保守值。"""
    return body.adjusted(
        -SHADOW_SPREAD,
        SHADOW_OFFSET - SHADOW_SPREAD,
        SHADOW_SPREAD,
        SHADOW_OFFSET + SHADOW_SPREAD,
    )


def paint_soft_shadow(
    painter: QPainter, body: QRectF, radius: float, canvas: QRectF
) -> None:
    """在 ``body``（圆角半径 ``radius``）外画一圈柔和投影，不画出 ``canvas``。"""
    outside = QPainterPath()
    outside.addRect(canvas)
    inner = QPainterPath()
    inner.addRoundedRect(body, radius, radius)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # 只画在表面外面：底色半透明时，阴影不该从表面里透出来
    painter.setClipPath(outside.subtracted(inner))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, SHADOW_ALPHA))
    cast_rect = body.adjusted(SHADOW_INSET, SHADOW_OFFSET, -SHADOW_INSET, SHADOW_OFFSET)
    for grow in range(SHADOW_SPREAD, 0, -1):
        painter.drawRoundedRect(
            cast_rect.adjusted(-grow, -grow, grow, grow), radius + grow, radius + grow
        )
    painter.restore()
