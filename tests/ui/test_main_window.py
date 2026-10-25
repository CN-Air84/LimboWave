from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QRect, SignalInstance
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from limbowave.ui.chat_view import ChatView
from limbowave.ui.main_window import MainWindow


def test_window_title_identifies_app(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    assert "LimboWave" in window.windowTitle()


def test_window_holds_no_public_state(qtbot: QtBot) -> None:
    """架构守卫：主窗口的公开属性只允许是信号，不得挂载业务数据。

    窗口只表达意图、展示状态；一旦有人往窗口上挂会话或消息集合，这里会立刻失败。
    """
    window = MainWindow()
    qtbot.addWidget(window)

    data_attributes = [
        name
        for name, value in vars(window).items()
        if not name.startswith("_") and not isinstance(value, SignalInstance)
    ]

    assert data_attributes == []


def test_window_has_central_widget(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    assert window.centralWidget() is not None


def test_window_exposes_command_channel(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    with qtbot.waitSignal(window.command_requested, timeout=1000) as blocker:
        window.command_requested.emit("ping")

    assert blocker.args == ["ping"]


def test_window_fits_on_small_high_dpi_screens(qtbot: QtBot) -> None:
    """高 DPI 守卫：布局的最小尺寸要能在小屏上放下。

    200% 缩放下 1280 逻辑宽的窗口需要 2560 物理像素的屏幕；笔记本常见
    1920 物理宽对应 960 逻辑宽。所以最小尺寸必须明显小于 1280——
    否则高分屏用户打开就是裁切的。
    """
    window = MainWindow()
    qtbot.addWidget(window)
    hint = window.minimumSizeHint()
    assert hint.width() <= 960, f"布局最小宽度 {hint.width()} 在 200% 缩放下会超出屏幕"
    assert hint.height() <= 700


def test_all_three_panes_visible(qtbot: QtBot) -> None:
    """三栏（导航 / 聊天 / 工具栏）都在，且都能被压缩而不消失。"""
    window = MainWindow()
    qtbot.addWidget(window)
    window.chat.add_user_message("hi")
    window.chat._advanced_btn.click()  # 高级面板只按需打开
    assert window.sidebar.isVisibleTo(window)
    assert window.chat.isVisibleTo(window)
    assert window.toolbar.isVisibleTo(window)
    for pane in (window.sidebar, window.chat, window.toolbar):
        assert pane.minimumWidth() < 400  # 没有哪个栏锁死到大宽度


def test_empty_session_centers_composer_and_collapses_advanced(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.resize(1200, 800)
    qtbot.wait(20)

    chat = window.chat
    assert not chat.docked
    assert window.toolbar.collapsed
    assert not window.toolbar.isVisible()
    composer_mid = chat._composer.mapTo(chat, QPoint(0, chat._composer.height() // 2)).y()
    assert abs(composer_mid - chat.height() // 2) <= 2

    # 高级栏开关在输入框右上角，点击原地切换展开/收起
    toggle = chat._advanced_btn
    assert toggle.isVisible()
    assert toggle.parent() is chat._composer
    assert toggle.y() <= 12
    assert chat._composer.width() - toggle.geometry().right() <= 16
    collapsed_text = toggle.text()
    toggle.click()
    qtbot.wait(20)
    assert not window.toolbar.collapsed
    assert window.toolbar.isVisible()
    assert toggle.isVisible()
    assert toggle.text() != collapsed_text
    toggle.click()
    qtbot.wait(20)
    assert window.toolbar.collapsed
    assert toggle.isVisible()
    assert toggle.text() == collapsed_text

    chat.add_user_message("hi")
    assert chat.docked
    assert window.toolbar.collapsed
    toggle.click()
    assert not window.toolbar.collapsed
    assert chat.advanced_expanded
    qtbot.waitUntil(_settled(chat, expanded=True), timeout=2000)
    assert window.toolbar.isVisible()
    _assert_over_composer(window)

    chat.clear_transcript()
    assert not chat.docked
    assert window.toolbar.collapsed
    assert not chat.advanced_expanded
    qtbot.waitUntil(lambda: chat.composer_lift > 0, timeout=2000)


def test_opening_history_from_empty_session_keeps_advanced_collapsed(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.resize(1200, 800)
    qtbot.wait(20)

    chat = window.chat
    assert not chat.docked
    assert window.toolbar.collapsed

    chat.load_history([("user", "old message", "", "m1")])

    assert chat.docked
    assert window.toolbar.collapsed
    assert not chat.advanced_expanded
    qtbot.waitUntil(_settled(chat, expanded=False), timeout=2000)
    assert not window.toolbar.isVisible()


def _settled(chat: ChatView, *, expanded: bool) -> Callable[[], bool]:
    """输入区域已落到底部，高级栏显隐状态也已同步。"""
    progress = 1.0 if expanded else 0.0
    return lambda: chat.composer_lift == 0 and chat.advanced_progress == progress


def _geometry_in_host(window: MainWindow, widget: QWidget) -> QRect:
    return QRect(widget.mapTo(window._content_host, QPoint(0, 0)), widget.size())


def _assert_centered(window: MainWindow, left: int, right: int) -> None:
    host = window._content_host.rect()
    assert abs((left - host.left()) - (host.right() - right)) <= 1


def _assert_over_composer(window: MainWindow) -> None:
    """高级区域叠盖输入框右半部，不挤窄或移动输入框。"""
    panel = _geometry_in_host(window, window.toolbar)
    composer = _geometry_in_host(window, window.chat._composer)
    assert composer.contains(panel)
    assert abs(panel.width() - composer.width() / 2) <= 1
    assert panel.right() == composer.right()
    assert panel.bottom() == composer.bottom()
    _assert_centered(window, composer.left(), composer.right())


def test_advanced_toggle_keeps_composer_and_transcript_geometry(qtbot: QtBot) -> None:
    """展开仅叠盖输入框，不改变输入框、侧栏和消息区尺寸。"""
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.resize(1200, 800)
    chat = window.chat
    chat.add_user_message("hi")
    chat._advanced_btn.click()
    qtbot.waitUntil(_settled(chat, expanded=True), timeout=2000)

    # A user-adjusted sidebar must keep its width across both transitions.
    window._splitter.setSizes([350, 850])
    qtbot.wait(20)
    sidebar_width = window.sidebar.width()
    composer_width = chat._composer.width()
    viewport_width = chat._scroll.viewport().width()
    for collapsed in (True, False, True):
        window.toolbar.set_collapsed(collapsed)
        qtbot.waitUntil(_settled(chat, expanded=not collapsed), timeout=2000)
        assert abs(window.sidebar.width() - sidebar_width) <= 1
        assert chat._composer.width() == composer_width
        assert chat._scroll.viewport().width() == viewport_width
        assert window.toolbar.isVisible() is not collapsed
        if collapsed:
            composer = _geometry_in_host(window, chat._composer)
            _assert_centered(window, composer.left(), composer.right())
        else:
            _assert_over_composer(window)


def test_advanced_slide_reverses_from_current_progress(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.chat.add_user_message("hi")
    chat = window.chat
    qtbot.waitUntil(lambda: chat._dock_anim is None and chat.composer_lift == 0)
    # The lift can round to zero one tick before the final layout is applied.
    # Record settled composer geometry, not the preceding docking frame.
    chat.layout().activate()
    before = chat._composer.geometry()
    transcript = chat._scroll.geometry()
    previous = 0.0
    for expanded in (True, False, True):
        window.toolbar.set_collapsed(not expanded)
        animation = chat._advanced_anim
        assert animation is not None
        animation.pause()
        assert chat.advanced_progress == previous
        animation.setCurrentTime(animation.duration() // 2)
        progress = chat.advanced_progress
        assert min(previous, float(expanded)) < progress < max(previous, float(expanded))
        clip = chat._advanced_clip
        assert clip is not None and clip.isVisible()
        assert 0 < window.toolbar.x() < clip.width()
        assert window.toolbar.x() == round(clip.width() * (1.0 - progress))
        assert chat._composer.geometry() == before
        assert chat._scroll.geometry() == transcript
        assert clip.mask().boundingRect().width() < clip.width()
        assert window.toolbar.graphicsEffect() is None
        previous = progress
    animation.setCurrentTime(animation.duration())
    assert chat.advanced_progress == 1.0
    assert window.toolbar.x() == 0
    _assert_over_composer(window)
    window.toolbar.set_collapsed(True)
    animation = chat._advanced_anim
    assert animation is not None
    animation.pause()
    animation.setCurrentTime(animation.duration() // 2)
    assert window.toolbar.isVisible()
    animation.setCurrentTime(animation.duration())
    assert not window.toolbar.isVisible()
    assert not chat._advanced_clip.isVisible()


def test_advanced_overlay_tracks_growing_composer(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.chat.add_user_message("hi")
    window.chat._advanced_btn.click()
    qtbot.waitUntil(_settled(window.chat, expanded=True))
    for text in ("short draft", "a line\n" * 12, "short again"):
        window.chat._input.setPlainText(text)
        qtbot.wait(80)
        _assert_over_composer(window)
        panel = _geometry_in_host(window, window.toolbar)
        assert not panel.intersects(_geometry_in_host(window, window.chat._scroll))


def test_narrow_window_does_not_auto_open_advanced(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.resize(850, 800)
    qtbot.wait(20)
    window.chat.add_user_message("hi")

    assert window.toolbar.collapsed
    assert not window.toolbar.isVisible()
    assert not window.chat.advanced_expanded
    assert window.chat._advanced_btn.isVisible()


def test_advanced_region_does_not_change_transcript_wrapping(qtbot: QtBot) -> None:
    """叠盖区域不占布局空间，不影响消息换行，并保持贴底。"""
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.resize(1200, 800)
    chat = window.chat
    chat.load_history([("user", f"消息 {index}", "", None) for index in range(40)])
    qtbot.waitUntil(lambda: chat.composer_lift == 0, timeout=2000)
    bar = chat._scroll.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.value() == bar.maximum())
    qtbot.wait(50)
    original = (chat._scroll.geometry(), chat._rows[-1].size(), bar.maximum())
    for expanded in (True, False, True):
        chat._advanced_btn.click()
        qtbot.waitUntil(_settled(chat, expanded=expanded), timeout=2000)
        qtbot.wait(50)
        assert (chat._scroll.geometry(), chat._rows[-1].size(), bar.maximum()) == original
        assert bar.value() == bar.maximum()


def test_full_page_replaces_entire_workspace(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QLabel

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.chat.add_user_message("hi")
    page = QLabel("settings")
    window.show_full_page(page)

    assert window.showing_full_page
    assert page.isVisibleTo(window)
    assert not window.sidebar.isVisibleTo(window)
    assert not window.chat.isVisibleTo(window)
    assert not window.toolbar.isVisibleTo(window)

    window.show_workspace()
    assert not window.showing_full_page
    assert window.sidebar.isVisibleTo(window)
    assert window.chat.isVisibleTo(window)
    assert not window.toolbar.isVisibleTo(window)


def test_settings_transition_fades_and_slides_both_directions(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QLabel

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    window.show_full_page(QLabel("settings"))
    entering = window._transition
    assert entering is not None
    assert entering._direction == 1
    assert not entering._outgoing.isNull()
    assert entering._incoming_widget is window._root_stack.currentWidget()
    assert entering._incoming_widget.pos().x() == 0
    qtbot.waitUntil(lambda: entering._progress > 0, timeout=1000)
    qtbot.waitUntil(lambda: window._transition is None, timeout=1500)
    assert window.showing_full_page

    window.show_workspace()
    leaving = window._transition
    assert leaving is not None
    assert leaving._direction == -1
    assert leaving._incoming_widget is window._workspace
    assert leaving._incoming_widget.pos().x() == 0
    qtbot.waitUntil(lambda: leaving._progress > 0, timeout=1000)
    qtbot.waitUntil(lambda: window._transition is None, timeout=1500)
    assert not window.showing_full_page
    assert window.sidebar.isVisibleTo(window)


def test_settings_transition_can_be_interrupted_or_resized(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QLabel

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    window.show_full_page(QLabel("first"))
    first = window._transition
    window.show_workspace()
    assert first is not window._transition
    window.show_full_page(QLabel("second"))
    assert window.showing_full_page
    assert window._root_stack.count() == 2
    window.resize(1100, 750)
    qtbot.waitUntil(lambda: window._transition is None, timeout=1000)
    assert window.showing_full_page


def test_transition_snapshot_caps_high_dpi_frame_without_losing_content(qtbot: QtBot) -> None:
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QWidget

    from limbowave.ui.main_window import _MAX_TRANSITION_PIXELS, _page_snapshot

    class HighDpiPage(QWidget):
        def devicePixelRatioF(self) -> float:
            return 2.0

    page = HighDpiPage()
    qtbot.addWidget(page)
    page.resize(2000, 1200)
    page.setAutoFillBackground(True)
    palette = page.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#ed315e"))
    page.setPalette(palette)

    frame = _page_snapshot(page)
    assert frame.width() * frame.height() <= _MAX_TRANSITION_PIXELS + 3000
    assert frame.devicePixelRatio() < 2.0
    assert frame.toImage().pixelColor(frame.width() // 2, frame.height() // 2) == QColor("#ed315e")


def test_settings_page_can_be_retained_without_reconstruction(qtbot: QtBot) -> None:
    from PySide6.QtWidgets import QLabel

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    page = QLabel("settings")
    window.show_full_page(page)
    qtbot.waitUntil(lambda: window._transition is None, timeout=1500)

    window.show_workspace(release_page=False)
    qtbot.waitUntil(lambda: window._transition is None, timeout=1500)
    assert window._root_stack.indexOf(page) >= 0
    window.show_full_page(page)
    assert window._full_page is page
    assert window._root_stack.count() == 2


def test_thinking_popup_follows_theme_switch(qtbot: QtBot) -> None:
    """回归：高级栏「思考」下拉的弹层必须跟随主题深浅切换。

    曾缺陷：``_body`` / ``content_host`` 的无选择器 ``background`` 规则会级联
    进弹层（弹层窗口是 combo 的子对象），按 Qt 样式表优先级压过全局 QSS 的
    ``QComboBox QAbstractItemView``——弹层不再画主题底色，烘死在启动时的
    配色上，切浅色后仍是一块深色。
    """
    from PySide6.QtWidgets import QApplication

    from limbowave.domain.appearance import BUILTIN_THEMES
    from limbowave.ui import theme

    dark = BUILTIN_THEMES[0]
    light = next(d for d in BUILTIN_THEMES if d.id == "builtin:clear-day")
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    old_css = app.styleSheet()

    def popup_background() -> str:
        combo = window._toolbar._thinking
        combo.showPopup()
        qtbot.wait(300)  # 上拉进场动效 150ms，等它播完再采样
        image = combo.view().grab().toImage()
        # Sample the empty right side of an unselected row, not its text.
        row = (combo.currentIndex() + 1) % combo.count()
        rect = combo.view().visualRect(combo.model().index(row, 0))
        color = image.pixelColor(image.width() - 16, rect.center().y())
        combo.hidePopup()
        qtbot.wait(200)
        return color.name().upper()

    def switch(definition) -> None:
        previous = theme.current_palette()
        old_card = theme.card_surface()
        old_sizes = (theme.FS_TINY, theme.FS_SMALL, theme.FS_BASE, theme.FS_TITLE)
        theme.apply_appearance_theme(definition)
        app.setStyleSheet(theme.app_stylesheet())
        theme.refresh_inline_styles(
            window, previous, old_sizes, old_card_surface=old_card
        )
        window.set_appearance_theme(definition)
        qtbot.wait(50)

    theme.apply_appearance_theme(dark)
    app.setStyleSheet(theme.app_stylesheet())
    try:
        window = MainWindow()
        qtbot.addWidget(window)
        window.show()
        qtbot.wait(100)

        window.chat._advanced_btn.click()
        qtbot.waitUntil(lambda: window.chat.advanced_progress == 1.0)

        # 深色启动：弹层画主题的浮层底色，而不是系统擦除色/祖先烘死的底色
        assert popup_background() == theme.BG_ELEVATED.upper()

        # 切浅色（复刻 app._apply_appearance 的核心步骤）后必须跟随
        switch(light)
        assert popup_background() == theme.BG_ELEVATED.upper()
    finally:
        theme.apply_appearance_theme(dark)
        app.setStyleSheet(old_css)
