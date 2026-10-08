"""首次使用演示：文案绑定、三步主路径、失败屏停留、自定义地址。"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot

from limbowave.ui.oobe_copy import (
    BAD_URL,
    MORE_PROVIDERS,
    NEED_KEY,
    NEED_PROVIDER,
    NEED_URL,
    RAIL_GROUPS,
    SCREENS,
    greeting_for_hour,
    mask_middle,
    present,
    provider_by_id,
)
from limbowave.ui.oobe_demo import OobeDemo, valid_http_url


def _window(qtbot: QtBot) -> OobeDemo:
    window = OobeDemo()
    qtbot.addWidget(window)
    window.resize(1080, 740)
    window.show()
    return window


def test_rail_lists_every_screen_once() -> None:
    listed = [screen_id for _heading, screen_ids in RAIL_GROUPS for screen_id in screen_ids]
    assert listed == list(SCREENS)
    assert len(listed) == len(set(listed))


def test_sentences_fill_provider_name_and_url() -> None:
    kimi = provider_by_id("kimi")
    success = present("success", kimi)
    assert "Kimi" in success.body
    network = present("error_network", kimi, url="https://api.moonshot.cn/v1")
    assert "https://api.moonshot.cn/v1" in network.body
    missing = present("error_network", provider_by_id("custom"))
    assert "尚未填写" in missing.body
    refused = present("error_key", provider_by_id("custom"))
    assert "这家服务商并没有接受" in refused.body


def test_valid_http_url_rejects_bare_scheme() -> None:
    assert valid_http_url("https://api.example.com/v1")
    assert valid_http_url("http://127.0.0.1:8080")
    assert not valid_http_url("https://")
    assert not valid_http_url("ftp://example.com")
    assert not valid_http_url("  ")


def test_each_screen_shows_its_own_title(qtbot: QtBot) -> None:
    window = _window(qtbot)
    for screen_id in SCREENS:
        window.show_screen(screen_id)
        copy = present(screen_id, window._provider(), url=window._current_url())
        assert window._title.text() == copy.title
        assert window._primary.text() == copy.primary
        assert window._rail_buttons[screen_id].isChecked()


def test_rail_check_does_not_auto_continue(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("checking")
    assert window.screen_id == "checking"
    assert not window._check_timer.isActive()


def test_greeting_follows_the_local_hour() -> None:
    assert greeting_for_hour(4) == "夜深了……"
    assert greeting_for_hour(5) == "早上好！"
    assert greeting_for_hour(10) == "早上好！"
    assert greeting_for_hour(11) == "中午好！"
    assert greeting_for_hour(12) == "中午好！"
    assert greeting_for_hour(13) == "下午好~"
    assert greeting_for_hour(17) == "下午好~"
    assert greeting_for_hour(18) == "晚上好，"
    assert greeting_for_hour(22) == "晚上好，"
    assert greeting_for_hour(23) == "夜深了……"
    filled = present("intro", provider_by_id("deepseek"), hour=11)
    assert filled.title == "中午好！\n欢迎回到LimboWave。"


def test_mask_middle_keeps_only_the_ends() -> None:
    assert mask_middle("  ") == ""
    assert mask_middle("ab") == "****"
    assert mask_middle("sk-demo") == "s****o"
    assert mask_middle("sk-1234567890abcd") == "sk-1****abcd"


def test_opens_on_the_product_intro(qtbot: QtBot) -> None:
    window = _window(qtbot)
    assert window.screen_id == "intro"
    copy = present("intro", window._provider(), hour=None)
    assert window._title.text() == copy.title
    assert window._title.text().endswith("欢迎回到LimboWave。")
    assert not window._eyebrow.isVisible()
    paragraphs = [
        label.text()
        for label in window.findChildren(QLabel)
        if label.objectName() == "oobeIntroParagraph"
    ]
    assert paragraphs == [
        "放下心来，倾诉心中所想；让芯连心，倾听灵魂的声音。\n"
        "认出纯洁的你、记住真正的你——仅此而已。\n"
        "LimboWave/灵波，共鸣，共振。"
    ]
    qtbot.mouseClick(window._primary, Qt.MouseButton.LeftButton)
    assert window.screen_id == "welcome"
    qtbot.keyClick(window, Qt.Key.Key_Escape)
    assert window.screen_id == "intro"


def test_preview_happy_path_requires_a_selected_model(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("welcome")
    qtbot.mouseClick(window._primary, Qt.MouseButton.LeftButton)
    assert window.screen_id == "provider"
    assert not window._primary.isEnabled()
    assert window._hint.text() == NEED_PROVIDER

    qtbot.mouseClick(window._rows["kimi"], Qt.MouseButton.LeftButton)
    assert window._primary.isEnabled()
    qtbot.mouseClick(window._primary, Qt.MouseButton.LeftButton)
    assert window.screen_id == "key"
    assert "Kimi" in window._body.text()
    assert window._address.text() == "https://api.moonshot.cn/v1"
    assert window._hint.text() == NEED_KEY

    window._key.setText("  sk-demo  ")
    assert window._primary.isEnabled()
    qtbot.mouseClick(window._primary, Qt.MouseButton.LeftButton)
    assert window.screen_id == "checking"
    assert "https://api.moonshot.cn/v1" in window._body.text()
    assert window._secret.text() == "s****o"
    assert "sk-demo" not in window._secret.text()
    assert not window._primary.isEnabled()
    qtbot.waitUntil(lambda: window.screen_id == "models", timeout=2000)
    assert not window._primary.isEnabled()
    qtbot.mouseClick(window.actual_models._rows["demo-chat"].add, Qt.MouseButton.LeftButton)
    assert not window._primary.isEnabled()
    window._default_model.setCurrentIndex(0)
    qtbot.mouseClick(window._primary, Qt.MouseButton.LeftButton)
    assert window.screen_id == "success"
    assert "Kimi" in window._body.text()

    qtbot.mouseClick(window._primary, Qt.MouseButton.LeftButton)
    assert window.screen_id == "done"


def test_custom_provider_needs_a_real_url(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("provider")
    qtbot.mouseClick(window._rows["custom"], Qt.MouseButton.LeftButton)
    qtbot.mouseClick(window._primary, Qt.MouseButton.LeftButton)
    assert window.screen_id == "key_custom"
    assert not window._address.isVisible()
    assert window._url.isVisible()

    window._key.setText("secret")
    assert window._hint.text() == NEED_URL
    assert not window._primary.isEnabled()
    window._url.setText("not a url")
    assert window._hint.text() == BAD_URL
    window._url.setText("https://relay.example/v1")
    assert window._primary.isEnabled()


def test_empty_key_can_still_check(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("key")
    assert window._hint.text() == NEED_KEY
    assert window._primary.isEnabled()
    qtbot.mouseClick(window._primary, Qt.MouseButton.LeftButton)
    assert window.screen_id == "checking"
    assert not window._secret.isVisible()


def test_custom_empty_key_only_needs_a_url(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("key_custom")
    assert window._hint.text() == NEED_URL
    assert not window._primary.isEnabled()
    window._url.setText("https://relay.example/v1")
    assert window._hint.text() == NEED_KEY
    assert window._primary.isEnabled()


def test_more_providers_are_the_last_combo_on_the_left(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("provider")
    combo = window._more
    assert combo.placeholderText() == MORE_PROVIDERS
    assert combo.currentIndex() == -1
    assert [combo.itemData(index) for index in range(combo.count())] == [
        "kimi-intl",
        "glm-intl",
        "minimax-intl",
        "mimo",
        "stepfun",
        "sensenova",
    ]
    left = combo.parent()
    assert left is not None
    assert window._rows["kimi"].parent() is not left
    assert window._rows["custom"].parent() is not left
    left_layout = left.layout()
    assert left_layout is not None
    last = left_layout.itemAt(left_layout.count() - 1)
    assert last is not None
    assert last.widget() is combo

    combo.setCurrentIndex(combo.findData("mimo"))
    assert window._selected == "mimo"
    assert window._primary.isEnabled()
    qtbot.mouseClick(window._rows["kimi"], Qt.MouseButton.LeftButton)
    assert window._selected == "kimi"
    assert combo.currentIndex() == -1


def test_escape_steps_back_without_rechecking(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("success")
    qtbot.keyClick(window, Qt.Key.Key_Escape)
    assert window.screen_id == "key"
    assert not window._check_timer.isActive()


def test_skip_path_explains_chat_stays_closed(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("welcome")
    qtbot.mouseClick(window._secondary, Qt.MouseButton.LeftButton)
    assert window.screen_id == "browse"
    qtbot.mouseClick(window._secondary, Qt.MouseButton.LeftButton)
    assert window.screen_id == "later"
    assert "等连接完成" in window._body.text()


def test_live_check_waits_instead_of_pretending_success(qtbot: QtBot) -> None:
    window = OobeDemo(preview=False)
    qtbot.addWidget(window)
    window.resize(1080, 740)
    window.show()
    seen: list[tuple[int, str, str]] = []
    window.check_requested.connect(lambda token, url, secret: seen.append((token, url, secret)))
    window.show_screen("key")
    window._key.setText("sk-live")
    qtbot.mouseClick(window._primary, Qt.MouseButton.LeftButton)
    assert window.screen_id == "checking"
    assert not window._check_timer.isActive()
    assert seen == [(seen[0][0], "https://api.deepseek.com", "sk-live")]
    assert "sk-live" not in window._body.text()

    finished: list[bool] = []
    window.finished.connect(lambda: finished.append(True))
    window.show_screen("success")
    qtbot.mouseClick(window._primary, Qt.MouseButton.LeftButton)
    assert finished == [True]
    assert window.screen_id == "success"


def test_live_skip_leaves_without_the_demo_ending(qtbot: QtBot) -> None:
    window = OobeDemo(preview=False)
    qtbot.addWidget(window)
    window.show()
    skipped: list[bool] = []
    window.skipped.connect(lambda: skipped.append(True))
    window.show_screen("welcome")
    qtbot.mouseClick(window._secondary, Qt.MouseButton.LeftButton)
    assert window.screen_id == "browse"
    qtbot.mouseClick(window._secondary, Qt.MouseButton.LeftButton)
    assert skipped == [True]
    assert window.screen_id == "browse"


@pytest.mark.parametrize("preview", [False, True])
def test_screen_change_animates_the_whole_column(qtbot: QtBot, preview: bool) -> None:
    window = OobeDemo(preview=preview)
    qtbot.addWidget(window)
    window.resize(1080, 740)
    window.show()
    pages = window._pages
    assert pages._animation is None

    window.show_screen("welcome")
    assert window.screen_id == "welcome"
    assert pages._stack.isAncestorOf(window._title)
    assert pages._stack.isAncestorOf(window._primary)
    assert pages._outgoing is not None
    assert pages._animation is not None
    assert pages._effect.opacity() == 0.0
    pages._animation.setCurrentTime(80)
    assert pages._outgoing.y() < 0
    pages._animation.setCurrentTime(160)
    assert pages._outgoing is None
    assert pages._stack.y() == 18
    pages._animation.setCurrentTime(100)
    assert 0.0 < pages._effect.opacity() < 1.0
    assert 0 < pages._stack.y() < 18
    qtbot.waitUntil(lambda: pages._animation is None)
    assert pages._stack.pos() == QPoint(0, 0)
    assert not pages._effect.isEnabled()
    assert window._primary.isEnabled()


def test_escape_reverses_transition_without_losing_key(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("key")
    window._key.setText("sk-retained")
    window.show_screen("checking")
    qtbot.keyClick(window, Qt.Key.Key_Escape)
    assert window.screen_id == "key"
    assert window._key.text() == "sk-retained"
    pages = window._pages
    assert pages._animation is not None
    pages._animation.setCurrentTime(80)
    assert pages._outgoing is not None
    assert pages._outgoing.y() > 0
    pages._animation.setCurrentTime(160)
    assert pages._stack.y() == -18
    qtbot.waitUntil(lambda: pages._animation is None)
    assert not window._check_timer.isActive()


def test_status_screens_animate_even_when_the_inner_page_is_unchanged(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("checking")
    index = window._stack.currentIndex()
    window.show_screen("success")
    assert window._stack.currentIndex() == index
    assert window._pages._animation is not None
    assert window._pages._outgoing is not None
    assert window._title.text() == present("success", window.provider()).title


def test_rapid_navigation_finishes_on_the_latest_screen(qtbot: QtBot) -> None:
    window = _window(qtbot)
    for screen in ("welcome", "provider", "key", "checking", "error_key", "provider"):
        window.show_screen(screen)
        assert window._pages._animation is not None
        window._pages._animation.setCurrentTime(60)
    qtbot.waitUntil(lambda: window._pages._animation is None)
    assert window.screen_id == "provider"
    assert window._pages._outgoing is None
    assert window._pages._stack.pos() == QPoint(0, 0)
    assert window._column.width() == 720
    assert window._column.geometry().right() < window._pages.width()


def test_same_screen_refresh_does_not_start_a_transition(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("intro")
    assert window._pages._animation is None
    window.show_screen("key")
    qtbot.waitUntil(lambda: window._pages._animation is None)
    window._key.setText("sk-edit")
    window.show_screen("key")
    assert window._pages._animation is None
    assert window._key.text() == "sk-edit"


def test_hidden_oobe_clears_transition_and_reopens_without_a_snapshot(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("provider")
    assert window._pages._animation is not None
    window.hide()
    assert window._pages._animation is None
    assert window._pages._outgoing is None
    assert not window._pages._effect.isEnabled()
    window.show_screen("welcome")
    window.show()
    assert window._pages._animation is None
    assert window.screen_id == "welcome"
    assert window._column.width() == 480
    assert window._primary.isVisible()


@pytest.mark.parametrize("incoming", [False, True])
def test_transition_does_not_intercept_button_clicks(qtbot: QtBot, incoming: bool) -> None:
    window = _window(qtbot)
    window.show_screen("welcome")
    pages = window._pages
    if incoming:
        assert pages._animation is not None
        pages._animation.setCurrentTime(160)
    # A direct qtbot click bypasses hit-testing and would miss an opaque overlay.
    center = window._primary.mapTo(pages, window._primary.rect().center())
    assert pages.childAt(center) is window._primary


def test_transition_keeps_the_stage_background_transparent(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.setStyleSheet("QWidget { background: #010203; }" + window.styleSheet())
    window.show_screen("welcome")
    pages = window._pages
    assert pages._outgoing is not None
    outgoing = pages._outgoing.pixmap().toImage()
    assert outgoing.pixelColor(outgoing.width() - 2, outgoing.height() - 2).alpha() == 0
    qtbot.waitUntil(lambda: pages._animation is None)
    current = pages._stack.grab().toImage()
    assert current.pixelColor(current.width() - 2, current.height() - 2).alpha() == 0


def test_resize_during_transition_preserves_layout_and_input_focus(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("provider")
    window._select_provider("kimi")
    qtbot.mouseClick(window._primary, Qt.MouseButton.LeftButton)
    assert window.screen_id == "key"
    window.resize(1260, 820)
    qtbot.keyClicks(window._key, "sk-during-transition")
    qtbot.waitUntil(lambda: window._pages._animation is None)
    assert window._key.hasFocus()
    assert window._key.text() == "sk-during-transition"
    assert window._pages._stack.geometry() == window._pages.rect()
    assert window._column.width() == 480


@pytest.mark.parametrize("screen_id", ["key", "key_custom"])
def test_provider_form_has_an_editable_display_name(qtbot: QtBot, screen_id: str) -> None:
    window = _window(qtbot)
    window.show_screen(screen_id)
    field = window._display_name
    assert field.isVisible()
    assert field.accessibleName() == "显示名"
    assert not field.isReadOnly()
    assert field.text() == ("" if screen_id == "key_custom" else "DeepSeek")
    assert field.placeholderText()
    field.setText("  我的服务商  ")
    assert window.display_name() == "我的服务商"
    if screen_id == "key_custom":
        window._url.setText("https://relay.example/v1")
    qtbot.keyClick(field, Qt.Key.Key_Return)
    assert window.screen_id == "checking"
    window.show_screen("success")
    assert "我的服务商" in window._body.text()
    qtbot.keyClick(window, Qt.Key.Key_Escape)
    assert field.text() == "  我的服务商  "


def test_display_name_drafts_are_kept_separately_for_each_provider(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("key")
    window._display_name.setText("DeepSeek 工作")
    window.show_screen("key_custom")
    assert window._display_name.text() == ""
    window._display_name.setText("我的中转")
    window.show_screen("provider")
    window._select_provider("kimi")
    window.show_screen("key")
    assert window._display_name.text() == "Kimi"
    window.show_screen("key_custom")
    assert window._display_name.text() == "我的中转"
    window.show_screen("key")
    assert window._display_name.text() == "DeepSeek 工作"
    window._display_name.clear()
    window.show_screen("key_custom")
    window.show_screen("key")
    assert window._display_name.text() == ""
    assert window._primary.isEnabled()


def test_preview_rail_models_uses_mock_catalog_and_retains_choice(qtbot: QtBot) -> None:
    window = _window(qtbot)
    window.show_screen("models")
    assert list(window.actual_models._rows) == ["demo-chat", "demo-reasoner"]
    window.actual_models._rows["demo-chat"].add.click()
    window._default_model.setCurrentIndex(0)
    window._primary.click()
    assert window.screen_id == "success"
    window._secondary.click()
    qtbot.waitUntil(lambda: window.screen_id == "models", timeout=2000)
    assert window._default_model.currentData() == "demo-chat"
    assert window._primary.isEnabled()
