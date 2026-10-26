"""Reversible rich-text typography for the local Zen presentation mode."""
from __future__ import annotations

from PySide6.QtGui import QFont, QTextBlockFormat, QTextCursor, QTextDocument, QTextFormat

from limbowave.ui import theme

ZEN_COLUMN_WIDTH = 720
ZEN_MESSAGE_SPACING = 32
ZEN_LINE_HEIGHT = 160
_ORIGINAL_HEIGHT = int(QTextFormat.Property.UserProperty) + 120
_ORIGINAL_HEIGHT_TYPE = _ORIGINAL_HEIGHT + 1


def apply_zen_typography(document: QTextDocument, enabled: bool) -> None:
    """Keep text, links, selection, lists and code intact; only change reading metrics.

    Block properties travel with streamed paragraphs. Reapply after setHtml, which
    replaces block formatting, and restore the original line height when leaving.
    """
    normal_font = document.property("zenNormalFont")
    if enabled:
        if not isinstance(normal_font, QFont):
            normal_font = document.defaultFont()
            document.setProperty("zenNormalFont", normal_font)
        font = QFont(normal_font)
        font.setPixelSize(theme.FS_BASE + 1)
        document.setDefaultFont(font)
    elif isinstance(normal_font, QFont):
        document.setDefaultFont(normal_font)
        document.setProperty("zenNormalFont", None)
    else:
        return

    cursor = QTextCursor(document)
    cursor.beginEditBlock()
    block = document.begin()
    while block.isValid():
        fmt = block.blockFormat()
        if enabled:
            if not fmt.hasProperty(_ORIGINAL_HEIGHT):
                fmt.setProperty(_ORIGINAL_HEIGHT, fmt.lineHeight())
                fmt.setProperty(_ORIGINAL_HEIGHT_TYPE, fmt.lineHeightType())
            fmt.setLineHeight(
                ZEN_LINE_HEIGHT, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
            )
        elif fmt.hasProperty(_ORIGINAL_HEIGHT):
            fmt.setLineHeight(fmt.doubleProperty(_ORIGINAL_HEIGHT),
                              fmt.intProperty(_ORIGINAL_HEIGHT_TYPE))
            fmt.clearProperty(_ORIGINAL_HEIGHT)
            fmt.clearProperty(_ORIGINAL_HEIGHT_TYPE)
        cursor.setPosition(block.position())
        cursor.setBlockFormat(fmt)
        block = block.next()
    cursor.endEditBlock()
