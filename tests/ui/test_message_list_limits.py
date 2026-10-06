"""Task 0.2 的验收门槛实测（B 方案：不补原型，补测量）。

计划书给 Task 0.2 的四条门槛，此前的证据状况是：数据层有基线，
UI 层只有「物化了多少行」这一类计数断言，**没有任何一条门槛被直接验证过**。
这个文件把能自动化的部分补上，并把不能自动化的部分写明理由。

四条门槛与本文件的对应：

1. **2,000 条可接受地滚动** → 真实滚动流畅度（帧时间）在本环境无法可靠自动化，
   见文末 `test_scroll_smoothness_is_not_automated` 的说明。这里改为验证
   **让滚动变贵的那个量**：组件数量与历史长度**无关**（`..._widget_count_is_bounded...`）。
2. **流式更新不导致整页重新布局** → `..._streaming_touches_only_the_stream_row`：
   给每一行的文档挂计数器，断言 10,000 个增量期间**只有流式那一行**的文档变化。
3. **组件销毁后无持续增长的 QObject** → `..._repeated_rebuild_does_not_grow_widgets`
4. **分支切换可在目标时间内完成** → `..._branch_switch_is_bounded_and_page_sized`
   （计划书没给"目标时间"的数字，这里自行定一个宽上限并打印实测值）

数字口径与压力测试一致：**断言宽上限（防病态），同时打印实测值**，
不写死毫秒阈值——固定阈值在不同机器上只会变成随机失败。
"""

from __future__ import annotations

import time

from PySide6.QtWidgets import QApplication, QTextBrowser, QWidget
from pytestqt.qtbot import QtBot

from limbowave.ui.chat_view import HISTORY_PAGE, ChatView

# 每行 (role, content, thinking, message_id)
Message = tuple[str, str, str, str | None]


def _history(count: int) -> list[Message]:
    out: list[Message] = []
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        out.append((role, f"第 {index} 条消息的内容", "", f"m{index}"))
    return out


def _widget_count(view: ChatView) -> int:
    """视图子树里的 QWidget 总数（含未处理 deleteLater 的残留，正因如此才有意义）。"""
    return len(view.findChildren(QWidget))


def _pump() -> None:
    """跑一轮事件循环：``deleteLater`` 要等事件循环才会真正销毁对象。"""
    QApplication.processEvents()


# 计数器放在模块级注册表里，**不放进 QObject 属性**：
# `setProperty` 会把 Python dict 转成 QVariantMap，`property()` 读回来的是
# 一份快照副本——闭包改的是原对象，读到的永远是 0（踩过这个坑）。
# 打在组件上的只有整数标记号。
_COUNTERS: dict[int, dict[str, int]] = {}


def _tag_browsers(view: ChatView) -> int:
    """给视图里每个文档挂变化计数器，并在组件上打标记号。返回打标数量。"""
    _COUNTERS.clear()
    for index, browser in enumerate(view.findChildren(QTextBrowser)):
        counter = {"n": 0}
        _COUNTERS[index] = counter

        def bump(*_args: object, _c: dict[str, int] = counter) -> None:
            _c["n"] += 1

        browser.document().contentsChanged.connect(bump)
        browser.setProperty("_limbowave_tag", index)
    return len(_COUNTERS)


def _read_counters() -> dict[int, int]:
    return {tag: counter["n"] for tag, counter in _COUNTERS.items()}


def _tag_of(widget: QWidget) -> int:
    return int(widget.property("_limbowave_tag"))


# ---------- 门槛 2：流式更新不导致整页重新布局 ----------


def test_streaming_touches_only_the_stream_row(qtbot: QtBot) -> None:
    """10,000 个增量期间，只有流式那一行的文档在变——其余行一次都没重排。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history(200))
    view.begin_assistant()
    view.append_assistant_delta("起点")  # 首个增量创建分段与正文文档
    _pump()

    assert _tag_browsers(view) > 1, "应当有多个文档被纳入监控"
    assert view._stream_row is not None
    assert view._stream_row._content is not None
    stream_tag = _tag_of(view._stream_row._content)

    for _ in range(10_000):
        view.append_assistant_delta("字")

    # Presentation is frame-coalesced, not synchronously repainted per token.
    qtbot.waitUntil(lambda: view._stream_row._content.toPlainText() == "起点" + "字" * 10_000)
    counters = _read_counters()
    assert counters[stream_tag] > 0, "流式行必须真的在更新"
    others = {tag: value for tag, value in counters.items() if tag != stream_tag}
    assert others, "应当还有其他行的文档被纳入监控"
    assert all(value == 0 for value in others.values()), (
        f"除流式行外不该有文档变化，实测 {[ (t, v) for t, v in others.items() if v ]}"
    )


def test_streaming_does_not_rebuild_rows(qtbot: QtBot) -> None:
    """流式期间行对象**保持同一批实例**：没有重建，就没有整页重新布局。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history(120))
    rows_before = list(view._rows)
    count_before = _widget_count(view)

    view.begin_assistant()
    for _ in range(200):
        view.append_assistant_delta("增量")

    # 老行还是同一批对象（身份相同 → 没被重建）
    assert view._rows[: len(rows_before)] == rows_before
    assert len(view._rows) == len(rows_before) + 1  # 只多了流式那一行
    assert _widget_count(view) <= count_before + 40, "不该因流式新建大量组件"


def test_streaming_10k_deltas_cost_is_recorded(qtbot: QtBot) -> None:
    """把 10,000 个增量的真实成本打出来，只断言宽上限（防病态）。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history(200))
    view.begin_assistant()

    started = time.perf_counter()
    for _ in range(10_000):
        view.append_assistant_delta("字")
    elapsed = time.perf_counter() - started

    print(f"\n[0.2] 10,000 个流式增量的界面成本：{elapsed * 1000:.0f} ms")
    assert elapsed < 30.0, "10,000 个增量不该慢到 30 秒量级（病态行为）"


# ---------- 门槛 1（结构部分）：组件数量与历史长度无关 ----------


def test_widget_count_is_bounded_regardless_of_history_length(qtbot: QtBot) -> None:
    """历史 200 / 2,000 / 10,000 条，物化出来的组件数量应当**一样多**。

    滚动成本取决于组件数量，而组件数量被滞性化钉在 ``HISTORY_PAGE`` 上，
    所以"能不能接受地滚动"不随会话变长而恶化——这是可自动化的那部分结论。
    """
    counts: dict[int, int] = {}
    for length in (200, 2_000, 10_000):
        view = ChatView()
        qtbot.addWidget(view)
        view.load_history(_history(length))
        _pump()
        assert len(view._rows) == HISTORY_PAGE, f"{length} 条时物化行数应为一页"
        counts[length] = _widget_count(view)
        print(f"[0.2] 历史 {length} 条 → 物化 {len(view._rows)} 行 / {counts[length]} 个组件")

    # 三者应当同量级（差异只来自滚动条/加载更早按钮这类固定开销）
    smallest, largest = min(counts.values()), max(counts.values())
    assert largest - smallest <= 8, f"组件数量不该随历史长度增长：{counts}"


def test_load_earlier_costs_one_page(qtbot: QtBot) -> None:
    """「加载更早」一次只多物化一页——翻历史的成本是常数，不是累计。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history(1_000))

    rows_before = len(view._rows)
    started = time.perf_counter()
    view._load_earlier()
    elapsed = time.perf_counter() - started

    print(f"\n[0.2] 点一次「加载更早」：{elapsed * 1000:.0f} ms")
    assert len(view._rows) == rows_before + HISTORY_PAGE, "一次只多一页"
    assert elapsed < 5.0


# ---------- 门槛 3：组件销毁后无持续增长的 QObject ----------


def test_repeated_rebuild_does_not_grow_widgets(qtbot: QtBot) -> None:
    """反复重建（切会话/切分支的形态）后，组件数量回到同一水位，不累积。

    这条此前**完全没有证据**。注意 ``deleteLater`` 要等事件循环，
    所以每轮都要 ``processEvents`` 之后再数，否则数到的是待销毁的残留。
    """
    view = ChatView()
    qtbot.addWidget(view)

    view.load_history(_history(300))
    _pump()
    baseline = _widget_count(view)

    levels: list[int] = []
    for _ in range(4):
        view.clear_transcript()
        _pump()
        view.load_history(_history(300))
        _pump()
        levels.append(_widget_count(view))

    print(f"\n[0.2] 反复重建 {len(levels)} 轮的水位：baseline={baseline} levels={levels}")
    assert all(level == baseline for level in levels), (
        f"每轮重建后组件数应回到同一水位，实测 {levels}（首轮 {baseline}）"
    )


def test_rebuild_after_streaming_does_not_leak_rows(qtbot: QtBot) -> None:
    """流式之后重建同样不残留：流式行也要被清掉，不留悬挂引用。"""
    view = ChatView()
    qtbot.addWidget(view)
    view.load_history(_history(120))
    view.begin_assistant()
    for _ in range(50):
        view.append_assistant_delta("x")
    _pump()

    view.clear_transcript()
    _pump()

    assert view._rows == []
    assert view._stream_row is None
    # 只剩固定的外壳组件（输入区、工具栏等），不该还留着消息行
    remaining = len(view.findChildren(QTextBrowser))
    assert remaining <= 1, f"清空后不该还有消息文档组件，实测 {remaining}"


def test_switching_between_many_branches_stays_level(qtbot: QtBot) -> None:
    """在多条分支间来回切（10 条分支，每条都渲染一遍）也不涨。"""
    view = ChatView()
    qtbot.addWidget(view)

    view.load_history(_history(80))
    _pump()
    baseline = _widget_count(view)

    for branch in range(10):
        view.clear_transcript()
        view.load_history(_history(60 + branch))
        _pump()

    assert _widget_count(view) == baseline, "来回切分支不该让组件数上涨"


# ---------- 门槛 4：分支切换耗时 ----------


def test_branch_switch_is_bounded_and_page_sized(qtbot: QtBot) -> None:
    """切换分支 = 清空 + 渲染目标分支（控制器的实际动作）。

    计划书没定义"目标时间"，这里自定宽上限并打印实测值：真正要保证的性质是
    **切换成本只与一页有关，与目标分支多长无关**。
    """
    view = ChatView()
    qtbot.addWidget(view)

    timings: dict[int, float] = {}
    for length in (100, 1_000, 5_000):
        view.load_history(_history(200))  # 起点：先待在另一条分支上

        started = time.perf_counter()
        view.clear_transcript()
        view.load_history(_history(length))
        elapsed = (time.perf_counter() - started) * 1000
        timings[length] = elapsed
        print(f"[0.2] 切到 {length} 条的分支：{elapsed:.0f} ms")

    assert all(value < 5_000 for value in timings.values()), timings
    # 成本应当与目标分支长度基本无关：5,000 条不该比 100 条慢一个量级
    assert timings[5_000] < max(timings[100] * 10, 500), (
        f"切换成本不该随分支长度增长：{timings}"
    )


# ---------- 明确不自动化的那一条 ----------


def test_scroll_smoothness_is_not_automated() -> None:
    """门槛 1 的**滚动流畅度本身**没有被自动化验证——这里如实记录原因。

    要测的是"人眼可感的滚动流畅度"，可靠做法是测量连续滚动时的帧时间
    （`QWidget.repaint` 间隔或 offscreen 平台的帧回调）。在本项目环境里：

    - 测试跑在 Qt 的 offscreen 平台上，没有真实合成器，帧时间不代表用户所见；
    - 把"帧时间上限"写成断言，在 CI/别的机器上只会变成随机失败。

    因此门槛 1 改由三条**可自动化且真正决定滚动成本**的性质承担：
    组件数量被钉在一页（`..._widget_count_is_bounded...`）、
    翻历史是常数成本（`test_load_earlier_costs_one_page`）、
    流式不重建其余行（`test_streaming_does_not_rebuild_rows`）。
    滚动流畅度本身留作**人工观察项**，在台账里如实标注。
    """
    # 这条测试没有断言，存在的意义是把上面这段理由写进代码里
    assert HISTORY_PAGE > 0
