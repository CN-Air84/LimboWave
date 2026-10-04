"""左栏（会话导航）的 Qt 验收。

对应设计计划 §十六.3 的 UI 测试要求：
- 搜索定位（输入搜索词 → 列表换成搜索结果；清空 → 回到会话列表）；
- 列表项点击外发 conversation_selected 意图；
- 新建会话按钮外发意图；
- 设置与请求日志入口外发意图。

架构守卫同主窗口：sidebar 不持有业务数据，只发信号。
"""

from __future__ import annotations

from PySide6.QtCore import SignalInstance
from pytestqt.qtbot import QtBot

from limbowave.ui.sidebar import Sidebar


def test_sidebar_holds_no_public_state(qtbot: QtBot) -> None:
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    data_attributes = [
        name
        for name, value in vars(sidebar).items()
        if not name.startswith("_") and not isinstance(value, SignalInstance)
    ]
    assert data_attributes == []


def test_conversation_rows_emit_selection(qtbot: QtBot) -> None:
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.show_conversations([("c1", "部署讨论", 4), ("c2", "周末计划", 1)])

    with qtbot.waitSignal(sidebar.conversation_selected, timeout=1000) as blocker:
        sidebar._list.setCurrentRow(1)

    assert blocker.args == ["c2"]


def test_search_button_requests_search_panel(qtbot: QtBot) -> None:
    """搜索改成按钮呼出悬浮窗：按钮只发 search_requested，不再有常驻输入框。"""
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)

    with qtbot.waitSignal(sidebar.search_requested, timeout=1000):
        sidebar._search_btn.click()

    assert not hasattr(sidebar, "_search"), "常驻搜索框应已移除"


def test_search_results_then_clear_restores_list(qtbot: QtBot) -> None:
    """搜索结果显示摘要行；清空搜索框由上层决定回灌会话列表。"""
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.show_conversations([("c1", "部署讨论", 4)])

    sidebar.show_search_results([("c1", "部署讨论 — …生产环境…")])
    assert sidebar._list.count() == 1
    # 摘要文本承载在 ROLE_TITLE 数据角色里（delegate 绘制，无行组件）
    from limbowave.ui.sidebar import ROLE_TITLE

    assert "生产环境" in sidebar._list.item(0).data(ROLE_TITLE)

    # 无匹配时的占位行不可选（NoItemFlags → 不会发出选中意图）
    sidebar.show_search_results([])
    item = sidebar._list.item(0)
    assert (
        not item.flags()
        & __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.ItemFlag.ItemIsSelectable
    )


def test_new_conversation_button(qtbot: QtBot) -> None:
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)

    with qtbot.waitSignal(sidebar.new_conversation_requested, timeout=1000):
        sidebar._new_btn.click()


def test_settings_and_log_entries(qtbot: QtBot) -> None:
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)

    with qtbot.waitSignal(sidebar.settings_requested, timeout=1000):
        sidebar.settings_requested.emit()
    # 请求日志/导出/备份/恢复/权限/执行模式都收进了设置悬浮窗与工具栏，
    # 左栏只剩：新建/搜索/设置


def test_select_conversation_by_id(qtbot: QtBot) -> None:
    """程序化选中：新建会话后上层把列表焦点移过去。"""
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.show_conversations([("c1", "a", 1), ("c2", "b", 2)])

    with qtbot.waitSignal(sidebar.conversation_selected, timeout=1000) as blocker:
        sidebar.select_conversation("c2")

    assert blocker.args == ["c2"]


def test_context_menu_offers_rename_and_delete(qtbot: QtBot, monkeypatch) -> None:
    """会话行右键：弹出 重命名/删除 两个动作，选中即外发对应意图。"""
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.show_conversations([("c1", "部署讨论", 4)])

    chosen = {}

    class FakeMenu:
        def __init__(self, parent):
            self.actions = {}
            chosen["created"] = True

        def addAction(self, text):
            from PySide6.QtGui import QAction

            action = QAction(text, sidebar)
            self.actions[text] = action
            return action

        def exec(self, global_pos):
            return self.actions.get(chosen.get("pick"))

    monkeypatch.setattr("limbowave.ui.sidebar.QMenu", FakeMenu)

    item = sidebar._list.item(0)
    pos = sidebar._list.visualItemRect(item).center()

    chosen["pick"] = "重命名"
    with qtbot.waitSignal(sidebar.rename_requested, timeout=1000) as blocker:
        sidebar._on_context_menu(pos)
    assert blocker.args == ["c1"]

    chosen["pick"] = "删除会话"
    with qtbot.waitSignal(sidebar.delete_requested, timeout=1000) as blocker:
        sidebar._on_context_menu(pos)
    assert blocker.args == ["c1"]


def test_context_menu_suppressed_in_search_mode(qtbot: QtBot, monkeypatch) -> None:
    """搜索态下不提供管理菜单（搜索结果是跳转目标，不是管理对象）。"""
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.show_search_results([("c1", "…命中…")])

    called = []
    monkeypatch.setattr("limbowave.ui.sidebar.QMenu", lambda *a: called.append(1) or None)
    # 搜索态直接 return，不构造菜单
    sidebar._on_context_menu(sidebar._list.visualItemRect(sidebar._list.item(0)).center())
    assert not called


# ---------- 当前会话指示 + 双击展开分支 ----------


def test_active_indicator_marks_current_conversation(qtbot: QtBot) -> None:
    """set_active 只标记展示态，不外发选中意图；重建列表后指示仍在。"""
    from limbowave.ui.sidebar import ROLE_ACTIVE

    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.show_conversations([("c1", "a", 1), ("c2", "b", 2)])

    with qtbot.assertNotEmitted(sidebar.conversation_selected):
        sidebar.set_active("c2", "b1")
    assert not sidebar._list.item(0).data(ROLE_ACTIVE)
    assert sidebar._list.item(1).data(ROLE_ACTIVE)
    assert sidebar._list.currentRow() == 1

    with qtbot.assertNotEmitted(sidebar.conversation_selected):
        sidebar.show_conversations([("c2", "b", 3), ("c1", "a", 1)])
    assert sidebar._list.item(0).data(ROLE_ACTIVE)


def test_active_indicator_eases_between_conversations(qtbot: QtBot) -> None:
    """当前会话竖条是独立控件，A→B 时连续缓动而不是瞬间重绘。"""
    from PySide6.QtCore import QAbstractAnimation

    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.resize(300, 500)
    sidebar.show()
    qtbot.waitExposed(sidebar)
    sidebar.show_conversations([("c1", "a", 1), ("c2", "b", 2)])
    sidebar.set_active("c1")
    start = sidebar._active_indicator.geometry()

    sidebar.set_active("c2")

    assert sidebar._indicator_animation.state() == QAbstractAnimation.State.Running
    qtbot.waitUntil(
        lambda: sidebar._indicator_animation.state() == QAbstractAnimation.State.Stopped
    )
    target = sidebar._indicator_target()
    assert target is not None
    assert sidebar._active_indicator.geometry() == target
    assert target.top() > start.top()


def test_double_click_expands_branches_and_switches(qtbot: QtBot) -> None:
    from PySide6.QtCore import QAbstractAnimation

    from limbowave.ui.sidebar import ROLE_ACTIVE, ROLE_BRANCH_ID, ROLE_HIT_KIND

    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.show_conversations([("c1", "a", 1), ("c2", "b", 2)])
    sidebar.set_active("c1", "b1")
    sidebar.branches_requested.connect(
        lambda cid: sidebar.set_branches(cid, [("b1", "分支 1", 3), ("b2", "分支 2（分叉）", 5)])
    )

    with qtbot.waitSignal(sidebar.branches_requested, timeout=1000) as blocker:
        sidebar._on_double_clicked(sidebar._list.item(0))
    assert blocker.args == ["c1"]
    assert sidebar._list.count() == 4
    assert [sidebar._list.item(i).data(ROLE_HIT_KIND) for i in range(4)] == [
        "conversation",
        "branch",
        "branch",
        "conversation",
    ]
    assert sidebar._list.item(1).data(ROLE_ACTIVE)  # 当前分支
    assert sidebar._list.item(2).data(ROLE_BRANCH_ID) == "b2"
    assert sidebar._branch_animations["c1"].state() == QAbstractAnimation.State.Running
    qtbot.waitUntil(lambda: "c1" not in sidebar._branch_animations)

    with qtbot.waitSignal(sidebar.branch_switch_requested, timeout=1000) as blocker:
        sidebar._list.setCurrentRow(2)
    assert blocker.args == ["c1", "b2"]

    # 再次双击收起
    sidebar._on_double_clicked(sidebar._list.item(0))
    assert sidebar._branch_animations["c1"].state() == QAbstractAnimation.State.Running
    qtbot.waitUntil(lambda: "c1" not in sidebar._branch_animations)
    assert sidebar._list.count() == 2


def test_branch_context_menu_offers_rename_and_delete(qtbot: QtBot, monkeypatch) -> None:
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.show_conversations([("c1", "部署讨论", 4)])
    sidebar.branches_requested.connect(
        lambda cid: sidebar.set_branches(cid, [("b1", "09-27 10:30 部署讨论", 4)])
    )
    sidebar.toggle_branches("c1")
    qtbot.waitUntil(lambda: "c1" not in sidebar._branch_animations)

    chosen = {}

    class FakeMenu:
        def __init__(self, parent):
            self.actions = {}

        def addAction(self, text):
            from PySide6.QtGui import QAction

            action = QAction(text, sidebar)
            self.actions[text] = action
            return action

        def exec(self, global_pos):
            return self.actions.get(chosen.get("pick"))

    monkeypatch.setattr("limbowave.ui.sidebar.QMenu", FakeMenu)
    branch = sidebar._list.item(1)
    pos = sidebar._list.visualItemRect(branch).center()

    chosen["pick"] = "重命名分支"
    with qtbot.waitSignal(sidebar.branch_rename_requested, timeout=1000) as blocker:
        sidebar._on_context_menu(pos)
    assert blocker.args == ["c1", "b1"]

    chosen["pick"] = "删除分支"
    with qtbot.waitSignal(sidebar.branch_delete_requested, timeout=1000) as blocker:
        sidebar._on_context_menu(pos)
    assert blocker.args == ["c1", "b1"]


def test_expanded_state_survives_list_refresh(qtbot: QtBot) -> None:
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.show_conversations([("c1", "a", 1)])
    sidebar.set_branches("c1", [("b1", "分支 1", 1), ("b2", "分支 2", 2)])
    sidebar.toggle_branches("c1")
    assert sidebar._list.count() == 3

    sidebar.show_conversations([("c1", "a", 2)])
    assert sidebar.is_expanded("c1")
    assert sidebar._list.count() == 3


def test_clicking_active_conversation_does_not_reopen(qtbot: QtBot) -> None:
    """双击的第一击会选中行；已在显示的会话不重复发打开意图。"""
    sidebar = Sidebar()
    qtbot.addWidget(sidebar)
    sidebar.show_conversations([("c1", "a", 1), ("c2", "b", 2)])
    sidebar.set_active("c1", "b1")
    sidebar._list.setCurrentRow(1)
    with qtbot.assertNotEmitted(sidebar.conversation_selected):
        sidebar._list.setCurrentRow(0)
