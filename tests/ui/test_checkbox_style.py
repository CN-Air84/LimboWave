from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QCheckBox, QStyle, QStyleOptionButton, QWidget
from pytestqt.qtbot import QtBot

from limbowave.domain.appearance import MaterialSettings
from limbowave.ui import theme
from limbowave.ui.checkbox_style import CheckBoxStyle, _CheckTransition
from limbowave.ui.theme_effects import install_hover_suspension


def _box(qtbot: QtBot, *, checked: bool = False) -> tuple[QCheckBox, CheckBoxStyle]:
    style = CheckBoxStyle()
    box = QCheckBox("option")
    box.setStyle(style)
    box.setChecked(checked)
    box.resize(160, 28)
    qtbot.addWidget(box)
    box.show()
    qtbot.waitExposed(box)
    box._test_style = style  # type: ignore[attr-defined]  # keep the Python wrapper alive
    return box, style


def _transition(box: QCheckBox) -> _CheckTransition:
    box.grab()  # the transition is created on first paint
    found = box.findChild(QObject, "limbowaveCheckTransition")
    assert isinstance(found, _CheckTransition)
    return found


def _indicator_rect(box: QCheckBox, style: CheckBoxStyle):
    option = QStyleOptionButton()
    option.initFrom(box)
    return style.subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, box)


def test_unchecked_and_checked_both_draw_a_visible_box(qtbot: QtBot) -> None:
    for checked in (False, True):
        box, style = _box(qtbot, checked=checked)
        rect = _indicator_rect(box, style)
        image = box.grab().toImage()
        scale = image.devicePixelRatio()
        middle = round(rect.center().y() * scale)
        edge = image.pixelColor(round(rect.left() * scale), middle)
        outside = image.pixelColor(max(0, round((rect.left() - 2) * scale)), middle)
        assert edge != outside, checked


def test_toggle_animates_in_both_directions(qtbot: QtBot) -> None:
    box, _ = _box(qtbot)
    transition = _transition(box)
    assert transition.fill == 0.0  # first paint lands on the current state, no animation

    box.setChecked(True)
    box.grab()
    animation = transition._fill_animation
    assert animation.state() == animation.State.Running
    animation.setCurrentTime(animation.duration() // 2)
    assert 0.0 < transition.fill < 1.0
    animation.setCurrentTime(animation.duration())
    assert transition.fill == 1.0

    box.setChecked(False)
    box.grab()
    assert animation.state() == animation.State.Running
    assert animation.duration() == transition.UNCHECK_MS
    animation.setCurrentTime(animation.duration() // 2)
    assert 0.0 < transition.fill < 1.0
    # The check mark retracts instead of vanishing while the fill fades out.
    assert transition.glyph == Qt.CheckState.Checked
    animation.setCurrentTime(animation.duration())
    assert transition.fill == 0.0


def test_hover_only_emphasises_frame_not_surface(qtbot: QtBot) -> None:
    box, style = _box(qtbot)
    transition = _transition(box)
    rect = _indicator_rect(box, style)
    before = box.grab().toImage()

    QApplication.sendEvent(box, QEvent(QEvent.Type.Enter))
    transition._hover_animation.setCurrentTime(transition.HOVER_ENTER_MS)
    assert transition.hover == 1.0
    after = box.grab().toImage()
    scale = after.devicePixelRatio()
    # Label area and indicator interior keep their surface; only the stroke changes.
    label = (round((rect.right() + 20) * scale), 1)
    inside = (round(rect.center().x() * scale), round(rect.center().y() * scale))
    for x, y in (label, inside):
        assert after.pixelColor(x, y) == before.pixelColor(x, y)
    edge = (round(rect.left() * scale), round(rect.center().y() * scale))
    assert after.pixelColor(*edge) != before.pixelColor(*edge)


def test_hover_suspension_leaves_checkboxes_alone(qtbot: QtBot) -> None:
    root = QWidget()
    box = QCheckBox("option", root)
    qtbot.addWidget(root)
    install_hover_suspension(root, MaterialSettings(hover_enter_ms=0, hover_restore_ms=0))
    QApplication.sendEvent(box, QEvent(QEvent.Type.Enter))
    assert not box.property("limbowaveHoverFilter")
    assert box.styleSheet() == ""


def test_disabled_checked_box_does_not_use_accent(qtbot: QtBot) -> None:
    box, style = _box(qtbot, checked=True)
    box.setEnabled(False)
    rect = _indicator_rect(box, style)
    image = box.grab().toImage()
    scale = image.devicePixelRatio()
    corner = image.pixelColor(round((rect.left() + 3) * scale), round((rect.top() + 3) * scale))
    assert corner != QColor(theme.ACCENT)
