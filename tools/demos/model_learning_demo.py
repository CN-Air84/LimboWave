"""Action-based model configuration lessons in an isolated in-memory sandbox.

No real endpoints, keys, files, network or production OOBE changes.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from limbowave.application.services.routing_service import RoutingService
from limbowave.domain.configuration import AppConfiguration
from limbowave.domain.models import ActualModel, LogicalModel, ModelBinding
from limbowave.domain.providers import EndpointConfig, ProviderProtocol
from limbowave.domain.routing import RoutingError
from limbowave.ui import theme
from limbowave.ui.oobe_demo import _Stage


@dataclass
class ModelSandbox:
    """Use the real schema/router; never use a repository or credential service."""

    logical_id: str = ""
    logical_name: str = ""
    bindings: list[str] = field(default_factory=list)
    failed: bool = False
    explicit_target: str | None = None
    trace: list[tuple[str, str]] = field(default_factory=list)

    def configuration(self) -> AppConfiguration:
        endpoints = [
            EndpointConfig(
                id=k,
                name="练习甲站" if k == "a" else "练习乙站",
                base_url=f"https://{k}.example.invalid/v1",
                api=ProviderProtocol.OPENAI_COMPLETIONS,
            )
            for k in ("a", "b")
        ]
        actual = [
            ActualModel(endpoint_id="a", model_id="xyz-chat", supports_tools=True),
            ActualModel(endpoint_id="a", model_id="xyz-think", supports_tools=True),
            ActualModel(endpoint_id="b", model_id="xyz-chat", supports_tools=False),
        ]
        logical = (
            [
                LogicalModel(
                    id=self.logical_id,
                    name=self.logical_name,
                    bindings=[
                        ModelBinding(endpoint_id=k, model_id="xyz-chat") for k in self.bindings
                    ],
                )
            ]
            if self.logical_name
            else []
        )
        return AppConfiguration(
            endpoints=endpoints,
            actual_models=actual,
            models=logical,
            default_model_id=self.logical_id if logical else None,
        )

    def create_logical(self, name: str, *, logical_id: str = "xyz-chat") -> None:
        name = name.strip()
        logical_id = logical_id.strip()
        # A scaffolded naming exercise, not a new restriction on real user configuration.
        if logical_id != "xyz-chat":
            raise ValueError("这关练习沿用实际模型的 ID：xyz-chat，保留可辨认的型号。")
        if name.casefold().replace(" ", "").replace("-", "") != "xyzchat":
            raise ValueError("显示名称用 XYZ Chat：保留型号，只调整大小写和空格。")
        self.logical_id = logical_id
        self.logical_name = name

    def bind(self, endpoint: str) -> None:
        if not self.logical_name:
            raise ValueError("先创建逻辑模型，再告诉它要调用哪个实际模型。")
        if endpoint not in ("a", "b"):
            raise ValueError("绑定需要“站点 + 模型 ID”，不是只有一个站点地址。")
        if endpoint in self.bindings:
            raise ValueError("这个实际模型已经绑定，不必重复添加。")
        self.bindings.append(endpoint)

    def prefer(self, endpoint: str) -> None:
        if endpoint not in self.bindings:
            raise ValueError("只能从已绑定的实际模型里选首选。")
        self.bindings.remove(endpoint)
        self.bindings.insert(0, endpoint)
        self.explicit_target = None

    def route(self, *, endpoint: str | None = None, record: bool = False):
        result = RoutingService(self.configuration()).route(endpoint_id=endpoint)
        if record:
            self.trace.append((result.endpoint.id, result.model_id))
        return result


@dataclass(frozen=True)
class Lesson:
    name: str
    title: str
    explanation: str
    task: str
    hint: str


LESSONS = (
    Lesson(
        "先认站点",
        "先认识提供模型的服务。",
        "站点端点是一份连接配置：API地址、调用协议和密钥属于这里。\n"
        "它可以是官方服务、中转服务或你自己的本地兼容服务；一个站点能提供多个模型。",
        "你的 API 地址和密钥变了，应该修改哪一层？",
        "先看右侧的“站点端点”：这层负责连接，不负责给聊天入口起名字。",
    ),
    Lesson(
        "认出实际模型",
        "同一个名字，不是同一条配置。",
        "实际模型指“某个站点上的某个模型 ID”。甲站的 xyz-chat 和乙站的 xyz-chat，\n"
        "ID相同，但地址、认证和能力来源不同，必须保留成两个条目。拉取清单也不等于全部添加。",
        "右侧已有 甲站 · xyz-chat。现在要接上 乙站的同名模型，选哪个条目？",
        "找完整的“乙站 + xyz-chat”。别只看模型ID，也别只选乙站地址。",
    ),
    Lesson(
        "建立逻辑模型",
        "保留型号，把显示名称写清楚。",
        "逻辑模型是日常选择的统一入口。逻辑 ID 建议沿用实际模型 ID，方便对应型号。\n"
        "例如：实际 ID xyz-chat → 逻辑 ID xyz-chat → 显示名称 XYZ Chat。只调整大小写和空格。",
        "填写逻辑 ID xyz-chat 和显示名称 XYZ Chat，创建后再检查为什么还不能调用。",
        "型号不要改成用途或场景。先填 xyz-chat、XYZ Chat，再创建并检查；未绑定仍不能调用。",
    ),
    Lesson(
        "亲手绑定",
        "把名字，接到真实的调用目标。",
        "绑定回答“选择这个逻辑模型时，可调用哪些站点上的实际模型”。\n"
        "一个逻辑模型可绑多个站点；同一站点至多一条绑定。能力不同也不能靠改名变成一样。",
        "先绑定 甲站 · xyz-chat，再绑定 乙站 · xyz-chat。端点本身不是完整绑定。",
        "按两次对应的实际模型按钮。看右侧新增两条连线；第一次绑定会先排在第一位。",
    ),
    Lesson(
        "排首选",
        "第一位，才是默认去向。",
        "绑定顺序不是装饰。第一条是这个逻辑模型的首选站点。\n"
        "切换站点改变请求的接收方；逻辑名称保持不变，不意味着请求还去同一家公司。",
        "把 乙站移到第一位，选出你预测的去向，再点击“验证去向”。",
        "先点“乙站设为首选”，再选“乙站 · xyz-chat”。验证会使用项目真实的 RoutingService。",
    ),
    Lesson(
        "核对能力",
        "名字一样，能力也要分别看。",
        "工具、流式、思考等声明属于“某个站点的某个实际模型”。\n"
        "练习数据中甲支持工具，乙明确不支持；这只是教学数据，不代表任何真实提供方。",
        "当前首选乙站不支持工具。应该去哪里核对或修改这条能力声明？",
        "去“设置 → 站点端点 → 实际模型”，选乙站的xyz-chat。别给逻辑名称贴一个“支持”标签。",
    ),
    Lesson(
        "失败也不乱换",
        "失败后，也不会偷偷换个去处。",
        "首选请求失败会如实报告，不静默跨站点重发。你可以明确选择另一个已绑定站点。\n"
        "本关的失败和重试全是模拟，不会调用模型，也不会改变真正的首选配置。",
        "先模拟乙站失败，选出下一步，再明确选择甲站并主动验证重试。",
        "模拟失败 → 选“我来明确切换站点” → 选甲站 → 验证重试。最后看默认首选仍是乙。",
    ),
)


def text(value: str, role: str = "body") -> QLabel:
    widget = QLabel(value)
    widget.setObjectName(role)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def action(value: str, handler, *, primary: bool = False) -> QPushButton:
    widget = QPushButton(value)
    widget.setProperty("accent" if primary else "flat", True)
    widget.setAutoDefault(False)
    widget.setMinimumHeight(38)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.clicked.connect(handler)
    return widget


class ModelLearningDemo(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("modelLearning")
        self.setWindowTitle("LimboWave · 模型配置练习（独立沙盒 Demo）")
        self.resize(1160, 820)
        self.setMinimumSize(960, 730)
        self.sandbox = ModelSandbox()
        self.step = 0
        self.completed: set[int] = set()
        self.choices: dict[str, QPushButton] = {}
        self.route_guess: str | None = None
        self.failure_choice = False
        self.manual_choice: str | None = None
        self.feedback_history: list[str] = []
        self.hint_count = 0
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QFrame()
        header.setObjectName("header")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(26, 14, 26, 14)
        header_row.addWidget(text("LimboWave / 灵波", "brand"))
        header_row.addSpacing(20)
        header_row.addWidget(text("模型配置 · 带着做一次", "muted"))
        header_row.addStretch()
        self.theme_button = action("浅色预览", self.toggle_theme)
        header_row.addWidget(self.theme_button)
        root.addWidget(header)
        progress = QHBoxLayout()
        progress.setContentsMargins(36, 16, 36, 0)
        progress.setSpacing(18)
        self.counter = text("", "small")
        self.counter.setWordWrap(False)
        self.counter.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        progress.addWidget(self.counter)
        self.progress = QProgressBar()
        self.progress.setRange(0, len(LESSONS))
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setAccessibleName("练习掌握进度")
        progress.addWidget(self.progress, 1)
        root.addLayout(progress)
        stage = _Stage()
        body = QHBoxLayout(stage)
        body.setContentsMargins(36, 24, 36, 18)
        body.setSpacing(28)
        left = QWidget()
        left.setObjectName("transparent")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.eyebrow = text("", "eyebrow")
        self.title = text("", "title")
        self.explanation = text("")
        left_layout.addWidget(self.eyebrow)
        left_layout.addSpacing(6)
        left_layout.addWidget(self.title)
        left_layout.addSpacing(10)
        left_layout.addWidget(self.explanation)
        left_layout.addSpacing(16)
        self.task = text("", "task")
        left_layout.addWidget(self.task)
        left_layout.addSpacing(10)
        self.exercise_scroll = QScrollArea()
        self.exercise_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.exercise_scroll.setWidgetResizable(True)
        self.exercise_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.exercise = QWidget()
        self.exercise.setObjectName("transparent")
        self.exercise_layout = QVBoxLayout(self.exercise)
        self.exercise_layout.setContentsMargins(0, 0, 8, 0)
        self.exercise_layout.setSpacing(8)
        self.exercise_layout.addStretch()
        self.exercise_scroll.setWidget(self.exercise)
        left_layout.addWidget(self.exercise_scroll, 1)
        body.addWidget(left, 1)
        right = QWidget()
        right.setObjectName("transparent")
        right.setMinimumWidth(0)
        right.setMaximumWidth(340)
        context = QVBoxLayout(right)
        context.setContentsMargins(0, 0, 0, 0)
        context.addWidget(text("你的练习配置", "mapTitle"))
        context.addWidget(text("看着它，一步步接起来。只在本窗口内变化。", "small"))
        context.addSpacing(10)
        self.map = text("", "map")
        self.map.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        context.addWidget(self.map)
        self.route_display = text("尚未验证调用去向。", "route")
        context.addWidget(self.route_display)
        context.addSpacing(12)
        context.addWidget(text("第一次设置，其实替你完成了什么？", "mapTitle"))
        context.addWidget(
            text(
                "你添加并明确选择默认模型后，引导会创建或复用逻辑入口，"
                "绑定所选实际模型，并设为默认。不是把所有拉到的清单都变成逻辑模型。",
                "small",
            )
        )
        context.addSpacing(12)
        context.addWidget(
            text("这张图不是你的真实配置。\n无密钥 · 不联网 · 不写入资料库", "safety")
        )
        context.addStretch()
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setMinimumWidth(290)
        right_scroll.setMaximumWidth(340)
        right_scroll.setWidget(right)
        self.context_scroll = right_scroll
        body.addWidget(right_scroll)
        root.addWidget(stage, 1)
        footer = QFrame()
        footer.setObjectName("footer")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(36, 14, 36, 18)
        self.feedback = text("", "feedback")
        self.feedback.setAccessibleName("练习反馈")
        self.feedback.setMinimumHeight(43)
        footer_layout.addWidget(self.feedback)
        navigation = QHBoxLayout()
        self.back = action("上一步", self.previous)
        self.hint = action("带我看一步", self.show_hint)
        self.leave = action("暂离练习", self.close)
        navigation.addWidget(self.back)
        navigation.addWidget(self.hint)
        navigation.addStretch()
        navigation.addWidget(self.leave)
        self.next = action("完成这一步再继续", self.advance, primary=True)
        navigation.addWidget(self.next)
        footer_layout.addLayout(navigation)
        root.addWidget(footer)
        self.apply_style()
        self.render()

    def clear_exercise(self) -> None:
        self.choices = {}
        while self.exercise_layout.count():
            item = self.exercise_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()

    def choice(self, key: str, title: str, description: str = "") -> QPushButton:
        control = action(
            title + (f"\n{description}" if description else ""),
            lambda checked=False, k=key: self.answer(k),
        )
        control.setObjectName("choice")
        control.setAccessibleName(title)
        control.setMinimumHeight(55 if description else 40)
        control.setCheckable(True)
        control.setProperty("answerKey", key)
        self.choices[key] = control
        self.exercise_layout.addWidget(control)
        return control

    def render(self) -> None:
        self.clear_exercise()
        self.back.setEnabled(self.step > 0)
        self.hint.setVisible(self.step < len(LESSONS))
        self.progress.setValue(len(self.completed))
        self.counter.setText(
            f"{min(self.step + 1, len(LESSONS))} / {len(LESSONS)} · 已练会 {len(self.completed)} 项"
        )
        self.feedback_for("这一关可以慢慢试，选错会解释原因，不扣分。", prefix=False)
        if self.step == len(LESSONS):
            self.render_summary()
            return
        lesson = LESSONS[self.step]
        self.eyebrow.setText(f"第 {self.step + 1} 小步 · {lesson.name}")
        self.title.setText(lesson.title)
        self.explanation.setText(lesson.explanation)
        self.task.setText("动手任务  /  " + lesson.task)
        if self.step == 0:
            self.choice("endpoint", "站点端点", "管理 API 地址、协议和密钥")
            self.choice("actual", "实际模型", "管理该站点的模型 ID 和能力声明")
            self.choice("logical", "逻辑模型", "管理逻辑 ID、显示名称、绑定和首选顺序")
        elif self.step == 1:
            self.choice("a", "甲站 · xyz-chat", "甲站 ＋ 模型 ID xyz-chat")
            self.choice("b", "乙站 · xyz-chat", "乙站 ＋ 模型 ID xyz-chat")
            self.choice("bare", "只有 乙站 API 地址", "没有模型 ID")
        elif self.step == 2:
            self.exercise_layout.addWidget(text("逻辑模型 ID · 对应实际型号", "mapTitle"))
            self.logical_id_input = QLineEdit(self.sandbox.logical_id)
            self.logical_id_input.setPlaceholderText("xyz-chat")
            self.logical_id_input.setMaxLength(100)
            self.logical_id_input.setAccessibleName("练习逻辑模型 ID")
            self.exercise_layout.addWidget(self.logical_id_input)
            self.exercise_layout.addWidget(text("显示名称 · 让型号更好读", "mapTitle"))
            self.name_input = QLineEdit(self.sandbox.logical_name)
            self.name_input.setPlaceholderText("XYZ Chat")
            self.name_input.setMaxLength(100)
            self.name_input.setAccessibleName("练习逻辑模型显示名称")
            self.exercise_layout.addWidget(self.name_input)
            self.create_button = action("创建逻辑模型", self.create_logical, primary=True)
            self.check_button = action("检查可用性（不发请求）", self.check_unbound)
            self.exercise_layout.addWidget(self.create_button)
            self.exercise_layout.addWidget(self.check_button)
            self.exercise_layout.addWidget(
                text(
                    "逻辑 ID 用于配置识别；显示名称用于界面展示。创建后仍需要绑定实际模型。",
                    "small",
                )
            )
        elif self.step == 3:
            self.choice("bind_a", "绑定 甲站 · xyz-chat", "第一条，作为当前默认去向")
            self.choice("bind_b", "绑定 乙站 · xyz-chat", "给同一入口再接上一个站点")
            self.choice("bare", "只绑定 乙站地址", "不指定模型 ID")
        elif self.step == 4:
            self.prefer_button = action("把 乙站设为首选", self.prefer_b, primary=True)
            self.exercise_layout.addWidget(self.prefer_button)
            self.choice("guess_a", "预测：甲站 · xyz-chat")
            self.choice("guess_b", "预测：乙站 · xyz-chat")
            self.verify_button = action("验证去向（不发请求）", self.verify_route)
            self.exercise_layout.addWidget(self.verify_button)
        elif self.step == 5:
            self.choice("actual", "乙站的实际模型能力", "设置 → 站点端点 → 实际模型")
            self.choice("logical", "只改逻辑模型的名称", "把 XYZ Chat 改名不会增加工具能力")
            self.choice("key", "给站点换一把密钥", "不核对该模型的能力声明")
        elif self.step == 6:
            self.fail_button = action("模拟首选 乙站失败", self.simulate_failure, primary=True)
            self.exercise_layout.addWidget(self.fail_button)
            self.choice("auto", "自动换甲站重发", "让应用自行决定换一个接收方")
            self.choice("manual", "我来明确切换站点", "先选择，再主动重试")
            self.switch_button = action("明确选择：这次使用 甲站", self.select_a)
            self.retry_button = action("验证这次重试（不发请求）", self.verify_retry)
            self.exercise_layout.addWidget(self.switch_button)
            self.exercise_layout.addWidget(self.retry_button)
        self.exercise_layout.addStretch()
        self.sync()

    def feedback_for(
        self,
        message: str,
        correct: bool = False,
        *,
        prefix: bool = True,
    ) -> None:
        self.feedback_history.append(message)
        marker = ("完成  /  " if correct else "再看一眼  /  ") if prefix else ""
        self.feedback.setText(marker + message)
        self.feedback.setProperty("correct", correct)
        self.feedback.style().unpolish(self.feedback)
        self.feedback.style().polish(self.feedback)

    def invalidate_from(self, step: int) -> None:
        self.completed.difference_update(range(step, len(LESSONS)))

    def complete(self, message: str) -> None:
        self.completed.add(self.step)
        self.feedback_for(message, True)
        self.sync()

    def answer(self, key: str) -> None:
        if self.step != 3:
            self.invalidate_from(self.step)
        for name, control in self.choices.items():
            control.setChecked(name == key)
        if self.step == 0:
            if key == "endpoint":
                self.complete(
                    "对。改连接，就去站点端点；所有使用这个站点的模型都共用这份连接配置。"
                )
            else:
                self.feedback_for(
                    "实际模型负责模型 ID 与能力，逻辑模型负责入口和绑定；"
                    "地址、协议和密钥要去站点端点修改。"
                )
        elif self.step == 1:
            if key == "b":
                self.complete(
                    "对。乙站＋xyz-chat 是另一条实际模型。同名不合并；同一站点也可以有别的模型 ID。"
                )
            elif key == "a":
                self.feedback_for(
                    "这是甲站已有的条目；模型名相同，站点不同，不能把甲站的条目当成乙站。"
                )
            else:
                self.feedback_for(
                    "只有地址还不能调用具体模型。这里还需要乙站提供的模型 ID：xyz-chat。"
                )
        elif self.step == 3:
            if key == "bare":
                self.feedback_for(
                    "这条线要落到“站点＋模型 ID”，不能只连到站点。请选上面的实际模型。"
                )
            elif key == "bind_b" and "a" not in self.sandbox.bindings:
                self.feedback_for(
                    "这次先练 甲→乙 的顺序。请先绑定甲，下一关我们再亲手把乙调为首选。"
                )
            else:
                endpoint = "a" if key == "bind_a" else "b"
                try:
                    self.sandbox.bind(endpoint)
                except ValueError as exc:
                    self.feedback_for(str(exc))
                else:
                    if len(self.sandbox.bindings) == 2:
                        self.complete(
                            "两条绑定都接好了。首选目前是甲，不是随机选择，也不是同时发给两个站点。"
                        )
                    else:
                        self.feedback_for(
                            "甲站已绑定。再点“绑定乙站”，看看一个日常入口如何接上第二个站点。", True
                        )
                if self.sandbox.bindings:
                    current = self.sandbox.route()
                    self.route_display.setText(
                        f"练习默认去向（未联网）\n{current.endpoint.name} → {current.model_id}"
                    )
                self.sync()
        elif self.step == 4:
            self.route_guess = "a" if key == "guess_a" else "b"
            self.feedback_for("预测已选。还需要点“验证去向”，用真实路由规则检查，不只靠猜。")
        elif self.step == 5:
            if key == "actual":
                self.complete(
                    "对。先检查乙站这条实际模型；不要由同名、入口名称或另一个站点的结果推断它支持工具。"
                )
            else:
                self.feedback_for(
                    "改名或换密钥不会凭空增加模型能力。去乙站的实际模型详情看声明/检测；未知也不能当支持。"
                )
        elif self.step == 6:
            if not self.sandbox.failed:
                self.feedback_for("先点“模拟首选乙站失败”，观察应用有没有擅自把请求转去甲。")
            elif key == "auto":
                self.failure_choice = False
                self.manual_choice = None
                self.sandbox.explicit_target = None
                self.feedback_for(
                    "不会这样。甲是另一个接收方，失败不代表你同意把消息改发给它。请明确切换。"
                )
            else:
                self.failure_choice = True
                self.feedback_for("对，选择权在你。现在亲手点“这次使用甲站”，再验证重试。", True)
        self.sync()

    def create_logical(self) -> None:
        self.invalidate_from(2)
        try:
            self.sandbox.create_logical(
                self.name_input.text(),
                logical_id=self.logical_id_input.text(),
            )
        except ValueError as exc:
            self.feedback_for(str(exc))
            self.sync()
            return
        self.sandbox.bindings.clear()
        self.sandbox.failed = False
        self.sandbox.explicit_target = None
        self.sandbox.trace.clear()
        self.route_display.setText("已创建逻辑入口；尚未绑定，等待检查可用性。")
        self.feedback_for(
            "逻辑 ID 和显示名称已创建，型号仍可对应。点“检查可用性”，看还缺哪一步。", True
        )
        self.sync()

    def check_unbound(self) -> None:
        if not self.sandbox.logical_name:
            self.feedback_for("先填逻辑 ID 和显示名称并创建；这一步还没有绑定实际模型。")
            return
        try:
            self.sandbox.route()
        except RoutingError:
            self.route_display.setText(
                "路由检查：尚未绑定实际模型。\n没有猜测地址，也没有发送请求。"
            )
            self.complete(
                "检查得对：只有逻辑模型还不能聊天，必须绑定实际模型。下一步我们一起接线。"
            )
        else:
            self.complete("这个入口已绑定过，已有明确去向。创建入口和建立绑定仍是不同动作。")

    def prefer_b(self) -> None:
        self.invalidate_from(4)
        self.sandbox.prefer("b")
        self.route_guess = None
        self.route_display.setText("绑定顺序已改为 乙 → 甲。\n先预测、再验证默认去向。")
        self.feedback_for("乙已排第一。现在预测会发到哪里，再点击验证。", True)
        self.sync()

    def verify_route(self) -> None:
        if self.sandbox.bindings[0] != "b":
            self.feedback_for("还没调整首选。先点“把乙站设为首选”，再观察右侧顺序。")
            return
        if self.route_guess is None:
            self.feedback_for("先选一个预测，再验证。目标由绑定顺序决定，不由名字决定。")
            return
        actual = self.sandbox.route()
        if self.route_guess != actual.endpoint.id:
            self.feedback_for("再看第一条绑定：现在是乙。甲仍然可选，但不是当前默认目标。")
            return
        self.route_display.setText(
            f"真实路由规则（未联网）\n{self.sandbox.logical_name} → 乙站 → {actual.model_id}"
        )
        self.complete("预测正确。默认去乙站，调用xyz-chat；逻辑名称没变，但接收方已经由你改了。")

    def simulate_failure(self) -> None:
        self.invalidate_from(6)
        self.sandbox.route(record=True)
        self.sandbox.failed = True
        self.sandbox.explicit_target = None
        self.failure_choice = False
        self.manual_choice = None
        self.route_display.setText(
            "模拟结果：乙站失败。\n实际请求：0；模拟尝试：仅乙。\n没有自动选择甲站。"
        )
        self.feedback_for("失败已显示。绑定列表没变，仍以乙为首选；现在选你希望的下一步。")
        self.sync()

    def select_a(self) -> None:
        self.invalidate_from(6)
        self.sync()
        if not self.sandbox.failed or not self.failure_choice:
            self.feedback_for("先模拟失败并选“我来明确切换站点”，不要跳过接收方确认。")
            return
        self.manual_choice = "a"
        self.sandbox.explicit_target = "a"
        self.feedback_for("这次明确选了甲；现在点验证重试。它不会把保存的默认首选也改成甲。", True)

    def verify_retry(self) -> None:
        if self.manual_choice != "a" or not self.failure_choice:
            self.feedback_for("还没明确选择甲站。只有自动猜测，不算获得切换接收方的许可。")
            return
        result = self.sandbox.route(endpoint=self.manual_choice, record=True)
        self.route_display.setText(
            f"手动指定的本次去向（未联网）\n甲站 → {result.model_id}\n保存的默认首选仍是乙。"
        )
        self.complete("你主动切到了甲，且只验证这次调用。它是会话/本次选择，不等于重排全局默认。")

    def sync(self) -> None:
        completed = self.step in self.completed
        self.next.setEnabled(completed)
        self.next.setText("继续" if completed else "完成这一步再继续")
        self.progress.setValue(len(self.completed))
        self.counter.setText(
            f"{min(self.step + 1, len(LESSONS))} / {len(LESSONS)} · 已练会 {len(self.completed)} 项"
        )
        if self.step == 3:
            for endpoint in ("a", "b"):
                self.choices["bind_" + endpoint].setEnabled(endpoint not in self.sandbox.bindings)
        self.refresh_map()

    def refresh_map(self) -> None:
        logical = (
            f"显示名称：{self.sandbox.logical_name}\n逻辑 ID：{self.sandbox.logical_id}"
            if self.sandbox.logical_name
            else "显示名称：待填写\n逻辑 ID：待填写（示例 xyz-chat）"
        )
        bindings = (
            "\n".join(
                f"  {i + 1}. {'甲' if key == 'a' else '乙'}站 · xyz-chat"
                + ("  ← 首选" if i == 0 else "")
                for i, key in enumerate(self.sandbox.bindings)
            )
            or "  尚无绑定 → 不能调用"
        )
        self.map.setText(
            "逻辑模型 · 按型号选择\n"
            + logical
            + "\n"
            + bindings
            + "\n\n站点端点 · 连接配置\n甲站   a.example.invalid\n乙站   b.example.invalid\n"
            "地址 / 协议 / 密钥在这里\n\n实际模型 · 站点＋模型ID\n"
            "甲站 · xyz-chat   工具：支持\n甲站 · xyz-think  另一模型ID\n"
            "乙站 · xyz-chat   工具：不支持"
        )

    def show_hint(self) -> None:
        self.hint_count += 1
        self.feedback_for("提示：" + LESSONS[self.step].hint)
        if self.step == 2:
            self.logical_id_input.setFocus()
        elif self.step == 3:
            key = "bind_a" if "a" not in self.sandbox.bindings else "bind_b"
            self.choices[key].setFocus()
        elif self.step == 4:
            self.prefer_button.setFocus()
        elif self.step == 6:
            self.fail_button.setFocus()
        else:
            key = {0: "endpoint", 1: "b", 5: "actual"}[self.step]
            self.choices[key].setFocus()
        # Hints guide; they never click an answer or unlock Continue.

    def previous(self) -> None:
        if self.step > 0:
            self.step -= 1
            self.invalidate_from(self.step)
            name = self.sandbox.logical_name
            self.sandbox = ModelSandbox()
            if self.step >= 3:
                self.sandbox.create_logical(name or "XYZ Chat")
            if self.step >= 4:
                self.sandbox.bind("a")
                self.sandbox.bind("b")
            if self.step >= 5:
                self.sandbox.prefer("b")
            self.route_guess = None
            self.failure_choice = False
            self.manual_choice = None
            self.route_display.setText("重做这一关，后续练习从这里重新接上；不改真实配置。")
            self.render()

    def advance(self) -> None:
        if self.step == len(LESSONS):
            self.restart()
        elif self.step in self.completed:
            self.step += 1
            self.render()

    def restart(self) -> None:
        self.sandbox = ModelSandbox()
        self.completed.clear()
        self.route_guess = None
        self.failure_choice = False
        self.manual_choice = None
        self.feedback_history.clear()
        self.hint_count = 0
        self.route_display.setText("尚未验证调用去向。")
        self.step = 0
        self.render()

    def render_summary(self) -> None:
        self.eyebrow.setText("练会了，不只是看过了")
        self.title.setText("以后改哪里，心里有数了。")
        self.explanation.setText(
            "站点负责连接，实际模型负责站点内的ID和能力，逻辑模型用可辨认的型号统一入口。\n"
            "你刚亲手创建、绑定、调整并验证了去向；真正的资料库没有变化。"
        )
        self.task.setText("带走这张操作清单")
        for title, detail in [
            ("换API地址、协议、密钥", "设置 → 站点端点 → 对应站点"),
            ("加模型、核对流式/思考/工具能力", "设置 → 站点端点 → 实际模型"),
            ("改逻辑 ID、显示名称、绑定和首选顺序", "设置 → 逻辑模型"),
            ("本会话临时换站点", "聊天高级设置中明确选已绑定站点；不等于改全局首选"),
        ]:
            self.exercise_layout.addWidget(text(title, "mapTitle"))
            self.exercise_layout.addWidget(text(detail, "small"))
        self.exercise_layout.addWidget(
            text(
                "绑定不同站点不是效果一致的保证；能力未知时不能当作支持。失败不会静默跨站重发。",
                "safety",
            )
        )
        self.exercise_layout.addStretch()
        self.next.setEnabled(True)
        self.next.setText("重新练一次")
        self.feedback_for(
            "七个练习完成。正式接入时，这里再继续语音、联网和其他介绍；本Demo到此为止。", True
        )
        self.refresh_map()

    def toggle_theme(self) -> None:
        dark = theme.current_palette().name == "dark"
        theme.set_palette("light" if dark else "dark")
        self.theme_button.setText("深色预览" if dark else "浅色预览")
        self.apply_style()

    def apply_style(self) -> None:
        app = QApplication.instance()
        if app:
            app.setStyleSheet(theme.app_stylesheet())
        self.setStyleSheet(f"""
            QWidget#modelLearning {{ background: {theme.BG_APP}; }}
            QWidget#transparent, QLabel {{ background: transparent; }}
            QFrame#header, QFrame#footer {{ background: {theme.BG_SURFACE}; border: none; }}
            QFrame#header {{ border-bottom: 1px solid {theme.BORDER}; }}
            QFrame#footer {{ border-top: 1px solid {theme.BORDER}; }}
            QLabel#brand, QLabel#mapTitle {{ color: {theme.TEXT_PRIMARY};
                font-size: {theme.FS_BASE + 1}px; font-weight: 600; }}
            QLabel#title {{
                color: {theme.TEXT_PRIMARY};
                font-size: {theme.FS_TITLE * 2}px;
                font-weight: 600;
            }}
            QLabel#eyebrow {{ color: {theme.ACCENT}; font-size: {theme.FS_SMALL}px; }}
            QLabel#body, QLabel#map, QLabel#task {{
                color: {theme.TEXT_PRIMARY};
                font-size: {theme.FS_BASE}px;
            }}
            QLabel#small, QLabel#muted {{
                color: {theme.TEXT_SECONDARY};
                font-size: {theme.FS_SMALL}px;
            }}
            QLabel#map {{ padding: 18px; background: {theme.BG_SURFACE};
                border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_LG}px; }}
            QLabel#task {{ font-weight: 600; }}
            QLabel#route {{ color: {theme.ACCENT}; font-size: {theme.FS_SMALL}px;
                padding: 12px 0; }}
            QLabel#safety {{ color: {theme.TEXT_SECONDARY}; font-size: {theme.FS_SMALL}px; }}
            QLabel#feedback {{ color: {theme.TEXT_PRIMARY}; font-size: {theme.FS_BASE}px; }}
            QLabel#feedback[correct="true"] {{ color: {theme.ACCENT}; }}
            QPushButton#choice {{ text-align: left; padding: 10px 14px;
                border: 1px solid {theme.BORDER}; background: {theme.BG_SURFACE};
                border-radius: {theme.RADIUS_MD}px; font-weight: 400; }}
            QPushButton#choice:checked {{ border-color: {theme.ACCENT};
                background: {theme.BG_ELEVATED}; }}
            QScrollArea {{ background: transparent; border: none; }}
            QProgressBar {{ background: {theme.BG_ELEVATED}; border: none; border-radius: 3px; }}
            QProgressBar::chunk {{ background: {theme.ACCENT}; border-radius: 3px; }}
        """)
        self.progress.setStyleSheet(
            f"QProgressBar {{ background: {theme.BG_ELEVATED}; border: none; }}"
            f"QProgressBar::chunk {{ background: {theme.ACCENT}; }}"
        )
        for stage in self.findChildren(_Stage):
            stage.update()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    font = Path("C:/Windows/Fonts/msyh.ttc")
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        theme.set_font_family("Microsoft YaHei")
    theme.set_palette("dark")
    window = ModelLearningDemo()
    window.show()
    if args.screenshots:
        from PySide6.QtCore import QTimer
        from PySide6.QtTest import QTest

        args.screenshots.mkdir(parents=True, exist_ok=True)

        def capture() -> None:
            QTest.qWait(100)
            window.grab().save(str(args.screenshots / "01-endpoint-dark.png"))
            window.answer("endpoint")
            window.advance()
            window.grab().save(str(args.screenshots / "02-actual-model-dark.png"))
            window.answer("b")
            window.advance()
            window.logical_id_input.setText("xyz-chat")
            window.name_input.setText("XYZ Chat")
            QTest.qWait(80)
            window.grab().save(str(args.screenshots / "03-logical-id-and-name-dark.png"))
            window.create_logical()
            window.check_unbound()
            window.advance()
            window.answer("bind_a")
            window.answer("bind_b")
            window.advance()
            window.prefer_b()
            window.answer("guess_a")
            window.verify_route()
            QTest.qWait(80)
            window.grab().save(str(args.screenshots / "05-preferred-feedback-dark.png"))
            window.answer("guess_b")
            window.verify_route()
            window.advance()
            window.answer("actual")
            window.advance()
            window.simulate_failure()
            window.answer("auto")
            QTest.qWait(80)
            window.grab().save(str(args.screenshots / "07-failure-feedback-dark.png"))
            window.answer("manual")
            window.select_a()
            window.verify_retry()
            window.advance()
            QTest.qWait(80)
            window.grab().save(str(args.screenshots / "08-complete-dark.png"))
            window.restart()
            window.toggle_theme()
            window.resize(960, 730)
            QTest.qWait(80)
            window.grab().save(str(args.screenshots / "01-endpoint-light-small.png"))
            window.close()
            app.quit()

        QTimer.singleShot(100, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
