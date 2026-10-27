"""Acceptance checks for scaffolded, non-persistent model configuration practice."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from limbowave.domain.routing import RoutingError
from tools.demos.model_learning_demo import LESSONS, ModelLearningDemo, ModelSandbox


@pytest.fixture
def lesson(qtbot, qapp):
    from limbowave.ui import theme

    palette = theme.current_palette().name
    style = qapp.styleSheet()
    view = ModelLearningDemo()
    qtbot.addWidget(view)
    view.show()
    qtbot.waitUntil(view.isVisible)
    yield view
    view.close()
    theme.set_palette(palette)
    qapp.setStyleSheet(style)


def solve(view):
    if view.step == 0:
        view.choices["endpoint"].click()
    elif view.step == 1:
        view.choices["b"].click()
    elif view.step == 2:
        view.logical_id_input.setText("xyz-chat")
        view.name_input.setText("XYZ Chat")
        view.create_button.click()
        view.check_button.click()
    elif view.step == 3:
        view.choices["bind_a"].click()
        view.choices["bind_b"].click()
    elif view.step == 4:
        view.prefer_button.click()
        view.choices["guess_b"].click()
        view.verify_button.click()
    elif view.step == 5:
        view.choices["actual"].click()
    elif view.step == 6:
        view.fail_button.click()
        view.choices["manual"].click()
        view.switch_button.click()
        view.retry_button.click()


def reach(view, target):
    while view.step < target:
        solve(view)
        assert view.next.isEnabled()
        view.next.click()


def test_every_lesson_needs_a_correct_action_not_just_next(lesson):
    for index in range(len(LESSONS)):
        assert lesson.step == index and not lesson.next.isEnabled()
        lesson.advance()
        assert lesson.step == index
        lesson.hint.click()
        assert not lesson.next.isEnabled() and index not in lesson.completed
        solve(lesson)
        assert lesson.next.isEnabled()
        lesson.next.click()
    assert lesson.step == len(LESSONS)
    assert lesson.progress.value() == len(LESSONS)
    assert lesson.sandbox.logical_name == "XYZ Chat"
    assert lesson.sandbox.logical_id == "xyz-chat"
    assert lesson.sandbox.bindings == ["b", "a"]
    assert lesson.sandbox.trace == [("b", "xyz-chat"), ("a", "xyz-chat")]
    assert "不是你的真实配置" in "".join(w.text() for w in lesson.findChildren(QLabel))


@pytest.mark.parametrize("wrong", ["actual", "logical"])
def test_wrong_answer_explains_and_does_not_advance(lesson, wrong):
    lesson.choices[wrong].click()
    assert lesson.step == 0 and not lesson.next.isEnabled()
    assert "地址" in lesson.feedback.text() and "密钥" in lesson.feedback.text()
    lesson.choices["endpoint"].click()
    assert lesson.next.isEnabled()
    lesson.choices[wrong].click()
    assert not lesson.next.isEnabled()


def test_same_id_at_different_endpoints_is_not_same_actual(lesson):
    reach(lesson, 1)
    lesson.choices["a"].click()
    assert not lesson.next.isEnabled() and "不能" in lesson.feedback.text()
    lesson.choices["bare"].click()
    assert not lesson.next.isEnabled() and "模型 ID" in lesson.feedback.text()
    lesson.choices["b"].click()
    actual = lesson.sandbox.configuration().actual_models
    assert len({a.key for a in actual}) == 3


def test_create_name_is_not_a_complete_model_binding(lesson):
    reach(lesson, 2)
    lesson.check_button.click()
    assert not lesson.next.isEnabled()
    lesson.name_input.setText("   ")
    lesson.create_button.click()
    assert not lesson.next.isEnabled()
    lesson.logical_id_input.setText("xyz-chat")
    lesson.name_input.setText("XYZ Chat")
    lesson.create_button.click()
    assert not lesson.next.isEnabled()
    with pytest.raises(RoutingError, match="绑定"):
        lesson.sandbox.route()
    lesson.check_button.click()
    assert lesson.next.isEnabled() and lesson.sandbox.bindings == []


def test_binding_instructions_and_duplicate_guard(lesson):
    reach(lesson, 3)
    lesson.choices["bare"].click()
    lesson.choices["bind_b"].click()
    assert lesson.sandbox.bindings == [] and not lesson.next.isEnabled()
    lesson.choices["bind_a"].click()
    assert lesson.sandbox.bindings == ["a"] and not lesson.next.isEnabled()
    lesson.choices["bind_b"].click()
    assert lesson.sandbox.bindings == ["a", "b"] and lesson.next.isEnabled()
    with pytest.raises(ValueError, match="重复"):
        lesson.sandbox.bind("a")


def test_route_requires_reorder_prediction_and_validation(lesson):
    reach(lesson, 4)
    lesson.choices["guess_b"].click()
    lesson.verify_button.click()
    assert not lesson.next.isEnabled()
    lesson.prefer_button.click()
    assert not lesson.next.isEnabled()
    lesson.choices["guess_a"].click()
    lesson.verify_button.click()
    assert not lesson.next.isEnabled() and "第一条" in lesson.feedback.text()
    lesson.choices["guess_b"].click()
    assert not lesson.next.isEnabled()
    lesson.verify_button.click()
    assert lesson.next.isEnabled() and lesson.sandbox.route().endpoint.id == "b"


def test_ability_is_per_actual_model_not_logical_name(lesson):
    reach(lesson, 5)
    lesson.choices["logical"].click()
    assert not lesson.next.isEnabled()
    lesson.choices["actual"].click()
    assert lesson.next.isEnabled()
    config = lesson.sandbox.configuration()
    assert config.actual_model("a", "xyz-chat").supports_tools
    assert not config.actual_model("b", "xyz-chat").supports_tools


def test_no_cross_site_retry_until_explicit_action(lesson):
    reach(lesson, 6)
    lesson.choices["manual"].click()
    lesson.switch_button.click()
    lesson.retry_button.click()
    assert lesson.sandbox.trace == [] and not lesson.next.isEnabled()
    lesson.fail_button.click()
    assert lesson.sandbox.trace == [("b", "xyz-chat")]
    lesson.choices["auto"].click()
    assert not lesson.next.isEnabled() and lesson.sandbox.trace == [("b", "xyz-chat")]
    lesson.choices["manual"].click()
    assert not lesson.next.isEnabled()
    lesson.switch_button.click()
    assert not lesson.next.isEnabled()
    lesson.retry_button.click()
    assert lesson.next.isEnabled()
    assert lesson.sandbox.trace == [("b", "xyz-chat"), ("a", "xyz-chat")]
    assert lesson.sandbox.bindings == ["b", "a"]
    assert lesson.sandbox.route().endpoint.id == "b"


@pytest.mark.parametrize("target", [2, 3, 4, 5, 6])
def test_return_to_previous_step_can_be_relearned_without_dead_end(lesson, target):
    reach(lesson, target + 1)
    lesson.back.click()
    assert lesson.step == target and not lesson.next.isEnabled()
    solve(lesson)
    assert lesson.next.isEnabled()
    lesson.next.click()
    assert lesson.step == target + 1


def test_restart_clears_all_sandbox_progress(lesson):
    reach(lesson, len(LESSONS))
    lesson.next.click()
    assert lesson.step == 0 and not lesson.completed
    assert not lesson.sandbox.logical_name and lesson.sandbox.bindings == []
    assert lesson.sandbox.trace == [] and not lesson.next.isEnabled()
    assert lesson.route_display.text() == "尚未验证调用去向。"


def test_keyboard_can_answer_and_controls_remain_in_small_window(lesson, qtbot):
    lesson.resize(960, 730)
    qtbot.wait(50)
    lesson.choices["endpoint"].setFocus()
    qtbot.keyClick(lesson.choices["endpoint"], Qt.Key.Key_Space)
    assert lesson.next.isEnabled()
    for control in (lesson.next, lesson.back, lesson.hint, lesson.leave):
        corner = control.mapTo(lesson, control.rect().bottomRight())
        assert lesson.rect().contains(corner)
    assert lesson.exercise_scroll.horizontalScrollBar().maximum() == 0
    assert lesson.context_scroll.widget().width() <= lesson.context_scroll.viewport().width()
    lesson.toggle_theme()
    assert lesson.width() == 960


def test_sandbox_config_has_no_keys_or_persistence_dependencies():
    sandbox = ModelSandbox()
    sandbox.create_logical("XYZ Chat")
    sandbox.bind("a")
    config = sandbox.configuration()
    assert all(e.credential_ref is None and ".invalid" in e.base_url for e in config.endpoints)
    import tools.demos.model_learning_demo as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not [
        name
        for name in imports
        if any(
            word in name
            for word in (
                "httpx",
                "socket",
                "requests",
                "credential_service",
                "secret_store",
                "repository",
                "vault",
            )
        )
    ]


def test_already_valid_bindings_are_not_trapped_by_an_extra_wrong_click(lesson):
    reach(lesson, 3)
    solve(lesson)
    assert lesson.next.isEnabled()
    lesson.choices["bare"].click()
    assert lesson.sandbox.bindings == ["a", "b"]
    assert lesson.next.isEnabled()


def test_recreating_logical_name_invalidates_downstream_success(lesson):
    reach(lesson, len(LESSONS))
    while lesson.step > 2:
        lesson.previous()
    assert set(lesson.completed) == {0, 1}
    lesson.logical_id_input.setText("xyz-chat")
    lesson.name_input.setText("XYZ Chat")
    lesson.create_button.click()
    assert lesson.sandbox.bindings == []
    assert not lesson.next.isEnabled()
    lesson.check_button.click()
    assert lesson.next.isEnabled() and set(lesson.completed) == {0, 1, 2}


def test_invalid_name_does_not_unblock_continue(lesson):
    reach(lesson, 2)
    solve(lesson)
    assert lesson.next.isEnabled()
    lesson.name_input.clear()
    lesson.create_button.click()
    assert not lesson.next.isEnabled()


def test_changed_failure_decision_requires_new_explicit_selection(lesson):
    reach(lesson, 6)
    lesson.fail_button.click()
    lesson.choices["manual"].click()
    lesson.switch_button.click()
    lesson.choices["auto"].click()
    lesson.retry_button.click()
    assert not lesson.next.isEnabled()
    assert lesson.manual_choice is None
    assert lesson.sandbox.trace == [("b", "xyz-chat")]


def test_practice_station_names_use_chinese_ordinals(lesson):
    from PySide6.QtWidgets import QAbstractButton, QLabel

    from tools.demos.model_learning_demo import LESSONS

    words = []
    for index in range(len(LESSONS)):
        reach(lesson, index)
        words.extend(item.text() for item in lesson.findChildren(QLabel))
        words.extend(item.text() for item in lesson.findChildren(QAbstractButton))
        words.append(LESSONS[index].hint)
        solve(lesson)
        words.append(lesson.feedback.text())
    assert any("甲站" in word for word in words)
    assert any("乙站" in word for word in words)
    assert not [word for word in words if "A站" in word or "B站" in word]
    assert [endpoint.name for endpoint in lesson.sandbox.configuration().endpoints] == [
        "练习甲站",
        "练习乙站",
    ]


def test_plain_heading_and_neutral_model_ids(lesson):
    from tools.demos.model_learning_demo import LESSONS

    assert lesson.title.text() == "先认识提供模型的服务。"
    assert {entry.model_id for entry in lesson.sandbox.configuration().actual_models} == {
        "xyz-chat",
        "xyz-think",
    }
    assert all("lumen" not in str(item) for item in LESSONS)
    for entry in lesson.sandbox.configuration().actual_models:
        assert "lumen" not in entry.model_id


def test_logical_id_and_display_name_keep_actual_model_identity(lesson):
    reach(lesson, 2)
    lesson.logical_id_input.setText("xyz-chat")
    lesson.name_input.setText("XYZ Chat")
    lesson.create_button.click()
    config = lesson.sandbox.configuration()
    assert config.models[0].id == "xyz-chat"
    assert config.models[0].name == "XYZ Chat"
    assert config.default_model_id == "xyz-chat"
    assert not lesson.next.isEnabled()
    lesson.check_button.click()
    assert lesson.next.isEnabled()
    assert "xyz-chat" in lesson.map.text() and "XYZ Chat" in lesson.map.text()


@pytest.mark.parametrize(
    "logical_id, display_name",
    [
        ("daily-chat", "XYZ Chat"),
        ("xyz-chat", "每天聊聊"),
    ],
)
def test_lesson_does_not_teach_unrelated_logical_model_names(lesson, logical_id, display_name):
    reach(lesson, 2)
    lesson.logical_id_input.setText(logical_id)
    lesson.name_input.setText(display_name)
    lesson.create_button.click()
    assert not lesson.next.isEnabled()
    assert lesson.sandbox.configuration().models == []
    assert "xyz-chat" in lesson.feedback.text() or "XYZ Chat" in lesson.feedback.text()
